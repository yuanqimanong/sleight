"""Capacity is retained until remote cleanup succeeds, including ambiguous starts.

SQLite and PostgreSQL coordinate clients of a Manager. Both stores keep expired records: expiration schedules cleanup rather
than freeing a slot while the browser is still alive.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..core.errors import Busy, InstanceError, NotFound
from ..core.types import InstanceStatus
from ..deploy.inventory import sleight_home
from ..service.database import Database, database

log = logging.getLogger("sleight.runtime")


class SQLiteLedger:
    scope = "host"

    def __init__(self, path: str | Path | None = None, *, db=None) -> None:
        self.path = Path(path) if path else sleight_home() / "control.db"
        self.db = db or (Database("sqlite:///" + str(self.path), import_legacy=False) if path else database())
        self.scope = "shared" if self.db.engine.dialect.name == "postgresql" else "host"

    @contextmanager
    def transaction(self, key: str) -> Iterator[dict[str, Any]]:
        with self.db.state(key) as state:
            yield state

    def now(self):
        return self.db.now()


class Governor:
    def __init__(self, *, resource: str, limit: int = 3, ledger: Any = None,
                 ttl: float = 120, heartbeat: bool = True) -> None:
        if not resource or limit < 1 or ttl < 3:
            raise ValueError("resource, positive limit and ttl >= 3 are required")
        self.resource, self.limit, self.ttl = resource, limit, ttl
        self.ledger = ledger or SQLiteLedger()
        self.owner = uuid.uuid4().hex
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self.heartbeat()
        if heartbeat:
            self._thread = threading.Thread(target=self._beat, daemon=True,
                                            name="sleight-owner-" + self.owner[:8])
            self._thread.start()

    def _beat(self) -> None:
        while not self._closed.wait(self.ttl / 3):
            try:
                self.heartbeat()
            except Exception:
                log.exception("Owner heartbeat failed")

    def heartbeat(self) -> None:
        with self.ledger.transaction(self.resource) as state:
            previous = state.get("limit")
            if previous is not None and previous != self.limit:
                live = any(v > self.ledger.now() for v in state["owners"].values())
                if live or state["records"]:
                    raise InstanceError(
                        f"Shared capacity is {previous}; all clients must use the same limit "
                        "(stop clients and clear profiles before changing it)"
                    )
            state["limit"] = self.limit
            state["owners"][self.owner] = self.ledger.now() + self.ttl
            # Prune owner keys only when no record refers to them.
            used = {r["owner"] for r in state["records"].values()}
            state["owners"] = {k: v for k, v in state["owners"].items()
                               if k in used or v > self.ledger.now()}

    def creation_tags(self) -> tuple[str, ...]:
        return ("sleight-owner:" + self.owner,)

    def created(self, pid: str, *, ephemeral: bool) -> None:
        with self.ledger.transaction(self.resource) as state:
            if pid in state["records"] and state["records"][pid]["owner"] != self.owner:
                raise InstanceError("Instance is owned by another client")
            state["records"].setdefault(pid, {
                "owner": self.owner, "ephemeral": ephemeral, "slot": False,
                "state": "idle", "attempts": 0, "retry_at": 0, "error": "",
            })

    def reserve(self, manager: Any, pid: str) -> None:
        with self.ledger.transaction(self.resource) as state:
            # Actual running profiles include ones started through the official UI.
            actual = {p["id"] for p in manager.list_profiles() if p.get("status") == "running"}
            records = state["records"]
            for key, row in records.items():
                if row["state"] == "running" and key not in actual:
                    row.update(slot=False, state="idle")
            current = records.get(pid)
            if current and current["ephemeral"] and current["owner"] != self.owner:
                raise Busy("Another client owns this ephemeral instance")
            if current and current["state"] in ("cleanup", "cleaning"):
                raise Busy("Instance is awaiting cleanup")
            if current and current["slot"]:
                if current["owner"] != self.owner and (current["state"] == "starting" or current["ephemeral"]):
                    raise Busy("Another client owns this instance")
                return
            occupied = actual | {k for k, r in records.items() if r["slot"]}
            if pid not in occupied and len(occupied) >= self.limit:
                raise Busy(f"Manager capacity {self.limit} reached ({len(occupied)} occupied)")
            if current is None:
                current = {"ephemeral": False, "attempts": 0, "retry_at": 0, "error": ""}
                records[pid] = current
            current.update(owner=self.owner, slot=True, state="starting")

    def started(self, pid: str) -> None:
        with self.ledger.transaction(self.resource) as state:
            if pid in state["records"] and state["records"][pid].get("slot"):
                state["records"][pid]["state"] = "running"

    def release(self, pid: str) -> None:
        with self.ledger.transaction(self.resource) as state:
            row = state["records"].get(pid)
            if row and row["state"] not in ("cleanup", "cleaning"):
                row.update(slot=False, state="idle")

    def deleted(self, pid: str) -> None:
        with self.ledger.transaction(self.resource) as state:
            state["records"].pop(pid, None)

    def request_cleanup(self, pid: str, *, failed_start: bool = False) -> None:
        with self.ledger.transaction(self.resource) as state:
            row = state["records"].get(pid)
            if row and row["owner"] == self.owner and (row["ephemeral"] or failed_start):
                row.update(state="cleanup", retry_at=0)

    def snapshot(self) -> dict[str, Any]:
        with self.ledger.transaction(self.resource) as state:
            return {"resource": self.resource, "limit": self.limit, "scope": self.ledger.scope,
                    "records": json.loads(json.dumps(state["records"]))}

    def collect(self, manager: Any) -> list[str]:
        """Recover owned ephemeral profiles, including lost create responses.

        A collector must keep running independently of the fin worker. Multiple
        collectors can share the ledger; cleanup claims are serialized.
        """
        profiles = manager.list_profiles()
        now = self.ledger.now()
        claims: list[tuple[str, str, bool]] = []
        with self.ledger.transaction(self.resource) as state:
            records, owners = state["records"], state["owners"]
            for p in profiles:
                tags = {t.get("tag", "") for t in p.get("tags", [])}
                owner = next((t.split(":", 1)[1] for t in tags if t.startswith("sleight-owner:")), "")
                if owner and "ephemeral" in tags and p["id"] not in records:
                    records[p["id"]] = {"owner": owner, "ephemeral": True,
                        "slot": p.get("status") == "running", "state": "idle",
                        "retry_at": 0, "attempts": 0, "error": ""}
            for pid, row in records.items():
                expired = owners.get(row["owner"], 0) < now
                abandoned_start = row["state"] == "starting" and expired
                pending = row["state"] == "cleanup" or (
                    row["state"] == "cleaning" and row.get("claim_until", 0) < now)
                if (pending or abandoned_start or (row["ephemeral"] and expired)) and row["retry_at"] <= now:
                    claim = uuid.uuid4().hex
                    row.update(state="cleaning", claim=claim, claim_until=now + 660)
                    claims.append((pid, claim, row["ephemeral"]))
        done = []
        for pid, claim, ephemeral in claims:
            error = ""
            try:
                if ephemeral:
                    manager.delete_profile(pid, force=True)
                elif manager.status(pid) is not InstanceStatus.NOT_FOUND:
                    manager.stop(pid)
            except NotFound:
                pass
            except Exception as exc:
                error = type(exc).__name__  # Never persist a response containing credentials.
                log.warning("Cleanup failed for %s (%s)", pid, error)
            with self.ledger.transaction(self.resource) as state:
                row = state["records"].get(pid)
                if not row:
                    if not error:
                        done.append(pid)
                    continue
                if row.get("claim") != claim:
                    continue
                if error:
                    row["attempts"] += 1
                    row.update(state="cleanup", error=error,
                               retry_at=self.ledger.now() + min(300, 2 ** min(row["attempts"], 8)))
                else:
                    state["records"].pop(pid)
            if not error:
                done.append(pid)
        return done

    def close(self) -> None:
        self._closed.set()
        if self._thread:
            self._thread.join(timeout=2)


_cache: dict[tuple, Governor] = {}
_cache_lock = threading.Lock()


def shared_governor(resource: str, *, limit: int = 3, ttl: float = 120) -> Governor:
    with _cache_lock:
        key = (resource, str(database().url), str(database().root), limit)
        if key not in _cache:
            ledger = SQLiteLedger()
            _cache[key] = Governor(resource=resource, limit=limit, ttl=ttl, ledger=ledger)
        else:
            _cache[key].heartbeat()
        return _cache[key]


def close_governors():
    db = database()
    with _cache_lock:
        for key, governor in list(_cache.items()):
            if key[1:3] == (str(db.url), str(db.root)):
                governor.close()
                del _cache[key]

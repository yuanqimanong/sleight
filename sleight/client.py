"""Authenticated clients for the single Sleight browser service.

Browser actions run on the server; callbacks and screenshot files stay local.
Only creation requests are retryable with their original idempotency key.
"""

from __future__ import annotations

import base64
import dataclasses
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

from .core import errors
from .core.extract import ExtractedDocument
from .core.request import FetchResponse
from .core.resources import NetworkResource
from .core.types import ClearReport, Condition, InstanceInfo, InstanceStatus


class ServiceClient:
    def __init__(self, upstream: str, token: str, *, timeout=90, transport=None):
        url = urlsplit(upstream)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.query or url.fragment:
            raise ValueError("Sleight upstream must be an HTTP(S) URL without credentials")
        if not token:
            raise ValueError("Sleight token is required")
        self.timeout, self.deadline = timeout, None
        self.http = httpx.Client(base_url=upstream.rstrip("/") + "/", timeout=timeout,
                                 headers={"X-Sleight-Token": token}, trust_env=False, transport=transport)
        self.me = self.request("GET", "api/auth/me")
        if self.me.get("api_version") != 1:
            self.http.close()
            raise errors.InstanceError("Sleight service protocol is incompatible; upgrade the service and client together")

    def request(self, method, path, *, timeout=None, **kwargs):
        limit = timeout or self.timeout
        if self.deadline:
            left = self.deadline() - time.monotonic()
            if left <= 0:
                raise errors.TimeoutError("Browser task budget exhausted")
            limit = min(limit, left)
        try:
            response = self.http.request(method, path, timeout=limit, **kwargs)
        except httpx.TimeoutException as exc:
            raise errors.TimeoutError("Sleight request timed out; browser actions are not replayed") from exc
        except httpx.HTTPError as exc:
            raise errors.ConnectionError("Sleight service is unreachable") from exc
        if response.is_error:
            with suppress(ValueError, TypeError):
                detail = response.json().get("detail", {})
                if isinstance(detail, dict):
                    cls = getattr(errors, detail.get("error", ""), None)
                    if isinstance(cls, type) and issubclass(cls, errors.SleightError):
                        raise cls(detail.get("message", "Sleight operation failed"))
            cls = {401: errors.AuthError, 403: errors.AuthError, 404: errors.NotFound,
                   409: errors.Busy, 504: errors.TimeoutError}.get(response.status_code, errors.InstanceError)
            raise cls(f"Sleight request failed ({response.status_code})")
        return response.json() if response.content else None

    def create_profile(self, *, request_id=None, **body):
        return self.request("POST", "api/v1/profiles", json=body,
                            headers={"Idempotency-Key": request_id or uuid.uuid4().hex})

    def session(self, profile: str, *, request_id=None, **options):
        encoded = {k: dataclasses.asdict(v) if dataclasses.is_dataclass(v) else v for k, v in options.items()}
        row = self.request("POST", "api/v1/sessions", json={"profile": profile, "options": encoded},
                           headers={"Idempotency-Key": request_id or uuid.uuid4().hex})
        if row["state"] == "released":
            raise errors.SessionLost("The idempotent request refers to a closed session")
        return RemoteSession(self, row["id"])

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class RemoteObject:
    def __init__(self, session, key, kind):
        self.session, self.key, self.kind = session, key, kind

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in {"blocked", "allowed", "children", "tag", "attrs"} or (name == "by_type" and self.kind == "BlockStats") or (name == "text" and self.kind == "StaticElement"):
            return self.session._rpc("$get", object=self.key, name=name)
        return lambda *args, **kw: self.session._rpc(name, args=args, kwargs=kw, object=self.key)

    def __iter__(self):
        return iter(self.snapshot())

    def __len__(self):
        return len(self.snapshot())


class RemoteCapture(RemoteObject):
    def __init__(self, obj, predicate, callback):
        super().__init__(obj.session, obj.key, obj.kind)
        self.predicate, self.callback, self.seen = predicate, callback, set()
        self.frozen = None

    def snapshot(self):
        rows = self.frozen if self.frozen is not None else self.session._rpc("snapshot", object=self.key)
        rows = [r for r in rows if not self.predicate or self.predicate(r)]
        for row in rows:
            marker = row.request_id
            if marker not in self.seen:
                self.seen.add(marker)
                if self.callback:
                    self.callback(row)
        return rows

    def urls(self, *types):
        return list(dict.fromkeys(r.url for r in self.snapshot() if not types or r.resource_type in types))

    def by_type(self):
        result = {}
        for row in self.snapshot():
            result.setdefault(row.resource_type, []).append(row)
        return result


class RemoteTransport:
    def __init__(self, session):
        self.session = session

    @property
    def closed(self):
        return self.session.closed

    def call(self, method, params=None, **kwargs):
        return self.session._rpc("$cdp", args=[method, params or {}], kwargs=kwargs,
                                 timeout=kwargs.get("timeout", 60))

    def close(self):
        self.session.close()


class RemoteSession:
    def __init__(self, client, sid):
        self.client, self.id = client, sid
        self._closed = threading.Event()
        self._lost = None
        self.transport = RemoteTransport(self)
        self.captures = []
        self.thread = threading.Thread(target=self._heartbeat, daemon=True, name="sleight-client-heartbeat")
        self.thread.start()

    @property
    def closed(self):
        return self._closed.is_set()

    @property
    def target_id(self):
        return self._rpc("$get", name="target_id")

    @property
    def cdp_session_id(self):
        return self._rpc("$get", name="cdp_session_id")

    def _heartbeat(self):
        while not self._closed.wait(20):
            try:
                self.client.request("POST", f"api/v1/sessions/{self.id}/heartbeat", timeout=10)
            except Exception as exc:
                self._lost = exc
                self._closed.set()

    def _encode(self, value):
        if isinstance(value, RemoteObject):
            return {"$object": value.key}
        if isinstance(value, Condition):
            return {"$condition": type(value).__name__, "fields": dataclasses.asdict(value)}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._encode(v) for v in value]
        if isinstance(value, dict):
            return {k: self._encode(v) for k, v in value.items()}
        if callable(value):
            raise ValueError("Python callbacks cannot be sent to the browser service")
        return value

    def _decode(self, value):
        if isinstance(value, list):
            return [self._decode(v) for v in value]
        if isinstance(value, dict):
            if "$bytes" in value:
                return base64.b64decode(value["$bytes"])
            if "$object" in value:
                return RemoteObject(self, value["$object"], value["type"])
            if "$value" in value:
                fields = self._decode(value["fields"])
                cls = {"NetworkResource": NetworkResource, "ClearReport": ClearReport,
                       "FetchResponse": FetchResponse,
                       "ExtractedDocument": ExtractedDocument}.get(value["$value"])
                return cls(**fields) if cls else SimpleNamespace(**fields)
            return {k: self._decode(v) for k, v in value.items()}
        return value

    def _rpc(self, method, *, args=(), kwargs=None, timeout=60, **extra):
        if self.closed:
            raise errors.SessionLost("Remote session is closed or its heartbeat failed") from self._lost
        try:
            value = self.client.request("POST", f"api/v1/sessions/{self.id}/call", timeout=timeout + 5,
                                        json={"method": method, "args": self._encode(args),
                                              "kwargs": self._encode(kwargs or {}), "timeout": timeout, **extra})
        except (errors.LeaseLost, errors.SessionLost) as exc:
            self._lost = exc
            self._closed.set()
            raise
        return self._decode(value)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kw: self._rpc(name, args=args, kwargs=kw, timeout=kw.get("timeout", 60))

    @contextmanager
    def block(self, **kwargs):
        obj = self._rpc("block", kwargs=kwargs)
        try:
            yield obj
        finally:
            self._rpc("$exit", object=obj.key)

    @contextmanager
    def capture_resources(self, *, predicate=None, on_discovered=None, **kwargs):
        obj = RemoteCapture(self._rpc("capture_resources", kwargs=kwargs), predicate, on_discovered)
        self.captures.append(obj)
        try:
            yield obj
        finally:
            obj.frozen = obj.snapshot()
            self.captures.remove(obj)
            self._rpc("$exit", object=obj.key)

    def pump_events(self, duration, *, tick=.25):
        self._rpc("pump_events", args=[duration], kwargs={"tick": tick}, timeout=max(60, duration + 5))
        for capture in self.captures:
            capture.snapshot()

    def screenshot(self, path=None, *, target=None):
        data = self._rpc("screenshot", kwargs={"target": target})
        if path:
            Path(path).write_bytes(data)
        return data

    def fetch(self, url, *, method="GET", headers=None, body=None, timeout=30,
              max_bytes=1024 * 1024):
        from .core.request import request_expression
        request_expression(url, method=method, headers=headers, body=body,
                           timeout=timeout, max_bytes=max_bytes)
        if self.client.deadline:
            timeout = min(timeout, self.client.deadline() - time.monotonic())
            if timeout < .1:
                raise errors.TimeoutError("Browser task budget exhausted")
        return self._rpc("fetch", args=[url], kwargs={"method": method, "headers": headers,
                         "body": body, "timeout": timeout, "max_bytes": max_bytes}, timeout=timeout + 2)

    def probe_exit_ip(self, expression="null", *, timeout=15, endpoints=None):
        return self._rpc("$probe_exit_ip", kwargs={"expression": expression, "timeout": timeout, "endpoints": endpoints},
                         timeout=timeout * 2 + 5)

    def close(self):
        if self.closed and not self._lost:
            return
        self._closed.set()
        with suppress(errors.AuthError, errors.NotFound):
            self.client.request("DELETE", f"api/v1/sessions/{self.id}", timeout=15)
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class RemoteManager:
    """Compatibility facade for crawlers using create → lease → session."""
    remote_service = True

    def __init__(self, upstream, token, *, timeout=90, transport=None):
        self.client = ServiceClient(upstream, token, timeout=timeout, transport=transport)
        self.governor = None
        self.name = "sleight"
        self.fixed_names = self.client.me["user"].get("profile_names", [])

    def create_profile(self, spec=None, *, launch=False, **body):
        if spec is not None:
            body.update(name=spec.name, fingerprint_seed=spec.fingerprint_seed)
        row = self.client.create_profile(**body)
        return self._info(row)

    def _info(self, row):
        return InstanceInfo(row["id"], self.name, name=row["name"], tags=frozenset(["ephemeral"] if row["ephemeral"] else []))

    def list_profiles(self):
        return self.client.request("GET", "api/v1/profiles")

    def profile(self, pid):
        return self.client.request("GET", f"api/v1/profiles/{pid}")

    get_profile = profile

    def delete_profile(self, pid, *, force=False):
        self.client.request("DELETE", f"api/v1/profiles/{pid}")

    def find_profile(self, name):
        return self.client.create_profile(name=name, profile_name=name, ephemeral=False)

    def status(self, pid):
        try:
            row = self.profile(pid)
        except errors.NotFound:
            return InstanceStatus.NOT_FOUND
        return InstanceStatus.RUNNING if row["state"] == "running" else InstanceStatus.STOPPED

    @contextmanager
    def lease(self, *, instance_id=None, **kwargs):
        names = kwargs.get("names") or []
        if not instance_id and not names:
            raise ValueError("Service leases require a profile ID")
        sessions = []
        manager = self
        class Handle:
            info = manager._info(manager.profile(instance_id)) if instance_id else InstanceInfo("", manager.name)
            def session(self, **options):
                candidates = [instance_id] if instance_id else names
                last_error = None
                for candidate in candidates:
                    try:
                        pid = candidate if instance_id else manager.find_profile(candidate)["id"]
                        session = manager.client.session(pid, **options)
                        self.info = manager._info(manager.profile(pid))
                        sessions.append(session)
                        return session
                    except (errors.Busy, errors.NotFound) as exc:
                        last_error = exc
                raise last_error or errors.NotFound("No authorized fixed profile is available")
            def probe_exit_ip(self, expression, *, timeout=15):
                if not sessions:
                    raise errors.SessionLost("Exit-IP probes require an active browser session")
                return sessions[-1].probe_exit_ip(expression, timeout=timeout)
        try:
            yield Handle()
        finally:
            for session in sessions:
                if not session.closed:
                    session.close()

    def close(self):
        self.client.close()

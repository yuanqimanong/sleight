"""Transactional cooperative leases, shared by all service workers."""

import secrets

from ..service.database import database


class DatabaseLease:
    def __init__(self, db=None):
        self.db = db or database()

    def acquire(self, key: str, *, ttl: float):
        if ttl <= 0:
            raise ValueError("Lease TTL must be positive")
        with self.db.state("instance-leases") as state:
            if key in state["records"]:
                return None
            token = secrets.token_hex(16)
            state["records"][key] = {"token": token, "expires": self.db.now() + ttl}
            return token

    def renew(self, key: str, token: str, *, ttl: float):
        if ttl <= 0:
            raise ValueError("Lease TTL must be positive")
        with self.db.state("instance-leases") as state:
            row = state["records"].get(key)
            if not row or row["token"] != token or row["expires"] <= self.db.now():
                return False
            row["expires"] = self.db.now() + ttl
            return True

    def release(self, key: str, token: str):
        with self.db.state("instance-leases") as state:
            if state["records"].get(key, {}).get("token") == token:
                del state["records"][key]

    def cleaned(self, instance_id: str, *, namespace: str | None = None):
        """Only call after the browser is confirmed stopped/deleted."""
        with self.db.state("instance-leases") as state:
            for key in list(state["records"]):
                if (key == namespace + ":" + instance_id) if namespace else key.endswith(":" + instance_id):
                    del state["records"][key]

"""Durable job history; a restart interrupts work instead of pretending it resumes."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ...service.database import Database, database
from ..inventory import sleight_home

log = logging.getLogger("sleight.jobs")


@dataclass
class Job:
    id: str
    kind: str
    host: str
    status: str = "running"
    lines: list[str] = field(default_factory=list)
    log_offset: int = 0
    error: str = ""
    result: Any = None
    started: float = field(default_factory=time.time)
    finished: float = 0

    def public(self) -> dict[str, Any]:
        return asdict(self)


class Jobs:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or sleight_home() / "control.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self.db = Database("sqlite:///" + str(path), import_legacy=False) if path else database()
        for raw in self.db.list("jobs"):
                job = Job(**raw)
                if job.status == "running":
                    job.status, job.error, job.finished = "interrupted", "服务已重启，请检查目标状态后重试", time.time()
                self._jobs[job.id] = job
        for job in self._jobs.values():
            self._save(job)

    def _save(self, job: Job) -> None:
        self.db.put("jobs", job.id, job.public())

    def start(self, kind: str, host: str, work: Callable[[Callable[[str], None]], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, host=host)
        with self._lock:
            self._reap()
            self._jobs[job.id] = job
            self._save(job)

        def say(message: str) -> None:
            with self._lock:
                job.lines.append(message)
                job.log_offset += max(0, len(job.lines) - 1000)
                job.lines = job.lines[-1000:]
                self._save(job)

        def run() -> None:
            try:
                result = work(say)
            except BaseException as exc:
                log.exception("Job %s failed", job.id)
                with self._lock:
                    job.status, job.error = "error", f"{type(exc).__name__}: {exc}"
            else:
                with self._lock:
                    job.status, job.result = "ok", result
            finally:
                with self._lock:
                    job.finished = time.time()
                    self._save(job)

        threading.Thread(target=run, name="sleight-job-" + job.id, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.public() for j in sorted(self._jobs.values(), key=lambda j: -j.started)]

    def _reap(self) -> None:
        finished = sorted((j for j in self._jobs.values() if j.finished), key=lambda j: -j.finished)
        for job in finished[100:]:
            del self._jobs[job.id]
            self.db.delete("jobs", job.id)

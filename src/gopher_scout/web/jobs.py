"""Tiny in-process job runner so UI buttons can trigger long agent runs.

Jobs live only as long as the server process; their *effects* are in the database.
A per-key lock stops two jobs touching the same clone at once.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: int
    label: str
    key: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    status: str = "running"  # running | done | failed
    result: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"


class JobManager:
    def __init__(self, history: int = 30) -> None:
        self._ids = itertools.count(1)
        self._jobs: dict[int, Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._history = history

    def busy(self, key: str) -> bool:
        return any(j.running and j.key == key for j in self._jobs.values())

    def submit(self, label: str, key: str, factory: Callable[[], Awaitable[Any]]) -> Job:
        if self.busy(key):
            raise RuntimeError(f"a job for {key} is already running")
        job = Job(id=next(self._ids), label=label, key=key)
        self._jobs[job.id] = job

        async def runner() -> None:
            try:
                outcome = await factory()
                job.status, job.result = "done", None if outcome is None else str(outcome)
            except Exception as exc:
                log.exception("job %s failed", label)
                job.status, job.result = "failed", str(exc)[:500]
            finally:
                job.finished_at = datetime.now(UTC)

        task = asyncio.create_task(runner())
        self._tasks.add(task)  # keep a reference so the task is not garbage-collected
        task.add_done_callback(self._tasks.discard)
        self._trim()
        return job

    def recent(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)

    def _trim(self) -> None:
        finished = [j for j in self.recent() if not j.running]
        for job in finished[self._history :]:
            self._jobs.pop(job.id, None)

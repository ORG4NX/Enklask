"""Background refresh jobs.

Manual refreshes used to run inside the HTTP request: with the browser-based
sources that meant a spinner for tens of seconds and nothing to show when a
source failed. Each refresh now runs in its own thread, keyed by what it polls
(``all``, ``topic:<id>``, ``search:<id>``). The dashboard starts a job, then
polls its status until it is finished.

Only one job per key runs at a time (a second request joins the running one),
and the last finished job of each key is kept so the UI can say "refreshed
3 min ago, 2 new, Leboncoin failed" without a database round-trip.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    key: str
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    # PollResult (duck-typed: new_count, seen_count, errors, sources_run).
    result: object | None = None
    # Set when the poll function itself raised.
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def errors(self) -> dict[str, str]:
        return dict(getattr(self.result, "errors", {}) or {})

    @property
    def new_count(self) -> int:
        return int(getattr(self.result, "new_count", 0) or 0)

    @property
    def ok(self) -> bool:
        return not self.running and self.error is None and not self.errors


class RefreshJobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Job | None:
        with self._lock:
            return self._jobs.get(key)

    def is_running(self, key: str) -> bool:
        job = self.get(key)
        return job is not None and job.running

    def start(self, key: str, fn: Callable[[], object]) -> Job:
        """Run ``fn`` in a daemon thread under ``key``.

        Returns the already-running job for that key instead of starting a
        second one, so hammering the button never piles up polls.
        """
        with self._lock:
            current = self._jobs.get(key)
            if current is not None and current.running:
                return current
            job = Job(key=key)
            self._jobs[key] = job

        thread = threading.Thread(
            target=self._run, args=(job, fn), name=f"refresh-{key}", daemon=True
        )
        thread.start()
        return job

    def run_blocking(self, key: str, fn: Callable[[], object]) -> Job | None:
        """Run ``fn`` on the calling thread, registered under ``key`` so the
        dashboard can see it (used by the scheduler). Returns None, without
        running, if a job with that key is already in flight."""
        with self._lock:
            current = self._jobs.get(key)
            if current is not None and current.running:
                return None
            job = Job(key=key)
            self._jobs[key] = job
        self._run(job, fn)
        return job

    @staticmethod
    def _run(job: Job, fn: Callable[[], object]) -> None:
        try:
            job.result = fn()
        except Exception as exc:  # the job must always finish
            logger.exception("Refresh job %s failed", job.key)
            job.error = str(exc) or type(exc).__name__
        finally:
            job.finished_at = _now()

    def clear(self) -> None:
        """Forget every job (tests)."""
        with self._lock:
            self._jobs.clear()


jobs = RefreshJobs()


def topic_key(topic_id: int) -> str:
    return f"topic:{topic_id}"


def search_key(search_id: int) -> str:
    return f"search:{search_id}"


ALL_KEY = "all"

"""Run the workbook export on a worker thread so the app stays usable.

Building the workbook walks every model year, resource pool and fuel, which
takes long enough that blocking the script run would freeze the interface.
The worker here is deliberately free of any Streamlit dependency: it only
touches pandas and openpyxl, so it needs no script run context and can safely
publish progress through a plain lock-guarded record.
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkbookProgress:
    """A point-in-time view of how far the export has advanced."""

    stage: str
    done: int
    total: int

    @property
    def fraction(self) -> float:
        return min(max(self.done / self.total, 0.0), 1.0) if self.total else 0.0


class ProgressReporter:
    """Thread-safe progress record shared between the worker and the app."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._progress = WorkbookProgress("Starting", 0, 0)

    def set(self, stage: str, done: int = 0, total: int = 0) -> None:
        with self._lock:
            self._progress = WorkbookProgress(stage, done, total)

    def advance(self, done: int, total: int) -> None:
        with self._lock:
            self._progress = WorkbookProgress(self._progress.stage, done, total)

    def snapshot(self) -> WorkbookProgress:
        with self._lock:
            return self._progress


class WorkbookJob:
    """One background workbook build, owned by a single browser session."""

    def __init__(self, builder) -> None:
        self._reporter = ProgressReporter()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workbook")
        self._future: Future = self._executor.submit(builder, self._reporter)
        self._future.add_done_callback(lambda _: self._executor.shutdown(wait=False))

    @property
    def running(self) -> bool:
        return not self._future.done()

    @property
    def progress(self) -> WorkbookProgress:
        return self._reporter.snapshot()

    def result(self) -> bytes:
        """Return the finished workbook, re-raising any failure from the worker."""
        return self._future.result()

    @property
    def error(self) -> BaseException | None:
        if self._future.done() and not self._future.cancelled():
            return self._future.exception()
        return None

    def cancel(self) -> None:
        """Stop waiting on the job and release its worker thread."""
        self._future.cancel()
        self._executor.shutdown(wait=False)


def start_workbook_job(builder) -> WorkbookJob:
    """Start ``builder(reporter)`` on a worker thread and return its handle."""
    return WorkbookJob(builder)


def cancel_all(jobs: dict | None) -> None:
    """Cancel every job held for a session, ignoring ones already finished."""
    for job in list((jobs or {}).values()):
        try:
            job.cancel()
        except Exception:  # pragma: no cover - a finished job cannot be cancelled
            continue

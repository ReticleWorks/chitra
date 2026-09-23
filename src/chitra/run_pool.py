"""A bounded worker pool with one run in flight per key.

This is the Unreal Agent async-operation shape Chitra uses for every slow
call the monitor loop must never wait on: the pass writes an in-progress
record (or relies on the pool's in-flight marker), submits the work here,
and consumes the worker's durable result record on a later pass. A lane key
that already has a run queued or executing is not requeued, so slow calls
cannot pile up on a lane that is still settling. ``attempts`` counts runs
queued since the caller last consumed a result, which bounds both a worker
that dies before recording and a record that is always stale.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait

import structlog

logger = structlog.get_logger()


class RunPool:
    """Bounded executor: at most one operation in flight per key.

    ``on_complete`` runs on the worker thread right after a run finishes and
    is the wake signal a daemon loop can sleep on instead of a fixed poll.
    """

    def __init__(
        self,
        max_workers: int = 4,
        *,
        thread_name_prefix: str = "chitra-worker",
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._on_complete = on_complete
        self._executor = self._new_executor()
        self._lock = threading.Lock()
        self._in_flight: dict[str, Future[None]] = {}
        self._attempts: dict[str, int] = {}

    def submit(self, key: str, work: Callable[[], None]) -> None:
        """Queue ``work`` unless this key already has a run in flight."""
        with self._lock:
            current = self._in_flight.get(key)
            if current is not None and not current.done():
                return
            self._attempts[key] = self._attempts.get(key, 0) + 1
            future = self._executor.submit(work)
            self._in_flight[key] = future
        future.add_done_callback(lambda done: self._completed(key, done))

    def _new_executor(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix=self._thread_name_prefix)

    def in_flight(self, key: str) -> bool:
        """Return whether this key has a queued or running operation."""
        with self._lock:
            current = self._in_flight.get(key)
            return current is not None and not current.done()

    def _completed(self, key: str, future: Future[None]) -> None:
        try:
            future.result()
        except Exception as exc:
            logger.error("run_pool_work_failed", key=key, error=str(exc))
        finally:
            with self._lock:
                if self._in_flight.get(key) is future:
                    self._in_flight.pop(key, None)
            if self._on_complete is not None:
                try:
                    self._on_complete()
                except Exception:  # noqa: BLE001 -- a wake hook must never kill a worker
                    logger.warning("run_pool_wake_hook_failed", key=key)

    def attempts(self, key: str) -> int:
        """Return how many runs this key queued since its last consumed result."""
        with self._lock:
            return self._attempts.get(key, 0)

    def reset(self, key: str) -> None:
        """Clear the attempt count once the loop has consumed a result."""
        with self._lock:
            self._attempts.pop(key, None)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no key has a run in flight; used by tests and shutdown."""
        with self._lock:
            futures = [future for future in self._in_flight.values() if not future.done()]
        if not futures:
            return True
        _finished, pending = wait(futures, timeout=timeout)
        return not pending

    def shutdown(self) -> None:
        """Cancel queued runs without blocking exit on running work.

        The module-level pool outlives one ``run_forever`` call, so a fresh
        executor replaces the stopped one; a later pass in the same process
        can still queue work instead of failing on a shut-down executor.
        """
        with self._lock:
            stopped, self._executor = self._executor, self._new_executor()
        stopped.shutdown(wait=False, cancel_futures=True)

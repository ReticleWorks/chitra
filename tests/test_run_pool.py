"""Unit tests for the shared bounded worker pool."""

from __future__ import annotations

import threading

from chitra.run_pool import RunPool


def test_one_run_in_flight_per_key() -> None:
    pool = RunPool(max_workers=4)
    release = threading.Event()
    ran: list[str] = []

    def work() -> None:
        release.wait(5)
        ran.append("ran")

    pool.submit("lane", work)
    pool.submit("lane", work)  # dropped: a run is already in flight for this key
    release.set()

    assert pool.wait_idle(timeout=5)
    assert ran == ["ran"]

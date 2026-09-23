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
    assert pool.attempts("lane") == 1


def test_attempts_count_resubmissions_until_reset() -> None:
    pool = RunPool(max_workers=1)

    pool.submit("lane", lambda: None)
    assert pool.wait_idle(timeout=5)
    pool.submit("lane", lambda: None)
    assert pool.wait_idle(timeout=5)
    assert pool.attempts("lane") == 2

    pool.reset("lane")
    assert pool.attempts("lane") == 0


def test_on_complete_fires_and_failed_work_frees_the_key() -> None:
    done = threading.Event()
    pool = RunPool(max_workers=1, on_complete=done.set)

    def boom() -> None:
        raise RuntimeError("worker died")

    pool.submit("lane", boom)
    assert done.wait(5)
    assert pool.wait_idle(timeout=5)
    assert not pool.in_flight("lane")

    # A dead worker does not lock the key forever.
    ran: list[str] = []
    pool.submit("lane", lambda: ran.append("ran"))
    assert pool.wait_idle(timeout=5)
    assert ran == ["ran"]


def test_pool_is_bounded_but_every_key_still_runs() -> None:
    pool = RunPool(max_workers=2)
    ran: list[str] = []

    for index in range(6):
        pool.submit(f"lane-{index}", lambda index=index: ran.append(f"lane-{index}"))

    assert pool.wait_idle(timeout=15)
    assert sorted(ran) == [f"lane-{index}" for index in range(6)]

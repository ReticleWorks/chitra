"""Regression tests for goal changes while dispatchd waits on a lane lock."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _goal_fixtures import enrollment_fields

import chitra.dispatchd as dispatchd_mod
from chitra.dispatch import LaneLock
from chitra.dispatchd import run_once
from chitra.goals import GoalRecord, get_goal, hold_goal, redirect_goal, upsert_goal
from chitra.orders import DispatchOrder, DispatchResult, DispatchStatus
from chitra.supervision import goal_digest


def _goal(session_ref: str) -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Ship the exact tested implementation safely through acceptance.",
        done_when="The acceptance tests pass.",
        source="task",
        status="working",
        **enrollment_fields("The acceptance tests pass."),
    )


def _run_goal_change_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate_goal: Callable[[Path, str], None],
) -> tuple[DispatchResult, list[str]]:
    session_ref = "host-a:persistent-oversight:0.0"
    goals_root = tmp_path / "goals"
    current = upsert_goal(goals_root, _goal(session_ref))
    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    order = DispatchOrder(
        order_id="goal-lock-race",
        session_ref=session_ref,
        nudge="Continue the exact enrolled goal.",
        task_type="persistent-oversight",
        goal_version=current.goal_version,
        goal_digest=goal_digest(current),
    )
    (orders_dir / f"{order.order_id}.json").write_text(order.model_dump_json(), encoding="utf-8")

    deliveries: list[str] = []

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        deliveries.append(order.order_id)
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            status=DispatchStatus.SENT,
        )

    real_acquire = LaneLock.acquire
    changed = False

    def acquire_then_change_goal(self: LaneLock, **kwargs: Any) -> bool:
        nonlocal changed
        acquired = real_acquire(self, **kwargs)
        if not changed:
            changed = True
            mutate_goal(goals_root, session_ref)
        return acquired

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    monkeypatch.setattr(LaneLock, "acquire", acquire_then_change_goal)

    result = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        goals_root=goals_root,
    )[0]
    return result, deliveries


def test_goal_redirect_while_waiting_on_lane_lock_blocks_stale_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def redirect(root: Path, session_ref: str) -> None:
        redirect_goal(
            root,
            session_ref,
            reason="the exact strategic target changed while the order waited",
            goal="Ship the revised exact implementation safely through acceptance.",
        )

    result, deliveries = _run_goal_change_race(tmp_path, monkeypatch, redirect)

    assert result.status is DispatchStatus.BLOCKED
    assert result.reason == "stale-goal-contract"
    assert deliveries == []


def test_goal_hold_while_waiting_on_lane_lock_blocks_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hold(root: Path, session_ref: str) -> None:
        hold_goal(root, session_ref, reason="a protected decision now requires review")

    result, deliveries = _run_goal_change_race(tmp_path, monkeypatch, hold)

    assert result.status is DispatchStatus.BLOCKED
    assert result.reason == "goal-not-actionable"
    assert deliveries == []


def test_concurrent_goal_hold_waits_until_dispatch_paste_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hold using dispatchd's lock cannot land between recheck and paste."""
    session_ref = "host-a:persistent-oversight:0.0"
    goals_root = tmp_path / "goals"
    current = upsert_goal(goals_root, _goal(session_ref))
    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    order = DispatchOrder(
        order_id="goal-lock-threaded-race",
        session_ref=session_ref,
        nudge="Continue the exact enrolled goal.",
        # This test isolates the shared goal/lane lock. Strict autonomous
        # transcript binding has its own dispatchd tests.
        task_type="goal-lock-race",
        goal_version=current.goal_version,
        goal_digest=goal_digest(current),
    )
    (orders_dir / f"{order.order_id}.json").write_text(order.model_dump_json(), encoding="utf-8")

    lock_dir = tmp_path / "locks"
    started = threading.Event()
    finished = threading.Event()
    workers: list[threading.Thread] = []

    def fake_dispatch(dispatch_order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        assert not finished.is_set()
        return DispatchResult(order_id=dispatch_order.order_id, session_ref=dispatch_order.session_ref, status=DispatchStatus.SENT)

    real_acquire = LaneLock.acquire
    hold_started = False

    def acquire_and_start_hold(self: LaneLock, **kwargs: Any) -> bool:
        nonlocal hold_started
        acquired = real_acquire(self, **kwargs)
        if hold_started:
            return acquired
        hold_started = True

        def hold() -> None:
            started.set()
            hold_goal(goals_root, session_ref, reason="operator review is required", lock_dir=lock_dir)
            finished.set()

        worker = threading.Thread(target=hold)
        workers.append(worker)
        worker.start()
        assert started.wait(timeout=2)
        return acquired

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    monkeypatch.setattr(LaneLock, "acquire", acquire_and_start_hold)

    result = run_once(
        queue_dir,
        lock_dir=lock_dir,
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        goals_root=goals_root,
    )[0]
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert result.status is DispatchStatus.SENT
    assert finished.is_set()
    stored = get_goal(goals_root, session_ref)
    assert stored is not None
    assert stored.status == "held"

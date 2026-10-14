"""Dispatchd gates queued orders against the four-state lane lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import chitra.dispatchd as dispatchd_mod
from chitra.dispatch import DispatchResult, DispatchStatus
from chitra.dispatchd import run_once
from chitra.orders import NATIVE_CONTROL_PAUSE_PRUNE_TASK_TYPE, DispatchOrder

SESSION_REF = "host-b:lane-lifecycle:0.0"


def _write_order(queue_dir: Path, order: DispatchOrder) -> None:
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True, exist_ok=True)
    (orders_dir / f"{order.order_id}.json").write_text(order.model_dump_json(), encoding="utf-8")


def _run_once(
    queue_dir: Path,
    state_root: Path,
    *,
    dispatch_runner: Any | None = None,
) -> list[DispatchResult]:
    return run_once(
        queue_dir,
        lock_dir=state_root / "locks",
        ledger_path=state_root / "ledger.jsonl",
        ledger_key_path=state_root / "ledger.key",
        goals_root=state_root,
        dispatch_runner=dispatch_runner,
    )


def _sent(order: DispatchOrder, **_: Any) -> DispatchResult:
    return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)


def test_queued_order_is_deferred_when_pause_lands_before_lock_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir = tmp_path / "queue"
    state_root = tmp_path / "state"
    order = DispatchOrder(order_id="queued-before-pause", session_ref=SESSION_REF, nudge="continue the goal")
    _write_order(queue_dir, order)
    calls: list[str] = []
    acquired: list[bool] = []

    def fake_dispatch(dispatched: DispatchOrder, **_: Any) -> DispatchResult:
        calls.append(dispatched.order_id)
        return _sent(dispatched)

    original_acquire = dispatchd_mod.LaneLock.acquire

    def acquire_after_lock(self: Any, *args: Any, **kwargs: Any) -> Any:
        acquired.append(True)
        return original_acquire(self, *args, **kwargs)

    def paused_lifecycle(*_: Any) -> Any:
        assert acquired, "lifecycle must be re-read only after the lane lock is held"
        return SimpleNamespace(state="paused")

    monkeypatch.setattr(dispatchd_mod.LaneLock, "acquire", acquire_after_lock)
    monkeypatch.setattr(dispatchd_mod, "get_lane_lifecycle", paused_lifecycle)
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    results = _run_once(queue_dir, state_root)

    assert results[0].status == DispatchStatus.DEFERRED
    assert results[0].reason == "lane-lifecycle-paused-deferred"
    assert calls == []
    assert (queue_dir / "deferred" / f"{order.order_id}.json").exists()
    assert not (queue_dir / "results" / f"{order.order_id}.json").exists()


def test_lifecycle_deferral_requeues_and_delivers_after_active_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir = tmp_path / "queue"
    state_root = tmp_path / "state"
    order = DispatchOrder(order_id="resume-deferred", session_ref=SESSION_REF, nudge="continue after resume")
    _write_order(queue_dir, order)
    lifecycle_state = "paused"
    calls: list[str] = []

    def fake_dispatch(dispatched: DispatchOrder, **_: Any) -> DispatchResult:
        calls.append(dispatched.order_id)
        return _sent(dispatched)

    monkeypatch.setattr(
        dispatchd_mod,
        "get_lane_lifecycle",
        lambda *_: SimpleNamespace(state=lifecycle_state),
    )
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    first = _run_once(queue_dir, state_root)
    assert first[0].status == DispatchStatus.DEFERRED
    assert calls == []

    lifecycle_state = "active"
    second = _run_once(queue_dir, state_root)

    assert second[0].status == DispatchStatus.SENT
    assert calls == [order.order_id]
    assert (queue_dir / "processed" / f"{order.order_id}.json").exists()
    assert not (queue_dir / "deferred" / f"{order.order_id}.json").exists()


def test_only_typed_pause_prune_control_can_deliver_while_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir = tmp_path / "queue"
    state_root = tmp_path / "state"
    order = DispatchOrder(
        order_id="pause-prune",
        session_ref=SESSION_REF,
        nudge="remove the stale recurring enforcement hook",
        task_type=NATIVE_CONTROL_PAUSE_PRUNE_TASK_TYPE,
    )
    _write_order(queue_dir, order)
    calls: list[str] = []

    def fake_dispatch(dispatched: DispatchOrder, **_: Any) -> DispatchResult:
        calls.append(dispatched.order_id)
        return _sent(dispatched)

    monkeypatch.setattr(dispatchd_mod, "get_lane_lifecycle", lambda *_: SimpleNamespace(state="paused"))
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    results = _run_once(queue_dir, state_root)

    assert results[0].status == DispatchStatus.SENT
    assert calls == [order.order_id]


def test_pause_prune_control_stays_deferred_while_shelved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir = tmp_path / "queue"
    state_root = tmp_path / "state"
    order = DispatchOrder(
        order_id="shelved-pause-prune",
        session_ref=SESSION_REF,
        nudge="remove the stale recurring enforcement hook",
        task_type=NATIVE_CONTROL_PAUSE_PRUNE_TASK_TYPE,
    )
    _write_order(queue_dir, order)
    calls: list[str] = []

    def fake_dispatch(dispatched: DispatchOrder, **_: Any) -> DispatchResult:
        calls.append(dispatched.order_id)
        return _sent(dispatched)

    monkeypatch.setattr(dispatchd_mod, "get_lane_lifecycle", lambda *_: SimpleNamespace(state="shelved"))
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    results = _run_once(queue_dir, state_root)

    assert results[0].status == DispatchStatus.DEFERRED
    assert calls == []
    assert (queue_dir / "deferred" / f"{order.order_id}.json").exists()


@pytest.mark.parametrize("state", ["shelved", "closed"])
def test_shelved_and_closed_lanes_never_paste_ordinary_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    queue_dir = tmp_path / "queue"
    state_root = tmp_path / "state"
    order = DispatchOrder(order_id=f"no-paste-{state}", session_ref=SESSION_REF, nudge="ordinary work")
    _write_order(queue_dir, order)
    calls: list[str] = []

    def fake_dispatch(dispatched: DispatchOrder, **_: Any) -> DispatchResult:
        calls.append(dispatched.order_id)
        return _sent(dispatched)

    monkeypatch.setattr(dispatchd_mod, "get_lane_lifecycle", lambda *_: SimpleNamespace(state=state))
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    results = _run_once(queue_dir, state_root)

    assert calls == []
    if state == "shelved":
        assert results[0].status == DispatchStatus.DEFERRED
        assert (queue_dir / "deferred" / f"{order.order_id}.json").exists()
        assert not (queue_dir / "results" / f"{order.order_id}.json").exists()
    else:
        assert results[0].status == DispatchStatus.BLOCKED
        assert results[0].reason == "lane-lifecycle-closed"
        assert (queue_dir / "results" / f"{order.order_id}.json").exists()
        assert (queue_dir / "processed" / f"{order.order_id}.json").exists()


def test_shelved_backlog_is_reconciled_to_audit_when_lane_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir = tmp_path / "queue"
    state_root = tmp_path / "state"
    order = DispatchOrder(order_id="shelved-then-closed", session_ref=SESSION_REF, nudge="ordinary work")
    _write_order(queue_dir, order)
    lifecycle_state = "shelved"
    calls: list[str] = []

    def fake_dispatch(dispatched: DispatchOrder, **_: Any) -> DispatchResult:
        calls.append(dispatched.order_id)
        return _sent(dispatched)

    monkeypatch.setattr(
        dispatchd_mod,
        "get_lane_lifecycle",
        lambda *_: SimpleNamespace(state=lifecycle_state),
    )
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    first = _run_once(queue_dir, state_root)
    assert first[0].status == DispatchStatus.DEFERRED

    lifecycle_state = "closed"
    second = _run_once(queue_dir, state_root)

    assert second[0].status == DispatchStatus.BLOCKED
    assert second[0].reason == "lane-lifecycle-closed"
    assert calls == []
    assert (queue_dir / "results" / f"{order.order_id}.json").exists()
    assert (queue_dir / "processed" / f"{order.order_id}.json").exists()


def test_active_lifecycle_recheck_still_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir = tmp_path / "queue"
    state_root = tmp_path / "state"
    order = DispatchOrder(order_id="active-recheck", session_ref=SESSION_REF, nudge="continue active work")
    _write_order(queue_dir, order)
    calls: list[str] = []

    def fake_dispatch(dispatched: DispatchOrder, **_: Any) -> DispatchResult:
        calls.append(dispatched.order_id)
        return _sent(dispatched)

    monkeypatch.setattr(dispatchd_mod, "get_lane_lifecycle", lambda *_: SimpleNamespace(state="active"))
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    results = _run_once(queue_dir, state_root)

    assert results[0].status == DispatchStatus.SENT
    assert calls == [order.order_id]


def _write_lane_record(lanes_root: Path, lane: str, record: dict[str, Any]) -> None:
    lane_dir = lanes_root / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    (lane_dir / "lane.json").write_text(json.dumps(record), encoding="utf-8")


def test_nonce_order_for_lane_with_no_live_owner_reaches_terminal_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash-reconciling order terminalizes when its lane has no live owner.

    Regression for the dispatchd retry loop: a nonce-bearing order whose
    target lane's lane-run record reports a dead/ended owner used to be
    DELIVERY_UNCONFIRMED forever (it could not be pasted again and no
    consumption proof could ever appear). It must finalize BLOCKED and
    clear the nonce so the queue stops retrying it.
    """
    queue_dir = tmp_path / "queue"
    state_root = tmp_path / "state"
    lanes_root = tmp_path / "lanes"
    order = DispatchOrder(order_id="retired-owner", session_ref="cx-dead-lane", nudge="continue the goal")
    _write_order(queue_dir, order)
    in_flight = queue_dir / "in_flight"
    in_flight.mkdir(parents=True, exist_ok=True)
    (in_flight / f".{order.order_id}.nonce").write_text("{}", encoding="utf-8")
    _write_lane_record(
        lanes_root,
        "cx-dead-lane",
        {"state": "failed", "ended_at": "2026-10-12T11:30:58+00:00", "owner_session_id": 999_999_999},
    )
    calls: list[str] = []

    def fake_dispatch(dispatched: DispatchOrder, **_: Any) -> DispatchResult:
        calls.append(dispatched.order_id)
        return _sent(dispatched)

    monkeypatch.setenv("AGENT_TOOLS_LANE_ROOT", str(lanes_root))
    monkeypatch.setattr(dispatchd_mod, "get_lane_lifecycle", lambda *_: SimpleNamespace(state="active"))
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    results = _run_once(queue_dir, state_root)

    assert calls == []
    assert results[0].status == DispatchStatus.BLOCKED
    assert results[0].reason == "target lane retired; order abandoned"
    assert (queue_dir / "processed" / f"{order.order_id}.json").exists()
    assert (queue_dir / "results" / f"{order.order_id}.json").exists()
    assert not (in_flight / f".{order.order_id}.nonce").exists()


def test_order_for_lane_with_live_owner_still_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner check must not terminalize a lane whose owner is alive."""
    queue_dir = tmp_path / "queue"
    state_root = tmp_path / "state"
    lanes_root = tmp_path / "lanes"
    order = DispatchOrder(order_id="live-owner", session_ref="cx-live-lane", nudge="continue the goal")
    _write_order(queue_dir, order)
    _write_lane_record(
        lanes_root,
        "cx-live-lane",
        {"state": "running", "owner_session_id": os.getpid(), "worker_pid": 999_999_999},
    )
    calls: list[str] = []

    def fake_dispatch(dispatched: DispatchOrder, **_: Any) -> DispatchResult:
        calls.append(dispatched.order_id)
        return _sent(dispatched)

    monkeypatch.setenv("AGENT_TOOLS_LANE_ROOT", str(lanes_root))
    monkeypatch.setattr(dispatchd_mod, "get_lane_lifecycle", lambda *_: SimpleNamespace(state="active"))
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    results = _run_once(queue_dir, state_root)

    assert results[0].status == DispatchStatus.SENT
    assert calls == [order.order_id]
    assert (queue_dir / "processed" / f"{order.order_id}.json").exists()

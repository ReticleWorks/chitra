"""Fail-closed tests for autonomous goal-bound dispatch orders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _goal_fixtures import enrollment_fields
from pydantic import ValidationError

import chitra.dispatchd as dispatchd_mod
from chitra.dispatch import DispatchOrder, DispatchStatus
from chitra.goals import GoalRecord, GoalsSchemaNewerError, upsert_goal
from chitra.question_handler import handle_question
from chitra.supervision import goal_digest


def _goal(session_ref: str = "host-b:binding:0.0") -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Ship the bounded feature safely for the enrolled session.",
        done_when="The focused tests pass.",
        source="task:test",
        status="working",
        **enrollment_fields("The focused tests pass."),
    )


def _write_order(queue_dir: Path, order: DispatchOrder) -> Path:
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True, exist_ok=True)
    path = orders_dir / f"{order.order_id}.json"
    path.write_text(order.model_dump_json(), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "fields",
    [
        {"goal_version": None, "goal_digest": "a" * 64},
        {"goal_version": 1, "goal_digest": None},
    ],
)
def test_persistent_oversight_requires_complete_goal_binding(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="exact goal contract binding"):
        DispatchOrder(
            order_id="unbound-persistent",
            session_ref="host-b:binding:0.0",
            nudge="continue",
            task_type="persistent-oversight",
            **fields,
        )


def test_goal_contract_answer_requires_complete_goal_binding() -> None:
    goal = _goal()
    question_result = handle_question(goal, "What proves the goal is done?")
    assert question_result.answer is not None
    with pytest.raises(ValidationError, match="exact goal contract binding"):
        DispatchOrder(
            order_id="unbound-answer",
            session_ref=goal.session_ref,
            nudge=question_result.answer,
            message_kind="goal_contract_answer",
            question_result=question_result,
        )


def test_unbound_persistent_order_is_quarantined_after_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed autonomous orders cannot wedge a claimed file or reach tmux."""
    calls: list[str] = []
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", lambda order, **kwargs: calls.append(order.nudge))

    queue_dir = tmp_path / "queue"
    source = DispatchOrder(order_id="unbound-source", session_ref="host-b:binding:0.0", nudge="continue")
    payload = source.model_dump(mode="json")
    payload.update(task_type="persistent-oversight", goal_version=None, goal_digest=None)
    order_path = queue_dir / "orders" / "unbound-persistent.json"
    order_path.parent.mkdir(parents=True)
    order_path.write_text(json.dumps(payload), encoding="utf-8")

    results = dispatchd_mod.run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].status == DispatchStatus.FAILED
    assert results[0].reason.startswith("invalid-order:")
    assert calls == []
    assert not list((queue_dir / "in_flight").glob("*.json"))
    assert (queue_dir / "invalid" / order_path.name).exists()


def test_newer_goal_schema_pre_lock_rejection_finalizes_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_ref = "host-b:newer-schema:0.0"
    goals_root = tmp_path / "goals"
    goal = upsert_goal(goals_root, _goal(session_ref))
    order = DispatchOrder(
        order_id="newer-pre-lock",
        session_ref=session_ref,
        nudge="continue",
        task_type="persistent-oversight",
        goal_version=goal.goal_version,
        goal_digest=goal_digest(goal),
    )
    queue_dir = tmp_path / "queue"
    order_path = _write_order(queue_dir, order)
    calls = {"get_goal": 0, "dispatch": 0}

    def newer_goal(*args: Any, **kwargs: Any) -> None:
        calls["get_goal"] += 1
        raise GoalsSchemaNewerError("newer goals schema")

    monkeypatch.setattr(dispatchd_mod, "get_goal", newer_goal)
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", lambda *args, **kwargs: calls.__setitem__("dispatch", 1))

    result = dispatchd_mod.run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        goals_root=goals_root,
    )[0]

    assert result.status == DispatchStatus.BLOCKED
    assert result.reason == "goals-schema-newer-than-installed"
    assert calls == {"get_goal": 1, "dispatch": 0}
    assert not order_path.exists()
    assert (queue_dir / "processed" / order_path.name).exists()
    assert not list((queue_dir / "in_flight").glob("*.json"))


def test_newer_goal_schema_under_lock_rejection_finalizes_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_ref = "host-b:newer-schema-lock:0.0"
    goals_root = tmp_path / "goals"
    goal = upsert_goal(goals_root, _goal(session_ref))
    order = DispatchOrder(
        order_id="newer-under-lock",
        session_ref=session_ref,
        nudge="continue",
        task_type="persistent-oversight",
        goal_version=goal.goal_version,
        goal_digest=goal_digest(goal),
    )
    queue_dir = tmp_path / "queue"
    order_path = _write_order(queue_dir, order)
    calls = {"get_goal": 0, "dispatch": 0}

    def newer_after_pre_lock(*args: Any, **kwargs: Any) -> GoalRecord | None:
        calls["get_goal"] += 1
        if calls["get_goal"] == 1:
            return goal
        raise GoalsSchemaNewerError("newer goals schema")

    monkeypatch.setattr(dispatchd_mod, "get_goal", newer_after_pre_lock)
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", lambda *args, **kwargs: calls.__setitem__("dispatch", 1))

    result = dispatchd_mod.run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        goals_root=goals_root,
    )[0]

    assert result.status == DispatchStatus.BLOCKED
    assert result.reason == "goals-schema-newer-than-installed"
    assert calls == {"get_goal": 2, "dispatch": 0}
    assert not order_path.exists()
    assert (queue_dir / "processed" / order_path.name).exists()
    assert not list((queue_dir / "in_flight").glob("*.json"))

"""Tests for the durable operator-answer transaction (board answer → lane)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra.decisions import read_decisions
from chitra.goals import GoalRecord, add_ask, add_foreground_task, hold_goal, upsert_goal
from chitra.operator_answer import QUESTION_HOLD_PREFIX, record_operator_answer
from chitra.orders import DispatchOrder
from chitra.supervision import goal_digest


def _goal(session_ref: str = "host-b:feeds-9:0.0") -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Ship the tested feature to production safely.",
        done_when="Tests pass.",
        source="task",
        status="working",
        **enrollment_fields("Tests pass."),
    )


def _orders(queue_dir: Path) -> list[DispatchOrder]:
    orders_dir = queue_dir / "orders"
    if not orders_dir.is_dir():
        return []
    return [DispatchOrder.model_validate_json(path.read_text()) for path in orders_dir.glob("*.json")]


def test_answer_on_question_held_lane_rules_retires_relays_and_resumes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    goal = upsert_goal(root, _goal())
    ask = "the question requests operator-controlled authority: credentials. Gates: credentials. Question: May I use the prod key?"
    add_ask(root, goal.session_ref, ask)
    hold_goal(root, goal.session_ref, reason=f"{QUESTION_HOLD_PREFIX} credentials")

    stored = record_operator_answer(root, goal.session_ref, "Use the staging key only.", queue_dir=tmp_path / "queue")

    decisions = read_decisions(root / "decisions.jsonl")
    assert len(decisions) == 1
    ruling = decisions[0]
    assert ruling.session_ref == goal.session_ref
    assert ruling.goal_version == goal.goal_version
    assert ruling.goal_digest == goal_digest(goal)
    assert ruling.answer == "Use the staging key only."
    assert ruling.question

    assert stored.open_asks == ()
    assert stored.retired_asks[-1]["state"] == "resolved-by-operator"
    assert stored.retired_asks[-1]["basis"] == "Use the staging key only."
    assert stored.retired_asks[-1]["citation"] == f"decision:{ruling.decision_id}"
    assert stored.status == "working"
    assert stored.hold_reason == ""

    orders = _orders(tmp_path / "queue")
    assert len(orders) == 1
    order = orders[0]
    assert order.message_kind == "operator_relay"
    assert order.nudge == "[O] Use the staging key only."
    assert order.tag == "[O]"
    assert order.session_ref == goal.session_ref
    assert order.goal_version == goal.goal_version
    assert order.goal_digest == goal_digest(goal)


def test_resubmitting_the_same_answer_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "state"
    goal = upsert_goal(root, _goal())
    add_ask(root, goal.session_ref, "Which cache backend?")

    record_operator_answer(root, goal.session_ref, "Use sqlite.", queue_dir=tmp_path / "queue")
    again = record_operator_answer(root, goal.session_ref, "Use sqlite.", queue_dir=tmp_path / "queue")

    assert len(read_decisions(root / "decisions.jsonl")) == 1
    assert len(_orders(tmp_path / "queue")) == 1
    # The second call does not fabricate a second retirement record.
    assert len(again.retired_asks) == 1


def test_answer_without_open_asks_relays_without_fabricating_a_retirement(tmp_path: Path) -> None:
    root = tmp_path / "state"
    goal = upsert_goal(root, _goal())

    stored = record_operator_answer(root, goal.session_ref, "Unblock it.", queue_dir=tmp_path / "queue")

    assert stored.retired_asks == ()
    decisions = read_decisions(root / "decisions.jsonl")
    assert len(decisions) == 1
    assert decisions[0].answer == "Unblock it."
    orders = _orders(tmp_path / "queue")
    assert len(orders) == 1
    assert orders[0].nudge == "[O] Unblock it."


def test_answer_resolves_question_residuals_on_the_foreground_queue(tmp_path: Path) -> None:
    root = tmp_path / "state"
    goal = upsert_goal(root, _goal())
    add_foreground_task(root, goal.session_ref, kind="question", text="Pick a cache backend.", source="monitord")

    stored = record_operator_answer(root, goal.session_ref, "Use sqlite.", queue_dir=tmp_path / "queue")

    assert stored.foreground_tasks == ()
    assert stored.retired_foreground_tasks[-1]["state"] == "resolved-by-operator"
    assert stored.retired_foreground_tasks[-1]["basis"] == "Use sqlite."
    assert len(_orders(tmp_path / "queue")) == 1


def test_answer_does_not_unhold_a_lane_with_a_newer_unanswered_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ask that lands after the operator's snapshot is not covered by the
    answer, so the lane keeps its question hold and the relay stays parked."""
    import chitra.goals as goals_mod
    import chitra.operator_answer as operator_answer_mod

    root = tmp_path / "state"
    goal = upsert_goal(root, _goal())
    add_ask(root, goal.session_ref, "Pick a cache backend?")
    hold_goal(root, goal.session_ref, reason=f"{QUESTION_HOLD_PREFIX} cache pick")

    real_get_goal = goals_mod.get_goal
    staged = {"snapshotted": False}

    def staged_get_goal(root_arg, session_ref):
        record = real_get_goal(root_arg, session_ref)
        if not staged["snapshotted"]:
            staged["snapshotted"] = True
            # A second question lands while the answer transaction runs; the
            # operator's frozen snapshot still names only the first ask, so
            # the answer must not cover the one it never saw.
            snapshot = record
            add_ask(root_arg, session_ref, "Approve the deploy window?")
            return snapshot
        return record

    monkeypatch.setattr(operator_answer_mod, "get_goal", staged_get_goal)
    stored = record_operator_answer(root, goal.session_ref, "Use sqlite.", queue_dir=tmp_path / "queue")

    assert stored.open_asks == ("Approve the deploy window?",)
    assert stored.status == "held"
    assert stored.retired_asks[-1]["basis"] == "Use sqlite."
    # The relay order exists (queued), bound to the contract.
    assert len(_orders(tmp_path / "queue")) == 1


def test_answer_on_a_foreign_hold_relays_but_does_not_resume(tmp_path: Path) -> None:
    """A rate-limit/manual hold owns its own resume path; the answer still
    lands as a ruling and a queued relay order."""
    root = tmp_path / "state"
    goal = upsert_goal(root, _goal())
    add_ask(root, goal.session_ref, "Pick a cache backend?")
    hold_goal(root, goal.session_ref, reason="rate-limit window")

    stored = record_operator_answer(root, goal.session_ref, "Use sqlite.", queue_dir=tmp_path / "queue")

    assert stored.status == "held"
    assert stored.hold_reason == "rate-limit window"
    assert len(_orders(tmp_path / "queue")) == 1

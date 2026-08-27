from __future__ import annotations

from pathlib import Path

from chitra.detect import Finding, IncidentRecord, LadderDecision
from chitra.goals import GoalRecord
from chitra.orders import DispatchResult, DispatchStatus
from chitra.supervision import SupervisionLedger, goal_digest
from chitra.supervisor import (
    build_corrective_order,
    order_marker,
    reconcile_corrective_action,
)

SESSION = "host:lane-a:0"
LANE = "lane-a"


def _goal() -> GoalRecord:
    return GoalRecord(
        session_ref=SESSION,
        goal="Ship the bounded supervisor safely.",
        done_when="The supervisor tests pass with durable evidence.",
        source="task-file:test-supervisor",
        status="working",
        intent="Keep the supervised session moving.",
        scope="The supervisor source and tests.",
        now="",
        last_verified="",
        created_at="2026-08-26T00:00:00+00:00",
        updated_at="2026-08-26T00:00:00+00:00",
    )


def _finding() -> Finding:
    return Finding(
        detector="unnecessary_steps",
        fingerprint_seed={"test": "persistent-supervisor"},
        event_refs=("event-1",),
        unmet_item="supervisor-progress",
        expected_next_progress="run the focused supervisor test",
        detail="the lane repeated a step without producing scoped progress",
    )


def _second_finding() -> Finding:
    return Finding(
        detector="drift",
        fingerprint_seed={"test": "persistent-supervisor-second"},
        event_refs=("event-2",),
        unmet_item="supervisor-alignment",
        expected_next_progress="return to the exact bounded goal",
        detail="the lane drifted from the frozen scope",
    )


def _decision(finding: Finding, *, action: str = "hold", stage: str = "nudge") -> LadderDecision:
    return LadderDecision(
        action=action,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        record=IncidentRecord(
            lane=LANE,
            fingerprint=finding.fingerprint,
            detector=finding.detector,
            stage=stage,  # type: ignore[arg-type]
            order_marker=order_marker(finding),
            opened_at="2026-08-26T00:00:00+00:00",
            event_refs=finding.event_refs,
            unmet_item=finding.unmet_item,
            expected_next_progress=finding.expected_next_progress,
            detail=finding.detail,
        ),
        reason="test decision",
    )


def _kwargs(tmp_path: Path, goal: GoalRecord, finding: Finding, decision: LadderDecision) -> dict[str, object]:
    return {
        "state_root": tmp_path / "state",
        "queue_dir": tmp_path / "queue",
        "lane": LANE,
        "goal": goal,
        "finding": finding,
        "decision": decision,
        "shadow_mode": False,
        "max_action_attempts": 3,
        "retry_delay_seconds": 0,
    }


def _seed_state(tmp_path: Path, goal: GoalRecord, finding: Finding, decision: LadderDecision, state: str, attempt: int = 0) -> None:
    order = build_corrective_order(goal, finding, decision, retry_attempt=attempt)
    SupervisionLedger(tmp_path / "state", LANE).transition(
        state=state,  # type: ignore[arg-type]
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest_value=goal_digest(goal),
        reason="test seed",
        finding_fingerprint=finding.fingerprint,
        stage=decision.stage,
        order_id=order.order_id,
        order_marker=decision.record.order_marker,
        attempt=attempt,
    )


def _write_result(tmp_path: Path, order_id: str, *, reason: str = "lane unavailable") -> None:
    path = tmp_path / "queue" / "results" / f"{order_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        DispatchResult(
            order_id=order_id,
            session_ref=SESSION,
            status=DispatchStatus.FAILED,
            reason=reason,
        ).model_dump_json(),
        encoding="utf-8",
    )


def test_hold_without_matching_pending_intent_does_not_enqueue(tmp_path: Path) -> None:
    goal, finding = _goal(), _finding()
    result = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, _decision(finding)))  # type: ignore[arg-type]
    assert result.enqueued is False
    assert result.reason == "held incident has no matching durable corrective intent"
    assert not list((tmp_path / "queue").glob("**/*.json"))


def test_action_pending_hold_resumes_with_same_order_id(tmp_path: Path) -> None:
    goal, finding = _goal(), _finding()
    decision = _decision(finding)
    _seed_state(tmp_path, goal, finding, decision, "action_pending")
    expected = build_corrective_order(goal, finding, decision)
    result = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    assert result.enqueued is True
    assert result.order_id == expected.order_id
    assert (tmp_path / "queue" / "orders" / f"{expected.order_id}.json").is_file()


def test_action_queued_missing_queue_or_result_blocks_without_repaste(tmp_path: Path) -> None:
    goal, finding = _goal(), _finding()
    decision = _decision(finding)
    _seed_state(tmp_path, goal, finding, decision, "action_pending")
    _seed_state(tmp_path, goal, finding, decision, "action_queued")
    result = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    assert result.state == "blocked"
    assert result.enqueued is False
    assert "duplicate delivery is not safe" in result.reason
    assert not list((tmp_path / "queue").glob("**/*.json"))


def test_terminal_failed_result_schedules_attempt_specific_retry_immediately(tmp_path: Path) -> None:
    goal, finding = _goal(), _finding()
    decision = _decision(finding, action="open")
    first = build_corrective_order(goal, finding, decision, retry_attempt=0)
    _seed_state(tmp_path, goal, finding, decision, "action_queued")
    _write_result(tmp_path, first.order_id, reason="goal-not-actionable")

    failed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    assert failed.state == "blocked"
    assert "retry scheduled" in failed.reason

    resumed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    retry = build_corrective_order(goal, finding, decision, retry_attempt=1)
    assert resumed.enqueued is True
    assert resumed.order_id == retry.order_id
    assert resumed.order_id != first.order_id
    assert (tmp_path / "queue" / "orders" / f"{retry.order_id}.json").is_file()


def test_three_terminal_failures_exhaust_without_fourth_order(tmp_path: Path) -> None:
    goal, finding = _goal(), _finding()
    decision = _decision(finding, action="open")
    first = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    assert first.enqueued is True

    order_ids = [first.order_id]
    for attempt in range(3):
        current_id = order_ids[-1]
        _write_result(tmp_path, current_id)
        failed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
        assert failed.state == "blocked"
        if attempt < 2:
            resumed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
            assert resumed.enqueued is True
            order_ids.append(resumed.order_id)
        else:
            assert failed.reason.find("exhausted") >= 0

    exhausted = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    assert exhausted.enqueued is False
    assert "exhausted" in exhausted.reason
    assert len(order_ids) == 3
    assert len(list((tmp_path / "queue" / "orders").glob("*.json"))) == 3


def test_sibling_finding_cannot_reset_an_action_retry_cursor(tmp_path: Path) -> None:
    goal = _goal()
    first_finding = _finding()
    second_finding = _second_finding()
    first_decision = _decision(first_finding, action="open")
    second_decision = _decision(second_finding, action="open")

    first = reconcile_corrective_action(  # type: ignore[arg-type]
        **_kwargs(tmp_path, goal, first_finding, first_decision)
    )
    assert first.enqueued is True
    _write_result(tmp_path, first.order_id)

    sibling = reconcile_corrective_action(  # type: ignore[arg-type]
        **_kwargs(tmp_path, goal, second_finding, second_decision)
    )
    assert sibling.enqueued is True

    failed = reconcile_corrective_action(  # type: ignore[arg-type]
        **_kwargs(tmp_path, goal, first_finding, first_decision)
    )
    assert failed.state == "blocked"
    assert "retry scheduled" in failed.reason

    # Fair scheduling may revisit the sibling before this action. Its queue
    # reconciliation must not replace the first action's attempt cursor.
    sibling_again = reconcile_corrective_action(  # type: ignore[arg-type]
        **_kwargs(tmp_path, goal, second_finding, second_decision)
    )
    assert sibling_again.enqueued is False

    resumed = reconcile_corrective_action(  # type: ignore[arg-type]
        **_kwargs(tmp_path, goal, first_finding, first_decision)
    )
    retry = build_corrective_order(goal, first_finding, first_decision, retry_attempt=1)
    assert resumed.enqueued is True
    assert resumed.order_id == retry.order_id

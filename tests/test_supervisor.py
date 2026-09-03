from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra.autonomy import AutonomyPolicy, CapabilityGrant, autonomy_policy_sha256
from chitra.detect import Finding, IncidentRecord, LadderDecision
from chitra.goals import GoalRecord, get_goal, upsert_goal
from chitra.orders import DispatchResult, DispatchStatus
from chitra.supervision import SupervisionLedger, goal_digest
from chitra.supervisor import (
    MAX_CORRECTIVE_RETRY_ATTEMPTS,
    build_corrective_order,
    classify_delivery_failure,
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


def test_corrective_order_uses_grants_instead_of_topic_escalation() -> None:
    policy = AutonomyPolicy(
        grants=(
            CapabilityGrant(grant_id="credentials-prod", capability="credential_use", targets=("production",)),
            CapabilityGrant(
                grant_id="spend-usd",
                capability="spend",
                max_amount="25",
                currency="USD",
            ),
            CapabilityGrant(grant_id="security-goal", capability="security_change"),
            CapabilityGrant(grant_id="irreversible-goal", capability="irreversible_action"),
        )
    )
    goal = replace(_goal(), autonomy_policy=policy)
    finding = _finding()

    order = build_corrective_order(goal, finding, _decision(finding, action="open"))

    assert f"sha256:{autonomy_policy_sha256(policy)}" in order.nudge
    assert "spend@goal (amount<=25 USD)" in order.nudge
    assert "Continue autonomously when an active grant covers" in order.nudge
    assert "Escalate only missing credentials" not in order.nudge


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


def test_goal_not_actionable_waits_for_lifecycle_instead_of_retrying_text(tmp_path: Path) -> None:
    goal, finding = _goal(), _finding()
    decision = _decision(finding, action="open")
    first = build_corrective_order(goal, finding, decision, retry_attempt=0)
    _seed_state(tmp_path, goal, finding, decision, "action_queued")
    _write_result(tmp_path, first.order_id, reason="goal-not-actionable")

    failed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    assert failed.state == "blocked"
    assert "lifecycle wait required" in failed.reason

    repeated = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    assert repeated.enqueued is False
    assert repeated.order_id == first.order_id
    assert "lifecycle wait required" in repeated.reason
    assert SupervisionLedger(tmp_path / "state", LANE).latest().obstacle == "lifecycle_wait"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (DispatchStatus.BLOCKED, "goals-schema-newer-than-installed", "lifecycle_wait"),
        (DispatchStatus.BLOCKED, "lane-lifecycle-closed", "lifecycle_wait"),
        (DispatchStatus.BLOCKED, "lane-lifecycle-paused-deferred", "lifecycle_wait"),
        (DispatchStatus.BLOCKED, "lane-lifecycle-unavailable: malformed recovery record", "foreground_replan"),
        (DispatchStatus.BLOCKED, "lane-lifecycle-unknown: archived", "foreground_replan"),
        (DispatchStatus.BLOCKED, "session namespace denied by prefix 'private-'", "foreground_replan"),
        (DispatchStatus.BLOCKED, "session namespace is not owned by this dispatcher", "foreground_replan"),
        (DispatchStatus.BLOCKED, "remote dispatch to host-b not in allowlist", "foreground_replan"),
        (DispatchStatus.FAILED, "unsupported session_ref (expected host:session:pane)", "foreground_replan"),
        (DispatchStatus.BLOCKED, "blocked: pane contains unsent draft", "transport_retry"),
    ],
)
def test_delivery_failure_classification_separates_semantics_from_transport(
    status: DispatchStatus,
    reason: str,
    expected: str,
) -> None:
    result = DispatchResult(order_id="classification", session_ref=SESSION, status=status, reason=reason)

    assert classify_delivery_failure(result) == expected


def test_terminal_failures_keep_pursuing_beyond_five_successive_actions(tmp_path: Path) -> None:
    goal, finding = _goal(), _finding()
    decision = _decision(finding, action="open")
    first = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    assert first.enqueued is True

    order_ids = [first.order_id]
    for _attempt in range(6):
        current_id = order_ids[-1]
        _write_result(tmp_path, current_id)
        failed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
        assert failed.state == "blocked"
        assert "retry scheduled" in failed.reason
        resumed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
        assert resumed.enqueued is True
        order_ids.append(resumed.order_id)

    assert len(order_ids) == 7
    assert len(list((tmp_path / "queue" / "orders").glob("*.json"))) == 7


def test_semantic_delivery_failure_creates_a_foreground_replan_not_a_retry(tmp_path: Path) -> None:
    goal, finding = _goal(), _finding()
    goal = replace(goal, goal="Ship the bounded persistent supervisor safely today.", **enrollment_fields(goal.done_when))
    decision = _decision(finding, action="open")
    upsert_goal(tmp_path / "state", goal)
    first = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
    _write_result(tmp_path, first.order_id, reason="stale-goal-contract")

    failed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]

    assert failed.state == "blocked"
    assert "foreground replan required" in failed.reason
    assert list((tmp_path / "queue" / "orders").glob("*.json")) == [
        tmp_path / "queue" / "orders" / f"{first.order_id}.json"
    ]
    stored = get_goal(tmp_path / "state", goal.session_ref)
    assert stored is not None
    assert len(stored.foreground_tasks) == 1
    assert stored.foreground_tasks[0].kind == "replan"


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


def test_transport_retry_cap_holds_the_lane_instead_of_nudging_forever(tmp_path: Path) -> None:
    goal, finding = _goal(), _finding()
    goal = replace(goal, goal="Ship the bounded persistent supervisor safely today.", **enrollment_fields(goal.done_when))
    decision = _decision(finding, action="open")
    upsert_goal(tmp_path / "state", goal)

    current_id = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision)).order_id  # type: ignore[arg-type]
    capped = None
    for _attempt in range(MAX_CORRECTIVE_RETRY_ATTEMPTS):
        _write_result(tmp_path, current_id)
        failed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
        assert failed.state == "blocked"
        if "retry cap" in failed.reason:
            capped = failed
            break
        resumed = reconcile_corrective_action(**_kwargs(tmp_path, goal, finding, decision))  # type: ignore[arg-type]
        assert resumed.enqueued is True
        current_id = resumed.order_id

    assert capped is not None, "the retry cap was never reached"
    assert "lane held" in capped.reason
    held = get_goal(tmp_path / "state", goal.session_ref)
    assert held is not None
    assert held.status == "held"
    assert held.hold_reason.startswith("corrective-retry-exhausted")

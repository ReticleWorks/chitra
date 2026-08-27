from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _goal_fixtures import enrollment_fields

from chitra.dispatch import directive_voice_violation
from chitra.goal_enforcement import ReviewFinding, SessionReviewSignal, freeze_goal
from chitra.goals import GoalRecord
from chitra.reasoned_dispatch import build_reasoned_dispatch
from chitra.reasoning import PrinciplesIndex


def _goal(*, scope: str = "Source tests and documentation only") -> GoalRecord:
    return GoalRecord(
        session_ref="localhost:lane:0.0",
        intent="Deliver the requested correction while preserving every explicit authority boundary.",
        goal="Build and verify the requested reasoned dispatch producer.",
        done_when="The focused lint and test gates pass locally.",
        scope=scope,
        source="task-file:/tmp/reasoned-dispatch.md",
        status="working",
        **enrollment_fields("The focused lint and test gates pass locally."),
    )


def _review(
    goal: GoalRecord,
    *,
    verdict: str,
    finding: ReviewFinding | None = None,
) -> SessionReviewSignal:
    return SessionReviewSignal.create(
        session_ref=goal.session_ref,
        goal_contract_id=freeze_goal(goal).contract_id,
        behavior_sha256="1" * 64,
        verdict=verdict,
        reviewer_ids=("reviewer-1", "reviewer-2"),
        findings=() if finding is None else (finding,),
        recorded_at="2026-07-14T12:00:00+00:00",
    )


def test_rejected_completion_builds_reasoned_action_with_valid_attestation() -> None:
    goal = _goal()
    review = _review(
        goal,
        verdict="reject",
        finding=ReviewFinding(
            code="unsupported_completion",
            detail="The completion claim did not cite the required checks.",
            citation="I finished the implementation.",
        ),
    )

    order = build_reasoned_dispatch(
        goal,
        review,
        principles=PrinciplesIndex(),
        now=datetime(2026, 7, 14, 12, tzinfo=UTC),
    )

    assert order is not None
    assert order.message_kind == "reasoned_action"
    assert order.session_ref == goal.session_ref
    assert order.created_at == "2026-07-14T12:00:00+00:00"
    assert order.decision_attestation is not None
    assert order.decision_attestation.review_signal_id == review.signal_id
    assert order.decision_attestation.review_verdict == "reject"
    assert order.decision_attestation.authority_class == "corrective"
    assert order.decision_attestation.autonomy == "autonomous"
    assert order.decision_attestation.operator_confirmed is False
    assert order.nudge == order.decision_attestation.approved_text


def test_rejected_goal_drift_builds_reasoned_nudge() -> None:
    goal = _goal()
    review = _review(
        goal,
        verdict="reject",
        finding=ReviewFinding(
            code="goal_drift",
            detail="The lane moved to work outside the frozen goal.",
            citation="I also redesigned an unrelated subsystem.",
        ),
    )

    order = build_reasoned_dispatch(goal, review, principles=PrinciplesIndex())

    assert order is not None
    assert order.message_kind == "reasoned_nudge"
    assert order.decision_attestation is not None
    assert order.decision_attestation.goal_fields == ("goal", "scope")


@pytest.mark.parametrize(
    "code",
    ["false_blocker", "deferred_to_operator", "idle_no_action", "unverified_claim"],
)
def test_rejected_persistence_finding_builds_dispatch(code: str) -> None:
    goal = _goal()
    review = _review(
        goal,
        verdict="reject",
        finding=ReviewFinding(
            code=code,
            detail="The turn stalled instead of taking its in-authority next action.",
            citation="I am blocked and handing this back to the operator.",
        ),
    )

    order = build_reasoned_dispatch(goal, review, principles=PrinciplesIndex())

    assert order is not None
    assert order.message_kind == "reasoned_nudge"
    assert "in-authority" in order.nudge
    assert order.decision_attestation is not None
    assert order.decision_attestation.outcome == "answer"
    assert order.decision_attestation.source == "goal"
    assert order.decision_attestation.authority_class == "corrective"
    assert order.decision_attestation.operator_confirmed is False
    assert order.decision_attestation.goal_fields == ("goal", "scope")


def test_persistence_correction_passes_directive_voice_guard() -> None:
    goal = _goal()
    review = _review(
        goal,
        verdict="reject",
        finding=ReviewFinding(
            code="false_blocker",
            detail="The turn declared a false blocker.",
            citation="I cannot proceed with this task.",
        ),
    )

    order = build_reasoned_dispatch(goal, review, principles=PrinciplesIndex())

    assert order is not None
    assert directive_voice_violation(order.nudge) is None


def test_mixed_drift_and_persistence_findings_take_persistence_branch() -> None:
    goal = _goal()
    review = SessionReviewSignal.create(
        session_ref=goal.session_ref,
        goal_contract_id=freeze_goal(goal).contract_id,
        behavior_sha256="1" * 64,
        verdict="reject",
        reviewer_ids=("reviewer-1", "reviewer-2"),
        findings=(
            ReviewFinding(
                code="false_blocker",
                detail="The turn declared a false blocker.",
                citation="I cannot proceed with any of this.",
            ),
            ReviewFinding(
                code="goal_drift",
                detail="The lane moved to work outside the frozen goal.",
                citation="I also redesigned an unrelated subsystem.",
            ),
        ),
        recorded_at="2026-07-14T12:00:00+00:00",
    )

    order = build_reasoned_dispatch(goal, review, principles=PrinciplesIndex())

    assert order is not None
    assert order.message_kind == "reasoned_nudge"
    assert order.decision_attestation is not None
    assert "stalled" in order.decision_attestation.approved_text
    assert "Stay within scope" in order.decision_attestation.approved_text


def test_accepted_review_builds_no_dispatch() -> None:
    goal = _goal()

    assert (
        build_reasoned_dispatch(
            goal,
            _review(goal, verdict="accept"),
            principles=PrinciplesIndex(),
            review_rejection_confirmed=True,
        )
        is None
    )


def test_additional_operator_gate_builds_no_dispatch() -> None:
    goal = _goal(scope="Credential and API key changes are explicitly outside scope")
    review = _review(
        goal,
        verdict="reject",
        finding=ReviewFinding(
            code="unsupported_completion",
            detail="The completion claim was unsupported.",
            citation="Everything is complete.",
        ),
    )

    assert (
        build_reasoned_dispatch(
            goal,
            review,
            principles=PrinciplesIndex(),
            review_rejection_confirmed=True,
        )
        is None
    )


def test_abstained_decision_builds_no_dispatch() -> None:
    goal = _goal()
    review = _review(
        goal,
        verdict="reject",
        finding=ReviewFinding(
            code="other",
            detail="The adverse finding is outside the classified correction set.",
            citation="An unusual concern remains.",
        ),
    )

    assert (
        build_reasoned_dispatch(
            goal,
            review,
            principles=PrinciplesIndex(),
            review_rejection_confirmed=True,
        )
        is None
    )

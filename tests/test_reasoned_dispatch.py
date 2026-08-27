from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from _goal_fixtures import enrollment_fields

from chitra.autonomy import AutonomyPolicy, CapabilityGrant, CapabilityUse
from chitra.dispatch import directive_voice_violation, enqueue_dispatch_order
from chitra.goal_enforcement import ReviewFinding, SessionReviewSignal, freeze_goal
from chitra.goals import GoalRecord
from chitra.orders import DispatchOrder
from chitra.reasoned_dispatch import build_reasoned_dispatch
from chitra.reasoning import DecisionQuestion, DecisionReasoner, GoalJudgment, PrinciplesIndex


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


def test_scope_mentions_do_not_create_blanket_topic_gates() -> None:
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

    order = build_reasoned_dispatch(
        goal,
        review,
        principles=PrinciplesIndex(),
        review_rejection_confirmed=True,
    )

    assert order is not None
    assert order.decision_attestation is not None
    assert order.decision_attestation.autonomy == "autonomous"


def test_explicit_spend_grant_queues_autonomous_reasoned_action(tmp_path) -> None:
    policy = AutonomyPolicy(
        initiative="aggressive",
        grants=(
            CapabilityGrant(
                grant_id="goal-spend-usd-25",
                capability="spend",
                max_amount="25",
                currency="USD",
            ),
        ),
    )
    goal = replace(_goal(), autonomy_policy=policy)
    approved_text = "Purchase the 20 USD goal fixture, record the receipt, and continue the frozen goal."
    attestation = DecisionReasoner(PrinciplesIndex()).decide(
        goal,
        GoalJudgment(
            determines_answer=True,
            answer=approved_text,
            goal_fields=["goal", "scope", "autonomy_policy"],
            inference="The purchase is goal-scoped and stays within the enrolled 25 USD ceiling.",
        ),
        DecisionQuestion(
            text="May the lane spend 20 USD on the goal-scoped fixture?",
            answer_category="action",
            capability_uses=[CapabilityUse(capability="spend", amount="20", currency="USD")],
        ),
    )

    assert attestation.autonomy == "autonomous"
    assert attestation.operator_confirmation_required is False
    assert attestation.capability_grant_ids == ("goal-spend-usd-25",)
    queued = enqueue_dispatch_order(
        tmp_path / "queue",
        DispatchOrder(
            order_id="reasoned-spend-1",
            session_ref=goal.session_ref,
            nudge=approved_text,
            message_kind="reasoned_action",
            decision_attestation=attestation,
        ),
    )

    stored = DispatchOrder.model_validate_json(queued.read_text(encoding="utf-8"))
    assert stored.decision_attestation is not None
    assert stored.decision_attestation.autonomy == "autonomous"
    assert stored.decision_attestation.operator_confirmation_required is False


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

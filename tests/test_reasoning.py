from __future__ import annotations

import json
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra.dispatch import DispatchOrder
from chitra.goal_enforcement import ReviewFinding, SessionReviewSignal, WatchedSessionBehavior, freeze_goal
from chitra.goals import GoalRecord
from chitra.reasoning import (
    DecisionQuestion,
    DecisionReasoner,
    GoalJudgment,
    OracleRequest,
    OracleVerdict,
    PrinciplesIndex,
    ReasoningContractError,
)


def _goal() -> GoalRecord:
    return GoalRecord(
        session_ref="localhost:lane:0.0",
        intent="Deliver a safe reviewed change without expanding operator authority",
        goal="Build the requested reasoning module on a feature branch",
        done_when="Tests pass and a pull request is opened",
        scope="Code tests and pull request only",
        source="task-file:/tmp/goal.md",
        status="working",
        goal_version=3,
        **enrollment_fields("Tests pass and a pull request is opened"),
    )


def _undetermined() -> GoalJudgment:
    return GoalJudgment(
        determines_answer=False,
        goal_fields=["scope"],
        inference="The goal leaves this implementation detail open.",
    )


def test_goal_judgment_wins_without_principle_or_oracle() -> None:
    calls: list[OracleRequest] = []

    def oracle(request: OracleRequest) -> OracleVerdict:
        calls.append(request)
        raise AssertionError("oracle must not be called")

    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        GoalJudgment(
            determines_answer=True,
            answer="Open a pull request; do not deploy.",
            goal_fields=["done_when", "scope"],
            inference="The done-when and scope explicitly settle delivery.",
        ),
        DecisionQuestion(text="Should this be deployed?", expensive_to_reverse=True),
        oracle=oracle,
    )

    assert decision.approved_text == "Open a pull request; do not deploy."
    assert decision.source == "goal"
    assert decision.goal_version == 3
    assert decision.goal_fields == ("done_when", "scope")
    assert decision.principle_ids == ()
    assert calls == []


def test_principle_fills_goal_gap_without_oracle() -> None:
    calls: list[OracleRequest] = []

    def oracle(request: OracleRequest) -> OracleVerdict:
        calls.append(request)
        raise AssertionError("oracle must not be called")

    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        _undetermined(),
        DecisionQuestion(text="Should we add a typed Pydantic schema at this model boundary?"),
        oracle=oracle,
    )

    assert decision.source == "principle"
    assert decision.principle_ids == ("A04",)
    assert "docs/agent_docs/rules/must-rules.md" in decision.principle_citations
    assert calls == []


def test_oracle_is_called_only_after_goal_and_principles_are_insufficient() -> None:
    calls: list[OracleRequest] = []

    def oracle(request: OracleRequest) -> OracleVerdict:
        calls.append(request)
        return OracleVerdict(
            verdict="Keep the existing wire format until the operator chooses a migration window.",
            evidence_refs=["infra/ccr/agents/oracle.md:1"],
            confidence_basis="The interface is expensive to reverse and neither governing source selects a format.",
        )

    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        _undetermined(),
        DecisionQuestion(
            text="Choose between frobnicated wire formats quux and zorb",
            risk_class="a2",
            genuinely_ambiguous=True,
            expensive_to_reverse=True,
        ),
        oracle=oracle,
    )

    assert decision.source == "oracle-escalated"
    assert decision.oracle_escalated is True
    assert decision.autonomy == "autonomous"
    assert decision.operator_confirmation_required is False
    assert decision.operator_gate_reasons == ()
    assert len(calls) == 1
    assert all(match.confidence < 0.75 for match in calls[0].principle_matches)
    assert len(decision.insufficiency_reasons) == 2


def test_routine_insufficiency_uses_foreground_oracle_when_available() -> None:
    calls: list[OracleRequest] = []

    def oracle(request: OracleRequest) -> OracleVerdict:
        calls.append(request)
        return OracleVerdict(
            verdict="Use the repository's existing frobnicator name and continue.",
            evidence_refs=["src/frobnicator.py:1"],
            confidence_basis="Foreground inspection found the established name.",
        )

    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        _undetermined(),
        DecisionQuestion(text="Which frobnicator nickname should we use?", risk_class="a1"),
        oracle=oracle,
    )

    assert decision.outcome == "answer"
    assert decision.source == "oracle-escalated"
    assert decision.autonomy == "autonomous"
    assert len(calls) == 1


def test_missing_oracle_returns_a_foreground_residual_not_a_user_gate() -> None:
    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        _undetermined(),
        DecisionQuestion(text="Which frobnicator nickname should we use?"),
    )

    assert decision.outcome == "abstain"
    assert decision.source == "foreground-residual"
    assert decision.autonomy == "foreground_residual"
    assert decision.operator_confirmation_required is False


def test_corrupt_principles_lock_fails_closed(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "src/chitra/principles.lock.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["principles"][0]["guidance"] = "silently changed"
    corrupt = tmp_path / "principles.lock.json"
    corrupt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReasoningContractError, match="corpus_id"):
        PrinciplesIndex(corrupt)


def test_incomplete_goal_fails_before_any_reasoning() -> None:
    incomplete = GoalRecord(
        session_ref="localhost:lane:0.0",
        goal="A goal with enough words to validate",
        done_when="A result is eventually made available",
        source="task-file:/tmp/goal.md",
        status="working",
    )

    with pytest.raises(ReasoningContractError, match="strict specification"):
        DecisionReasoner(PrinciplesIndex()).decide(
            incomplete,
            _undetermined(),
            DecisionQuestion(text="Should we proceed?"),
        )


def test_reasoned_dispatch_requires_exact_answer_and_attestation() -> None:
    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        GoalJudgment(
            determines_answer=True,
            answer="Open a pull request; do not deploy.",
            goal_fields=["done_when", "scope"],
            inference="The goal explicitly settles delivery.",
        ),
        DecisionQuestion(text="Should this be deployed?"),
    )

    order = DispatchOrder(
        order_id="reasoned-1",
        session_ref="localhost:lane:0.0",
        nudge=decision.approved_text,
        message_kind="reasoned_answer",
        decision_attestation=decision,
    )
    assert order.decision_attestation is not None
    assert order.decision_attestation.source == "goal"

    with pytest.raises(ValueError, match="exactly match"):
        DispatchOrder(
            order_id="reasoned-2",
            session_ref="localhost:lane:0.0",
            nudge="mutated after review",
            message_kind="reasoned_answer",
            decision_attestation=decision,
        )


def test_reasoned_dispatch_cannot_omit_attestation() -> None:
    with pytest.raises(ValueError, match="requires decision_attestation"):
        DispatchOrder(
            order_id="reasoned-3",
            session_ref="localhost:lane:0.0",
            nudge="Unattested autonomous answer",
            message_kind="reasoned_answer",
        )


def _accepted_review(goal: GoalRecord) -> SessionReviewSignal:
    frozen = freeze_goal(goal)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "The lane asks a bounded technical question.")
    return SessionReviewSignal.create(
        session_ref=goal.session_ref,
        goal_contract_id=frozen.contract_id,
        behavior_sha256=behavior.behavior_sha256,
        verdict="accept",
        reviewer_ids=("reviewer-1", "reviewer-2"),
    )


def test_default_goal_policy_releases_goal_scoped_actions() -> None:
    goal = _goal()
    judgment = GoalJudgment(
        determines_answer=True,
        answer="Use the existing typed boundary.",
        goal_fields=["scope"],
        inference="The scope directly settles this bounded implementation choice.",
    )
    review = _accepted_review(goal)
    autonomous = DecisionReasoner(PrinciplesIndex()).decide(
        goal,
        judgment,
        DecisionQuestion(
            text="May the lane use the existing typed boundary?",
            authority_class="routine",
            session_review=review,
        ),
    )
    assert autonomous.autonomy == "autonomous"
    assert autonomous.operator_confirmation_required is False

    for flag in ("credentials", "irreversible", "strategy_redirect", "spend"):
        released = DecisionReasoner(PrinciplesIndex()).decide(
            goal,
            judgment,
            DecisionQuestion(
                text="May the lane take this sensitive action?",
                authority_class="routine",
                session_review=review,
                **{flag: True},
            ),
        )
        assert released.autonomy == "autonomous"
        assert released.operator_confirmation_required is False

    textually_released = DecisionReasoner(PrinciplesIndex()).decide(
        goal,
        judgment,
        DecisionQuestion(
            text="May the lane use an API key to purchase a paid plan?",
            authority_class="routine",
            session_review=review,
        ),
    )
    assert textually_released.autonomy == "autonomous"
    assert textually_released.operator_gate_reasons == ()


def test_default_authority_class_is_routine_and_autonomous() -> None:
    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        GoalJudgment(
            determines_answer=True,
            answer="Use the existing typed boundary.",
            goal_fields=["scope"],
            inference="The scope settles this bounded answer.",
        ),
        DecisionQuestion(text="May the lane use the existing typed boundary?"),
    )

    assert decision.authority_class == "routine"
    assert decision.autonomy == "autonomous"
    assert decision.operator_confirmation_required is False

    text_only_gate = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        GoalJudgment(
            determines_answer=True,
            answer="Use the existing typed boundary.",
            goal_fields=["scope"],
            inference="The scope settles this bounded answer.",
        ),
        DecisionQuestion(
            text="The model labels this operator required, but no capability is missing.",
            authority_class="operator_required",
        ),
    )
    assert text_only_gate.autonomy == "autonomous"


def test_small_delta_and_goal_scoped_replanning_use_enrolled_grants() -> None:
    goal = _goal()
    judgment = GoalJudgment(
        determines_answer=True,
        answer="Make the bounded implementation change.",
        goal_fields=["scope"],
        inference="The scope settles the bounded change.",
    )
    review = _accepted_review(goal)

    missing_verification = DecisionReasoner(PrinciplesIndex()).decide(
        goal,
        judgment,
        DecisionQuestion(authority_class="small_delta", session_review=review, text="Make the small change."),
    )
    assert missing_verification.autonomy == "autonomous"

    for flag in (
        "new_dependency",
        "new_schema",
        "new_hook",
        "new_authorization_boundary",
        "out_of_scope_path",
    ):
        gated = DecisionReasoner(PrinciplesIndex()).decide(
            goal,
            judgment,
            DecisionQuestion(
                authority_class="small_delta",
                session_review=review,
                text="Make the small change.",
                verification_refs=["test:small-delta"],
                **{flag: True},
            ),
        )
        assert gated.autonomy == "autonomous"


def test_legacy_corrective_review_class_does_not_override_goal_policy() -> None:
    goal = _goal()
    judgment = GoalJudgment(
        determines_answer=True,
        answer="Take the corrective action.",
        goal_fields=["goal", "scope"],
        inference="The rejected review identifies a corrective action.",
    )
    accepted = _accepted_review(goal)
    gated = DecisionReasoner(PrinciplesIndex()).decide(
        goal,
        judgment,
        DecisionQuestion(authority_class="corrective", session_review=accepted, text="Correct the rejected turn."),
    )
    assert gated.autonomy == "autonomous"
    assert gated.operator_confirmation_required is False

    rejected = SessionReviewSignal.create(
        session_ref=goal.session_ref,
        goal_contract_id=freeze_goal(goal).contract_id,
        behavior_sha256="1" * 64,
        verdict="reject",
        reviewer_ids=("reviewer-1",),
        findings=(ReviewFinding(code="other", detail="Unclassified finding.", citation="The lane stopped."),),
    )
    unrecognized = DecisionReasoner(PrinciplesIndex()).decide(
        goal,
        judgment,
        DecisionQuestion(authority_class="corrective", session_review=rejected, text="Correct the rejected turn."),
    )
    assert unrecognized.autonomy == "autonomous"
    assert unrecognized.operator_confirmation_required is False


def test_none_cannot_be_hashed_or_attested_as_approved_text() -> None:
    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(),
        GoalJudgment(
            determines_answer=True,
            answer="A concrete answer.",
            goal_fields=["scope"],
            inference="The scope settles it.",
        ),
        DecisionQuestion(text="What is the answer?"),
    )
    payload = decision.model_dump(mode="python", exclude={"attestation_id", "approved_text", "approved_text_sha256"})
    with pytest.raises(ReasoningContractError, match="approved_text"):
        type(decision).create(approved_text=None, **payload)

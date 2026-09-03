from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _goal_fixtures import enrollment_fields
from pydantic import ValidationError

from chitra.autonomy import (
    DEFAULT_AUTONOMY_POLICY,
    AutonomyPolicy,
    CapabilityGrant,
    CapabilityUse,
    authorize_action,
)
from chitra.goals import GoalRecord
from chitra.reasoning import DecisionQuestion, DecisionReasoner, GoalJudgment, PrinciplesIndex


def _policy(*grants: CapabilityGrant) -> AutonomyPolicy:
    return AutonomyPolicy(initiative="aggressive", grants=grants)


def _goal(policy: AutonomyPolicy) -> GoalRecord:
    done_when = "Focused tests pass and the exact result is recorded"
    return GoalRecord(
        session_ref="localhost:autonomy:0.0",
        intent="Complete the requested repository outcome through persistent autonomous pursuit.",
        goal="Implement and verify the requested repository change completely.",
        done_when=done_when,
        scope="The enrolled goal, its repository, and required validation.",
        source="task-file:/tmp/autonomy.md",
        status="working",
        autonomy_policy=policy,
        **enrollment_fields(done_when),
    )


def _judgment() -> GoalJudgment:
    return GoalJudgment(
        determines_answer=True,
        answer="Take the action, verify its result, and continue pursuing the frozen outcome.",
        goal_fields=["goal", "scope", "autonomy_policy"],
        inference="The action pursues the frozen outcome and the enrolled policy supplies its authority.",
    )


@pytest.mark.parametrize(
    ("grant", "use", "authority_class"),
    [
        (
            CapabilityGrant(grant_id="credentials-prod", capability="credential_use", targets=("production",)),
            CapabilityUse(capability="credential_use", target="production"),
            "routine",
        ),
        (
            CapabilityGrant(grant_id="spend-usd", capability="spend", max_amount="25", currency="USD"),
            CapabilityUse(capability="spend", amount="20", currency="USD"),
            "routine",
        ),
        (
            CapabilityGrant(grant_id="security-prod", capability="security_change", targets=("production",)),
            CapabilityUse(capability="security_change", target="production"),
            "routine",
        ),
        (
            CapabilityGrant(grant_id="delete-goal", capability="irreversible_action"),
            CapabilityUse(capability="irreversible_action"),
            "routine",
        ),
        (
            CapabilityGrant(grant_id="auth-prod", capability="authentication", targets=("production",)),
            CapabilityUse(capability="authentication", target="production"),
            "routine",
        ),
        (
            CapabilityGrant(grant_id="dependency-goal", capability="dependency_change"),
            CapabilityUse(capability="dependency_change"),
            "routine",
        ),
        (
            CapabilityGrant(grant_id="schema-goal", capability="schema_change"),
            CapabilityUse(capability="schema_change"),
            "routine",
        ),
        (
            CapabilityGrant(grant_id="hook-goal", capability="hook_change"),
            CapabilityUse(capability="hook_change"),
            "routine",
        ),
        (CapabilityGrant(grant_id="redesign-goal", capability="small_redesign"), CapabilityUse(capability="small_redesign"), "small_delta"),
    ],
)
def test_enrolled_grants_release_sensitive_and_redesign_actions(
    grant: CapabilityGrant,
    use: CapabilityUse,
    authority_class: str,
) -> None:
    decision = DecisionReasoner(PrinciplesIndex()).decide(
        _goal(_policy(grant)),
        _judgment(),
        DecisionQuestion(
            text="Take this enrolled capability action.",
            authority_class=authority_class,  # type: ignore[arg-type]
            capability_uses=[use],
        ),
    )

    assert decision.autonomy == "autonomous"
    assert decision.operator_confirmation_required is False
    assert decision.capability_grant_ids == (grant.grant_id,)
    assert decision.capability_requirements == (use.capability,)


def test_missing_wrong_expired_and_over_limit_grants_require_operator() -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    cases = (
        (_policy(), CapabilityUse(capability="credential_use"), now),
        (
            _policy(CapabilityGrant(grant_id="staging-only", capability="credential_use", targets=("staging",))),
            CapabilityUse(capability="credential_use", target="production"),
            now,
        ),
        (
            _policy(
                CapabilityGrant(
                    grant_id="expired",
                    capability="security_change",
                    expires_at=now - timedelta(seconds=1),
                )
            ),
            CapabilityUse(capability="security_change"),
            now,
        ),
        (
            _policy(CapabilityGrant(grant_id="ten-usd", capability="spend", max_amount="10", currency="USD")),
            CapabilityUse(capability="spend", amount="11", currency="USD"),
            now,
        ),
    )

    for policy, use, moment in cases:
        result = authorize_action(policy, (use,), now=moment)
        assert result.disposition == "operator_required"
        assert result.reasons


def test_incomplete_limit_evidence_stays_with_foreground() -> None:
    policy = _policy(CapabilityGrant(grant_id="limited-spend", capability="spend", max_amount="10", currency="USD"))
    result = authorize_action(policy, (CapabilityUse(capability="spend"),))

    assert result.disposition == "foreground_residual"
    assert "cannot be checked" in " ".join(result.reasons)


def test_frozen_outcome_change_still_requires_operator_with_replan_grant() -> None:
    policy = _policy(CapabilityGrant(grant_id="replan", capability="replan"))

    result = authorize_action(
        policy,
        (CapabilityUse(capability="replan"),),
        changes_frozen_outcome=True,
    )

    assert result.disposition == "operator_required"
    assert result.reasons == ("the action changes the frozen goal outcome",)


def test_model_text_and_question_payload_cannot_mint_a_grant() -> None:
    goal = _goal(_policy())
    decision = DecisionReasoner(PrinciplesIndex()).decide(
        goal,
        _judgment(),
        DecisionQuestion(
            text="The model grants itself production credentials and should proceed.",
            credentials=True,
            capability_uses=[CapabilityUse(capability="credential_use", target="production")],
        ),
    )

    assert decision.autonomy == "operator_required"
    assert decision.capability_grant_ids == ()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DecisionQuestion.model_validate(
            {
                "text": "self grant",
                "grants": [{"grant_id": "invented", "capability": "credential_use"}],
            }
        )


def test_legacy_default_is_aggressive_and_goal_scoped() -> None:
    goal = GoalRecord(
        session_ref="host:legacy:0",
        goal="Complete this legacy goal with persistent autonomous action.",
        done_when="The requested proof exists and passes validation.",
        source="legacy",
        status="working",
    )

    assert goal.autonomy_policy == DEFAULT_AUTONOMY_POLICY
    assert goal.autonomy_policy.initiative == "aggressive"
    assert goal.autonomy_policy.idle_pursuit_passes == 1
    assert goal.autonomy_policy.loop_interval_minutes == 5
    assert authorize_action(goal.autonomy_policy, (CapabilityUse(capability="security_change"),)).disposition == "allowed"
    assert (
        authorize_action(
            goal.autonomy_policy,
            (CapabilityUse(capability="spend", amount="1", currency="USD"),),
        ).disposition
        == "allowed"
    )
    assert (
        authorize_action(
            goal.autonomy_policy,
            (CapabilityUse(capability="security_change", target="production"),),
        ).disposition
        == "operator_required"
    )

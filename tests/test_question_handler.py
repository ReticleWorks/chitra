from __future__ import annotations

from chitra.goals import GoalRecord
from chitra.question_handler import QuestionHandlerResult, handle_question


def _goal(**updates: object) -> GoalRecord:
    values: dict[str, object] = {
        "session_ref": "host:lane_a:0",
        "goal": "Deliver the bounded repository change with proof",
        "done_when": "Focused tests pass and the required artifact exists",
        "source": "task-file:test",
        "status": "working",
        "intent": "Complete the requested repository outcome for this session",
        "scope": "source code; focused tests; documentation; production deployment is out of scope",
        "goal_version": 3,
    }
    values.update(updates)
    return GoalRecord(**values)  # type: ignore[arg-type]


def test_next_and_done_are_copied_from_the_frozen_contract() -> None:
    goal = _goal()
    next_result = handle_question(goal, "What should I do next?")
    done_result = handle_question(goal, "What proves the goal is done?")

    assert next_result.disposition == "answered"
    assert next_result.source == "frozen_goal"
    assert goal.done_when in (next_result.answer or "")
    assert done_result.disposition == "answered"
    assert done_result.answer == f"The completion condition is: {goal.done_when}"
    assert next_result.goal_digest == done_result.goal_digest
    assert next_result.request_id != done_result.request_id


def test_scope_answers_only_explicit_items_and_preserves_stable_identity() -> None:
    goal = _goal()
    included = handle_question(goal, "Is focused tests in scope?")
    excluded = handle_question(goal, "Is production deployment in scope?")
    absent = handle_question(goal, "Is a dashboard in scope?")
    partial = handle_question(goal, "Is tests in scope?")
    not_in = handle_question(goal, "Is production deployment not in scope?")
    repeated = handle_question(goal, "Is focused tests in scope?")

    assert included.disposition == "answered"
    assert included.answer == "focused tests is in the frozen scope."
    assert excluded.disposition == "answered"
    assert excluded.answer == "production deployment is out of the frozen scope."
    assert absent.disposition == "operator_required"
    assert absent.gate_reasons == ("unknown_or_ambiguous",)
    assert partial.disposition == "operator_required"
    assert not_in.disposition == "answered"
    assert not_in.answer == "production deployment is out of the frozen scope."
    assert included.request_id == repeated.request_id
    assert included.queue_key == included.request_id


def test_sensitive_authority_classes_are_operator_gated() -> None:
    questions = {
        "credentials": "Can I use the API key now?",
        "spend": "Can I spend $10 on this?",
        "irreversible": "Should I delete the old artifact?",
        "security_boundary": "Can I change the authorization boundary?",
        "authentication": "May I make a small reversible redesign of the authentication flow?",
        "authn": "May I make a small reversible change to authn?",
        "new_dependency": "Should I install a new dependency?",
        "new_schema": "Can I add a schema migration?",
        "new_hook": "Should I add a new hook?",
        "strategic_scope_change": "Should we expand the scope?",
    }

    for expected, question in questions.items():
        result = handle_question(_goal(), question)
        assert result.disposition == "operator_required", question
        expected_gate = "security_boundary" if expected in {"authentication", "authn"} else expected
        assert expected_gate in result.gate_reasons, (question, result.gate_reasons)
        assert result.answer is None
        assert result.source == "operator_required"


def test_small_reversible_redesign_is_answered_only_for_an_explicit_scope_item() -> None:
    included = handle_question(_goal(), "May I make a small reversible refactor of source code?")
    excluded = handle_question(_goal(), "May I make a bounded reversible redesign of production deployment?")
    absent = handle_question(_goal(), "May I make a small reversible change to the release workflow?")

    assert included.disposition == "answered"
    assert included.kind == "small_delta"
    assert "small reversible change" in (included.answer or "")
    assert "Focused tests pass" in (included.answer or "")
    assert excluded.disposition == "answered"
    assert (excluded.answer or "").startswith("Do not change production deployment")
    assert absent.disposition == "operator_required"
    assert absent.gate_reasons == ("irreversible",)


def test_unqualified_redesign_is_not_assumed_small_or_reversible() -> None:
    for question in (
        "May I refactor source code?",
        "Should I redesign source code?",
        "Can we revise documentation?",
    ):
        result = handle_question(_goal(), question)
        assert result.disposition == "operator_required", question
        assert result.kind == "unknown"
        assert result.gate_reasons == ("unknown_or_ambiguous",)


def test_unknown_ambiguous_and_invalid_contracts_do_not_get_invented_answers() -> None:
    unknown = handle_question(_goal(), "Should we redesign the workflow?")
    ambiguous = handle_question(_goal(), "Is tests and docs in scope?")
    invalid = handle_question(_goal(scope=""), "Is focused tests in scope?")

    for result in (unknown, ambiguous):
        assert result.disposition == "operator_required"
        assert result.answer is None
        assert result.gate_reasons == ("unknown_or_ambiguous",)
    assert invalid.disposition == "operator_required"
    assert invalid.gate_reasons == ("invalid_frozen_goal",)


def test_empty_question_is_a_typed_operator_gate() -> None:
    result = handle_question(_goal(), "   ")
    assert result.disposition == "operator_required"
    assert result.question == "<empty question>"
    assert result.gate_reasons == ("unknown_or_ambiguous",)


def test_result_is_typed_and_does_not_claim_review_authority() -> None:
    result = handle_question(_goal(), "What are the completion criteria?")
    assert isinstance(result, QuestionHandlerResult)
    assert result.model_config["extra"] == "forbid"
    assert not hasattr(result, "reviewer")

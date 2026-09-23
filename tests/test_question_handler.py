from __future__ import annotations

from chitra.autonomy import AutonomyPolicy, CapabilityGrant
from chitra.decisions import DecisionEntry
from chitra.goals import GoalRecord
from chitra.question_handler import QuestionHandlerResult, extract_questions, handle_question
from chitra.supervision import goal_digest


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
    assert absent.disposition == "residual"
    assert absent.source == "foreground_reasoning"
    assert absent.gate_reasons == ("unknown_or_ambiguous",)
    assert partial.disposition == "residual"
    assert not_in.disposition == "answered"
    assert not_in.answer == "production deployment is out of the frozen scope."
    assert included.request_id == repeated.request_id
    assert included.queue_key == included.request_id


def test_goal_granted_sensitive_topics_reach_foreground_instead_of_the_user() -> None:
    for question in (
        "Can I use the API key now?",
        "Should I delete the old artifact?",
        "Can I change the authorization boundary?",
        "May I make a small reversible redesign of the authentication flow?",
        "Should I install a new dependency?",
        "Can I add a schema migration?",
        "Should I add a new hook?",
        "Should we expand the scope?",
    ):
        result = handle_question(_goal(), question)
        assert result.disposition == "residual", question
        assert result.source == "foreground_reasoning"

    wrong_target = handle_question(_goal(), "May I use a production API key?")
    assert wrong_target.disposition == "operator_required"
    assert wrong_target.gate_reasons == ("credentials",)

    spend = handle_question(_goal(), "Can I spend $10 on this?")
    assert spend.disposition == "residual"
    assert spend.source == "foreground_reasoning"

    production_spend = handle_question(_goal(), "May I spend $10 on production?")
    assert production_spend.disposition == "operator_required"
    assert production_spend.gate_reasons == ("spend",)

    granted_target = handle_question(
        _goal(
            autonomy_policy=AutonomyPolicy(
                grants=(CapabilityGrant(grant_id="credentials-prod", capability="credential_use", targets=("production",)),)
            )
        ),
        "May I use a production API key?",
    )
    assert granted_target.disposition == "residual"
    assert granted_target.source == "foreground_reasoning"


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
    assert absent.disposition == "residual"
    assert absent.source == "foreground_reasoning"


def test_unqualified_redesign_becomes_a_foreground_reasoning_residual() -> None:
    for question in (
        "May I refactor source code?",
        "Should I redesign source code?",
        "Can we revise documentation?",
    ):
        result = handle_question(_goal(), question)
        assert result.disposition == "residual", question
        assert result.source == "foreground_reasoning"
        assert result.kind == "unknown"
        assert result.gate_reasons == ("unknown_or_ambiguous",)


def test_unknown_ambiguous_and_invalid_contracts_become_foreground_residuals() -> None:
    unknown = handle_question(_goal(), "Should we redesign the workflow?")
    ambiguous = handle_question(_goal(), "Is tests and docs in scope?")
    invalid = handle_question(_goal(scope=""), "Is focused tests in scope?")

    for result in (unknown, ambiguous):
        assert result.disposition == "residual"
        assert result.source == "foreground_reasoning"
        assert result.answer is None
        assert result.gate_reasons == ("unknown_or_ambiguous",)
    assert invalid.disposition == "residual"
    assert invalid.gate_reasons == ("invalid_frozen_goal",)


def test_empty_question_is_a_typed_foreground_residual() -> None:
    result = handle_question(_goal(), "   ")
    assert result.disposition == "residual"
    assert result.source == "foreground_reasoning"
    assert result.question == "<empty question>"
    assert result.gate_reasons == ("unknown_or_ambiguous",)


def test_result_is_typed_and_does_not_claim_review_authority() -> None:
    result = handle_question(_goal(), "What are the completion criteria?")
    assert isinstance(result, QuestionHandlerResult)
    assert result.model_config["extra"] == "forbid"
    assert not hasattr(result, "reviewer")


def _decision(decision: str, *, decision_id: str = "dec-test-1", **fields: object) -> DecisionEntry:
    return DecisionEntry(
        decision_id=decision_id,
        at="2026-09-22T00:00:00+00:00",
        kind="adjudication",
        decision=decision,
        basis="Recorded test ruling.",
        citation="test-suite",
        authority="test authority",
        **fields,  # type: ignore[arg-type]
    )


def test_recorded_decision_answers_a_covered_question_with_its_citation() -> None:
    ruling = _decision("Keep the order queue on plain JSONL files; do not add a database.")
    result = handle_question(_goal(), "Should the order queue move to a SQLite database?", decisions=[ruling])

    assert result.disposition == "answered"
    assert result.source == "decisions_log"
    assert result.answer == f"{ruling.decision} (decision {ruling.decision_id})"
    assert result.gate_reasons == ()


def test_uncovered_goal_change_still_reaches_the_operator() -> None:
    ruling = _decision("Keep the order queue on plain JSONL files; do not add a database.")
    result = handle_question(
        _goal(),
        "Can we change the goal outcome to include a dashboard?",
        decisions=[ruling],
    )

    assert result.disposition == "operator_required"
    assert result.answer is None
    assert result.gate_reasons == ("strategic_scope_change",)


def test_credential_question_is_never_auto_answered() -> None:
    ruling = _decision("The stored API key credential file stays inside the vault.")
    result = handle_question(
        _goal(),
        "May I read the stored API key credential file in the vault?",
        decisions=[ruling],
    )

    assert result.disposition == "residual"
    assert result.source == "foreground_reasoning"
    assert result.answer is None


def test_newer_ruling_supersedes_the_one_it_reverses() -> None:
    old = _decision("Keep the order queue on plain JSONL files; do not add a database.", decision_id="dec-old")
    new = _decision("Move the order queue to a SQLite database now.", decision_id="dec-new")
    result = handle_question(_goal(), "Should the order queue move to a SQLite database?", decisions=[old, new])

    assert result.source == "decisions_log"
    assert result.answer == f"{new.decision} (decision dec-new)"


def test_shared_function_words_do_not_let_an_unrelated_ruling_answer() -> None:
    ruling = _decision("Should a flaky check appear, rerun it once with this seed before filing it.")
    result = handle_question(_goal(), "Should I continue with this approach or stop?", decisions=[ruling])

    assert result.source != "decisions_log"


def test_reask_in_a_new_occurrence_is_a_new_request() -> None:
    goal = _goal()
    first = handle_question(goal, "What proves the goal is done?", occurrence="event-1")
    repeated = handle_question(goal, "What proves the goal is done?", occurrence="event-2")

    assert first.occurrence == "event-1"
    assert repeated.occurrence == "event-2"
    assert first.request_id != repeated.request_id


def test_bound_decision_only_answers_its_own_lane_and_contract() -> None:
    goal = _goal()
    digest = goal_digest(goal)
    question = "Should the order queue move to a SQLite database?"
    other_lane = _decision(
        "Keep the order queue on plain JSONL files; do not add a database.",
        session_ref="host:other_lane:0",
        goal_version=goal.goal_version,
        goal_digest=digest,
    )
    other_version = _decision(
        "Keep the order queue on plain JSONL files; do not add a database.",
        decision_id="dec-old-version",
        session_ref=goal.session_ref,
        goal_version=goal.goal_version + 1,
        goal_digest=digest,
    )
    matching = _decision(
        "Keep the order queue on plain JSONL files; do not add a database.",
        decision_id="dec-bound",
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest=digest,
    )

    missed = handle_question(goal, question, decisions=[other_lane, other_version])
    assert missed.source != "decisions_log"

    hit = handle_question(goal, question, decisions=[other_lane, other_version, matching])
    assert hit.source == "decisions_log"
    assert hit.answer == f"{matching.decision} (decision dec-bound)"


def test_verbatim_answer_is_the_relayed_text_for_a_bound_ruling() -> None:
    goal = _goal()
    ruling = _decision(
        "The operator answered this work session's open ask on the board.",
        decision_id="board-answer-1",
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest=goal_digest(goal),
        question="Which database should the feed cache use?",
        answer="Use the existing sqlite cache.",
    )
    result = handle_question(goal, "Which database should the feed cache use?", decisions=[ruling])

    assert result.disposition == "answered"
    assert result.source == "decisions_log"
    assert result.answer == "Use the existing sqlite cache. (decision board-answer-1)"


def test_extract_questions_finds_each_real_question_and_ignores_code() -> None:
    text = (
        "I finished the migration.\n"
        "```python\n"
        "value = ok ? a : b  # not a question\n"
        "url = \"https://x.test/?a=b&c=d\"\n"
        "```\n"
        "Should I use the existing cache for this?\n"
        "The flag is `verbose?` in config.\n"
        "See https://docs.test/path?q=1 for details.\n"
        "Which database should the feed use?\n"
    )

    assert extract_questions(text) == (
        "Should I use the existing cache for this?",
        "Which database should the feed use?",
    )


def test_extract_questions_catches_declarative_blockers_without_a_question_mark() -> None:
    text = (
        "The build is ready.\n"
        "I am waiting for your confirmation before I deploy.\n"
        "Please confirm which approach to take.\n"
    )

    assert extract_questions(text) == (
        "I am waiting for your confirmation before I deploy.",
        "Please confirm which approach to take.",
    )


def test_extract_questions_deduplicates_within_one_turn_but_not_across_calls() -> None:
    text = "What is the next step?\nWHAT IS THE NEXT STEP?\nDone."
    questions = extract_questions(text)

    assert questions == ("What is the next step?",)
    # A re-ask in a later response is a fresh occurrence, not a duplicate.
    assert extract_questions(text) == questions

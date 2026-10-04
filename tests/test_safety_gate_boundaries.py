"""Boundary tests for chitra's autonomy and permission deny paths.

These replace the held unit suites in test_autonomy.py, test_question_handler.py,
test_lane_permissions.py, test_policy_config.py, test_codex_thresholds.py and
test_receipt_path_confinement.py. Every deny path is exercised through a real
entry point over real files:

* ``chitra-convo brief`` for the enrolled-policy operator-brief gate: goals live
  in a real state dir, the conversation log is a real JSONL file, and exhaustion
  evidence resolves against a real delivery ledger.
* monitord's own question pass (``handle_agent_question``) over real
  ``CanonicalEvent`` objects, persisted goals, ``decisions.jsonl`` and a real
  dispatch queue; ``dispatchd --once`` for the forged-answer rejection.
* ``chitra-lane-anchor start`` over a real tmux server on a unique socket, with
  the agent binary stubbed only at the outermost PATH edge and its argv logged.
* ``chitra-usage`` (``codex-snapshot``/``evaluate``/``policy``) with a stub Codex
  app-server binary and real snapshot/policy files.
* ``chitra-receipts`` (``verify``/``ingest``) over real receipt files and
  validator registries.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from _goal_fixtures import VALID_INTERVIEW_RECEIPT, enrollment_fields

from chitra.autonomy import AutonomyPolicy, CapabilityGrant
from chitra.decisions import DecisionEntry, append_decision
from chitra.goals import EnrolledDoneWhenItem, GoalRecord, get_goal, upsert_goal
from chitra.journal import ByteRange, CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.monitord import MonitordConfig, handle_agent_question
from chitra.question_handler import handle_question
from chitra.supervision import goal_digest
from chitra.supervisor import build_question_order
from chitra.validation_receipts import receipt_path

VENV_BIN = Path(sys.executable).parent
LANE = "lane-a"
SESSION = "host:lane-a:0.0"

SCOPE = "source code; focused tests; documentation; production deployment is out of scope"
DONE_WHEN = "Focused tests pass and the required artifact exists"


def _script(name: str) -> str:
    candidate = VENV_BIN / name
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which(name)
    if resolved is None:
        pytest.skip(f"console script not on PATH: {name}")
    return resolved


def _env(**overrides: str | None) -> dict[str, str]:
    env = dict(os.environ)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)


def _goal(session_ref: str = SESSION, *, policy: AutonomyPolicy | None = None, **updates: object) -> GoalRecord:
    values: dict[str, object] = {
        "session_ref": session_ref,
        "goal": "Deliver the bounded repository change with proof.",
        "intent": "Complete the requested repository outcome through persistent autonomous pursuit.",
        "done_when": DONE_WHEN,
        "scope": SCOPE,
        "source": "task-file:/tmp/g3-boundary.md",
        "status": "working",
        "goal_version": 3,
        **enrollment_fields(DONE_WHEN),
    }
    if policy is not None:
        values["autonomy_policy"] = policy
    values.update(updates)
    return GoalRecord(**values)  # type: ignore[arg-type]


def _policy(*grants: CapabilityGrant) -> AutonomyPolicy:
    return AutonomyPolicy(initiative="aggressive", grants=grants)


def _goal_document(root: Path, session_ref: str = SESSION) -> dict[str, object]:
    document = json.loads((root / "goals.json").read_text(encoding="utf-8"))
    for record in document["goals"]:
        if record.get("session_ref") == session_ref:
            return record
    raise AssertionError(f"goal {session_ref} was not persisted under {root}")


# --- chitra-convo brief: the enrolled-policy operator-brief gate ---------------


def _attempt(action: str, result: str, order_id: str) -> dict[str, object]:
    return {"action": action, "result": result, "evidence": {"kind": "order", "ref": order_id}}


def _decision_brief(
    session_ref: str,
    *,
    reason: str,
    subject: str,
    blocker: str,
    decision: str = "May the work session use the named authority?",
    basis: str = "research",
    attempts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "session_ref": session_ref,
        "program": "Chitra boundary tests",
        "subject": subject,
        "progress": "Progress is parked on the open authority question.",
        "stage": "Waiting for a human decision on authority.",
        "category": "decision",
        "decision": decision,
        "recommendation": "Deny the request and keep the work session bounded.",
        "recommendation_basis": basis,
        "options": [
            {"label": "Allow once", "consequence": "The work session uses the authority once."},
            {"label": "Deny", "consequence": "The work session stays bounded and reports the blocker."},
        ],
        "exhaustion": {
            "reason": reason,
            "attempts": attempts if attempts is not None else [_attempt("Tried the local access path.", "Refused.", "tried-1")],
            "residual_blocker": blocker,
        },
        "source_quote": ["The work session asked for the named authority."],
        "source_ref": "transcript:line-1",
    }


def _convo_state(tmp_path: Path, goal: GoalRecord, *, order_ids: tuple[str, ...] = ("tried-1",)) -> Path:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    upsert_goal(state, goal)
    ledger = state / "ledger.jsonl"
    ledger.write_text("".join(json.dumps({"order_id": order_id}) + "\n" for order_id in order_ids), encoding="utf-8")
    return state


def _send_brief(state: Path, session_ref: str, brief: dict[str, object]) -> subprocess.CompletedProcess[str]:
    brief_path = state / "brief.json"
    brief_path.write_text(json.dumps(brief), encoding="utf-8")
    return _run(
        [
            _script("chitra-convo"),
            "brief",
            "--session-ref",
            session_ref,
            "--json",
            str(brief_path),
            "--raw",
            "The work session asked the question verbatim.",
        ],
        env=_env(CHITRA_STATE_DIR=str(state)),
    )


def _convlog_entries(state: Path) -> list[dict[str, object]]:
    path = state / "conversation.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_brief_reaches_operator(result: subprocess.CompletedProcess[str], state: Path) -> None:
    assert result.returncode == 0, result.stderr
    assert "thread=" in result.stderr
    assert result.stdout.strip(), "the rendered operator brief must print when the lane is held for a person"
    kinds = [entry.get("kind") for entry in _convlog_entries(state)]
    assert "operator_brief" in kinds


def _assert_brief_suppressed(result: subprocess.CompletedProcess[str], state: Path) -> None:
    assert result.returncode == 0, result.stderr
    assert "operator brief suppressed" in result.stderr
    assert not result.stdout.strip()
    assert _convlog_entries(state) == []


# --- monitord's question pass --------------------------------------------------


def _event(event_id: str, text: str, *, session_ref: str = SESSION) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        instance="test",
        lane=LANE,
        client=Client.CLAUDE,
        client_version="2.1.229",
        process_id=None,
        transcript=TranscriptIdentity(path="/tmp/test.jsonl", device=0, inode=0),
        session_id=session_ref,
        resume_id=None,
        observed_at="2026-08-26T00:00:00+00:00",
        native_time=None,
        native_type="assistant",
        native_join_id=None,
        raw_byte_range=ByteRange(start=0, end=1),
        raw_sha256=None,
        normalized_type=CanonicalType.FINAL_RESPONSE,
        payload_digest="d" * 64,
        normalizer_version="test",
        payload={"text": text},
        raw_record=None,
    )


def _monitord_config(tmp_path: Path) -> MonitordConfig:
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    (queue / "orders").mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    return MonitordConfig(
        state_dir=state,
        transcript_root=tmp_path / "transcripts",
        findings_path=tmp_path / "findings.jsonl",
        poll_seconds=1,
        shadow_mode=False,
        dispatch_queue_dir=queue,
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        retry_delay_seconds=0,
    )


def _question_pass(config: MonitordConfig, goal: GoalRecord, texts: list[str]) -> str:
    upsert_goal(config.state_dir, goal)
    events = tuple(_event(f"evt-{index}", text, session_ref=goal.session_ref) for index, text in enumerate(texts, start=1))
    return handle_agent_question(config, goal, events, journal_events=events, lane=LANE)


def _queued_orders(queue_dir: Path) -> list[dict[str, object]]:
    orders_dir = queue_dir / "orders"
    if not orders_dir.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(orders_dir.glob("*.json"))]


def _assert_lane_held_for_operator(config: MonitordConfig, session_ref: str = SESSION) -> dict[str, object]:
    document = _goal_document(config.state_dir, session_ref)
    assert document["status"] == "held", document
    assert "operator-required question" in str(document.get("hold_reason", ""))
    assert document["open_asks"], document
    assert _queued_orders(config.dispatch_queue_dir) == []
    return document


def _assert_foreground_residual(config: MonitordConfig, session_ref: str = SESSION) -> dict[str, object]:
    document = _goal_document(config.state_dir, session_ref)
    assert document["status"] != "held", document
    assert document["open_asks"] == [], document
    kinds = [task["kind"] for task in document["foreground_tasks"]]
    assert "question" in kinds, document
    assert _queued_orders(config.dispatch_queue_dir) == []
    return document


def _decision_entry(
    decision: str,
    *,
    decision_id: str = "dec-test-1",
    **fields: object,
) -> DecisionEntry:
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


# === test_autonomy.py deny paths, through chitra-convo + the question pass ====


def test_a_credential_brief_without_a_grant_reaches_the_operator(tmp_path: Path) -> None:
    """Missing grant: the categorical denial is parked with a person, not the lane."""
    state = _convo_state(tmp_path, _goal(policy=_policy()))
    brief = _decision_brief(
        SESSION,
        reason="credential",
        subject="The stored credential is needed to finish the deployed change.",
        blocker="The stored credential is still required to finish the deployed change.",
    )

    _assert_brief_reaches_operator(_send_brief(state, SESSION, brief), state)


def test_a_grant_for_another_target_does_not_release_production(tmp_path: Path) -> None:
    """Wrong-target grant: a staging grant never releases a production credential."""
    policy = _policy(CapabilityGrant(grant_id="staging-only", capability="credential_use", targets=("staging",)))
    state = _convo_state(tmp_path, _goal(policy=policy))
    brief = _decision_brief(
        SESSION,
        reason="credential",
        subject="The production credential is needed to finish the change.",
        blocker="The production credential is still required to finish the deployed change.",
    )

    _assert_brief_reaches_operator(_send_brief(state, SESSION, brief), state)


def test_an_expired_grant_does_not_release_its_capability(tmp_path: Path) -> None:
    """Expired grant: the lapse is treated exactly like no grant at all."""
    policy = _policy(
        CapabilityGrant(
            grant_id="expired-credential",
            capability="credential_use",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    state = _convo_state(tmp_path, _goal(policy=policy))
    brief = _decision_brief(
        SESSION,
        reason="credential",
        subject="The stored credential is needed to finish the change.",
        blocker="The stored credential is still required to finish the deployed change.",
    )

    _assert_brief_reaches_operator(_send_brief(state, SESSION, brief), state)


def test_an_enrolled_grant_suppresses_the_operator_brief(tmp_path: Path) -> None:
    """The allowed side of the same gate: a live grant keeps the brief inside."""
    policy = _policy(CapabilityGrant(grant_id="credentials-prod", capability="credential_use", targets=("production",)))
    state = _convo_state(tmp_path, _goal(policy=policy))
    brief = _decision_brief(
        SESSION,
        reason="credential",
        subject="The production credential is needed to finish the change.",
        blocker="The production credential is still required to finish the deployed change.",
    )

    _assert_brief_suppressed(_send_brief(state, SESSION, brief), state)


def test_an_irreversible_action_brief_follows_the_same_grant_gate(tmp_path: Path) -> None:
    """irreversible_consent maps to the irreversible_action grant check."""
    reason = "irreversible_consent"
    subject = "The old deployment must be deleted before the new one ships."
    blocker = "Deleting the old deployment is irreversible and still required."

    denied_state = _convo_state(tmp_path / "denied", _goal(policy=_policy()))
    denied = _decision_brief(SESSION, reason=reason, subject=subject, blocker=blocker)
    _assert_brief_reaches_operator(_send_brief(denied_state, SESSION, denied), denied_state)

    policy = _policy(CapabilityGrant(grant_id="delete-goal", capability="irreversible_action"))
    granted_state = _convo_state(tmp_path / "granted", _goal(policy=policy))
    _assert_brief_suppressed(_send_brief(granted_state, SESSION, denied), granted_state)


def test_changing_the_frozen_outcome_reaches_the_operator_even_with_a_replan_grant(tmp_path: Path) -> None:
    """An operator_decision reason is a frozen-outcome change: grants cannot release it."""
    policy = _policy(CapabilityGrant(grant_id="replan", capability="replan"))
    state = _convo_state(tmp_path, _goal(policy=policy))
    brief = _decision_brief(
        SESSION,
        reason="operator_decision",
        basis="operator-preference",
        subject="The finished outcome should change to include a dashboard.",
        blocker="The requested change alters the frozen outcome of the goal.",
    )

    _assert_brief_reaches_operator(_send_brief(state, SESSION, brief), state)


def test_over_limit_spend_is_held_for_the_operator(tmp_path: Path) -> None:
    """A spend above the enrolled limit is a person question, not a lane decision."""
    config = _monitord_config(tmp_path)
    policy = _policy(CapabilityGrant(grant_id="ten-usd", capability="spend", max_amount="10", currency="USD"))

    outcome = _question_pass(config, _goal(policy=policy), ["May I spend $11 on this?"])

    assert outcome == "operator_required"
    document = _assert_lane_held_for_operator(config)
    ask = document["open_asks"][0]
    assert "spend" in ask


def test_unpriceable_spend_stays_with_the_foreground(tmp_path: Path) -> None:
    """A limit that cannot be checked from evidence is foreground work, not an ask."""
    config = _monitord_config(tmp_path)
    policy = _policy(CapabilityGrant(grant_id="limited-spend", capability="spend", max_amount="10", currency="USD"))

    outcome = _question_pass(config, _goal(policy=policy), ["Can I spend on this?"])

    assert outcome == "reasoning_required"
    _assert_foreground_residual(config)


def test_the_legacy_default_policy_is_goal_scoped(tmp_path: Path) -> None:
    """Unenrolled policy defaults: production is not covered, the goal target is."""
    brief = _decision_brief(
        SESSION,
        reason="credential",
        subject="The production credential is needed to finish the change.",
        blocker="The production credential is still required to finish the deployed change.",
    )
    denied_state = _convo_state(tmp_path / "denied", _goal())
    _assert_brief_reaches_operator(_send_brief(denied_state, SESSION, brief), denied_state)

    goal_scoped = _decision_brief(
        SESSION,
        reason="credential",
        subject="The stored credential is needed to finish the change.",
        blocker="The stored credential is still required to finish the deployed change.",
    )
    allowed_state = _convo_state(tmp_path / "allowed", _goal())
    _assert_brief_suppressed(_send_brief(allowed_state, SESSION, goal_scoped), allowed_state)


# === test_question_handler.py deny paths, through the real question pass ======


def test_a_credential_question_is_never_auto_answered(tmp_path: Path) -> None:
    """A word-overlap ruling is never authority for a credential ask."""
    config = _monitord_config(tmp_path)
    append_decision(
        config.state_dir / "decisions.jsonl",
        _decision_entry("The stored API key credential file stays inside the vault."),
    )

    outcome = _question_pass(config, _goal(), ["May I read the stored API key credential file in the vault?"])

    assert outcome == "reasoning_required"
    _assert_foreground_residual(config)


def test_a_production_credential_question_holds_the_lane_for_the_operator(tmp_path: Path) -> None:
    config = _monitord_config(tmp_path)

    outcome = _question_pass(config, _goal(), ["May I use a production API key?"])

    assert outcome == "operator_required"
    document = _assert_lane_held_for_operator(config)
    assert "credentials" in document["open_asks"][0]


def test_a_granted_credential_target_goes_to_foreground_not_the_operator(tmp_path: Path) -> None:
    """The grant covers the ask, so it is residual work — still not auto-answered."""
    config = _monitord_config(tmp_path)
    policy = _policy(CapabilityGrant(grant_id="credentials-prod", capability="credential_use", targets=("production",)))
    append_decision(
        config.state_dir / "decisions.jsonl",
        _decision_entry("Yes, use the stored production credential token from the vault."),
    )

    outcome = _question_pass(config, _goal(policy=policy), ["May I use the stored production credential token?"])

    assert outcome == "reasoning_required"
    _assert_foreground_residual(config)


def test_production_spend_is_held_for_the_operator(tmp_path: Path) -> None:
    config = _monitord_config(tmp_path)

    outcome = _question_pass(config, _goal(), ["May I spend $10 on production?"])

    assert outcome == "operator_required"
    document = _assert_lane_held_for_operator(config)
    assert "spend" in document["open_asks"][0]


def test_a_production_security_change_is_held_for_the_operator(tmp_path: Path) -> None:
    config = _monitord_config(tmp_path)

    outcome = _question_pass(config, _goal(), ["May I change the production access control?"])

    assert outcome == "operator_required"
    document = _assert_lane_held_for_operator(config)
    assert "security" in document["open_asks"][0]


def test_a_goal_outcome_change_still_reaches_the_operator(tmp_path: Path) -> None:
    """A ruling about the order queue cannot answer a frozen-outcome change."""
    config = _monitord_config(tmp_path)
    append_decision(
        config.state_dir / "decisions.jsonl",
        _decision_entry("Keep the order queue on plain JSONL files; do not add a database."),
    )

    outcome = _question_pass(config, _goal(), ["Can we change the goal outcome to include a dashboard?"])

    assert outcome == "operator_required"
    document = _assert_lane_held_for_operator(config)
    assert "strategic_scope_change" in document["open_asks"][0]


@pytest.mark.parametrize(
    "question",
    [
        "Should we redesign the workflow?",
        "Is tests and docs in scope?",
        "May I refactor source code?",
        "Should I delete the old artifact?",
        "Should I install a new dependency?",
        "Can I add a schema migration?",
        "Should I add a new hook?",
        "Should we expand the scope?",
        "Can I spend on this?",
    ],
)
def test_unsettled_questions_become_foreground_tasks_not_asks_or_answers(tmp_path: Path, question: str) -> None:
    config = _monitord_config(tmp_path)

    outcome = _question_pass(config, _goal(), [question])

    assert outcome == "reasoning_required", question
    _assert_foreground_residual(config)


def test_an_invalid_frozen_contract_is_residual_not_answered(tmp_path: Path) -> None:
    """A goal with no scope cannot answer a scope question."""
    config = _monitord_config(tmp_path)

    outcome = _question_pass(config, _goal(scope=""), ["Is focused tests in scope?"])

    assert outcome == "reasoning_required"
    document = _assert_foreground_residual(config)
    assert any("valid scope" in task["text"] for task in document["foreground_tasks"]), document


def test_a_bound_decision_only_answers_its_own_lane_and_contract(tmp_path: Path) -> None:
    """Rulings bound to another lane or another goal version stay inert."""
    config = _monitord_config(tmp_path)
    goal = _goal()
    digest = goal_digest(goal)
    decisions_path = config.state_dir / "decisions.jsonl"
    append_decision(
        decisions_path,
        _decision_entry(
            "Keep the order queue on plain JSONL files; do not add a database.",
            session_ref="host:other_lane:0",
            goal_version=goal.goal_version,
            goal_digest=digest,
        ),
    )
    append_decision(
        decisions_path,
        _decision_entry(
            "Keep the order queue on plain JSONL files; do not add a database.",
            decision_id="dec-old-version",
            session_ref=goal.session_ref,
            goal_version=goal.goal_version + 1,
            goal_digest=digest,
        ),
    )

    outcome = _question_pass(config, goal, ["Should the order queue move to a SQLite database?"])

    assert outcome == "reasoning_required"
    _assert_foreground_residual(config)


def test_a_bound_decision_answers_with_its_verbatim_text(tmp_path: Path) -> None:
    """A matching bound ruling is relayed verbatim as a queued goal answer."""
    config = _monitord_config(tmp_path)
    goal = _goal()
    append_decision(
        config.state_dir / "decisions.jsonl",
        _decision_entry(
            "The recorded ruling answers this work session's open ask.",
            decision_id="board-answer-1",
            session_ref=goal.session_ref,
            goal_version=goal.goal_version,
            goal_digest=goal_digest(goal),
            question="Which database should the feed cache use?",
            answer="Use the existing sqlite cache.",
        ),
    )

    outcome = _question_pass(config, goal, ["Which database should the feed cache use?"])

    assert outcome == "answer_queued"
    orders = _queued_orders(config.dispatch_queue_dir)
    assert len(orders) == 1
    assert orders[0]["message_kind"] == "goal_contract_answer"
    assert orders[0]["nudge"] == "Use the existing sqlite cache. (decision board-answer-1)"


def test_a_newer_ruling_supersedes_the_one_it_reverses(tmp_path: Path) -> None:
    config = _monitord_config(tmp_path)
    decisions_path = config.state_dir / "decisions.jsonl"
    append_decision(
        decisions_path,
        _decision_entry("Keep the order queue on plain JSONL files; do not add a database.", decision_id="dec-old"),
    )
    append_decision(
        decisions_path,
        _decision_entry("Move the order queue to a SQLite database now.", decision_id="dec-new"),
    )

    outcome = _question_pass(config, _goal(), ["Should the order queue move to a SQLite database?"])

    assert outcome == "answer_queued"
    orders = _queued_orders(config.dispatch_queue_dir)
    assert orders[0]["nudge"] == "Move the order queue to a SQLite database now. (decision dec-new)"


def test_shared_function_words_do_not_let_an_unrelated_ruling_answer(tmp_path: Path) -> None:
    config = _monitord_config(tmp_path)
    append_decision(
        config.state_dir / "decisions.jsonl",
        _decision_entry("Should a flaky check appear, rerun it once with this seed before filing it."),
    )

    outcome = _question_pass(config, _goal(), ["Should I continue with this approach or stop?"])

    assert outcome == "reasoning_required"
    _assert_foreground_residual(config)


def test_scope_questions_answer_only_explicit_items(tmp_path: Path) -> None:
    """In/out-of-scope answers come from the frozen contract; anything else is residual."""
    config = _monitord_config(tmp_path)

    _question_pass(
        config,
        _goal(),
        [
            "Is focused tests in scope?",
            "Is production deployment in scope?",
            "Is a dashboard in scope?",
        ],
    )

    orders = _queued_orders(config.dispatch_queue_dir)
    nudges = [order["nudge"] for order in orders]
    assert nudges == [
        "focused tests is in the frozen scope.",
        "production deployment is out of the frozen scope.",
    ]
    document = _goal_document(config.state_dir)
    kinds = [task["kind"] for task in document["foreground_tasks"]]
    assert "question" in kinds


def test_extraction_ignores_code_fences_and_urls(tmp_path: Path) -> None:
    """Question marks inside code and URLs never reach the handler."""
    config = _monitord_config(tmp_path)
    text = (
        "I finished the migration.\n"
        "```python\n"
        "value = ok ? a : b  # not a question\n"
        "Is focused tests in scope?\n"
        'url = "https://x.test/?a=b&c=d"\n'
        "```\n"
        "The flag is `verbose?` in config.\n"
        "The inline code `Is source code in scope?` is not a question.\n"
        "See https://docs.test/path?q=1 for details.\n"
        "What proves the goal is done?\n"
    )

    outcome = _question_pass(config, _goal(), [text])

    assert outcome == "answer_queued"
    orders = _queued_orders(config.dispatch_queue_dir)
    assert len(orders) == 1
    assert "completion condition" in orders[0]["nudge"]


def test_a_reasked_question_is_a_new_delivery_request(tmp_path: Path) -> None:
    """A re-ask in a later turn is a second order, not a dropped duplicate."""
    config = _monitord_config(tmp_path)

    _question_pass(config, _goal(), ["What proves the goal is done?", "What proves the goal is done?"])

    orders = _queued_orders(config.dispatch_queue_dir)
    assert len(orders) == 2
    request_ids = {order["question_result"]["request_id"] for order in orders}
    assert len(request_ids) == 2


# === dispatchd --once: forged goal-contract answers are refused ===============


def _write_order(queue_dir: Path, order: object) -> Path:
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True, exist_ok=True)
    path = orders_dir / f"{order.order_id}.json"  # type: ignore[attr-defined]
    path.write_text(order.model_dump_json(), encoding="utf-8")  # type: ignore[attr-defined]
    return path


def test_dispatchd_rejects_forged_and_stale_contract_answers(tmp_path: Path) -> None:
    """The daemon re-derives the answer at delivery; a forged payload is BLOCKED."""
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    ledger = tmp_path / "ledger.jsonl"
    key = tmp_path / "ledger.key"
    state.mkdir(parents=True)
    upsert_goal(state, _goal())
    # The contract an order binds is the goal as persisted -- enrollment
    # derives fields (lane, timestamps, enrolled text) that change the digest.
    goal = get_goal(state, SESSION)
    assert goal is not None

    result = handle_question(goal, "What proves the goal is done?", occurrence="evt-1")
    assert result.disposition == "answered"
    valid = build_question_order(goal, result)
    _write_order(queue, valid)

    # A credential question can never produce an answered contract; a queue file
    # claiming one for it is forged even though the model shape validates.
    forged_result = result.model_copy(update={"question": "May I use a production API key?"})
    forged = valid.model_copy(update={"question_result": forged_result, "order_id": valid.order_id + "-forged"})
    _write_order(queue, forged)

    # A model-valid order bound to a contract version the stored goal no longer matches.
    stale_result = result.model_copy(update={"goal_version": goal.goal_version + 1})
    stale = valid.model_copy(
        update={
            "question_result": stale_result,
            "goal_version": goal.goal_version + 1,
            "order_id": valid.order_id + "-stale",
        }
    )
    _write_order(queue, stale)

    run = _run(
        [
            _script("dispatchd"),
            "--once",
            "--queue-dir",
            str(queue),
            "--goals-root",
            str(state),
            "--ledger-path",
            str(ledger),
            "--ledger-key-path",
            str(key),
        ],
        env=_env(),
    )

    assert run.returncode == 0, run.stderr
    results = {entry["order_id"]: entry for entry in json.loads(run.stdout[run.stdout.index("[\n") :])}
    assert results[forged.order_id]["status"] == "blocked"
    assert results[forged.order_id]["reason"] == "invalid-goal-contract-answer"
    assert results[stale.order_id]["status"] == "blocked"
    assert results[stale.order_id]["reason"] == "stale-goal-contract"
    # The honest answer is never contract-rejected (delivery itself may fail
    # without a real tmux pane, which is a different, non-deny result).
    assert results[valid.order_id]["reason"] not in {
        "invalid-goal-contract-answer",
        "stale-goal-contract",
        "goal-held",
        "goal-not-actionable",
    }


# === test_lane_permissions.py deny paths, through a real tmux launch ==========

AGENT_STUB = """
import json
import os
import sys
import time

log_path = os.environ.get("CHITRA_STUB_ARGV_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
if "-p" in sys.argv[1:]:
    payload = os.environ.get("CHITRA_STUB_HEADLESS_JSON", "")
    if payload:
        sys.stdout.write(payload + "\\n")
        sys.stdout.flush()
    stderr = os.environ.get("CHITRA_STUB_STDERR", "")
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()
    sys.exit(int(os.environ.get("CHITRA_STUB_HEADLESS_EXIT", "0")))
time.sleep(600)
"""

HEADLESS_CLEAN = json.dumps(
    [
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "is_error": False,
            "permission_denials": [],
            "result": "file_write: RAN exit=0\ngh_api_write: RAN exit=0\nfleet_ssh: RAN exit=0",
        },
    ]
)


def _headless(result_text: str, *, denials: list[dict[str, object]] | None = None) -> str:
    return json.dumps(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "is_error": False,
                "permission_denials": denials or [],
                "result": result_text,
            },
        ]
    )


def _write_agent_stub(bin_dir: Path, name: str) -> Path:
    script = bin_dir / name
    script.write_text(f"#!{sys.executable}\n{AGENT_STUB}", encoding="utf-8")
    script.chmod(0o755)
    return script


def _real_tmux() -> str:
    """Resolve the unwrapped tmux binary.

    Some hosts put an env-scrubbing tmux wrapper first on PATH: it replaces
    the tmux server's environment, which drops both the stub bin directory's
    lead and the CHITRA_STUB_* variables the agent stub reads, so the pane
    ends up running the provider's real CLI instead of the stub.  A wrapper
    keeps the real binary beside it as ``tmux.real``.
    """
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is required for a real lane launch")
    resolved = Path(tmux).resolve()
    real = resolved.with_name("tmux.real")
    return str(real if real.is_file() else resolved)


def _lane_fixture(tmp_path: Path, *, backend: str = "claude", lane_index: int = 0) -> dict[str, object]:
    """A rendered lanes.yaml + enrolled goal + stub agent for one launch."""
    root = tmp_path / f"lane{lane_index}"
    state_dir = root / "state"
    workdir = root / "work"
    home = root / "home"
    config_dir = root / "config"
    bin_dir = root / "bin"
    credentials_dir = root / "credentials"
    for directory in (state_dir, workdir, home, config_dir, bin_dir, credentials_dir):
        directory.mkdir(parents=True, exist_ok=True)
    claude_credentials = credentials_dir / "claude-credentials.json"
    claude_credentials.write_text('{"fake": "credentials for boundary tests only"}', encoding="utf-8")
    ssh_key = credentials_dir / "ssh-dispatch-key"
    ssh_key.write_text("fake-ssh-key-for-boundary-tests", encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(workdir)], check=True)
    (workdir / "README.md").write_text("boundary worktree\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(workdir), "-c", "user.email=test@example.com", "-c", "user.name=Boundary", "add", "README.md"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(workdir),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Boundary",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        check=True,
    )

    tmux_session = f"g3{os.getpid()}l{lane_index}x{abs(hash(tmp_path.name)) % 10000}"
    tmux_socket = root / f"tmux-{tmux_session}.sock"
    _write_agent_stub(bin_dir, backend)
    # The launch resolves ``tmux`` through PATH like the agent stub does; put
    # the unwrapped binary first so an env-scrubbing host wrapper never sees
    # the call.
    (bin_dir / "tmux").symlink_to(_real_tmux())

    session_ref = f"tophand:{tmux_session}:0.0"
    upsert_goal(state_dir, _goal(session_ref))

    manifest = root / "lanes.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "lanes": [
                    {
                        "id": f"g3lane{lane_index}",
                        "account": "ubuntu",
                        "uid": os.geteuid(),
                        "home": str(home),
                        "workdir": str(workdir),
                        "config_dir": str(config_dir),
                        "state_dir": str(state_dir),
                        "tmux_socket": str(tmux_socket),
                        "tmux_session": tmux_session,
                        "credentials": {
                            "claude_credentials": str(claude_credentials),
                            "ssh_dispatch_key": str(ssh_key),
                        },
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    argv_log = root / "agent-argv.jsonl"
    return {
        "root": root,
        "state_dir": state_dir,
        "workdir": workdir,
        "bin_dir": bin_dir,
        "manifest": manifest,
        "lane_id": f"g3lane{lane_index}",
        "tmux_session": tmux_session,
        "tmux_socket": tmux_socket,
        "argv_log": argv_log,
        "receipt": state_dir / "lane-launch.json",
    }


def _launch(
    fixture: dict[str, object],
    *,
    backend: str,
    model: str | None,
    effort: str,
    headless_json: str,
    ssh_target: str | None = None,
    extra_env: dict[str, str] | None = None,
    headless_exit: str = "0",
) -> subprocess.CompletedProcess[str]:
    argv = [
        _script("chitra-lane-anchor"),
        "--lanes-file",
        str(fixture["manifest"]),
        "--lane",
        str(fixture["lane_id"]),
        "--backend",
        backend,
        "--effort",
        effort,
        "--socket-path",
        str(fixture["root"] / "control.sock"),
    ]
    if model is not None:
        argv += ["--model", model]
    if ssh_target is not None:
        argv += ["--selftest-ssh-target", ssh_target]
    argv.append("start")
    overrides: dict[str, str | None] = {
        "PATH": f"{fixture['bin_dir']}:{os.environ['PATH']}",
        "CHITRA_STUB_ARGV_LOG": str(fixture["argv_log"]),
        "CHITRA_STUB_HEADLESS_JSON": headless_json,
        "CHITRA_STUB_HEADLESS_EXIT": headless_exit,
    }
    if extra_env:
        overrides.update(extra_env)
    result = _run(argv, env=_env(**overrides), timeout=240)
    return result


@pytest.fixture
def _lane_cleanup():
    sockets: list[tuple[Path, str]] = []
    yield sockets
    for socket_path, session in sockets:
        subprocess.run(
            [_real_tmux(), "-S", str(socket_path), "kill-session", "-t", session],
            check=False,
            capture_output=True,
        )


def _track(fixture: dict[str, object], sockets: list[tuple[Path, str]]) -> None:
    sockets.append((fixture["tmux_socket"], fixture["tmux_session"]))  # type: ignore[arg-type]


def _argv_log(fixture: dict[str, object]) -> list[list[str]]:
    path = Path(fixture["argv_log"])  # type: ignore[arg-type]
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pane_argv(calls: list[list[str]], backend: str) -> list[str]:
    # The stub logs sys.argv[1:], so the pane call is the launch argv.
    return next(call for call in calls if "-p" not in call)


def _probe_argv(calls: list[list[str]], backend: str) -> list[str]:
    return next(call for call in calls if "-p" in call)


def _tmux_has_session(socket_path: Path, session: str) -> bool:
    result = subprocess.run(
        [_real_tmux(), "-S", str(socket_path), "has-session", "-t", session],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def test_a_clean_probe_launch_records_the_flag_and_what_it_proved(tmp_path: Path, _lane_cleanup) -> None:
    """The full-permission flag reaches the real pane, the probes run, and the
    launch receipt records the proof."""
    fixture = _lane_fixture(tmp_path)
    _track(fixture, _lane_cleanup)

    run = _launch(
        fixture,
        backend="claude",
        model="sonnet",
        effort="high",
        headless_json=HEADLESS_CLEAN,
        ssh_target="agent@renegade",
    )

    assert run.returncode == 0, run.stderr
    calls = _argv_log(fixture)
    pane = _pane_argv(calls, "claude")
    assert "--dangerously-skip-permissions" in pane
    probe = _probe_argv(calls, "claude")
    # The probe runs the lane's own command plus the headless ask.
    assert probe[: len(pane)] == pane
    tail = probe[len(pane) :]
    assert tail[0] == "--output-format" and tail[1] == "json" and tail[2] == "-p"
    prompt = tail[3]
    # Every probed class is named, and the prompt forbids working around a refusal.
    assert "file-writing tool" in prompt
    assert "gh api --method POST" in prompt
    assert "agent@renegade" in prompt
    assert "do not look for another way to do it" in prompt
    receipt = json.loads(Path(fixture["receipt"]).read_text(encoding="utf-8"))
    self_test = receipt["permission_self_test"]
    assert self_test["live"] is True
    assert self_test["passed"] is True
    assert set(self_test["probed"]) == {"file_write", "gh_api_write", "fleet_ssh"}
    assert self_test["refusals"] == []


def test_an_unset_ssh_target_is_reported_unprobed_on_a_real_launch(tmp_path: Path, _lane_cleanup) -> None:
    fixture = _lane_fixture(tmp_path)
    _track(fixture, _lane_cleanup)

    run = _launch(fixture, backend="claude", model="sonnet", effort="high", headless_json=HEADLESS_CLEAN)

    assert run.returncode == 0, run.stderr
    assert "did not probe: fleet_ssh" in run.stderr
    receipt = json.loads(Path(fixture["receipt"]).read_text(encoding="utf-8"))
    self_test = receipt["permission_self_test"]
    assert self_test["passed"] is True
    assert "fleet_ssh" in self_test["unprobed"]


def test_the_ssh_probe_target_can_come_from_the_environment(tmp_path: Path, _lane_cleanup) -> None:
    fixture = _lane_fixture(tmp_path)
    _track(fixture, _lane_cleanup)

    run = _launch(
        fixture,
        backend="claude",
        model="sonnet",
        effort="high",
        headless_json=HEADLESS_CLEAN,
        extra_env={"CHITRA_LANE_SELFTEST_SSH_TARGET": "agent@twinridge"},
    )

    assert run.returncode == 0, run.stderr
    probe = _probe_argv(_argv_log(fixture), "claude")
    prompt = probe[-1]
    assert "agent@twinridge" in prompt


@pytest.mark.parametrize(
    ("headless_json", "marker"),
    [
        (
            _headless(
                "file_write: RAN exit=0",
                denials=[{"tool_name": "Bash", "tool_input": {"command": "gh api --method POST /markdown"}}],
            ),
            "declined Bash",
        ),
        (
            _headless(
                "file_write: RAN exit=0\n"
                "gh_api_write: REFUSED Permission for this action was denied by the Claude Code "
                "auto mode classifier. Reason: Blocked by classifier."
            ),
            "auto mode classifier",
        ),
        (
            _headless("file_write: RAN exit=0\ngh_api_write: REFUSED a permission guard blocked me"),
            "gh_api_write was refused",
        ),
    ],
    ids=["structured-denial", "prose-refusal", "named-class-refusal"],
)
def test_a_reported_refusal_fails_the_launch_and_leaves_no_receipt(tmp_path: Path, _lane_cleanup, headless_json: str, marker: str) -> None:
    """A structured, prose, or named-class refusal is a launch defect: exit 78,
    the session is stopped, and no launch receipt is written."""
    fixture = _lane_fixture(tmp_path)
    _track(fixture, _lane_cleanup)

    run = _launch(fixture, backend="claude", model="sonnet", effort="high", headless_json=headless_json)

    assert run.returncode == 78, run.stderr
    assert "LAUNCH DEFECT" in run.stderr
    assert marker in run.stderr
    assert not Path(fixture["receipt"]).exists()
    assert not _tmux_has_session(Path(fixture["tmux_socket"]), str(fixture["tmux_session"]))


def test_a_probe_that_ran_and_failed_is_not_a_refusal(tmp_path: Path, _lane_cleanup) -> None:
    """GitHub being down is a fleet fact, not a permission denial."""
    fixture = _lane_fixture(tmp_path)
    _track(fixture, _lane_cleanup)

    run = _launch(
        fixture,
        backend="claude",
        model="sonnet",
        effort="high",
        headless_json=_headless("file_write: RAN exit=0\ngh_api_write: RAN exit=1\nfleet_ssh: RAN exit=255"),
        ssh_target="agent@renegade",
    )

    assert run.returncode == 0, run.stderr
    assert Path(fixture["receipt"]).is_file()
    assert _tmux_has_session(Path(fixture["tmux_socket"]), str(fixture["tmux_session"]))


def test_an_agent_that_cannot_run_leaves_the_lane_unproven(tmp_path: Path, _lane_cleanup) -> None:
    """A self-test that could not run is not a pass and not a refusal."""
    fixture = _lane_fixture(tmp_path)
    _track(fixture, _lane_cleanup)

    run = _launch(
        fixture,
        backend="claude",
        model="sonnet",
        effort="high",
        headless_json="",
        headless_exit="1",
        extra_env={"CHITRA_STUB_STDERR": "claude: command not found"},
    )

    assert run.returncode == 78, run.stderr
    assert "LaneSelfTestUnavailable" in run.stderr
    assert not Path(fixture["receipt"]).exists()
    assert not _tmux_has_session(Path(fixture["tmux_socket"]), str(fixture["tmux_session"]))


def test_unreadable_probe_output_is_unproven_not_a_pass(tmp_path: Path, _lane_cleanup) -> None:
    fixture = _lane_fixture(tmp_path)
    _track(fixture, _lane_cleanup)

    run = _launch(
        fixture,
        backend="claude",
        model="sonnet",
        effort="high",
        headless_json="not json at all",
    )

    assert run.returncode == 78, run.stderr
    assert not Path(fixture["receipt"]).exists()
    assert not _tmux_has_session(Path(fixture["tmux_socket"]), str(fixture["tmux_session"]))


def test_a_codex_lane_launches_at_full_access_without_spending_a_live_probe(tmp_path: Path, _lane_cleanup) -> None:
    fixture = _lane_fixture(tmp_path, backend="codex")
    _track(fixture, _lane_cleanup)

    run = _launch(fixture, backend="codex", model="gpt-5.6-sol", effort="high", headless_json=HEADLESS_CLEAN)

    assert run.returncode == 0, run.stderr
    calls = _argv_log(fixture)
    pane = _pane_argv(calls, "codex")
    assert "--dangerously-bypass-approvals-and-sandbox" in pane
    assert pane[:2] == ["--model", "gpt-5.6-sol"]
    assert pane[pane.index("--profile") + 1] == "chitra-g3lane0"
    # No headless probe run was spent on a Codex lane.
    assert all("-p" not in call for call in calls)
    receipt = json.loads(Path(fixture["receipt"]).read_text(encoding="utf-8"))
    self_test = receipt["permission_self_test"]
    assert self_test["live"] is False
    assert self_test["passed"] is True
    assert self_test["unprobed"]


def test_an_opencode_lane_launches_with_provider_owned_permission_policy(tmp_path: Path, _lane_cleanup) -> None:
    fixture = _lane_fixture(tmp_path, backend="opencode")
    _track(fixture, _lane_cleanup)

    run = _launch(fixture, backend="opencode", model="opencode/x-preview-f-free", effort="high", headless_json=HEADLESS_CLEAN)

    assert run.returncode == 0, run.stderr
    calls = _argv_log(fixture)
    pane = _pane_argv(calls, "opencode")
    assert pane == ["--model", "opencode/x-preview-f-free"]
    assert all("-p" not in call for call in calls)
    receipt = json.loads(Path(fixture["receipt"]).read_text(encoding="utf-8"))
    self_test = receipt["permission_self_test"]
    assert self_test["live"] is False
    assert self_test["passed"] is True
    assert "OpenCode" in self_test["detail"]


# === test_codex_thresholds.py deny paths, through chitra-usage ================

CODEX_STUB = """
import json
import os
import sys

limits = json.loads(os.environ["CHITRA_STUB_RATE_LIMITS"])
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if message.get("id") == 1:
        sys.stdout.write(json.dumps({"id": 1, "result": {}}) + "\\n")
    elif message.get("id") == 2:
        sys.stdout.write(json.dumps({"id": 2, "result": {"rateLimits": limits}}) + "\\n")
    else:
        continue
    sys.stdout.flush()
"""


def _weekly_reset() -> int:
    """A reset far enough out to classify as the weekly window."""
    return int(datetime.now(UTC).timestamp()) + 5 * 86400


def _window(pct: float, resets_at: int) -> dict[str, object]:
    return {"pct": pct, "resets_at": resets_at}


def _snapshot_file(
    directory: Path,
    name: str,
    *,
    kind: str,
    session_id: str,
    five_hour: dict[str, object] | None = None,
    seven_day: dict[str, object] | None = None,
    account: str = "agent@example.com",
) -> Path:
    path = directory / name
    payload = {
        "schema": "chitra.usage.v1",
        "kind": kind,
        "ts": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "tmux_session": "",
        "five_hour": five_hour,
        "seven_day": seven_day,
        "account": account,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _codex_stub(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "codex"
    stub.write_text(f"#!{sys.executable}\n{CODEX_STUB}", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _evaluate(
    tmp_path: Path,
    *,
    snapshots: list[tuple[str, str, dict[str, object]]] | None = None,
    rate_limits: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Run ``chitra-usage evaluate`` over real snapshot files + a stub app-server."""
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir(exist_ok=True)
    argv = [_script("chitra-usage"), "evaluate", "--dir", str(snap_dir)]
    env: dict[str, str | None] = {}
    for spec in snapshots or []:
        name, kind, windows = spec
        _snapshot_file(
            snap_dir,
            name,
            kind=kind,
            session_id=Path(name).stem,
            five_hour=windows.get("five_hour"),
            seven_day=windows.get("seven_day"),
            # evaluate_grouped merges every snapshot sharing an account, so each
            # fixture session keeps its own identity.
            account=f"{Path(name).stem}@example.com",
        )
    if rate_limits is not None:
        stub = _codex_stub(tmp_path / "bin")
        argv += ["--codex", "--codex-bin", str(stub)]
        env["CHITRA_STUB_RATE_LIMITS"] = json.dumps(rate_limits)
    run = _run(argv, env=_env(**env))
    assert run.returncode == 0, run.stderr
    return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]


def test_a_weekly_cap_in_the_primary_slot_pauses_on_the_weekly_threshold(tmp_path: Path) -> None:
    """The measured tophand shape: the weekly cap arrives in ``primary``.

    Read through the real app-server protocol (stub binary at the outermost
    edge), then classified by reset horizon at evaluate time.
    """
    weekly_reset = _weekly_reset()
    verdicts = _evaluate(
        tmp_path,
        rate_limits={"primary": {"used_percent": 100, "resets_at": weekly_reset}, "secondary": None},
    )

    (verdict,) = verdicts
    assert verdict["level"] == "pause"
    assert verdict["binding_window"] == "7d"
    assert verdict["resume_at_epoch"] == weekly_reset


def test_the_codex_weekly_pause_bites_before_the_claude_seven_day_pause(tmp_path: Path) -> None:
    """91% is past Codex's weekly line and short of Claude's."""
    verdicts = _evaluate(
        tmp_path,
        snapshots=[
            ("claude.json", "claude", {"seven_day": _window(91.0, _weekly_reset())}),
            ("codex.json", "codex", {"seven_day": _window(91.0, _weekly_reset())}),
        ],
    )

    by_kind = {verdict["kind"]: verdict for verdict in verdicts}
    assert by_kind["codex"]["level"] == "pause"
    assert by_kind["codex"]["binding_window"] == "7d"
    assert by_kind["claude"]["level"] == "approaching"


def test_codex_weekly_warns_at_its_own_line(tmp_path: Path) -> None:
    (verdict,) = _evaluate(
        tmp_path,
        snapshots=[("codex.json", "codex", {"seven_day": _window(86.0, _weekly_reset())})],
    )

    assert verdict["level"] == "approaching"
    assert verdict["binding_window"] == "7d"
    assert verdict["resume_at_epoch"] == 0


def test_a_genuine_codex_five_hour_window_uses_the_five_hour_ladder(tmp_path: Path) -> None:
    short_reset = int(datetime.now(UTC).timestamp()) + 2 * 3600
    (verdict,) = _evaluate(
        tmp_path,
        snapshots=[("codex.json", "codex", {"five_hour": _window(93.0, short_reset)})],
    )

    assert verdict["level"] == "pause"
    assert verdict["binding_window"] == "5h"
    assert verdict["resume_at_epoch"] == short_reset


def test_the_more_severe_codex_window_binds_when_both_are_present(tmp_path: Path) -> None:
    short_reset = int(datetime.now(UTC).timestamp()) + 2 * 3600
    (verdict,) = _evaluate(
        tmp_path,
        snapshots=[
            (
                "codex.json",
                "codex",
                {"five_hour": _window(81.0, short_reset), "seven_day": _window(95.0, _weekly_reset())},
            )
        ],
    )

    assert verdict["level"] == "pause"
    assert verdict["binding_window"] == "7d"


def test_claude_windows_are_never_reclassified(tmp_path: Path) -> None:
    """A Claude five-hour window at 96% binds 5h even with a far-out reset —
    reclassified as weekly it would bind 7d instead."""
    (verdict,) = _evaluate(
        tmp_path,
        snapshots=[
            (
                "claude.json",
                "claude",
                {"five_hour": _window(96.0, _weekly_reset()), "seven_day": _window(20.0, int(datetime.now(UTC).timestamp()) + 3600)},
            )
        ],
    )

    assert verdict["level"] == "pause"
    assert verdict["binding_window"] == "5h"


@pytest.mark.parametrize(
    ("windows", "expected_level", "expected_window"),
    [
        ({"seven_day": _window(96.0, _weekly_reset())}, "pause", "7d"),
        ({"seven_day": _window(95.0, _weekly_reset())}, "pause", "7d"),
        ({"seven_day": _window(91.0, _weekly_reset())}, "approaching", "7d"),
        ({"seven_day": _window(90.0, _weekly_reset())}, "approaching", "7d"),
        ({"seven_day": _window(50.0, _weekly_reset())}, "ok", ""),
        ({"five_hour": _window(92.0, int(datetime.now(UTC).timestamp()) + 3600)}, "pause", "5h"),
        ({"five_hour": _window(80.0, int(datetime.now(UTC).timestamp()) + 3600)}, "approaching", "5h"),
    ],
)
def test_the_claude_ladder_binds_at_its_exact_lines(
    tmp_path: Path,
    windows: dict[str, dict[str, object]],
    expected_level: str,
    expected_window: str,
) -> None:
    (verdict,) = _evaluate(tmp_path, snapshots=[("claude.json", "claude", windows)])

    assert verdict["level"] == expected_level
    assert verdict["binding_window"] == expected_window


def test_a_quiet_codex_account_is_ok(tmp_path: Path) -> None:
    short_reset = int(datetime.now(UTC).timestamp()) + 2 * 3600
    (verdict,) = _evaluate(
        tmp_path,
        snapshots=[("codex.json", "codex", {"five_hour": _window(12.0, short_reset), "seven_day": _window(34.0, _weekly_reset())})],
    )

    assert verdict["level"] == "ok"


@pytest.mark.parametrize(
    ("windows", "expected_level", "expected_window"),
    [
        ({"seven_day": _window(90.0, _weekly_reset())}, "pause", "7d"),
        ({"seven_day": _window(89.0, _weekly_reset())}, "approaching", "7d"),
        ({"seven_day": _window(85.0, _weekly_reset())}, "approaching", "7d"),
        ({"five_hour": _window(92.0, int(datetime.now(UTC).timestamp()) + 3600)}, "pause", "5h"),
        ({"five_hour": _window(91.0, int(datetime.now(UTC).timestamp()) + 3600)}, "approaching", "5h"),
        ({"five_hour": _window(80.0, int(datetime.now(UTC).timestamp()) + 3600)}, "approaching", "5h"),
    ],
)
def test_the_codex_ladder_binds_at_its_exact_lines(
    tmp_path: Path,
    windows: dict[str, dict[str, object]],
    expected_level: str,
    expected_window: str,
) -> None:
    (verdict,) = _evaluate(tmp_path, snapshots=[("codex.json", "codex", windows)])

    assert verdict["level"] == expected_level
    assert verdict["binding_window"] == expected_window


# === test_policy_config.py deny paths, through chitra-usage policy ============


def _usage_policy_cli(tmp_path: Path, data: dict[str, object] | str, *, env_config: str | None = None) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "policy.yaml"
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    overrides: dict[str, str | None] = {"CHITRA_POLICY_CONFIG": env_config}
    argv = [_script("chitra-usage"), "policy"]
    if env_config is None:
        argv += ["--policy-config", str(path)]
    else:
        argv += ["--policy-config", str(path)]
    return _run(argv, env=_env(**overrides))


def _assert_policy_refused(tmp_path: Path, data: dict[str, object] | str) -> None:
    run = _usage_policy_cli(tmp_path, data)
    assert run.returncode != 0, f"policy accepted: {data!r}\n{run.stdout}"


def test_usage_policy_rejects_each_invalid_value(tmp_path: Path) -> None:
    for usage in (
        {"pause_5h_pct": 0},
        {"pause_7d_pct": 101},
        {"warn_5h_pct": -1},
        {"warn_7d_pct": 101},
        {"warn_5h_pct": 86, "pause_5h_pct": 85},
        {"warn_7d_pct": 93, "pause_7d_pct": 92},
        {"max_running": 0},
    ):
        _assert_policy_refused(tmp_path, {"usage": usage})


def test_an_inverted_or_impossible_codex_ladder_is_refused(tmp_path: Path) -> None:
    for usage in (
        {"codex_warn_weekly_pct": 95.0, "codex_pause_weekly_pct": 90.0},
        {"codex_warn_5h_pct": 95.0, "codex_pause_5h_pct": 92.0},
        {"codex_pause_weekly_pct": 0.0},
        {"codex_pause_5h_pct": 101.0},
    ):
        _assert_policy_refused(tmp_path, {"usage": usage})


def test_load_policy_rejects_each_inverted_ladder(tmp_path: Path) -> None:
    for invalid in (
        {"l3_mem_available_pct": 16},
        {"clear_memory_some_avg60": 11},
        {"l3_max_running": 7},
        {"consecutive_sweeps": 0},
    ):
        _assert_policy_refused(tmp_path, {"load": invalid})


def test_pr_review_policy_rejects_each_invalid_value(tmp_path: Path) -> None:
    for pr_review in (
        {"max_diff_lines": 0},
        {"max_diff_files": 0},
        {"reviewer_count": 0},
        {"blast_radius_keywords": ["auth", "  "]},
    ):
        _assert_policy_refused(tmp_path, {"pr_review": pr_review})


def test_policy_rejects_invalid_schema_values(tmp_path: Path) -> None:
    for data in (
        {"completion_gate": {"required_evidence": ["unknown"]}},
        {"dispatch": {"banned_attribution_patterns": ["["]}},
        {"dispatch": {"extra_idle_input_regexes": ["["]}},
    ):
        _assert_policy_refused(tmp_path, data)


def test_guidance_policy_rejects_empty_document_values(tmp_path: Path) -> None:
    _assert_policy_refused(tmp_path, {"guidance": {"canonical_decisions": {"default": ""}}})


def test_merge_policy_rejects_each_unsafe_configuration(tmp_path: Path) -> None:
    for merge in (
        {"allowed_repos": ["chitra"]},
        {"lane_authors": [" lane-bot"]},
        {"enabled": True},
        {"hold_labels": ["  "]},
        {"hold_labels": []},
    ):
        _assert_policy_refused(tmp_path, {"merge": merge})


def test_a_configured_policy_error_is_not_ignored(tmp_path: Path) -> None:
    run = _run(
        [_script("chitra-usage"), "policy", "--policy-config", str(tmp_path / "missing.yaml")],
        env=_env(),
    )
    assert run.returncode != 0

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("completion_gate: [", encoding="utf-8")
    run = _run([_script("chitra-usage"), "policy", "--policy-config", str(malformed)], env=_env())
    assert run.returncode != 0


def test_policy_loads_through_the_environment_variable(tmp_path: Path) -> None:
    configured = tmp_path / "configured.yaml"
    configured.write_text(yaml.safe_dump({"usage": {"pause_5h_pct": 88.0}}), encoding="utf-8")

    run = _run(
        [_script("chitra-usage"), "policy"],
        env=_env(CHITRA_POLICY_CONFIG=str(configured)),
    )

    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["pause_5h_pct"] == 88.0


def test_an_explicit_policy_path_wins_over_the_environment(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.yaml"
    configured = tmp_path / "configured.yaml"
    explicit.write_text(yaml.safe_dump({"usage": {"pause_5h_pct": 86.0}}), encoding="utf-8")
    configured.write_text(yaml.safe_dump({"usage": {"pause_5h_pct": 71.0}}), encoding="utf-8")

    run = _run(
        [_script("chitra-usage"), "policy", "--policy-config", str(explicit)],
        env=_env(CHITRA_POLICY_CONFIG=str(configured)),
    )

    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["pause_5h_pct"] == 86.0


def test_usage_policy_overrides_are_applied(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "usage": {
                    "pause_5h_pct": 86.0,
                    "pause_7d_pct": 93.0,
                    "warn_5h_pct": 71.0,
                    "warn_7d_pct": 86.0,
                    "max_running": 3,
                    "auto_resume": False,
                }
            }
        ),
        encoding="utf-8",
    )

    run = _run([_script("chitra-usage"), "policy", "--policy-config", str(path)], env=_env())

    assert run.returncode == 0, run.stderr
    usage = json.loads(run.stdout)
    assert usage["pause_5h_pct"] == 86.0
    assert usage["max_running"] == 3
    assert usage["auto_resume"] is False


def test_auto_transfer_is_reported_in_the_effective_policy(tmp_path: Path) -> None:
    run = _run([_script("chitra-usage"), "policy"], env=_env(CHITRA_POLICY_CONFIG=None))
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["auto_transfer"] is True

    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"usage": {"auto_transfer": False}}), encoding="utf-8")
    run = _run([_script("chitra-usage"), "policy", "--policy-config", str(path)], env=_env())
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["auto_transfer"] is False


def test_a_valid_merge_policy_loads(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "merge": {
                    "enabled": True,
                    "allowed_repos": ["ReticleWorks/chitra"],
                    "lane_authors": ["lane-bot"],
                    "app_login": "polyphony-automation[bot]",
                    "poll_seconds": 300,
                }
            }
        ),
        encoding="utf-8",
    )

    run = _run([_script("chitra-usage"), "policy", "--policy-config", str(path)], env=_env())
    assert run.returncode == 0, run.stderr


# === test_receipt_path_confinement.py deny paths, through chitra-receipts =====

RECEIPT_SESSION = "host:path-confinement:0.0"
RECEIPT_NAME = "path-check"


def _write_receipt(
    receipt_dir: Path,
    target: Path,
    *,
    command: list[str],
    receipt_name: str = RECEIPT_NAME,
) -> Path:
    receipt_dir.mkdir(parents=True, exist_ok=True)
    report = receipt_dir / "report.json"
    report.write_text(
        json.dumps({"schema_version": "chitra-validator-report-v1", "command": command, "exit_code": 0}),
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "receipt_name": receipt_name,
        "validator": {"name": "pytest", "version": "test"},
        "target": {"artifact": {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}},
        "exercise": {"command": command},
        "result": {"status": "PASS", "validator_acceptance": True},
        "not_exercised": [],
        "artifacts": [{"path": "report.json", "kind": "report", "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}],
        "produced_at": "2026-08-26T00:00:00Z",
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "UTF-8 JSON; keys sorted; separators comma and colon; ensure_ascii false",
            "scope": "entire receipt with /integrity/digest omitted",
            "hand_authored_fields": [],
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    integrity = payload["integrity"]
    assert isinstance(integrity, dict)
    integrity["digest"] = hashlib.sha256(encoded).hexdigest()
    receipt = receipt_dir / f"{receipt_name}.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    return receipt


def _stored_receipt(
    root: Path,
    target: Path,
    *,
    command: list[str],
    receipt_name: str = RECEIPT_NAME,
) -> Path:
    return _write_receipt(
        receipt_path(root, RECEIPT_SESSION, receipt_name).parent,
        target,
        command=command,
        receipt_name=receipt_name,
    )


def _validators_file(directory: Path, argv: list[str]) -> Path:
    registry = directory / "validators.json"
    registry.write_text(json.dumps({"pytest": {"argv": argv}}), encoding="utf-8")
    return registry


def _receipts(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _run([_script("chitra-receipts"), *argv], env=env or _env())


def _verifier_argv0() -> str:
    """The interpreter path the receipt verifier binds its trusted argv to.

    ``chitra-receipts`` resolves ``sys.executable`` from its own shebang, so
    a declared exercise command must spell that exact interpreter — which is
    not necessarily the alias this test run itself was invoked under.
    """
    script = Path(_script("chitra-receipts"))
    shebang = script.read_text(encoding="utf-8").splitlines()[0]
    if shebang.startswith("#!"):
        return shebang[2:]
    return sys.executable


def _marker_command(marker: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]


@pytest.mark.parametrize("target_kind", ["traversal", "external", "symlink"])
def test_a_receipt_target_outside_the_approved_root_never_executes(tmp_path: Path, target_kind: str) -> None:
    """Traversal, external, and symlink targets are refused before the
    verifier invocation is ever spawned: the marker the target writes when
    pytest actually runs it stays absent."""
    root = tmp_path / "root"
    root.mkdir()
    marker = tmp_path / "validator-ran"
    # An empty registry leaves pytest on Chitra's trusted argv: python -m pytest
    # <declared target>.  Confinement gates that one subprocess call.
    registry = tmp_path / "validators.json"
    registry.write_text("{}", encoding="utf-8")

    def target_file(path: Path) -> Path:
        path.write_text(
            f"from pathlib import Path\ndef test_target() -> None:\n    Path({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        return path

    external = target_file(tmp_path / "external.py")
    if target_kind == "traversal":
        target_file(tmp_path / "target.py")
        (root / "nested").mkdir(parents=True)
        declared = root / "nested" / ".." / ".." / "target.py"
    elif target_kind == "external":
        declared = external
    else:
        declared = root / "target-alias.py"
        declared.symlink_to(external)

    command = [_verifier_argv0(), "-m", "pytest", str(declared)]
    _stored_receipt(root, declared, command=command)

    run = _receipts(
        "verify",
        "--root",
        str(root),
        "--session-ref",
        RECEIPT_SESSION,
        RECEIPT_NAME,
        env=_env(CHITRA_VALIDATORS_FILE=str(registry)),
    )

    assert run.returncode == 1, run.stdout + run.stderr
    output = json.loads(run.stdout)
    assert output["verified"] is False
    assert any(phrase in " ".join(output["issues"]) for phrase in ("traversal", "outside the approved workspace", "symlink"))
    assert not marker.exists()


def test_an_unregistered_in_workspace_target_verifies_and_executes(tmp_path: Path) -> None:
    """The confinement control: the same trusted pytest invocation runs for a
    real in-workspace target, and the receipt verifies."""
    root = tmp_path / "root"
    root.mkdir()
    marker = tmp_path / "validator-ran"
    registry = tmp_path / "validators.json"
    registry.write_text("{}", encoding="utf-8")
    target = root / "test_target.py"
    target.write_text(
        f"from pathlib import Path\ndef test_target() -> None:\n    Path({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    _stored_receipt(root, target, command=[_verifier_argv0(), "-m", "pytest", str(target)])

    run = _receipts(
        "verify",
        "--root",
        str(root),
        "--session-ref",
        RECEIPT_SESSION,
        RECEIPT_NAME,
        env=_env(CHITRA_VALIDATORS_FILE=str(registry)),
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["verified"] is True
    assert marker.read_text(encoding="utf-8") == "ran"


def test_an_in_workspace_target_verifies_and_runs_the_validator(tmp_path: Path) -> None:
    """The control for the confinement gate: a real target inside the root runs."""
    root = tmp_path / "root"
    root.mkdir()
    marker = tmp_path / "validator-ran"
    command = _marker_command(marker)
    registry = _validators_file(tmp_path, command)
    target = root / "test_target.py"
    target.write_text("def test_target() -> None:\n    assert True\n", encoding="utf-8")
    _stored_receipt(root, target, command=command)

    run = _receipts(
        "verify",
        "--root",
        str(root),
        "--session-ref",
        RECEIPT_SESSION,
        RECEIPT_NAME,
        env=_env(CHITRA_VALIDATORS_FILE=str(registry)),
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["verified"] is True
    assert marker.read_text(encoding="utf-8") == "ran"


def test_a_registered_validator_may_run_an_external_target_but_the_receipt_target_cannot_redirect(
    tmp_path: Path,
) -> None:
    """The receipt's declared target cannot steer the registered command."""
    root = tmp_path / "root"
    root.mkdir()
    external_target = tmp_path / "repository-test.py"
    external_target.write_text("def test_operator_target() -> None:\n    assert True\n", encoding="utf-8")
    misleading_target = root / "misleading_target.py"
    misleading_target.write_text(
        "def test_receipt_target() -> None:\n    raise AssertionError('receipt target ran')\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-m", "pytest", str(external_target)]
    registry = _validators_file(tmp_path, command)
    _stored_receipt(root, misleading_target, command=command)

    run = _receipts(
        "verify",
        "--root",
        str(root),
        "--session-ref",
        RECEIPT_SESSION,
        RECEIPT_NAME,
        env=_env(CHITRA_VALIDATORS_FILE=str(registry)),
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["verified"] is True


def test_a_rehashed_receipt_with_a_forged_target_digest_is_rejected(tmp_path: Path) -> None:
    """Re-sealing a receipt over a forged target digest does not verify."""
    root = tmp_path / "root"
    root.mkdir()
    command = [sys.executable, "-c", "raise SystemExit(0)"]
    registry = _validators_file(tmp_path, command)
    target = root / "validator-output.log"
    target.write_text("observed output\n", encoding="utf-8")
    receipt = _stored_receipt(root, target, command=command)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["target"]["artifact"]["sha256"] = "0" * 64
    payload["integrity"].pop("digest")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload["integrity"]["digest"] = hashlib.sha256(encoded).hexdigest()
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    run = _receipts(
        "verify",
        "--root",
        str(root),
        "--session-ref",
        RECEIPT_SESSION,
        RECEIPT_NAME,
        env=_env(CHITRA_VALIDATORS_FILE=str(registry)),
    )

    assert run.returncode == 1, run.stdout + run.stderr
    output = json.loads(run.stdout)
    assert output["verified"] is False
    assert "current target artifact digest does not match the receipt" in output["issues"]


def test_a_registry_beside_the_receipt_cannot_select_the_command(tmp_path: Path) -> None:
    """A validators.json dropped beside the stored receipt is not trusted."""
    root = tmp_path / "root"
    root.mkdir()
    marker = tmp_path / "source-registry-ran"
    malicious_command = _marker_command(marker)
    trusted_command = [sys.executable, "-c", "import sys; sys.exit(0)"]
    registry = _validators_file(tmp_path, trusted_command)
    target = root / "target.py"
    target.write_text("def test_target() -> None:\n    assert True\n", encoding="utf-8")

    receipt = _stored_receipt(root, target, command=malicious_command)
    # The attacker's registry sits right beside the stored receipt.
    (receipt.parent / "validators.json").write_text(
        json.dumps({"pytest": {"argv": malicious_command}}),
        encoding="utf-8",
    )

    env = _env(CHITRA_VALIDATORS_FILE=str(registry))
    run = _receipts(
        "verify",
        "--root",
        str(root),
        "--session-ref",
        RECEIPT_SESSION,
        RECEIPT_NAME,
        env=env,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert json.loads(run.stdout)["verified"] is False
    assert not marker.exists()

    trusted_receipt_dir = receipt_path(root, RECEIPT_SESSION, "path-check-trusted").parent
    _write_receipt(
        trusted_receipt_dir,
        target,
        command=trusted_command,
        receipt_name="path-check-trusted",
    )
    accepted = _receipts(
        "verify",
        "--root",
        str(root),
        "--session-ref",
        RECEIPT_SESSION,
        "path-check-trusted",
        env=env,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert json.loads(accepted.stdout)["verified"] is True


def _receipt_goal(root: Path) -> None:
    upsert_goal(
        root,
        GoalRecord(
            session_ref=RECEIPT_SESSION,
            goal="Verify an external receipt source safely.",
            done_when="The external receipt verifies.",
            source="task-file:/tmp/external-ingest.md",
            status="working",
            intent="Use only the trusted state registry.",
            scope="External receipt ingest.",
            interview_receipt=VALID_INTERVIEW_RECEIPT,
            enrolled_done_when_items=(
                EnrolledDoneWhenItem(
                    id="done-1",
                    text="The external receipt verifies.",
                    validator="pytest",
                    required_receipt=RECEIPT_NAME,
                ),
            ),
        ),
    )


def test_ingest_uses_only_the_trusted_registry_for_external_sources(tmp_path: Path) -> None:
    """Ingesting an upload never runs the command its own registry offers."""
    root = tmp_path / "root"
    root.mkdir()
    _receipt_goal(root)
    marker = tmp_path / "source-registry-ran"
    malicious_command = _marker_command(marker)
    trusted_command = [sys.executable, "-m", "pytest"]
    registry = _validators_file(tmp_path, trusted_command)
    target = root / "target.py"
    target.write_text("def test_target() -> None:\n    assert True\n", encoding="utf-8")

    malicious_source = tmp_path / "malicious-upload"
    receipt = _write_receipt(malicious_source, target, command=malicious_command)
    (malicious_source / "validators.json").write_text(
        json.dumps({"pytest": {"argv": malicious_command}}),
        encoding="utf-8",
    )

    env = _env(CHITRA_VALIDATORS_FILE=str(registry))
    refused = _receipts(
        "ingest",
        "--root",
        str(root),
        "--session-ref",
        RECEIPT_SESSION,
        str(receipt),
        env=env,
    )
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert not marker.exists()
    assert not receipt_path(root, RECEIPT_SESSION, RECEIPT_NAME).exists()

    # The enrollment pinned pytest to the conftest registry definition; the
    # trusted registry must carry that same argv or the receipt is drifted.
    trusted_command_full = [sys.executable, "-c", "import sys; sys.exit(0)"]
    registry.write_text(json.dumps({"pytest": {"argv": trusted_command_full}}), encoding="utf-8")
    trusted_source = _write_receipt(tmp_path / "trusted-upload", target, command=trusted_command_full)
    accepted = _receipts(
        "ingest",
        "--root",
        str(root),
        "--session-ref",
        RECEIPT_SESSION,
        str(trusted_source),
        env=env,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert json.loads(accepted.stdout)["stored"] is True
    assert receipt_path(root, RECEIPT_SESSION, RECEIPT_NAME).is_file()

"""Credential-free acceptance tests for transcript-to-goal supervision.

These tests deliberately exercise the monitor entrypoint instead of seeding
``EventJournal`` directly.  A transcript binding is the authority that tells
the monitor which enrolled lane a transcript belongs to; a missing or
inconsistent binding must never be guessed from another goal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _goal_fixtures import enrollment_fields, ingest_passing_receipt, passing_completion_evidence

import chitra.dispatchd as dispatchd_mod
import chitra.ledger as ledger_mod
import chitra.monitord as monitord_mod
from chitra.completion_gate import CompletionEvidence
from chitra.detect import Finding, IncidentStore
from chitra.goals import GoalRecord, add_ask, get_goal, redirect_goal, update_now, upsert_goal
from chitra.journal import CanonicalEvent
from chitra.journal.store import EventJournal
from chitra.monitord import IDLE_PURSUIT_PASSES, main, resolve_config, run_once
from chitra.orders import DispatchOrder, DispatchResult, DispatchStatus
from chitra.supervision import SupervisionLedger, deterministic_order_id, goal_digest

CLAUDE_VERSION = "2.1.229"
INSTANCE = "pytest-supervisor"


def _goal(session_ref: str) -> GoalRecord:
    done_when = f"The enrolled check for {session_ref} passes."
    return GoalRecord(
        session_ref=session_ref,
        goal=f"Advance the exact goal for {session_ref}.",
        done_when=done_when,
        source=f"task-file:/tmp/{session_ref.replace(':', '-')}.md",
        status="working",
        intent="Keep this enrolled lane moving toward its recorded outcome.",
        scope="The credential-free persistent-supervision acceptance test.",
        **enrollment_fields(done_when),
    )


def _write_claude_transcript(path: Path, *, session_id: str, marker: str) -> None:
    """Write the smallest fixture that yields journal events without a client."""
    rows: list[dict[str, Any]] = [
        {
            "parentUuid": None,
            "sessionId": session_id,
            "uuid": f"{marker}-user",
            "version": CLAUDE_VERSION,
            "type": "user",
            "message": {"role": "user", "content": f"Work the enrolled goal for {marker}."},
        },
        {
            "parentUuid": f"{marker}-user",
            "sessionId": session_id,
            "uuid": f"{marker}-assistant",
            "version": CLAUDE_VERSION,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu-{marker}",
                        "name": "Bash",
                        "input": {"command": f"printf {marker}"},
                    }
                ],
            },
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_bindings(path: Path, bindings: list[dict[str, str]]) -> None:
    path.write_text(
        json.dumps({"schema": "chitra.transcript-bindings.v1", "bindings": bindings}, indent=2),
        encoding="utf-8",
    )


def _binding(*, session_ref: str, lane: str, path: Path) -> dict[str, str]:
    return {
        "session_ref": session_ref,
        "lane": lane,
        "path": str(path),
        "client": "claude",
        "client_version": CLAUDE_VERSION,
        "instance": INSTANCE,
    }


def _run_monitor(state: Path, bindings: Path) -> int:
    """Use the agreed bindings config path, as a deployed CLI would."""
    return main(
        [
            "--state-dir",
            str(state),
            "--transcript-bindings-path",
            str(bindings),
            "--once",
        ]
    )


def test_monitord_ingests_two_bound_transcripts_with_exact_goal_refs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    transcript_root = tmp_path / "transcripts"
    bindings_path = tmp_path / "transcript-bindings.json"
    goals = (_goal("host:alpha:0.0"), _goal("host:beta:0.0"))
    for goal in goals:
        upsert_goal(state, goal)

    alpha_transcript = transcript_root / "alpha.jsonl"
    beta_transcript = transcript_root / "beta.jsonl"
    _write_claude_transcript(alpha_transcript, session_id="native-alpha", marker="alpha")
    _write_claude_transcript(beta_transcript, session_id="native-beta", marker="beta")
    _write_bindings(
        bindings_path,
        [
            _binding(session_ref="host:alpha:0.0", lane="alpha", path=alpha_transcript),
            _binding(session_ref="host:beta:0.0", lane="beta", path=beta_transcript),
        ],
    )

    # There are no pre-seeded journals. Discovery must start at the native
    # transcript paths named by the versioned binding manifest.
    assert not (state / "journal").exists()
    assert _run_monitor(state, bindings_path) == 0
    capsys.readouterr()

    for session_ref, lane in (("host:alpha:0.0", "alpha"), ("host:beta:0.0", "beta")):
        events = EventJournal(state, lane).load()
        assert events
        assert {event.lane for event in events} == {lane}
        assert {event.goal_ref for event in events} == {session_ref}
        assert {event.instance for event in events} == {INSTANCE}


def test_unbound_or_mismatched_binding_cannot_fall_back_to_first_goal_or_enroll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    bindings_path = tmp_path / "transcript-bindings.json"
    alpha = upsert_goal(state, _goal("host:alpha:0.0"))

    mismatched_transcript = tmp_path / "transcripts" / "mismatched.jsonl"
    unbound_transcript = tmp_path / "transcripts" / "unbound.jsonl"
    _write_claude_transcript(mismatched_transcript, session_id="native-mismatch", marker="mismatch")
    _write_claude_transcript(unbound_transcript, session_id="native-unbound", marker="unbound")
    _write_bindings(
        bindings_path,
        [
            # The session is enrolled, but its declared lane disagrees with
            # the immutable GoalRecord lane_id and must be rejected.
            _binding(session_ref=alpha.session_ref, lane="not-alpha", path=mismatched_transcript),
            # There is no GoalRecord for this session at all.
            _binding(session_ref="host:unbound:0.0", lane="unbound", path=unbound_transcript),
        ],
    )

    import chitra.monitord as monitord

    enrolled_sessions: list[str] = []

    def record_runs(config: object, session_ref: str, items: object) -> tuple[object, ...]:
        enrolled_sessions.append(session_ref)
        return ()

    monkeypatch.setattr(monitord, "record_enrolled_validator_runs", record_runs)
    assert _run_monitor(state, bindings_path) == 0
    capsys.readouterr()

    # Observation is still allowed for diagnosis. What is forbidden is
    # borrowing alpha's goal for either unresolved lane.
    assert EventJournal(state, "not-alpha").load()
    assert EventJournal(state, "unbound").load()
    assert all(event.goal_ref == alpha.session_ref for event in EventJournal(state, "not-alpha").load())
    assert all(event.goal_ref == "host:unbound:0.0" for event in EventJournal(state, "unbound").load())
    assert enrolled_sessions == []
    assert get_goal(state, alpha.session_ref) == alpha
    assert not (state / "queue" / "orders").exists()


def test_lane_rebinding_filters_prior_transcript_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    goal = upsert_goal(state, _goal("host:alpha:0.0"))
    old_transcript = tmp_path / "transcripts" / "old.jsonl"
    new_transcript = tmp_path / "transcripts" / "new.jsonl"
    _write_claude_transcript(old_transcript, session_id="native-old", marker="old")
    _write_claude_transcript(new_transcript, session_id="native-new", marker="new")
    _write_bindings(
        bindings_path,
        [_binding(session_ref=goal.session_ref, lane=goal.lane_id, path=old_transcript)],
    )
    observed: list[tuple[str, ...]] = []

    def capture(_config: object, _lane: str, _goal: object, events: tuple[CanonicalEvent, ...]) -> list[Finding]:
        observed.append(tuple(event.session_id for event in events))
        return []

    monkeypatch.setattr(monitord_mod, "run_detectors", capture)
    run_once(_live_config(state, bindings_path, queue))

    _write_bindings(
        bindings_path,
        [_binding(session_ref=goal.session_ref, lane=goal.lane_id, path=new_transcript)],
    )
    run_once(_live_config(state, bindings_path, queue))

    assert observed == [("native-old", "native-old"), ("native-new", "native-new")]


def test_same_path_native_session_replacement_filters_old_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    goal = upsert_goal(state, _goal("host:alpha:0.0"))
    transcript = tmp_path / "transcripts" / "alpha.jsonl"
    _write_claude_transcript(transcript, session_id="native-old", marker="old")
    _write_bindings(
        bindings_path,
        [_binding(session_ref=goal.session_ref, lane=goal.lane_id, path=transcript)],
    )
    observed: list[tuple[str, ...]] = []

    def capture(_config: object, _lane: str, _goal: object, events: tuple[CanonicalEvent, ...]) -> list[Finding]:
        observed.append(tuple(event.session_id for event in events))
        return []

    monkeypatch.setattr(monitord_mod, "run_detectors", capture)
    run_once(_live_config(state, bindings_path, queue))

    replacement = tmp_path / "transcripts" / "replacement.jsonl"
    _write_claude_transcript(replacement, session_id="native-new", marker="new")
    replacement.replace(transcript)
    run_once(_live_config(state, bindings_path, queue))

    assert observed == [("native-old", "native-old"), ("native-new", "native-new")]


def test_idle_pursuit_is_persistent_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    monkeypatch.setattr(monitord_mod, "run_detectors", lambda *_args, **_kwargs: [])

    run_once(_live_config(state, bindings_path, queue))
    run_once(_live_config(state, bindings_path, queue))
    assert not (state / "incidents" / f"{goal.lane_id}.jsonl").exists()

    # A fresh monitor process must retain the clean-pass count and emit one
    # deterministic pursuit intent on the threshold pass.
    restarted = _live_config(state, bindings_path, queue)
    run_once(restarted)
    incidents = IncidentStore(state, goal.lane_id).load()
    assert len(incidents) == 1
    assert incidents[0].detector == "idle_pursuit"
    assert incidents[0].unmet_item == goal.enrolled_done_when_items[0].id
    assert incidents[0].stage == "nudge"

    run_once(_live_config(state, bindings_path, queue))
    assert len(IncidentStore(state, goal.lane_id).load()) == 1


def test_idle_pursuit_does_not_fire_while_a_question_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    monkeypatch.setattr(monitord_mod, "run_detectors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(monitord_mod, "handle_agent_question", lambda *_args, **_kwargs: "answer_queued")

    for _ in range(IDLE_PURSUIT_PASSES + 1):
        run_once(_live_config(state, bindings_path, queue))

    assert not (state / "incidents" / f"{goal.lane_id}.jsonl").exists()


def test_idle_pursuit_does_not_compete_with_an_undelivered_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    monkeypatch.setattr(monitord_mod, "run_detectors", lambda *_args, **_kwargs: [])
    SupervisionLedger(state, goal.lane_id).transition(
        state="action_pending",
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest_value=goal_digest(goal),
        reason="the existing corrective action still awaits delivery",
        finding_fingerprint="existing-finding",
        stage="nudge",
        order_id="existing-order",
        order_marker="CHITRA-ORDER:existing-order",
    )

    for _ in range(IDLE_PURSUIT_PASSES + 1):
        run_once(_live_config(state, bindings_path, queue))

    assert not (state / "incidents" / f"{goal.lane_id}.jsonl").exists()


def test_idle_pursuit_does_not_bypass_an_operator_ask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    monkeypatch.setattr(monitord_mod, "run_detectors", lambda *_args, **_kwargs: [])
    add_ask(state, goal.session_ref, "operator approval is required")
    update_now(state, goal.session_ref, now="waiting for operator approval", status="blocked")

    for _ in range(IDLE_PURSUIT_PASSES + 1):
        run_once(_live_config(state, bindings_path, queue))

    assert not (state / "incidents" / f"{goal.lane_id}.jsonl").exists()
    assert not (queue / "orders").exists()


def test_idle_pursuit_waits_for_an_existing_consumed_action_to_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    monkeypatch.setattr(monitord_mod, "run_detectors", lambda *_args, **_kwargs: [])
    ledger = SupervisionLedger(state, goal.lane_id)
    common = {
        "session_ref": goal.session_ref,
        "goal_version": goal.goal_version,
        "goal_digest_value": goal_digest(goal),
        "finding_fingerprint": "existing-finding",
        "stage": "nudge",
        "order_id": "existing-order",
        "order_marker": "CHITRA-ORDER:existing-order",
    }
    ledger.transition(state="action_pending", reason="existing action recorded", **common)
    ledger.transition(state="action_queued", reason="existing action queued", **common)
    ledger.transition(state="awaiting_progress", reason="existing action consumed", **common)

    for _ in range(IDLE_PURSUIT_PASSES + 1):
        run_once(_live_config(state, bindings_path, queue))

    assert not (state / "incidents" / f"{goal.lane_id}.jsonl").exists()


def _prepare_repeated_action_case(tmp_path: Path) -> tuple[Path, Path, Path, GoalRecord]:
    """Build one bound lane whose fixture has three identical tool calls."""
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    goal = upsert_goal(state, _goal("host:alpha:0.0"))
    transcript = tmp_path / "transcripts" / "alpha.jsonl"
    fixture = Path(__file__).parent / "fixtures" / "failure-modes" / "claude-unnecessary-steps.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_bytes(fixture.read_bytes())
    _write_bindings(
        bindings_path,
        [_binding(session_ref=goal.session_ref, lane=goal.lane_id, path=transcript)],
    )
    return state, bindings_path, queue, goal


def _live_config(state: Path, bindings_path: Path, queue: Path) -> object:
    return resolve_config(
        state_dir=state,
        transcript_bindings_path=bindings_path,
        dispatch_queue_dir=queue,
        shadow_mode=False,
    )


def _stub_finding(name: str) -> Finding:
    return Finding(
        detector=f"test-{name}",
        fingerprint_seed={"name": name},
        event_refs=(),
        unmet_item="done-1",
        expected_next_progress=f"make progress for {name}",
        detail=f"finding {name}",
    )


def test_run_once_evaluates_only_the_fair_scheduled_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later findings must get their first ladder sighting on a later pass."""
    state, bindings_path, queue, _goal_record = _prepare_repeated_action_case(tmp_path)
    findings = [_stub_finding("first"), _stub_finding("second")]
    monkeypatch.setattr(monitord_mod, "run_detectors", lambda *_args, **_kwargs: findings)

    first_pass = run_once(_live_config(state, bindings_path, queue))
    assert first_pass["findings_opened"] == 1
    incidents = IncidentStore(state, "alpha").load()
    assert [record.detector for record in incidents] == ["test-first"]

    second_pass = run_once(_live_config(state, bindings_path, queue))
    assert second_pass["findings_opened"] == 1
    incidents = IncidentStore(state, "alpha").load()
    assert [record.detector for record in incidents] == ["test-first", "test-second"]


def test_goal_revision_gives_the_same_observation_a_fresh_nudge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    finding = _stub_finding("same-observation")
    monkeypatch.setattr(monitord_mod, "run_detectors", lambda *_args, **_kwargs: [finding])

    run_once(_live_config(state, bindings_path, queue))
    first = IncidentStore(state, goal.lane_id).load()
    assert len(first) == 1
    assert first[0].stage == "nudge"

    redirect_goal(
        state,
        goal.session_ref,
        reason="operator changed the bounded strategy",
        goal="Advance the exact goal with the revised strategy.",
    )
    run_once(_live_config(state, bindings_path, queue))
    incidents = IncidentStore(state, goal.lane_id).load()
    assert len(incidents) == 2
    assert [record.stage for record in incidents] == ["nudge", "nudge"]
    assert incidents[0].fingerprint != incidents[1].fingerprint


def test_unbound_observation_then_goal_binding_opens_initial_nudge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    transcript = tmp_path / "transcripts" / "alpha.jsonl"
    _write_claude_transcript(transcript, session_id="native-alpha", marker="alpha")
    _write_bindings(
        bindings_path,
        [_binding(session_ref="host:alpha:0.0", lane="alpha", path=transcript)],
    )
    finding = _stub_finding("unbound-then-bound")
    monkeypatch.setattr(monitord_mod, "run_detectors", lambda *_args, **_kwargs: [finding])

    run_once(_live_config(state, bindings_path, queue))
    assert not (state / "incidents" / "alpha.jsonl").exists()

    goal = upsert_goal(state, _goal("host:alpha:0.0"))
    run_once(_live_config(state, bindings_path, queue))
    incidents = IncidentStore(state, goal.lane_id).load()
    assert len(incidents) == 1
    assert incidents[0].stage == "nudge"


def _expected_nudge_order(state: Path, goal: GoalRecord, queue: Path) -> tuple[str, DispatchOrder]:
    findings_path = state / "monitord-findings.jsonl"
    records = [json.loads(line) for line in findings_path.read_text(encoding="utf-8").splitlines()]
    repeated = next(record for record in records if record["detector"] == "unnecessary_steps")
    order_id = deterministic_order_id(goal.session_ref, goal.goal_version, repeated["fingerprint"], "nudge")
    order_path = queue / "orders" / f"{order_id}.json"
    return order_id, DispatchOrder.model_validate_json(order_path.read_text(encoding="utf-8"))


def test_live_bound_three_repeat_finding_enqueues_one_goal_bound_action_and_is_restart_idempotent(
    tmp_path: Path,
) -> None:
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    config = _live_config(state, bindings_path, queue)

    first = run_once(config)
    assert first["lanes_observed"] == 1
    order_id, order = _expected_nudge_order(state, goal, queue)
    assert order.order_id == order_id
    assert order.session_ref == goal.session_ref
    assert "[M] monitord" in order.nudge
    assert goal.goal in order.nudge
    assert "change approach so the repeated read produces new scoped state" in order.nudge

    ledger = SupervisionLedger(state, goal.lane_id)
    latest = ledger.latest()
    assert latest is not None
    assert latest.state == "action_queued"
    assert latest.session_ref == goal.session_ref
    assert latest.order_id == order_id
    before = ledger.load()

    # A fresh monitor process must reconcile its durable action rather than
    # enqueueing a second order or appending a duplicate transition.
    run_once(_live_config(state, bindings_path, queue))
    after = ledger.load()
    assert len(list((queue / "orders").glob("*.json"))) == 1
    assert [record.event_id for record in after] == [record.event_id for record in before]


def test_action_pending_restarts_and_retries_enqueue_with_same_deterministic_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    import chitra.supervisor as supervisor
    real_enqueue = supervisor.enqueue_dispatch_order

    def fail_enqueue(*args: object, **kwargs: object) -> Path:
        raise RuntimeError("simulated enqueue crash after action_pending")

    monkeypatch.setattr(supervisor, "enqueue_dispatch_order", fail_enqueue)
    with pytest.raises(RuntimeError, match="action_pending"):
        run_once(_live_config(state, bindings_path, queue))

    pending = SupervisionLedger(state, goal.lane_id).latest()
    assert pending is not None
    assert pending.state == "action_pending"
    assert not list((queue / "orders").glob("*.json"))

    monkeypatch.setattr(supervisor, "enqueue_dispatch_order", real_enqueue)
    run_once(_live_config(state, bindings_path, queue))
    order_id, order = _expected_nudge_order(state, goal, queue)
    assert order.order_id == order_id
    assert order.session_ref == goal.session_ref
    assert SupervisionLedger(state, goal.lane_id).latest().state == "action_queued"  # type: ignore[union-attr]
    assert len(list((queue / "orders").glob("*.json"))) == 1


def test_action_queued_transition_restarts_after_order_enqueue_without_duplicate_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    real_transition = SupervisionLedger.transition

    def fail_queued_transition(self: SupervisionLedger, **kwargs: object) -> object:
        if kwargs.get("state") == "action_queued":
            raise RuntimeError("simulated action_queued ledger crash")
        return real_transition(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SupervisionLedger, "transition", fail_queued_transition)
    with pytest.raises(RuntimeError, match="action_queued"):
        run_once(_live_config(state, bindings_path, queue))

    queued_order = list((queue / "orders").glob("*.json"))
    assert len(queued_order) == 1
    pending = SupervisionLedger(state, goal.lane_id).latest()
    assert pending is not None
    assert pending.state == "action_pending"

    monkeypatch.setattr(SupervisionLedger, "transition", real_transition)
    run_once(_live_config(state, bindings_path, queue))
    order_id, order = _expected_nudge_order(state, goal, queue)
    assert order.order_id == order_id
    order_path = queue / "orders" / f"{order_id}.json"
    assert order_path.exists()
    assert SupervisionLedger(state, goal.lane_id).latest().state == "action_queued"  # type: ignore[union-attr]
    assert len(list((queue / "orders").glob("*.json"))) == 1


def _append_nudge_and_final_response(transcript: Path, nudge: str, *, session_id: str) -> None:
    """Append a real Claude user turn and its later final response."""
    rows = [
        {
            "parentUuid": "fixture-assistant",
            "sessionId": session_id,
            "uuid": "oversight-user",
            "version": CLAUDE_VERSION,
            "type": "user",
            "message": {"role": "user", "content": nudge},
        },
        {
            "parentUuid": "oversight-user",
            "sessionId": session_id,
            "uuid": "oversight-assistant",
            "version": CLAUDE_VERSION,
            "type": "assistant",
            "message": {
                "id": "oversight-message",
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "The next scoped progress is complete and independently evidenced.",
                    }
                ],
            },
        },
        {
            "parentUuid": "oversight-assistant",
            "sessionId": session_id,
            "uuid": "oversight-stop-hook",
            "version": CLAUDE_VERSION,
            "type": "system",
            "subtype": "stop_hook_summary",
        },
        {
            "parentUuid": "oversight-stop-hook",
            "sessionId": session_id,
            "uuid": "oversight-turn-duration",
            "version": CLAUDE_VERSION,
            "type": "system",
            "subtype": "turn_duration",
        },
    ]
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(row) + "\n" for row in rows))


def _append_repeated_post_consumption_calls(transcript: Path, *, session_id: str) -> None:
    rows: list[dict[str, Any]] = []
    for index in range(3):
        call_id = f"toolu-post-consumption-{index}"
        rows.extend(
            [
                {
                    "sessionId": session_id,
                    "uuid": f"post-call-{index}",
                    "version": CLAUDE_VERSION,
                    "type": "assistant",
                    "message": {
                        "id": f"post-message-{index}",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": "Bash",
                                "input": {"command": "cat src/module.py"},
                            }
                        ],
                    },
                },
                {
                    "sessionId": session_id,
                    "uuid": f"post-result-{index}",
                    "version": CLAUDE_VERSION,
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": "def existing():\n    pass\n",
                                "is_error": False,
                                "tool_use_id": call_id,
                            }
                        ],
                    },
                },
            ]
        )
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(row) + "\n" for row in rows))


def test_monitor_dispatchd_monitor_reconciles_signed_delivery_and_advances_only_after_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed delivery plus a later assistant turn is the only ladder proof."""
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    transcript = tmp_path / "transcripts" / "alpha.jsonl"
    first_config = _live_config(state, bindings_path, queue)

    first = run_once(first_config)
    assert first["lanes_observed"] == 1
    order_id, order = _expected_nudge_order(state, goal, queue)
    finding_fingerprint = next(
        record["fingerprint"]
        for record in map(
            json.loads,
            (state / "monitord-findings.jsonl").read_text(encoding="utf-8").splitlines(),
        )
        if record["detector"] == "unnecessary_steps"
    )
    initial_incident = IncidentStore(state, goal.lane_id).latest(
        finding_fingerprint
    )
    assert initial_incident is not None
    assert initial_incident.stage == "nudge"
    assert initial_incident.consumption is None

    native_session_id = json.loads(transcript.read_text(encoding="utf-8").splitlines()[0])["sessionId"]

    def fake_dispatch(dispatch_order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        assert dispatch_order.order_id == order_id
        assert dispatch_order.nudge == order.nudge
        _append_nudge_and_final_response(transcript, dispatch_order.nudge, session_id=native_session_id)
        return DispatchResult(
            order_id=dispatch_order.order_id,
            session_ref=dispatch_order.session_ref,
            status=DispatchStatus.SENT,
            transcript_path=str(transcript),
        )

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    dispatch_results = dispatchd_mod.run_once(
        queue,
        lock_dir=tmp_path / "dispatch-locks",
        ledger_path=state / "ledger.jsonl",
        ledger_key_path=state / "ledger.key",
        goals_root=state,
        projects_root=tmp_path / "projects",
    )
    assert len(dispatch_results) == 1
    assert dispatch_results[0].status is DispatchStatus.SENT
    assert dispatch_results[0].delivery_ledger_verified is True
    assert order.nudge in transcript.read_text(encoding="utf-8")
    assert "The next scoped progress is complete" in transcript.read_text(encoding="utf-8")

    ledger_lines = (state / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ledger_lines) == 1
    signed_entry = ledger_mod.LedgerEntry.model_validate_json(ledger_lines[0])
    assert signed_entry.order_id == order_id
    assert signed_entry.session_ref == goal.session_ref
    assert signed_entry.native_session_id == native_session_id
    assert ledger_mod.verify_entry(signed_entry, key=(state / "ledger.key").read_bytes())

    # The monitor must ingest the appended user turn and later final response,
    # then attach consumption proof. It must not advance merely because time
    # elapsed or because dispatchd wrote a SENT result.
    second = run_once(_live_config(state, bindings_path, queue))
    assert second["lanes_observed"] == 1
    supervised = SupervisionLedger(state, goal.lane_id).latest()
    assert supervised is not None
    assert supervised.state == "awaiting_progress"
    consumed = IncidentStore(state, goal.lane_id).latest(initial_incident.fingerprint)
    assert consumed is not None
    assert consumed.stage == "nudge"
    assert consumed.consumption is not None
    assert consumed.consumption.user_event_id
    assert consumed.consumption.turn_event_id

    # The historical finding alone cannot advance after consumption.
    third = run_once(_live_config(state, bindings_path, queue))
    assert third["lanes_observed"] == 1
    assert third["findings_opened"] == 0
    assert SupervisionLedger(state, goal.lane_id).latest().state == "observing"  # type: ignore[union-attr]
    assert IncidentStore(state, goal.lane_id).latest(initial_incident.fingerprint).stage == "nudge"  # type: ignore[union-attr]

    # Only a genuine post-consumption recurrence may issue the next stage.
    _append_repeated_post_consumption_calls(transcript, session_id=native_session_id)
    fourth = run_once(_live_config(state, bindings_path, queue))
    assert fourth["lanes_observed"] == 1
    advanced = IncidentStore(state, goal.lane_id).latest(initial_incident.fingerprint)
    assert advanced is not None
    assert advanced.stage == "redirect"
    assert advanced.consumption is None
    assert len(list((queue / "orders").glob("*.json"))) == 1
    next_order = DispatchOrder.model_validate_json(next((queue / "orders").glob("*.json")).read_text(encoding="utf-8"))
    assert next_order.order_id != order_id
    assert next_order.session_ref == goal.session_ref


def test_sent_result_without_valid_signed_ledger_proof_cannot_count_as_consumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged SENT result cannot make supervision believe the lane consumed it."""
    state, bindings_path, queue, goal = _prepare_repeated_action_case(tmp_path)
    run_once(_live_config(state, bindings_path, queue))
    order_id, order = _expected_nudge_order(state, goal, queue)

    result_path = queue / "results" / f"{order_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        DispatchResult(
            order_id=order_id,
            session_ref=goal.session_ref,
            status=DispatchStatus.SENT,
            transcript_path=None,
            delivery_ledger_verified=True,
        ).model_dump_json(),
        encoding="utf-8",
    )
    # The result claims proof, but no signed delivery row exists. The exact
    # nudge also never entered the bound transcript.
    assert not (state / "ledger.jsonl").exists()
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", lambda *_args, **_kwargs: pytest.fail("monitor must not dispatch"))

    run_once(_live_config(state, bindings_path, queue))
    latest = SupervisionLedger(state, goal.lane_id).latest()
    assert latest is not None
    assert latest.state == "action_queued"
    fingerprint = next(
        record["fingerprint"]
        for record in map(json.loads, (state / "monitord-findings.jsonl").read_text(encoding="utf-8").splitlines())
        if record["detector"] == "unnecessary_steps"
    )
    incident = IncidentStore(state, goal.lane_id).latest(fingerprint)
    assert incident is not None
    assert incident.consumption is None
    pending = list((queue / "orders").glob("*.json"))
    assert len(pending) == 1
    assert pending[0].stem == order.order_id


def _append_completion_response(transcript: Path, *, session_id: str, claim: str) -> None:
    rows = [
        {
            "parentUuid": "fixture-assistant",
            "sessionId": session_id,
            "uuid": "completion-assistant",
            "version": CLAUDE_VERSION,
            "type": "assistant",
            "message": {
                "id": "completion-message",
                "role": "assistant",
                "content": [{"type": "text", "text": claim}],
            },
        },
        {
            "parentUuid": "completion-assistant",
            "sessionId": session_id,
            "uuid": "completion-stop-hook",
            "version": CLAUDE_VERSION,
            "type": "system",
            "subtype": "stop_hook_summary",
        },
        {
            "parentUuid": "completion-stop-hook",
            "sessionId": session_id,
            "uuid": "completion-turn-duration",
            "version": CLAUDE_VERSION,
            "type": "system",
            "subtype": "turn_duration",
        },
    ]
    with transcript.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _completion_claim_line(*, receipt_name: str = "tests-green", validator_result: str = "pass") -> str:
    return "Done.\nCHITRA-COMPLETION: " + json.dumps(
        {
            "kind": "artifact",
            "done_when_item_id": "done-1",
            "receipt_name": receipt_name,
            "validator": "pytest",
            "validator_result": validator_result,
            "citation": "proof /tmp/completion-report.json",
        },
        separators=(",", ":"),
    )


def _completion_case(tmp_path: Path, *, session_ref: str = "host:alpha:0.0") -> tuple[Path, Path, Path, GoalRecord, Path]:
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    goal = upsert_goal(state, _goal(session_ref))
    transcript = tmp_path / "transcripts" / f"{goal.lane_id}.jsonl"
    _write_claude_transcript(transcript, session_id=f"native-{goal.lane_id}", marker=goal.lane_id)
    _write_bindings(
        bindings_path,
        [_binding(session_ref=goal.session_ref, lane=goal.lane_id, path=transcript)],
    )
    return state, bindings_path, queue, goal, transcript


def test_monitor_does_not_run_enrolled_validators_without_a_completion_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal, _transcript = _completion_case(tmp_path)
    calls: list[str] = []

    def record_runs(root: Path, session_ref: str, items: object) -> tuple[CompletionEvidence, ...]:
        del root, items
        calls.append(session_ref)
        return ()

    monkeypatch.setattr(monitord_mod, "record_enrolled_validator_runs", record_runs)
    summary = run_once(_live_config(state, bindings_path, queue))

    assert summary["completion_disputed"] is False
    assert summary["validator_receipts_recorded"] == 0
    assert calls == []
    assert get_goal(state, goal.session_ref).status == "working"  # type: ignore[union-attr]


def test_failing_enrolled_validator_disputes_a_structured_completion_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal, transcript = _completion_case(tmp_path)
    claim = _completion_claim_line(validator_result="pass")
    _append_completion_response(transcript, session_id="native-alpha", claim=claim)

    def failing_run(root: Path, session_ref: str, items: object) -> tuple[CompletionEvidence, ...]:
        del root, session_ref, items
        return (
            CompletionEvidence(
                done_when_item_id="done-1",
                receipt_name="tests-green",
                validator="pytest",
                validator_result="fail",
                citation="proof /tmp/failing-report.json",
            ),
        )

    monkeypatch.setattr(monitord_mod, "record_enrolled_validator_runs", failing_run)
    summary = run_once(_live_config(state, bindings_path, queue))

    assert summary["completion_disputed"] is True
    stored = get_goal(state, goal.session_ref)
    assert stored is not None
    assert stored.status == "completion-disputed"
    supervision = SupervisionLedger(state, goal.lane_id).latest()
    assert supervision is not None
    assert supervision.state != "completion_verified"


def test_passing_validator_and_structured_claim_mark_goal_verified_and_supervision_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal, transcript = _completion_case(tmp_path)
    ingest_passing_receipt(state, goal.session_ref)
    _append_completion_response(
        transcript,
        session_id="native-alpha",
        claim=_completion_claim_line(),
    )

    def passing_run(root: Path, session_ref: str, items: object) -> tuple[CompletionEvidence, ...]:
        del root, session_ref, items
        return (passing_completion_evidence(),)

    monkeypatch.setattr(monitord_mod, "record_enrolled_validator_runs", passing_run)
    summary = run_once(_live_config(state, bindings_path, queue))

    assert summary["completion_disputed"] is False
    stored = get_goal(state, goal.session_ref)
    assert stored is not None
    assert stored.status == "done-pending-close"
    assert stored.completion_proofs
    supervision = SupervisionLedger(state, goal.lane_id).latest()
    assert supervision is not None
    assert supervision.state == "completion_verified"
    assert supervision.session_ref == goal.session_ref


def test_fabricated_or_cross_goal_receipt_cannot_mark_claiming_goal_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, bindings_path, queue, goal, transcript = _completion_case(tmp_path)
    other = upsert_goal(state, _goal("host:beta:0.0"))
    other_receipt = ingest_passing_receipt(state, other.session_ref)
    _append_completion_response(
        transcript,
        session_id="native-alpha",
        claim=_completion_claim_line(),
    )

    def forged_run(root: Path, session_ref: str, items: object) -> tuple[CompletionEvidence, ...]:
        del root, session_ref, items
        return (
            CompletionEvidence(
                done_when_item_id="done-1",
                receipt_name="tests-green",
                validator="pytest",
                validator_result="pass",
                citation=str(other_receipt),
            ),
        )

    monkeypatch.setattr(monitord_mod, "record_enrolled_validator_runs", forged_run)
    run_once(_live_config(state, bindings_path, queue))

    claiming_goal = get_goal(state, goal.session_ref)
    assert claiming_goal is not None
    assert claiming_goal.status != "done-pending-close"
    assert not claiming_goal.completion_proofs
    supervision = SupervisionLedger(state, goal.lane_id).latest()
    assert supervision is None or supervision.state != "completion_verified"

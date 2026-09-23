"""Persistence fixes: unmet-item binding, blocker records, deferral outside
claims, terminal-state surfacing, and findings for lanes the monitor cannot
observe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields, ingest_passing_receipt

import chitra.monitord as monitord_mod
from chitra.detect import (
    BlockerClaimStore,
    Finding,
    IncidentRecord,
    IncidentStore,
    LadderDecision,
    detect_blocker_claims,
    detect_deferral_language,
    detect_false_done,
    first_unmet_item_id,
    met_done_items,
)
from chitra.goals import (
    EnrolledDoneWhenItem,
    GoalRecord,
    due_goals,
    get_goal,
    upsert_goal,
)
from chitra.journal import ByteRange, CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.journal.store import EventJournal
from chitra.monitord import MonitordConfig, resolve_config, run_once
from chitra.orders import DispatchResult, DispatchStatus
from chitra.supervision import goal_digest
from chitra.supervisor import (
    MAX_CORRECTIVE_RETRY_ATTEMPTS,
    order_marker,
    reconcile_corrective_action,
    record_terminal_pursuit_alert,
)

LANE = "alpha"
SESSION = "host:alpha:0.0"
INSTANCE = "pytest-supervisor"
CLAUDE_VERSION = "2.1.229"


def _event(
    event_id: str,
    normalized_type: CanonicalType,
    *,
    lane: str = LANE,
    session_id: str = "native-1",
    text: str | None = None,
    transcript_path: str = "/t.jsonl",
    goal_ref: str | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        instance=INSTANCE,
        lane=lane,
        client=Client.CLAUDE,
        client_version=CLAUDE_VERSION,
        process_id=None,
        transcript=TranscriptIdentity(path=transcript_path, device=0, inode=0, generation=0),
        session_id=session_id,
        resume_id=None,
        observed_at="2026-08-23T12:00:00Z",
        native_time=None,
        native_type="assistant",
        native_join_id=None,
        raw_byte_range=ByteRange(start=0, end=1),
        raw_sha256=None,
        normalized_type=normalized_type,
        goal_ref=goal_ref,
        payload_digest="d" * 64,
        normalizer_version="n1",
        payload={"text": text} if text is not None else {},
        raw_record=None,
    )


def _tool_call(event_id: str) -> CanonicalEvent:
    return _event(event_id, CanonicalType.TOOL_CALL).model_copy(
        update={"payload": {"tool_name": "Bash", "input": {"command": "make check"}}}
    )


def _two_item_goal(session_ref: str = SESSION) -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Ship both of the enrolled outcomes for this lane.",
        done_when="first outcome verified; second outcome verified",
        source="task-file:test",
        status="working",
        intent="Exercise unmet-item binding.",
        scope="tests",
        enrolled_done_when_items=(
            EnrolledDoneWhenItem(
                id="item-a", text="first outcome verified", validator="pytest", required_receipt="receipt-a"
            ),
            EnrolledDoneWhenItem(
                id="item-b", text="second outcome verified", validator="pytest", required_receipt="receipt-b"
            ),
        ),
        interview_receipt=enrollment_fields("x")["interview_receipt"],  # type: ignore[arg-type]
    )


def _single_item_goal(session_ref: str = SESSION) -> GoalRecord:
    done_when = f"The enrolled check for {session_ref} passes."
    return GoalRecord(
        session_ref=session_ref,
        goal=f"Advance the exact goal for {session_ref}.",
        done_when=done_when,
        source="task-file:test",
        status="working",
        intent="Keep this enrolled lane moving toward its recorded outcome.",
        scope="The persistence-findings acceptance test.",
        **enrollment_fields(done_when),  # type: ignore[arg-type]
    )


def test_first_unmet_item_skips_receipt_verified_items(tmp_path: Path) -> None:
    """met_done_items is the receipt truth; the first unmet item is the bind."""
    goal = upsert_goal(tmp_path, _two_item_goal())
    ingest_passing_receipt(tmp_path, goal.session_ref, validator="pytest", receipt_name="receipt-a")

    met = met_done_items(goal.enrolled_done_when_items, receipt_root=tmp_path, session_ref=goal.session_ref)

    assert met == frozenset({"item-a"})
    assert first_unmet_item_id(goal.enrolled_done_when_items, met) == "item-b"


def test_first_unmet_item_falls_back_to_first_enrolled_without_receipts(tmp_path: Path) -> None:
    goal = upsert_goal(tmp_path, _two_item_goal())

    met = met_done_items(goal.enrolled_done_when_items, receipt_root=tmp_path, session_ref=goal.session_ref)

    assert met == frozenset()
    assert first_unmet_item_id(goal.enrolled_done_when_items, met) == "item-a"


def test_false_done_exit_finding_binds_the_unmet_item(tmp_path: Path) -> None:
    """A lane that exits before the contract binds the real unmet item, not item[0]."""
    goal = upsert_goal(tmp_path, _two_item_goal())
    ingest_passing_receipt(tmp_path, goal.session_ref, validator="pytest", receipt_name="receipt-a")

    findings = detect_false_done(
        final_response=None,
        enrolled_items=goal.enrolled_done_when_items,
        receipt_names_by_item={},
        receipt_roots={goal.session_ref: tmp_path},
        session_ref=goal.session_ref,
    )

    assert len(findings) == 1
    assert findings[0].unmet_item == "item-b"


def test_deferral_scan_covers_ordinary_assistant_text() -> None:
    """Deferral vocabulary outside a completion claim is a typed finding."""
    deferred = _event(
        "turn-defer", CanonicalType.FINAL_RESPONSE, text="I have left the parser rework as future work."
    )
    clean = _event(
        "turn-clean", CanonicalType.FINAL_RESPONSE, text="The parser rework is merged and verified."
    )

    findings = detect_deferral_language(
        (deferred, clean),
        enrolled_items=_two_item_goal().enrolled_done_when_items,
        met_items=frozenset({"item-a"}),
    )

    assert len(findings) == 1
    assert findings[0].detector == "deferral"
    assert findings[0].event_refs == ("turn-defer",)
    assert findings[0].unmet_item == "item-b"
    assert "future work" in findings[0].detail


def test_blocker_claim_records_then_repeats_and_rotates(tmp_path: Path) -> None:
    """A claimed block is parsed, durably recorded, and counted on one track."""
    enrolled = _two_item_goal().enrolled_done_when_items
    store = BlockerClaimStore(tmp_path, LANE)
    first_turn = _event(
        "turn-1", CanonicalType.FINAL_RESPONSE, text="Blocked on the upstream schema review."
    )
    findings, records = detect_blocker_claims((first_turn,), enrolled_items=enrolled, goal_digest="g" * 64)
    assert findings == []
    assert len(records) == 1
    assert records[0].unmet_item == "item-a"
    first_key = records[0].claim_key
    assert first_key
    assert store.append(records) == 1
    # The store is durable and idempotent across passes.
    assert store.append(records) == 0
    assert len(store.load()) == 1

    # The same excuse re-asserted after a tool call is evidence of work, so
    # it records silently rather than firing.
    repeat_turn = _event(
        "turn-2", CanonicalType.FINAL_RESPONSE, text="Still blocked on the upstream schema review."
    )
    findings, records = detect_blocker_claims(
        (first_turn, _tool_call("call-0"), repeat_turn),
        enrolled_items=enrolled,
        history=store.load(),
        goal_digest="g" * 64,
    )
    assert findings == []
    assert len(records) == 1
    assert records[0].claim_key == first_key
    store.append(records)

    # The same excuse a third time with no intervening tool call is a finding.
    same_again = _event(
        "turn-3", CanonicalType.FINAL_RESPONSE, text="Still blocked on the upstream schema review."
    )
    findings, records = detect_blocker_claims(
        (first_turn, _tool_call("call-0"), repeat_turn, same_again),
        enrolled_items=enrolled,
        history=store.load(),
        goal_digest="g" * 64,
    )
    assert [finding.detector for finding in findings] == ["false_blocker"]
    assert findings[0].unmet_item == "item-a"
    assert findings[0].event_refs == ("turn-2", "turn-3")
    store.append(records)

    # A rotated excuse while the item stays unmet is a changed-excuse finding
    # on the same pressure track.
    rotated = _event(
        "turn-4", CanonicalType.FINAL_RESPONSE, text="Now I cannot continue because the registry is down."
    )
    findings, _records = detect_blocker_claims(
        (first_turn, repeat_turn, same_again, rotated),
        enrolled_items=enrolled,
        history=store.load(),
        goal_digest="g" * 64,
    )
    assert [finding.detector for finding in findings] == ["changed_excuse"]
    assert findings[0].unmet_item == "item-a"


def _write_claude_transcript(path: Path, *, session_id: str, marker: str, final_text: str | None = None) -> None:
    """Write the smallest fixture that yields journal events without a client."""
    rows: list[dict[str, object]] = [
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
    if final_text is not None:
        rows.append(
            {
                "parentUuid": f"{marker}-assistant",
                "sessionId": session_id,
                "uuid": f"{marker}-final",
                "version": CLAUDE_VERSION,
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "id": f"msg-{marker}",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": final_text}],
                },
            }
        )
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


def _live_config(state: Path, bindings_path: Path, queue: Path) -> MonitordConfig:
    return resolve_config(
        state_dir=state,
        transcript_bindings_path=bindings_path,
        dispatch_queue_dir=queue,
        shadow_mode=False,
    )


def _findings(state: Path) -> list[dict[str, object]]:
    path = state / "monitord-findings.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_enrolled_but_invisible_lane_produces_a_finding_and_board_task(tmp_path: Path) -> None:
    """An enrolled lane with no transcript binding or journal is an explicit
    observation failure, not a lane the pass never visits."""
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    upsert_goal(state, _single_item_goal())
    _write_bindings(bindings_path, [])

    summary = run_once(_live_config(state, bindings_path, queue))

    assert summary["lanes_observed"] == 1
    records = _findings(state)
    assert [record["detector"] for record in records] == ["unobservable_lane"]
    assert records[0]["unmet_item"] == "done-1"
    stored = get_goal(state, SESSION)
    assert stored is not None
    assert any(
        task.kind == "investigate" and "cannot be observed" in task.text for task in stored.foreground_tasks
    )


def test_bound_but_unmatched_lane_produces_binding_unmatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Journal events that all fail the binding match are a finding, not a
    silent skip (e.g. a transcript consumed under an earlier session ref)."""
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    upsert_goal(state, _single_item_goal())
    transcript = tmp_path / "transcripts" / "alpha.jsonl"
    _write_claude_transcript(transcript, session_id="native-current", marker="current")
    _write_bindings(bindings_path, [_binding(session_ref=SESSION, lane=LANE, path=transcript)])
    # The transcript was already consumed under an older session ref, so the
    # ingestor appends nothing this pass and no journaled event can match.
    monkeypatch.setattr(monitord_mod, "ingest_transcript_bindings", lambda *_a, **_k: ())
    resolved = str(transcript.expanduser().resolve(strict=False))
    EventJournal(state, LANE).append(
        (
            _event("old-1", CanonicalType.TOOL_CALL, session_id="native-current",
                   transcript_path=resolved, goal_ref="host:older:0.0"),
            _event("old-2", CanonicalType.TOOL_CALL, session_id="native-current",
                   transcript_path=resolved, goal_ref="host:older:0.0"),
        )
    )

    summary = run_once(_live_config(state, bindings_path, queue))

    assert summary["lanes_observed"] == 1
    assert [record["detector"] for record in _findings(state)] == ["binding_unmatched"]


def test_unresolved_binding_finding_and_foreground_route(tmp_path: Path) -> None:
    """A binding whose session resolves to another lane's goal emits one
    unresolved_binding finding and routes a task onto the claimed goal."""
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    upsert_goal(state, _single_item_goal())
    transcript = tmp_path / "transcripts" / "other.jsonl"
    _write_claude_transcript(transcript, session_id="native-other", marker="other")
    # The manifest claims lane "beta" serves alpha's session; alpha's goal
    # says lane "alpha" — the binding must be reconciled by a person.
    _write_bindings(bindings_path, [_binding(session_ref=SESSION, lane="beta", path=transcript)])

    summary = run_once(_live_config(state, bindings_path, queue))

    assert summary["lanes_observed"] == 2
    detectors = [record["detector"] for record in _findings(state)]
    assert "unresolved_binding" in detectors
    # The enrolled lane itself is unobservable now: both failures surface.
    assert "unobservable_lane" in detectors
    stored = get_goal(state, SESSION)
    assert stored is not None
    assert any(
        task.kind == "investigate" and "transcript binding" in task.text for task in stored.foreground_tasks
    )


def test_unsafe_lane_names_do_not_crash_the_pass(tmp_path: Path) -> None:
    """A journal file whose stem cannot be a lane name is skipped, not fatal."""
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    _write_bindings(bindings_path, [])
    journal_dir = state / "journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "a b.jsonl").write_text("{}\n", encoding="utf-8")

    summary = run_once(_live_config(state, bindings_path, queue))

    assert summary["lanes_observed"] == 0


def test_relaunch_stage_surfaces_one_board_alert(tmp_path: Path) -> None:
    """An incident already at relaunch produces one deduplicated foreground
    investigation task per pass instead of silent permanence."""
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    bindings_path = tmp_path / "transcript-bindings.json"
    goal = upsert_goal(state, _single_item_goal())
    transcript = tmp_path / "transcripts" / "alpha.jsonl"
    _write_claude_transcript(
        transcript,
        session_id="native-alpha",
        marker="alpha",
        final_text="The parser rework is left as future work.",
    )
    _write_bindings(bindings_path, [_binding(session_ref=SESSION, lane=LANE, path=transcript)])
    IncidentStore(state, LANE)._append(
        IncidentRecord(
            lane=LANE,
            goal_digest=goal_digest(goal),
            fingerprint="seeded-relaunch-track",
            detector="deferral",
            stage="relaunch",
            order_marker="[M] monitord:seed",
            opened_at="2026-08-23T12:00:00Z",
            event_refs=("e0",),
            unmet_item="done-1",
            expected_next_progress="produce evidence for the unmet item",
            detail="seeded relaunch-stage incident",
        )
    )

    run_once(_live_config(state, bindings_path, queue))
    stored = get_goal(state, SESSION)
    assert stored is not None
    alerts = [
        task for task in stored.foreground_tasks
        if task.kind == "investigate" and "terminal state" in task.text
    ]
    assert len(alerts) == 1

    # A second pass must not multiply the alert while the goal is unchanged.
    run_once(_live_config(state, bindings_path, queue))
    stored = get_goal(state, SESSION)
    assert stored is not None
    assert [
        task for task in stored.foreground_tasks
        if task.kind == "investigate" and "terminal state" in task.text
    ] == alerts


def test_terminal_pursuit_alert_dedupes_by_content(tmp_path: Path) -> None:
    goal = upsert_goal(tmp_path, _single_item_goal())

    record_terminal_pursuit_alert(tmp_path, goal, detail="first terminal state")
    record_terminal_pursuit_alert(tmp_path, goal, detail="first terminal state")

    stored = get_goal(tmp_path, SESSION)
    assert stored is not None
    assert len(stored.foreground_tasks) == 1

    record_terminal_pursuit_alert(tmp_path, goal, detail="a distinct terminal state")

    stored = get_goal(tmp_path, SESSION)
    assert stored is not None
    assert len(stored.foreground_tasks) == 2


def _drive_transport_failures(tmp_path: Path) -> GoalRecord:
    goal = upsert_goal(tmp_path / "state", _single_item_goal(SESSION))
    finding = Finding(
        detector="drift",
        fingerprint_seed={"test": "retry-cap-persistence"},
        event_refs=("event-1",),
        unmet_item="done-1",
        expected_next_progress="return to the enrolled item",
        detail="the lane drifted",
    )
    decision = LadderDecision(
        action="open",
        stage="nudge",
        record=IncidentRecord(
            lane=LANE,
            goal_digest=goal_digest(goal),
            fingerprint=finding.fingerprint,
            detector=finding.detector,
            stage="nudge",
            order_marker=order_marker(finding),
            opened_at="2026-08-26T00:00:00+00:00",
            event_refs=finding.event_refs,
            unmet_item=finding.unmet_item,
            expected_next_progress=finding.expected_next_progress,
            detail=finding.detail,
        ),
        reason="test decision",
    )
    kwargs: dict[str, object] = {
        "state_root": tmp_path / "state",
        "queue_dir": tmp_path / "queue",
        "lane": LANE,
        "goal": goal,
        "finding": finding,
        "decision": decision,
        "shadow_mode": False,
        "retry_delay_seconds": 0,
    }
    current_id = reconcile_corrective_action(**kwargs).order_id  # type: ignore[arg-type]
    for _attempt in range(MAX_CORRECTIVE_RETRY_ATTEMPTS):
        result_path = tmp_path / "queue" / "results" / f"{current_id}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            DispatchResult(
                order_id=current_id,
                session_ref=SESSION,
                status=DispatchStatus.FAILED,
                reason="lane unavailable",
            ).model_dump_json(),
            encoding="utf-8",
        )
        failed = reconcile_corrective_action(**kwargs)  # type: ignore[arg-type]
        if "retry cap" in failed.reason:
            break
        resumed = reconcile_corrective_action(**kwargs)  # type: ignore[arg-type]
        current_id = resumed.order_id
    else:
        raise AssertionError("the retry cap was never reached")
    return goal


def test_retry_cap_hold_re_arms_on_schedule_and_alerts(tmp_path: Path) -> None:
    """Retry-cap exhaustion holds the lane with a resume time and emits one
    foreground alert, so the pursuit cannot die silently."""
    goal = _drive_transport_failures(tmp_path)

    held = get_goal(tmp_path / "state", goal.session_ref)
    assert held is not None
    assert held.status == "held"
    assert held.hold_reason.startswith("corrective-retry-exhausted")
    assert held.resume_at
    later = datetime.now(UTC) + timedelta(seconds=3700)
    assert any(record.session_ref == SESSION for record in due_goals(tmp_path / "state", now=later))
    assert any(
        task.kind == "investigate" and "terminal state" in task.text for task in held.foreground_tasks
    )


def test_non_transient_lifecycle_rejection_surfaces_one_alert(tmp_path: Path) -> None:
    """A lifecycle rejection that can never resolve itself is surfaced; a
    deferred lifecycle wait stays quiet."""
    goal = upsert_goal(tmp_path / "state", _single_item_goal(SESSION))
    finding = Finding(
        detector="drift",
        fingerprint_seed={"test": "lifecycle-alert"},
        event_refs=("event-1",),
        unmet_item="done-1",
        expected_next_progress="return to the enrolled item",
        detail="the lane drifted",
    )
    decision = LadderDecision(
        action="open",
        stage="nudge",
        record=IncidentRecord(
            lane=LANE,
            goal_digest=goal_digest(goal),
            fingerprint=finding.fingerprint,
            detector=finding.detector,
            stage="nudge",
            order_marker=order_marker(finding),
            opened_at="2026-08-26T00:00:00+00:00",
            event_refs=finding.event_refs,
            unmet_item=finding.unmet_item,
            expected_next_progress=finding.expected_next_progress,
            detail=finding.detail,
        ),
        reason="test decision",
    )
    kwargs: dict[str, object] = {
        "state_root": tmp_path / "state",
        "queue_dir": tmp_path / "queue",
        "lane": LANE,
        "goal": goal,
        "finding": finding,
        "decision": decision,
        "shadow_mode": False,
        "retry_delay_seconds": 0,
    }
    first = reconcile_corrective_action(**kwargs)  # type: ignore[arg-type]
    result_path = tmp_path / "queue" / "results" / f"{first.order_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)

    def write_result(reason: str) -> None:
        result_path.write_text(
            DispatchResult(
                order_id=first.order_id,
                session_ref=SESSION,
                status=DispatchStatus.BLOCKED,
                reason=reason,
            ).model_dump_json(),
            encoding="utf-8",
        )

    # A deferred lifecycle wait resolves itself; it stays silent.
    write_result("lane-lifecycle-paused-deferred")
    deferred = reconcile_corrective_action(**kwargs)  # type: ignore[arg-type]
    assert deferred.state == "blocked"
    stored = get_goal(tmp_path / "state", SESSION)
    assert stored is not None
    assert stored.foreground_tasks == ()

    # A non-transient lifecycle rejection surfaces one foreground alert.
    write_result("lane-lifecycle-closed")
    failed = reconcile_corrective_action(**kwargs)  # type: ignore[arg-type]
    assert failed.state == "blocked"
    stored = get_goal(tmp_path / "state", SESSION)
    assert stored is not None
    assert [
        task.kind for task in stored.foreground_tasks
        if "terminal state" in task.text
    ] == ["investigate"]

    # And it does not stack duplicates while nothing has changed.
    reconcile_corrective_action(**kwargs)  # type: ignore[arg-type]
    stored = get_goal(tmp_path / "state", SESSION)
    assert stored is not None
    assert len(stored.foreground_tasks) == 1

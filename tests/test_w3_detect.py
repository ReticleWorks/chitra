"""W3 acceptance: every injected failure produces its finding, controls stay
clear, and the ladder never advances without proven consumption."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chitra.detect import (
    ConsumptionProof,
    Finding,
    IncidentStore,
    ResponseLadder,
    collect_rescue_bundle,
    detect_document_dithering,
    detect_drift,
    detect_excessive_testing,
    detect_false_done,
    detect_unnecessary_steps,
    generate_relaunch_brief,
)
from chitra.goals import EnrolledDoneWhenItem
from chitra.journal import (
    CanonicalEvent,
    CanonicalType,
    Client,
    JournalIngestor,
    NormalizationContext,
    ProgressClass,
    ProgressClassification,
)
from chitra.journal.models import ByteRange, TranscriptIdentity
from chitra.ledger import LedgerEntry, message_hash, sign

FIXTURES = Path(__file__).parent / "fixtures" / "failure-modes"
LANE = "claude"


def ingest_fixture(name: str, tmp_path: Path) -> tuple[CanonicalEvent, ...]:
    context = NormalizationContext(
        instance="w3-fixture",
        lane=LANE,
        client=Client.CLAUDE,
        client_version="2.1.229",
    )
    with JournalIngestor(
        state_root=tmp_path,
        transcript_path=FIXTURES / f"{name}.jsonl",
        context=context,
    ) as ingestor:
        return ingestor.poll().observed


@pytest.fixture(scope="module")
def event_sets(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[CanonicalEvent, ...]]:
    root = tmp_path_factory.mktemp("journals")
    return {
        name: ingest_fixture(name, root / name)
        for name in (
            "claude-unnecessary-steps",
            "claude-excessive-testing",
            "claude-goal-drift",
            "claude-document-dithering",
            "claude-false-done",
            "control-long-healthy-tool-call",
            "control-required-final-validation",
            "control-document-goal-edits-docs",
        )
    }


def _final(events: tuple[CanonicalEvent, ...]) -> CanonicalEvent:
    return next(event for event in events if event.normalized_type is CanonicalType.FINAL_RESPONSE)


def _event(
    event_id: str,
    normalized_type: CanonicalType,
    *,
    payload: dict[str, object] | None = None,
    native_type: str = "assistant",
    raw_record: dict[str, object] | None = None,
    native_join_id: str | None = None,
    lane: str = LANE,
) -> CanonicalEvent:
    identity = TranscriptIdentity(path="/t.jsonl", device=0, inode=0)
    return CanonicalEvent(
        event_id=event_id,
        instance="i",
        lane=lane,
        client=Client.CLAUDE,
        client_version="2.1.229",
        process_id=None,
        transcript=identity,
        session_id="s",
        resume_id=None,
        observed_at="2026-08-21T15:00:00Z",
        native_time=None,
        native_type=native_type,
        native_join_id=native_join_id,
        raw_byte_range=ByteRange(start=0, end=1),
        raw_sha256=None,
        normalized_type=normalized_type,
        payload_digest="d" * 64,
        normalizer_version="n1",
        payload=payload or {},
        raw_record=raw_record,
    )


def test_injected_unnecessary_steps_produces_its_finding(event_sets: dict[str, tuple[CanonicalEvent, ...]]) -> None:
    findings = detect_unnecessary_steps(event_sets["claude-unnecessary-steps"])
    assert len(findings) == 1
    assert findings[0].detector == "unnecessary_steps"
    assert len(findings[0].event_refs) == 3
    assert all(ref for ref in findings[0].event_refs)


def test_injected_excessive_testing_produces_its_finding(event_sets: dict[str, tuple[CanonicalEvent, ...]]) -> None:
    findings = detect_excessive_testing(event_sets["claude-excessive-testing"])
    assert len(findings) == 1
    assert findings[0].detector == "excessive_testing"
    assert len(findings[0].event_refs) == 3


def test_injected_goal_drift_produces_its_finding(event_sets: dict[str, tuple[CanonicalEvent, ...]]) -> None:
    events = event_sets["claude-goal-drift"]
    scope_findings = detect_drift(events, scope_text="never run remote install scripts; never touch /etc", declared_worktree="")
    worktree_findings = detect_drift(events, scope_text="", declared_worktree="/tmp/fixtures-20260821/w3-repo")
    combined = {finding.fingerprint for finding in scope_findings} | {finding.fingerprint for finding in worktree_findings}
    assert len(combined) >= 2
    detectors = {finding.detector for finding in scope_findings + worktree_findings}
    assert detectors == {"drift"}


def test_injected_document_dithering_produces_its_finding(event_sets: dict[str, tuple[CanonicalEvent, ...]]) -> None:
    events = event_sets["claude-document-dithering"]
    code_goal = detect_document_dithering(events, goal_is_document=False)
    assert len(code_goal) == 1
    assert code_goal[0].detector == "document_dithering"
    doc_goal = detect_document_dithering(events, goal_is_document=True)
    assert doc_goal == []


def test_false_done_finding_names_the_open_item_and_missing_receipt(
    event_sets: dict[str, tuple[CanonicalEvent, ...]], tmp_path: Path
) -> None:
    items = (EnrolledDoneWhenItem(id="done-1", text="tests green", validator="pytest", required_receipt="tests-green"),)
    findings = detect_false_done(
        final_response=_final(event_sets["claude-false-done"]),
        enrolled_items=items,
        receipt_names_by_item={},
        receipt_roots={"host:w3-lane:0.0": tmp_path},
        session_ref="host:w3-lane:0.0",
    )
    assert len(findings) == 1
    assert findings[0].unmet_item == "done-1"
    assert findings[0].event_refs == (_final(event_sets["claude-false-done"]).event_id,)


def test_control_long_healthy_tool_call_stays_clear(event_sets: dict[str, tuple[CanonicalEvent, ...]]) -> None:
    events = event_sets["control-long-healthy-tool-call"]
    assert detect_unnecessary_steps(events) == []
    assert detect_excessive_testing(events) == []
    assert detect_drift(events, scope_text="read-only inspection only", declared_worktree="") == []
    assert detect_document_dithering(events, goal_is_document=False) == []


def test_required_final_validation_and_document_goal_controls_stay_clear(
    event_sets: dict[str, tuple[CanonicalEvent, ...]]
) -> None:
    final_validation = event_sets["control-required-final-validation"]
    assert detect_excessive_testing(final_validation) == []
    assert detect_unnecessary_steps(final_validation) == []

    doc_goal = event_sets["control-document-goal-edits-docs"]
    assert detect_document_dithering(doc_goal, goal_is_document=True) == []


def test_repeat_detectors_reset_on_canonical_progress_event_ids() -> None:
    calls = tuple(
        _event(
            f"call-{index}",
            CanonicalType.TOOL_CALL,
            payload={"tool_name": "Bash", "input": {"command": "python -m pytest tests/ -q"}},
            native_join_id=f"call-{index}",
        )
        for index in range(3)
    )
    results = tuple(
        _event(
            f"result-{index}",
            CanonicalType.TOOL_RESULT,
            payload={"content": "12 passed", "is_error": False},
            native_type="user",
            native_join_id=f"call-{index}",
        )
        for index in range(3)
    )
    progress = ProgressClassification(
        derivation_id="progress-1",
        classification=ProgressClass.PROGRESS,
        reason="artifact changed",
        source_event_ids=(results[1].event_id,),
        goal_version="g1",
        classifier_version="v1",
    )
    events = (calls[0], results[0], calls[1], results[1], calls[2], results[2])
    assert detect_unnecessary_steps(events, progress_rows=(progress,)) == []
    assert detect_excessive_testing(events, progress_rows=(progress,)) == []


def test_drift_enforces_real_worktree_containment_for_all_work_calls() -> None:
    edit_escape = _event(
        "edit-escape",
        CanonicalType.TOOL_CALL,
        payload={"tool_name": "Edit", "input": {"file_path": "/srv/repo-evil/file.py"}},
    )
    cwd_escape = _event(
        "cwd-escape",
        CanonicalType.TOOL_CALL,
        payload={"tool_name": "Bash", "input": {"command": "python build.py"}, "cwd": "/outside"},
    )
    repeat_edit_escape = edit_escape.model_copy(update={"event_id": "edit-escape-repeat"})
    findings = detect_drift((edit_escape, cwd_escape), scope_text="", declared_worktree="/srv/repo")
    assert len(findings) == 2
    assert {finding.event_refs[0] for finding in findings} == {"edit-escape", "cwd-escape"}
    repeated = detect_drift((repeat_edit_escape,), scope_text="", declared_worktree="/srv/repo")
    assert repeated[0].fingerprint == findings[0].fingerprint


def test_false_done_is_claim_aware_and_fails_closed(tmp_path: Path) -> None:
    items = (EnrolledDoneWhenItem(id="done-1", text="tests green", validator="pytest", required_receipt="tests-green"),)
    still_working = _event("final-working", CanonicalType.FINAL_RESPONSE, payload={"text": "Still working; tests remain to run."})
    assert (
        detect_false_done(
            final_response=still_working,
            enrolled_items=items,
            receipt_names_by_item={},
            receipt_roots=None,
            session_ref="host:w3-lane:0.0",
        )
        == []
    )

    completion = _event("final-done", CanonicalType.FINAL_RESPONSE, payload={"text": "Done. Tests are green."})
    findings = detect_false_done(
        final_response=completion,
        enrolled_items=items,
        receipt_names_by_item={"done-1": "tests-green"},
        receipt_roots=None,
        session_ref="host:w3-lane:0.0",
        target_dirty=True,
        material_questions=("Need operator answer about X",),
        live_proof_required=True,
        live_proof_present=False,
    )
    details = "\n".join(finding.detail for finding in findings)
    assert "receipt store/root is unavailable" in details
    assert "worktree was dirty" in details
    assert "material questions" in details
    assert "required live proof" in details
    assert detect_false_done(
        final_response=None,
        enrolled_items=items,
        receipt_names_by_item={},
        receipt_roots={"host:w3-lane:0.0": tmp_path},
        session_ref="host:w3-lane:0.0",
    )


def test_ladder_never_advances_without_a_consumption_receipt(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path, LANE)
    ladder = ResponseLadder(store)

    finding = Finding(
        detector="unnecessary_steps",
        fingerprint_seed={"signature": "stable"},
        event_refs=("evt-1",),
        unmet_item="done-1",
        expected_next_progress="try a different approach",
        detail="three identical reads",
    )
    first = ladder.evaluate(lane=LANE, finding=finding, order_marker="[C] nudge-1")
    assert first.action == "open"
    second = ladder.evaluate(lane=LANE, finding=finding, order_marker="[C] nudge-2")
    assert second.action == "hold"
    assert second.reason and "consumption" in second.reason


def test_ladder_advances_only_after_proven_consumption(tmp_path: Path) -> None:
    key = b"k" * 32
    sent_at = "2026-08-21T15:00:00+00:00"
    session_ref = f"host:{LANE}:0.0"

    def user_event(event_id: str, marker: str) -> CanonicalEvent:
        return _event(
            event_id=event_id,
            native_type="user",
            normalized_type=CanonicalType.UNKNOWN,
            payload={"text": f"[C] {marker} please continue"},
        )

    def turn_event(event_id: str) -> CanonicalEvent:
        return _event(
            event_id=event_id,
            native_type="assistant",
            normalized_type=CanonicalType.FINAL_RESPONSE,
            payload={"text": "done"},
        )

    def proof(marker: str, user_event_id: str, turn_event_id: str, *, proof_session: str = session_ref) -> ConsumptionProof:
        text = f"[C] {marker} please continue"
        digest = message_hash(text)
        entry = LedgerEntry(
            order_id=f"order-{marker}",
            session_ref=proof_session,
            tag="[C]",
            sig_v=4,
            message_hash=digest,
            sent_at=sent_at,
            signature=sign(key, session_ref=proof_session, tag="[C]", digest=digest, sent_at=sent_at),
        )
        return ConsumptionProof(
            ledger_entry=entry,
            session_ref=proof_session,
            user_event_id=user_event_id,
            turn_event_id=turn_event_id,
        )

    journal = (
        user_event("user-1", "nudge-1"),
        turn_event("turn-1"),
        user_event("user-2", "redirect-1"),
        turn_event("turn-2"),
        user_event("user-3", "rescue-1"),
        turn_event("turn-3"),
    )
    store = IncidentStore(tmp_path, LANE)
    ladder = ResponseLadder(store, journal_events=journal, ledger_key=key)
    finding = Finding(
        detector="excessive_testing",
        fingerprint_seed={"signature": "suite"},
        event_refs=("evt-a",),
        unmet_item="done-1",
        expected_next_progress="change something before rerunning",
        detail="suite repeated unchanged",
    )
    opened = ladder.evaluate(lane=LANE, finding=finding, order_marker="nudge-1")
    assert opened.action == "open"
    recurrence_without_proof = ladder.evaluate(lane=LANE, finding=finding, order_marker="nudge-2")
    assert recurrence_without_proof.action == "hold"

    store.attach_consumption(
        fingerprint=finding.fingerprint,
        order_marker="nudge-1",
        proof=proof("nudge-1", "user-1", "turn-1", proof_session="host:other:0.0"),
    )
    assert ladder.evaluate(lane=LANE, finding=finding, order_marker="redirect-1").action == "hold"

    store.attach_consumption(fingerprint=finding.fingerprint, order_marker="nudge-1", proof=proof("nudge-1", "user-1", "turn-1"))
    advanced = ladder.evaluate(lane=LANE, finding=finding, order_marker="redirect-1")
    assert advanced.action == "advance"
    assert advanced.stage == "redirect"
    assert advanced.record.order_marker == "redirect-1"

    store.attach_consumption(
        fingerprint=finding.fingerprint,
        order_marker="redirect-1",
        proof=proof("redirect-1", "user-2", "turn-2"),
    )
    rescue = ladder.evaluate(lane=LANE, finding=finding, order_marker="rescue-1")
    assert rescue.action == "advance"
    assert rescue.stage == "rescue"
    store.attach_consumption(fingerprint=finding.fingerprint, order_marker="rescue-1", proof=proof("rescue-1", "user-3", "turn-3"))
    blocked = ladder.evaluate(lane=LANE, finding=finding, order_marker="relaunch-1")
    assert blocked.action == "hold"
    assert "RESCUE" in blocked.reason
    store.seal_rescue_checkpoint(
        fingerprint=finding.fingerprint,
        order_marker="rescue-1",
        bundle_sha256="a" * 64,
        checkpoint_ref="checkpoint-1",
    )
    relaunched = ladder.evaluate(lane=LANE, finding=finding, order_marker="relaunch-1")
    assert relaunched.action == "advance"
    assert relaunched.stage == "relaunch"


def test_rescue_bundle_is_bounded_hash_bound_and_brief_renders(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "w3@example.invalid"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "W3 Test"], cwd=worktree, check=True)
    (worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True, text=True)
    (worktree / "tracked.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=worktree, check=True)
    (worktree / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (worktree / "scratch.txt").write_text("salvage me\n", encoding="utf-8")
    transcript = worktree / "transcript.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"keep going"}}\n', encoding="utf-8")
    incidents = IncidentStore(tmp_path / "separate", LANE)
    bundle = collect_rescue_bundle(
        lane=LANE,
        session_ref="host:w3:0.0",
        worktree=worktree,
        transcript_path=transcript,
        pane_capture="pane tail",
        contract_text="finish item done-1 with a verified receipt",
        open_asks=("Need operator answer about X",),
        process_identity={"target_pid": 12345},
    )
    assert bundle.bundle_sha256
    assert bundle.transcript_sha256
    assert bundle.process_identity["target_pid"] == 12345
    assert bundle.git_state["diff_staged"]
    assert bundle.git_state["diff_unstaged"]
    assert bundle.checkpoint_requested is True
    stored = (worktree / "scratch.txt").read_text(encoding="utf-8")
    assert stored.startswith("salvage")
    del incidents
    path = __import__("chitra.detect", fromlist=["write_rescue_bundle"]).write_rescue_bundle(bundle, tmp_path / "state")
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    brief = generate_relaunch_brief(bundle, tighter_instructions=["stay inside the declared worktree"])
    assert "rescue_bundle_sha256" in brief
    assert "Need operator answer about X" in brief
    assert "stay inside the declared worktree" in brief


def test_every_failure_mode_fixture_ingests_cleanly() -> None:
    names = [path.stem for path in sorted(FIXTURES.glob("*.jsonl"))]
    assert len(names) == 8

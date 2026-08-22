"""W3 acceptance: every injected failure produces its finding, controls stay
clear, and the ladder never advances without proven consumption."""

from __future__ import annotations

from pathlib import Path

import pytest

from chitra.detect import (
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
)

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
        )
    }


def _final(events: tuple[CanonicalEvent, ...]) -> CanonicalEvent:
    return next(event for event in events if event.normalized_type is CanonicalType.FINAL_RESPONSE)


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


def test_ladder_never_advances_without_a_consumption_receipt(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path, LANE)
    ladder = ResponseLadder(store)
    from chitra.detect import Finding

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
    from chitra.detect import ConsumptionProof, Finding
    from chitra.journal.models import ByteRange, TranscriptIdentity
    from chitra.ledger import LedgerEntry, message_hash, sign

    key = b"k" * 32
    nudge_text = "[C] nudge-1 please continue"
    sent_at = "2026-08-21T15:00:00+00:00"
    entry = LedgerEntry(
        order_id="order-1",
        session_ref=f"host:{LANE}:0.0",
        tag="[C]",
        sig_v=4,
        message_hash=message_hash(nudge_text),
        sent_at=sent_at,
        signature=sign(key, session_ref=f"host:{LANE}:0.0", tag="[C]", digest=message_hash(nudge_text), sent_at=sent_at),
    )

    def user_event(event_id: str, marker: str) -> CanonicalEvent:
        identity = TranscriptIdentity(path="/t.jsonl", device=0, inode=0)
        return CanonicalEvent(
            event_id=event_id,
            instance="i",
            lane=LANE,
            client=Client.CLAUDE,
            client_version="2.1.229",
            process_id=None,
            transcript=identity,
            session_id="s",
            resume_id=None,
            observed_at="2026-08-21T15:00:00Z",
            native_time=None,
            native_type="user",
            native_join_id=None,
            raw_byte_range=ByteRange(start=0, end=1),
            raw_sha256=None,
            normalized_type=CanonicalType.TOOL_RESULT,
            payload_digest="d" * 64,
            normalizer_version="n1",
            payload={"text": f"[C] {marker} please continue"},
            raw_record=None,
        )

    def turn_event(event_id: str) -> CanonicalEvent:
        identity = TranscriptIdentity(path="/t.jsonl", device=0, inode=0)
        return CanonicalEvent(
            event_id=event_id,
            instance="i",
            lane=LANE,
            client=Client.CLAUDE,
            client_version="2.1.229",
            process_id=None,
            transcript=identity,
            session_id="s",
            resume_id=None,
            observed_at="2026-08-21T15:05:00Z",
            native_time=None,
            native_type="assistant",
            native_join_id=None,
            raw_byte_range=ByteRange(start=2, end=3),
            raw_sha256=None,
            normalized_type=CanonicalType.FINAL_RESPONSE,
            payload_digest="e" * 64,
            normalizer_version="n1",
            payload={"text": "done"},
            raw_record=None,
        )

    journal = (user_event("user-1", "nudge-1"), turn_event("turn-1"), user_event("user-2", "nudge-2"), turn_event("turn-2"))
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

    proof = ConsumptionProof(ledger_entry=entry, user_event_id="user-1", turn_event_id="turn-1")
    store.attach_consumption(fingerprint=finding.fingerprint, order_marker="nudge-1", proof=proof)
    advanced = ladder.evaluate(lane=LANE, finding=finding, order_marker="nudge-2")
    assert advanced.action == "advance"
    assert advanced.stage == "redirect"


def test_rescue_bundle_is_bounded_hash_bound_and_brief_renders(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "scratch.txt").write_text("salvage me\n", encoding="utf-8")
    incidents = IncidentStore(tmp_path / "separate", LANE)
    bundle = collect_rescue_bundle(
        lane=LANE,
        session_ref="host:w3:0.0",
        worktree=worktree,
        transcript_path=worktree / "transcript.jsonl",
        pane_capture="pane tail",
        contract_text="finish item done-1 with a verified receipt",
    )
    assert bundle.bundle_sha256
    assert bundle.checkpoint_requested is True
    stored = (worktree / "scratch.txt").read_text(encoding="utf-8")
    assert stored.startswith("salvage")
    del incidents
    path = __import__("chitra.detect", fromlist=["write_rescue_bundle"]).write_rescue_bundle(bundle, tmp_path / "state")
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    brief = generate_relaunch_brief(bundle, tighter_instructions=["stay inside the declared worktree"])
    assert "rescue_bundle_sha256" in brief
    assert "stay inside the declared worktree" in brief


def test_every_failure_mode_fixture_ingests_cleanly() -> None:
    names = [path.stem for path in sorted(FIXTURES.glob("*.jsonl"))]
    assert len(names) == 6

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from chitra.journal import (
    CanonicalEvent,
    CanonicalType,
    Client,
    JournalIngestor,
    LifecycleReceipt,
    NormalizationContext,
    ProgressClass,
    UnsupportedClientVersion,
    classify_progress,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "w11"


@dataclass(frozen=True)
class FixtureCase:
    client: Client
    version: str
    filename: str
    line_count: int
    event_counts: dict[str, int]
    event_digest: str
    session_id: str
    resume_boundary: int

    @property
    def path(self) -> Path:
        return FIXTURE_DIR / self.filename


CASES = (
    FixtureCase(
        client=Client.CLAUDE,
        version="2.1.229",
        filename="claude-2.1.229-7d41b635-8c69-4e8d-ae27-c5d61d5f3a11.jsonl",
        line_count=111,
        event_counts={
            "final_response": 4,
            "tool_call": 11,
            "tool_error": 1,
            "tool_result": 10,
            "unknown": 85,
        },
        event_digest="5b25d16a56de54581b2e91fb802f96bf4f1969072cdecb8fdebfacc4e23023cf",
        session_id="7d41b635-8c69-4e8d-ae27-c5d61d5f3a11",
        resume_boundary=94,
    ),
    FixtureCase(
        client=Client.CODEX,
        version="0.149.0",
        filename="codex-0.149.0-01a024cd-3459-7ae2-b7a1-7dd78d2b68bd.jsonl",
        line_count=62,
        event_counts={
            "compaction": 1,
            "final_response": 3,
            "tool_call": 3,
            "tool_error": 1,
            "tool_result": 2,
            "unknown": 52,
        },
        event_digest="4b578ff2e9c66ea76dca2646698c340b39486f255eb4b8663fd52fc24da2f6cd",
        session_id="01a024cd-3459-7ae2-b7a1-7dd78d2b68bd",
        resume_boundary=48,
    ),
)


def context(case: FixtureCase, *, lane: str | None = None) -> NormalizationContext:
    return NormalizationContext(
        instance="w11-fixture",
        lane=lane or case.client.value,
        client=case.client,
        client_version=case.version,
        observed_at="2026-08-21T00:00:00Z",
    )


def ingest(case: FixtureCase, state_root: Path, *, path: Path | None = None, chunk_size: int = 64 * 1024) -> tuple[CanonicalEvent, ...]:
    with JournalIngestor(
        state_root=state_root,
        transcript_path=path or case.path,
        context=context(case),
        chunk_size=chunk_size,
    ) as ingestor:
        return ingestor.poll().observed


def fixture_projection(events: tuple[CanonicalEvent, ...]) -> list[dict[str, object]]:
    return [
        {
            "id": event.event_id,
            "type": event.normalized_type.value,
            "join": event.native_join_id,
            "start": event.raw_byte_range.start if event.raw_byte_range else None,
            "end": event.raw_byte_range.end if event.raw_byte_range else None,
            "payload": event.payload_digest,
        }
        for event in events
    ]


def semantic_projection(events: tuple[CanonicalEvent, ...]) -> list[tuple[str, str, str | None, str]]:
    return [(event.event_id, event.normalized_type.value, event.native_join_id, event.payload_digest) for event in events]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.client.value)
def test_w11_fixture_yields_exact_duplicate_free_events(tmp_path: Path, case: FixtureCase) -> None:
    events = ingest(case, tmp_path)

    assert len(events) == case.line_count
    assert len({event.event_id for event in events}) == case.line_count
    assert Counter(event.normalized_type.value for event in events) == case.event_counts
    encoded = json.dumps(fixture_projection(events), sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == case.event_digest

    calls = {event.native_join_id for event in events if event.normalized_type is CanonicalType.TOOL_CALL}
    results = {event.native_join_id for event in events if event.normalized_type in {CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR}}
    assert calls == results
    assert all(event.session_id == case.session_id for event in events)
    assert all(event.raw_byte_range and event.raw_byte_range.end > event.raw_byte_range.start for event in events)

    journal_path = tmp_path / "journal" / f"{case.client.value}.jsonl"
    assert (journal_path.stat().st_mode & 0o777) == 0o600
    first_row = json.loads(journal_path.read_text(encoding="utf-8").splitlines()[0])
    assert first_row["schema"] == "chitra.journal.event.v1"
    assert "schema_name" not in first_row


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.client.value)
def test_one_byte_chunks_equal_the_fixture_baseline(tmp_path: Path, case: FixtureCase) -> None:
    baseline = ingest(case, tmp_path / "baseline")
    byte_split = ingest(case, tmp_path / "byte-split", chunk_size=1)
    assert fixture_projection(byte_split) == fixture_projection(baseline)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.client.value)
def test_partial_record_is_deferred_until_newline(tmp_path: Path, case: FixtureCase) -> None:
    data = case.path.read_bytes()
    transcript = tmp_path / case.filename
    transcript.write_bytes(data[:-5])
    with JournalIngestor(
        state_root=tmp_path / "state",
        transcript_path=transcript,
        context=context(case),
        chunk_size=7,
    ) as ingestor:
        first = ingestor.poll()
        assert len(first.observed) == case.line_count - 1
        with transcript.open("ab") as output:
            output.write(data[-5:])
        second = ingestor.poll()
        assert len(second.observed) == 1
        assert not second.rotations
    assert semantic_projection(first.observed + second.observed) == semantic_projection(ingest(case, tmp_path / "baseline"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.client.value)
def test_rotation_replay_matches_without_duplicates(tmp_path: Path, case: FixtureCase) -> None:
    baseline = ingest(case, tmp_path / "baseline")
    lines = case.path.read_bytes().splitlines(keepends=True)
    boundary = len(lines) // 2
    transcript = tmp_path / "rotating.jsonl"
    transcript.write_bytes(b"".join(lines[:boundary]))

    with JournalIngestor(
        state_root=tmp_path / "state",
        transcript_path=transcript,
        context=context(case),
    ) as ingestor:
        first = ingestor.poll()
        transcript.rename(tmp_path / "rotating.jsonl.1")
        transcript.write_bytes(b"".join(lines[boundary:]))
        second = ingestor.poll()
        assert len(second.rotations) == 1
        assert second.rotations[0].previous.inode != second.rotations[0].current.inode

    replayed = first.observed + second.observed
    assert semantic_projection(replayed) == semantic_projection(baseline)
    assert len({event.event_id for event in replayed}) == len(replayed)
    assert len((tmp_path / "state" / "journal" / f"{case.client.value}.jsonl").read_text().splitlines()) == len(replayed)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.client.value)
def test_same_inode_resume_uses_external_receipt(tmp_path: Path, case: FixtureCase) -> None:
    baseline = ingest(case, tmp_path / "baseline")
    lines = case.path.read_bytes().splitlines(keepends=True)
    transcript = tmp_path / "resumed.jsonl"
    transcript.write_bytes(b"".join(lines[: case.resume_boundary]))

    with JournalIngestor(
        state_root=tmp_path / "state",
        transcript_path=transcript,
        context=context(case),
    ) as ingestor:
        first = ingestor.poll()
        first_identity = ingestor.reader.identity
        receipt = LifecycleReceipt(
            receipt_id=f"resume-{case.client.value}-1",
            event_type="resume",
            occurred_at="2026-08-21T15:00:00Z",
            session_id=case.session_id,
            resume_id=f"{case.client.value}-process-2",
            method="launcher_process_receipt",
            evidence={"same_inode": True},
        )
        resume_event = ingestor.record_resume(receipt)
        with transcript.open("ab") as output:
            output.write(b"".join(lines[case.resume_boundary :]))
        second = ingestor.poll()
        assert ingestor.reader.identity == first_identity
        assert not second.rotations

    assert resume_event.normalized_type is CanonicalType.RESUME
    assert all(event.resume_id == receipt.resume_id for event in second.observed)
    assert semantic_projection(first.observed + second.observed) == semantic_projection(baseline)
    stored = (tmp_path / "state" / "journal" / f"{case.client.value}.jsonl").read_text().splitlines()
    assert len(stored) == case.line_count + 1


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.client.value)
@pytest.mark.parametrize("chunk_size", (1, 7, 65_536))
def test_property_reingest_is_idempotent(tmp_path: Path, case: FixtureCase, chunk_size: int) -> None:
    state_root = tmp_path / "state"
    with JournalIngestor(
        state_root=state_root,
        transcript_path=case.path,
        context=context(case),
        chunk_size=chunk_size,
    ) as first_ingestor:
        first = first_ingestor.poll()
    with JournalIngestor(
        state_root=state_root,
        transcript_path=case.path,
        context=context(case),
        chunk_size=chunk_size,
    ) as second_ingestor:
        second = second_ingestor.poll()

    assert len(first.appended) == case.line_count
    assert semantic_projection(second.observed) == semantic_projection(first.observed)
    assert second.appended == ()
    assert len(second_ingestor.journal.load()) == case.line_count


def test_version_gate_fails_closed(tmp_path: Path) -> None:
    case = CASES[0]
    with pytest.raises(UnsupportedClientVersion, match="fixture-gated versions"):
        JournalIngestor(
            state_root=tmp_path,
            transcript_path=case.path,
            context=NormalizationContext(
                instance="test",
                lane="unsupported",
                client=case.client,
                client_version="2.1.230",
            ),
        )


def test_progress_classification_stays_evidence_bound(tmp_path: Path) -> None:
    event = next(event for event in ingest(CASES[0], tmp_path) if event.normalized_type is CanonicalType.TOOL_RESULT)
    unknown = classify_progress(event, goal_version="goal-v1")
    assert unknown.classification is ProgressClass.UNKNOWN

    changed = event.model_copy(update={"payload": {**event.payload, "progress_evidence": {"artifact_changed": True}}})
    progress = classify_progress(changed, goal_version="goal-v1", related_events=(event,))
    assert progress.classification is ProgressClass.PROGRESS
    assert progress.source_event_ids == (event.event_id,)

    tool_call = next(
        candidate for candidate in ingest(CASES[1], tmp_path / "codex") if candidate.normalized_type is CanonicalType.TOOL_CALL
    )
    non_progress = classify_progress(tool_call, goal_version="goal-v1")
    assert non_progress.classification is ProgressClass.NON_PROGRESS

    assert os.stat(tmp_path / "journal" / "claude.jsonl").st_size > 0

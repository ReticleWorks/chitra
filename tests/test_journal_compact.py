"""Journal compaction and replay-deduped lane loading."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from chitra.detect import Finding
from chitra.journal import (
    Client,
    EventJournal,
    JournalIngestor,
    NormalizationContext,
    derive_progress_rows,
)
from chitra.journal.compact import (
    JournalCompactionLockedError,
    compact_journal,
    main,
)
from chitra.monitord import load_lane_events, resolve_config, run_detectors

FIXTURE = Path(__file__).parent / "fixtures" / "w11" / "claude-2.1.229-synthetic.jsonl"
CODEX_FIXTURE = Path(__file__).parent / "fixtures" / "w11" / "codex-0.149.0-synthetic.jsonl"
LANE = "alpha"


def _context(client: Client = Client.CLAUDE, client_version: str = "2.1.229") -> NormalizationContext:
    return NormalizationContext(
        instance="compact-test",
        lane=LANE,
        client=client,
        client_version=client_version,
    )


def _duplicated_journal(
    tmp_path: Path,
    fixture: Path = FIXTURE,
    client: Client = Client.CLAUDE,
    client_version: str = "2.1.229",
) -> tuple[EventJournal, int, int]:
    """Write a journal carrying three replay generations of one transcript.

    Pre-dedupe ingestion minted a fresh event ID per replay, so the copies
    share every dedupe field but the ID itself.
    """
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(fixture.read_bytes())
    with JournalIngestor(
        state_root=tmp_path / "ingest-state",
        transcript_path=transcript,
        context=_context(client, client_version),
    ) as ingestor:
        events = ingestor.poll().observed
    journal = EventJournal(tmp_path / "state", LANE)
    # Rows are written directly because EventJournal.append now deduplicates
    # on the same content key the pre-dedupe daemon let replayed rows hoard.
    journal.directory.mkdir(parents=True, exist_ok=True)
    journal.path.write_text(
        "".join(
            event.model_copy(
                update={"event_id": event.event_id if generation == 0 else f"{event.event_id}-g{generation}"}
            ).model_dump_json()
            + "\n"
            for generation in range(3)
            for event in events
        ),
        encoding="utf-8",
    )
    return journal, len(events), len(events) * 3


def test_compact_journal_removes_replay_duplicates_and_is_idempotent(tmp_path: Path) -> None:
    journal, unique, total = _duplicated_journal(tmp_path)
    assert len(journal.path.read_text(encoding="utf-8").splitlines()) == total

    result = compact_journal(journal.path)

    assert result["before"] == total
    assert result["after"] == unique
    assert result["removed"] == total - unique
    assert len(journal.load()) == unique
    second = compact_journal(journal.path)
    assert second["before"] == unique
    assert second["after"] == unique
    assert second["removed"] == 0


def test_compact_journal_dry_run_reports_without_writing(tmp_path: Path) -> None:
    journal, unique, total = _duplicated_journal(tmp_path)

    result = compact_journal(journal.path, dry_run=True)

    assert result["before"] == total
    assert result["after"] == unique
    assert result["dry_run"] is True
    assert len(journal.path.read_text(encoding="utf-8").splitlines()) == total


def test_compact_journal_refuses_a_held_lane_lock(tmp_path: Path) -> None:
    journal, _unique, total = _duplicated_journal(tmp_path)

    with FileLock(str(journal.lock_path)), pytest.raises(JournalCompactionLockedError):
        compact_journal(journal.path)

    assert len(journal.path.read_text(encoding="utf-8").splitlines()) == total


def test_compact_cli_state_dir_and_journal_modes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    journal, unique, _total = _duplicated_journal(tmp_path)

    assert main(["--state-dir", str(tmp_path / "state"), "--lane", LANE]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["after"] == unique
    assert len(journal.load()) == unique

    assert main(["--journal", str(journal.path), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["removed"] == 0


def test_compact_cli_refuses_held_lock_with_exit_code(tmp_path: Path) -> None:
    journal, _unique, _total = _duplicated_journal(tmp_path)

    with FileLock(str(journal.lock_path)):
        assert main(["--journal", str(journal.path)]) == 2


def test_load_lane_events_drops_replay_duplicate_rows(tmp_path: Path) -> None:
    journal, unique, total = _duplicated_journal(tmp_path)
    config = resolve_config(state_dir=tmp_path / "state")

    events = load_lane_events(config, LANE)

    assert len(journal.load()) == total
    assert len(events) == unique
    assert len({event.event_id for event in events}) == unique


def test_detector_results_match_before_and_after_replay_dedup(tmp_path: Path) -> None:
    """The monitor pass's detector path sees equivalent input pre- and post-dedupe.

    ``journal.load()`` over the corrupted file is the pre-fix view (every
    replayed row); ``load_lane_events`` is the current deduplicated view. Both
    go through ``derive_progress_rows`` and ``run_detectors`` exactly as the
    pass calls them.

    Progress rows are identical for every retained event: dedupe only removes
    the replay-copy rows and never reclassifies a kept row.

    Findings differ in one documented way. Replay copies inflate the repeat
    counters behind the threshold detectors, so the corrupted stream refires
    each real finding once per generation and can manufacture findings the
    real stream never earned; every such extra finding cites at least one
    replay-copy event id that does not exist in the real stream. Every
    finding the deduplicated stream produces appears verbatim in the
    duplicated run. That inflation is the defect this PR removes.
    """
    journal, unique, total = _duplicated_journal(
        tmp_path, fixture=CODEX_FIXTURE, client=Client.CODEX, client_version="0.149.0"
    )
    config = resolve_config(state_dir=tmp_path / "state")

    pre_fix_events = tuple(journal.load())
    post_fix_events = load_lane_events(config, LANE)
    assert len(pre_fix_events) == total
    assert len(post_fix_events) == unique
    real_ids = {event.event_id for event in post_fix_events}

    rows_dup = derive_progress_rows(pre_fix_events, goal_version="1")
    rows_dedup = derive_progress_rows(post_fix_events, goal_version="1")
    assert len(rows_dup) == total
    assert tuple(row for row in rows_dup if row.source_event_ids[0] in real_ids) == rows_dedup

    goal = SimpleNamespace(
        scope="",
        intent="",
        goal="finish the enrolled implementation",
        session_ref="detector-equivalence",
        enrolled_done_when_items=(),
    )
    # Separate state dirs keep each run's blocker-claim history independent.
    findings_dup = run_detectors(
        resolve_config(state_dir=tmp_path / "detectors-dup"),
        LANE,
        goal,
        pre_fix_events,
        progress_rows=rows_dup,
    )
    findings_dedup = run_detectors(
        resolve_config(state_dir=tmp_path / "detectors-dedup"),
        LANE,
        goal,
        post_fix_events,
        progress_rows=rows_dedup,
    )

    def identity(finding: Finding) -> tuple[object, ...]:
        return (finding.detector, finding.fingerprint, finding.event_refs, finding.unmet_item, finding.detail)

    # The lane must produce a real finding for the comparison to mean anything.
    assert findings_dedup
    real = {identity(finding) for finding in findings_dedup}
    # Every real finding is reproduced verbatim in the duplicated run.
    assert real <= {identity(finding) for finding in findings_dup}
    # Every extra finding exists only because replay copies inflated a count:
    # each one cites at least one replay-copy id absent from the real stream.
    extras = [finding for finding in findings_dup if identity(finding) not in real]
    assert extras
    assert all(any(ref not in real_ids for ref in finding.event_refs) for finding in extras)


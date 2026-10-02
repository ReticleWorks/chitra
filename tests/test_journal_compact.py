"""Journal compaction and replay-deduped lane loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from filelock import FileLock

from chitra.journal import (
    Client,
    EventJournal,
    JournalIngestor,
    NormalizationContext,
)
from chitra.journal.compact import (
    JournalCompactionLockedError,
    compact_journal,
    main,
)
from chitra.monitord import load_lane_events, resolve_config

FIXTURE = Path(__file__).parent / "fixtures" / "w11" / "claude-2.1.229-synthetic.jsonl"
LANE = "alpha"


def _context() -> NormalizationContext:
    return NormalizationContext(
        instance="compact-test",
        lane=LANE,
        client=Client.CLAUDE,
        client_version="2.1.229",
    )


def _duplicated_journal(tmp_path: Path) -> tuple[EventJournal, int, int]:
    """Write a journal carrying three replay generations of one transcript.

    Pre-dedupe ingestion minted a fresh event ID per replay, so the copies
    share every dedupe field but the ID itself.
    """
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(FIXTURE.read_bytes())
    with JournalIngestor(
        state_root=tmp_path / "ingest-state",
        transcript_path=transcript,
        context=_context(),
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

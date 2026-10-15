"""Monitor contracts at the journal/pass and process containment boundaries."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_monitord import _event, _goal, _write_registry

import chitra.monitord as monitor
from chitra._fsio import exclusive_lock
from chitra.goals import upsert_goal
from chitra.journal import CanonicalType, Client
from chitra.journal.compact import compact_journal
from chitra.journal.store import EventJournal
from chitra.transcript_bindings import TranscriptBinding


def test_monitor_incremental_context_keeps_completion_and_original_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "healthy"
    config = monitor.resolve_config(state_dir=tmp_path)
    _write_registry(tmp_path, monkeypatch)
    upsert_goal(tmp_path, _goal(f"host:{lane}:0.0"))
    checked_claims: list[str] = []
    original_check = monitor.check_enrollment_and_receipts

    def check(*args: Any, **kwargs: Any) -> Any:
        checked_claims.extend(event.event_id for event in kwargs["final_responses"])
        return original_check(*args, **kwargs)

    monkeypatch.setattr(monitor, "check_enrollment_and_receipts", check)
    journal = EventJournal(tmp_path, lane)
    claim = _event("early-claim", CanonicalType.FINAL_RESPONSE, lane=lane).model_copy(update={"payload": {"text": "All done."}})
    call = _event("background", CanonicalType.TOOL_CALL, lane=lane).model_copy(
        update={"native_join_id": "job", "payload": {"tool_name": "Bash", "input": {"run_in_background": True}}}
    )
    implementations = tuple(
        _event(f"code-{session}", CanonicalType.TOOL_CALL, lane=lane).model_copy(
            update={"session_id": session, "payload": {"tool_name": "Edit", "input": {"file_path": "src/main.py"}}}
        )
        for session in ("session-1", "session-2")
    )
    journal.append((claim, call, *implementations))
    with journal.path.open("a") as handle:
        for index in range(5000):
            handle.write(_event(f"event-{index}", CanonicalType.UNKNOWN, lane=lane).model_dump_json() + "\n")
    sibling_lane = "sibling"
    sibling_journal = EventJournal(tmp_path, sibling_lane)
    sibling_journal.append((_event("sibling-event", CanonicalType.UNKNOWN, lane=sibling_lane),))
    while True:
        summary = monitor.run_once(config)
        assert not summary["lane_errors"]
        cache = monitor._LANE_JOURNALS[(str(tmp_path), lane)]
        sibling_cache = monitor._LANE_JOURNALS[(str(tmp_path), sibling_lane)]
        for stored_cache in (cache, sibling_cache):
            assert not stored_cache.metadata_loaded
            assert not stored_cache.sizes and not stored_cache.ordinals
            assert not stored_cache.protected and not stored_cache.pending
            assert not stored_cache.implementation_keys
            metadata_row = stored_cache.store.execute(
                "SELECT payload FROM lane_metadata WHERE lane_key=?", (stored_cache.store_key,)
            ).fetchone()
            assert metadata_row is not None
            metadata = json.loads(metadata_row[0])
            assert len(metadata["sizes"]) <= monitor._CACHE_EVENTS
            assert stored_cache.store.execute(
                "SELECT COUNT(*) FROM event_payloads WHERE lane_key=?", (stored_cache.store_key,)
            ).fetchone()[0] == len(metadata["sizes"])
        cache.restore_metadata()
        assert len(cache.sizes) <= monitor._CACHE_EVENTS
        assert cache.bytes <= monitor._CACHE_BYTES
        assert monitor.event_dedupe_key(claim) in cache.protected
        assert cache.pending["job"] == monitor.event_dedupe_key(call)
        assert len(cache.implementation_keys) == len(implementations)
        cache.spill_metadata()
        sibling_cache.restore_metadata()
        assert len(sibling_cache.sizes) == 1
        sibling_cache.spill_metadata()
        assert cache.progress_key is None
        assert cache.progress_rows == ()
        assert cache.detector_key is None
        assert cache.findings == []
        if summary["backlog_lanes"]:
            assert not checked_claims
        if not summary["backlog_lanes"]:
            break
    other_state = tmp_path / "other-state"
    other_lane = "also-healthy"
    EventJournal(other_state, other_lane).append((_event("other-lane-event", CanonicalType.UNKNOWN, lane=other_lane),))
    other_config = monitor.resolve_config(state_dir=other_state)
    assert [event.event_id for event in monitor.load_lane_events(other_config, other_lane)] == ["other-lane-event"]
    monitor._discard_lane_journal((str(other_state), other_lane))
    events = monitor.load_lane_events(config, lane)
    assert cache.evicted > 0
    assert claim.event_id in {event.event_id for event in events}
    assert {event.event_id for event in implementations} <= {event.event_id for event in events}
    assert claim.event_id in checked_claims
    assert monitor._lane_work_in_flight(config, "session-1", events) == ("background tool call job (Bash) is still running",)
    assert len(journal.load()) == 5004
    limited = [
        json.loads(line)
        for line in config.findings_path.read_text().splitlines()
        if json.loads(line)["detector"] == "detector-context-limited"
    ]
    assert limited and "UNKNOWN" in limited[-1]["detail"]

    detector_calls = 0
    original_detector = monitor.detect_unnecessary_steps

    def counted_detector(*args: Any, **kwargs: Any) -> Any:
        nonlocal detector_calls
        detector_calls += 1
        return original_detector(*args, **kwargs)

    progress_before = journal.progress_path.read_bytes()
    monkeypatch.setattr(monitor, "detect_unnecessary_steps", counted_detector)
    assert not monitor.run_once(config)["lane_errors"]
    assert detector_calls > 0
    assert journal.progress_path.read_bytes() == progress_before
    assert cache.progress_rows == ()
    assert cache.findings == []
    # Dedupe stays correct across fresh instances and a compaction inode swap.
    assert EventJournal(tmp_path, lane).append((claim,)) == ()
    with journal.path.open("ab") as handle:
        handle.write((claim.model_dump_json() + "\n").encode())
    original = journal.path.read_bytes()
    result = compact_journal(journal.path)
    assert result["removed"] == 1
    assert Path(result["original"]).read_bytes() == original
    assert EventJournal(tmp_path, lane).append((claim,)) == ()
    assert len(journal.load()) == 5004


def test_monitor_cli_raises_its_descriptor_limit_before_startup(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "chitra.monitord",
            "--once",
            "--state-dir",
            str(tmp_path / "state"),
            "--transcript-root",
            str(tmp_path / "transcripts"),
            "--lanes-file",
            str(tmp_path / "empty-lanes.yaml"),
            "--agent-manifest-dir",
            str(tmp_path / "empty-manifests"),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(monitor.__file__).parents[1])},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "monitord_descriptor_capacity_ready" in result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["journal", "ingest", "resolve", "protected", "locked", "metadata", "spill", "cpu", "rss"])
def test_monitor_contains_failure_without_silent_success(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if failure in {"cpu", "rss"}:
        target = tmp_path / "long_pass.py"
        target.write_text(
            "import signal\n"
            "from chitra.monitor_limits import monitor_limits\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "with monitor_limits(30, 64, 0.5):\n"
            + ("    while True: pass\n" if failure == "cpu" else "    blocks = []\n    while True: blocks.append(bytearray(1024 * 1024))\n")
        )
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(target)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(monitor.__file__).parents[1])},
            timeout=8,
        )
        assert result.returncode == -9
        assert time.monotonic() - start < 6
        assert "monitord_self_limit_exceeded" in result.stderr
        assert ("CPU" if failure == "cpu" else "RSS") in result.stderr
        return

    config = monitor.resolve_config(state_dir=tmp_path)
    healthy = EventJournal(tmp_path, "healthy")
    healthy.append((_event("good", CanonicalType.UNKNOWN, lane="healthy"),))
    broken = EventJournal(tmp_path, "broken")
    if failure == "journal":
        broken.path.write_text('{"invalid":\n')
    elif failure == "protected":
        monkeypatch.setattr(monitor, "_CACHE_EVENTS", 2)
        broken.append(tuple(_event(f"claim-{i}", CanonicalType.FINAL_RESPONSE, lane="broken") for i in range(3)))
    elif failure == "locked":
        broken.append((_event("locked", CanonicalType.UNKNOWN, lane="broken"),))
    elif failure == "spill":
        broken.append((_event("spill", CanonicalType.UNKNOWN, lane="broken"),))
        original_spill = monitor._LaneJournalCache.spill_metadata

        def fail_broken_spill(cache: Any) -> None:
            if cache.store_key == json.dumps((str(tmp_path), "broken")):
                raise sqlite3.OperationalError("temporary metadata unavailable")
            original_spill(cache)

        monkeypatch.setattr(monitor._LaneJournalCache, "spill_metadata", fail_broken_spill)
    elif failure == "metadata":
        broken.append((_event("metadata", CanonicalType.UNKNOWN, lane="broken"),))
        assert not monitor.run_once(config)["lane_errors"]
        cache = monitor._LANE_JOURNALS[(str(tmp_path), "broken")]
        assert not cache.metadata_loaded
        cache.store.execute("DELETE FROM lane_metadata WHERE lane_key=?", (cache.store_key,))
        cache.store.commit()
    else:
        transcript = tmp_path / "denied.jsonl"
        transcript.mkdir()  # Opening a directory must isolate this binding's OSError.
        binding = TranscriptBinding(
            session_ref="session-1",
            lane="broken",
            path=str(transcript),
            client=Client.CLAUDE,
            client_version="2.1.229",
            instance="i",
        )
        monkeypatch.setattr(monitor, "load_transcript_bindings", lambda *_args, **_kwargs: (binding,))
        if failure == "resolve":
            def denied(*_args: object, **_kwargs: object) -> Path:
                raise OSError("resolution unavailable")

            monkeypatch.setattr(monitor, "_resolved_binding_ref", denied)
    with exclusive_lock(broken.lock_path) if failure == "locked" else contextlib.nullcontext():
        summary = monitor.run_once(config)
    assert "broken" in summary["lane_errors"]
    expected_results = ["broken", "healthy"] if failure == "spill" else ["healthy"]
    assert [result["lane"] for result in summary["results"]] == expected_results
    if failure != "spill":
        records = [json.loads(line) for line in config.findings_path.read_text().splitlines()]
        assert any(row["lane"] == "broken" and row["detector"] == "monitor-internal-error" for row in records)
    if failure == "protected":
        cache = monitor._LANE_JOURNALS[(str(tmp_path), "broken")]
        assert not cache.metadata_loaded
        cache.restore_metadata()
        assert len(cache.sizes) <= 2
        assert len(broken.load()) == 3
        cache.spill_metadata()
        assert not cache.metadata_loaded
    if failure == "spill":
        cache = monitor._LANE_JOURNALS[(str(tmp_path), "broken")]
        assert cache.metadata_loaded
        assert cache.sizes

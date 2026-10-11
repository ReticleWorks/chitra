"""Monitor contracts at the journal/pass and process containment boundaries."""

from __future__ import annotations

import contextlib
import json
import os
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
    while True:
        summary = monitor.run_once(config)
        assert not summary["lane_errors"]
        cache = monitor._LANE_JOURNALS[(str(tmp_path), lane)]
        assert len(cache.events) <= monitor._CACHE_EVENTS
        assert cache.bytes <= monitor._CACHE_BYTES
        if summary["backlog_lanes"]:
            assert not checked_claims
        if not summary["backlog_lanes"]:
            break
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

    def never(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unchanged detector input was scanned again")

    monkeypatch.setattr(monitor, "detect_unnecessary_steps", never)
    assert not monitor.run_once(config)["lane_errors"]
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


@pytest.mark.parametrize("failure", ["journal", "ingest", "resolve", "protected", "locked", "cpu", "rss"])
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
    assert [result["lane"] for result in summary["results"]] == ["healthy"]
    records = [json.loads(line) for line in config.findings_path.read_text().splitlines()]
    assert any(row["lane"] == "broken" and row["detector"] == "monitor-internal-error" for row in records)
    if failure == "protected":
        cache = monitor._LANE_JOURNALS[(str(tmp_path), "broken")]
        assert len(cache.events) <= 2
        assert len(broken.load()) == 3

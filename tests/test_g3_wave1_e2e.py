"""Wave-1 burn-down E2E additions: one test per area left with no coverage.

Each test drives the real entry point with fakes only at the outer edge:

- ``chitra-outcomes`` -- the installed console script, over a real state dir;
- ``draft-scanner`` -- the installed console script, with a PATH-level tmux
  shim as the only external edge;
- ``chitra-goals scan-asks`` -- the installed console script, over a real
  transcript file on disk;
- the ``canonical_choices`` detector pipeline -- the exact call monitord's
  pass makes: a real policy.yaml on disk (CHITRA_POLICY_CONFIG), a real
  on-disk event journal loaded through ``load_lane_events``, then
  ``run_detectors``.

No other new tests are added by this PR.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from chitra.journal.models import ByteRange, CanonicalEvent, CanonicalType, Client, TranscriptIdentity

VENV_BIN = Path(sys.executable).parent


def _script(name: str) -> str:
    path = VENV_BIN / name
    if path.exists():
        return str(path)
    import shutil

    found = shutil.which(name)
    assert found is not None, f"{name} console script not found"
    return found


def test_chitra_outcomes_cli_end_to_end(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {
            "order_id": "build-1",
            "session_ref": "host:lane-a:0.0",
            "task_type": "build",
            "status": "completed",
            "created_at": "2026-07-14T09:00:00+00:00",
            "terminal_at": "2026-07-14T09:10:00+00:00",
        },
        {
            "order_id": "build-2",
            "session_ref": "host:lane-a:0.0",
            "task_type": "build",
            "status": "completed",
            "created_at": "2026-07-14T09:20:00+00:00",
            "terminal_at": "2026-07-14T09:50:00+00:00",
        },
    ]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    proc = subprocess.run(
        [_script("chitra-outcomes"), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["totals"]["dispatch_count"] == 2
    assert [family["family"] for family in payload["families"]] == ["build"]


def test_draft_scanner_cli_flags_a_real_unsubmitted_draft(tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    tmux = shim_dir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  capture-pane) printf "a half-typed operator message, no prompt\\n" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}:{env['PATH']}"

    proc = subprocess.run(
        [_script("draft-scanner"), "--targets", "localhost:sess:0.0"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert [finding["session_ref"] for finding in payload["findings"]] == ["localhost:sess:0.0"]
    assert payload["errors"] == []


def test_chitra_goals_scan_asks_cli_extracts_operator_asks(tmp_path: Path) -> None:
    transcript = tmp_path / "lane.jsonl"
    assistant = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "All checks pass.\nFor operator to decide:\n1. Pick the release window."}
            ],
        },
    }
    transcript.write_text(json.dumps(assistant) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [_script("chitra-goals"), "--root", str(tmp_path / "state"), "scan-asks", "--transcript", str(transcript)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "1. Pick the release window." in proc.stdout


def test_monitord_detector_pipeline_flags_configured_deprecated_path_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from chitra.journal.store import EventJournal
    from chitra.monitord import load_lane_events, resolve_config, run_detectors

    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "canonical_choices:\n"
        "  choices:\n"
        "    ops.ledger-path:\n"
        "      kind: deprecated_path\n"
        "      subject: /deprecated/ledger.json\n"
        "      canonical_value: /approved/ledger.json\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CHITRA_POLICY_CONFIG", str(policy))

    state_dir = tmp_path / "state"
    lane = "lane-e2e"
    journal = EventJournal(state_dir, lane)
    journal.directory.mkdir(parents=True)
    event = CanonicalEvent(
        event_id="e-e2e-1",
        instance="i",
        lane=lane,
        client=Client.CLAUDE,
        client_version="2.1.229",
        process_id=None,
        transcript=TranscriptIdentity(path="/t.jsonl", device=0, inode=0),
        session_id="session-1",
        resume_id=None,
        observed_at="2026-08-23T12:00:00Z",
        native_time=None,
        native_type="assistant",
        native_join_id=None,
        raw_byte_range=ByteRange(start=0, end=1),
        raw_sha256=None,
        normalized_type=CanonicalType.TOOL_CALL,
        payload_digest="d" * 64,
        normalizer_version="n1",
        payload={
            "tool_name": "write_file",
            "input": {"file_path": "/deprecated/ledger.json"},
            "cwd": "/work",
        },
        raw_record=None,
    )
    journal.path.write_text(
        json.dumps(event.model_dump(mode="json", by_alias=True)) + "\n", encoding="utf-8"
    )

    config = resolve_config(state_dir=state_dir, lanes_file=tmp_path / "lanes.yaml")
    events = load_lane_events(config, lane)
    assert len(events) == 1

    goal = SimpleNamespace(scope="", intent="", goal="", enrolled_done_when_items=(), session_ref="")
    findings = run_detectors(config, lane, goal, events)

    assert [finding.detector for finding in findings] == ["canonical_choices.deprecated_path"]
    assert "/approved/ledger.json" in findings[0].expected_next_progress

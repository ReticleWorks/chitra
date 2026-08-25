"""Tests for the composed monitord entrypoint."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra.detect import Finding
from chitra.goals import GoalRecord, GoalStatus, upsert_goal
from chitra.journal import ByteRange, CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.journal.store import EventJournal, classify_progress
from chitra.monitord import (
    MonitordConfig,
    append_finding_records,
    build_arg_parser,
    check_enrollment_and_receipts,
    resolve_config,
    run_detectors,
    run_once,
)

LANE = "lane-a:0.0"
SEEDED_LANE = "lane-a.0.0"


def _config(tmp_path: Path) -> MonitordConfig:
    return resolve_config(state_dir=tmp_path)


def _event(
    event_id: str,
    normalized_type: CanonicalType,
    *,
    lane: str = LANE,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
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
        normalized_type=normalized_type,
        payload_digest="d" * 64,
        normalizer_version="n1",
        payload={},
        raw_record=None,
    )

def test_resolve_config_defaults_to_shadow_mode_on() -> None:
    config = resolve_config()
    assert config.shadow_mode is True
    assert config.poll_seconds > 0

def test_resolve_config_rejects_non_positive_poll_seconds() -> None:
    with pytest.raises(ValueError):
        resolve_config(poll_seconds=0)

def test_resolve_config_honors_explicit_shadow_mode_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_MONITORD_SHADOW_MODE", "1")
    assert resolve_config(shadow_mode=False).shadow_mode is False

def test_resolve_config_reads_shadow_mode_environment_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_MONITORD_SHADOW_MODE", "0")
    assert resolve_config().shadow_mode is False
    monkeypatch.setenv("CHITRA_MONITORD_SHADOW_MODE", "1")
    assert resolve_config().shadow_mode is True

def test_cli_flag_turns_shadow_mode_off_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_MONITORD_SHADOW_MODE", "1")
    args = build_arg_parser().parse_args(["--no-shadow-mode"])
    assert args.shadow_mode is False

def test_run_detectors_orders_findings_by_detector_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events = (
        _event("e1", CanonicalType.TOOL_CALL),
        _event("e2", CanonicalType.FINAL_RESPONSE),
    )
    findings = run_detectors(config, LANE, None, events)
    order = [finding.detector for finding in findings]
    assert order == sorted(order, key=lambda name: ["drift", "unnecessary_steps", "excessive_testing", "document_dithering"].index(name))

def test_append_finding_records_writes_schema_stamped_jsonl(tmp_path: Path) -> None:
    config = _config(tmp_path)
    from chitra.detect import Finding

    finding_one = Finding(
        detector="drift",
        fingerprint_seed={"lane": LANE},
        event_refs=("e1",),
        unmet_item="",
        expected_next_progress="",
        detail="scope breach observed",
    )
    appended = append_finding_records(config, LANE, [finding_one])
    assert appended == 1
    lines = (config.state_dir / "monitord-findings.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["schema"] == "chitra.monitord.pass.v1"
    assert record["lane"] == LANE
    assert record["detector"] == "drift"
    assert record["shadow_mode"] is True

def test_run_once_observes_real_journal_and_composes_outputs(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, SEEDED_LANE)
    journal.append(tuple(_event(f"e{i}", CanonicalType.TOOL_CALL, lane=SEEDED_LANE) for i in range(1, 4)))
    config = resolve_config(state_dir=tmp_path)
    summary = run_once(config)
    assert summary["lanes_observed"] == 1
    assert [result["lane"] for result in summary["results"]] == [SEEDED_LANE]
    assert summary["findings_opened"] == 1
    findings_lines = config.findings_path.read_text(encoding="utf-8").splitlines()
    assert len(findings_lines) == 1
    finding_record = json.loads(findings_lines[0])
    assert finding_record["schema"] == "chitra.monitord.pass.v1"
    assert finding_record["lane"] == SEEDED_LANE
    assert finding_record["detector"] == "unnecessary_steps"
    incident_lines = (tmp_path / "incidents" / f"{SEEDED_LANE}.jsonl").read_text(encoding="utf-8").splitlines()
    incident_record = json.loads(incident_lines[-1])
    assert incident_record["lane"] == SEEDED_LANE
    assert incident_record["detector"] == "unnecessary_steps"
    assert incident_record["stage"] == "nudge"
    presence_lines = (tmp_path / "presence" / "chitra-monitord.jsonl").read_text(encoding="utf-8").splitlines()
    presence_record = json.loads(presence_lines[-1])
    assert presence_record["instance"] == "chitra-monitord"
    assert presence_record["lanes"] == [SEEDED_LANE]

def test_run_once_excludes_per_lane_progress_journal_from_lane_discovery(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, SEEDED_LANE)
    events = tuple(_event(f"e{i}", CanonicalType.TOOL_CALL, lane=SEEDED_LANE) for i in range(1, 4))
    journal.append(events)
    journal.append_progress(tuple(classify_progress(event, goal_version="0") for event in events))
    assert journal.progress_path.is_file()
    config = resolve_config(state_dir=tmp_path)
    summary = run_once(config)
    assert summary["lanes_observed"] == 1
    assert [result["lane"] for result in summary["results"]] == [SEEDED_LANE]
    finding_records = [json.loads(line) for line in config.findings_path.read_text(encoding="utf-8").splitlines()]
    assert [record["lane"] for record in finding_records] == [SEEDED_LANE]
    presence_lines = (tmp_path / "presence" / "chitra-monitord.jsonl").read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line)["lanes"] == [SEEDED_LANE] for line in presence_lines)


def test_stuck_action_ladder_records_nudge_without_acting_in_shadow_mode(tmp_path: Path) -> None:
    from chitra.monitord import evaluate_findings

    finding = Finding(
        detector="stuck",
        fingerprint_seed={"case": "shadow-nudge"},
        event_refs=("final-1", "final-2", "final-3"),
        unmet_item="item-1",
        expected_next_progress="record explicit progress",
        detail="completed turns did not advance the item",
    )
    config = resolve_config(state_dir=tmp_path, shadow_mode=True)
    actions = evaluate_findings(config, "lane-a.0.0", [finding])

    assert actions == ["nudge"]
    records = [
        json.loads(line)
        for line in (tmp_path / "actions" / "lane-a.0.0.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["action"] == "nudge"
    assert records[-1]["acted"] is False
    assert not (tmp_path / "queue" / "orders").exists()


def test_stuck_action_ladder_enqueues_nudge_only_outside_shadow_mode(tmp_path: Path) -> None:
    from chitra.monitord import evaluate_findings

    finding = Finding(
        detector="stuck",
        fingerprint_seed={"case": "live-nudge"},
        event_refs=("final-1", "final-2", "final-3"),
        unmet_item="item-1",
        expected_next_progress="record explicit progress",
        detail="completed turns did not advance the item",
    )
    queue = tmp_path / "queue"
    config = resolve_config(state_dir=tmp_path, dispatch_queue_dir=queue, shadow_mode=False)
    actions = evaluate_findings(config, "lane-a.0.0", [finding])

    assert actions == ["nudge"]
    orders = list((queue / "orders").glob("*.json"))
    assert len(orders) == 1
    assert json.loads(orders[0].read_text(encoding="utf-8"))["session_ref"] == "lane-a.0.0"
    records = [
        json.loads(line)
        for line in (tmp_path / "actions" / "lane-a.0.0.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["acted"] is True


def _goal(session_ref: str, *, status: GoalStatus = "working") -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Ship the deterministic fleet digest daemon safely today.",
        done_when="The digest file exists and is verified.",
        source="task-file:docs/sweep-digest.md",
        status=status,
        intent="Build a deterministic sensing daemon for compact fleet-state deltas.",
        scope="Daemon module tests and deployment unit only.",
        now="",
        last_verified="",
        created_at="",
        updated_at="",
        **enrollment_fields(
            "The digest file exists and is verified.",
            validator="stub-check",
            required_receipt="daemon-digest-written",
        ),
    )


def _write_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from chitra.validator_registry import VALIDATORS_ENV_VAR

    registry = tmp_path / "validators.json"
    registry.write_text(json.dumps({"stub-check": {"argv": [sys.executable, "-c", "raise SystemExit(1)"]}}), encoding="utf-8")
    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(registry))


def test_check_enrollment_disputes_when_the_validator_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_registry(tmp_path, monkeypatch)
    upsert_goal(tmp_path, _goal("session-1"))
    recorded, disputed, findings = check_enrollment_and_receipts(_config(tmp_path), "session-1")
    assert disputed is True
    assert recorded == 1
    assert all(finding.detector == "false_done" for finding in findings)

def test_check_enrollment_is_silent_for_unenrolled_sessions(tmp_path: Path) -> None:
    recorded, disputed, findings = check_enrollment_and_receipts(_config(tmp_path), "no-such-session")
    assert (recorded, disputed, findings) == (0, False, [])

def test_deprecated_daemon_entrypoints_warn_toward_monitord() -> None:
    import chitra.sweepd as sweepd
    import chitra.triaged as triaged
    import chitra.watchd as watchd

    for module in (watchd, triaged, sweepd):
        with pytest.warns(DeprecationWarning, match="deprecated by chitra-monitord"), contextlib.suppress(SystemExit):
            module.main(["--help"])

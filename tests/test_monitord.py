"""Tests for the composed monitord entrypoint."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _goal_fixtures import enrollment_fields, ingest_passing_receipt, passing_completion_evidence
from structlog.testing import capture_logs

import chitra.monitord as monitord_mod
import chitra.run_pool as run_pool_mod
from chitra.completion_gate import CompletionEvidence
from chitra.decisions import DecisionEntry, append_decision
from chitra.goal_enforcement import ReviewerProcessError
from chitra.goals import EnrolledDoneWhenItem, GoalRecord, GoalsSchemaNewerError, GoalStatus, get_goal, upsert_goal
from chitra.journal import ByteRange, CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.journal.store import EventJournal, classify_progress
from chitra.monitord import (
    MonitordConfig,
    append_finding_records,
    build_arg_parser,
    check_enrollment_and_receipts,
    handle_agent_question,
    ingest_transcript_bindings,
    resolve_config,
    run_detectors,
    run_once,
)
from chitra.recovery import capture_worktree_binding, transition_lane_lifecycle
from chitra.review_rubric import ReviewerVerdict, ReviewFinding
from chitra.transcript_bindings import TranscriptBinding

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

def test_unseen_version_ingests_and_logs_unknown_count(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "w11" / "claude-2.1.280-synthetic.jsonl"
    # A client version no fixture covers, plus a record type the normalizer does not know.
    transcript = tmp_path / "unseen.jsonl"
    transcript.write_text(
        fixture.read_text(encoding="utf-8").replace('"2.1.280"', '"9.9.9"')
        + json.dumps({"type": "brand-new-record", "sessionId": "fixture-claude-280-session", "version": "9.9.9"})
        + "\n",
        encoding="utf-8",
    )
    binding = TranscriptBinding(
        session_ref="goal-x", lane="lane-x", path=str(transcript), client=Client.CLAUDE, client_version="9.9.9", instance="i"
    )

    with capture_logs() as logs:
        observed = ingest_transcript_bindings(_config(tmp_path), (binding,))

    assert len(observed) == 13
    assert len(EventJournal(tmp_path, "lane-x").load()) == 13
    (drift,) = [entry for entry in logs if entry["event"] == "monitord_unknown_events_ingested"]
    assert drift["lane"] == "lane-x"
    assert drift["events"] == 7
    assert drift["types"]["brand-new-record"] == 1


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
        _event("e1", CanonicalType.TOOL_CALL, lane=SEEDED_LANE),
        _event("e2", CanonicalType.FINAL_RESPONSE),
    )
    findings = run_detectors(config, LANE, None, events)
    order = [finding.detector for finding in findings]
    assert order == sorted(order, key=lambda name: ["drift", "unnecessary_steps", "excessive_testing", "document_dithering"].index(name))


def test_run_detectors_binds_findings_to_the_goal_enrollment(tmp_path: Path) -> None:
    config = _config(tmp_path)
    goal = SimpleNamespace(
        scope="",
        intent="",
        goal="finish the enrolled implementation",
        enrolled_done_when_items=(
            EnrolledDoneWhenItem(
                id="implementation-complete",
                text="the implementation passes its checks",
                validator="pytest",
                required_receipt="checks-green",
            ),
        ),
    )
    events = tuple(_event(f"repeat-{index}", CanonicalType.TOOL_CALL) for index in range(3))

    findings = run_detectors(config, SEEDED_LANE, goal, events)

    unnecessary = [finding for finding in findings if finding.detector == "unnecessary_steps"]
    assert unnecessary
    assert all(finding.unmet_item == "implementation-complete" for finding in unnecessary)


def _git_worktree(tmp_path: Path) -> Path:
    workdir = tmp_path / "worktree"
    workdir.mkdir()

    def _run(*args: str) -> None:
        subprocess.run(["git", "-C", str(workdir), *args], check=True, capture_output=True)

    _run("init")
    _run("config", "user.email", "chitra-test@example.test")
    _run("config", "user.name", "Chitra Test")
    (workdir / "README.md").write_text("initial\n", encoding="utf-8")
    _run("add", "README.md")
    _run("commit", "-m", "initial")
    return workdir


def test_run_detectors_flags_only_work_outside_the_checkpointed_worktree(tmp_path: Path) -> None:
    """The drift boundary check must see the lane's durable worktree path."""
    workdir = _git_worktree(tmp_path)
    transition_lane_lifecycle(
        tmp_path,
        session_ref="host:lane-a:0.0",
        target="active",
        binding=capture_worktree_binding(workdir),
        resume_note="Begin the enrolled work.",
    )
    goal = SimpleNamespace(
        scope="",
        intent="",
        goal="finish the enrolled implementation",
        session_ref="host:lane-a:0.0",
        enrolled_done_when_items=(),
    )
    events = (
        _event("e-out", CanonicalType.TOOL_CALL).model_copy(
            update={"payload": {"tool_name": "Edit", "input": {"file_path": str(tmp_path / "elsewhere" / "x.py")}}}
        ),
        _event("e-in", CanonicalType.TOOL_CALL).model_copy(
            update={"payload": {"tool_name": "Edit", "input": {"file_path": str(workdir / "x.py")}}}
        ),
    )

    drift = [finding for finding in run_detectors(_config(tmp_path), LANE, goal, events) if finding.detector == "drift"]

    assert [finding.event_refs for finding in drift] == [("e-out",)]


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
    assert summary["findings_opened"] == 0
    findings_lines = config.findings_path.read_text(encoding="utf-8").splitlines()
    assert len(findings_lines) == 1
    finding_record = json.loads(findings_lines[0])
    assert finding_record["schema"] == "chitra.monitord.pass.v1"
    assert finding_record["lane"] == SEEDED_LANE
    assert finding_record["detector"] == "unnecessary_steps"
    # A journal without an exact transcript-to-goal binding remains
    # diagnostic-only. It may record observations, but cannot mutate the
    # incident ladder that a later goal could inherit.
    assert not (tmp_path / "incidents" / f"{SEEDED_LANE}.jsonl").exists()
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


def test_run_once_skips_a_non_active_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = EventJournal(tmp_path, SEEDED_LANE)
    journal.append(tuple(_event(f"e{i}", CanonicalType.TOOL_CALL, lane=SEEDED_LANE) for i in range(1, 4)))
    monkeypatch.setattr(monitord_mod, "get_lane_lifecycle", lambda *_args: SimpleNamespace(enforcement_enabled=False))
    monkeypatch.setattr(
        monitord_mod,
        "run_detectors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("detectors must not run for paused lanes")),
    )

    summary = run_once(resolve_config(state_dir=tmp_path, shadow_mode=False))

    assert summary["lanes_observed"] == 1
    assert summary["findings_opened"] == 0
    assert not (tmp_path / "monitord-findings.jsonl").exists()


def test_run_once_stays_alive_but_takes_no_action_on_newer_goal_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitord_mod, "load_transcript_bindings", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(monitord_mod, "ingest_transcript_bindings", lambda *_args, **_kwargs: ())

    def reject_newer_schema(_root: Path) -> list[GoalRecord]:
        raise GoalsSchemaNewerError("newer goals schema")

    monkeypatch.setattr(monitord_mod, "list_goals", reject_newer_schema)
    monkeypatch.setattr(
        monitord_mod,
        "run_detectors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("detectors must not run")),
    )

    summary = run_once(resolve_config(state_dir=tmp_path, shadow_mode=False))

    assert summary["blocked_reason"] == "goals-schema-newer-than-installed"
    assert summary["lanes_observed"] == 0
    assert summary["findings_opened"] == 0
    assert summary["results"] == []


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


def _write_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int = 1) -> None:
    import sys

    from chitra.validator_registry import VALIDATORS_ENV_VAR

    registry = tmp_path / "validators.json"
    registry.write_text(
        json.dumps({"stub-check": {"argv": [sys.executable, "-c", f"raise SystemExit({exit_code})"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(registry))


def test_check_enrollment_disputes_when_the_validator_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_registry(tmp_path, monkeypatch)
    upsert_goal(tmp_path, _goal("session-1"))
    final_response = _event("completion-1", CanonicalType.FINAL_RESPONSE).model_copy(
        update={
            "payload": {
                "text": "Done.\nCHITRA-COMPLETION: "
                + json.dumps(
                    {
                        "kind": "artifact",
                        "done_when_item_id": "done-1",
                        "receipt_name": "daemon-digest-written",
                        "validator": "stub-check",
                        "validator_result": "pass",
                        "citation": "proof /tmp/daemon-digest.json",
                    }
                )
            }
        }
    )
    recorded, disputed, findings, pending = check_enrollment_and_receipts(
        resolve_config(state_dir=tmp_path, shadow_mode=False),
        "session-1",
        final_response,
    )
    assert disputed is True
    assert recorded == 1
    assert pending is False
    assert all(finding.detector == "false_done" for finding in findings)


def test_check_enrollment_accepts_a_plain_claim_when_the_validator_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, monkeypatch, exit_code=0)
    upsert_goal(tmp_path, _goal("session-1"))
    final_response = _event("completion-plain", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "Done. The digest file exists and is verified."}}
    )
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    # The isolated review runs on the worker pool: the first pass queues it
    # and reports pending, a later pass consumes the durable signal.
    recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=_StubReviewer("accept")
    )
    assert (recorded, disputed, findings, pending) == (1, False, [], True)
    assert monitord_mod._REVIEW_POOL.wait_idle(timeout=15)

    recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config,
        "session-1",
        final_response,
        reviewer=_StubReviewer("accept"),
    )

    assert (recorded, disputed, findings, pending) == (1, False, [], False)
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None
    assert stored.status == "done-pending-close"


def test_check_enrollment_disputes_a_plain_claim_when_the_validator_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, monkeypatch)
    upsert_goal(tmp_path, _goal("session-1"))
    final_response = _event("completion-plain-fail", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "Done. The digest file exists and is verified."}}
    )

    recorded, disputed, findings, pending = check_enrollment_and_receipts(
        resolve_config(state_dir=tmp_path, shadow_mode=False),
        "session-1",
        final_response,
    )

    assert disputed is True
    assert recorded == 1
    assert pending is False
    assert all(finding.detector == "false_done" for finding in findings)
    assert any("stub-check" in finding.detail for finding in findings)
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None
    assert stored.status == "completion-disputed"


def test_check_enrollment_runs_nothing_for_a_negated_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, monkeypatch, exit_code=0)
    upsert_goal(tmp_path, _goal("session-1"))
    calls: list[str] = []

    def record_runs(root: Path, session_ref: str, items: object) -> tuple[CompletionEvidence, ...]:
        del root, items
        calls.append(session_ref)
        return ()

    monkeypatch.setattr(monitord_mod, "record_enrolled_validator_runs", record_runs)
    final_response = _event("completion-negated", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "Not done yet."}}
    )

    recorded, disputed, findings, pending = check_enrollment_and_receipts(
        resolve_config(state_dir=tmp_path, shadow_mode=False),
        "session-1",
        final_response,
    )

    assert (recorded, disputed, findings, pending) == (0, False, [], False)
    assert calls == []
    assert not (tmp_path / "validation-receipts").exists()
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None
    assert stored.status == "working"


def test_check_enrollment_is_silent_for_unenrolled_sessions(tmp_path: Path) -> None:
    assert check_enrollment_and_receipts(_config(tmp_path), "no-such-session") == (0, False, [], False)


def _claim_event(event_id: str) -> CanonicalEvent:
    return _event(event_id, CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "Done. The digest file exists and is verified."}}
    )


def test_check_enrollment_replays_the_stored_outcome_for_an_unchanged_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """proof.md gap 7: a repeated claim must not re-execute its validators.

    The first evaluation runs them and stores a durable outcome marker; the
    second identical evaluation replays it. Changing the registry bytes
    reopens the check.
    """
    _write_registry(tmp_path, monkeypatch, exit_code=1)
    upsert_goal(tmp_path, _goal("session-1"))
    real = monitord_mod.record_enrolled_validator_runs
    calls: list[str] = []

    def counting(root: Path, session_ref: str, items: object) -> tuple[CompletionEvidence, ...]:
        calls.append(session_ref)
        return real(root, session_ref, items)

    monkeypatch.setattr(monitord_mod, "record_enrolled_validator_runs", counting)
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)
    claim = _claim_event("claim-1")

    first = check_enrollment_and_receipts(config, "session-1", claim)
    second = check_enrollment_and_receipts(config, "session-1", claim)

    assert calls == ["session-1"]
    assert first[0] == 1 and first[1] is True and first[3] is False
    assert second[0] == first[0] and second[1] == first[1]
    assert [finding.fingerprint for finding in second[2]] == [finding.fingerprint for finding in first[2]]

    _write_registry(tmp_path, monkeypatch, exit_code=0)
    third = check_enrollment_and_receipts(
        config, "session-1", claim, reviewer=_StubReviewer("accept")
    )
    assert calls == ["session-1", "session-1"]
    # The registry pin is frozen at enrollment, so edited registry bytes
    # reopen the check but cannot flip the frozen item to a pass: the
    # drifted validator refuses to run and the claim stays disputed.
    assert third[1] is True
    fourth = check_enrollment_and_receipts(
        config, "session-1", claim, reviewer=_StubReviewer("accept")
    )
    assert calls == ["session-1", "session-1"]
    assert fourth[1] == third[1]
    assert [finding.fingerprint for finding in fourth[2]] == [finding.fingerprint for finding in third[2]]


def test_check_enrollment_evaluates_every_claim_in_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """detect.md gap 9: the newest final response must not erase an earlier one."""
    _write_registry(tmp_path, monkeypatch, exit_code=1)
    upsert_goal(tmp_path, _goal("session-1"))
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)
    earlier = _claim_event("claim-earlier")
    latest = _claim_event("claim-latest")

    recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config,
        "session-1",
        latest,
        final_responses=(earlier, latest),
    )

    assert (recorded, disputed, pending) == (2, True, False)
    assert monitord_mod._load_claim_checks(config, "session-1").keys() == {"claim-earlier", "claim-latest"}
    refs = {ref for finding in findings for ref in finding.event_refs}
    assert "claim-earlier" in refs


def test_check_enrollment_surfaces_a_goal_store_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """proof.md gap 11: a failed get_goal is a finding, not a silent pass."""
    _write_registry(tmp_path, monkeypatch)
    upsert_goal(tmp_path, _goal("session-1"))

    def broken(root: Path, session_ref: str, **kwargs: object) -> None:
        raise OSError("goal store read failed")

    monkeypatch.setattr(monitord_mod, "get_goal", broken)

    with capture_logs() as logs:
        recorded, disputed, findings, pending = check_enrollment_and_receipts(
            _config(tmp_path), "session-1", _claim_event("claim-1")
        )

    assert (recorded, disputed, pending) == (0, True, False)
    (finding,) = findings
    assert finding.detector == "monitor-internal-error"
    assert finding.fingerprint_seed["reason"] == "goal-lookup-failed"
    assert any(entry["event"] == "monitord_goal_lookup_failed" for entry in logs)


def test_check_enrollment_flags_exit_before_contract_when_the_turn_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that ended with no final response fails its enrolled contract."""
    _write_registry(tmp_path, monkeypatch)
    upsert_goal(tmp_path, _goal("session-1", status="turn-finished-unverified"))
    boundary = _event("resume-1", CanonicalType.RESUME)

    recorded, disputed, findings, pending = check_enrollment_and_receipts(
        _config(tmp_path),
        "session-1",
        None,
        final_responses=(),
        turn_ended=True,
        turn_end_event=boundary,
    )

    assert (recorded, disputed, pending) == (0, True, False)
    (finding,) = findings
    assert finding.detector == "false_done"
    assert finding.fingerprint_seed["reason"] == "exit-before-contract"
    assert finding.event_refs == ("resume-1",)


def test_load_lane_events_parses_only_new_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    journal = EventJournal(tmp_path, SEEDED_LANE)
    journal.append((_event("e1", CanonicalType.TOOL_CALL, lane=SEEDED_LANE),))
    assert [event.event_id for event in monitord_mod.load_lane_events(config, SEEDED_LANE)] == ["e1"]

    parses: list[object] = []
    original = CanonicalEvent.model_validate_json

    def counting(cls: type[CanonicalEvent], data: object, *args: object, **kwargs: object) -> CanonicalEvent:
        parses.append(data)
        return original(data, *args, **kwargs)

    monkeypatch.setattr(CanonicalEvent, "model_validate_json", classmethod(counting))
    try:
        journal.append((_event("e2", CanonicalType.TOOL_CALL, lane=SEEDED_LANE),))
        assert [event.event_id for event in monitord_mod.load_lane_events(config, SEEDED_LANE)] == ["e1", "e2"]
        assert len(parses) == 1
        assert [event.event_id for event in monitord_mod.load_lane_events(config, SEEDED_LANE)] == ["e1", "e2"]
        assert len(parses) == 1
    finally:
        monitord_mod._LANE_JOURNALS.clear()


def test_load_lane_events_reloads_after_rewrite_and_truncate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    journal = EventJournal(tmp_path, SEEDED_LANE)
    journal.append((_event("e1", CanonicalType.TOOL_CALL, lane=SEEDED_LANE),))
    assert [event.event_id for event in monitord_mod.load_lane_events(config, SEEDED_LANE)] == ["e1"]

    try:
        # Same-size rewrite: a different event row of identical serialized length.
        replacement = _event("e2", CanonicalType.TOOL_CALL, lane=SEEDED_LANE)
        journal.path.write_text(replacement.model_dump_json() + "\n", encoding="utf-8")
        stat = journal.path.stat()
        os.utime(journal.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        assert [event.event_id for event in monitord_mod.load_lane_events(config, SEEDED_LANE)] == ["e2"]

        journal.path.write_text("", encoding="utf-8")
        assert monitord_mod.load_lane_events(config, SEEDED_LANE) == ()
    finally:
        monitord_mod._LANE_JOURNALS.clear()


def test_ingest_transcript_bindings_pools_the_ingestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """detect.md gap 15: a second pass must not replay the whole transcript."""
    fixture = Path(__file__).parent / "fixtures" / "w11" / "claude-2.1.229-synthetic.jsonl"
    transcript = tmp_path / "pooled.jsonl"
    transcript.write_bytes(fixture.read_bytes())
    binding = TranscriptBinding(
        session_ref="goal-x",
        lane="lane-x",
        path=str(transcript),
        client=Client.CLAUDE,
        client_version="2.1.229",
        instance="i",
    )
    config = _config(tmp_path)
    try:
        first = ingest_transcript_bindings(config, (binding,))
        key = (str(config.state_dir), str(transcript.resolve()))
        assert key in monitord_mod._INGESTOR_POOL
        pooled = monitord_mod._INGESTOR_POOL[key][1]

        assert ingest_transcript_bindings(config, (binding,)) == ()
        assert monitord_mod._INGESTOR_POOL[key][1] is pooled

        # The pooled normalizer answers the session id without a fresh replay.
        def never(_path: Path) -> str:
            raise AssertionError("native_session_identity replayed the transcript")

        monkeypatch.setattr(monitord_mod, "native_session_identity", never)
        assert monitord_mod._bound_native_session_id(config, transcript.resolve()) == "fixture-claude-session"

        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"type": "brand-new-record", "sessionId": "fixture-claude-session"}) + "\n"
            )
        assert len(ingest_transcript_bindings(config, (binding,))) == 1
        assert len(first) == 10
    finally:
        for _key, (_context, ingestor) in monitord_mod._INGESTOR_POOL.items():
            ingestor.close()
        monitord_mod._INGESTOR_POOL.clear()


class _StubReviewer:
    """Deterministic stand-in for the isolated ``claude -p`` reviewer."""

    def __init__(self, verdict: str) -> None:
        self._verdict = verdict
        self.calls: list[str] = []

    def review(self, goal: object, behavior: object, reviewer_id: str) -> ReviewerVerdict:
        self.calls.append(reviewer_id)
        findings: tuple[ReviewFinding, ...] = ()
        if self._verdict == "reject":
            findings = (
                ReviewFinding(
                    code="unsupported_completion",
                    detail="the turn claims proof it does not contain",
                    citation="Done.",
                ),
            )
        return ReviewerVerdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,  # type: ignore[attr-defined]
            behavior_sha256=behavior.behavior_sha256,  # type: ignore[attr-defined]
            verdict=self._verdict,  # type: ignore[arg-type]
            findings=findings,
        )


def _verified_claim_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CanonicalEvent:
    """Enroll a goal, store its passing receipt, and return a clean claim event."""
    goal = upsert_goal(tmp_path, replace(_goal("session-1"), **enrollment_fields("The digest file exists and is verified.")))
    ingest_passing_receipt(tmp_path, goal.session_ref)
    monkeypatch.setattr(
        monitord_mod,
        "record_enrolled_validator_runs",
        lambda *_args, **_kwargs: (passing_completion_evidence(),),
    )
    return _event("completion-1", CanonicalType.FINAL_RESPONSE).model_copy(
        update={
            "payload": {
                "text": "Done.\nCHITRA-COMPLETION: "
                + json.dumps(
                    {
                        "kind": "artifact",
                        "done_when_item_id": "done-1",
                        "receipt_name": "tests-green",
                        "validator": "pytest",
                        "validator_result": "pass",
                        "citation": "proof /tmp/daemon-digest.json",
                    }
                )
            }
        }
    )


def test_completion_claim_reaches_the_isolated_reviewer_and_applies_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final_response = _verified_claim_setup(tmp_path, monkeypatch)
    reviewer = _StubReviewer("reject")
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    # First pass queues the isolated review on the worker pool; its durable
    # signal is what a later pass consumes.
    _recorded, disputed, _findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert pending is True
    assert monitord_mod._REVIEW_POOL.wait_idle(timeout=15)

    _recorded, disputed, findings, _pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )

    assert reviewer.calls == ["reviewer-1-1", "reviewer-1-2"]
    assert disputed is True
    assert len(findings) == 1
    assert findings[0].detector == "false_done"
    assert findings[0].unmet_item == "isolated completion review"
    assert "rejected the completion claim" in findings[0].detail
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None
    assert stored.status == "completion-disputed"

    # A repeat pass over the unchanged claim reuses the stored signal rather
    # than paying for another isolated review round.
    _recorded, disputed_again, _findings, _pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert disputed_again is True
    assert reviewer.calls == ["reviewer-1-1", "reviewer-1-2"]


def test_completion_claim_the_isolated_reviewer_accepts_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final_response = _verified_claim_setup(tmp_path, monkeypatch)
    reviewer = _StubReviewer("accept")
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    _recorded, disputed, _findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert pending is True
    assert monitord_mod._REVIEW_POOL.wait_idle(timeout=15)

    _recorded, disputed, findings, _pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )

    assert reviewer.calls == ["reviewer-1-1", "reviewer-1-2"]
    assert disputed is False
    assert findings == []
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None
    assert stored.status == "done-pending-close"


class _RecordingReviewer(_StubReviewer):
    """Stub reviewer that also records the still-running list it was shown."""

    def __init__(self, verdict: str) -> None:
        super().__init__(verdict)
        self.still_running: list[tuple[str, ...]] = []

    def review(self, goal: object, behavior: object, reviewer_id: str) -> ReviewerVerdict:
        self.still_running.append(behavior.still_running)  # type: ignore[attr-defined]
        return super().review(goal, behavior, reviewer_id)


def test_insufficient_review_holds_the_claim_until_lane_work_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final_response = _verified_claim_setup(tmp_path, monkeypatch)
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)
    running = ("background tool call t1 (Bash) is still running",)
    undecided = _RecordingReviewer("insufficient")

    result = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=undecided, still_running=running
    )
    assert result[1:] == (False, [], True)
    assert monitord_mod._REVIEW_POOL.wait_idle(timeout=15)

    result = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=undecided, still_running=running
    )

    assert result[1:] == (False, [], True)
    assert undecided.still_running == [running, running]
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None
    assert stored.status == "working"

    # Once the lane work finishes, the stored "insufficient" signal no longer
    # holds and the same claim is judged afresh — again on the worker pool.
    accepting = _RecordingReviewer("accept")
    _recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=accepting
    )
    assert (disputed, findings, pending) == (False, [], True)
    assert monitord_mod._REVIEW_POOL.wait_idle(timeout=15)

    _recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=accepting
    )

    assert (disputed, findings, pending) == (False, [], False)
    assert accepting.still_running == [(), ()]
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None
    assert stored.status == "done-pending-close"


def _review_run_record(config: MonitordConfig, session_ref: str) -> dict[str, Any]:
    return json.loads(monitord_mod._review_run_path(config, session_ref).read_text(encoding="utf-8"))


def test_review_run_record_tracks_the_background_judge_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    final_response = _verified_claim_setup(tmp_path, monkeypatch)
    release = threading.Event()

    class _BlockedAccepting(_StubReviewer):
        def review(self, goal: object, behavior: object, reviewer_id: str) -> ReviewerVerdict:
            release.wait(15)
            return super().review(goal, behavior, reviewer_id)

    reviewer = _BlockedAccepting("accept")
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    _recorded, _disputed, _findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert pending is True
    run = _review_run_record(config, "session-1")
    assert run["state"] == "running"
    assert run["attempt"] == 1
    assert run["op_id"]
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None and stored.status == "working"

    release.set()
    assert monitord_mod._REVIEW_POOL.wait_idle(timeout=15)
    run = _review_run_record(config, "session-1")
    assert run["state"] == "done"
    assert str(run["signal_id"]).startswith("sha256:")

    _recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert (disputed, findings, pending) == (False, [], False)
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None
    assert stored.status == "done-pending-close"


class _FailingReviewer:
    """Reviewer whose ``claude -p`` call always dies."""

    def __init__(self) -> None:
        self.calls = 0

    def review(self, goal: object, behavior: object, reviewer_id: str) -> ReviewerVerdict:
        self.calls += 1
        raise ReviewerProcessError("claude -p exited 1")


def test_failed_review_disputes_then_relaunches_only_after_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final_response = _verified_claim_setup(tmp_path, monkeypatch)
    reviewer = _FailingReviewer()
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    _recorded, _disputed, _findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert pending is True
    assert monitord_mod._REVIEW_POOL.wait_idle(timeout=15)

    # A dead judge disputes with the unchanged review-unavailable finding and
    # the record carries the error and the earliest retry.
    _recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert disputed is True
    assert pending is False
    assert any("isolated completion review could not run" in finding.detail for finding in findings)
    run = _review_run_record(config, "session-1")
    assert run["state"] == "failed"
    assert run["attempt"] == 1
    assert run["error"]
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None and stored.status == "completion-disputed"

    # The recorded backoff is still in the future: no second round launches.
    _recorded, disputed, _findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert disputed is True
    assert reviewer.calls == 1

    # Once the retry is due, the next pass relaunches with a bumped attempt.
    monitord_mod._write_review_run(
        monitord_mod._review_run_path(config, "session-1"),
        {**run, "next_retry_at": "2000-01-01T00:00:00+00:00"},
    )
    _recorded, _disputed, _findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert pending is True
    assert _review_run_record(config, "session-1")["attempt"] == 2
    assert monitord_mod._REVIEW_POOL.wait_idle(timeout=15)
    assert reviewer.calls == 2


def test_running_review_record_with_no_live_worker_is_relaunched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    final_response = _verified_claim_setup(tmp_path, monkeypatch)
    release = threading.Event()

    class _BlockedAccepting(_StubReviewer):
        def review(self, goal: object, behavior: object, reviewer_id: str) -> ReviewerVerdict:
            release.wait(15)
            return super().review(goal, behavior, reviewer_id)

    reviewer = _BlockedAccepting("accept")
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    _recorded, _disputed, _findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert pending is True
    first = _review_run_record(config, "session-1")
    assert first["state"] == "running"
    goal = get_goal(tmp_path, "session-1")
    assert goal is not None
    review_key = monitord_mod._op_key(config, goal, "session-1", "review")
    # The judge's own pool holds the round; the shared pool stays free.
    assert monitord_mod._REVIEW_POOL.in_flight(review_key)
    assert not monitord_mod._RUN_POOL.in_flight(review_key)

    # A monitor restart replaces the pool: the running record has no live
    # worker behind it, so the round counts as lost and is relaunched.
    old_pool = monitord_mod._REVIEW_POOL
    monkeypatch.setattr(
        monitord_mod,
        "_REVIEW_POOL",
        run_pool_mod.RunPool(max_workers=2, on_complete=monitord_mod._wake_monitor),
    )
    try:
        with capture_logs() as logs:
            _recorded, _disputed, _findings, pending = check_enrollment_and_receipts(
                config, "session-1", final_response, reviewer=reviewer
            )
        assert pending is True
        assert any(entry["event"] == "monitord_review_run_lost" for entry in logs)
        relaunched = _review_run_record(config, "session-1")
        assert relaunched["attempt"] == 2
        assert relaunched["op_id"] != first["op_id"]
    finally:
        release.set()
    assert old_pool.wait_idle(timeout=15)
    assert monitord_mod._REVIEW_POOL.wait_idle(timeout=15)

    _recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config, "session-1", final_response, reviewer=reviewer
    )
    assert (disputed, findings, pending) == (False, [], False)
    stored = get_goal(tmp_path, "session-1")
    assert stored is not None
    assert stored.status == "done-pending-close"


def test_lane_work_in_flight_names_only_unanswered_background_calls(tmp_path: Path) -> None:
    def call(event_id: str, join_id: str, *, background: bool) -> CanonicalEvent:
        return _event(event_id, CanonicalType.TOOL_CALL).model_copy(
            update={
                "native_join_id": join_id,
                "payload": {"tool_name": "Bash", "input": {"command": "sleep 60", "run_in_background": background}},
            }
        )

    events = (
        call("c1", "t-open", background=True),
        call("c2", "t-done", background=True),
        _event("r2", CanonicalType.TOOL_RESULT).model_copy(update={"native_join_id": "t-done"}),
        call("c3", "t-foreground", background=False),
    )

    assert monitord_mod._lane_work_in_flight(_config(tmp_path), "session-1", events) == (
        "background tool call t-open (Bash) is still running",
    )


def test_validator_pool_accepts_work_after_shutdown() -> None:
    pool = run_pool_mod.RunPool(max_workers=1)
    pool.shutdown()
    ran: list[str] = []

    pool.submit("lane", lambda: ran.append("ran"))

    assert pool.wait_idle(timeout=5)
    assert ran == ["ran"]


def test_routine_question_is_queued_as_an_exact_goal_contract_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, monkeypatch)
    goal = upsert_goal(tmp_path, _goal("session-1"))
    final_response = _event("question-1", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "What proves the goal is done?"}}
    )
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    outcome = handle_agent_question(config, goal, (final_response,))

    assert outcome == "answer_queued"
    orders = list((tmp_path / "queue" / "orders").glob("*.json"))
    assert len(orders) == 1
    payload = json.loads(orders[0].read_text(encoding="utf-8"))
    assert payload["message_kind"] == "goal_contract_answer"
    assert payload["goal_version"] == goal.goal_version
    assert goal.done_when in payload["nudge"]


def test_protected_question_holds_the_goal_without_queueing_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, monkeypatch)
    goal = upsert_goal(tmp_path, _goal("session-1"))
    final_response = _event("question-2", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "May I use a production API key?"}}
    )
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    outcome = handle_agent_question(config, goal, (final_response,))

    assert outcome == "operator_required"
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "held"
    assert stored.open_asks
    assert not list((tmp_path / "queue").glob("**/*.json"))


def test_decided_question_queues_the_cited_answer_without_an_operator_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, monkeypatch)
    goal = upsert_goal(tmp_path, _goal("session-1"))
    append_decision(
        tmp_path / "decisions.jsonl",
        DecisionEntry(
            decision_id="dec-queue-1",
            at="2026-09-22T00:00:00+00:00",
            kind="adjudication",
            decision="Keep the order queue on plain JSONL files; do not add a database.",
            basis="Recorded test ruling.",
            citation="test-suite",
            authority="test authority",
        ),
    )
    final_response = _event("question-decision-1", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "Should the order queue move to a SQLite database?"}}
    )
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    outcome = handle_agent_question(config, goal, (final_response,))

    assert outcome == "answer_queued"
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.open_asks == ()
    assert stored.foreground_tasks == ()
    orders = list((tmp_path / "queue" / "orders").glob("*.json"))
    assert len(orders) == 1
    payload = json.loads(orders[0].read_text(encoding="utf-8"))
    assert payload["message_kind"] == "goal_contract_answer"
    assert "dec-queue-1" in payload["nudge"]


def test_residual_question_stays_active_for_foreground_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, monkeypatch)
    goal = upsert_goal(tmp_path, _goal("session-1"))
    final_response = _event("question-residual", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "Should I redesign the workflow?"}}
    )
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    outcome = handle_agent_question(config, goal, (final_response,))

    assert outcome == "reasoning_required"
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "working"
    assert stored.open_asks == ()
    assert len(stored.foreground_tasks) == 1
    assert stored.foreground_tasks[0].kind == "question"
    assert stored.foreground_tasks[0].source == "monitord"
    assert "Should I redesign the workflow?" in stored.foreground_tasks[0].text
    assert not list((tmp_path / "queue").glob("**/*.json"))


def test_operator_ask_text_carries_the_gate_reasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gap 8: the persisted ask shows why the question was gated."""
    _write_registry(tmp_path, monkeypatch)
    goal = upsert_goal(tmp_path, _goal("session-1"))
    final_response = _event("question-gated", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "May I use a production API key?"}}
    )
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    assert handle_agent_question(config, goal, (final_response,)) == "operator_required"
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert len(stored.open_asks) == 1
    assert "Gates: credentials" in stored.open_asks[0]
    assert "May I use a production API key?" in stored.open_asks[0]
    assert "credentials" in stored.hold_reason


def test_two_questions_in_one_turn_are_handled_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 3/16: every extracted question gets its own durable outcome."""
    _write_registry(tmp_path, monkeypatch)
    goal = upsert_goal(tmp_path, _goal("session-1"))
    final_response = _event("question-pair", CanonicalType.FINAL_RESPONSE).model_copy(
        update={
            "payload": {
                "text": (
                    "What proves the goal is done?\n"
                    "Should I redesign the workflow?\n"
                )
            }
        }
    )
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    outcome = handle_agent_question(config, goal, (final_response,))

    # The routine question queues an answer; the unsettled one becomes a
    # residual task -- both in one pass over one event.
    assert outcome == "reasoning_required"
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert len(stored.foreground_tasks) == 1
    assert "Should I redesign the workflow?" in stored.foreground_tasks[0].text
    orders = list((tmp_path / "queue" / "orders").glob("*.json"))
    assert len(orders) == 1
    payload = json.loads(orders[0].read_text(encoding="utf-8"))
    assert payload["message_kind"] == "goal_contract_answer"


def test_reasked_question_in_a_later_turn_is_a_new_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 16: the same words asked again get a fresh occurrence-bound request."""
    _write_registry(tmp_path, monkeypatch)
    goal = upsert_goal(tmp_path, _goal("session-1"))
    first_turn = _event("question-first", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "What proves the goal is done?"}}
    )
    second_turn = _event("question-second", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "What proves the goal is done?"}}
    )
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    handle_agent_question(config, goal, (first_turn,))
    handle_agent_question(config, goal, (second_turn,))

    orders = sorted((tmp_path / "queue" / "orders").glob("*.json"))
    assert len(orders) == 2
    results = [json.loads(path.read_text())["question_result"] for path in orders]
    assert results[0]["request_id"] != results[1]["request_id"]
    assert {result["occurrence"] for result in results} == {"question-first", "question-second"}


def test_shadow_questions_neither_queue_answers_nor_mutate_the_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, monkeypatch)
    goal = upsert_goal(tmp_path, _goal("session-1"))
    config = resolve_config(state_dir=tmp_path, shadow_mode=True)
    routine = _event("question-shadow-1", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "What proves the goal is done?"}}
    )
    protected = _event("question-shadow-2", CanonicalType.FINAL_RESPONSE).model_copy(
        update={"payload": {"text": "May I use a production API key?"}}
    )

    assert handle_agent_question(config, goal, (routine,)) == "shadow_answer"
    assert handle_agent_question(config, goal, (protected,)) == "operator_required"
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "working"
    assert stored.open_asks == ()
    assert not list((tmp_path / "queue").glob("**/*.json"))

def test_deprecated_daemon_entrypoints_warn_toward_monitord() -> None:
    import chitra.sweepd as sweepd
    import chitra.triaged as triaged
    import chitra.watchd as watchd

    for module in (watchd, triaged, sweepd):
        with pytest.warns(DeprecationWarning, match="deprecated by chitra-monitord"), contextlib.suppress(SystemExit):
            module.main(["--help"])


def test_bad_binding_skips_its_lane_and_other_lanes_still_ingest(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "w11" / "claude-2.1.280-synthetic.jsonl"
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        fixture.read_text(encoding="utf-8")
        + json.dumps({"type": "user", "sessionId": "another-session", "version": "2.1.280"})
        + "\n",
        encoding="utf-8",
    )
    common = {"client": Client.CLAUDE, "client_version": "2.1.280", "instance": "i"}
    bindings = (
        TranscriptBinding(session_ref="g-bad", lane="lane-bad", path=str(bad), **common),
        TranscriptBinding(session_ref="g-ok", lane="lane-ok", path=str(fixture), **common),
    )

    with capture_logs() as logs:
        observed = ingest_transcript_bindings(_config(tmp_path), bindings)

    assert observed and {event.lane for event in observed} == {"lane-ok"}
    assert any(entry["event"] == "monitord_binding_ingest_failed" and entry["lane"] == "lane-bad" for entry in logs)


def test_run_forever_wakes_when_a_worker_completes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import threading
    import time

    passes = 0

    def _counting_pass(_config: MonitordConfig) -> dict[str, int]:
        nonlocal passes
        passes += 1
        if passes == 1:
            # A worker landing during the sleep must wake the loop, not wait
            # out the poll interval.
            monitord_mod._VALIDATOR_RUN_POOL.submit(f"{tmp_path}:wake-test", lambda: None)
        return {}

    monkeypatch.setattr(monitord_mod, "run_once", _counting_pass)
    monkeypatch.setattr(monitord_mod, "notify_ready", lambda: None)
    monkeypatch.setattr(monitord_mod, "notify_watchdog", lambda: None)

    stop = threading.Event()
    threading.Timer(3.0, stop.set).start()
    started = time.monotonic()
    monitord_mod.run_forever(replace(_config(tmp_path), poll_seconds=30.0), stop_event=stop)
    elapsed = time.monotonic() - started

    assert passes >= 2
    assert elapsed < 10.0

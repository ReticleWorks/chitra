"""Faked-completion regressions from missing/proof.md.

Each test reproduces a proven gap and must fail on the pre-fix tree:

- gap 2: a fabricated PVR-family receipt verifies without Chitra executing
  anything; the fix makes registered identities run the generic path.
- gap 3 / detect.md gap 11: validators execute in the monitor's cwd; the fix
  binds them to the lane's recorded worktree for both run and re-execution.
- gap 4 / detect.md gap 10: ``target_dirty``/``live_proof_required`` are dead
  parameters; the fix wires a worktree probe and the fresh-run proof into
  ``detect_false_done``.
- gap 5: the validator identity is a bare name; the fix pins the registry
  definition digest at enrollment and refuses drift.
- gap 6: a symlinked state-root component makes every receipt unverifiable;
  the fix resolves stored paths before the confinement compare.
- gap 13: shadow mode executes lane-triggered validators; the fix returns
  before any execution.
- gap 14: validators inherit the daemon environment; the fix runs them with
  a scrubbed environment.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _goal_fixtures import VALID_INTERVIEW_RECEIPT, passing_completion_evidence

import chitra.monitord as monitord_mod
import chitra.validator_registry as validator_registry
from chitra.goals import (
    EnrolledDoneWhenItem,
    GoalRecord,
    GoalValidationError,
    get_goal,
    mark_completion_gate_passed,
    upsert_goal,
)
from chitra.journal import ByteRange, CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.monitord import check_enrollment_and_receipts, resolve_config
from chitra.recovery import capture_worktree_binding, transition_lane_lifecycle
from chitra.review_rubric import ReviewerVerdict
from chitra.validation_receipts import (
    ReceiptError,
    ingest_receipt,
    load_receipt_file,
    receipt_path,
    record_enrolled_validator_runs,
    verify_receipt,
)
from chitra.validator_registry import VALIDATORS_ENV_VAR, RegisteredValidator, run_registered_validator

SESSION_REF = "host:lane-a:0.0"


def _write_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: dict[str, list[str]]) -> Path:
    registry = tmp_path / "validators.json"
    registry.write_text(
        json.dumps({name: {"argv": argv} for name, argv in entries.items()}),
        encoding="utf-8",
    )
    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(registry))
    monkeypatch.setenv("CHITRA_LANES_FILE", str(tmp_path / "lanes-missing.yaml"))
    return registry


def _enroll(tmp_path: Path, *, validator: str, receipt_name: str, session_ref: str = SESSION_REF) -> GoalRecord:
    done_when = "The enrolled validator run proves the goal done."
    return upsert_goal(
        tmp_path,
        GoalRecord(
            session_ref=session_ref,
            goal="Ship the supervised lane change safely today.",
            done_when=done_when,
            source="task-file:/tmp/b2-proof.md",
            status="working",
            intent="Close only on live validator evidence bound to the lane.",
            scope="Validator execution and receipt verification only.",
            interview_receipt=VALID_INTERVIEW_RECEIPT,
            enrolled_done_when_items=(
                EnrolledDoneWhenItem(
                    id="done-1",
                    text=done_when,
                    validator=validator,
                    required_receipt=receipt_name,
                ),
            ),
        ),
    )


def _git_worktree(tmp_path: Path, name: str = "worktree") -> Path:
    workdir = tmp_path / name
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


def _bind_worktree(root: Path, session_ref: str, workdir: Path, *, target: str = "active") -> None:
    transition_lane_lifecycle(
        root,
        session_ref=session_ref,
        target=target,
        binding=capture_worktree_binding(workdir),
        resume_note="Bind the lane to its worktree.",
    )


def _claim_event(event_id: str = "completion-1", *, text: str | None = None) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        instance="i",
        lane="lane-a:0.0",
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
        normalized_type=CanonicalType.FINAL_RESPONSE,
        payload_digest="d" * 64,
        normalizer_version="n1",
        payload={"text": text if text is not None else "Done. The enrolled validator run proves the goal done."},
        raw_record=None,
    )


class _AcceptReviewer:
    """Deterministic stand-in for the isolated ``claude -p`` reviewer."""

    def review(self, goal: object, behavior: object, reviewer_id: str) -> ReviewerVerdict:
        return ReviewerVerdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,  # type: ignore[attr-defined]
            behavior_sha256=behavior.behavior_sha256,  # type: ignore[attr-defined]
            verdict="accept",
            findings=(),
        )


def _receipt_integrity(payload: dict[str, object]) -> dict[str, object]:
    integrity = {
        "algorithm": "sha256",
        "canonicalization": "UTF-8 JSON; keys sorted; separators comma and colon; ensure_ascii false",
        "scope": "entire receipt with /integrity/digest omitted",
        "hand_authored_fields": [],
    }
    unsigned = dict(payload)
    unsigned["integrity"] = integrity
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    integrity["digest"] = hashlib.sha256(encoded).hexdigest()
    return integrity


def _write_fabricated_pvr_source(root: Path, *, receipt_name: str, command: list[str]) -> Path:
    """Build a fully self-consistent PVR-family receipt no execution produced."""
    source = root / "pvr-source"
    source.mkdir()
    target = source / "artifact.bin"
    target.write_bytes(b"fabricated target\n")
    target_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    coverage_rows = [{"surface": "cli", "covered": True}]
    origin = "argv:" + json.dumps(command, separators=(",", ":"), ensure_ascii=False)
    audit = source / "pvr-audit.jsonl"
    report = source / "pvr-report.json"
    audit.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"fresh_run_sentinel": "PVR-FRESH-RUN-v2"},
                {"event": "run_binding", "family_id": "smoke", "target": origin, "deployed_sha": target_sha},
                {"event": "coverage_ledger", "rows": coverage_rows},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    report.write_text(
        json.dumps(
            {
                "schema_version": "pvr-v2",
                "coverage_ledger": coverage_rows,
                "verdict": {
                    "must_gates": [{"gate": "cli", "passed": True}],
                    "should_gates": [{"gate": "polish", "score": 1.0, "threshold": 0.5}],
                },
                "findings": [],
                "run_meta": {
                    "family_id": "smoke",
                    "deployed_sha": target_sha,
                    "target": origin,
                    "audit_log_sha256": hashlib.sha256(audit.read_bytes()).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "receipt_name": receipt_name,
        "validator": {
            "name": "Polyvalidation Rig",
            "version": "0.2.0",
            "family_id": "smoke",
            "report_path": "pvr-report.json",
            "report_schema": "pvr-v2",
        },
        "target": {"artifact": {"path": str(target), "sha256": target_sha}},
        "exercise": {"command": command},
        "result": {"status": "PASS", "validator_acceptance": True},
        "not_exercised": [],
        "artifacts": [
            {
                "path": "pvr-report.json",
                "kind": "pvr-validation-report",
                "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            },
            {
                "path": "pvr-audit.jsonl",
                "kind": "pvr-audit-log",
                "sha256": hashlib.sha256(audit.read_bytes()).hexdigest(),
            },
        ],
        "produced_at": "2026-08-22T00:00:00Z",
        "integrity": {},
    }
    payload["integrity"] = _receipt_integrity(payload)
    receipt = source / "receipt.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    return receipt


def _place_stored_receipt(root: Path, session_ref: str, source: Path) -> Path:
    stored = receipt_path(root, session_ref, "pvr-pass")
    stored.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(source.read_text(encoding="utf-8"))
    stored.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    for artifact in payload["artifacts"]:
        evidence = stored.parent / str(artifact["path"])
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_bytes((source.parent / str(artifact["path"])).read_bytes())
    return stored


def test_fabricated_pvr_receipt_for_a_registered_family_never_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """proof.md gap 2: PVR paper alone cannot close a registered-validator item."""
    marker = tmp_path / "validator-actually-ran"
    _write_registry(
        tmp_path,
        monkeypatch,
        {
            "pvr-v2/smoke": [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); raise SystemExit(0)",
            ]
        },
    )
    goal = _enroll(tmp_path, validator="pvr-v2/smoke", receipt_name="pvr-pass")

    forged = _write_fabricated_pvr_source(tmp_path, receipt_name="pvr-pass", command=["pvr", "run", "--family", "smoke"])

    # The fabricated envelope is internally consistent but no execution exists:
    # ingest must refuse it rather than storing a verified PASS.
    with pytest.raises(ReceiptError):
        ingest_receipt(tmp_path, goal.session_ref, forged)
    assert not marker.exists()

    # The same fabrication planted in the store cannot verify either.
    _place_stored_receipt(tmp_path, goal.session_ref, forged)
    verification = verify_receipt(tmp_path, goal.session_ref, "pvr-pass")
    assert verification.completion_eligible is False
    assert not marker.exists()

    # A real Chitra-executed run for the enrolled identity still closes.
    proofs = record_enrolled_validator_runs(tmp_path, goal.session_ref, goal.enrolled_done_when_items)
    assert marker.exists()
    assert proofs[0].validator_result == "pass"
    completed = mark_completion_gate_passed(
        tmp_path,
        goal.session_ref,
        now="the registered validator actually ran",
        last_verified="2026-08-22T00:00:00Z",
        completion_evidence=proofs,
    )
    assert completed.status == "done-pending-close"


def test_enrolled_validator_runs_in_the_lane_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """proof.md gap 3 / detect.md gap 11: the run's cwd is the lane's tree."""
    workdir = _git_worktree(tmp_path)
    _write_registry(
        tmp_path,
        monkeypatch,
        {"suite": [sys.executable, "-c", "import os; print(os.path.realpath(os.getcwd()))"]},
    )
    goal = _enroll(tmp_path, validator="suite", receipt_name="suite-pass")
    _bind_worktree(tmp_path, goal.session_ref, workdir)

    record_enrolled_validator_runs(tmp_path, goal.session_ref, goal.enrolled_done_when_items)

    output = (receipt_path(tmp_path, goal.session_ref, "suite-pass").parent / "suite-pass.output.log").read_text(
        encoding="utf-8"
    )
    assert str(workdir.resolve()) in output
    receipt, _raw = load_receipt_file(receipt_path(tmp_path, goal.session_ref, "suite-pass"))
    artifact = receipt.target["artifact"]
    assert isinstance(artifact, dict)
    assert artifact["worktree"] == str(workdir.resolve())


def test_verification_reruns_the_validator_in_the_lane_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gap 3: re-execution is bound to the same recorded worktree as the run."""
    workdir = _git_worktree(tmp_path)
    realpath = str(workdir.resolve())
    _write_registry(
        tmp_path,
        monkeypatch,
        {
            "suite": [
                sys.executable,
                "-c",
                f"import os, sys; sys.exit(0 if os.path.realpath(os.getcwd()) == {realpath!r} else 1)",
            ]
        },
    )
    goal = _enroll(tmp_path, validator="suite", receipt_name="suite-pass")
    _bind_worktree(tmp_path, goal.session_ref, workdir)

    record_enrolled_validator_runs(tmp_path, goal.session_ref, goal.enrolled_done_when_items)
    verification = verify_receipt(tmp_path, goal.session_ref, "suite-pass")
    assert verification.completion_eligible is True

    # A receipt recorded against one worktree stops verifying once the lane is
    # bound somewhere else: the binding follows the latest checkpoint.
    moved = _git_worktree(tmp_path, name="worktree-moved")
    _bind_worktree(tmp_path, goal.session_ref, moved, target="paused")
    moved_check = verify_receipt(tmp_path, goal.session_ref, "suite-pass")
    assert moved_check.completion_eligible is False


def test_registry_drift_after_enrollment_fails_the_run_and_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """proof.md gap 5: a swapped validator definition cannot stand in for the pin."""
    marker = tmp_path / "drifted-argv-ran"
    registry = _write_registry(
        tmp_path,
        monkeypatch,
        {"suite": [sys.executable, "-c", "raise SystemExit(1)"]},
    )
    goal = _enroll(tmp_path, validator="suite", receipt_name="suite-pass")
    item = get_goal(tmp_path, goal.session_ref)
    assert item is not None
    assert item.enrolled_done_when_items[0].validator_sha256 == validator_registry.registered_validator_digest(
        RegisteredValidator(argv=(sys.executable, "-c", "raise SystemExit(1)"))
    )

    registry.write_text(
        json.dumps(
            {
                "suite": {
                    "argv": [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); raise SystemExit(0)",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    proofs = record_enrolled_validator_runs(tmp_path, goal.session_ref, goal.enrolled_done_when_items)
    assert proofs[0].validator_result == "fail"
    assert not marker.exists()

    stored, _raw = load_receipt_file(receipt_path(tmp_path, goal.session_ref, "suite-pass"))
    assert stored.result["status"] == "FAIL"
    output = (receipt_path(tmp_path, goal.session_ref, "suite-pass").parent / "suite-pass.output.log").read_text(
        encoding="utf-8"
    )
    assert "changed since enrollment" in output

    verification = verify_receipt(tmp_path, goal.session_ref, "suite-pass")
    assert verification.verified is False
    assert any("drifted" in issue or "pin" in issue for issue in verification.issues)

    with pytest.raises(GoalValidationError):
        mark_completion_gate_passed(
            tmp_path,
            goal.session_ref,
            now="attempted close on a drifted validator",
            last_verified="2026-08-22T00:00:00Z",
            completion_evidence=(passing_completion_evidence(receipt_name="suite-pass", validator="suite"),),
        )


def test_receipts_verify_under_a_symlinked_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """proof.md gap 6: a symlinked component in the state root cannot blind verification."""
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real)
    _write_registry(tmp_path, monkeypatch, {"pytest": [sys.executable, "-c", "raise SystemExit(0)"]})
    goal = _enroll(link, validator="pytest", receipt_name="tests-green")

    record_enrolled_validator_runs(link, goal.session_ref, goal.enrolled_done_when_items)

    verification = verify_receipt(link, goal.session_ref, "tests-green")
    assert verification.verified is True
    assert verification.completion_eligible is True
    completed = mark_completion_gate_passed(
        link,
        goal.session_ref,
        now="verified under the symlinked root",
        last_verified="2026-08-22T00:00:00Z",
        completion_evidence=(passing_completion_evidence(),),
    )
    assert completed.status == "done-pending-close"


def test_shadow_mode_never_executes_lane_triggered_validators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """proof.md gap 13: observe-only mode spawns nothing and writes no receipts."""
    marker = tmp_path / "shadow-validator-ran"
    _write_registry(
        tmp_path,
        monkeypatch,
        {
            "stub-check": [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); raise SystemExit(0)",
            ]
        },
    )
    goal = _enroll(tmp_path, validator="stub-check", receipt_name="daemon-digest-written")

    result = check_enrollment_and_receipts(
        resolve_config(state_dir=tmp_path, shadow_mode=True),
        goal.session_ref,
        _claim_event(),
        reviewer=_AcceptReviewer(),
    )

    assert result == (0, False, [], False)
    assert not marker.exists()
    assert not (tmp_path / "validation-receipts").exists()
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "working"


def test_registered_validator_runs_with_a_scrubbed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """proof.md gap 14: ambient daemon secrets never reach the validator child."""
    monkeypatch.setenv("CHITRA_LEAK_PROBE", "hunter2-secret")
    entry = RegisteredValidator(
        argv=(sys.executable, "-c", "import os; print('\\n'.join(sorted(os.environ)))"),
    )

    exit_code, output = run_registered_validator(entry)

    assert exit_code == 0
    assert "CHITRA_LEAK_PROBE" not in output
    assert "PATH" in output
    assert "HOME" in output


def test_dirty_lane_worktree_disputes_a_completion_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """proof.md gap 4 / detect.md gap 10: the tree-clean leg is wired to a live probe."""
    workdir = _git_worktree(tmp_path)
    _write_registry(tmp_path, monkeypatch, {"stub-check": [sys.executable, "-c", "raise SystemExit(0)"]})
    goal = _enroll(tmp_path, validator="stub-check", receipt_name="daemon-digest-written")
    _bind_worktree(tmp_path, goal.session_ref, workdir)
    (workdir / "uncommitted-scratch.txt").write_text("half-written\n", encoding="utf-8")

    captured: dict[str, Any] = {}
    real_detector = monitord_mod.detect_false_done

    def spy(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return real_detector(**kwargs)

    monkeypatch.setattr(monitord_mod, "detect_false_done", spy)

    _recorded, disputed, findings, pending = check_enrollment_and_receipts(
        resolve_config(state_dir=tmp_path, shadow_mode=False),
        goal.session_ref,
        _claim_event(),
        reviewer=_AcceptReviewer(),
    )

    assert disputed is True
    assert pending is False
    assert captured.get("target_dirty") is True
    assert captured.get("live_proof_required") is True
    assert captured.get("live_proof_present") is True
    assert any("worktree was dirty" in finding.detail for finding in findings)
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "completion-disputed"


def test_clean_lane_worktree_still_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gap 4 control: a clean checkpointed tree keeps the claim path passing."""
    workdir = _git_worktree(tmp_path)
    _write_registry(tmp_path, monkeypatch, {"stub-check": [sys.executable, "-c", "raise SystemExit(0)"]})
    goal = _enroll(tmp_path, validator="stub-check", receipt_name="daemon-digest-written")
    _bind_worktree(tmp_path, goal.session_ref, workdir)
    config = resolve_config(state_dir=tmp_path, shadow_mode=False)

    # The isolated review runs on the worker pool: the first pass queues it
    # and reports pending, a later pass consumes the durable signal.
    recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config,
        goal.session_ref,
        _claim_event(),
        reviewer=_AcceptReviewer(),
    )
    assert (recorded, disputed, findings, pending) == (1, False, [], True)
    assert monitord_mod._VALIDATOR_RUN_POOL.wait_idle(timeout=15)

    recorded, disputed, findings, pending = check_enrollment_and_receipts(
        config,
        goal.session_ref,
        _claim_event(),
        reviewer=_AcceptReviewer(),
    )

    assert (recorded, disputed, findings, pending) == (1, False, [], False)
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "done-pending-close"

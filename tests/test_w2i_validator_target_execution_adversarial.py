"""W2i regression: a hash-bound failing pytest target cannot pass any gate.

The frozen trusted pytest mapping executes Chitra's own fixed invocation
against the exact current target, so a nine-field PASS receipt that is fully
internally consistent — envelope digest, evidence hashes, and current target
identity all verified — but whose target is a real pytest file containing
`assert False` must be rejected at ingest, at the completion transition, and
at final close.  The report's claimed exit zero is exposed by the trusted
re-execution, and a presence-only `--version` exercise can never establish
PASS in the first place.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from _goal_fixtures import VALID_INTERVIEW_RECEIPT, passing_completion_evidence

from chitra.goals import (
    EnrolledDoneWhenItem,
    GoalRecord,
    GoalValidationError,
    close_goal,
    load_goals,
    mark_completion_gate_passed,
    upsert_goal,
)
from chitra.validation_receipts import ReceiptError, ingest_receipt, list_receipts

RECEIPT_NAME = "pytest-failing-target"
SESSION_REF = "host:w2i-target-execution:0.0"


def _enroll(root: Path) -> GoalRecord:
    done_when = "The exact pytest target passes under Chitra's trusted verifier."
    return upsert_goal(
        root,
        GoalRecord(
            session_ref=SESSION_REF,
            goal="Reject a consistent PASS receipt bound to a failing exact target.",
            done_when=done_when,
            source="task-file:/tmp/w2i-target-execution.md",
            status="working",
            intent="Make PASS depend on executing the exact hash-bound target.",
            scope="Trusted validator target execution only.",
            interview_receipt=VALID_INTERVIEW_RECEIPT,
            enrolled_done_when_items=(
                EnrolledDoneWhenItem(
                    id="done-1",
                    text=done_when,
                    validator="pytest",
                    required_receipt=RECEIPT_NAME,
                ),
            ),
        ),
    )


def _write_hash_bound_failing_target_receipt(root: Path, command: list[str]) -> Path:
    source = root / "source"
    target = source / "targets" / "test_exact_target_fails.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "def test_exact_target_fails() -> None:\n    assert False\n",
        encoding="utf-8",
    )
    report = source / "evidence" / "pytest-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({"schema_version": "chitra-validator-report-v1", "command": command, "exit_code": 0}),
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "receipt_name": RECEIPT_NAME,
        "validator": {"name": "pytest", "version": "test"},
        "target": {"artifact": {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}},
        "exercise": {"command": command},
        "result": {"status": "PASS", "validator_acceptance": True},
        "not_exercised": [],
        "artifacts": [
            {"path": "evidence/pytest-report.json", "kind": "report", "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
        ],
        "produced_at": "2026-08-22T00:00:00Z",
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "UTF-8 JSON; keys sorted; separators comma and colon; ensure_ascii false",
            "scope": "entire receipt with /integrity/digest omitted",
            "hand_authored_fields": [],
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    integrity = payload["integrity"]
    assert isinstance(integrity, dict)
    integrity["digest"] = hashlib.sha256(canonical).hexdigest()
    receipt = source / "receipt.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    return receipt


def _place_stored_receipt(root: Path, source: Path) -> Path:
    stored = root / "validation-receipts" / f"{RECEIPT_NAME}.json"
    stored.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(source.read_text(encoding="utf-8"))
    stored.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    for artifact in payload["artifacts"]:
        evidence = stored.parent / str(artifact["path"])
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_bytes((source.parent / str(artifact["path"])).read_bytes())
    return stored


def _trusted_shaped_command(target_path: Path) -> list[str]:
    return [sys.executable, "-m", "pytest", str(target_path)]


def test_failing_exact_target_is_rejected_at_ingest_completion_transition_and_close(tmp_path: Path) -> None:
    goal = _enroll(tmp_path)

    presence_only_source = _write_hash_bound_failing_target_receipt(
        tmp_path,
        [sys.executable, "-m", "pytest", "--version"],
    )
    with pytest.raises(ReceiptError):
        ingest_receipt(tmp_path, goal.session_ref, presence_only_source)

    source = _write_hash_bound_failing_target_receipt(
        tmp_path,
        _trusted_shaped_command(tmp_path / "source" / "targets" / "test_exact_target_fails.py"),
    )
    with pytest.raises(ReceiptError):
        ingest_receipt(tmp_path, goal.session_ref, source)
    assert list_receipts(tmp_path, goal.session_ref) == []

    _place_stored_receipt(tmp_path, source)
    with pytest.raises(GoalValidationError):
        mark_completion_gate_passed(
            tmp_path,
            goal.session_ref,
            now="claimed pass over a failing target",
            last_verified="2026-08-22T00:00:00Z",
            completion_evidence=(passing_completion_evidence(receipt_name=RECEIPT_NAME),),
        )
    assert load_goals(tmp_path)[0].status == "working"

    record = goal.to_dict()
    record["status"] = "done-pending-close"
    record["completion_proofs"] = [passing_completion_evidence(receipt_name=RECEIPT_NAME).model_dump(mode="json")]
    goals_payload = {"schema": "chitra.goals.v3", "updated_at": "2026-08-22T00:00:00Z", "goals": [record]}
    (tmp_path / "goals.json").write_text(json.dumps(goals_payload), encoding="utf-8")

    with pytest.raises((ReceiptError, GoalValidationError), match="not verified"):
        close_goal(tmp_path, goal.session_ref)
    assert load_goals(tmp_path)[0].status == "done-pending-close"

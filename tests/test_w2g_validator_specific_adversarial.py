"""W2g validator-specific trust regression: an unrelated command cannot pose as the enrolled validator.

A receipt author may author the exercise and report as evidence, but the frozen
validator identity alone selects the Chitra-owned trusted verifier whose
observed result establishes PASS.  A pytest-labeled receipt whose exercise is
`/bin/true` — a program pytest never exercised against the target — must be
rejected at ingest, at the completion transition, and at final close.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from _goal_fixtures import VALID_INTERVIEW_RECEIPT, passing_completion_evidence, trusted_validator_argv

from chitra.goals import (
    EnrolledDoneWhenItem,
    GoalRecord,
    GoalValidationError,
    close_goal,
    mark_completion_gate_passed,
    upsert_goal,
)
from chitra.validation_receipts import ReceiptError, ingest_receipt


def _write_pytest_labeled_true_receipt(root: Path) -> Path:
    source = root / "source"
    report = source / "evidence" / "report.json"
    report.parent.mkdir(parents=True)
    command = ["/bin/true"]
    assert command != trusted_validator_argv(str(report))
    report.write_text(
        json.dumps(
            {
                "schema_version": "chitra-validator-report-v1",
                "command": command,
                "exit_code": 0,
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    payload: dict[str, object] = {
        "receipt_name": "pytest-green",
        "validator": {"name": "pytest", "version": "test"},
        "target": {"artifact": {"path": str(report), "sha256": digest}},
        "exercise": {"command": command},
        "result": {"status": "PASS", "validator_acceptance": True},
        "not_exercised": [],
        "artifacts": [{"path": "evidence/report.json", "kind": "report", "sha256": digest}],
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


def test_unrelated_true_cannot_pose_as_the_enrolled_pytest_validator(tmp_path: Path) -> None:
    done_when = "The current target actually passed pytest."
    goal = upsert_goal(
        tmp_path,
        GoalRecord(
            session_ref="host:w2g-validator-specific:0.0",
            goal="Reject a pytest-labeled receipt whose exercise pytest never ran.",
            done_when=done_when,
            source="task-file:/tmp/w2g-validator-specific.md",
            status="working",
            intent="Make completion depend on the frozen validator's own trusted verifier.",
            scope="Generic validator identity binding only.",
            interview_receipt=VALID_INTERVIEW_RECEIPT,
            enrolled_done_when_items=(
                EnrolledDoneWhenItem(
                    id="done-1",
                    text=done_when,
                    validator="pytest",
                    required_receipt="pytest-green",
                ),
            ),
        ),
    )
    source = _write_pytest_labeled_true_receipt(tmp_path)

    with pytest.raises((ReceiptError, GoalValidationError)):
        ingest_receipt(tmp_path, goal.session_ref, source)
        completed = mark_completion_gate_passed(
            tmp_path,
            goal.session_ref,
            now="/bin/true cannot speak for pytest",
            last_verified="2026-08-22T00:00:00Z",
            completion_evidence=(passing_completion_evidence(receipt_name="pytest-green"),),
        )
        assert completed.status == "done-pending-close"
        assert close_goal(tmp_path, goal.session_ref) == completed

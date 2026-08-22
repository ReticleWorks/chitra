"""W2e adversarial regression: caller-fabricated generic validator results."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from _goal_fixtures import VALID_INTERVIEW_RECEIPT, passing_completion_evidence

from chitra.goals import (
    EnrolledDoneWhenItem,
    GoalRecord,
    GoalValidationError,
    close_goal,
    mark_completion_gate_passed,
    upsert_goal,
)
from chitra.validation_receipts import ReceiptError, ingest_receipt


def _write_caller_fabricated_pass(root: Path) -> Path:
    source = root / "source"
    report = source / "evidence" / "report.json"
    report.parent.mkdir(parents=True)
    command = ["/usr/bin/false"]
    assert subprocess.run(command, check=False).returncode == 1
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
        "receipt_name": "fabricated-green",
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


def test_caller_cannot_fabricate_a_green_generic_validator_report(tmp_path: Path) -> None:
    done_when = "The declared command actually exits successfully."
    goal = upsert_goal(
        tmp_path,
        GoalRecord(
            session_ref="host:w2e-adversarial:0.0",
            goal="Reject a caller-fabricated generic validator result.",
            done_when=done_when,
            source="task-file:/tmp/w2e-adversarial.md",
            status="working",
            intent="Make completion depend on the real validator result.",
            scope="Generic validator receipt verification only.",
            interview_receipt=VALID_INTERVIEW_RECEIPT,
            enrolled_done_when_items=(
                EnrolledDoneWhenItem(
                    id="done-1",
                    text=done_when,
                    validator="pytest",
                    required_receipt="fabricated-green",
                ),
            ),
        ),
    )
    source = _write_caller_fabricated_pass(tmp_path)

    with pytest.raises((ReceiptError, GoalValidationError)):
        ingest_receipt(tmp_path, goal.session_ref, source)
        completed = mark_completion_gate_passed(
            tmp_path,
            goal.session_ref,
            now="caller fabricated green report",
            last_verified="2026-08-22T00:00:00Z",
            completion_evidence=(passing_completion_evidence(receipt_name="fabricated-green"),),
        )
        assert completed.status == "done-pending-close"
        assert close_goal(tmp_path, goal.session_ref) == completed

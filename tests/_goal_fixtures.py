from __future__ import annotations

import hashlib
import json
from pathlib import Path

from chitra.completion_gate import CompletionEvidence
from chitra.goals import EnrolledDoneWhenItem, InterviewReceipt
from chitra.validation_receipts import ingest_receipt

VALID_INTERVIEW_RECEIPT = InterviewReceipt(
    name="interview:test-goal",
    completed_at="2026-08-21T12:00:00+00:00",
    answers_sha256="a" * 64,
    provenance=(
        "operator:test intent",
        "operator:test done condition",
        "operator:test scope exclusion",
        "operator:test constraint",
    ),
)


def enrollment_fields(
    done_when: str,
    *,
    item_id: str = "done-1",
    validator: str = "pytest",
    required_receipt: str = "tests-green",
) -> dict[str, object]:
    return {
        "interview_receipt": VALID_INTERVIEW_RECEIPT,
        "enrolled_done_when_items": (
            EnrolledDoneWhenItem(
                id=item_id,
                text=done_when,
                validator=validator,
                required_receipt=required_receipt,
            ),
        ),
    }


def passing_completion_evidence(
    *,
    item_id: str = "done-1",
    validator: str = "pytest",
    receipt_name: str = "tests-green",
    kind: str = "artifact",
    citation: str = "proof /tmp/test-results.json",
) -> CompletionEvidence:
    return CompletionEvidence(
        kind=kind,  # type: ignore[arg-type]
        done_when_item_id=item_id,
        receipt_name=receipt_name,
        validator=validator,
        validator_result="pass",
        citation=citation,
    )


def trusted_validator_argv(target_path: str) -> list[str]:
    """The exact argv Chitra's trusted pytest verifier runs for PASS receipts."""
    import sys

    return [sys.executable, "-m", "pytest", target_path]


def ingest_passing_receipt(
    root: Path,
    session_ref: str,
    *,
    validator: str = "pytest",
    receipt_name: str = "tests-green",
) -> Path:
    """Install a minimal hash-bound PASS receipt for goal-store tests."""
    source_dir = root / "test-receipt-sources" / hashlib.sha256(session_ref.encode()).hexdigest()
    target_path = source_dir / "targets" / "test_receipt_target_passes.py"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        "def test_receipt_target_passes() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    evidence_path = source_dir / "evidence" / "test-report.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    command = trusted_validator_argv(str(target_path))
    evidence_path.write_text(
        json.dumps({"schema_version": "chitra-validator-report-v1", "command": command, "exit_code": 0}),
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "receipt_name": receipt_name,
        "validator": {"name": validator, "version": "test"},
        "target": {"artifact": {"path": str(target_path), "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest()}},
        "exercise": {"command": command},
        "result": {"status": "PASS", "validator_acceptance": True},
        "not_exercised": [],
        "artifacts": [
            {"path": "evidence/test-report.json", "kind": "report", "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()}
        ],
        "produced_at": "2026-08-21T12:00:00Z",
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
    source = source_dir / "receipt.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    return ingest_receipt(root, session_ref, source)

"""Receipt validator path confinement regressions."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from _goal_fixtures import VALID_INTERVIEW_RECEIPT

from chitra.goals import EnrolledDoneWhenItem, GoalRecord, upsert_goal
from chitra.validation_receipts import ReceiptError, ingest_receipt, receipt_path, verify_receipt_file


def _write_receipt(
    root: Path,
    target: Path,
    *,
    command_target: Path | None = None,
    command: list[str] | None = None,
    receipt_dir: Path | None = None,
) -> Path:
    receipt_dir = receipt_dir or receipt_path(root, "host:path-confinement:0.0", "path-check").parent
    receipt_dir.mkdir(parents=True, exist_ok=True)
    report = receipt_dir / "report.json"
    command = command or [sys.executable, "-m", "pytest", str(command_target or target)]
    report.write_text(
        json.dumps({"schema_version": "chitra-validator-report-v1", "command": command, "exit_code": 0}),
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "receipt_name": "path-check",
        "validator": {"name": "pytest", "version": "test"},
        "target": {"artifact": {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}},
        "exercise": {"command": command},
        "result": {"status": "PASS", "validator_acceptance": True},
        "not_exercised": [],
        "artifacts": [
            {"path": "report.json", "kind": "report", "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
        ],
        "produced_at": "2026-08-26T00:00:00Z",
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "UTF-8 JSON; keys sorted; separators comma and colon; ensure_ascii false",
            "scope": "entire receipt with /integrity/digest omitted",
            "hand_authored_fields": [],
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    integrity = payload["integrity"]
    assert isinstance(integrity, dict)
    integrity["digest"] = hashlib.sha256(encoded).hexdigest()
    receipt = receipt_dir / "path-check.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    return receipt


@pytest.mark.parametrize("target_kind", ["traversal", "external", "symlink"])
def test_untrusted_pytest_target_never_executes_outside_approved_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    monkeypatch.delenv("CHITRA_VALIDATORS_FILE")
    external = tmp_path.parent / f"{tmp_path.name}-external.py"
    external.write_text("def test_external_target() -> None:\n    raise AssertionError('executed')\n", encoding="utf-8")
    if target_kind == "traversal":
        in_workspace = tmp_path / "target.py"
        in_workspace.write_text("def test_target() -> None:\n    assert True\n", encoding="utf-8")
        (tmp_path / "nested").mkdir()
        target = tmp_path / "nested" / ".." / "target.py"
    elif target_kind == "external":
        target = external
    else:
        target = tmp_path / "target-alias.py"
        target.symlink_to(external)

    receipt = _write_receipt(tmp_path, target)
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)

    verification = verify_receipt_file(receipt, approved_root=tmp_path)

    assert verification.verified is False
    assert any("approved workspace" in issue or "symlink" in issue or "traversal" in issue for issue in verification.issues)
    run.assert_not_called()


def test_in_workspace_target_is_allowed_to_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHITRA_VALIDATORS_FILE")
    target = tmp_path / "test_valid_target.py"
    target.write_text("def test_valid_target() -> None:\n    assert True\n", encoding="utf-8")

    verification = verify_receipt_file(_write_receipt(tmp_path, target), approved_root=tmp_path)

    assert verification.verified is True
    assert verification.completion_eligible is True


def test_registered_validator_may_run_external_target_but_receipt_target_cannot_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_target = tmp_path.parent / f"{tmp_path.name}-repository-test.py"
    external_target.write_text("def test_operator_target() -> None:\n    assert True\n", encoding="utf-8")
    misleading_target = tmp_path / "misleading_target.py"
    misleading_target.write_text("def test_receipt_target() -> None:\n    raise AssertionError('receipt target ran')\n", encoding="utf-8")
    registry = tmp_path / "validators.json"
    registry.write_text(
        json.dumps({"pytest": {"argv": [sys.executable, "-m", "pytest", str(external_target)]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHITRA_VALIDATORS_FILE", str(registry))

    verification = verify_receipt_file(
        _write_receipt(tmp_path, misleading_target, command_target=external_target),
        approved_root=tmp_path,
    )

    assert verification.verified is True
    assert verification.completion_eligible is True


def test_registered_validator_rejects_a_rehashed_receipt_with_a_forged_target_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "validator-output.log"
    target.write_text("observed output\n", encoding="utf-8")
    command = [sys.executable, "-c", "raise SystemExit(0)"]
    registry = tmp_path / "validators.json"
    registry.write_text(json.dumps({"pytest": {"argv": command}}), encoding="utf-8")
    monkeypatch.setenv("CHITRA_VALIDATORS_FILE", str(registry))
    receipt = _write_receipt(tmp_path, target, command=command)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["target"]["artifact"]["sha256"] = "0" * 64
    payload["integrity"].pop("digest")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload["integrity"]["digest"] = hashlib.sha256(encoded).hexdigest()
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    verification = verify_receipt_file(receipt, approved_root=tmp_path)

    assert verification.verified is False
    assert "current target artifact digest does not match the receipt" in verification.issues


def test_source_local_registry_cannot_select_validator_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target.py"
    target.write_text("def test_target() -> None:\n    assert True\n", encoding="utf-8")
    source_dir = tmp_path / "source-upload"
    marker = tmp_path / "source-registry-ran"
    malicious_command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]
    trusted_command = [sys.executable, "-c", "import sys; sys.exit(0)"]
    (tmp_path / "validators.json").write_text(
        json.dumps({"pytest": {"argv": trusted_command}}),
        encoding="utf-8",
    )
    (source_dir / "validators.json").parent.mkdir(parents=True)
    (source_dir / "validators.json").write_text(
        json.dumps({"pytest": {"argv": malicious_command}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHITRA_VALIDATORS_FILE", str(tmp_path / "validators.json"))

    malicious_receipt = _write_receipt(
        tmp_path,
        target,
        command=malicious_command,
        receipt_dir=source_dir,
    )
    rejected = verify_receipt_file(malicious_receipt, approved_root=tmp_path)

    assert rejected.verified is False
    assert marker.exists() is False

    trusted_receipt = _write_receipt(tmp_path, target, command=trusted_command, receipt_dir=tmp_path / "trusted-upload")
    accepted = verify_receipt_file(trusted_receipt, approved_root=tmp_path)

    assert accepted.verified is True
    assert accepted.completion_eligible is True


def test_external_source_receipt_uses_only_trusted_state_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target.py"
    target.write_text("def test_target() -> None:\n    assert True\n", encoding="utf-8")
    source_dir = tmp_path.parent / f"{tmp_path.name}-external-upload"
    trusted_command = [sys.executable, "-c", "import sys; sys.exit(0)"]
    (tmp_path / "validators.json").write_text(
        json.dumps({"pytest": {"argv": trusted_command}}),
        encoding="utf-8",
    )
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "validators.json").write_text(
        json.dumps({"pytest": {"argv": [sys.executable, "-c", "raise SystemExit(91)"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHITRA_VALIDATORS_FILE", str(tmp_path / "validators.json"))

    source = _write_receipt(tmp_path, target, command=trusted_command, receipt_dir=source_dir)
    verification = verify_receipt_file(source, approved_root=tmp_path)

    assert verification.verified is True
    assert verification.completion_eligible is True


def test_ingest_external_source_uses_trusted_state_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target.py"
    target.write_text("def test_target() -> None:\n    assert True\n", encoding="utf-8")
    trusted_command = [sys.executable, "-c", "import sys; sys.exit(0)"]
    (tmp_path / "validators.json").write_text(
        json.dumps({"pytest": {"argv": trusted_command}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("CHITRA_VALIDATORS_FILE")
    session_ref = "host:external-ingest:0.0"
    upsert_goal(
        tmp_path,
        GoalRecord(
            session_ref=session_ref,
            goal="Verify an external receipt source safely.",
            done_when="The external receipt verifies.",
            source="task-file:/tmp/external-ingest.md",
            status="working",
            intent="Use only the trusted state registry.",
            scope="External receipt ingest.",
            interview_receipt=VALID_INTERVIEW_RECEIPT,
            enrolled_done_when_items=(
                EnrolledDoneWhenItem(
                    id="done-1",
                    text="The external receipt verifies.",
                    validator="pytest",
                    required_receipt="path-check",
                ),
            ),
        ),
    )

    malicious_source = tmp_path.parent / f"{tmp_path.name}-malicious-upload"
    marker = tmp_path / "source-registry-ran"
    malicious_command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]
    malicious_source.mkdir(parents=True, exist_ok=True)
    (malicious_source / "validators.json").write_text(
        json.dumps({"pytest": {"argv": malicious_command}}),
        encoding="utf-8",
    )
    source = _write_receipt(tmp_path, target, command=malicious_command, receipt_dir=malicious_source)

    with pytest.raises(ReceiptError):
        ingest_receipt(tmp_path, session_ref, source)
    assert marker.exists() is False

    trusted_source = tmp_path.parent / f"{tmp_path.name}-trusted-upload"
    trusted = _write_receipt(tmp_path, target, command=trusted_command, receipt_dir=trusted_source)
    stored = ingest_receipt(tmp_path, session_ref, trusted)

    assert stored.exists()

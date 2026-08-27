"""Acceptance tests for W12 validation receipt storage and close binding."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from _goal_fixtures import VALID_INTERVIEW_RECEIPT, ingest_passing_receipt, passing_completion_evidence

from chitra.goals import EnrolledDoneWhenItem, GoalRecord, GoalValidationError, close_goal, mark_completion_gate_passed, upsert_goal
from chitra.receipts_cli import main
from chitra.validation_receipts import (
    ReceiptError,
    ingest_receipt,
    list_receipts,
    load_receipt_file,
    receipt_path,
    verify_receipt,
)

GOLDEN_RECEIPTS = Path(__file__).parent / "fixtures" / "w12-golden-receipts"


def _goal(*, receipt_name: str = "tests-green") -> GoalRecord:
    done_when = "The exact validation receipt passes its current target checks."
    return GoalRecord(
        session_ref="host:receipt-lane:0.0",
        goal="Store and verify the exact enrolled validation receipt safely.",
        done_when=done_when,
        source="task-file:/tmp/receipt-test.md",
        status="working",
        intent="Make completion depend on durable evidence rather than a caller claim.",
        scope="Receipt storage verification and close behavior only.",
        interview_receipt=VALID_INTERVIEW_RECEIPT,
        enrolled_done_when_items=(
            EnrolledDoneWhenItem(
                id="done-1",
                text=done_when,
                validator="pytest",
                required_receipt=receipt_name,
            ),
        ),
    )


def _negative_receipt(root: Path, status: str) -> Path:
    source_dir = root / f"{status}-source"
    artifact = source_dir / "evidence" / "result.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(f"validator result: {status}\n", encoding="utf-8")
    artifact_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    receipt_name = f"tests-{status.replace('_', '-')}"
    not_exercised: list[object] = [] if status == "FAIL" else [{"surface": "live", "reason": "not run"}]
    payload: dict[str, object] = {
        "receipt_name": receipt_name,
        "validator": {"name": "pytest", "version": "test"},
        "target": {"artifact": {"path": str(artifact), "sha256": artifact_digest}},
        "exercise": {"command": ["/bin/true"]},
        "result": {"status": status, "validator_acceptance": False},
        "not_exercised": not_exercised,
        "artifacts": [{"path": "evidence/result.txt", "kind": "report", "sha256": artifact_digest}],
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
    path = source_dir / "receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_verified_pass_receipt_closes_its_exact_item(tmp_path: Path) -> None:
    goal = upsert_goal(tmp_path, _goal())
    stored_receipt = ingest_passing_receipt(tmp_path, goal.session_ref)

    completed = mark_completion_gate_passed(
        tmp_path,
        goal.session_ref,
        now="receipt verified",
        last_verified="2026-08-21T12:00:00Z",
        completion_evidence=(passing_completion_evidence(),),
    )

    assert completed.completion_proofs[0].citation == str(stored_receipt)
    assert verify_receipt(tmp_path, goal.session_ref, "tests-green").completion_eligible is True
    assert close_goal(tmp_path, goal.session_ref) == completed


@pytest.mark.parametrize("status", ["FAIL", "not_exercised"])
def test_valid_negative_receipts_cannot_close(status: str, tmp_path: Path) -> None:
    receipt_name = f"tests-{status.replace('_', '-')}"
    goal = upsert_goal(tmp_path, _goal(receipt_name=receipt_name))
    source = _negative_receipt(tmp_path, status)
    ingest_receipt(tmp_path, goal.session_ref, source)
    proof = passing_completion_evidence(receipt_name=receipt_name)

    verification = verify_receipt(tmp_path, goal.session_ref, receipt_name)
    assert verification.verified is True
    assert verification.status == status
    assert verification.completion_eligible is False
    with pytest.raises(GoalValidationError, match="only verified PASS receipts close"):
        mark_completion_gate_passed(
            tmp_path,
            goal.session_ref,
            now="caller claimed pass",
            last_verified="2026-08-21T12:00:00Z",
            completion_evidence=(proof,),
        )


def test_ingest_binds_name_and_validator_and_receipts_are_immutable(tmp_path: Path) -> None:
    goal = upsert_goal(tmp_path, _goal())
    stored = ingest_passing_receipt(tmp_path, goal.session_ref)
    assert stored.stat().st_mode & 0o777 == 0o600
    assert [receipt.receipt_name for receipt in list_receipts(tmp_path, goal.session_ref)] == ["tests-green"]

    payload = json.loads(stored.read_text(encoding="utf-8"))
    payload["result"]["status"] = "FAIL"
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReceiptError, match="integrity digest"):
        ingest_receipt(tmp_path, goal.session_ref, replacement)


def test_tampered_evidence_fails_reverification_and_close(tmp_path: Path) -> None:
    goal = upsert_goal(tmp_path, _goal())
    stored = ingest_passing_receipt(tmp_path, goal.session_ref)
    payload = json.loads(stored.read_text(encoding="utf-8"))
    artifact = stored.parent / payload["artifacts"][0]["path"]
    artifact.write_text("tampered\n", encoding="utf-8")

    verification = verify_receipt(tmp_path, goal.session_ref, "tests-green")
    assert verification.verified is False
    assert "digest mismatch" in verification.issues[0]
    with pytest.raises(GoalValidationError, match="receipt is not verified"):
        mark_completion_gate_passed(
            tmp_path,
            goal.session_ref,
            now="tampered receipt",
            last_verified="2026-08-21T12:00:00Z",
            completion_evidence=(passing_completion_evidence(),),
        )


def test_receipts_cli_ingest_list_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    goal = upsert_goal(tmp_path, _goal())
    source_dir = tmp_path / "source-goal"
    source_goal = upsert_goal(source_dir, _goal())
    source = ingest_passing_receipt(source_dir, source_goal.session_ref)
    original, _raw = load_receipt_file(source)

    common = ["--root", str(tmp_path), "--session-ref", goal.session_ref]
    assert main(["ingest", *common, str(source)]) == 0
    capsys.readouterr()
    assert main(["list", *common]) == 0
    assert original.receipt_name in capsys.readouterr().out
    assert main(["verify", *common, original.receipt_name]) == 0
    assert '"completion_eligible": true' in capsys.readouterr().out
    assert receipt_path(tmp_path, goal.session_ref, original.receipt_name).exists()


def _self_asserted_pass_receipt(root: Path, *, command: list[str], report_exit_code: int) -> Path:
    source_dir = root / "fabricated-source"
    artifact = source_dir / "evidence" / "result.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "chitra-validator-report-v1",
                "command": command,
                "exit_code": report_exit_code,
            }
        ),
        encoding="utf-8",
    )
    artifact_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    payload: dict[str, object] = {
        "receipt_name": "self-asserted-pytest-pass",
        "validator": {"name": "pytest", "version": "test"},
        "target": {"artifact": {"path": str(artifact), "sha256": artifact_digest}},
        "exercise": {"command": command},
        "result": {"status": "PASS", "validator_acceptance": True},
        "not_exercised": [],
        "artifacts": [{"path": "evidence/result.json", "kind": "report", "sha256": artifact_digest}],
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
    path = source_dir / "receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_fabricated_pass_with_failing_validator_report_cannot_close(tmp_path: Path) -> None:
    goal = upsert_goal(tmp_path, _goal(receipt_name="self-asserted-pytest-pass"))
    source = _self_asserted_pass_receipt(tmp_path, command=["/usr/bin/false"], report_exit_code=1)

    with pytest.raises(ReceiptError, match="cannot support PASS|cannot establish its PASS"):
        ingest_receipt(tmp_path, goal.session_ref, source)

    stored = receipt_path(tmp_path, goal.session_ref, "self-asserted-pytest-pass")
    stored.parent.mkdir(parents=True)
    stored.write_text(source.read_text(encoding="utf-8"))
    verification = verify_receipt(tmp_path, goal.session_ref, "self-asserted-pytest-pass")
    assert verification.verified is False
    with pytest.raises(GoalValidationError, match="receipt is not verified"):
        mark_completion_gate_passed(
            tmp_path,
            goal.session_ref,
            now="caller self-asserted pass",
            last_verified="2026-08-21T12:00:00Z",
            completion_evidence=(passing_completion_evidence(receipt_name="self-asserted-pytest-pass"),),
        )


def test_receipt_is_stored_at_the_contract_path(tmp_path: Path) -> None:
    goal = upsert_goal(tmp_path, _goal())
    stored = ingest_passing_receipt(tmp_path, goal.session_ref)

    assert stored == receipt_path(tmp_path, goal.session_ref, "tests-green")
    assert stored.name == "tests-green.json"
    assert stored.parent.parent == tmp_path / "validation-receipts"
    assert len(stored.parent.name) == 64


def test_three_w12_golden_envelopes_round_trip_exactly() -> None:
    observed: dict[str, str] = {}
    for path in sorted(GOLDEN_RECEIPTS.glob("*.json")):
        receipt, raw = load_receipt_file(path)
        assert receipt.model_dump(mode="json") == raw
        observed[path.name] = str(receipt.result["status"])

    assert observed == {
        "analysis-document-task.json": "not_exercised",
        "code-task.json": "PASS",
        "live-deployment-task.json": "FAIL",
    }


# ---------------------------------------------------------------------------
# registered validators: watchd executes and stores the result itself
# ---------------------------------------------------------------------------


def _failing_item(validator: str, receipt: str) -> EnrolledDoneWhenItem:
    return EnrolledDoneWhenItem(id="done-1", text="The check passes.", validator=validator, required_receipt=receipt)


def _write_registry(tmp_path: Path, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    from chitra.validator_registry import VALIDATORS_ENV_VAR

    registry = tmp_path / "validators.json"
    registry.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(registry))


def test_record_registered_run_writes_a_pass_receipt_from_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from chitra.validation_receipts import record_enrolled_validator_runs
    from chitra.validator_registry import RegisteredValidator

    _write_registry(tmp_path, {"stub-check": {"argv": [sys.executable, "-c", "print('checked')"]}}, monkeypatch)

    item = _failing_item("stub-check", "stub-receipt")
    entry = RegisteredValidator(argv=[sys.executable, "-c", "print('checked')"])

    proofs = record_enrolled_validator_runs(tmp_path, "host:lane:0.0", [item])

    assert len(proofs) == 1
    assert proofs[0].validator_result == "pass"
    stored = json.loads((receipt_path(tmp_path, "host:lane:0.0", "stub-receipt")).read_text(encoding="utf-8"))
    assert stored["result"] == {"status": "PASS", "validator_acceptance": True}
    assert stored["exercise"]["command"] == list(entry.argv)
    report = json.loads(
        (receipt_path(tmp_path, "host:lane:0.0", "stub-receipt").parent / "stub-receipt.report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report == {"schema_version": "chitra-validator-report-v1", "command": list(entry.argv), "exit_code": 0}


def test_record_registered_run_stores_a_fail_receipt_from_exit_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from chitra.validation_receipts import record_registered_run
    from chitra.validator_registry import RegisteredValidator

    _write_registry(tmp_path, {"fail-check": {"argv": [sys.executable, "-c", "print('boom'); raise SystemExit(1)"]}}, monkeypatch)

    item = _failing_item("fail-check", "fail-receipt")
    entry = RegisteredValidator(argv=[sys.executable, "-c", "print('boom'); raise SystemExit(1)"])

    proof = record_registered_run(tmp_path, "host:lane:0.0", item, entry)

    assert proof.validator_result == "fail"
    stored = json.loads((receipt_path(tmp_path, "host:lane:0.0", "fail-receipt")).read_text(encoding="utf-8"))
    assert stored["result"]["status"] == "FAIL"
    assert stored["result"]["validator_acceptance"] is False
    output_log = (
        receipt_path(tmp_path, "host:lane:0.0", "fail-receipt").parent / "fail-receipt.output.log"
    ).read_text(encoding="utf-8")
    assert "boom" in output_log


def test_record_enrolled_validator_runs_fails_closed_for_unregistered_validators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from chitra.validation_receipts import record_enrolled_validator_runs

    _write_registry(
        tmp_path,
        {"lane-reports-done": {"argv": [sys.executable, "-c", "pass"]}},
        monkeypatch,
    )
    items = [_failing_item("lane-reports-done", "claimed-receipt"), _failing_item("never-registered", "other-receipt")]

    proofs = record_enrolled_validator_runs(tmp_path, "host:lane:0.0", items)

    assert len(proofs) == 2
    by_receipt = {proof.receipt_name: proof.validator_result for proof in proofs}
    assert by_receipt == {"claimed-receipt": "pass", "other-receipt": "fail"}
    stored = json.loads(receipt_path(tmp_path, "host:lane:0.0", "other-receipt").read_text(encoding="utf-8"))
    assert stored["result"]["status"] == "FAIL"
    assert stored["result"]["validator_acceptance"] is False
    report = json.loads(
        (receipt_path(tmp_path, "host:lane:0.0", "other-receipt").parent / "other-receipt.report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["exit_code"] == 125


def test_duplicate_required_receipt_names_are_rejected_at_enrollment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from _goal_fixtures import VALID_INTERVIEW_RECEIPT

    from chitra.goals import GoalRecord, upsert_goal
    from chitra.validator_registry import VALIDATORS_ENV_VAR

    registry = tmp_path / "validators.json"
    registry.write_text(json.dumps({"stub": {"argv": [sys.executable, "-c", "pass"]}}), encoding="utf-8")
    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(registry))

    done_when = "The shared receipt cannot collapse two independent validator results."
    record = GoalRecord(
        session_ref="host:duplicate-lane:0.0",
        goal="Enroll two done items whose receipts stay independent and exact.",
        done_when=done_when,
        source="task-file:/tmp/duplicate.md",
        status="working",
        intent="Keep every enrolled validator result separately attributable.",
        scope="Duplicate receipt-name enrollment rejection only.",
        interview_receipt=VALID_INTERVIEW_RECEIPT,
        enrolled_done_when_items=(
            EnrolledDoneWhenItem(id="done-1", text=done_when, validator="stub", required_receipt="same"),
            EnrolledDoneWhenItem(id="done-2", text=done_when, validator="stub", required_receipt="same"),
        ),
    )

    with pytest.raises(GoalValidationError, match=r"required receipt 'same' is duplicated"):
        upsert_goal(tmp_path, record)
    assert not (tmp_path / "goals.json").exists()


def test_multi_item_runs_with_distinct_receipts_stay_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from chitra.validation_receipts import record_enrolled_validator_runs

    _write_registry(
        tmp_path,
        {
            "pass-check": {"argv": [sys.executable, "-c", "pass"]},
            "fail-check": {"argv": [sys.executable, "-c", "import sys; sys.exit(1)"]},
        },
        monkeypatch,
    )
    items = [
        _failing_item("pass-check", "good-receipt"),
        EnrolledDoneWhenItem(id="done-2", text="The check fails.", validator="fail-check", required_receipt="bad-receipt"),
    ]

    proofs = record_enrolled_validator_runs(tmp_path, "host:lane:0.0", items)

    by_item = {proof.done_when_item_id: proof.validator_result for proof in proofs}
    assert by_item == {"done-1": "pass", "done-2": "fail"}
    assert verify_receipt(tmp_path, "host:lane:0.0", "good-receipt").completion_eligible is True
    bad_verification = verify_receipt(tmp_path, "host:lane:0.0", "bad-receipt")
    assert bad_verification.status == "FAIL"
    assert bad_verification.completion_eligible is False


def test_registered_run_verifies_against_the_instance_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from chitra.validation_receipts import record_enrolled_validator_runs
    from chitra.validator_registry import VALIDATORS_ENV_VAR

    registry = tmp_path / "validators.json"
    registry.write_text(json.dumps({"stub-check": {"argv": [sys.executable, "-c", "pass"]}}), encoding="utf-8")
    monkeypatch.delenv(VALIDATORS_ENV_VAR, raising=False)

    proofs = record_enrolled_validator_runs(tmp_path, "host:lane:0.0", [_failing_item("stub-check", "rooted-receipt")])

    assert len(proofs) == 1
    verification = verify_receipt(tmp_path, "host:lane:0.0", "rooted-receipt")
    assert verification.verified is True
    assert verification.completion_eligible is True


def test_verify_fails_closed_with_exit_125_for_an_unmapped_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from chitra.validation_receipts import record_enrolled_validator_runs
    from chitra.validator_registry import VALIDATORS_ENV_VAR

    registry = tmp_path / "validators.json"
    registry.write_text(json.dumps({"vanishing-check": {"argv": [sys.executable, "-c", "pass"]}}), encoding="utf-8")
    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(registry))
    item = _failing_item("vanishing-check", "orphan-receipt")
    record_enrolled_validator_runs(tmp_path, "host:lane:0.0", [item])

    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(tmp_path / "empty-registry.json"))
    (tmp_path / "empty-registry.json").write_text("{}", encoding="utf-8")

    verification = verify_receipt(tmp_path, "host:lane:0.0", "orphan-receipt")

    assert verification.verified is False
    assert any("125" in issue for issue in verification.issues)


def test_traversal_receipt_name_never_writes_outside_the_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from chitra.validation_receipts import record_registered_run
    from chitra.validator_registry import RegisteredValidator

    _write_registry(tmp_path, {"stub-check": {"argv": [sys.executable, "-c", "pass"]}}, monkeypatch)
    item = _failing_item("stub-check", "../escaped")

    with pytest.raises(ReceiptError, match="path-safe"):
        record_registered_run(tmp_path, "host:lane:0.0", item, RegisteredValidator(argv=[sys.executable, "-c", "pass"]))
    assert not (tmp_path.parent / "escaped.output.log").exists()
    assert not (tmp_path.parent / "escaped.report.json").exists()
    assert not (tmp_path.parent / "escaped.json").exists()

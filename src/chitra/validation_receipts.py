"""Hash-bound validation receipts for frozen goal items.

Receipts are immutable, per-lane records below the instance state root.  The
common envelope deliberately carries validator-specific details inside its
nine fixed top-level fields.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chitra._fsio import locked_json_store, parse_iso8601, write_json_atomic
from chitra.completion_gate import CompletionEvidence, EnrolledDoneWhenItemLike, completion_receipt_issues
from chitra.state_paths import state_dir

_TOP_LEVEL_FIELDS = {
    "receipt_name",
    "validator",
    "target",
    "exercise",
    "result",
    "not_exercised",
    "artifacts",
    "produced_at",
    "integrity",
}
_CANONICALIZATION = "UTF-8 JSON; keys sorted; separators comma and colon; ensure_ascii false"
_INTEGRITY_SCOPE = "entire receipt with /integrity/digest omitted"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RECEIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class ReceiptError(ValueError):
    """Raised when a receipt is malformed, unbound, mutable, or unverified."""


class ReceiptArtifact(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    path: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("artifact sha256 must be a lowercase SHA-256 digest")
        return value


class ReceiptIntegrity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["sha256"]
    canonicalization: str
    scope: str
    digest: str
    hand_authored_fields: list[dict[str, object]] = Field(default_factory=list)

    @field_validator("digest")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("integrity digest must be a lowercase SHA-256 digest")
        return value


class ValidationReceipt(BaseModel):
    """W12's nine-field common validation envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_name: str = Field(min_length=1)
    validator: dict[str, object]
    target: dict[str, object]
    exercise: dict[str, object]
    result: dict[str, object]
    not_exercised: list[object]
    artifacts: list[ReceiptArtifact]
    produced_at: str
    integrity: ReceiptIntegrity

    @field_validator("receipt_name")
    @classmethod
    def receipt_name_is_safe(cls, value: str) -> str:
        if _SAFE_RECEIPT_NAME_RE.fullmatch(value) is None:
            raise ValueError("receipt_name must be a path-safe stable name")
        return value

    @field_validator("produced_at")
    @classmethod
    def produced_at_is_utc(cls, value: str) -> str:
        parse_iso8601(
            value,
            invalid_message="produced_at must be an RFC 3339 datetime",
            timezone_message="produced_at must use UTC",
            require_utc=True,
        )
        return value


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    receipt_name: str
    status: str
    verified: bool
    completion_eligible: bool
    issues: tuple[str, ...]
    path: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_name": self.receipt_name,
            "status": self.status,
            "verified": self.verified,
            "completion_eligible": self.completion_eligible,
            "issues": list(self.issues),
            "path": str(self.path),
        }


def receipts_root(root: Path | None = None) -> Path:
    """Return the instance receipt directory."""
    return (state_dir() if root is None else root) / "validation-receipts"


def receipt_path(root: Path | None, session_ref: str, receipt_name: str) -> Path:
    if _SAFE_RECEIPT_NAME_RE.fullmatch(receipt_name) is None:
        raise ReceiptError("receipt_name must be a path-safe stable name")
    return receipts_root(root) / f"{receipt_name}.json"


def _canonical_digest(payload: Mapping[str, object]) -> str:
    unsigned = dict(payload)
    integrity = unsigned.get("integrity")
    if not isinstance(integrity, dict):
        raise ReceiptError("integrity must be an object")
    unsigned["integrity"] = {key: value for key, value in integrity.items() if key != "digest"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_raw(path: Path) -> dict[str, object]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReceiptError(f"receipt not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"receipt is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReceiptError("receipt must be a JSON object")
    return cast(dict[str, object], payload)


def load_receipt_file(path: Path) -> tuple[ValidationReceipt, dict[str, object]]:
    """Load and validate the fixed envelope and its self-digest."""
    raw = _load_raw(path)
    if set(raw) != _TOP_LEVEL_FIELDS:
        missing = sorted(_TOP_LEVEL_FIELDS - set(raw))
        extra = sorted(set(raw) - _TOP_LEVEL_FIELDS)
        raise ReceiptError(f"receipt must contain exactly the nine common fields; missing={missing!r}; extra={extra!r}")
    try:
        receipt = ValidationReceipt.model_validate(raw)
    except ValueError as exc:
        raise ReceiptError(str(exc)) from exc
    if receipt.integrity.canonicalization != _CANONICALIZATION:
        raise ReceiptError("unsupported integrity canonicalization")
    if receipt.integrity.scope != _INTEGRITY_SCOPE:
        raise ReceiptError("unsupported integrity scope")
    expected = _canonical_digest(raw)
    if not hmac.compare_digest(receipt.integrity.digest, expected):
        raise ReceiptError("receipt integrity digest does not match its canonical envelope")
    _validate_shape(receipt)
    return receipt, raw


def _object(value: object, *, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReceiptError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _text(payload: Mapping[str, object], name: str, *, parent: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ReceiptError(f"{parent}.{name} must be a non-empty string")
    return value


def _validate_shape(receipt: ValidationReceipt) -> None:
    if not receipt.validator:
        raise ReceiptError("validator must be a non-empty object")
    _text(receipt.validator, "name", parent="validator")
    _text(receipt.validator, "version", parent="validator")

    target_kinds = [name for name in ("commit", "artifact") if name in receipt.target]
    if target_kinds != ["commit"] and target_kinds != ["artifact"]:
        raise ReceiptError("target must contain exactly one of commit or artifact")
    target = _object(receipt.target[target_kinds[0]], name=f"target.{target_kinds[0]}")
    target_digest = _text(target, "sha" if target_kinds[0] == "commit" else "sha256", parent=f"target.{target_kinds[0]}")
    if _SHA256_RE.fullmatch(target_digest) is None and not (target_kinds[0] == "commit" and re.fullmatch(r"[0-9a-f]{7,64}", target_digest)):
        raise ReceiptError(f"target.{target_kinds[0]} digest is invalid")
    target_path = Path(
        _text(target, "repository" if target_kinds[0] == "commit" else "path", parent=f"target.{target_kinds[0]}")
    )
    if not target_path.is_absolute():
        raise ReceiptError(f"target.{target_kinds[0]} path must be absolute")

    exercise_kinds = [name for name in ("command", "live_boundary") if name in receipt.exercise]
    if exercise_kinds != ["command"] and exercise_kinds != ["live_boundary"]:
        raise ReceiptError("exercise must contain exactly one of command or live_boundary")
    if exercise_kinds[0] == "command":
        command = receipt.exercise["command"]
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ReceiptError("exercise.command must be a non-empty list of strings")
    else:
        boundary = _object(receipt.exercise["live_boundary"], name="exercise.live_boundary")
        for field in ("host", "service", "url"):
            _text(boundary, field, parent="exercise.live_boundary")

    status = receipt.result.get("status")
    if status not in ("PASS", "FAIL", "not_exercised"):
        raise ReceiptError("result.status must be PASS, FAIL, or not_exercised")
    if not isinstance(receipt.result.get("validator_acceptance"), bool):
        raise ReceiptError("result.validator_acceptance must be a boolean")
    if status == "PASS" and (receipt.result["validator_acceptance"] is not True or receipt.not_exercised):
        raise ReceiptError("PASS requires validator acceptance and an empty not_exercised list")
    if status == "PASS" and not receipt.artifacts:
        raise ReceiptError("PASS requires at least one hash-bound evidence artifact")
    if status != "PASS" and receipt.result["validator_acceptance"] is True:
        raise ReceiptError("FAIL and not_exercised cannot claim validator acceptance")

    seen_paths: set[str] = set()
    for artifact in receipt.artifacts:
        _safe_relative_path(artifact.path)
        if artifact.path in seen_paths:
            raise ReceiptError(f"artifact path is duplicated: {artifact.path}")
        seen_paths.add(artifact.path)


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ReceiptError(f"evidence path must be relative and cannot traverse its receipt directory: {value!r}")
    return path


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_issues(receipt: ValidationReceipt, base: Path) -> list[str]:
    issues: list[str] = []
    for artifact in receipt.artifacts:
        path = base / _safe_relative_path(artifact.path)
        try:
            actual = _hash_file(path)
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            issues.append(f"artifact {artifact.path!r} is not readable: {exc}")
            continue
        if not hmac.compare_digest(actual, artifact.sha256):
            issues.append(f"artifact {artifact.path!r} digest mismatch")
    return issues


def _target_issues(receipt: ValidationReceipt) -> list[str]:
    issues: list[str] = []
    if "artifact" in receipt.target:
        target = _object(receipt.target["artifact"], name="target.artifact")
        path = Path(_text(target, "path", parent="target.artifact"))
        expected = _text(target, "sha256", parent="target.artifact")
        try:
            actual = _hash_file(path)
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            return [f"current target artifact is not readable: {exc}"]
        if not hmac.compare_digest(actual, expected):
            issues.append("current target artifact digest does not match the receipt")
        return issues

    target = _object(receipt.target["commit"], name="target.commit")
    repository = Path(_text(target, "repository", parent="target.commit"))
    expected = _text(target, "sha", parent="target.commit")
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        issues.append(f"current target commit is unreadable: {result.stderr.strip() or result.stdout.strip()}")
    elif result.stdout.strip() != expected:
        issues.append("current target commit does not match the receipt")
    if "live_boundary" in receipt.exercise:
        issues.append("live deployment identity requires host and service readback; a URL and caller-supplied commit cannot close")
    return issues


def _validator_identifiers(receipt: ValidationReceipt) -> set[str]:
    name = _text(receipt.validator, "name", parent="validator")
    identifiers = {name}
    family = receipt.validator.get("family_id")
    if isinstance(family, str) and family:
        identifiers.add(family)
        identifiers.add(f"{name}/{family}")
        schema = receipt.validator.get("report_schema")
        if isinstance(schema, str) and schema:
            identifiers.add(f"{schema}/{family}")
        if name.startswith("Polyvalidation Rig"):
            identifiers.add(f"pvr-v2/{family}")
    return identifiers


def _validator_issues(receipt: ValidationReceipt, base: Path) -> list[str]:
    name = _text(receipt.validator, "name", parent="validator")
    if not name.startswith("Polyvalidation Rig"):
        return _generic_validator_issues(receipt, base)
    report_path = receipt.validator.get("report_path")
    status = receipt.result["status"]
    if report_path is None:
        return [] if status == "not_exercised" else ["PVR PASS or FAIL receipt requires validator.report_path"]
    if not isinstance(report_path, str):
        return ["validator.report_path must be a string"]
    artifact_by_path = {artifact.path: artifact for artifact in receipt.artifacts}
    if report_path not in artifact_by_path:
        return ["validator.report_path must name a hash-bound artifact"]
    try:
        report = _load_raw(base / _safe_relative_path(report_path))
    except ReceiptError as exc:
        return [str(exc)]
    required = {"schema_version", "coverage_ledger", "verdict", "findings", "run_meta"}
    issues: list[str] = []
    if set(report) != required or report.get("schema_version") != "pvr-v2":
        return ["PVR report does not have the exact pvr-v2 top-level shape"]
    run_meta = report.get("run_meta")
    if not isinstance(run_meta, dict):
        return ["PVR report run_meta must be an object"]
    family = receipt.validator.get("family_id")
    if family is not None and run_meta.get("family_id") != family:
        issues.append("PVR report family_id does not match the receipt")
    target_digest = _receipt_target_digest(receipt)
    if run_meta.get("deployed_sha") != target_digest:
        issues.append("PVR report target digest does not match the receipt")
    expected_origin = _pvr_origin(receipt)
    if run_meta.get("target") != expected_origin:
        issues.append("PVR report live target does not match the receipt exercise")

    audit_digest = run_meta.get("audit_log_sha256")
    audit = next((item for item in receipt.artifacts if "audit" in item.kind and item.sha256 == audit_digest), None)
    if audit is None:
        issues.append("PVR report audit digest is not bound to an artifact")
    else:
        issues.extend(_pvr_audit_issues(base / _safe_relative_path(audit.path), report, run_meta))

    verdict = report.get("verdict")
    accepted = _pvr_report_accepted(verdict)
    claimed_acceptance = receipt.result["validator_acceptance"]
    if claimed_acceptance is not accepted:
        issues.append("PVR acceptance checks disagree with result.validator_acceptance")
    if status == "PASS" and not accepted:
        issues.append("PVR report does not pass its acceptance gates")
    if status == "FAIL" and accepted:
        issues.append("PVR FAIL receipt contains an accepted report")
    return issues


def _receipt_target_digest(receipt: ValidationReceipt) -> str:
    if "artifact" in receipt.target:
        return _text(_object(receipt.target["artifact"], name="target.artifact"), "sha256", parent="target.artifact")
    return _text(_object(receipt.target["commit"], name="target.commit"), "sha", parent="target.commit")


def _pvr_origin(receipt: ValidationReceipt) -> str:
    if "command" in receipt.exercise:
        command = cast(list[str], receipt.exercise["command"])
        return "argv:" + json.dumps(command, separators=(",", ":"), ensure_ascii=False)
    boundary = _object(receipt.exercise["live_boundary"], name="exercise.live_boundary")
    return _text(boundary, "url", parent="exercise.live_boundary")


def _pvr_audit_issues(path: Path, report: Mapping[str, object], run_meta: Mapping[str, object]) -> list[str]:
    issues: list[str] = []
    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return [f"PVR audit is unreadable: {exc}"]
    fresh_indexes = [
        index
        for index, record in enumerate(records)
        if isinstance(record, dict) and record.get("fresh_run_sentinel") == "PVR-FRESH-RUN-v2"
    ]
    if not fresh_indexes:
        return ["PVR audit has no fresh-run sentinel"]
    run_records = records[fresh_indexes[-1] + 1 :]
    binding = next((record for record in run_records if isinstance(record, dict) and record.get("event") == "run_binding"), None)
    if binding is None:
        issues.append("PVR audit has no run binding after its last fresh-run sentinel")
    else:
        for field in ("family_id", "target", "deployed_sha"):
            if binding.get(field) != run_meta.get(field):
                issues.append(f"PVR audit {field} does not match the report")
    coverage = next(
        (record for record in reversed(run_records) if isinstance(record, dict) and record.get("event") == "coverage_ledger"),
        None,
    )
    if coverage is None or coverage.get("rows") != report.get("coverage_ledger"):
        issues.append("PVR audit coverage rows do not match the report")
    return issues


def _pvr_report_accepted(verdict: object) -> bool:
    if not isinstance(verdict, dict):
        return False
    must = verdict.get("must_gates")
    should = verdict.get("should_gates")
    if not isinstance(must, list) or not isinstance(should, list) or not must:
        return False
    if not all(isinstance(gate, dict) and gate.get("passed") is True for gate in must):
        return False
    return all(
        isinstance(gate, dict)
        and isinstance(gate.get("score"), int | float)
        and isinstance(gate.get("threshold"), int | float)
        and gate["score"] >= gate["threshold"]
        for gate in should
    )


def _generic_validator_issues(receipt: ValidationReceipt, base: Path) -> list[str]:
    """Fail closed on PASS claims that lack a machine-readable validator report."""
    status = cast(str, receipt.result["status"])
    if status != "PASS":
        return []
    report_artifact = next((item for item in receipt.artifacts if item.kind == "report"), None)
    if report_artifact is None:
        return ["PASS requires a hash-bound validator report artifact"]
    try:
        raw_report = json.loads((base / _safe_relative_path(report_artifact.path)).read_text(encoding="utf-8"))
    except (ReceiptError, FileNotFoundError, json.JSONDecodeError) as exc:
        return [f"validator report is unreadable: {exc}"]
    if not isinstance(raw_report, dict):
        return ["validator report must be a JSON object"]
    required = {"schema_version", "exit_code"}
    command = receipt.exercise.get("command")
    if isinstance(command, list):
        required.add("command")
    if set(raw_report) != required or raw_report.get("schema_version") != "chitra-validator-report-v1":
        return ["validator report does not have the exact chitra-validator-report-v1 shape"]
    exit_code = raw_report.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return ["validator report exit_code must be an integer"]
    issues: list[str] = []
    if isinstance(command, list) and raw_report["command"] != command:
        issues.append("validator report command does not match the receipt exercise")
    if exit_code != 0:
        issues.append(f"validator report records failure (exit_code={exit_code}); it cannot support PASS")
    return issues


def verify_receipt_file(path: Path, *, verify_current_target: bool = True) -> ReceiptVerification:
    """Verify one stored or source receipt without trusting caller status text."""
    try:
        receipt, _raw = load_receipt_file(path)
    except ReceiptError as exc:
        return ReceiptVerification("", "invalid", False, False, (str(exc),), path)
    issues = _artifact_issues(receipt, path.parent)
    issues.extend(_validator_issues(receipt, path.parent))
    if verify_current_target:
        issues.extend(_target_issues(receipt))
    verified = not issues
    status = cast(str, receipt.result["status"])
    eligible = verified and status == "PASS" and receipt.result["validator_acceptance"] is True and not receipt.not_exercised
    return ReceiptVerification(receipt.receipt_name, status, verified, eligible, tuple(issues), path)


def verify_receipt(
    root: Path | None,
    session_ref: str,
    receipt_name: str,
    *,
    verify_current_target: bool = True,
) -> ReceiptVerification:
    return verify_receipt_file(
        receipt_path(root, session_ref, receipt_name),
        verify_current_target=verify_current_target,
    )


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as stream:
            temporary = stream.name
            with source.open("rb") as source_stream:
                shutil.copyfileobj(source_stream, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def ingest_receipt(root: Path | None, session_ref: str, source: Path) -> Path:
    """Verify, bind, and atomically store one immutable receipt for a lane."""
    from chitra.goals import get_goal

    goal = get_goal(root, session_ref)
    if goal is None:
        raise ReceiptError(f"goal not found: {session_ref}")
    receipt, raw = load_receipt_file(source)
    matching = [item for item in goal.enrolled_done_when_items if item.required_receipt == receipt.receipt_name]
    if not matching:
        raise ReceiptError(f"receipt {receipt.receipt_name!r} is not required by an enrolled done item")
    identifiers = _validator_identifiers(receipt)
    mismatched = [item.id for item in matching if item.validator not in identifiers]
    if mismatched:
        raise ReceiptError(f"receipt validator does not match enrolled item(s): {mismatched!r}")
    source_check = verify_receipt_file(source, verify_current_target=False)
    if not source_check.verified:
        raise ReceiptError("receipt evidence verification failed: " + "; ".join(source_check.issues))

    destination = receipt_path(root, session_ref, receipt.receipt_name)
    with locked_json_store(destination):
        if destination.exists():
            existing = _load_raw(destination)
            if existing != raw:
                raise ReceiptError(f"stored receipt {receipt.receipt_name!r} is immutable and differs from the source")
            stored_check = verify_receipt_file(destination, verify_current_target=False)
            if not stored_check.verified:
                raise ReceiptError("stored receipt verification failed: " + "; ".join(stored_check.issues))
            return destination
        for artifact in receipt.artifacts:
            relative = _safe_relative_path(artifact.path)
            stored_artifact = destination.parent / relative
            if stored_artifact.exists():
                if _hash_file(stored_artifact) != artifact.sha256:
                    raise ReceiptError(f"stored evidence path is immutable and has a different digest: {artifact.path!r}")
            else:
                _copy_file_atomic(source.parent / relative, stored_artifact)
        write_json_atomic(destination, raw, fsync=True)
        os.chmod(destination, 0o600)
        stored_check = verify_receipt_file(destination, verify_current_target=False)
        if not stored_check.verified:
            raise ReceiptError("stored receipt verification failed: " + "; ".join(stored_check.issues))
    return destination


def list_receipts(root: Path | None, session_ref: str) -> list[ValidationReceipt]:
    directory = receipts_root(root)
    receipts: list[ValidationReceipt] = []
    for path in sorted(directory.glob("*.json")):
        receipt, _raw = load_receipt_file(path)
        receipts.append(receipt)
    return receipts


def require_verified_completion_receipts(
    root: Path | None,
    session_ref: str,
    enrolled_items: Sequence[EnrolledDoneWhenItemLike],
    claimed_evidence: Sequence[CompletionEvidence],
) -> tuple[CompletionEvidence, ...]:
    """Return store-backed proofs or fail closed for any frozen item."""
    claim_issues = completion_receipt_issues(enrolled_items, claimed_evidence)
    if claim_issues:
        raise ReceiptError("; ".join(claim_issues))
    verified_proofs: list[CompletionEvidence] = []
    for item in enrolled_items:
        proof = next(
            candidate
            for candidate in claimed_evidence
            if candidate.done_when_item_id == item.id
            and candidate.receipt_name == item.required_receipt
            and candidate.validator == item.validator
            and candidate.validator_result == "pass"
        )
        path = receipt_path(root, session_ref, item.required_receipt)
        try:
            receipt, _raw = load_receipt_file(path)
        except ReceiptError as exc:
            raise ReceiptError(f"done item {item.id!r} receipt is unavailable: {exc}") from exc
        if item.validator not in _validator_identifiers(receipt):
            raise ReceiptError(f"done item {item.id!r} receipt validator does not match {item.validator!r}")
        verification = verify_receipt_file(path)
        if not verification.verified:
            raise ReceiptError(f"done item {item.id!r} receipt is not verified: {'; '.join(verification.issues)}")
        if not verification.completion_eligible:
            raise ReceiptError(
                f"done item {item.id!r} receipt status {verification.status!r} cannot close; only verified PASS receipts close"
            )
        verified_proofs.append(proof.model_copy(update={"kind": "artifact", "citation": str(path)}))
    return tuple(verified_proofs)

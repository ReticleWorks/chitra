"""Hash-bound validation receipts for frozen goal items.

Receipts are immutable, per-lane records below the instance state root.  The
common envelope deliberately carries validator-specific details inside its
nine fixed top-level fields.  Validator targets and evidence remain confined
to the explicit instance workspace supplied by the caller; receipt claims
never select an untrusted filesystem path for execution.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chitra._fsio import locked_json_store, parse_iso8601, write_json_atomic
from chitra.completion_gate import CompletionEvidence, EnrolledDoneWhenItemLike, completion_receipt_issues
from chitra.state_paths import state_dir
from chitra.validator_registry import (
    UNRUNNABLE_EXIT_CODE,
    RegisteredValidator,
    load_validators,
    run_registered_validator,
)

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


@dataclass(frozen=True, slots=True)
class TrustedValidator:
    """Chitra's own verifier implementation for one enrolled validator identity."""

    argv: tuple[str, ...]
    target_kind: Literal["artifact", "commit"]


_TRUSTED_VALIDATORS: dict[str, TrustedValidator] = {
    "pytest": TrustedValidator(("pytest",), "artifact"),
    "ruff": TrustedValidator(("ruff", "check"), "artifact"),
    "mypy": TrustedValidator(("mypy",), "artifact"),
}


def _trusted_verifier_argv(name: str, target_path: str) -> tuple[str, ...]:
    """Return the mapped verifier's fixed invocation over one exact target path.

    The enrolled validator identity alone selects the program and its fixed
    arguments; the only variable is the target path whose current content must
    match the receipt binding.  Chitra runs the verifier implementation it
    ships with, resolved through sys.executable, never whatever a PATH lookup
    would find first and never a caller-chosen program, flag, or target such
    as a presence-only `--version` check.
    """
    argv = _TRUSTED_VALIDATORS[name].argv
    return (sys.executable, "-m", argv[0], *argv[1:], target_path)


def _trusted_verifier_argv_or_none(name: str, target_path: str = "<target>") -> tuple[str, ...] | None:
    """Return the mapped verifier's invocation shape, or None when unmapped."""
    trusted = _TRUSTED_VALIDATORS.get(name)
    if trusted is None:
        return None
    argv = trusted.argv
    return (sys.executable, "-m", argv[0], *argv[1:], target_path)


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


def _session_receipts_root(root: Path | None, session_ref: str) -> Path:
    """Return the collision-free receipt directory for one exact goal session."""
    if not session_ref.strip():
        raise ReceiptError("session_ref must be non-empty")
    session_key = hashlib.sha256(session_ref.encode("utf-8")).hexdigest()
    return receipts_root(root) / session_key


def receipt_path(root: Path | None, session_ref: str, receipt_name: str) -> Path:
    if _SAFE_RECEIPT_NAME_RE.fullmatch(receipt_name) is None:
        raise ReceiptError("receipt_name must be a path-safe stable name")
    return _session_receipts_root(root, session_ref) / f"{receipt_name}.json"


def _legacy_receipt_path(root: Path | None, receipt_name: str) -> Path:
    """Return the pre-session-isolation receipt path.

    The old layout had one receipt namespace for the whole instance.  It is
    only a migration input; new writes always use :func:`receipt_path`.
    """
    return receipts_root(root) / f"{receipt_name}.json"


def _legacy_receipt_is_exactly_enrolled(root: Path | None, session_ref: str, receipt_name: str) -> bool:
    """Prove that one and only one current goal can own a legacy receipt.

    A legacy envelope has no session field.  Therefore a root-layout receipt
    is unsafe whenever another enrolled session names the same receipt.  We
    fail closed instead of guessing which session produced it.
    """
    from chitra.goals import list_goals

    owners = [
        goal.session_ref
        for goal in list_goals(root)
        if any(item.required_receipt == receipt_name for item in goal.enrolled_done_when_items)
    ]
    return owners == [session_ref]


def _fsync_directory(path: Path) -> None:
    """Durably publish a directory entry where the platform permits it."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _migrate_legacy_receipt(root: Path | None, session_ref: str, receipt_name: str) -> Path:
    """Move one unambiguous root-layout receipt into its session directory.

    The legacy file is locked while ownership is checked.  Receipt and
    evidence files are first assembled and verified in a temporary directory,
    then the complete directory is published with one ``os.replace``.  Any
    failure before publication leaves the legacy file untouched.
    """
    legacy = _legacy_receipt_path(root, receipt_name)
    destination = receipt_path(root, session_ref, receipt_name)
    with locked_json_store(legacy):
        if destination.exists():
            return destination
        if not legacy.exists():
            raise ReceiptError(f"receipt not found: {destination}")
        if not _legacy_receipt_is_exactly_enrolled(root, session_ref, receipt_name):
            raise ReceiptError(
                f"legacy receipt {receipt_name!r} is not uniquely bound to session {session_ref!r}; refusing migration"
            )
        receipt, raw = load_receipt_file(legacy)
        if receipt.receipt_name != receipt_name:
            raise ReceiptError("legacy receipt name does not match its path")
        source_check = verify_receipt_file(
            legacy,
            verify_current_target=False,
            approved_root=root if root is not None else state_dir(),
        )
        if not source_check.verified:
            raise ReceiptError("legacy receipt evidence verification failed: " + "; ".join(source_check.issues))

        # Stage beside, rather than inside, the final session directory.  The
        # directory rename below then publishes the receipt and all evidence
        # as one filesystem operation.
        receipts_root_path = receipts_root(root)
        receipts_root_path.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.migration-", dir=receipts_root_path))
        published = False
        try:
            for artifact in receipt.artifacts:
                relative = _safe_relative_path(artifact.path)
                _copy_file_atomic(legacy.parent / relative, staging / relative)
            staged_receipt = staging / destination.name
            write_json_atomic(staged_receipt, raw, fsync=True)
            os.chmod(staged_receipt, 0o600)
            staged_check = verify_receipt_file(
                staged_receipt,
                verify_current_target=False,
                approved_root=root if root is not None else state_dir(),
            )
            if not staged_check.verified:
                raise ReceiptError("migrated receipt verification failed: " + "; ".join(staged_check.issues))
            _fsync_directory(staging)
            if destination.exists():
                existing = _load_raw(destination)
                if existing != raw:
                    raise ReceiptError(f"session receipt {receipt_name!r} already exists and differs from legacy receipt")
            elif not destination.parent.exists():
                try:
                    os.replace(staging, destination.parent)
                except OSError:
                    # Another receipt for this same session may have created
                    # the hashed directory after the existence check. Merge
                    # into that directory below, but do not hide any other
                    # rename failure.
                    if not destination.parent.is_dir():
                        raise
                else:
                    published = True

            if not published and not destination.exists():
                # A session directory can already contain other canonical
                # receipts. In that case publish each immutable artifact
                # atomically, then publish the receipt envelope last as the
                # commit marker. A crash can leave an unreferenced artifact,
                # but never a readable receipt with incomplete evidence.
                destination.parent.mkdir(parents=True, exist_ok=True)
                for artifact in receipt.artifacts:
                    relative = _safe_relative_path(artifact.path)
                    staged_artifact = staging / relative
                    stored_artifact = destination.parent / relative
                    if stored_artifact.exists():
                        if _hash_file(stored_artifact) != artifact.sha256:
                            raise ReceiptError(
                                f"stored evidence path is immutable and has a different digest: {artifact.path!r}"
                            )
                    else:
                        _copy_file_atomic(staged_artifact, stored_artifact)
                write_json_atomic(destination, raw, fsync=True)
                os.chmod(destination, 0o600)

            destination_check = verify_receipt_file(
                destination,
                verify_current_target=False,
                approved_root=root if root is not None else state_dir(),
            )
            if not destination_check.verified:
                raise ReceiptError(
                    "published migrated receipt verification failed: " + "; ".join(destination_check.issues)
                )
            _fsync_directory(destination.parent)
            # Removing only the old receipt after publication makes a crash
            # safe: a duplicate is harmless, while data loss is impossible.
            legacy.unlink()
            _fsync_directory(legacy.parent)
            return destination
        except OSError as exc:
            raise ReceiptError(f"legacy receipt migration failed: {exc}") from exc
        finally:
            if not published:
                shutil.rmtree(staging, ignore_errors=True)


def _receipt_path_for_read(root: Path | None, session_ref: str, receipt_name: str) -> Path:
    """Resolve the canonical path, migrating one safe legacy receipt if needed."""
    destination = receipt_path(root, session_ref, receipt_name)
    if destination.exists():
        return destination
    legacy = _legacy_receipt_path(root, receipt_name)
    if not legacy.exists():
        return destination
    return _migrate_legacy_receipt(root, session_ref, receipt_name)


def receipt_name(value: str) -> str:
    """Validate a receipt name before any path or file is derived from it."""
    if _SAFE_RECEIPT_NAME_RE.fullmatch(value) is None:
        raise ReceiptError("receipt_name must be a path-safe stable name")
    return value


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


def _approved_root_path(root: Path) -> Path:
    """Resolve the explicit workspace root used for receipt path checks.

    Receipt verification has no portable, safe filesystem sandbox.  The
    approved state root is therefore the boundary: every target and evidence
    path must stay beneath it, and no path component may be a symlink.  A
    missing or non-directory root is not an implicit permission to use the
    process working directory; it is a verification failure.
    """
    if not root.is_absolute():
        raise ReceiptError(f"approved receipt workspace must be absolute: {root}")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReceiptError(f"approved receipt workspace is unreadable: {exc}") from exc
    if not resolved.is_dir():
        raise ReceiptError(f"approved receipt workspace is not a directory: {root}")
    return resolved


def _confined_path(path: Path, approved_root: Path | None, *, label: str) -> Path:
    """Return ``path`` only when it is a real, non-symlink child of ``root``.

    The check is deliberately lexical *and* resolved.  Lexical confinement
    rejects absolute aliases and ``..`` tricks; walking the components and
    resolving the complete path rejects symlink escapes (including a symlink
    that points back inside the workspace).  Callers must pass an explicit
    root.  Without one, running a receipt's validator would turn its claimed
    path into authority, so verification fails closed before subprocess use.
    """
    if approved_root is None:
        raise ReceiptError(f"{label} has no approved workspace root; refusing to verify its path")
    root = _approved_root_path(approved_root)
    if not path.is_absolute():
        raise ReceiptError(f"{label} path must be absolute")
    if any(part in ("", ".", "..") for part in path.parts):
        raise ReceiptError(f"{label} path contains traversal components")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ReceiptError(f"{label} path is outside the approved workspace: {path}") from exc

    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ReceiptError(f"{label} path contains a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReceiptError(f"{label} path is unreadable: {exc}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReceiptError(f"{label} path escapes the approved workspace: {path}") from exc
    return path


def _artifact_root(base: Path, approved_root: Path | None) -> Path:
    """Choose the explicit container for receipt-relative evidence files.

    Stored receipts live below the approved state root.  An ingest source may
    instead be an external upload directory; that directory is a transport
    container, not a validator registry or target-execution authority.  Keep
    its evidence confined to itself, including symlink checks, until ingest
    copies it into the state root.
    """
    if approved_root is None:
        return base
    try:
        resolved_base = base.resolve(strict=True)
        resolved_root = _approved_root_path(approved_root)
        resolved_base.relative_to(resolved_root)
    except (OSError, RuntimeError, ReceiptError, ValueError):
        return base
    return approved_root


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_issues(receipt: ValidationReceipt, base: Path, approved_root: Path | None) -> list[str]:
    issues: list[str] = []
    evidence_root = _artifact_root(base, approved_root)
    for artifact in receipt.artifacts:
        try:
            path = _confined_path(
                base / _safe_relative_path(artifact.path),
                evidence_root,
                label=f"artifact {artifact.path!r}",
            )
            actual = _hash_file(path)
        except (FileNotFoundError, IsADirectoryError, PermissionError, ReceiptError) as exc:
            issues.append(f"artifact {artifact.path!r} is not readable: {exc}")
            continue
        if not hmac.compare_digest(actual, artifact.sha256):
            issues.append(f"artifact {artifact.path!r} digest mismatch")
    return issues


def _target_issues(receipt: ValidationReceipt, base: Path, approved_root: Path | None) -> list[str]:
    issues: list[str] = []
    # Registered validators execute only their operator-provisioned exact
    # argv, which may name an external repository. The receipt target never
    # selects that command, but its own digest remains evidence and must still
    # match the current approved target so an edited stored receipt fails.
    if "artifact" in receipt.target:
        target = _object(receipt.target["artifact"], name="target.artifact")
        path = Path(_text(target, "path", parent="target.artifact"))
        expected = _text(target, "sha256", parent="target.artifact")
        try:
            path = _confined_path(path, approved_root, label="target.artifact")
            actual = _hash_file(path)
        except (FileNotFoundError, IsADirectoryError, PermissionError, ReceiptError) as exc:
            return [f"current target artifact is not readable: {exc}"]
        if not hmac.compare_digest(actual, expected):
            issues.append("current target artifact digest does not match the receipt")
        return issues

    target = _object(receipt.target["commit"], name="target.commit")
    repository = Path(_text(target, "repository", parent="target.commit"))
    expected = _text(target, "sha", parent="target.commit")
    try:
        repository = _confined_path(repository, approved_root, label="target.commit repository")
    except ReceiptError as exc:
        return [f"current target commit is not readable: {exc}"]
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


def _validator_issues(receipt: ValidationReceipt, base: Path, approved_root: Path | None) -> list[str]:
    name = _text(receipt.validator, "name", parent="validator")
    if not name.startswith("Polyvalidation Rig"):
        return _generic_validator_issues(receipt, base, approved_root)
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
        report_path_on_disk = _confined_path(
            base / _safe_relative_path(report_path),
            _artifact_root(base, approved_root),
            label=f"validator report {report_path!r}",
        )
        report = _load_raw(report_path_on_disk)
    except (ReceiptError, OSError) as exc:
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
        try:
            audit_path = _confined_path(
                base / _safe_relative_path(audit.path),
                _artifact_root(base, approved_root),
                label=f"validator audit {audit.path!r}",
            )
        except ReceiptError as exc:
            issues.append(str(exc))
        else:
            issues.extend(_pvr_audit_issues(audit_path, report, run_meta))

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


def _receipt_target_artifact_path(receipt: ValidationReceipt) -> str:
    return _text(_object(receipt.target["artifact"], name="target.artifact"), "path", parent="target.artifact")


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


def _generic_validator_issues(receipt: ValidationReceipt, base: Path, approved_root: Path | None) -> list[str]:
    """Fail closed on PASS claims that lack a trusted validator execution.

    The frozen validator identity selects Chitra's own trusted verifier; the
    caller-authored exercise and report are evidence inputs only and never
    choose the program whose result establishes PASS.  An identity with no
    mapped trusted verifier fails closed.
    """
    status = cast(str, receipt.result["status"])
    if status != "PASS":
        return []
    command = receipt.exercise.get("command")
    if not isinstance(command, list):
        return ["PASS with a live_boundary exercise cannot be verified by the generic validator path"]
    report_artifact = next((item for item in receipt.artifacts if item.kind == "report"), None)
    if report_artifact is None:
        return ["PASS requires a hash-bound validator report artifact"]
    try:
        report_path = _confined_path(
            base / _safe_relative_path(report_artifact.path),
            _artifact_root(base, approved_root),
            label=f"validator report {report_artifact.path!r}",
        )
        raw_report = json.loads(report_path.read_text(encoding="utf-8"))
    except (ReceiptError, FileNotFoundError, IsADirectoryError, PermissionError, json.JSONDecodeError) as exc:
        return [f"validator report is unreadable: {exc}"]
    if not isinstance(raw_report, dict):
        return ["validator report must be a JSON object"]
    required = {"schema_version", "command", "exit_code"}
    if set(raw_report) != required or raw_report.get("schema_version") != "chitra-validator-report-v1":
        return ["validator report does not have the exact chitra-validator-report-v1 shape"]
    exit_code = raw_report.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return ["validator report exit_code must be an integer"]
    issues: list[str] = []
    if raw_report["command"] != command:
        issues.append("validator report command does not match the receipt exercise")
    issues.extend(_validator_binding_issues(receipt, base, approved_root))
    actual_exit_code = _trusted_validator_result(receipt, base, approved_root)
    if actual_exit_code != exit_code:
        issues.append(
            f"trusted re-execution of the receipt exercise exited {actual_exit_code}; "
            f"the caller-authored report claims exit_code={exit_code}"
        )
    elif exit_code != 0:
        issues.append(f"validator report records failure (exit_code={exit_code}); it cannot support PASS")
    return issues


def _base_of(receipt: ValidationReceipt) -> Path:
    """Return the receipt directory implied by the receipt's own target path."""
    return Path(_receipt_target_artifact_path(receipt)).parent


def _registered_validator_entry(
    name: str,
    base: Path,
    approved_root: Path | None = None,
) -> RegisteredValidator | None:
    """Resolve a validator from the approved instance root.

    Source receipts may live in an upload directory that contains an
    attacker-controlled ``validators.json``.  Once verification supplies its
    approved root, only that root may select a registered command.  The
    canonical-layout fallback is retained for private callers without an
    explicit root.
    """
    registry_root = approved_root if approved_root is not None else _state_root_for_receipt_dir(base)
    return load_validators(registry_root).get(name)


def _state_root_for_receipt_dir(base: Path) -> Path:
    """Return the state root that owns a receipt directory."""
    if base.parent.name == "validation-receipts":
        return base.parent.parent
    return base.parent if base.name == "validation-receipts" else base


def _validator_binding_issues(
    receipt: ValidationReceipt,
    base: Path | None = None,
    approved_root: Path | None = None,
) -> list[str]:
    """Reject a declared exercise that is not bound to this validator identity.

    ``base`` is the receipt directory when the caller knows it; without it the
    registry resolves from the directory implied by the receipt's own bound
    artifact path.
    """
    name = _text(receipt.validator, "name", parent="validator")
    command = cast(list[str], receipt.exercise["command"])
    if "artifact" not in receipt.target:
        return [f"{name}'s trusted verifier executes an exact artifact target; this receipt declares no artifact target"]
    resolved_base = base if base is not None else _base_of(receipt)
    registered = _registered_validator_entry(name, resolved_base, approved_root)
    expected = (
        tuple(registered.argv)
        if registered is not None
        else _trusted_verifier_argv_or_none(name, _receipt_target_artifact_path(receipt))
    )
    if expected is None:
        return [f"validator {name!r} has no trusted verifier invocation; its exercise cannot establish a PASS"]
    if tuple(command) != expected:
        return [
            f"exercise.command {command!r} is not {name}'s trusted verifier invocation "
            f"{list(expected)!r}; a caller-selected program or target cannot establish its PASS"
        ]
    return []


def _trusted_validator_target_issues(receipt: ValidationReceipt, approved_root: Path | None) -> list[str]:
    """Require the exact current target identity to match the receipt binding."""
    name = _text(receipt.validator, "name", parent="validator")
    trusted = _TRUSTED_VALIDATORS[name]
    kind = "commit" if "commit" in receipt.target else "artifact"
    if kind != trusted.target_kind:
        return [
            f"{name}'s trusted verifier binds a {trusted.target_kind} target; "
            f"this receipt declares a {kind} target"
        ]
    digest = _receipt_target_digest(receipt)
    repository = Path(
        _text(
            _object(receipt.target[kind], name=f"target.{kind}"),
            "repository" if kind == "commit" else "path",
            parent=f"target.{kind}",
        )
    )
    try:
        repository = _confined_path(repository, approved_root, label=f"{name} target")
        current_digest = (
            subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                timeout=60.0,
            ).stdout.strip()
            if kind == "commit"
            else _hash_file(repository)
        )
    except (OSError, subprocess.SubprocessError, FileNotFoundError, IsADirectoryError, PermissionError, ReceiptError) as exc:
        return [f"current target {kind} is unreadable: {exc}"]
    if not hmac.compare_digest(current_digest, digest):
        return [f"current target {kind} does not match the receipt"]
    return []


def _trusted_validator_result(receipt: ValidationReceipt, base: Path, approved_root: Path | None) -> int:
    """Run the frozen identity's trusted verifier against the exact current target.

    The enrolled validator name, not the receipt author, selects the program,
    its fixed arguments, and the target: after the declared target's current
    identity is hash-checked, Chitra's mapped verifier executes that exact
    target file, and its observed result is what supports the claimed PASS.  A
    presence-only or caller-authored exercise cannot stand in for it.  Any
    unmapped generic validator identity, unreadable or changed target, unbound
    exercise, or failing verification counts as failure to verify (125).
    """
    name = _text(receipt.validator, "name", parent="validator")
    trusted = _TRUSTED_VALIDATORS.get(name)
    registered = _registered_validator_entry(name, base, approved_root)
    if trusted is None and registered is None:
        return 125
    if _validator_binding_issues(receipt, base, approved_root):
        return 125
    if registered is not None:
        exit_code, output = run_registered_validator(registered)
        del output
        return exit_code
    try:
        target_path = _confined_path(
            Path(_receipt_target_artifact_path(receipt)),
            approved_root,
            label="target.artifact",
        )
    except ReceiptError:
        return 125
    if _trusted_validator_target_issues(receipt, approved_root):
        return 125
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in ("LANG", "LC_ALL", "TMPDIR")
    }
    try:
        completed = subprocess.run(
            [*_trusted_verifier_argv(name, str(target_path))],
            check=False,
            capture_output=True,
            cwd=base,
            env=environment,
            timeout=120.0,
        )
    except (OSError, subprocess.SubprocessError):
        return 125
    return completed.returncode


def _infer_approved_root(path: Path) -> Path | None:
    """Infer the state root only from the canonical stored receipt layout."""
    parent = path.parent
    if parent.parent.name != "validation-receipts" or len(parent.name) != 64:
        return None
    return parent.parent.parent


def verify_receipt_file(
    path: Path,
    *,
    verify_current_target: bool = True,
    approved_root: Path | None = None,
) -> ReceiptVerification:
    """Verify one stored or source receipt without trusting caller status text."""
    root = approved_root if approved_root is not None else _infer_approved_root(path)
    try:
        receipt, _raw = load_receipt_file(path)
    except ReceiptError as exc:
        return ReceiptVerification("", "invalid", False, False, (str(exc),), path)
    issues = _artifact_issues(receipt, path.parent, root)
    issues.extend(_validator_issues(receipt, path.parent, root))
    if verify_current_target:
        issues.extend(_target_issues(receipt, path.parent, root))
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
    path = receipt_path(root, session_ref, receipt_name)
    try:
        path = _receipt_path_for_read(root, session_ref, receipt_name)
    except ReceiptError as exc:
        return ReceiptVerification("", "invalid", False, False, (str(exc),), path)
    return verify_receipt_file(path, verify_current_target=verify_current_target, approved_root=root)


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
    source_check = verify_receipt_file(source, verify_current_target=False, approved_root=root or state_dir())
    if not source_check.verified:
        raise ReceiptError("receipt evidence verification failed: " + "; ".join(source_check.issues))

    destination = receipt_path(root, session_ref, receipt.receipt_name)
    with locked_json_store(destination):
        if destination.exists():
            existing = _load_raw(destination)
            if existing != raw:
                raise ReceiptError(f"stored receipt {receipt.receipt_name!r} is immutable and differs from the source")
            stored_check = verify_receipt_file(destination, verify_current_target=False, approved_root=root or state_dir())
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
        stored_check = verify_receipt_file(destination, verify_current_target=False, approved_root=root or state_dir())
        if not stored_check.verified:
            raise ReceiptError("stored receipt verification failed: " + "; ".join(stored_check.issues))
    return destination


def list_receipts(root: Path | None, session_ref: str) -> list[ValidationReceipt]:
    directory = _session_receipts_root(root, session_ref)
    from chitra.goals import get_goal

    goal = get_goal(root, session_ref)
    required_names = () if goal is None else tuple(item.required_receipt for item in goal.enrolled_done_when_items)
    for name in required_names:
        _receipt_path_for_read(root, session_ref, name)
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
        path = _receipt_path_for_read(root, session_ref, item.required_receipt)
        try:
            receipt, _raw = load_receipt_file(path)
        except ReceiptError as exc:
            raise ReceiptError(f"done item {item.id!r} receipt is unavailable: {exc}") from exc
        if item.validator not in _validator_identifiers(receipt):
            raise ReceiptError(f"done item {item.id!r} receipt validator does not match {item.validator!r}")
        verification = verify_receipt_file(path, approved_root=root or state_dir())
        if not verification.verified:
            raise ReceiptError(f"done item {item.id!r} receipt is not verified: {'; '.join(verification.issues)}")
        if not verification.completion_eligible:
            raise ReceiptError(
                f"done item {item.id!r} receipt status {verification.status!r} cannot close; only verified PASS receipts close"
            )
        verified_proofs.append(proof.model_copy(update={"kind": "artifact", "citation": str(path)}))
    return tuple(verified_proofs)


def _write_text_atomic(path: Path, content: str) -> None:
    """Atomically replace ``path`` with ``content`` through a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def record_registered_run(
    root: Path | None,
    session_ref: str,
    item: EnrolledDoneWhenItemLike,
    entry: RegisteredValidator | None,
    *,
    produced_at: str | None = None,
    output: str | None = None,
) -> CompletionEvidence:
    """Run one registered validator, store its receipt, and return its proof.

    Chitra — never the lane — executes the registry argv and writes the W12
    envelope below the exact session's receipt directory with the observed
    exit code as the result. A rerun overwrites both the evidence artifacts
    and the receipt atomically; the newest execution is the proof.
    A missing registry entry (``entry=None``) fails closed as exit 125 so an
    enrolled item whose validator later vanishes still leaves a stored FAIL.
    """
    receipt_name(item.required_receipt)
    if entry is None:
        exit_code, output = UNRUNNABLE_EXIT_CODE, (f"registered validator {item.validator!r} is not in this instance's validators.json")
    else:
        exit_code, output = run_registered_validator(entry)
    destination = receipt_path(root, session_ref, item.required_receipt)
    directory = destination.parent
    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / f"{item.required_receipt}.output.log"
    report_path = directory / f"{item.required_receipt}.report.json"
    _write_text_atomic(output_path, output + ("\n" if output else ""))
    report = {
        "schema_version": "chitra-validator-report-v1",
        "command": list(entry.argv) if entry is not None else [],
        "exit_code": exit_code,
    }
    _write_text_atomic(report_path, json.dumps(report, sort_keys=True))
    status = "PASS" if exit_code == 0 else "FAIL"
    payload: dict[str, object] = {
        "receipt_name": item.required_receipt,
        "validator": {
            "name": item.validator,
            "version": "registered" if entry is not None else "unregistered",
            **({"runs_as": entry.runs_as} if entry is not None else {}),
        },
        "target": {
            "artifact": {
                "path": str(output_path),
                "sha256": _hash_file(output_path),
            }
        },
        "exercise": {"command": list(entry.argv) if entry is not None else [f"<unregistered:{item.validator}>"]},
        "result": {"status": status, "validator_acceptance": exit_code == 0},
        "not_exercised": [],
        "artifacts": [
            {
                "path": output_path.name,
                "kind": "output",
                "sha256": _hash_file(output_path),
            },
            {
                "path": report_path.name,
                "kind": "report",
                "sha256": _hash_file(report_path),
            },
        ],
        "produced_at": produced_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": _CANONICALIZATION,
            "scope": _INTEGRITY_SCOPE,
            "hand_authored_fields": [],
        },
    }
    integrity = cast("dict[str, object]", payload["integrity"])
    integrity["digest"] = _canonical_digest(payload)
    with locked_json_store(destination):
        write_json_atomic(destination, payload, fsync=True)
        os.chmod(destination, 0o600)
    return CompletionEvidence(
        kind="artifact",
        done_when_item_id=item.id,
        receipt_name=item.required_receipt,
        validator=item.validator,
        validator_result="pass" if exit_code == 0 else "fail",
        citation=str(destination),
    )


def record_enrolled_validator_runs(
    root: Path | None,
    session_ref: str,
    items: Sequence[EnrolledDoneWhenItemLike],
) -> tuple[CompletionEvidence, ...]:
    """Execute every enrolled item's registered validator and store receipts.

    An item whose validator is not a registry key gets the fail-closed exit
    125 result stored on disk, so it can never pass and every enrolled item
    leaves an explicit receipt explaining why completion stays disputed.
    """
    registry = load_validators(root)
    return tuple(record_registered_run(root, session_ref, item, registry.get(item.validator)) for item in items)


def verified_disk_results(
    root: Path | None,
    session_ref: str,
    items: Sequence[EnrolledDoneWhenItemLike],
) -> dict[str, str]:
    """Read every enrolled item's receipt back and return integrity-checked results.

    The completion gate consumes this map instead of the runner's in-memory
    proofs: a receipt counts as ``pass`` only when its stored file loads,
    self-verifies, re-executes cleanly, and holds a PASS result. Anything
    missing, forged, stale, or failing closes as ``fail``.
    """
    results: dict[str, str] = {}
    for item in items:
        try:
            eligible = verify_receipt(root, session_ref, item.required_receipt).completion_eligible
        except ReceiptError:
            eligible = False
        results[item.required_receipt] = "pass" if eligible else "fail"
    return results

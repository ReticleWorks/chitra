"""Small, restart-safe close seam owned by Chitra.

The provider sees a close request and an opaque checkpoint reference. Chitra
keeps the signed checkpoint and its signing key on its own host. The provider
never reads Chitra's checkpoint file or key; it only returns evidence bound to
the exact operation and physical session supplied in the request.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from ._fsio import write_json_atomic
from .detect.rescue import load_or_create_checkpoint_key, sign_checkpoint_receipt, verify_checkpoint_receipt_signature
from .goals import get_goal
from .provider_protocol import Provider
from .recovery import (
    GovernedCloseDecision,
    RecoveryEngine,
    RecoveryStateError,
    RecoveryStateStore,
    _close_operation,
    _close_receipt_matches,
    _close_session,
    _same_close_operation,
)
from .session_contract import (
    CloseArchiveResult,
    ContractValidationError,
    JoinedLaneRecord,
    PendingProviderOperation,
)

_CHECKPOINT_SCHEMA = "chitra.governed-close-checkpoint.v1"
_CHECKPOINT_PROVENANCE = "governed-completion-checkpoint"
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _now(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None:
        raise ValueError("close timestamps must include a timezone")
    return current.astimezone(UTC)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _expected_session(record: JoinedLaneRecord) -> str:
    return _close_session(record)


def _checkpoint_path(state_root: Path, reference: str) -> Path | None:
    if _REFERENCE_RE.fullmatch(reference) is None:
        return None
    root = state_root.resolve()
    directory = (root / "checkpoints").resolve()
    path = (directory / f"{reference}.json").resolve()
    return path if directory.parent == root and path.parent == directory else None


def _checkpoint_binding(record: JoinedLaneRecord) -> dict[str, object]:
    provider = record.provider
    if provider.instance_id is None or provider.generation is None:
        raise RecoveryStateError("governed close requires a complete provider identity")
    return {
        "kind": str(provider.kind),
        "handle": provider.handle,
        "provider_session_id": _expected_session(record),
        "instance_id": provider.instance_id,
        "generation": provider.generation,
    }


def _checkpoint_payload(record: JoinedLaneRecord, reference: str) -> dict[str, object]:
    return {
        "schema_name": _CHECKPOINT_SCHEMA,
        "schema_version": 1,
        "checkpoint_ref": reference,
        "lane": record.lane_id,
        "goal_id": record.goal_id,
        "goal_version": record.goal_version,
        "session_ref": record.session_ref,
        "provider_binding": _checkpoint_binding(record),
        "provenance": {"kind": _CHECKPOINT_PROVENANCE, "owner": "chitra"},
    }


def _checkpoint_matches(
    payload: Mapping[str, object], record: JoinedLaneRecord, reference: str, state_root: Path
) -> bool:
    try:
        if not verify_checkpoint_receipt_signature(dict(payload), state_root=state_root):
            return False
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    if payload.get("checkpoint_ref") != reference:
        return False
    if payload.get("lane") != record.lane_id or payload.get("goal_id") not in (None, record.goal_id):
        return False
    if payload.get("goal_version") != record.goal_version or payload.get("session_ref") != record.session_ref:
        return False
    binding = payload.get("provider_binding")
    if isinstance(binding, Mapping):
        return dict(binding) == _checkpoint_binding(record)
    # Accept an older Chitra rescue receipt, but never infer identity from a
    # provider handle. Its binding is checked field by field below.
    recovery = payload.get("recovery_binding")
    expected = _checkpoint_binding(record)
    return (
        isinstance(recovery, Mapping)
        and recovery.get("goal_id") == record.goal_id
        and recovery.get("goal_version") == record.goal_version
        and all(
            recovery.get(source) == expected[target]
            for source, target in (
                ("provider_handle", "handle"),
                ("provider_session_id", "provider_session_id"),
                ("provider_instance_id", "instance_id"),
                ("provider_generation", "generation"),
            )
        )
    )


def _read_checkpoint(state_root: Path, record: JoinedLaneRecord, reference: str) -> bool:
    path = _checkpoint_path(state_root, reference)
    if path is None:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    # Keep the state-root argument explicit. It prevents a provider or a
    # remote host from ever becoming the source of checkpoint verification.
    return _checkpoint_matches(payload, record, reference, state_root)


def _write_checkpoint(state_root: Path, record: JoinedLaneRecord) -> str:
    unsigned = _checkpoint_payload(record, "")
    reference = "completion-" + _digest(unsigned)[:32]
    payload = _checkpoint_payload(record, reference)
    path = _checkpoint_path(state_root, reference)
    if path is None:
        raise RecoveryStateError("governed close generated an unsafe checkpoint reference")
    if path.exists():
        if not _read_checkpoint(state_root, record, reference):
            raise RecoveryStateError("existing completion checkpoint binding changed")
        return reference
    signed = dict(payload)
    signed["signature"] = sign_checkpoint_receipt(signed, key=load_or_create_checkpoint_key(state_root))
    write_json_atomic(path, signed, fsync=True)
    if not _read_checkpoint(state_root, record, reference):
        raise RecoveryStateError("new completion checkpoint failed local verification")
    return reference


def _ensure_checkpoint(state_root: Path, record: JoinedLaneRecord) -> JoinedLaneRecord:
    reference = record.checkpoint_reference
    if reference:
        if not _read_checkpoint(state_root, record, reference):
            raise RecoveryStateError("existing Chitra checkpoint is missing or not bound to this lane")
        return record
    return record.model_copy(update={"checkpoint_reference": _write_checkpoint(state_root, record)})


def _close_payload(record: JoinedLaneRecord) -> str:
    return _json(
        {
            "archive": True,
            "checkpoint_ref": record.checkpoint_reference,
            "lane_id": record.lane_id,
            "goal_id": record.goal_id,
            "goal_version": record.goal_version,
            "session_ref": record.session_ref,
            "provider_handle": record.provider.handle,
            "provider_session_id": _expected_session(record),
            "provider_instance_id": record.provider.instance_id,
            "provider_generation": record.provider.generation,
        }
    )


def _operation(record: JoinedLaneRecord, payload: str, now: datetime) -> PendingProviderOperation:
    return _close_operation(record, payload, now)


def _operation_matches(actual: PendingProviderOperation, expected: PendingProviderOperation) -> bool:
    return _same_close_operation(actual, expected)


def _close_evidence_path(state_root: Path, operation: PendingProviderOperation) -> Path:
    if _REFERENCE_RE.fullmatch(operation.operation_id) is None:
        raise RecoveryStateError("close operation ID is unsafe for evidence storage")
    return state_root / "close-evidence" / f"{operation.operation_id}.json"


def _read_close_evidence(
    state_root: Path,
    operation: PendingProviderOperation,
    record: JoinedLaneRecord,
) -> CloseArchiveResult | None:
    path = _close_evidence_path(state_root, operation)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping) or payload.get("schema") != "chitra.governed-close-evidence.v1":
        return None
    if payload.get("operation") != operation.model_dump(mode="json"):
        return None
    values = payload.get("result")
    try:
        result = CloseArchiveResult.from_dict(values)
    except (ContractValidationError, TypeError, ValueError):
        return None
    if not _close_receipt_matches(record, operation, result):
        return None
    return result


def _write_close_evidence(
    state_root: Path,
    operation: PendingProviderOperation,
    result: CloseArchiveResult,
) -> None:
    path = _close_evidence_path(state_root, operation)
    payload = {
        "schema": "chitra.governed-close-evidence.v1",
        "operation": operation.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
    }
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise RecoveryStateError("immutable close evidence changed")
        return
    write_json_atomic(path, payload, fsync=True)


def _wait(
    record: JoinedLaneRecord,
    reason: str,
    operation: PendingProviderOperation | None = None,
    result: CloseArchiveResult | None = None,
) -> GovernedCloseDecision:
    return GovernedCloseDecision(action="waiting", record=record, reason=reason, operation=operation, close_result=result)


def governed_close(
    *,
    provider: Provider | None,
    state_root: Path | None,
    goal_root: Path | None = None,
    record: JoinedLaneRecord | None = None,
    lane_id: str | None = None,
    now: datetime | None = None,
    persist: bool = True,
    state_store: RecoveryStateStore | None = None,
) -> GovernedCloseDecision:
    """Compatibility facade; RecoveryEngine owns close state transitions."""

    return RecoveryEngine(
        provider=provider,
        state_root=state_root,
        state_store=state_store,
        goal_root=goal_root,
    ).governed_close(record, lane_id=lane_id, now=now, persist=persist)


__all__ = ["governed_close"]

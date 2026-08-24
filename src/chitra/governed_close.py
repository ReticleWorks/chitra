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
from typing import Any, cast

from ._fsio import write_json_atomic
from .detect.rescue import load_or_create_checkpoint_key, sign_checkpoint_receipt, verify_checkpoint_receipt_signature
from .goals import get_goal
from .provider_protocol import CloseRequest, Provider, ProviderState, ProviderStatus
from .recovery import GovernedCloseDecision, RecoveryStateError, RecoveryStateStore
from .session_contract import (
    CloseArchiveResult,
    ContractValidationError,
    JoinedLaneRecord,
    OperationReference,
    PendingProviderOperation,
    validate_close_result,
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


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    for method_name in ("model_dump", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                dumped = method(mode="json") if method_name == "model_dump" else method()
            except TypeError:
                dumped = method()
            if isinstance(dumped, Mapping):
                return dumped
    raise ValueError(f"{name} must be an object")


def _capabilities(provider: object) -> Mapping[str, object]:
    raw = getattr(provider, "capabilities", {})
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    return raw if isinstance(raw, Mapping) else {}


def _supports(provider: object, name: str) -> bool:
    raw = _capabilities(provider)
    return raw.get(name) is True or bool(getattr(raw, name, False))


def _provider_kind(provider: object) -> str:
    value = getattr(provider, "provider_name", "")
    return str(getattr(value, "value", value))


def _expected_session(record: JoinedLaneRecord) -> str:
    value = record.provider.provider_session_id
    if not value:
        raise RecoveryStateError("governed close requires an exact physical provider session ID")
    return value


def _status(provider: Provider, record: JoinedLaneRecord) -> ProviderStatus | None:
    try:
        raw = provider.status()
        if isinstance(raw, ProviderStatus):
            return raw
        values = _mapping(raw, "provider status")
        state = ProviderState(str(values.get("state", "unknown")))
        generation = values.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        return ProviderStatus(
            provider=str(values.get("provider", record.provider.kind)),
            state=state,
            provider_session_id=cast(str | None, values.get("provider_session_id")),
            generation=generation,
            fresh=values.get("fresh") is True,
            provider_available=values.get("provider_available") is True,
            context_available=cast(bool | None, values.get("context_available")),
            current_turn_id=cast(str | None, values.get("current_turn_id")),
            last_event_id=cast(str | None, values.get("last_event_id")),
            reason=str(values.get("reason", "")),
        )
    except (AttributeError, TypeError, ValueError):
        return None


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
    provider = record.provider
    if provider.instance_id is None or provider.generation is None:
        raise RecoveryStateError("close operation lacks a complete provider identity")
    identity = {
        "lane_id": record.lane_id,
        "goal_id": record.goal_id,
        "goal_version": record.goal_version,
        "session_ref": record.session_ref,
        "provider_handle": provider.handle,
        "provider_session_id": _expected_session(record),
        "provider_instance_id": provider.instance_id,
        "provider_generation": provider.generation,
        "payload": payload,
    }
    operation_id = "close-" + _digest(identity)[:32]
    return PendingProviderOperation(
        operation_id=operation_id,
        kind="close",
        lane_id=record.lane_id,
        provider_handle=provider.handle,
        provider_session_id=_expected_session(record),
        idempotency_key=f"{operation_id}-idem",
        payload_digest=_digest(identity),
        payload=payload,
        provider_instance_id=provider.instance_id,
        provider_generation=provider.generation,
        created_at=now.isoformat(),
    )


def _operation_matches(actual: PendingProviderOperation, expected: PendingProviderOperation) -> bool:
    # ``created_at`` is the first durable attempt's timestamp. A restart must
    # compare the immutable envelope, not manufacture a new operation because
    # the retry happened at a later time.
    fields = (
        "operation_id",
        "kind",
        "lane_id",
        "provider_handle",
        "provider_session_id",
        "idempotency_key",
        "payload_digest",
        "payload",
        "provider_instance_id",
        "provider_generation",
    )
    return all(getattr(actual, field) == getattr(expected, field) for field in fields)


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
        validate_close_result(operation, result)
    except (ContractValidationError, TypeError, ValueError):
        return None
    if (
        result.state not in {"closed", "archived"}
        or result.provider_thread_ref != record.provider.handle
        or result.provider_session_id != operation.provider_session_id
        or result.same_provider_thread is not True
        or result.quiescent is not True
        or result.checkpoint_ref != record.checkpoint_reference
    ):
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


def _invoke(provider: Provider, operation: PendingProviderOperation, record: JoinedLaneRecord) -> CloseArchiveResult | None:
    request = CloseRequest(operation=operation, archive=True)
    try:
        if isinstance(getattr(provider, "capabilities", None), Mapping):
            raw = provider.close(
                cast(Any, {
                    "operation": operation.model_dump(mode="json"),
                    "archive": True,
                    "payload": json.loads(operation.payload),
                })
            )
        else:
            raw = provider.close(request)
        values = dict(_mapping(raw, "provider close result"))
        values.pop("kind", None)
        if values.get("provider_session_id") != _expected_session(record):
            return None
        result = CloseArchiveResult.from_dict(values)
        validate_close_result(operation, result)
        if (
            result.state not in {"closed", "archived"}
            or result.provider_thread_ref != record.provider.handle
            or result.same_provider_thread is not True
            or result.quiescent is not True
            or result.checkpoint_ref != record.checkpoint_reference
            or result.provider_session_id != operation.provider_session_id
        ):
            return None
        return result
    except (ContractValidationError, TypeError, ValueError, OSError):
        return None
    except Exception:  # provider outage or lost response remains pending
        return None


def _persist(store: RecoveryStateStore, record: JoinedLaneRecord) -> JoinedLaneRecord:
    return cast(JoinedLaneRecord, store.save(record))


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
    """Close one lane after durable checkpoint and exact identity checks."""

    if state_root is None or not persist:
        if record is None:
            raise RecoveryStateError("governed close requires a durable joined-lane store")
        return _wait(record, "governed close requires durable Chitra storage")
    if goal_root is None:
        if record is None:
            raise RecoveryStateError("governed close requires a completion goal root")
        return _wait(record, "governed close requires the completion gate")
    store = state_store or RecoveryStateStore(state_root, lane_id or (record.lane_id if record else ""))
    current = store.load()
    if current is not None:
        record = current
    if record is None:
        if not lane_id:
            raise RecoveryStateError("governed close requires a lane record or lane_id")
        raise RecoveryStateError(f"no joined lane record for {lane_id!r}")
    if record.last_close_result is not None:
        if record.lifecycle != "inactive":
            return _wait(record, "close evidence exists but the lane is not inactive")
        return GovernedCloseDecision(
            action="closed",
            record=record,
            reason="lane already has immutable close evidence",
            close_result=record.last_close_result,
        )
    if record.lifecycle != "active":
        return _wait(record, "lane is not active and has no close evidence")
    if goal_root is not None:
        try:
            goal = get_goal(goal_root, record.session_ref)
        except Exception as exc:  # an unreadable goal cannot authorize close
            return _wait(record, f"completion goal is unavailable: {exc}")
        if (
            goal is None
            or goal.status != "done-pending-close"
            or (goal.session_ref, goal.lane_id, goal.goal_id, goal.goal_version)
            != (record.session_ref, record.lane_id, record.goal_id, record.goal_version)
        ):
            return _wait(record, "exact enrolled goal is not done-pending-close")
    if provider is None:
        return _wait(record, "provider adapter is unavailable")
    if _provider_kind(provider) != str(record.provider.kind):
        return _wait(record, "provider kind does not match the joined lane")
    for capability in ("status", "close"):
        if not _supports(provider, capability) or not record.provider.capabilities.supports(cast(Any, capability)):
            return _wait(record, f"provider lacks required {capability} capability")
    try:
        working = _ensure_checkpoint(state_root, record)
    except (OSError, RecoveryStateError, TypeError, ValueError) as exc:
        return _wait(record, f"Chitra checkpoint is not durable: {exc}")
    pending = working.pending_operation
    if pending is not None and pending.kind != "close":
        return _wait(working, "another provider operation is still pending", pending)
    current_time = _now(now)
    if pending is None:
        try:
            expected = _operation(working, _close_payload(working), current_time)
            history = (
                *working.operation_history,
                OperationReference(
                    operation_id=expected.operation_id,
                    idempotency_key=expected.idempotency_key,
                    payload_digest=expected.payload_digest,
                    kind="close",
                    created_at=expected.created_at,
                ),
            )
            pending = expected
            working = _persist(
                store,
                working.model_copy(update={"pending_operation": expected, "operation_history": history}),
            )
        except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as exc:
            return _wait(working, f"close operation could not be durably recorded: {exc}")
    else:
        try:
            expected = _operation(working, _close_payload(working), current_time)
        except (RecoveryStateError, TypeError, ValueError) as exc:
            return _wait(working, str(exc), pending)
        if not _operation_matches(pending, expected):
            return _wait(working, "pending close identity or payload changed", pending)
    recovered = _read_close_evidence(state_root, pending, working)
    if recovered is not None:
        closed = working.model_copy(
            update={
                "lifecycle": "inactive",
                "pending_operation": None,
                "last_close_result": recovered,
                "recovery": working.recovery.model_copy(update={"stage": "complete", "pending_payload": None}),
                "next_check": None,
            }
        )
        try:
            closed = _persist(store, closed)
        except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as exc:
            return _wait(working, f"durable close evidence exists but Chitra could not persist close state: {exc}", pending, recovered)
        return GovernedCloseDecision(
            action="closed",
            record=closed,
            reason="reconciled durable provider close evidence",
            operation=pending,
            close_result=recovered,
        )
    status = _status(provider, working)
    expected_session = _expected_session(working)
    if (
        status is None
        or str(status.provider) != str(working.provider.kind)
        or status.provider_session_id != expected_session
        or status.generation != working.provider.generation
        or not status.fresh
        or not status.provider_available
        or status.context_available is False
        or status.current_turn_id is not None
        or status.state not in {ProviderState.IDLE, ProviderState.CLOSED, ProviderState.ARCHIVED}
    ):
        return _wait(working, "provider status does not prove the exact idle or already-closed physical session", pending)
    result = _invoke(provider, pending, working)
    if result is None:
        return _wait(working, "provider close response is unknown or failed exact identity validation", pending)
    try:
        _write_close_evidence(state_root, pending, result)
    except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as exc:
        return _wait(working, f"provider close succeeded but Chitra could not persist close evidence: {exc}", pending, result)
    closed = working.model_copy(
        update={
            "lifecycle": "inactive",
            "pending_operation": None,
            "last_close_result": result,
            "recovery": working.recovery.model_copy(update={"stage": "complete", "pending_payload": None}),
            "next_check": None,
        }
    )
    try:
        closed = _persist(store, closed)
    except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as exc:
        return _wait(working, f"provider close succeeded but Chitra could not persist close evidence: {exc}", pending, result)
    return GovernedCloseDecision(
        action="closed",
        record=closed,
        reason="provider close is bound to Chitra's checkpoint and physical session",
        operation=pending,
        close_result=result,
    )


__all__ = ["governed_close"]

"""Durable joined-lane storage and the restart send barrier.

The wire model is owned by :mod:`chitra.session_contract`.  This module only
provides the per-lane file mechanics and the small amount of reconciliation
needed before dispatch.

No provider, host, transcript, ledger, or journal is contacted here.  Probes
are observation-only callables supplied by the integration layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, cast

from pydantic import ValidationError

from ._fsio import locked_json_store, write_json_atomic
from .session_contract import (
    ContractValidationError,
    JoinedLaneRecord,
    NextCheck,
    ProviderOperationResult,
    validate_lane_update,
    validate_operation_result,
)

LANE_DIRECTORY: Final[str] = "joined-lanes"
PREVIOUS_DOCUMENT_SUFFIX: Final[str] = ".previous.json"
LOCK_SUFFIX: Final[str] = ".lock"
_LANE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"done", "closed", "archived", "complete"})


class JoinedLaneError(ValueError):
    """Base error for malformed or unsafe joined-lane state."""


class JoinedLaneCorruptError(JoinedLaneError):
    """The newest and retained previous lane documents are unreadable."""


class JoinedLaneRevisionError(JoinedLaneError):
    """A write would move a lane backwards or reuse a sequence number."""


class JoinedLaneConflictError(JoinedLaneRevisionError):
    """A caller supplied an optimistic-concurrency value that is stale."""


class JoinedLaneIdentityError(JoinedLaneError):
    """A lane cannot be safely tied to the observed provider/session."""


Probe = Callable[[JoinedLaneRecord], object | None]


def joined_lane_directory(root: Path) -> Path:
    """Return the directory containing one JSON document per lane."""

    return root / LANE_DIRECTORY


def _validate_lane_id(lane_id: str) -> None:
    if _LANE_ID_RE.fullmatch(lane_id) is None:
        raise JoinedLaneError(f"unsafe lane_id: {lane_id!r}")


def lane_document_path(root: Path, lane_id: str) -> Path:
    _validate_lane_id(lane_id)
    return joined_lane_directory(root) / f"{lane_id}.json"


def lane_previous_document_path(root: Path, lane_id: str) -> Path:
    _validate_lane_id(lane_id)
    return joined_lane_directory(root) / f"{lane_id}.previous.json"


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JoinedLaneCorruptError(f"invalid joined-lane document {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise JoinedLaneCorruptError(f"joined-lane document {path} must contain an object")
    return value


def _value(record: object, name: str, default: Any = None) -> Any:
    return getattr(record, name, default)


def _revision(record: object) -> int:
    value = _value(record, "revision", _value(record, "record_revision", None))
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise JoinedLaneRevisionError(f"joined-lane revision must be a positive integer: {value!r}")
    return value


def _update_sequence(record: object) -> int | None:
    """Read a contract sequence without inventing a second wire field."""

    for name in ("update_sequence", "update_seq"):
        value = _value(record, name, None)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise JoinedLaneRevisionError(f"joined-lane update sequence is invalid: {value!r}")
            return value
    update = _value(record, "current_update", None)
    sequence = _value(update, "sequence", None) if update is not None else None
    progress = _value(record, "last_useful_progress", None)
    progress_sequence = _value(progress, "update_sequence", None) if progress is not None else None
    values = [item for item in (sequence, progress_sequence) if item is not None]
    if not values:
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in values):
        raise JoinedLaneRevisionError("joined-lane update sequence is invalid")
    return max(cast(list[int], values))


def _ownership_epoch(record: object) -> int:
    value = _value(record, "chitra_ownership_epoch", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise JoinedLaneRevisionError(f"joined-lane ownership epoch must be a positive integer: {value!r}")
    return value


def _active(record: object) -> bool:
    return _text(_value(record, "lifecycle", "active")) == "active"


def _owner_identity(record: object) -> tuple[str, str, str, int]:
    provider = _value(record, "provider", None)
    handle = _text(_mapping_value(provider, "handle", "provider_id", default=""))
    instance_id = _text(_mapping_value(provider, "instance_id", "provider_instance_id", default=""))
    generation = _mapping_value(provider, "generation", "provider_generation", default=None)
    if not handle or not instance_id or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise JoinedLaneIdentityError("active joined-lane record has no complete provider owner identity")
    return (_text(_value(record, "session_ref", "")), handle, instance_id, generation)


def _owner_conflicts(candidate: object, existing: object) -> bool:
    if not _active(candidate) or not _active(existing) or _value(candidate, "lane_id", "") == _value(existing, "lane_id", ""):
        return False
    candidate_identity = _owner_identity(candidate)
    existing_identity = _owner_identity(existing)
    return candidate_identity[0] == existing_identity[0] or candidate_identity[1:] == existing_identity[1:]


def _validate_transition(previous: object, current: object) -> None:
    """Apply the canonical lane/update validators to one record transition."""

    if _value(previous, "lane_id", "") != _value(current, "lane_id", ""):
        raise JoinedLaneIdentityError("lane_id cannot change in a joined-lane record")
    if _value(previous, "goal_id", "") != _value(current, "goal_id", ""):
        raise JoinedLaneIdentityError("goal_id cannot change in a joined-lane record")
    if _value(previous, "session_ref", "") != _value(current, "session_ref", ""):
        raise JoinedLaneIdentityError("session_ref cannot change in a joined-lane record")
    if _ownership_epoch(current) < _ownership_epoch(previous):
        raise JoinedLaneRevisionError("ownership epoch must not decrease")
    previous_owner = _owner_identity(previous)
    current_owner = _owner_identity(current)
    if (
        previous_owner != current_owner
        and _active(previous)
        and _active(current)
        and _ownership_epoch(current) <= _ownership_epoch(previous)
    ):
        raise JoinedLaneIdentityError("active owner identity changes require a newer ownership epoch")
    previous_update = _value(previous, "current_update", None)
    current_update = _value(current, "current_update", None)
    if previous_update is not None and current_update is None:
        raise JoinedLaneRevisionError("current_update cannot be cleared from a joined-lane record")
    if previous_update is not None and current_update is not None and previous_update != current_update:
        try:
            validate_lane_update(previous_update, current_update)
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise JoinedLaneRevisionError(f"invalid joined-lane update transition: {exc}") from exc
    pending = _value(current, "pending_operation", None)
    result = _value(current, "last_operation_result", None)
    if pending is not None and result is not None:
        try:
            validate_operation_result(pending, result)
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise JoinedLaneIdentityError(f"operation result does not match pending operation: {exc}") from exc


def _record_status(record: object) -> str:
    explicit = _value(record, "status", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    lifecycle = _value(record, "lifecycle", None)
    if lifecycle in {"closed", "archived"}:
        return cast(str, lifecycle)
    result = _value(record, "last_operation_result", None)
    consumed = _value(result, "consumed", None) if result is not None else None
    accepted = _value(result, "accepted", None) if result is not None else None
    if consumed is True:
        return "observed"
    if accepted is True:
        return "provider_accepted"
    if _value(record, "pending_operation", None) is not None:
        return "sending"
    recovery = _value(record, "recovery", None)
    stage = _value(recovery, "stage", None) if recovery is not None else None
    return str(stage) if isinstance(stage, str) and stage else "working"


def _record_pending(record: object) -> bool:
    status = _record_status(record)
    return status not in TERMINAL_STATUSES


def _dump_record(record: object) -> dict[str, Any]:
    to_dict = getattr(record, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    else:
        model_dump = getattr(record, "model_dump", None)
        if not callable(model_dump):
            raise JoinedLaneError("joined-lane record must provide to_dict or model_dump")
        value = model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise JoinedLaneError("joined-lane record serialization must be an object")
    return dict(value)


def _fields(record: object) -> Mapping[str, Any]:
    model_fields = getattr(type(record), "model_fields", None)
    return model_fields if isinstance(model_fields, Mapping) else {}


def _copy_model[RecordT](record: RecordT, updates: Mapping[str, Any]) -> RecordT:
    model_copy = getattr(record, "model_copy", None)
    if callable(model_copy):
        supported = _fields(record)
        filtered = {key: value for key, value in updates.items() if not supported or key in supported}
        return cast(RecordT, model_copy(update=filtered))
    raise JoinedLaneError("joined-lane record must provide model_copy for updates")


@dataclass(frozen=True, slots=True)
class LoadedLaneRecord:
    record: Any
    source: Literal["current", "previous"]


class JoinedLaneStore:
    """Atomic per-lane storage for the canonical joined-lane record."""

    def __init__(self, root: Path, *, directory: Path | None = None) -> None:
        self.root = root
        self.directory = directory or joined_lane_directory(root)

    def path(self, lane_id: str) -> Path:
        _validate_lane_id(lane_id)
        return self.directory / f"{lane_id}.json"

    def previous_path(self, lane_id: str) -> Path:
        _validate_lane_id(lane_id)
        return self.directory / f"{lane_id}{PREVIOUS_DOCUMENT_SUFFIX}"

    def lock_path(self, lane_id: str) -> Path:
        _validate_lane_id(lane_id)
        return self.directory / f".{lane_id}.json{LOCK_SUFFIX}"

    def ownership_lock_path(self) -> Path:
        """Return the lock key that serializes active-owner transitions."""

        return self.directory / "active-owner-transaction"

    def _parse(self, path: Path) -> Any:
        payload = _json_object(path)
        try:
            return JoinedLaneRecord.from_dict(payload)
        except (ValidationError, JoinedLaneError, TypeError, ValueError) as exc:
            raise JoinedLaneCorruptError(f"invalid joined-lane record {path}: {exc}") from exc

    def load_with_source(self, lane_id: str) -> LoadedLaneRecord | None:
        current = self.path(lane_id)
        previous = self.previous_path(lane_id)
        current_corrupt = False
        try:
            return LoadedLaneRecord(self._parse(current), "current")
        except FileNotFoundError:
            pass
        except JoinedLaneCorruptError:
            current_corrupt = True
        try:
            return LoadedLaneRecord(self._parse(previous), "previous")
        except FileNotFoundError:
            if current_corrupt:
                raise JoinedLaneCorruptError(
                    f"newest joined-lane document is invalid and has no valid predecessor for {lane_id!r}"
                ) from None
            return None
        except JoinedLaneCorruptError as exc:
            if current.exists():
                raise JoinedLaneCorruptError(
                    f"both newest and previous joined-lane documents are invalid for {lane_id!r}"
                ) from exc
            raise

    def load(self, lane_id: str) -> Any | None:
        loaded = self.load_with_source(lane_id)
        return loaded.record if loaded is not None else None

    def require(self, lane_id: str) -> Any:
        loaded = self.load_with_source(lane_id)
        if loaded is None:
            raise JoinedLaneError(f"joined-lane record is missing: {lane_id}")
        return loaded.record

    def list(self) -> tuple[Any, ...]:
        if not self.directory.exists():
            return ()
        records: list[Any] = []
        for path in sorted(self.directory.glob("*.json"), key=lambda item: item.name):
            if path.name.endswith(PREVIOUS_DOCUMENT_SUFFIX):
                continue
            records.append(self.require(path.stem))
        return tuple(records)

    def unfinished(self) -> tuple[Any, ...]:
        return tuple(record for record in self.list() if _record_pending(record))

    def _write_locked(self, record: Any, current: Any | None) -> Any:
        # A round trip validates a candidate before it can become the newest
        # document.  The canonical contract remains the only wire schema.
        payload = _dump_record(record)
        checked = JoinedLaneRecord.from_dict(payload)
        if current is not None:
            write_json_atomic(self.previous_path(checked.lane_id), _dump_record(current), fsync=True)
        write_json_atomic(self.path(checked.lane_id), _dump_record(checked), fsync=True)
        return checked

    def _reject_duplicate_active_owner(self, candidate: Any) -> None:
        if not _active(candidate):
            return
        for existing in self.list():
            if _owner_conflicts(candidate, existing):
                raise JoinedLaneConflictError(
                    f"active provider owner is already assigned to joined lane {_value(existing, 'lane_id', '')}"
                )

    def save(
        self,
        record: Any,
        *,
        expected_revision: int | None = None,
        expected_update_sequence: int | None = None,
    ) -> Any:
        """Write a strictly newer record and retain its valid predecessor."""

        lane_id = _value(record, "lane_id", "")
        _validate_lane_id(lane_id)
        if _value(record, "session_ref", "") == "":
            raise JoinedLaneError("joined-lane record has no session_ref")
        new_revision = _revision(record)
        new_sequence = _update_sequence(record)
        path = self.path(lane_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json_store(self.ownership_lock_path()), locked_json_store(path):
            current = self.load(lane_id)
            if current is not None:
                old_revision = _revision(current)
                old_sequence = _update_sequence(current)
                if expected_revision is not None and old_revision != expected_revision:
                    raise JoinedLaneConflictError(
                        f"stale joined-lane revision for {lane_id}: expected {expected_revision}, found {old_revision}"
                    )
                if expected_update_sequence is not None and old_sequence != expected_update_sequence:
                    raise JoinedLaneConflictError(
                        f"stale joined-lane update sequence for {lane_id}: expected {expected_update_sequence}, found {old_sequence}"
                    )
                if new_revision <= old_revision:
                    raise JoinedLaneRevisionError(f"revision must increase for {lane_id}: {new_revision} <= {old_revision}")
                if old_sequence is not None and new_sequence is not None and new_sequence < old_sequence:
                    raise JoinedLaneRevisionError(
                        f"update sequence must not decrease for {lane_id}: {new_sequence} < {old_sequence}"
                    )
                _validate_transition(current, record)
            _ownership_epoch(record)
            self._reject_duplicate_active_owner(record)
            return self._write_locked(record, current)

    def put(self, record: Any, **kwargs: Any) -> Any:
        return self.save(record, **kwargs)

    def update(self, lane_id: str, mutate: Callable[[Any], Any | Mapping[str, Any]]) -> Any:
        """Read, mutate, validate, and persist one strictly newer snapshot."""

        _validate_lane_id(lane_id)
        path = self.path(lane_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json_store(self.ownership_lock_path()), locked_json_store(path):
            current = self.load(lane_id)
            if current is None:
                raise JoinedLaneError(f"cannot update missing joined-lane record: {lane_id}")
            value = mutate(current)
            candidate = _copy_model(current, value) if isinstance(value, Mapping) else value
            if _value(candidate, "lane_id", lane_id) != lane_id:
                candidate = _copy_model(candidate, {"lane_id": lane_id})
            revision_name = "revision" if "revision" in _fields(current) or hasattr(current, "revision") else "record_revision"
            updates: dict[str, Any] = {revision_name: _revision(current) + 1}
            if "update_sequence" in _fields(current) or hasattr(current, "update_sequence"):
                updates["update_sequence"] = (_update_sequence(current) or 0) + 1
            elif "update_seq" in _fields(current) or hasattr(current, "update_seq"):
                updates["update_seq"] = (_update_sequence(current) or 0) + 1
            candidate = _copy_model(candidate, updates)
            if _value(candidate, "session_ref", "") != _value(current, "session_ref", ""):
                raise JoinedLaneIdentityError(f"session_ref cannot change for joined lane {lane_id}")
            old_sequence = _update_sequence(current)
            new_sequence = _update_sequence(candidate)
            if old_sequence is not None and new_sequence is not None and new_sequence < old_sequence:
                raise JoinedLaneRevisionError(
                    f"update sequence must not decrease for {lane_id}: {new_sequence} < {old_sequence}"
                )
            _validate_transition(current, candidate)
            self._reject_duplicate_active_owner(candidate)
            return self._write_locked(candidate, current)

    def create(self, record: Any) -> Any:
        lane_id = _value(record, "lane_id", "")
        _validate_lane_id(lane_id)
        path = self.path(lane_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json_store(self.ownership_lock_path()), locked_json_store(path):
            if self.load(lane_id) is not None:
                raise JoinedLaneConflictError(f"joined-lane record already exists: {lane_id}")
            _ownership_epoch(record)
            self._reject_duplicate_active_owner(record)
            return self._write_locked(record, None)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "accepted", "acknowledged", "consumed", "observed"}
    return bool(value)


def _mapping_value(value: object, *keys: str, default: object = None) -> object:
    for key in keys:
        if isinstance(value, Mapping) and key in value:
            return value[key]
        candidate = getattr(value, key, None)
        if candidate is not None:
            return candidate
    return default


def _ownership_value(value: object, *keys: str, default: object = None) -> object:
    """Read ownership_provider's envelope or its nested result uniformly."""

    direct = _mapping_value(value, *keys, default=None)
    if direct is not None:
        return direct
    nested = _mapping_value(value, "result", default=None)
    return _mapping_value(nested, *keys, default=default)


def _ownership_generation(value: object) -> object:
    """Return the Chitra ownership epoch from a validated provider envelope."""

    direct = _mapping_value(value, "ownership_generation", "ownership_epoch", default=None)
    if direct is not None:
        return direct
    source = _mapping_value(value, "source", default=None)
    return _mapping_value(source, "generation", default=None)


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    status: str = "unknown"
    accepted: bool = False
    acknowledged: bool = False
    observed: bool = False
    operation_id: str = ""
    lane_id: str = ""
    order_id: str = ""
    direction_id: str = ""
    provider_handle: str = ""
    provider_instance_id: str = ""
    provider_generation: int | None = None
    identity: str = ""
    journal_event_id: str = ""
    ledger_id: str = ""
    provider_receipt_id: str = ""
    reason: str = ""
    evidence: str = ""


def normalize_provider_observation(value: object | None) -> ProviderObservation | None:
    if value is None:
        return None
    status = _text(_mapping_value(value, "status", default="unknown")).lower() or "unknown"
    accepted = _bool(_mapping_value(value, "accepted", "transport_accepted", default=False)) or status in {
        "accepted", "acknowledged", "acked", "consumed", "observed", "sent", "completed"
    }
    acknowledged = _bool(_mapping_value(value, "acknowledged", "ack", default=False)) or status in {
        "acknowledged", "acked", "consumed", "observed", "completed"
    }
    observed = _bool(_mapping_value(value, "observed", "consumed", default=False)) or status in {
        "consumed", "observed", "completed"
    }
    generation = _mapping_value(value, "provider_generation", "generation", default=None)
    if isinstance(generation, bool) or not isinstance(generation, int):
        generation = None
    return ProviderObservation(
        status=status,
        accepted=accepted,
        acknowledged=acknowledged,
        observed=observed,
        operation_id=_text(_mapping_value(value, "operation_id", "order_id", default="")),
        lane_id=_text(_mapping_value(value, "lane_id", default="")),
        order_id=_text(_mapping_value(value, "order_id", "operation_id", default="")),
        direction_id=_text(_mapping_value(value, "direction_id", default="")),
        provider_handle=_text(_mapping_value(value, "provider_handle", "handle", default="")),
        provider_instance_id=_text(_mapping_value(value, "provider_instance_id", "instance_id", default="")),
        provider_generation=generation,
        identity=_text(_mapping_value(value, "identity", "identity_token", default="")),
        journal_event_id=_text(_mapping_value(value, "journal_event_id", "event_id", default="")),
        ledger_id=_text(_mapping_value(value, "ledger_id", default="")),
        provider_receipt_id=_text(_mapping_value(value, "provider_receipt_id", "receipt_id", default="")),
        reason=_text(_mapping_value(value, "reason", default="")),
        evidence=_text(_mapping_value(value, "evidence", default="")),
    )


def _provider_identity(record: object) -> tuple[str, str, int | None]:
    provider = _value(record, "provider", None)
    generation = _mapping_value(provider, "generation", "provider_generation", default=None)
    return (
        _text(_mapping_value(provider, "handle", "provider_id", default="")),
        _text(_mapping_value(provider, "instance_id", "provider_instance_id", default="")),
        generation if isinstance(generation, int) and not isinstance(generation, bool) else None,
    )


def _wake_id(record: object) -> str:
    direct = _text(_value(record, "wake_id", ""))
    if direct:
        return direct
    direct = _text(_value(record, "wake_condition", ""))
    if direct:
        return direct
    next_check = _value(record, "next_check", None)
    next_wake = _text(_value(next_check, "wake_condition", "")) if next_check is not None else ""
    if next_wake:
        return next_wake
    recovery = _value(record, "recovery", None)
    attempted = _text(_value(recovery, "attempted_remedy", "")) if recovery is not None else ""
    return attempted.removeprefix("wake:") if attempted.startswith("wake:") else ""


def _canonical_next_check(record: object, at: str, reason: str) -> object:
    current = _value(record, "next_check", None)
    check_type = type(current) if current is not None else NextCheck
    return check_type(at=at, reason=reason or "restart reconciliation", wake_condition=_wake_id(record) or None)


def _canonical_recovery(record: object, *, reason: str, next_check: str, blocked: bool = False) -> object | None:
    current = _value(record, "recovery", None)
    if current is None:
        return None
    updates = {
        "stage": "diagnostic" if blocked else "waiting",
        "failure_signature": reason,
        "next_allowed_attempt": next_check or None,
    }
    return cast(object, current.model_copy(update=updates)) if hasattr(current, "model_copy") else current


def _apply_state(record: Any, updates: Mapping[str, Any]) -> Any:
    """Apply abstract reconciliation fields to either contract generation."""

    model_fields = _fields(record)
    applied: dict[str, Any] = {}
    for key, value in updates.items():
        if key in model_fields:
            applied[key] = value
    if "next_check_at" in updates and "next_check" in model_fields:
        at = updates["next_check_at"]
        applied["next_check"] = None if not at else _canonical_next_check(record, at, _text(updates.get("last_error", "")))
    if "last_error" in updates and "recovery" in model_fields:
        next_at = _text(updates.get("next_check_at", ""))
        recovery = _canonical_recovery(
            record,
            reason=_text(updates["last_error"]),
            next_check=next_at,
            blocked=updates.get("status") == "blocked",
        )
        if recovery is not None:
            applied["recovery"] = recovery
    if "wake_id" in updates:
        if "wake_id" in model_fields:
            applied["wake_id"] = updates["wake_id"]
        elif "wake_condition" in model_fields:
            applied["wake_condition"] = updates["wake_id"] or None
        elif "next_check" in model_fields and (
            applied.get("next_check") is not None or _value(record, "next_check", None) is not None
        ):
            current_check = applied.get("next_check") or _value(record, "next_check")
            if hasattr(current_check, "model_copy"):
                applied["next_check"] = current_check.model_copy(update={"wake_condition": updates["wake_id"] or None})
        if "recovery" in model_fields and _value(record, "recovery", None) is not None:
            recovery = applied.get("recovery") or _value(record, "recovery")
            if hasattr(recovery, "model_copy"):
                applied["recovery"] = recovery.model_copy(
                    update={"attempted_remedy": f"wake:{updates['wake_id']}" if updates["wake_id"] else ""}
                )
    return _copy_model(record, applied)


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    lane_id: str
    session_ref: str
    status: str
    send_allowed: bool
    reason: str = ""
    next_check_at: str = ""
    record: Any | None = None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    outcomes: tuple[ReconcileOutcome, ...]
    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.errors and all(item.send_allowed for item in self.outcomes)

    @property
    def blocked(self) -> tuple[ReconcileOutcome, ...]:
        return tuple(item for item in self.outcomes if not item.send_allowed)

    def allows(self, session_ref: str) -> bool:
        if self.errors:
            return False
        matched = [item for item in self.outcomes if item.session_ref == session_ref]
        if not matched:
            return not self.outcomes
        return all(item.send_allowed for item in matched)


class JoinedLaneReconciler:
    """Reconcile every unfinished record without allocating a new send ID."""

    def __init__(
        self,
        store: JoinedLaneStore,
        *,
        provider_probe: Probe,
        journal_probe: Probe | None = None,
        ledger_probe: Probe | None = None,
        ownership_probe: Probe,
        next_check_delay_seconds: int = 30,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if next_check_delay_seconds < 1:
            raise ValueError("next_check_delay_seconds must be positive")
        self.store = store
        self.provider_probe = provider_probe
        self.journal_probe = journal_probe
        self.ledger_probe = ledger_probe
        self.ownership_probe = ownership_probe
        self.next_check_delay_seconds = next_check_delay_seconds
        self._now = now or (lambda: datetime.now(UTC))

    def _next_check(self) -> str:
        current = self._now()
        if current.tzinfo is None:
            raise ValueError("reconciler clock must be timezone-aware")
        return (current.astimezone(UTC) + timedelta(seconds=self.next_check_delay_seconds)).isoformat()

    def _save(self, record: Any, **updates: Any) -> Any:
        return self.store.update(record.lane_id, lambda current: _apply_state(current, updates)) if updates else record

    def _identity_mismatch(self, record: Any, observation: ProviderObservation | None, ownership: object | None) -> str | None:
        expected_handle, expected_instance, expected_generation = _provider_identity(record)
        if observation is not None:
            for expected, actual, label in (
                (expected_handle, observation.provider_handle, "provider handle"),
                (expected_instance, observation.provider_instance_id, "provider instance"),
            ):
                if expected and expected != actual:
                    return f"{label} mismatch"
            if expected_generation is not None and expected_generation != observation.provider_generation:
                return "provider generation mismatch"
            operation = _value(record, "pending_operation", None)
            expected_operation = _text(_value(operation, "operation_id", _value(record, "send_deduplication_key", "")))
            if operation is not None and observation.lane_id != _value(record, "lane_id", ""):
                return "provider lane_id mismatch"
            if expected_operation and expected_operation != observation.operation_id:
                return "operation/order id mismatch"
        if ownership is not None:
            status = _text(_ownership_value(ownership, "status", default=""))
            if status and status not in {"owned", "authoritative", "ok"}:
                return f"ownership is not authoritative: {status}"
            expected_instance = _provider_identity(record)[1]
            actual_instance = _text(_mapping_value(ownership, "provider_instance_id", default=""))
            if not actual_instance or actual_instance != expected_instance:
                return "ownership provider instance mismatch"
            owned_ref = _text(_ownership_value(ownership, "session_ref", default=""))
            if not owned_ref or owned_ref != _value(record, "session_ref", ""):
                return "ownership session_ref mismatch"
            lane_id = _text(_ownership_value(ownership, "lane_id", default=""))
            if not lane_id or lane_id != _value(record, "lane_id", ""):
                return "ownership lane_id mismatch"
            lane_generation = _ownership_value(ownership, "lane_generation", default=None)
            if lane_generation != _value(record, "goal_version", None):
                return "ownership lane generation mismatch"
            raw_generation = _ownership_generation(ownership)
            expected_epoch = _value(record, "chitra_ownership_epoch", None)
            if raw_generation != expected_epoch:
                return "ownership generation mismatch"
        return None

    def _observation_matches(self, record: Any, observation: ProviderObservation | None) -> bool:
        """Require exact operation and provider fencing for evidence.

        A delivery ledger entry without the stable operation identity is not
        enough to prove that this lane consumed this direction.
        """

        if observation is None:
            return False
        operation = _value(record, "pending_operation", None)
        expected_operation = _text(
            _value(operation, "operation_id", _value(record, "send_deduplication_key", _value(record, "order_id", "")))
        )
        if expected_operation and observation.operation_id != expected_operation:
            return False
        if operation is not None and observation.lane_id != _value(record, "lane_id", ""):
            return False
        expected_handle, expected_instance, expected_generation = _provider_identity(record)
        if expected_handle and observation.provider_handle != expected_handle:
            return False
        if expected_instance and observation.provider_instance_id != expected_instance:
            return False
        return not (
            expected_generation is not None
            and observation.provider_generation != expected_generation
        )

    def _operation_result(self, record: Any, observation: ProviderObservation, *, consumed: bool) -> Any | None:
        pending = _value(record, "pending_operation", None)
        if pending is None:
            return None
        existing = _value(record, "last_operation_result", None)
        result_type = type(existing) if existing is not None else ProviderOperationResult
        operation_id = _text(_value(pending, "operation_id", _value(record, "send_deduplication_key", "")))
        values: dict[str, Any] = {
            "operation_id": operation_id,
            "kind": _value(pending, "kind", "send"),
            "lane_id": _value(pending, "lane_id", record.lane_id),
            "provider_handle": _value(pending, "provider_handle", _provider_identity(record)[0]),
            "provider_instance_id": _value(pending, "provider_instance_id", _provider_identity(record)[1] or "default"),
            "provider_generation": _value(pending, "provider_generation", _provider_identity(record)[2] or 1),
            "status": "consumed" if consumed else "accepted",
            "accepted": True,
            "consumed": consumed,
            "observed_at": self._now().astimezone(UTC).isoformat(),
            "evidence": observation.evidence or observation.provider_receipt_id or observation.journal_event_id or observation.ledger_id,
        }
        # Later shared-contract revisions bind the result to the same retry
        # envelope.  Copy those fields when the canonical model requires them;
        # do not invent a digest or idempotency key.
        result_fields = getattr(result_type, "model_fields", {})
        for name in ("idempotency_key", "payload_digest"):
            if name in result_fields:
                pending_value = _value(pending, name, "")
                if not pending_value:
                    return None
                values[name] = pending_value
        return result_type(**values)

    def _outcome(self, record: Any, status: str, allowed: bool, reason: str = "") -> ReconcileOutcome:
        next_at = _text(_value(record, "next_check_at", ""))
        if not next_at:
            check = _value(record, "next_check", None)
            next_at = _text(_value(check, "at", "")) if check is not None else ""
        return ReconcileOutcome(record.lane_id, record.session_ref, status, allowed, reason, next_at, record)

    def reconcile(self, record: Any) -> ReconcileOutcome:
        if not _record_pending(record):
            return self._outcome(record, _record_status(record), True)
        try:
            provider = normalize_provider_observation(self.provider_probe(record))
            ownership = self.ownership_probe(record)
            if provider is None:
                reason = "provider identity/status evidence is unavailable"
                updated = self._save(record, status="blocked", next_check_at=self._next_check(), last_error=reason)
                return self._outcome(updated, "blocked", False, reason)
            if ownership is None:
                reason = "authoritative ownership evidence is unavailable"
                updated = self._save(record, status="blocked", next_check_at=self._next_check(), last_error=reason)
                return self._outcome(updated, "blocked", False, reason)
            ownership_status = _text(_ownership_value(ownership, "status", default=""))
            authoritative = _mapping_value(ownership, "authoritative", default=None)
            if authoritative is not True or ownership_status not in {"owned", "authoritative", "ok"}:
                reason = f"ownership is not authoritative: {ownership_status or 'unknown'}"
                updated = self._save(record, status="identity_mismatch", next_check_at=self._next_check(), last_error=reason)
                return self._outcome(updated, "identity_mismatch", False, reason)
            mismatch = self._identity_mismatch(record, provider, ownership)
            if mismatch:
                updated = self._save(record, status="identity_mismatch", next_check_at=self._next_check(), last_error=mismatch)
                return self._outcome(updated, "identity_mismatch", False, mismatch)

            journal = normalize_provider_observation(self.journal_probe(record) if self.journal_probe else None)
            ledger = normalize_provider_observation(self.ledger_probe(record) if self.ledger_probe else None)
            journal_matches = self._observation_matches(record, journal)
            ledger_matches = self._observation_matches(record, ledger)
            ack_evidence = (
                journal
                if journal_matches and journal is not None and journal.acknowledged
                else ledger
                if ledger_matches and ledger is not None and ledger.acknowledged
                else None
            )
            evidence_ack = ack_evidence is not None
            # A signed delivery ledger proves transport bookkeeping only.  It
            # cannot, by itself, prove that the lane consumed the direction.
            observed_evidence = journal if journal_matches and journal is not None and journal.observed else None
            evidence_observed = observed_evidence is not None
            current_result = normalize_provider_observation(_value(record, "last_operation_result", None))
            accepted = bool(_value(record, "provider_accepted", False)) or bool(current_result and current_result.accepted)
            acknowledged = bool(_value(record, "acknowledged", False)) or bool(current_result and current_result.acknowledged)
            observed = bool(_value(record, "observed", False)) or bool(current_result and current_result.observed)

            if provider is not None and provider.status in {"rejected", "lost-response"}:
                updated = self._save(
                    record,
                    status="blocked",
                    next_check_at=self._next_check(),
                    last_error=f"provider result: {provider.status}",
                )
                return self._outcome(updated, "blocked", False, f"provider result: {provider.status}")

            # A consumed operation remains in the canonical record as
            # evidence.  It is not a new send to reconcile, but the lane is
            # still active and its provider/ownership identity was checked
            # above before the next send can proceed.
            if current_result is not None and current_result.observed:
                return self._outcome(record, "observed", True, "previous operation already consumed")

            if provider is not None and provider.accepted and not accepted:
                accepted = True
                result = self._operation_result(record, provider, consumed=False)
                updates: dict[str, Any] = {
                    "provider_accepted": True,
                    "status": "awaiting_ack",
                    "next_check_at": self._next_check(),
                    "last_error": "provider accepted; acknowledgement not durable",
                }
                if result is not None:
                    updates["last_operation_result"] = result
                record = self._save(record, **updates)
                current_result = normalize_provider_observation(_value(record, "last_operation_result", None))

            # Acceptance is not acknowledgement.  The ledger/journal probe is
            # the durable proof needed to pass this boundary.
            if accepted and not (acknowledged or evidence_ack):
                updated = self._save(
                    record,
                    status="awaiting_ack",
                    next_check_at=self._next_check(),
                    last_error="provider accepted; acknowledgement not durable",
                )
                return self._outcome(updated, "awaiting_ack", False, "provider accepted; waiting for durable acknowledgement")

            if evidence_ack and not acknowledged:
                acknowledged = True
                updates = {"acknowledged": True, "next_check_at": "", "last_error": ""}
                if ack_evidence is not None and ack_evidence.ledger_id:
                    updates["ledger_id"] = ack_evidence.ledger_id
                if ack_evidence is not None and ack_evidence.journal_event_id:
                    updates["journal_event_id"] = ack_evidence.journal_event_id
                if _value(record, "pending_operation", None) is not None:
                    result = self._operation_result(record, ack_evidence, consumed=False) if ack_evidence is not None else None
                    if result is not None:
                        updates["last_operation_result"] = result
                record = self._save(record, **updates)

            if evidence_observed:
                updates = {"observed": True, "acknowledged": True, "status": "observed", "next_check_at": "", "last_error": ""}
                if observed_evidence is not None and observed_evidence.journal_event_id:
                    updates["journal_event_id"] = observed_evidence.journal_event_id
                if _value(record, "pending_operation", None) is not None:
                    result = self._operation_result(record, observed_evidence, consumed=True) if observed_evidence is not None else None
                    if result is not None:
                        updates["last_operation_result"] = result
                updated = self._save(record, **updates)
                return self._outcome(updated, "observed", True, "direction observed")

            in_flight = accepted or acknowledged or observed or _value(record, "pending_operation", None) is not None
            if in_flight:
                updated = self._save(
                    record,
                    status="sent_unobserved",
                    next_check_at=self._next_check(),
                    last_error="direction sent or accepted but not observed in the journal",
                )
                return self._outcome(updated, "sent_unobserved", False, "sent-but-unobserved direction")
            return self._outcome(record, _record_status(record), True)
        except (OSError, JoinedLaneError, ValidationError, TypeError, ValueError) as exc:
            try:
                updated = self._save(record, status="blocked", next_check_at=self._next_check(), last_error=f"reconciliation failed: {exc}")
            except Exception as save_exc:  # noqa: BLE001 - the report remains fail-closed
                return ReconcileOutcome(
                    record.lane_id,
                    record.session_ref,
                    "blocked",
                    False,
                    f"reconciliation failed and could not persist: {save_exc}",
                    record=record,
                )
            return self._outcome(updated, "blocked", False, f"reconciliation failed: {exc}")

    def reconcile_all(self) -> ReconcileReport:
        try:
            records = self.store.unfinished()
        except (OSError, JoinedLaneError, ValidationError, ValueError) as exc:
            return ReconcileReport((), (f"joined-lane load failed: {exc}",))
        return ReconcileReport(tuple(self.reconcile(record) for record in records))

    def wake(self, lane_id: str, *, wake_id: str = "") -> ReconcileOutcome:
        """Re-run one lane without changing its operation/order identity."""

        record = self.store.require(lane_id)
        if wake_id and _wake_id(record) == wake_id:
            return self._outcome(record, "wake_reused", False, "wake already applied")
        if not _record_pending(record):
            updated = self._save(record, wake_id=wake_id) if wake_id else record
            return self._outcome(updated, "wake_reused", True, "lane is already complete")
        updates: dict[str, Any] = {
            "next_check_at": self._now().astimezone(UTC).isoformat() if wake_id else "",
        }
        if wake_id:
            updates["wake_id"] = wake_id
        updated = self._save(record, **updates)
        return self.reconcile(updated)


LaneRecord = JoinedLaneRecord
LaneStore = JoinedLaneStore
LaneReconciler = JoinedLaneReconciler
ReconciliationReport = ReconcileReport


def load_lane_record(root: Path, lane_id: str) -> JoinedLaneRecord | None:
    return JoinedLaneStore(root).load(lane_id)


def save_lane_record(root: Path, record: Any, **kwargs: Any) -> Any:
    return JoinedLaneStore(root).save(record, **kwargs)


def reconcile_before_send(
    root: Path,
    *,
    provider_probe: Probe,
    journal_probe: Probe | None = None,
    ledger_probe: Probe | None = None,
    ownership_probe: Probe,
    now: Callable[[], datetime] | None = None,
) -> ReconcileReport:
    """Load and reconcile every unfinished lane before dispatch is allowed."""

    return JoinedLaneReconciler(
        JoinedLaneStore(root),
        provider_probe=provider_probe,
        journal_probe=journal_probe,
        ledger_probe=ledger_probe,
        ownership_probe=ownership_probe,
        now=now,
    ).reconcile_all()


__all__ = [
    "JoinedLaneError",
    "JoinedLaneCorruptError",
    "JoinedLaneRevisionError",
    "JoinedLaneConflictError",
    "JoinedLaneIdentityError",
    "JoinedLaneRecord",
    "LaneRecord",
    "JoinedLaneStore",
    "LaneStore",
    "ProviderObservation",
    "normalize_provider_observation",
    "ReconcileOutcome",
    "ReconcileReport",
    "ReconciliationReport",
    "JoinedLaneReconciler",
    "LaneReconciler",
    "joined_lane_directory",
    "lane_document_path",
    "lane_previous_document_path",
    "load_lane_record",
    "save_lane_record",
    "reconcile_before_send",
]

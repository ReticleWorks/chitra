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
import shlex
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, cast

from pydantic import ValidationError

from ._fsio import locked_json_store, write_json_atomic
from .journal import EventJournal
from .ledger import LedgerEntry, verify_entry
from .ownership_provider import DEFAULT_SOCKET_PATH, QUERY_SCHEMA, request_json_line
from .provider_protocol import ProviderUpdate, UpdateKind
from .session_contract import (
    MAX_INLINE_WAKE_RECEIPTS,
    ContractValidationError,
    InterventionEvidence,
    JoinedLaneRecord,
    LaneLaunchPolicy,
    NextCheck,
    OperationReference,
    PendingProviderOperation,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    RecordTransitionKind,
    UsageReport,
    WakeReceipt,
    extend_wake_archive_digest,
    validate_record_transition,
)
from .usage_policy import (
    AmpCreatePolicyDecision,
    AmpCreateSearchEvidence,
    evaluate_amp_create_policy,
    evaluate_usage_policy,
    launch_policy_problem,
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


OwnershipProbe = Callable[[JoinedLaneRecord], object | None]


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


def _revision(record: JoinedLaneRecord) -> int:
    value = record.revision
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise JoinedLaneRevisionError(f"joined-lane revision must be a positive integer: {value!r}")
    return value


def _update_sequence(record: JoinedLaneRecord) -> int | None:
    """Return the canonical lane-update sequence, when one exists."""

    return None if record.current_update is None else record.current_update.sequence


def _ownership_epoch(record: JoinedLaneRecord) -> int:
    value = record.chitra_ownership_epoch
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise JoinedLaneRevisionError(f"joined-lane ownership epoch must be a positive integer: {value!r}")
    return value


def _active(record: JoinedLaneRecord) -> bool:
    return record.lifecycle == "active"


def _owner_identity(record: JoinedLaneRecord) -> tuple[str, str, str, int]:
    handle = record.provider.handle
    instance_id = record.provider.instance_id
    generation = record.provider.generation
    if not handle or not instance_id or generation is None or generation < 1:
        raise JoinedLaneIdentityError("active joined-lane record has no complete provider owner identity")
    return (record.session_ref, handle, instance_id, generation)


def _owner_conflicts(candidate: JoinedLaneRecord, existing: JoinedLaneRecord) -> bool:
    if not _active(candidate) or not _active(existing) or candidate.lane_id == existing.lane_id:
        return False
    candidate_identity = _owner_identity(candidate)
    existing_identity = _owner_identity(existing)
    return candidate_identity[0] == existing_identity[0] or candidate_identity[1:] == existing_identity[1:]


def _validate_transition(
    previous: JoinedLaneRecord,
    current: JoinedLaneRecord,
    *,
    transition: RecordTransitionKind = "steady",
) -> None:
    """Apply the canonical lane/update validators to one record transition."""

    try:
        validate_record_transition(previous, current, active_owners=(current.owner,), transition=transition)
    except (ContractValidationError, TypeError, ValueError) as exc:
        message = str(exc)
        if "lane_id is immutable" in message or "goal_id is immutable" in message or "session_ref" in message:
            raise JoinedLaneIdentityError(message) from exc
        raise JoinedLaneRevisionError(message) from exc


def _record_status(record: object) -> str:
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
        lane_ids: set[str] = set()
        for path in self.directory.glob("*.json"):
            if path.name.endswith(PREVIOUS_DOCUMENT_SUFFIX):
                lane_ids.add(path.name[: -len(PREVIOUS_DOCUMENT_SUFFIX)])
            else:
                lane_ids.add(path.stem)
        records: list[Any] = []
        for lane_id in sorted(lane_ids):
            records.append(self.require(lane_id))
        # The write path fences new assignments under the ownership lock, but
        # a restart must also reject a pre-existing split-brain snapshot. Do
        # not let recovery iterate two active lanes that claim one provider
        # session or provider generation.
        for index, candidate in enumerate(records):
            for existing in records[:index]:
                if _owner_conflicts(candidate, existing):
                    raise JoinedLaneConflictError(
                        f"active provider owner is assigned to joined lanes "
                        f"{_value(existing, 'lane_id', '')!r} and {_value(candidate, 'lane_id', '')!r}"
                    )
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
        transition: RecordTransitionKind = "steady",
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
                _validate_transition(current, record, transition=transition)
            _ownership_epoch(record)
            self._reject_duplicate_active_owner(record)
            return self._write_locked(record, current)

    def put(self, record: Any, **kwargs: Any) -> Any:
        return self.save(record, **kwargs)

    def update(
        self,
        lane_id: str,
        mutate: Callable[[Any], Any | Mapping[str, Any]],
        *,
        transition: RecordTransitionKind = "steady",
    ) -> Any:
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
            updates: dict[str, Any] = {"revision": _revision(current) + 1}
            candidate = _copy_model(candidate, updates)
            old_sequence = _update_sequence(current)
            new_sequence = _update_sequence(candidate)
            if old_sequence is not None and new_sequence is not None and new_sequence < old_sequence:
                raise JoinedLaneRevisionError(
                    f"update sequence must not decrease for {lane_id}: {new_sequence} < {old_sequence}"
                )
            _validate_transition(current, candidate, transition=transition)
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

    def ensure_from_goal(
        self,
        goal: Any,
        provider_result: ProviderOperationResult | None,
        *,
        provider_kind: str,
        provider_capabilities: ProviderCapabilities | None = None,
        provider_project_ref: str | None = None,
        provider_profile_digest: str | None = None,
        provider_version: str | None = None,
        launch_policy: LaneLaunchPolicy | None = None,
    ) -> JoinedLaneRecord:
        """Atomically materialize a missing lane from exact goal/evidence.

        Missing provider evidence is an explicit bootstrap failure.  The
        caller must keep the barrier closed rather than inventing a provider
        handle or a new operation identity.
        """

        lane_id = getattr(goal, "lane_id", "")
        session_ref = getattr(goal, "session_ref", "")
        goal_id = getattr(goal, "goal_id", "")
        goal_version = getattr(goal, "goal_version", None)
        if not all(isinstance(value, str) and value for value in (lane_id, session_ref, goal_id)):
            raise JoinedLaneIdentityError("goal lacks exact lane identity for joined-lane bootstrap")
        if isinstance(goal_version, bool) or not isinstance(goal_version, int) or goal_version < 1:
            raise JoinedLaneIdentityError("goal lacks an exact positive goal_version")
        if provider_result is None or provider_result.status in {"unknown", "lost-response"}:
            raise JoinedLaneIdentityError("provider evidence is unknown; joined-lane bootstrap is blocked")
        if provider_result.lane_id != lane_id:
            raise JoinedLaneIdentityError("provider evidence lane_id does not match goal")
        if provider_kind not in {"tophand", "amp"}:
            raise JoinedLaneIdentityError(f"unsupported provider kind: {provider_kind}")
        if provider_kind == "amp":
            if not isinstance(provider_capabilities, ProviderCapabilities):
                raise JoinedLaneIdentityError("Amp bootstrap requires measured provider capabilities")
            if launch_policy is None:
                raise JoinedLaneIdentityError("Amp bootstrap requires an authoritative Chitra launch policy")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (provider_project_ref, provider_profile_digest, provider_version)
            ):
                raise JoinedLaneIdentityError("Amp bootstrap requires authoritative project, profile, and version facts")
            assert provider_project_ref is not None
            assert provider_profile_digest is not None
            assert provider_version is not None
            if (
                launch_policy.lane_id,
                launch_policy.goal_id,
                launch_policy.goal_version,
                launch_policy.provider_kind,
                launch_policy.project_ref,
                launch_policy.profile_digest,
                launch_policy.provider_version,
            ) != (
                lane_id,
                goal_id,
                goal_version,
                "amp",
                provider_project_ref,
                provider_profile_digest,
                provider_version,
            ):
                raise JoinedLaneIdentityError("Amp launch policy does not match authoritative provider facts")
            capabilities = provider_capabilities
        else:
            # Tophand launch receipts prove the same provider surface used by
            # the governed recovery adapter.  Recording only send/read here
            # strands a completed lane before Chitra can checkpoint and close
            # it after a restart.
            capabilities = ProviderCapabilities.from_supported(
                (
                    "create_or_resume",
                    "status",
                    "send",
                    "read_updates",
                    "checkpoint",
                    "usage",
                    "cancel_current_turn",
                    "close",
                )
            )
        resolved_provider_version = provider_version if provider_kind == "amp" and provider_version is not None else ""
        provider = ProviderIdentity(
            kind=cast(Literal["tophand", "amp"], provider_kind),
            handle=provider_result.provider_handle,
            provider_session_id=session_ref,
            instance_id=provider_result.provider_instance_id,
            generation=provider_result.provider_generation,
            project_ref=provider_project_ref if provider_kind == "amp" else None,
            profile_digest=provider_profile_digest if provider_kind == "amp" else None,
            provider_version=resolved_provider_version,
            capabilities=capabilities,
        )
        history = (
            OperationReference(
                operation_id=provider_result.operation_id,
                idempotency_key=provider_result.idempotency_key,
                payload_digest=provider_result.payload_digest,
                kind=provider_result.kind,
                created_at=provider_result.observed_at,
            ),
        )
        candidate = JoinedLaneRecord(
            lane_id=lane_id,
            goal_id=goal_id,
            goal_version=goal_version,
            session_ref=session_ref,
            provider=provider,
            launch_policy=launch_policy if provider_kind == "amp" else None,
            operation_history=history,
            last_operation_result=provider_result,
        )
        try:
            return cast(JoinedLaneRecord, self.create(candidate))
        except JoinedLaneConflictError:
            existing = self.load(lane_id)
            if existing is None:
                raise
            return cast(JoinedLaneRecord, existing)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


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
    """Typed provider/journal evidence with no status-derived consumption."""

    status: str
    accepted: bool | None
    acknowledged: bool
    consumed: bool | None
    operation_id: str
    lane_id: str
    provider_handle: str
    provider_session_id: str | None
    process_start_token: str | None
    provider_instance_id: str
    provider_generation: int
    evidence: str

    @classmethod
    def from_operation(cls, result: ProviderOperationResult) -> ProviderObservation:
        instance_id = result.provider_instance_id
        generation = result.provider_generation
        if instance_id is None or generation is None:
            raise ValueError("provider result lacks complete provider identity")
        return cls(
            status=result.status,
            accepted=result.accepted,
            acknowledged=False,
            consumed=result.consumed,
            operation_id=result.operation_id,
            lane_id=result.lane_id,
            provider_handle=result.provider_handle,
            provider_session_id=result.provider_session_id,
            process_start_token=result.process_start_token,
            provider_instance_id=instance_id,
            provider_generation=generation,
            evidence=result.evidence,
        )

    @classmethod
    def from_update(cls, update: ProviderUpdate) -> ProviderObservation:
        evidence = update.payload.get("result_evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("provider update lacks its full result_evidence envelope")
        accepted = evidence.get("accepted")
        consumed = evidence.get("consumed")
        if accepted is not None and not isinstance(accepted, bool):
            raise ValueError("provider update accepted evidence must be boolean or null")
        if consumed is not None and not isinstance(consumed, bool):
            raise ValueError("provider update consumed evidence must be boolean or null")
        instance_id = update.provider_instance_id
        generation = update.provider_generation
        provider_handle = update.provider_handle
        if provider_handle is None:
            payload_handle = update.payload.get("provider_handle")
            provider_handle = payload_handle if isinstance(payload_handle, str) else None
        if instance_id is None or generation is None or provider_handle is None:
            raise ValueError("provider update lacks complete provider/session identity")
        return cls(
            status=str(update.kind),
            accepted=accepted,
            acknowledged=True,
            consumed=consumed,
            operation_id=update.operation_id,
            lane_id=update.lane_id,
            provider_handle=provider_handle,
            provider_session_id=update.provider_session_id,
            process_start_token=(
                update.payload.get("process_start_token")
                if isinstance(update.payload.get("process_start_token"), str)
                else None
            ),
            provider_instance_id=instance_id,
            provider_generation=generation,
            evidence=update.event_id,
        )


ProviderProbe = Callable[[JoinedLaneRecord], ProviderOperationResult | None]
JournalProbe = Callable[[JoinedLaneRecord], ProviderUpdate | None]
LedgerProbe = Callable[[JoinedLaneRecord], LedgerEntry | None]
RetryPendingOperation = Callable[[PendingProviderOperation], ProviderOperationResult | None]
UsageProbe = Callable[[JoinedLaneRecord], UsageReport | None]
AmpCreateSearchProbe = Callable[[JoinedLaneRecord], AmpCreateSearchEvidence | None]


def _provider_identity(record: object) -> tuple[str, str, int | None]:
    provider = _value(record, "provider", None)
    generation = _mapping_value(provider, "generation", "provider_generation", default=None)
    return (
        _text(_mapping_value(provider, "handle", "provider_id", default="")),
        _text(_mapping_value(provider, "instance_id", "provider_instance_id", default="")),
        generation if isinstance(generation, int) and not isinstance(generation, bool) else None,
    )


def _wake_id(record: object) -> str:
    receipts = _value(record, "wake_receipts", ())
    if not isinstance(receipts, tuple):
        return ""
    goal_version = _value(record, "goal_version", None)
    for receipt in reversed(receipts):
        if _value(receipt, "goal_version", None) == goal_version:
            return _text(_value(receipt, "wake_id", ""))
    return ""


def _wake_ids(record: object) -> frozenset[str]:
    receipts = _value(record, "wake_receipts", ())
    if not isinstance(receipts, tuple):
        return frozenset()
    goal_version = _value(record, "goal_version", None)
    return frozenset(
        _text(_value(receipt, "wake_id", ""))
        for receipt in receipts
        if _value(receipt, "goal_version", None) == goal_version
    )


def _canonical_next_check(record: object, at: str, reason: str, wake_condition: str | None = None) -> object:
    current = _value(record, "next_check", None)
    check_type = type(current) if current is not None else NextCheck
    condition = _text(wake_condition or "")
    if not condition and current is not None:
        condition = _text(_value(current, "wake_condition", ""))
    return check_type(at=at, reason=reason or "restart reconciliation", wake_condition=condition or None)


def _canonical_recovery(record: object, *, reason: str, next_check: str, blocked: bool = False) -> object | None:
    current = _value(record, "recovery", None)
    if current is None:
        return None
    updates = {
        "failure_signature": _value(current, "failure_signature", "") or reason,
        "next_allowed_attempt": next_check or None,
    }
    if blocked and _value(current, "stage", "none") in {"none", "complete"}:
        updates["stage"] = "waiting"
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
        applied["next_check"] = (
            None
            if not at
            else _canonical_next_check(
                record,
                at,
                _text(updates.get("last_error", "")),
                _text(updates.get("wake_condition", "")) or None,
            )
        )
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
    if "wake_id" in updates and "last_intervention" in model_fields and updates["wake_id"]:
        wake_id = _text(updates["wake_id"])
        applied["last_intervention"] = InterventionEvidence(
            operation_id=updates["wake_id"],
            action="Wake condition observed",
            consumed=True,
            useful_work_resumed=None,
            observed_at=updates["wake_observed_at"],
        )
        if "wake_receipts" in model_fields and wake_id not in _wake_ids(record):
            receipts = (
                *_value(record, "wake_receipts", ()),
                WakeReceipt(
                    wake_id=wake_id,
                    lane_id=_value(record, "lane_id", ""),
                    goal_id=_value(record, "goal_id", ""),
                    session_ref=_value(record, "session_ref", ""),
                    goal_version=_value(record, "goal_version", None),
                    wake_condition=_text(updates.get("wake_condition", "")),
                    event_sequence=updates["wake_event_sequence"],
                    observed_at=updates["wake_observed_at"],
                ),
            )
            archive_count = _value(record, "wake_archive_count", 0)
            archive_digest = _text(_value(record, "wake_archive_digest", ""))
            if len(receipts) > MAX_INLINE_WAKE_RECEIPTS:
                removed = receipts[: len(receipts) - MAX_INLINE_WAKE_RECEIPTS]
                receipts = receipts[len(removed) :]
                for receipt in removed:
                    archive_digest = extend_wake_archive_digest(archive_digest, receipt)
                archive_count += len(removed)
            applied["wake_receipts"] = receipts
            applied["wake_archive_count"] = archive_count
            applied["wake_archive_digest"] = archive_digest
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
            # An empty report proves that the store contained no unfinished
            # lanes.  Once any lane is present, an unknown session is denied.
            return not self.outcomes
        return all(item.send_allowed for item in matched)


class JoinedLaneReconciler:
    """Reconcile every unfinished record without allocating a new send ID."""

    def __init__(
        self,
        store: JoinedLaneStore,
        *,
        provider_probe: ProviderProbe,
        journal_probe: JournalProbe,
        ledger_probe: LedgerProbe | None = None,
        ownership_probe: OwnershipProbe,
        retry_pending_operation: RetryPendingOperation | None = None,
        usage_probe: UsageProbe | None = None,
        amp_create_search_probe: AmpCreateSearchProbe | None = None,
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
        self.retry_pending_operation = retry_pending_operation
        self.usage_probe = usage_probe
        self.amp_create_search_probe = amp_create_search_probe
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

    def _ledger_matches(self, record: Any, entry: LedgerEntry | None) -> bool:
        pending = _value(record, "pending_operation", None)
        return (
            entry is not None
            and pending is not None
            and entry.order_id == _value(pending, "operation_id", "")
            and entry.session_ref == _value(record, "session_ref", "")
        )

    def _operation_result(self, record: Any, observation: ProviderObservation, *, consumed: bool) -> ProviderOperationResult | None:
        pending = _value(record, "pending_operation", None)
        if pending is None:
            return None
        return ProviderOperationResult(
            operation_id=pending.operation_id,
            kind=pending.kind,
            lane_id=pending.lane_id,
            provider_handle=pending.provider_handle,
            provider_session_id=observation.provider_session_id,
            process_start_token=observation.process_start_token,
            idempotency_key=pending.idempotency_key,
            payload_digest=pending.payload_digest,
            provider_instance_id=pending.provider_instance_id,
            provider_generation=pending.provider_generation,
            status="consumed" if consumed else "accepted",
            accepted=True,
            consumed=consumed,
            observed_at=self._now().astimezone(UTC).isoformat(),
            evidence=observation.evidence,
        )

    def _outcome(self, record: Any, status: str, allowed: bool, reason: str = "") -> ReconcileOutcome:
        next_at = _text(_value(record, "next_check_at", ""))
        if not next_at:
            check = _value(record, "next_check", None)
            next_at = _text(_value(check, "at", "")) if check is not None else ""
        return ReconcileOutcome(record.lane_id, record.session_ref, status, allowed, reason, next_at, record)

    def _apply_mutation_policy(
        self,
        outcome: ReconcileOutcome,
        policy_outcome: ReconcileOutcome | None,
    ) -> ReconcileOutcome:
        """Keep read reconciliation evidence while denying the next provider mutation."""

        if policy_outcome is None or (not outcome.send_allowed and policy_outcome.status != "cancel_required"):
            return outcome
        outcome_record = outcome.record
        policy_record = policy_outcome.record
        if outcome_record is not None and policy_record is not None and _revision(outcome_record) > _revision(policy_record):
            outcome_record = self._save(
                outcome_record,
                status="blocked",
                next_check_at=policy_outcome.next_check_at,
                last_error=policy_outcome.reason,
            )
        elif policy_record is not None:
            outcome_record = policy_record
        return ReconcileOutcome(
            outcome.lane_id,
            outcome.session_ref,
            policy_outcome.status,
            False,
            policy_outcome.reason,
            policy_outcome.next_check_at,
            outcome_record,
        )

    def _amp_usage_policy_gate(
        self, record: JoinedLaneRecord
    ) -> tuple[ReconcileOutcome | None, AmpCreatePolicyDecision | None]:
        """Block Amp mutation until Chitra proves launch identity and cost headroom."""

        if record.provider.kind != "amp":
            return None, None
        problem = launch_policy_problem(record)
        if problem is not None:
            updated = self._save(record, status="blocked", next_check_at=self._next_check(), last_error=problem)
            return self._outcome(updated, "blocked", False, problem), None
        pending = record.pending_operation
        if pending is not None and pending.kind == "create_or_resume" and record.current_update is None:
            search = self.amp_create_search_probe(record) if self.amp_create_search_probe is not None else None
            create_decision = evaluate_amp_create_policy(record, search, now=self._now())
            if not create_decision.provider_reconciliation_allowed:
                updated = self._save(
                    record,
                    status="blocked",
                    next_check_at=self._next_check(),
                    last_error=create_decision.reason,
                )
                return self._outcome(updated, "blocked", False, create_decision.reason), create_decision
            return None, create_decision
        if self.usage_probe is None:
            reason = "Amp usage evidence probe is unavailable"
            updated = self._save(record, status="blocked", next_check_at=self._next_check(), last_error=reason)
            return self._outcome(updated, "blocked", False, reason), None
        decision = evaluate_usage_policy(record, self.usage_probe(record), now=self._now())
        if decision.mutation_allowed:
            return None, None
        status = "blocked"
        reason = decision.reason
        if decision.cancel_required:
            status = "cancel_required"
            reason = (
                f"{reason}; the supervisor must schedule a canonical cancel operation and verify provider quiescence"
            )
        updated = self._save(record, status="blocked", next_check_at=self._next_check(), last_error=reason)
        return self._outcome(updated, status, False, reason), None

    def reconcile(self, record: Any) -> ReconcileOutcome:
        try:
            if not _record_pending(record):
                return self._outcome(record, _record_status(record), True)
            policy_outcome, create_decision = self._amp_usage_policy_gate(record)
            provider_result = self.provider_probe(record)
            provider = ProviderObservation.from_operation(provider_result) if provider_result is not None else None
            ownership = self.ownership_probe(record)
            journal_update = self.journal_probe(record)
            journal = ProviderObservation.from_update(journal_update) if journal_update is not None else None
            ledger_entry = self.ledger_probe(record) if self.ledger_probe else None
            if provider is None and (create_decision is None or not create_decision.create_allowed):
                if policy_outcome is not None:
                    return policy_outcome
                reason = "provider identity/status evidence is unavailable"
                if create_decision is not None and create_decision.action == "adopt":
                    reason = "one matching Amp thread exists, but the Adapter did not return adoption evidence"
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
            mismatch = self._identity_mismatch(record, None, ownership)
            if mismatch:
                updated = self._save(record, status="identity_mismatch", next_check_at=self._next_check(), last_error=mismatch)
                return self._outcome(updated, "identity_mismatch", False, mismatch)

            create_retry_attempted = False
            if provider is None and create_decision is not None and create_decision.create_allowed:
                pending = _value(record, "pending_operation", None)
                retry_result = (
                    self.retry_pending_operation(pending)
                    if self.retry_pending_operation is not None and pending is not None
                    else None
                )
                create_retry_attempted = True
                if retry_result is None:
                    reason = "zero-match create reconciliation was proved, but the exact create operation was not scheduled"
                    updated = self._save(record, status="blocked", next_check_at=self._next_check(), last_error=reason)
                    return self._outcome(updated, "blocked", False, reason)
                if pending is None:
                    raise JoinedLaneIdentityError("create retry callback was given no pending operation")
                if (
                    retry_result.operation_id != pending.operation_id
                    or retry_result.idempotency_key != pending.idempotency_key
                    or retry_result.payload_digest != pending.payload_digest
                    or retry_result.lane_id != pending.lane_id
                    or retry_result.provider_instance_id != pending.provider_instance_id
                    or retry_result.provider_generation != pending.provider_generation
                ):
                    raise JoinedLaneIdentityError("create retry changed the pending operation envelope")
                provider = ProviderObservation.from_operation(retry_result)
            if provider is None:
                raise JoinedLaneIdentityError("create reconciliation did not produce provider evidence")

            journal_matches = self._observation_matches(record, journal)
            ledger_matches = self._ledger_matches(record, ledger_entry)
            ack_evidence = (
                journal
                if journal_matches and journal is not None and journal.acknowledged
                else ledger_entry
                if ledger_matches
                else None
            )
            evidence_ack = ack_evidence is not None
            # A signed delivery ledger proves transport bookkeeping only.  It
            # cannot, by itself, prove that the lane consumed the direction.
            observed_evidence = journal if journal_matches and journal is not None and journal.consumed is True else None
            evidence_observed = observed_evidence is not None
            stored_result = _value(record, "last_operation_result", None)
            current_result = ProviderObservation.from_operation(stored_result) if stored_result is not None else None
            accepted = bool(current_result and current_result.accepted)
            acknowledged = False
            observed = bool(current_result and current_result.consumed is True)

            if provider.status in {"unknown", "lost-response"} and evidence_observed and observed_evidence is not None:
                # A lost reply may still be resolved by an exact, durable
                # provider journal event.  Use that event's full identity
                # envelope for fencing; never infer identity from the loss.
                provider = observed_evidence
            if provider.status in {"unknown", "lost-response"} and not evidence_observed:
                pending = _value(record, "pending_operation", None)
                retry_result = (
                    None
                    if (
                        policy_outcome is not None
                        or create_retry_attempted
                        or (create_decision is not None and not create_decision.create_allowed)
                    )
                    else self.retry_pending_operation(pending)
                    if self.retry_pending_operation and pending is not None
                    else None
                )
                if retry_result is None:
                    reason = f"provider result: {provider.status}; exact journal evidence did not prove consumption"
                    if policy_outcome is not None:
                        reason = policy_outcome.reason
                    elif create_decision is not None and create_decision.action == "adopt":
                        reason = "one matching Amp thread must be adopted; create retry is denied"
                    updated = self._save(record, status="blocked", next_check_at=self._next_check(), last_error=reason)
                    return self._apply_mutation_policy(
                        self._outcome(updated, "blocked", False, reason),
                        policy_outcome,
                    )
                if pending is None:
                    raise JoinedLaneIdentityError("retry callback was given no pending operation")
                if (
                    retry_result.operation_id != pending.operation_id
                    or retry_result.idempotency_key != pending.idempotency_key
                    or retry_result.payload_digest != pending.payload_digest
                    or retry_result.lane_id != pending.lane_id
                    or retry_result.provider_instance_id != pending.provider_instance_id
                    or retry_result.provider_generation != pending.provider_generation
                ):
                    raise JoinedLaneIdentityError("retry callback changed the pending operation envelope")
                provider = ProviderObservation.from_operation(retry_result)

            mismatch = self._identity_mismatch(record, provider, ownership)
            if mismatch:
                updated = self._save(record, status="identity_mismatch", next_check_at=self._next_check(), last_error=mismatch)
                return self._outcome(updated, "identity_mismatch", False, mismatch)

            if provider.status == "rejected":
                updated = self._save(
                    record,
                    status="blocked",
                    next_check_at=self._next_check(),
                    last_error=f"provider result: {provider.status}",
                )
                return self._apply_mutation_policy(
                    self._outcome(updated, "blocked", False, f"provider result: {provider.status}"),
                    policy_outcome,
                )

            # A consumed operation remains in the canonical record as
            # evidence.  It is not a new send to reconcile, but the lane is
            # still active and its provider/ownership identity was checked
            # above before the next send can proceed.
            if current_result is not None and current_result.consumed is True:
                return self._apply_mutation_policy(
                    self._outcome(record, "observed", True, "previous operation already consumed"),
                    policy_outcome,
                )

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
                current_result = ProviderObservation.from_operation(_value(record, "last_operation_result"))

            # Acceptance is not acknowledgement.  The ledger/journal probe is
            # the durable proof needed to pass this boundary.
            if accepted and not (acknowledged or evidence_ack):
                updated = self._save(
                    record,
                    status="awaiting_ack",
                    next_check_at=self._next_check(),
                    last_error="provider accepted; acknowledgement not durable",
                )
                return self._apply_mutation_policy(
                    self._outcome(updated, "awaiting_ack", False, "provider accepted; waiting for durable acknowledgement"),
                    policy_outcome,
                )

            if evidence_ack and not acknowledged:
                acknowledged = True
                updates = {"acknowledged": True, "next_check_at": "", "last_error": ""}
                if _value(record, "pending_operation", None) is not None:
                    result = (
                        self._operation_result(record, ack_evidence, consumed=False)
                        if isinstance(ack_evidence, ProviderObservation)
                        else None
                    )
                    if result is not None:
                        updates["last_operation_result"] = result
                record = self._save(record, **updates)

            if evidence_observed:
                updates = {"observed": True, "acknowledged": True, "status": "observed", "next_check_at": "", "last_error": ""}
                if _value(record, "pending_operation", None) is not None:
                    result = self._operation_result(record, observed_evidence, consumed=True) if observed_evidence is not None else None
                    if result is not None:
                        updates["last_operation_result"] = result
                updated = self._save(record, **updates)
                return self._apply_mutation_policy(
                    self._outcome(updated, "observed", True, "direction observed"),
                    policy_outcome,
                )

            in_flight = accepted or acknowledged or observed or _value(record, "pending_operation", None) is not None
            if in_flight:
                updated = self._save(
                    record,
                    status="sent_unobserved",
                    next_check_at=self._next_check(),
                    last_error="direction sent or accepted but not observed in the journal",
                )
                return self._apply_mutation_policy(
                    self._outcome(updated, "sent_unobserved", False, "sent-but-unobserved direction"),
                    policy_outcome,
                )
            return self._apply_mutation_policy(
                self._outcome(record, _record_status(record), True),
                policy_outcome,
            )
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

    def wake(
        self,
        lane_id: str,
        *,
        wake_id: str = "",
        wake_condition: str = "a new safe provider fact or material lane update",
        event_sequence: int | None = None,
    ) -> ReconcileOutcome:
        """Re-run one lane without changing its operation/order identity."""

        record = self.store.require(lane_id)
        journal = EventJournal(self.store.root, lane_id)
        archived_wake_ids = {
            receipt.wake_id
            for receipt in journal.load_wakes()
            if receipt.goal_version == record.goal_version
        }
        if wake_id and (wake_id in _wake_ids(record) or wake_id in archived_wake_ids):
            return self._outcome(record, "wake_reused", False, "wake already applied")
        sequence = event_sequence
        if sequence is None and record.current_update is not None:
            sequence = record.current_update.sequence
        if wake_id and (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1):
            raise JoinedLaneIdentityError("wake receipt requires an exact positive event sequence")
        if wake_id:
            assert sequence is not None
            if not journal.proves_named_wake(
                wake_id=wake_id,
                event_sequence=sequence,
                goal_id=record.goal_id,
                session_ref=record.session_ref,
                goal_version=record.goal_version,
                wake_condition=wake_condition,
            ):
                raise JoinedLaneIdentityError("wake receipt lacks exact canonical evidence that its named condition changed")
        observed_at = self._now().astimezone(UTC).isoformat()
        if not _record_pending(record):
            updated = (
                self._save(
                    record,
                    wake_id=wake_id,
                    wake_condition=wake_condition,
                    wake_event_sequence=sequence,
                    wake_observed_at=observed_at,
                )
                if wake_id
                else record
            )
            if wake_id:
                assert sequence is not None
                journal.append_wakes(
                    (
                        WakeReceipt(
                            wake_id=wake_id,
                            lane_id=record.lane_id,
                            goal_id=record.goal_id,
                            session_ref=record.session_ref,
                            goal_version=record.goal_version,
                            wake_condition=wake_condition,
                            event_sequence=sequence,
                            observed_at=observed_at,
                        ),
                    )
                )
            return self._outcome(updated, "wake_reused", True, "lane is already complete")
        updates: dict[str, Any] = {
            "next_check_at": self._now().astimezone(UTC).isoformat() if wake_id else "",
            "wake_condition": wake_condition.strip() or "a new safe provider fact or material lane update",
        }
        if wake_id:
            assert sequence is not None
            updates["wake_id"] = wake_id
            updates["wake_observed_at"] = observed_at
            updates["wake_event_sequence"] = sequence
        updated = self._save(record, **updates)
        if wake_id:
            assert sequence is not None
            journal.append_wakes(
                (
                    WakeReceipt(
                        wake_id=wake_id,
                        lane_id=record.lane_id,
                        goal_id=record.goal_id,
                        session_ref=record.session_ref,
                        goal_version=record.goal_version,
                        wake_condition=updates["wake_condition"],
                        event_sequence=sequence,
                        observed_at=observed_at,
                    ),
                )
            )
        return self.reconcile(updated)


def load_lane_record(root: Path, lane_id: str) -> JoinedLaneRecord | None:
    return JoinedLaneStore(root).load(lane_id)


def save_lane_record(root: Path, record: Any, **kwargs: Any) -> Any:
    return JoinedLaneStore(root).save(record, **kwargs)


def reconcile_before_send(
    root: Path,
    *,
    provider_probe: ProviderProbe,
    journal_probe: JournalProbe,
    ledger_probe: LedgerProbe | None = None,
    ownership_probe: OwnershipProbe,
    retry_pending_operation: RetryPendingOperation | None = None,
    usage_probe: UsageProbe | None = None,
    amp_create_search_probe: AmpCreateSearchProbe | None = None,
    now: Callable[[], datetime] | None = None,
) -> ReconcileReport:
    """Load and reconcile every unfinished lane before dispatch is allowed."""

    return JoinedLaneReconciler(
        JoinedLaneStore(root),
        provider_probe=provider_probe,
        journal_probe=journal_probe,
        ledger_probe=ledger_probe,
        ownership_probe=ownership_probe,
        retry_pending_operation=retry_pending_operation,
        usage_probe=usage_probe,
        amp_create_search_probe=amp_create_search_probe,
        now=now,
    ).reconcile_all()


def build_production_reconciler(
    root: Path,
    *,
    provider_probe: ProviderProbe,
    journal_probe: JournalProbe,
    ownership_probe: OwnershipProbe,
    ledger_probe: LedgerProbe | None = None,
    retry_pending_operation: RetryPendingOperation | None = None,
    usage_probe: UsageProbe | None = None,
    amp_create_search_probe: AmpCreateSearchProbe | None = None,
) -> JoinedLaneReconciler:
    """Build the mandatory daemon startup barrier over canonical adapters.

    The daemon supplies probes backed by its provider, journal, ledger, and
    ownership services.  Keeping this constructor narrow prevents a startup
    path from silently selecting an in-memory or compatibility schema.
    """

    return JoinedLaneReconciler(
        JoinedLaneStore(root),
        provider_probe=provider_probe,
        journal_probe=journal_probe,
        ledger_probe=ledger_probe,
        ownership_probe=ownership_probe,
        retry_pending_operation=retry_pending_operation,
        usage_probe=usage_probe,
        amp_create_search_probe=amp_create_search_probe,
    )


def _provider_result_path(root: Path, lane_id: str) -> Path:
    _validate_lane_id(lane_id)
    return root / "provider-results" / f"{lane_id}.jsonl"


def filesystem_provider_probe(root: Path) -> ProviderProbe:
    """Read the latest typed provider result for a lane from durable state."""

    def probe(record: JoinedLaneRecord) -> ProviderOperationResult | None:
        path = _provider_result_path(root, record.lane_id)
        if not path.exists():
            return None
        pending = record.pending_operation
        found: ProviderOperationResult | None = None
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                result = ProviderOperationResult.model_validate_json(line)
                if result.lane_id != record.lane_id:
                    continue
                if pending is not None and result.operation_id != pending.operation_id:
                    continue
                if result.provider_instance_id != record.provider.instance_id or result.provider_generation != record.provider.generation:
                    continue
                found = result
        return found

    return probe


def journal_provider_probe(root: Path) -> JournalProbe:
    """Return exact full-envelope provider evidence from the canonical journal."""

    def probe(record: JoinedLaneRecord) -> ProviderUpdate | None:
        pending = record.pending_operation
        if pending is None:
            return None
        provider_instance_id = record.provider.instance_id
        provider_generation = record.provider.generation
        if provider_instance_id is None or provider_generation is None:
            return None
        expected_session_id = pending.provider_session_id or record.provider.provider_session_id or record.session_ref
        for event in reversed(EventJournal(root, record.lane_id).load()):
            payload = event.payload
            observed_session_id = payload.get("provider_session_id", event.session_id)
            if (
                event.lane != record.lane_id
                or event.goal_ref != record.goal_id
                or event.session_id != record.session_ref
                or event.goal_version != record.goal_version
                or observed_session_id != expected_session_id
            ):
                continue
            if payload.get("operation_id") != pending.operation_id:
                continue
            if payload.get("lane_id") != record.lane_id:
                continue
            if payload.get("goal_id") != record.goal_id or payload.get("session_ref") != record.session_ref:
                continue
            if payload.get("provider_handle") != pending.provider_handle:
                continue
            if payload.get("provider_instance_id") != provider_instance_id:
                continue
            if payload.get("provider_generation") != provider_generation:
                continue
            if payload.get("idempotency_key") != pending.idempotency_key:
                continue
            if payload.get("payload_digest") != pending.payload_digest:
                continue
            recovery = record.recovery
            if recovery.event_sequence is not None and payload.get("event_sequence") != recovery.event_sequence:
                continue
            evidence = payload.get("result_evidence")
            if not isinstance(evidence, Mapping):
                continue
            consumed = evidence.get("consumed")
            if consumed is not True and consumed is not False and consumed is not None:
                continue
            kind = UpdateKind.STEER_CONSUMED if consumed is True else UpdateKind.STEER_ACCEPTED
            return ProviderUpdate(
                event_id=event.event_id,
                cursor=event.event_id,
                kind=kind,
                provider_session_id=expected_session_id,
                provider_handle=pending.provider_handle,
                observed_at=event.observed_at,
                operation_id=pending.operation_id,
                lane_id=record.lane_id,
                idempotency_key=pending.idempotency_key,
                payload_digest=pending.payload_digest,
                provider_instance_id=provider_instance_id,
                provider_generation=provider_generation,
                payload={"result_evidence": dict(evidence)},
            )
        return None

    return probe


def ledger_provider_probe(ledger_path: Path, ledger_key_path: Path) -> LedgerProbe:
    """Read only verified delivery entries with exact operation/session IDs."""

    def probe(record: JoinedLaneRecord) -> LedgerEntry | None:
        pending = record.pending_operation
        if pending is None or not ledger_path.exists() or not ledger_key_path.exists():
            return None
        key = ledger_key_path.read_bytes()
        with ledger_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                entry = LedgerEntry.model_validate_json(line)
                if entry.order_id != pending.operation_id or entry.session_ref != record.session_ref:
                    continue
                if verify_entry(entry, key=key):
                    return entry
        return None

    return probe


def ownership_provider_probe(
    *,
    socket_path: Path = DEFAULT_SOCKET_PATH,
    local_extra: set[str] | None = None,
    remote_runner: Callable[[list[str]], object] | None = None,
    boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
) -> OwnershipProbe:
    """Query local or remote ownership authority with host and boot fences.

    Local lanes use the Unix ownership socket.  Remote lanes use the existing
    BatchMode SSH/forced-command read boundary; this adapter never discovers
    hosts, opens a shell, or performs login.
    """

    def probe(record: JoinedLaneRecord) -> object | None:
        host_id = record.session_ref.split(":", 1)[0]
        if not host_id:
            return None
        try:
            from .dispatch import is_local_host, run_cmd, ssh_command

            if is_local_host(host_id, local_extra):
                boot_id = boot_id_path.read_text(encoding="utf-8").strip()
                if not boot_id:
                    return None
                query = {
                    "schema": QUERY_SCHEMA,
                    "request_id": str(uuid.uuid4()),
                    "host_id": host_id,
                    "boot_id": boot_id,
                    "session_ref": record.session_ref,
                }
                return request_json_line(socket_path, query)
            command = ssh_command(host_id, f"chitra-ownership-query --session-ref {shlex.quote(record.session_ref)}")
            completed = (remote_runner or (lambda argv: run_cmd(argv, timeout=8)))(command)
            if getattr(completed, "returncode", 1) != 0:
                return None
            response = json.loads(getattr(completed, "stdout", ""))
            if not isinstance(response, Mapping):
                return None
            if response.get("host_id") != host_id or not isinstance(response.get("boot_id"), str):
                return None
            nested = response.get("result")
            if not isinstance(nested, Mapping) or nested.get("session_ref") != record.session_ref:
                return None
            return response
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    return probe


def build_filesystem_reconciler(
    root: Path,
    *,
    ledger_path: Path,
    ledger_key_path: Path,
    ownership_socket_path: Path = DEFAULT_SOCKET_PATH,
) -> JoinedLaneReconciler:
    """Build the daemon barrier from durable provider, journal, ledger, and ownership adapters."""

    return build_production_reconciler(
        root,
        provider_probe=filesystem_provider_probe(root),
        journal_probe=journal_provider_probe(root),
        ledger_probe=ledger_provider_probe(ledger_path, ledger_key_path),
        ownership_probe=ownership_provider_probe(socket_path=ownership_socket_path),
    )


__all__ = [
    "JoinedLaneError",
    "JoinedLaneCorruptError",
    "JoinedLaneRevisionError",
    "JoinedLaneConflictError",
    "JoinedLaneIdentityError",
    "JoinedLaneRecord",
    "JoinedLaneStore",
    "ProviderObservation",
    "ProviderProbe",
    "JournalProbe",
    "LedgerProbe",
    "RetryPendingOperation",
    "UsageProbe",
    "AmpCreateSearchProbe",
    "OwnershipProbe",
    "ReconcileOutcome",
    "ReconcileReport",
    "JoinedLaneReconciler",
    "joined_lane_directory",
    "lane_document_path",
    "lane_previous_document_path",
    "load_lane_record",
    "save_lane_record",
    "reconcile_before_send",
    "build_production_reconciler",
    "build_filesystem_reconciler",
    "filesystem_provider_probe",
    "journal_provider_probe",
    "ledger_provider_probe",
    "ownership_provider_probe",
]

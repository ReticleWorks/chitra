"""Durable pause records and goal-preserving session-manager recovery."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import inspect
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import structlog

from ._fsio import write_json_atomic
from .detect.detectors import Finding
from .detect.ladder import ResponseLadder
from .goals import LOAD_SHED_HOLD_REASON_PREFIX, GoalRecord, done_when_with_delta, get_goal
from .joined_lane import JoinedLaneCorruptError
from .joined_lane import JoinedLaneStore as CanonicalJoinedLaneStore
from .rate_limit_state import Transaction
from .session_contract import (
    InterventionEvidence,
    JoinedLaneRecord,
    NextCheck,
    OperatingFact,
    OperationReference,
    PendingProviderOperation,
    ProgressEvidence,
    ProviderOperationResult,
    RecoveryState,
)
from .state_paths import state_dir

logger = structlog.get_logger(__name__)

SCHEMA = "chitra.pause_recovery.v1"


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    """Everything an operator needs to inspect and resume one verified pause."""

    pause_id: str
    session_ref: str
    hold_reason: str
    transcript_path: str
    resume_note: str
    resume_at: str
    paused_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "pause_id": self.pause_id,
            "session_ref": self.session_ref,
            "hold_reason": self.hold_reason,
            "transcript_path": self.transcript_path,
            "resume_note": self.resume_note,
            "resume_at": self.resume_at,
            "paused_at": self.paused_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RecoveryRecord:
        if not isinstance(payload, dict):
            raise ValueError("pause recovery record must be an object")
        fields = ("pause_id", "session_ref", "hold_reason", "transcript_path", "resume_note", "resume_at", "paused_at")
        # ``resume_at`` is a wall-clock resume time only rate-limit holds carry;
        # load-shed holds resume when host pressure clears and persist it empty by
        # design, so it must be allowed empty while every other field stays required.
        optional_empty = {"resume_at"}
        values: dict[str, str] = {}
        for field in fields:
            value = payload.get(field)
            if not isinstance(value, str) or (not value.strip() and field not in optional_empty):
                raise ValueError(f"pause recovery record {field} must be a non-empty string")
            values[field] = value
        return cls(**values)


def recovery_records_path(root: Path | None = None) -> Path:
    """Return the consolidated pause-recovery document path for ``root``."""
    return (state_dir() if root is None else root) / "pause_recovery.json"


@contextlib.contextmanager
def _recovery_lock(root: Path | None) -> Iterator[None]:
    path = recovery_records_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.parent / f".{path.name}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def load_recovery_records(root: Path | None = None) -> list[RecoveryRecord]:
    """Load every recorded pause in insertion order."""
    path = recovery_records_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("pause_recovery.json is not a chitra.pause_recovery.v1 document")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("pause_recovery.json records must be a list")
    return [RecoveryRecord.from_dict(item) for item in raw_records]


def _write_recovery_records(root: Path | None, records: list[RecoveryRecord]) -> None:
    path = recovery_records_path(root)
    payload = {"schema": SCHEMA, "records": [record.to_dict() for record in records]}
    write_json_atomic(path, payload, fsync=True)


def _resume_note(goal: GoalRecord) -> str:
    current_work = goal.now.strip() or goal.intent.strip() or goal.goal.strip()
    return f"Goal at pause: {goal.goal.strip()} Current work: {current_work} Done when: {done_when_with_delta(goal).strip()}"


def record_pause_recovery(root: Path | None, txn: Transaction, *, paused_at: str) -> RecoveryRecord:
    """Persist one idempotent recovery record as a transaction reaches ``held``."""
    if txn.phase != "held":
        raise ValueError("pause recovery can only be recorded for a held transaction")
    goal = get_goal(root, txn.session_ref)
    if goal is None:
        raise ValueError(f"cannot record pause recovery without a goal for {txn.session_ref}")
    # ``resume_at`` is a wall-clock resume time that only rate-limit holds carry;
    # load-shed holds resume when host pressure clears (load-driven, not timed) and
    # are created with an empty ``resume_at`` by design, so requiring it here would
    # crash the guard sweep every time a load-shed lane reaches ``held``.
    required = [txn.session_ref, txn.hold_reason, txn.transcript_path, txn.created_at, paused_at]
    if not txn.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX):
        required.append(txn.resume_at)
    if not all(value.strip() for value in required):
        raise ValueError("held transaction is missing required pause recovery data")
    pause_key = "\0".join((txn.session_ref, txn.hold_reason, txn.resume_at, txn.created_at))
    record = RecoveryRecord(
        pause_id=hashlib.sha256(pause_key.encode("utf-8")).hexdigest(),
        session_ref=txn.session_ref,
        hold_reason=txn.hold_reason,
        transcript_path=txn.transcript_path,
        resume_note=_resume_note(goal),
        resume_at=txn.resume_at,
        paused_at=paused_at,
    )
    with _recovery_lock(root):
        records = load_recovery_records(root)
        existing = next((item for item in records if item.pause_id == record.pause_id), None)
        if existing is not None:
            return existing
        records.append(record)
        _write_recovery_records(root, records)
    logger.info("pause_recovery_recorded", session_ref=record.session_ref, pause_id=record.pause_id)
    return record


# ---------------------------------------------------------------------------
# Session-manager recovery
# ---------------------------------------------------------------------------

# ``RecoveryRecord`` above is the long-standing rate/load-limit pause record.
# The session-manager recovery state below is deliberately separate: a lane
# may be held for a provider limit and also have a material-progress stall.
# Mixing those documents made it possible for one daemon to erase the other
# daemon's evidence during a restart.
SESSION_RECOVERY_SCHEMA = "chitra.session-recovery.v1"
RECOVERY_OPERATION_SCHEMA = "chitra.recovery-operation.v1"

RecoveryAction = Literal[
    "noop",
    "progress_confirmed",
    "nudge",
    "correct",
    "checkpoint",
    "relaunch",
    "diagnostic",
    "waiting",
    "wake",
]

_RECOVERY_LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _recovery_now(value: datetime | str | None = None) -> datetime:
    """Return a timezone-aware UTC datetime without changing persisted text."""

    if value is None:
        current = datetime.now(UTC)
    elif isinstance(value, str):
        current = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        current = value
    if current.tzinfo is None:
        raise ValueError("recovery timestamps must include a timezone")
    return current.astimezone(UTC)


def _recovery_iso(value: datetime | str | None = None) -> str:
    return _recovery_now(value).isoformat()


def _recovery_due(check: NextCheck | None, now: datetime) -> bool:
    if check is None:
        return True
    return _recovery_now(check.at) <= now


def _safe_recovery_lane(lane_id: str) -> str:
    if _RECOVERY_LANE_RE.fullmatch(lane_id) is None:
        raise ValueError(f"unsafe recovery lane name: {lane_id!r}")
    return lane_id


class RecoveryStateError(ValueError):
    """Raised when durable recovery evidence cannot be trusted."""


@dataclass(frozen=True, slots=True)
class RecoveryOperation:
    """A retry-safe operation envelope used by the recovery sequence."""

    operation: PendingProviderOperation
    action: RecoveryAction
    created_at: str
    result: RecoveryOperationResult | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": RECOVERY_OPERATION_SCHEMA,
            "lane_id": self.operation.lane_id,
            "operation": self.operation.to_dict(),
            "action": self.action,
            "created_at": self.created_at,
            "result": None if self.result is None else self.result.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> RecoveryOperation:
        if not isinstance(payload, Mapping):
            raise RecoveryStateError("recovery operation must be an object")
        if payload.get("schema") != RECOVERY_OPERATION_SCHEMA:
            raise RecoveryStateError("recovery operation has an unknown schema")
        operation = PendingProviderOperation.from_dict(payload.get("operation"))
        lane_id = payload.get("lane_id")
        action = payload.get("action")
        created_at = payload.get("created_at")
        if (
            lane_id != operation.lane_id
            or not isinstance(action, str)
            or action
            not in {
                "nudge",
                "correct",
                "checkpoint",
                "relaunch",
                "diagnostic",
            }
        ):
            raise RecoveryStateError("recovery operation identity or action is invalid")
        if not isinstance(created_at, str):
            raise RecoveryStateError("recovery operation created_at is invalid")
        _recovery_now(created_at)
        raw_result = payload.get("result")
        result = None if raw_result is None else RecoveryOperationResult.from_dict(raw_result)
        return cls(operation=operation, action=cast(RecoveryAction, action), created_at=created_at, result=result)


@dataclass(frozen=True, slots=True)
class RecoveryOperationResult:
    """Provider result normalized without inventing consumption evidence."""

    status: Literal["accepted", "consumed", "rejected", "unknown", "lost-response"]
    accepted: bool | None
    consumed: bool | None
    observed_at: str
    evidence: str = ""
    checkpoint_ref: str = ""
    valid_checkpoint: bool | None = None
    session_ref: str = ""
    provider_handle: str = ""
    provider_instance_id: str = ""
    provider_generation: int | None = None
    material_progress: bool | None = None
    wake_condition: str = ""
    preserved_work_manifest: tuple[str, ...] = ()
    record: JoinedLaneRecord | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "consumed": self.consumed,
            "observed_at": self.observed_at,
            "evidence": self.evidence,
            "checkpoint_ref": self.checkpoint_ref,
            "valid_checkpoint": self.valid_checkpoint,
            "session_ref": self.session_ref,
            "provider_handle": self.provider_handle,
            "provider_instance_id": self.provider_instance_id,
            "provider_generation": self.provider_generation,
            "material_progress": self.material_progress,
            "wake_condition": self.wake_condition,
            "preserved_work_manifest": list(self.preserved_work_manifest),
            "record": None if self.record is None else self.record.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> RecoveryOperationResult:
        if not isinstance(payload, Mapping):
            raise RecoveryStateError("recovery operation result must be an object")
        status = payload.get("status")
        if status not in {"accepted", "consumed", "rejected", "unknown", "lost-response"}:
            raise RecoveryStateError("recovery operation result status is invalid")
        accepted = payload.get("accepted")
        consumed = payload.get("consumed")
        if accepted is not None and type(accepted) is not bool:
            raise RecoveryStateError("recovery operation accepted must be boolean or null")
        if consumed is not None and type(consumed) is not bool:
            raise RecoveryStateError("recovery operation consumed must be boolean or null")
        if status == "consumed" and (accepted is not True or consumed is not True):
            raise RecoveryStateError("consumed recovery result requires accepted=true and consumed=true")
        if status == "accepted" and (accepted is not True or consumed is True):
            raise RecoveryStateError("accepted recovery result requires accepted=true and no observed consumption")
        if status == "rejected" and (accepted is not False or consumed is True):
            raise RecoveryStateError("rejected recovery result requires accepted=false and no observed consumption")
        if status in {"unknown", "lost-response"} and (accepted is not None or consumed is not None):
            raise RecoveryStateError("unknown or lost recovery result cannot claim acceptance or consumption")
        observed_at = payload.get("observed_at")
        if not isinstance(observed_at, str):
            raise RecoveryStateError("recovery operation observed_at is invalid")
        _recovery_now(observed_at)
        text_fields = (
            "evidence",
            "checkpoint_ref",
            "session_ref",
            "provider_handle",
            "provider_instance_id",
            "wake_condition",
        )
        values: dict[str, object] = {}
        for field in text_fields:
            value = payload.get(field, "")
            if not isinstance(value, str):
                raise RecoveryStateError(f"recovery operation {field} must be a string")
            values[field] = value
        valid_checkpoint = payload.get("valid_checkpoint")
        if valid_checkpoint is not None and type(valid_checkpoint) is not bool:
            raise RecoveryStateError("valid_checkpoint must be boolean or null")
        material_progress = payload.get("material_progress")
        if material_progress is not None and type(material_progress) is not bool:
            raise RecoveryStateError("material_progress must be boolean or null")
        provider_generation = payload.get("provider_generation")
        if provider_generation is not None and (type(provider_generation) is not int or provider_generation < 1):
            raise RecoveryStateError("provider_generation must be a positive integer or null")
        manifest = payload.get("preserved_work_manifest", [])
        if not isinstance(manifest, list) or not all(isinstance(item, str) for item in manifest):
            raise RecoveryStateError("preserved_work_manifest must be a list of strings")
        raw_record = payload.get("record")
        nested_record = None if raw_record is None else JoinedLaneRecord.from_dict(raw_record)
        return cls(
            status=status,
            accepted=accepted,
            consumed=consumed,
            observed_at=observed_at,
            valid_checkpoint=valid_checkpoint,
            material_progress=material_progress,
            provider_generation=provider_generation,
            preserved_work_manifest=tuple(cast(list[str], manifest)),
            record=nested_record,
            **{field: cast(str, values[field]) for field in text_fields},
        )

    @property
    def terminal(self) -> bool:
        return self.status in {"accepted", "consumed", "rejected"}


class RecoveryProvider(Protocol):
    """Optional provider seam used by :class:`RecoveryEngine`.

    Implementations may expose any subset of these methods.  Missing methods
    are treated as an unknown provider result and cause durable waiting; they
    never become a user question and never trigger a guessed fallback route.
    """

    def send(self, record: JoinedLaneRecord, text: str, operation: PendingProviderOperation) -> object: ...

    def checkpoint(self, record: JoinedLaneRecord, operation: PendingProviderOperation) -> object:
        """Capture governed RESCUE/checkpoint evidence before relaunch."""

        ...

    def create_or_resume(self, record: JoinedLaneRecord, operation: PendingProviderOperation) -> object: ...

    def diagnostic(self, record: JoinedLaneRecord, operation: PendingProviderOperation, max_children: int = 1) -> object: ...

    def reconcile(self, record: JoinedLaneRecord, operation: PendingProviderOperation) -> object: ...


class RecoveryFactsReader(Protocol):
    """Read-only operating-facts callback.  It cannot perform login or writes."""

    def __call__(self, record: JoinedLaneRecord) -> Sequence[OperatingFact]: ...


class RecoveryStateStore:
    """Lane-scoped adapter over the canonical joined-lane store.

    Recovery must not create a second lane document.  The adapter keeps the
    convenient one-lane API used by the recovery engine while delegating all
    locking, strict round-trip validation, revision fencing, and previous
    snapshot fallback to :mod:`chitra.joined_lane`.
    """

    def __init__(self, state_root: Path, lane_id: str) -> None:
        self.state_root = state_root
        self.lane_id = _safe_recovery_lane(lane_id)

        self._store = CanonicalJoinedLaneStore(state_root)

    @property
    def path(self) -> Path:
        return self._store.path(self.lane_id)

    @property
    def previous_path(self) -> Path:
        return self._store.previous_path(self.lane_id)

    @property
    def lock_path(self) -> Path:
        return self._store.lock_path(self.lane_id)

    def load(self) -> JoinedLaneRecord | None:
        """Load newest valid state, falling back to the prior valid snapshot."""

        try:
            value = self._store.load(self.lane_id)
        except JoinedLaneCorruptError as exc:
            raise RecoveryStateError(str(exc)) from exc
        if value is None:
            return None
        if not isinstance(value, JoinedLaneRecord):
            raise RecoveryStateError("canonical joined-lane store returned an unexpected record type")
        return value

    def save(self, record: JoinedLaneRecord) -> JoinedLaneRecord:
        """Write one revision and retain the prior valid document."""

        if record.lane_id != self.lane_id:
            raise ValueError("joined lane record lane_id does not match this store")
        current = self.load()
        next_revision = max(record.revision, (current.revision + 1) if current is not None else record.revision)
        candidate = record.model_copy(update={"revision": next_revision})
        try:
            value = self._store.save(candidate)
        except (JoinedLaneCorruptError, TypeError, ValueError) as exc:
            raise RecoveryStateError(f"canonical joined-lane save failed: {exc}") from exc
        if not isinstance(value, JoinedLaneRecord):
            raise RecoveryStateError("canonical joined-lane store returned an unexpected record type")
        return value


LaneRecordStore = RecoveryStateStore
JoinedLaneStore = CanonicalJoinedLaneStore


class RecoveryOperationStore:
    """Atomic sidecar for the operation-before-effect retry boundary.

    ``JoinedLaneRecord.pending_operation`` is used when the surrounding
    contract has no lane-update operation result to protect.  A lane update
    already carries its own provider result, so this sidecar is the lossless
    fallback for recovery operations on those records.
    """

    def __init__(self, state_root: Path, lane_id: str) -> None:
        self.state_root = state_root
        self.lane_id = _safe_recovery_lane(lane_id)
        self.directory = state_root / "recovery_operations"
        self.path = self.directory / f"{self.lane_id}.json"
        self.lock_path = self.directory / f".{self.lane_id}.lock"

    def load(self) -> RecoveryOperation | None:
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RecoveryStateError(f"cannot read recovery operation {self.path}: {exc}") from exc
        return RecoveryOperation.from_dict(payload)

    def save(self, operation: RecoveryOperation) -> RecoveryOperation:
        if operation.operation.lane_id != self.lane_id:
            raise ValueError("recovery operation lane_id does not match this store")
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                write_json_atomic(self.path, operation.to_dict(), fsync=True)
                return operation
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def clear(self) -> None:
        """Forget a completed recovery cycle before a fresh cycle starts."""

        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """One deterministic recovery outcome for a scheduled check."""

    action: RecoveryAction
    stage: str
    record: JoinedLaneRecord
    reason: str
    operation: PendingProviderOperation | None = None
    message: str = ""
    facts: tuple[OperatingFact, ...] = ()
    wake_condition: str | None = None
    user_ask: None = None

    @property
    def asks_user(self) -> bool:
        """Routine recovery is never represented as an operator ask."""

        return False


def _result_from_provider(value: object, *, now: datetime) -> RecoveryOperationResult:
    """Normalize provider responses while preserving unknown/lost-response."""

    if isinstance(value, RecoveryOperationResult):
        return value
    if isinstance(value, ProviderOperationResult):
        return RecoveryOperationResult(
            status=value.status,
            accepted=value.accepted,
            consumed=value.consumed,
            observed_at=value.observed_at,
            evidence=value.evidence,
        )
    if type(value) is bool:
        return RecoveryOperationResult(
            status="consumed" if value else "rejected",
            accepted=bool(value),
            consumed=bool(value),
            observed_at=_recovery_iso(now),
        )
    if value is None:
        return RecoveryOperationResult(status="unknown", accepted=None, consumed=None, observed_at=_recovery_iso(now))
    if isinstance(value, Mapping):
        status_value = value.get("status")
        accepted = value.get("accepted")
        consumed = value.get("consumed")
        if status_value not in {"accepted", "consumed", "rejected", "unknown", "lost-response"}:
            if value.get("valid") is True or value.get("checkpoint_valid") is True:
                status_value = "consumed"
                accepted = True
                consumed = True
            else:
                status_value = "unknown"
        status_literal = cast(Literal["accepted", "consumed", "rejected", "unknown", "lost-response"], status_value)
        if status_value == "consumed":
            accepted, consumed = True, True
        elif status_value == "accepted":
            accepted, consumed = True, False
        elif status_value == "rejected":
            accepted, consumed = False, False
        elif status_value in {"unknown", "lost-response"}:
            accepted, consumed = None, None
        observed_at = value.get("observed_at", _recovery_iso(now))
        if not isinstance(observed_at, str):
            observed_at = _recovery_iso(now)
        try:
            _recovery_now(observed_at)
        except ValueError:
            observed_at = _recovery_iso(now)
        manifest = value.get("preserved_work_manifest", ())
        preserved = tuple(manifest) if isinstance(manifest, list | tuple) and all(isinstance(item, str) for item in manifest) else ()
        raw_record = value.get("record")
        nested_record: JoinedLaneRecord | None = None
        if isinstance(raw_record, JoinedLaneRecord):
            nested_record = raw_record
        elif isinstance(raw_record, Mapping):
            try:
                nested_record = JoinedLaneRecord.from_dict(raw_record)
            except (TypeError, ValueError):
                nested_record = None
        valid_checkpoint = value.get("valid_checkpoint", value.get("valid"))
        if valid_checkpoint is not None and type(valid_checkpoint) is not bool:
            valid_checkpoint = None
        material_progress = value.get("material_progress")
        if material_progress is not None and type(material_progress) is not bool:
            material_progress = None
        generation = value.get("provider_generation")
        if generation is not None and (type(generation) is not int or generation < 1):
            generation = None
        return RecoveryOperationResult(
            status=status_literal,
            accepted=accepted,
            consumed=consumed,
            observed_at=observed_at,
            evidence=str(value.get("evidence", "")) if isinstance(value.get("evidence", ""), str) else "",
            checkpoint_ref=str(value.get("checkpoint_ref", "")) if isinstance(value.get("checkpoint_ref", ""), str) else "",
            valid_checkpoint=cast(bool | None, valid_checkpoint),
            session_ref=str(value.get("session_ref", "")) if isinstance(value.get("session_ref", ""), str) else "",
            provider_handle=str(value.get("provider_handle", "")) if isinstance(value.get("provider_handle", ""), str) else "",
            provider_instance_id=str(value.get("provider_instance_id", ""))
            if isinstance(value.get("provider_instance_id", ""), str)
            else "",
            provider_generation=generation,
            material_progress=material_progress,
            wake_condition=str(value.get("wake_condition", "")) if isinstance(value.get("wake_condition", ""), str) else "",
            preserved_work_manifest=preserved,
            record=nested_record,
        )
    return RecoveryOperationResult(status="unknown", accepted=None, consumed=None, observed_at=_recovery_iso(now))


def _payload_digest(action: str, payload: str) -> str:
    return hashlib.sha256(json.dumps({"action": action, "payload": payload}, sort_keys=True).encode()).hexdigest()


class RecoveryEngine:
    """Run the bounded, goal-preserving recovery sequence.

    One due check can perform at most one provider mutation.  Every mutation
    is first recorded in :class:`RecoveryOperationStore`; a restart therefore
    reconciles the same operation ID instead of pasting a duplicate direction
    or launching a duplicate lane.
    """

    def __init__(
        self,
        *,
        provider: RecoveryProvider | object | None = None,
        state_root: Path | None = None,
        state_store: RecoveryStateStore | None = None,
        goal_root: Path | None = None,
        journal: object | None = None,
        facts_reader: RecoveryFactsReader | None = None,
        response_ladder: ResponseLadder | None = None,
        check_interval: timedelta = timedelta(minutes=5),
        wait_interval: timedelta = timedelta(minutes=30),
        wake_predicate: Callable[[JoinedLaneRecord, Sequence[OperatingFact], Sequence[object]], bool] | None = None,
    ) -> None:
        self.provider = provider
        self.state_root = state_root
        self._state_store = state_store
        self.goal_root = goal_root
        self.journal = journal
        self.facts_reader = facts_reader
        self.response_ladder = response_ladder
        self.check_interval = check_interval
        self.wait_interval = wait_interval
        self.wake_predicate = wake_predicate
        self._memory_operations: dict[str, RecoveryOperation] = {}

    def store_for(self, lane_id: str) -> RecoveryStateStore | None:
        if self._state_store is not None:
            return self._state_store
        return None if self.state_root is None else RecoveryStateStore(self.state_root, lane_id)

    def operation_store_for(self, lane_id: str) -> RecoveryOperationStore | None:
        return None if self.state_root is None else RecoveryOperationStore(self.state_root, lane_id)

    def load(self, lane_id: str) -> JoinedLaneRecord | None:
        store = self.store_for(lane_id)
        return None if store is None else store.load()

    def _persist(self, record: JoinedLaneRecord, *, persist: bool) -> JoinedLaneRecord:
        if not persist:
            return record
        store = self.store_for(record.lane_id)
        if store is not None:
            return store.save(record)
        return record.model_copy(update={"revision": record.revision + 1})

    def schedule(
        self,
        record: JoinedLaneRecord,
        failure_signature: str,
        *,
        reason: str = "Confirm whether the lane made useful progress",
        wake_condition: str = "a material update for the same logical lane",
        now: datetime | str | None = None,
        persist: bool = True,
    ) -> JoinedLaneRecord:
        """Durably schedule a check without changing the user's goal."""

        if not failure_signature.strip():
            raise ValueError("failure_signature must be non-empty")
        current = _recovery_now(now)
        prior = record.recovery
        same_failure = prior.failure_signature == failure_signature
        if prior.stage == "complete":
            operation_store = self.operation_store_for(record.lane_id)
            if operation_store is not None:
                operation_store.clear()
            else:
                self._memory_operations.pop(record.lane_id, None)
        recovery = RecoveryState(
            stage="confirm",
            failure_signature=failure_signature,
            attempted_remedy=prior.attempted_remedy if same_failure else "",
            attempt_count=prior.attempt_count if same_failure else 0,
            next_allowed_attempt=current.isoformat(),
        )
        condition = wake_condition.strip() or "a material update for the same logical lane"
        next_check = NextCheck(at=current.isoformat(), reason=reason.strip() or "Confirm useful progress", wake_condition=condition)
        candidate = record.model_copy(
            update={
                "recovery": recovery,
                "next_check": next_check,
                "last_intervention": record.last_intervention if same_failure else None,
            }
        )
        return self._persist(candidate, persist=persist)

    request_recovery = schedule
    schedule_check = schedule

    def run_once(
        self,
        record: JoinedLaneRecord,
        *,
        now: datetime | str | None = None,
        failure_signature: str | None = None,
        reason: str = "",
        wake_condition: str | None = None,
        facts: Sequence[OperatingFact] = (),
        events: Sequence[object] = (),
        progress_rows: Sequence[object] = (),
        wake_event: bool = False,
        goal: GoalRecord | None = None,
        finding: Finding | None = None,
        persist: bool = True,
    ) -> RecoveryDecision:
        """Advance one scheduled check by one bounded action."""

        current = _recovery_now(now)
        working = record
        if failure_signature is not None and (
            not working.recovery.failure_signature
            or working.recovery.stage in ("none", "complete")
            or failure_signature != working.recovery.failure_signature
        ):
            working = self.schedule(
                working,
                failure_signature,
                reason=reason or "Confirm whether the lane made useful progress",
                wake_condition=wake_condition or "a material update for the same logical lane",
                now=current,
                persist=persist,
            )
        if working.lifecycle != "active":
            return RecoveryDecision("noop", working.recovery.stage, working, "lane is not active")
        if not _recovery_due(working.next_check, current) and not wake_event:
            return RecoveryDecision(
                "noop", working.recovery.stage, working, "scheduled check is not due", wake_condition=working.wake_condition
            )

        resolved_goal = goal
        if resolved_goal is None and self.goal_root is not None:
            resolved_goal = get_goal(self.goal_root, working.session_ref)
        if resolved_goal is None:
            return self._wait(
                working,
                current,
                reason="goal record is not currently readable; preserve the lane and retry",
                wake_condition="the enrolled goal record becomes readable",
                facts=tuple(facts),
                persist=persist,
            )
        if resolved_goal.goal_id != working.goal_id or resolved_goal.lane_id != working.lane_id:
            return self._wait(
                working,
                current,
                reason="goal identity does not match the joined lane record; preserve both until reconciled",
                wake_condition="the same logical goal identity is observed in the goal store",
                facts=tuple(facts),
                persist=persist,
            )

        refreshed_facts = tuple(facts)
        if not refreshed_facts and self.facts_reader is not None:
            try:
                refreshed_facts = tuple(self.facts_reader(working))
            except Exception as exc:  # read-only fact readers must fail closed
                logger.warning("recovery_facts_unavailable", lane_id=working.lane_id, error=str(exc))
                refreshed_facts = ()

        progress = self._latest_progress(working, events=events, progress_rows=progress_rows)
        if progress is not None and self._new_useful_progress(working.last_useful_progress, progress):
            return self._progress_confirmed(working, progress, current, persist=persist)

        stage = working.recovery.stage
        ladder_reason = self._response_ladder_hold(working, finding=finding, stage=stage)
        if ladder_reason is not None:
            return self._wait(
                working,
                current,
                reason=ladder_reason,
                wake_condition="the canonical response ladder proves consumption of the prior order",
                facts=refreshed_facts,
                persist=persist,
            )
        if stage == "waiting":
            if wake_event or (self.wake_predicate is not None and self.wake_predicate(working, refreshed_facts, events)):
                candidate = working.model_copy(
                    update={
                        "recovery": working.recovery.model_copy(update={"stage": "confirm", "next_allowed_attempt": current.isoformat()}),
                        "next_check": NextCheck(
                            at=current.isoformat(),
                            reason="Wake condition changed; confirm useful progress",
                            wake_condition=working.wake_condition,
                        ),
                    }
                )
                candidate = self._persist(candidate, persist=persist)
                return RecoveryDecision("wake", "confirm", candidate, "durable wake condition changed", facts=refreshed_facts)
            return self._wait(
                working,
                current,
                reason="no new safe tactic; keep the goal open and check again",
                wake_condition=working.wake_condition or "a material update for the same logical lane",
                facts=refreshed_facts,
                persist=persist,
            )

        if stage in ("none", "complete", "confirm"):
            return self._nudge(working, resolved_goal, current, facts=refreshed_facts, persist=persist)
        if stage == "nudge":
            return self._after_nudge(working, resolved_goal, current, facts=refreshed_facts, persist=persist)
        if stage == "correct":
            return self._correct(working, resolved_goal, current, facts=refreshed_facts, persist=persist)
        if stage == "relaunch":
            return self._relaunch(working, resolved_goal, current, facts=refreshed_facts, persist=persist)
        if stage == "diagnostic":
            return self._diagnostic(working, current, facts=refreshed_facts, persist=persist)
        return self._wait(
            working,
            current,
            reason="unknown recovery stage; preserve state and wait for a fresh observation",
            wake_condition="a valid recovery stage is observed",
            facts=refreshed_facts,
            persist=persist,
        )

    check = run_once
    tick = run_once

    def _response_ladder_hold(self, record: JoinedLaneRecord, *, finding: Finding | None, stage: str) -> str | None:
        """Consult the existing incident ladder when a detector finding is supplied."""

        if self.response_ladder is None or finding is None or stage in {"none", "complete", "waiting"}:
            return None
        marker = f"recovery-{record.lane_id}-{record.recovery.failure_signature}-{stage}"
        try:
            decision = self.response_ladder.evaluate(lane=record.lane_id, finding=finding, order_marker=marker)
        except Exception as exc:
            logger.warning("recovery_ladder_unavailable", lane_id=record.lane_id, error=str(exc))
            return "the canonical response ladder is currently unavailable; preserve the lane and retry"
        return decision.reason if decision.action == "hold" else None

    def run_for_lane(self, lane_id: str, **kwargs: Any) -> RecoveryDecision:
        record = self.load(lane_id)
        if record is None:
            raise RecoveryStateError(f"no joined lane record for {lane_id!r}")
        return self.run_once(record, **kwargs)

    def _latest_progress(
        self,
        record: JoinedLaneRecord,
        *,
        events: Sequence[object],
        progress_rows: Sequence[object],
    ) -> ProgressEvidence | None:
        source_events: Sequence[object] = events
        rows: Sequence[object] = progress_rows
        if self.journal is not None and not source_events:
            loader = getattr(self.journal, "load", None)
            if callable(loader):
                try:
                    source_events = tuple(loader())
                except Exception:
                    source_events = ()
        if self.journal is not None and not rows:
            loader = getattr(self.journal, "load_progress", None)
            if callable(loader):
                try:
                    rows = tuple(loader())
                except Exception:
                    rows = ()
        progress_ids: set[str] = set()
        for row in rows:
            classification = getattr(row, "classification", None)
            if str(getattr(classification, "value", classification)) != "progress":
                continue
            for event_id in getattr(row, "source_event_ids", ()):
                if isinstance(event_id, str):
                    progress_ids.add(event_id)
        candidates: list[tuple[int, object]] = []
        for position, event in enumerate(source_events):
            event_id = getattr(event, "event_id", None)
            payload = getattr(event, "payload", {})
            explicit = (
                isinstance(payload, Mapping)
                and isinstance(payload.get("progress_evidence"), Mapping)
                and any(
                    payload["progress_evidence"].get(key) is True
                    for key in (
                        "artifact_changed",
                        "diagnostic_changed",
                        "required_item_verified",
                        "targeted_check_flipped",
                        "live_boundary_exercised",
                    )
                )
            )
            if isinstance(event_id, str) and (event_id in progress_ids or explicit):
                candidates.append((position, event))
        if not candidates:
            return None
        position, event = candidates[-1]
        observed_at = getattr(event, "observed_at", "")
        if not isinstance(observed_at, str):
            observed_at = ""
        try:
            _recovery_now(observed_at)
        except ValueError:
            return None
        payload = getattr(event, "payload", {})
        summary = "material progress evidenced by the canonical journal"
        if isinstance(payload, Mapping):
            for key in ("summary", "content", "progress_summary"):
                if isinstance(payload.get(key), str) and payload[key].strip():
                    summary = payload[key].strip()[:500]
                    break
        return ProgressEvidence(
            update_sequence=record.current_update.sequence if record.current_update is not None else position + 1,
            summary=summary,
            observed_at=observed_at,
            evidence_ref=cast(str, event_id),
        )

    @staticmethod
    def _new_useful_progress(previous: ProgressEvidence | None, current: ProgressEvidence | None) -> bool:
        if current is None:
            return False
        if previous is None:
            return True
        if current.evidence_ref and current.evidence_ref == previous.evidence_ref:
            return False
        try:
            return _recovery_now(current.observed_at) > _recovery_now(previous.observed_at)
        except ValueError:
            return current.update_sequence > previous.update_sequence

    def _progress_confirmed(
        self, record: JoinedLaneRecord, evidence: ProgressEvidence, now: datetime, *, persist: bool
    ) -> RecoveryDecision:
        recovery = record.recovery.model_copy(update={"stage": "complete", "next_allowed_attempt": None})
        candidate = record.model_copy(update={"last_useful_progress": evidence, "recovery": recovery, "next_check": None})
        candidate = self._persist(candidate, persist=persist)
        return RecoveryDecision("progress_confirmed", "complete", candidate, "material progress confirmed", wake_condition=None)

    def _operation_for(self, record: JoinedLaneRecord, action: RecoveryAction, payload: str, now: datetime) -> PendingProviderOperation:
        signature = record.recovery.failure_signature or "unspecified"
        token = hashlib.sha256(f"{record.lane_id}:{signature}:{action}".encode()).hexdigest()[:24]
        operation_id = f"recovery-{record.lane_id}-{token}"
        return PendingProviderOperation(
            operation_id=operation_id,
            kind="send" if action in ("nudge", "correct", "diagnostic") else "checkpoint" if action == "checkpoint" else "create_or_resume",
            lane_id=record.lane_id,
            provider_handle=record.provider.handle,
            idempotency_key=f"{operation_id}-idem",
            payload_digest=_payload_digest(action, payload),
            provider_instance_id=record.provider.instance_id,
            provider_generation=record.provider.generation,
            created_at=now.isoformat(),
        )

    def _operation_load(self, lane_id: str) -> RecoveryOperation | None:
        store = self.operation_store_for(lane_id)
        if store is not None:
            return store.load()
        return self._memory_operations.get(lane_id)

    def _operation_save(self, operation: RecoveryOperation) -> None:
        store = self.operation_store_for(operation.operation.lane_id)
        if store is not None:
            store.save(operation)
        else:
            self._memory_operations[operation.operation.lane_id] = operation

    def _invoke(
        self,
        method: object,
        *,
        record: JoinedLaneRecord,
        operation: PendingProviderOperation,
        action: RecoveryAction,
        text: str = "",
        facts: Sequence[OperatingFact] = (),
    ) -> object:
        if not callable(method):
            return None
        values: dict[str, object] = {
            "record": record,
            "lane": record,
            "session": record,
            "operation": operation,
            "text": text,
            "message": text,
            "facts": facts,
            "max_children": 1,
            "bound": 1,
            "action": action,
        }
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            # A tiny fake provider can expose no inspectable signature.  Keep
            # that adapter shape deterministic without trying a second call
            # after a provider-side TypeError.
            try:
                return method(record, operation)
            except Exception as exc:
                logger.warning("recovery_provider_call_failed", action=action, error=str(exc))
                return None
        parameters = signature.parameters
        accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
        kwargs = {name: value for name, value in values.items() if accepts_kwargs or name in parameters}
        try:
            return method(**kwargs)
        except Exception as exc:
            logger.warning("recovery_provider_call_failed", action=action, error=str(exc))
            return None

    def _provider_method(self, action: RecoveryAction) -> object | None:
        if self.provider is None:
            return None
        names: tuple[str, ...]
        if action in ("nudge", "correct"):
            names = ("send", "nudge", "send_direction")
        elif action == "checkpoint":
            names = ("checkpoint", "validate_checkpoint")
        elif action == "relaunch":
            names = ("create_or_resume", "relaunch", "resume")
        elif action == "diagnostic":
            names = ("diagnostic", "diagnostic_sibling", "spawn_diagnostic")
        else:
            names = ()
        for name in names:
            method = getattr(self.provider, name, None)
            if callable(method):
                return cast(object, method)
        return None

    def _reconcile(
        self, record: JoinedLaneRecord, envelope: RecoveryOperation, *, facts: Sequence[OperatingFact]
    ) -> RecoveryOperationResult:
        method = getattr(self.provider, "reconcile", None) if self.provider is not None else None
        raw = self._invoke(method, record=record, operation=envelope.operation, action=envelope.action, facts=facts)
        return _result_from_provider(raw, now=_recovery_now())

    def _execute(
        self,
        record: JoinedLaneRecord,
        *,
        action: RecoveryAction,
        payload: str,
        now: datetime,
        facts: Sequence[OperatingFact] = (),
        persist: bool,
    ) -> tuple[RecoveryOperationResult, PendingProviderOperation]:
        existing = self._operation_load(record.lane_id)
        expected = self._operation_for(record, action, payload, now)
        if existing is not None and existing.operation.operation_id != expected.operation_id:
            # A different failure signature starts a new logical recovery
            # cycle.  Reconcile an unfinished older operation before allowing
            # the new effect; never overwrite a pending operation blindly.
            older_result = existing.result
            older_needs_observation = (
                older_result is None
                or older_result.status in {"unknown", "lost-response"}
                or (existing.action in ("nudge", "correct") and older_result.consumed is not True)
                or (existing.action == "checkpoint" and older_result.valid_checkpoint is not True)
                or (
                    existing.action == "relaunch"
                    and not (
                        older_result is not None
                        and (older_result.status in {"consumed", "rejected"} or older_result.session_ref or older_result.record is not None)
                    )
                )
            )
            if older_needs_observation:
                result = self._reconcile(record, existing, facts=facts)
                self._operation_save(RecoveryOperation(existing.operation, existing.action, existing.created_at, result))
                if not result.terminal:
                    return result, existing.operation
                existing = self._operation_load(record.lane_id)
            if existing is not None and existing.operation.operation_id != expected.operation_id:
                existing = None
        if existing is not None and existing.result is None:
            # Never issue a second effect while the first response is unknown.
            result = self._reconcile(record, existing, facts=facts)
            if not result.terminal:
                return result, existing.operation
            self._operation_save(RecoveryOperation(existing.operation, existing.action, existing.created_at, result))
            return result, existing.operation
        if existing is not None and existing.result is not None and existing.operation.operation_id == expected.operation_id:
            existing_result = existing.result
            needs_observation = existing_result.status in {"unknown", "lost-response"}
            needs_consumption = action in ("nudge", "correct") and existing_result.consumed is not True
            needs_checkpoint = action == "checkpoint" and existing_result.valid_checkpoint is not True
            needs_relaunch = action == "relaunch" and not (
                existing_result.status in {"consumed", "rejected"} or existing_result.session_ref or existing_result.record is not None
            )
            if needs_observation or needs_consumption or needs_checkpoint or needs_relaunch:
                # Transport acceptance is not evidence that the lane consumed
                # a direction.  Reconcile the same operation until the
                # provider or canonical journal supplies that observation.
                result = self._reconcile(record, existing, facts=facts)
                self._operation_save(RecoveryOperation(existing.operation, existing.action, existing.created_at, result))
                return result, existing.operation
            return existing.result, existing.operation
        envelope = RecoveryOperation(operation=expected, action=action, created_at=now.isoformat())
        self._operation_save(envelope)
        method = self._provider_method(action)
        raw = self._invoke(method, record=record, operation=expected, action=action, text=payload, facts=facts)
        result = _result_from_provider(raw, now=now)
        if result.terminal:
            self._operation_save(RecoveryOperation(expected, action, now.isoformat(), result))
        return result, expected

    def _action_record(
        self,
        record: JoinedLaneRecord,
        *,
        action: RecoveryAction,
        operation: PendingProviderOperation,
        result: RecoveryOperationResult,
        now: datetime,
        stage: str,
        next_check: NextCheck | None,
        wake_condition: str | None,
        persist: bool,
        checkpoint_ref: str | None = None,
    ) -> JoinedLaneRecord:
        consumed = result.consumed
        recovery = record.recovery.model_copy(
            update={
                "stage": stage,
                "attempted_remedy": action,
                "attempt_count": record.recovery.attempt_count + 1,
                "next_allowed_attempt": next_check.at if next_check is not None else None,
                "last_intervention": action,
            }
        )
        intervention = record.last_intervention
        if action in ("nudge", "correct"):
            intervention = (
                record.last_intervention.model_copy(
                    update={
                        "operation_id": operation.operation_id,
                        "action": action,
                        "consumed": consumed,
                        "useful_work_resumed": result.material_progress,
                        "observed_at": result.observed_at,
                    }
                )
                if record.last_intervention is not None
                else InterventionEvidence(
                    operation_id=operation.operation_id,
                    action=action,
                    consumed=consumed,
                    useful_work_resumed=result.material_progress,
                    observed_at=result.observed_at,
                )
            )
        updates: dict[str, object] = {
            "recovery": recovery,
            "next_check": next_check,
            "last_intervention": intervention,
        }
        if operation.operation_id not in {entry.operation_id for entry in record.operation_history}:
            updates["operation_history"] = (
                *record.operation_history,
                OperationReference(
                    operation_id=operation.operation_id,
                    idempotency_key=operation.idempotency_key,
                    payload_digest=operation.payload_digest,
                    kind=operation.kind,
                    created_at=operation.created_at,
                ),
            )
        if checkpoint_ref:
            updates["checkpoint_reference"] = checkpoint_ref
        if result.preserved_work_manifest:
            updates["preserved_work_manifest"] = tuple(dict.fromkeys((*record.preserved_work_manifest, *result.preserved_work_manifest)))
        if result.record is not None:
            replacement = result.record
            if replacement.lane_id != record.lane_id or replacement.goal_id != record.goal_id:
                raise RecoveryStateError("provider relaunch changed the logical lane or goal identity")
            # A provider cannot discard a still-active roadmap merely by
            # returning a sparse resume response.
            updates["session_ref"] = replacement.session_ref
            updates["provider"] = replacement.provider
            updates["current_update"] = replacement.current_update or record.current_update
            updates["plan_assessment"] = replacement.plan_assessment
            updates["worktree_path"] = replacement.worktree_path or record.worktree_path
            updates["repository_commit"] = replacement.repository_commit or record.repository_commit
            updates["preserved_work_manifest"] = tuple(
                dict.fromkeys((*record.preserved_work_manifest, *replacement.preserved_work_manifest, *result.preserved_work_manifest))
            )
            observed_generation = replacement.physical_session_generation
            current_generation = record.physical_session_generation
            if current_generation is None:
                if observed_generation is not None:
                    updates["physical_session_generation"] = observed_generation
            else:
                updates["physical_session_generation"] = max(
                    current_generation + (1 if action == "relaunch" else 0), observed_generation or 0
                )
        elif action == "relaunch":
            # Unknown restart-fence values stay unknown.  Do not invent a
            # physical generation merely because a relaunch was requested.
            if record.physical_session_generation is not None:
                updates["physical_session_generation"] = record.physical_session_generation + 1
        # The recovery-operation sidecar is authoritative for this effect.  Do
        # not overwrite a lane-update operation result in ``last_operation_result``.
        candidate = record.model_copy(update=updates)
        return self._persist(candidate, persist=persist)

    def _next_check(self, now: datetime, *, reason: str, wake_condition: str, wait: bool = False) -> NextCheck:
        interval = self.wait_interval if wait else self.check_interval
        return NextCheck(at=(now + interval).isoformat(), reason=reason, wake_condition=wake_condition)

    def _nudge(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        now: datetime,
        *,
        facts: Sequence[OperatingFact],
        persist: bool,
    ) -> RecoveryDecision:
        update = record.current_update
        next_action = update.next_action if update is not None else "the next in-scope action"
        message = (
            "Continue the current logical lane against the enrolled goal. "
            f"Take the next in-scope action: {next_action}. "
            f"Goal: {goal.goal.strip()}"
        )
        result, operation = self._execute(record, action="nudge", payload=message, now=now, facts=facts, persist=persist)
        if not result.terminal or (result.status == "accepted" and result.consumed is not True):
            retrying_same_nudge = record.last_intervention is not None and record.last_intervention.action == "nudge"
            check = self._next_check(
                now,
                reason=(
                    "No nudge consumption proof; wait for a canonical receipt or material lane update"
                    if retrying_same_nudge
                    else "Confirm whether the nudge was consumed"
                ),
                wake_condition="a material lane update or a provider consumption receipt",
                wait=retrying_same_nudge,
            )
            stage = "waiting" if retrying_same_nudge else "nudge"
            candidate = self._action_record(
                record,
                action="nudge",
                operation=operation,
                result=result,
                now=now,
                stage=stage,
                next_check=check,
                wake_condition=check.wake_condition,
                persist=persist,
            )
            return RecoveryDecision(
                "nudge",
                stage,
                candidate,
                "nudge consumption is not yet proven",
                operation,
                message,
                tuple(facts),
                check.wake_condition,
            )
        next_stage = "correct" if result.terminal else "nudge"
        check = self._next_check(
            now, reason="Refresh operating facts and issue one targeted correction", wake_condition="a current fact or material lane update"
        )
        candidate = self._action_record(
            record,
            action="nudge",
            operation=operation,
            result=result,
            now=now,
            stage=next_stage,
            next_check=check,
            wake_condition=check.wake_condition,
            persist=persist,
        )
        return RecoveryDecision(
            "nudge",
            "nudge",
            candidate,
            "confirmed the missed check and issued the one allowed nudge",
            operation,
            message,
            tuple(facts),
            check.wake_condition,
        )

    def _after_nudge(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        now: datetime,
        *,
        facts: Sequence[OperatingFact],
        persist: bool,
    ) -> RecoveryDecision:
        # ``nudge`` is one-shot.  If an unknown response remains, reconcile it
        # with the same operation ID; never paste the same direction again.
        pending = self._operation_load(record.lane_id)
        if pending is not None and (
            pending.result is None or pending.result.status in {"unknown", "lost-response"} or pending.result.consumed is not True
        ):
            return self._nudge(record, goal, now, facts=facts, persist=persist)
        candidate = record.model_copy(
            update={
                "recovery": record.recovery.model_copy(update={"stage": "correct", "next_allowed_attempt": now.isoformat()}),
                "next_check": NextCheck(
                    at=now.isoformat(),
                    reason="Refresh facts and issue a targeted correction",
                    wake_condition="a current operating fact or material lane update",
                ),
            }
        )
        candidate = self._persist(candidate, persist=persist)
        return self._correct(candidate, goal, now, facts=facts, persist=persist)

    def _correct(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        now: datetime,
        *,
        facts: Sequence[OperatingFact],
        persist: bool,
    ) -> RecoveryDecision:
        if self.facts_reader is not None:
            try:
                facts = tuple(self.facts_reader(record))
            except Exception as exc:
                logger.warning("recovery_fact_refresh_failed", lane_id=record.lane_id, error=str(exc))
                facts = tuple(facts)
        update = record.current_update
        next_action = update.next_action if update is not None else "the next in-scope action"
        fact_state = ", ".join(f"{fact.name}={fact.state}" for fact in facts) or "operating facts unavailable"
        message = (
            "Refresh current operating facts and correct the stalled lane in place. "
            f"Take this targeted next action: {next_action}. "
            f"Do not change the enrolled goal: {goal.goal.strip()}. "
            f"Observed fact states: {fact_state}."
        )
        result, operation = self._execute(record, action="correct", payload=message, now=now, facts=facts, persist=persist)
        if not result.terminal or (result.status == "accepted" and result.consumed is not True):
            retrying_same_correction = record.last_intervention is not None and record.last_intervention.action == "correct"
            check = self._next_check(
                now,
                reason=(
                    "No correction consumption proof; wait for a canonical receipt or material lane update"
                    if retrying_same_correction
                    else "Confirm the targeted correction without duplicating it"
                ),
                wake_condition="a material lane update or a provider consumption receipt",
                wait=retrying_same_correction,
            )
            stage = "waiting" if retrying_same_correction else "correct"
            candidate = self._action_record(
                record,
                action="correct",
                operation=operation,
                result=result,
                now=now,
                stage=stage,
                next_check=check,
                wake_condition=check.wake_condition,
                persist=persist,
            )
            return RecoveryDecision(
                "correct",
                stage,
                candidate,
                "targeted correction consumption is not yet proven",
                operation,
                message,
                tuple(facts),
                check.wake_condition,
            )
        check = self._next_check(
            now, reason="Validate the checkpoint before relaunch", wake_condition="a valid checkpoint for the same logical lane"
        )
        candidate = self._action_record(
            record,
            action="correct",
            operation=operation,
            result=result,
            now=now,
            stage="relaunch",
            next_check=check,
            wake_condition=check.wake_condition,
            persist=persist,
        )
        return RecoveryDecision(
            "correct",
            "relaunch",
            candidate,
            "refreshed facts and issued one targeted correction",
            operation,
            message,
            tuple(facts),
            check.wake_condition,
        )

    def _relaunch(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        now: datetime,
        *,
        facts: Sequence[OperatingFact],
        persist: bool,
    ) -> RecoveryDecision:
        if not record.checkpoint_reference:
            checkpoint_payload = f"checkpoint:{record.lane_id}:{record.goal_id}:{record.recovery.failure_signature}"
            result, operation = self._execute(
                record, action="checkpoint", payload=checkpoint_payload, now=now, facts=facts, persist=persist
            )
            if not result.terminal or result.valid_checkpoint is not True:
                check = self._next_check(
                    now,
                    reason="Wait for a valid checkpoint without discarding unfinished work",
                    wake_condition="a valid checkpoint for the same logical lane",
                )
                candidate = self._action_record(
                    record,
                    action="checkpoint",
                    operation=operation,
                    result=result,
                    now=now,
                    stage="diagnostic",
                    next_check=check,
                    wake_condition=check.wake_condition,
                    persist=persist,
                    checkpoint_ref=None,
                )
                return RecoveryDecision(
                    "checkpoint",
                    "diagnostic",
                    candidate,
                    "checkpoint is not yet valid; relaunch is withheld",
                    operation,
                    facts=tuple(facts),
                    wake_condition=check.wake_condition,
                )
            checkpoint_ref = result.checkpoint_ref or f"recovery-{record.lane_id}-{record.revision}"
            check = self._next_check(
                now,
                reason="Relaunch the same logical lane from the validated checkpoint",
                wake_condition="the same logical lane is resumed",
            )
            candidate = self._action_record(
                record,
                action="checkpoint",
                operation=operation,
                result=result,
                now=now,
                stage="relaunch",
                next_check=check,
                wake_condition=check.wake_condition,
                persist=persist,
                checkpoint_ref=checkpoint_ref,
            )
            return RecoveryDecision(
                "checkpoint",
                "relaunch",
                candidate,
                "checkpoint validated",
                operation,
                facts=tuple(facts),
                wake_condition=check.wake_condition,
            )
        payload = f"resume:{record.lane_id}:{record.goal_id}:{record.checkpoint_reference}"
        result, operation = self._execute(record, action="relaunch", payload=payload, now=now, facts=facts, persist=persist)
        if not result.terminal or not (result.status in {"consumed", "rejected"} or result.session_ref or result.record is not None):
            retrying_same_relaunch = record.recovery.attempted_remedy == "relaunch"
            check = self._next_check(
                now,
                reason=(
                    "Relaunch remains unobserved; wait for the same logical lane to be reconciled"
                    if retrying_same_relaunch
                    else "Reconcile the same relaunch operation"
                ),
                wake_condition="the same logical lane is resumed",
                wait=retrying_same_relaunch,
            )
            stage = "waiting" if retrying_same_relaunch else "relaunch"
            candidate = self._action_record(
                record,
                action="relaunch",
                operation=operation,
                result=result,
                now=now,
                stage=stage,
                next_check=check,
                wake_condition=check.wake_condition,
                persist=persist,
            )
            return RecoveryDecision(
                "relaunch",
                stage,
                candidate,
                "relaunch response is not yet known",
                operation,
                facts=tuple(facts),
                wake_condition=check.wake_condition,
            )
        if result.session_ref and result.session_ref != record.session_ref and result.record is None:
            # A provider may rotate only the physical session reference.  Keep
            # the logical identity and unfinished update intact.
            result = replace(result, record=record.model_copy(update={"session_ref": result.session_ref}))
        check = self._next_check(
            now, reason="Check whether the relaunch produces useful progress", wake_condition="a material update after relaunch"
        )
        candidate = self._action_record(
            record,
            action="relaunch",
            operation=operation,
            result=result,
            now=now,
            stage="diagnostic",
            next_check=check,
            wake_condition=check.wake_condition,
            persist=persist,
        )
        return RecoveryDecision(
            "relaunch",
            "diagnostic",
            candidate,
            "relaunched the same logical lane once",
            operation,
            facts=tuple(facts),
            wake_condition=check.wake_condition,
        )

    def _diagnostic(
        self,
        record: JoinedLaneRecord,
        now: datetime,
        *,
        facts: Sequence[OperatingFact],
        persist: bool,
    ) -> RecoveryDecision:
        payload = f"diagnostic:{record.lane_id}:{record.goal_id}:{record.recovery.failure_signature}"
        result, operation = self._execute(record, action="diagnostic", payload=payload, now=now, facts=facts, persist=persist)
        if not result.terminal:
            retrying_same_diagnostic = record.recovery.attempted_remedy == "diagnostic"
            check = self._next_check(
                now,
                reason=(
                    "Bounded diagnostic remains unobserved; wait for a material finding"
                    if retrying_same_diagnostic
                    else "Reconcile the bounded diagnostic sibling"
                ),
                wake_condition="the bounded diagnostic returns a material finding",
                wait=retrying_same_diagnostic,
            )
            stage = "waiting" if retrying_same_diagnostic else "diagnostic"
            candidate = self._action_record(
                record,
                action="diagnostic",
                operation=operation,
                result=result,
                now=now,
                stage=stage,
                next_check=check,
                wake_condition=check.wake_condition,
                persist=persist,
            )
            return RecoveryDecision(
                "diagnostic",
                stage,
                candidate,
                "bounded diagnostic response is not yet known",
                operation,
                facts=tuple(facts),
                wake_condition=check.wake_condition,
            )
        condition = result.wake_condition or "a new safe provider fact or material update for the same logical lane"
        check = self._next_check(
            now, reason="Wait for a real wake condition after the bounded diagnostic", wake_condition=condition, wait=True
        )
        candidate = self._action_record(
            record,
            action="diagnostic",
            operation=operation,
            result=result,
            now=now,
            stage="waiting",
            next_check=check,
            wake_condition=condition,
            persist=persist,
        )
        return RecoveryDecision(
            "diagnostic",
            "waiting",
            candidate,
            "one bounded diagnostic completed; durable waiting is armed",
            operation,
            facts=tuple(facts),
            wake_condition=condition,
        )

    def _wait(
        self,
        record: JoinedLaneRecord,
        now: datetime,
        *,
        reason: str,
        wake_condition: str,
        facts: Sequence[OperatingFact],
        persist: bool,
    ) -> RecoveryDecision:
        condition = wake_condition.strip() or "a material update for the same logical lane"
        check = self._next_check(now, reason=reason, wake_condition=condition, wait=True)
        recovery = record.recovery.model_copy(update={"stage": "waiting", "next_allowed_attempt": check.at})
        candidate = record.model_copy(update={"recovery": recovery, "next_check": check})
        candidate = self._persist(candidate, persist=persist)
        return RecoveryDecision("waiting", "waiting", candidate, reason, facts=tuple(facts), wake_condition=condition)


def confirm_useful_progress(
    record: JoinedLaneRecord,
    *,
    journal: object | None = None,
    events: Sequence[object] = (),
    progress_rows: Sequence[object] = (),
) -> ProgressEvidence | None:
    """Read the canonical journal and return only explicitly useful progress."""

    engine = RecoveryEngine(journal=journal)
    return engine._latest_progress(record, events=events, progress_rows=progress_rows)


def schedule_recovery_check(
    record: JoinedLaneRecord,
    failure_signature: str,
    *,
    state_root: Path | None = None,
    now: datetime | str | None = None,
    reason: str = "Confirm whether the lane made useful progress",
    wake_condition: str = "a material update for the same logical lane",
) -> JoinedLaneRecord:
    """Convenience wrapper for a durable scheduled check."""

    return RecoveryEngine(state_root=state_root).schedule(
        record,
        failure_signature,
        now=now,
        reason=reason,
        wake_condition=wake_condition,
    )


def run_recovery_check(record: JoinedLaneRecord, **kwargs: Any) -> RecoveryDecision:
    """Compatibility wrapper for callers that use a function-style manager."""

    return RecoveryEngine(
        provider=kwargs.pop("provider", None),
        state_root=kwargs.pop("state_root", None),
        goal_root=kwargs.pop("goal_root", None),
        journal=kwargs.pop("journal", None),
        facts_reader=kwargs.pop("facts_reader", None),
        response_ladder=kwargs.pop("response_ladder", None),
    ).run_once(record, **kwargs)


RecoveryManager = RecoveryEngine


__all__ = [
    "JoinedLaneStore",
    "LaneRecordStore",
    "RECOVERY_OPERATION_SCHEMA",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryEngine",
    "RecoveryFactsReader",
    "RecoveryManager",
    "RecoveryOperation",
    "RecoveryOperationResult",
    "RecoveryOperationStore",
    "RecoveryProvider",
    "RecoveryRecord",
    "RecoveryStateError",
    "RecoveryStateStore",
    "SESSION_RECOVERY_SCHEMA",
    "confirm_useful_progress",
    "load_recovery_records",
    "record_pause_recovery",
    "recovery_records_path",
    "run_recovery_check",
    "schedule_recovery_check",
] 

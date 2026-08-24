"""Durable pause records and canonical session recovery."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import structlog

from ._fsio import read_json_rooted, write_json_atomic, write_json_rooted
from .detect.detectors import Finding
from .detect.ladder import IncidentStore, ResponseLadder
from .detect.rescue import (
    RecoveryCheckpointBinding,
    find_recovery_checkpoint_receipt,
    load_or_create_checkpoint_key,
    sign_checkpoint_receipt,
    verify_checkpoint_receipt_signature,
)
from .goals import (
    LOAD_SHED_HOLD_REASON_PREFIX,
    GoalNotFoundError,
    GoalRecord,
    GoalValidationError,
    close_goal,
    done_when_with_delta,
    get_goal,
    load_goals,
)
from .joined_lane import (
    JoinedLaneCorruptError,
    JoinedLaneReconciler,
    OwnershipProbe,
    journal_provider_probe,
    ledger_provider_probe,
    ownership_provider_probe,
)
from .joined_lane import JoinedLaneStore as CanonicalJoinedLaneStore
from .journal.models import CanonicalEvent, ProgressClass, ProgressClassification
from .journal.store import EventJournal
from .provider_protocol import (
    CheckpointRequest,
    CloseRequest,
    CreateOrResumeRequest,
    Provider,
    ProviderState,
    ProviderStatus,
    SendRequest,
    UpdateKind,
)
from .rate_limit_state import Transaction
from .session_contract import (
    MAX_INLINE_WAKE_RECEIPTS,
    CloseArchiveResult,
    ContractValidationError,
    InterventionEvidence,
    JoinedLaneRecord,
    NextCheck,
    OperatingFact,
    OperationReference,
    PendingProviderOperation,
    ProgressEvidence,
    ProviderOperationResult,
    RecordTransitionKind,
    RecoveryState,
    WakeReceipt,
    canonical_digest,
    extend_wake_archive_digest,
    validate_close_result,
    validate_operation_result,
)
from .state_paths import state_dir

logger = structlog.get_logger(__name__)
SCHEMA = "chitra.pause_recovery.v1"


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
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
        values: dict[str, str] = {}
        for field in fields:
            value = payload.get(field)
            if not isinstance(value, str) or (not value.strip() and field != "resume_at"):
                raise ValueError(f"pause recovery record {field} must be a non-empty string")
            values[field] = value
        return cls(**values)


def recovery_records_path(root: Path | None = None) -> Path:
    return (state_dir() if root is None else root) / "pause_recovery.json"


@contextlib.contextmanager
def _recovery_lock(root: Path | None) -> Iterator[None]:
    path = recovery_records_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.parent / f".{path.name}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_recovery_records(root: Path | None = None) -> list[RecoveryRecord]:
    try:
        payload: Any = json.loads(recovery_records_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("pause_recovery.json is not a chitra.pause_recovery.v1 document")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("pause_recovery.json records must be a list")
    return [RecoveryRecord.from_dict(item) for item in records]


def _write_recovery_records(root: Path | None, records: list[RecoveryRecord]) -> None:
    write_json_atomic(
        recovery_records_path(root),
        {"schema": SCHEMA, "records": [record.to_dict() for record in records]},
        fsync=True,
    )


def _resume_note(goal: GoalRecord) -> str:
    current = goal.now.strip() or goal.intent.strip() or goal.goal.strip()
    return f"Goal at pause: {goal.goal.strip()} Current work: {current} Done when: {done_when_with_delta(goal).strip()}"


def record_pause_recovery(root: Path | None, txn: Transaction, *, paused_at: str) -> RecoveryRecord:
    if txn.phase != "held":
        raise ValueError("pause recovery can only be recorded for a held transaction")
    goal = get_goal(root, txn.session_ref)
    if goal is None:
        raise ValueError(f"cannot record pause recovery without a goal for {txn.session_ref}")
    required = [txn.session_ref, txn.hold_reason, txn.transcript_path, txn.created_at, paused_at]
    if not txn.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX):
        required.append(txn.resume_at)
    if not all(value.strip() for value in required):
        raise ValueError("held transaction is missing required pause recovery data")
    key = "\0".join((txn.session_ref, txn.hold_reason, txn.resume_at, txn.created_at))
    record = RecoveryRecord(
        pause_id=hashlib.sha256(key.encode()).hexdigest(),
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


RecoveryAction = Literal[
    "noop",
    "progress_confirmed",
    "nudge",
    "correct",
    "reframe",
    "compress",
    "checkpoint",
    "relaunch",
    "diagnostic",
    "waiting",
    "wake",
]


class RecoveryStateError(ValueError):
    pass


class RecoveryFactsReader(Protocol):
    def __call__(self, record: JoinedLaneRecord) -> Sequence[OperatingFact]: ...


class RecoveryProviderResolver(Protocol):
    def __call__(self, record: JoinedLaneRecord) -> Provider | None: ...


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
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
        return False


GovernedCloseAction = Literal["closed", "waiting"]


@dataclass(frozen=True, slots=True)
class GovernedCloseDecision:
    """Result of one restart-safe, provider close attempt."""

    action: GovernedCloseAction
    record: JoinedLaneRecord
    reason: str
    operation: PendingProviderOperation | None = None
    close_result: CloseArchiveResult | None = None
    user_ask: None = None

    @property
    def asks_user(self) -> bool:
        return False


ResumeAction = Literal["resumed", "waiting"]


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    """Result of an explicit resume of an archived provider session."""

    action: ResumeAction
    record: JoinedLaneRecord
    reason: str
    operation: PendingProviderOperation | None = None
    result: ProviderOperationResult | None = None

    @property
    def asks_user(self) -> bool:
        return False


SupervisorDecision = RecoveryDecision | GovernedCloseDecision


class RecoveryStateStore:
    """One-lane adapter over the canonical joined-lane store."""

    def __init__(self, state_root: Path, lane_id: str) -> None:
        self.lane_id = lane_id
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

    @property
    def root(self) -> Path:
        return self._store.root

    @property
    def control_path(self) -> Path:
        """One lock shared by every supervisor mutating this lane."""

        return self.root / "lane-control" / f"{self.lane_id}.lock"

    @contextlib.contextmanager
    def lane_control_lock(self) -> Iterator[None]:
        path = self.control_path
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def close_evidence_path(self, operation: PendingProviderOperation) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation.operation_id) is None:
            raise RecoveryStateError("close operation ID is unsafe for evidence storage")
        if self.root.is_symlink():
            raise RecoveryStateError("close evidence state root is a symlink")
        root = self.root
        directory = root / "close-evidence"
        if directory.is_symlink():
            raise RecoveryStateError("close evidence directory is a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{operation.operation_id}.json"
        if path.is_symlink():
            raise RecoveryStateError("close evidence path must not be a symlink")
        return path

    @staticmethod
    def _read_nofollow_json(path: Path) -> object:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
                return json.load(handle)
        finally:
            os.close(descriptor)

    def read_close_evidence(
        self, operation: PendingProviderOperation, record: JoinedLaneRecord
    ) -> CloseArchiveResult | None:
        try:
            self.close_evidence_path(operation)
            payload = read_json_rooted(self.root, "close-evidence", f"{operation.operation_id}.json")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecoveryStateError, ValueError):
            return None
        if not isinstance(payload, Mapping) or payload.get("schema") != "chitra.governed-close-evidence.v1":
            return None
        if payload.get("operation") != operation.model_dump(mode="json"):
            return None
        try:
            result = CloseArchiveResult.from_dict(payload.get("result"))
        except (ContractValidationError, TypeError, ValueError):
            return None
        return result if _close_receipt_matches(record, operation, result, self.root) else None

    def has_durable_close_evidence(
        self, record: JoinedLaneRecord, result: CloseArchiveResult
    ) -> bool:
        """Check that a stored terminal result was produced by this store."""

        if result.state not in {"closed", "archived"} or result.signature is None:
            return False
        try:
            self.close_evidence_path(
                PendingProviderOperation(
                    operation_id=result.operation_id,
                    kind="close",
                    lane_id=record.lane_id,
                    provider_handle=result.provider_handle,
                    idempotency_key=result.idempotency_key,
                    payload_digest=result.payload_digest,
                    provider_session_id=result.provider_session_id,
                    provider_instance_id=result.provider_instance_id,
                    provider_generation=result.provider_generation,
                    payload="stored-close-evidence",
                    created_at=result.observed_at,
                    attempt=1,
                )
            )
            payload = read_json_rooted(
                self.root, "close-evidence", f"{result.operation_id}.json"
            )
        except (
            ContractValidationError,
            OSError,
            RecoveryStateError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return False
        if not isinstance(payload, Mapping) or payload.get("schema") != "chitra.governed-close-evidence.v1":
            return False
        operation_payload = payload.get("operation")
        if (
            not isinstance(operation_payload, Mapping)
            or operation_payload.get("operation_id") != result.operation_id
        ):
            return False
        if any(
            operation_payload.get(field) != expected
            for field, expected in (
                ("lane_id", record.lane_id),
                ("provider_handle", record.provider.handle),
                ("provider_session_id", record.provider.provider_session_id),
                ("provider_instance_id", record.provider.instance_id),
                ("provider_generation", record.provider.generation),
            )
        ):
            return False
        operation_body = operation_payload.get("payload")
        if not isinstance(operation_body, str):
            return False
        try:
            close_body = json.loads(operation_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(close_body, Mapping) or any(
            close_body.get(field) != expected
            for field, expected in (
                ("lane_id", record.lane_id),
                ("goal_id", record.goal_id),
                ("goal_version", record.goal_version),
                ("session_ref", record.session_ref),
                ("provider_handle", record.provider.handle),
                ("provider_session_id", record.provider.provider_session_id),
                ("provider_instance_id", record.provider.instance_id),
                ("provider_generation", record.provider.generation),
            )
        ):
            return False
        if payload.get("result") != result.model_dump(mode="json"):
            return False
        if not _verify_mapping_signature(result.to_dict(), self.root):
            return False
        return (
            result.provider_thread_ref == record.provider.handle
            and result.provider_session_id == record.provider.provider_session_id
            and result.checkpoint_ref == record.checkpoint_reference
        )

    def write_close_evidence(
        self, operation: PendingProviderOperation, result: CloseArchiveResult
    ) -> None:
        if result.signature is None:
            unsigned = {key: value for key, value in result.to_dict().items() if key != "signature"}
            result = result.model_copy(update={"signature": _sign_mapping(unsigned, self.root)})
        elif not _verify_mapping_signature(result.to_dict(), self.root):
            raise RecoveryStateError("close evidence signature is invalid")
        self.close_evidence_path(operation)
        payload = {
            "schema": "chitra.governed-close-evidence.v1",
            "operation": operation.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
        }
        try:
            existing = read_json_rooted(self.root, "close-evidence", f"{operation.operation_id}.json")
        except FileNotFoundError:
            existing = None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryStateError("existing close evidence is unreadable") from exc
        if existing is not None:
            if existing != payload:
                raise RecoveryStateError("immutable close evidence changed")
            return
        try:
            write_json_rooted(self.root, "close-evidence", f"{operation.operation_id}.json", payload)
        except FileExistsError as exc:
            existing = read_json_rooted(self.root, "close-evidence", f"{operation.operation_id}.json")
            if existing != payload:
                raise RecoveryStateError("immutable close evidence changed") from exc

    def _wake_transaction_path(self) -> Path:
        return self.root / "wake-transactions" / f"{self.lane_id}.json"

    def _clear_wake_transaction(self) -> None:
        path = self._wake_transaction_path()
        try:
            path.unlink()
        except FileNotFoundError:
            return
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _recover_wake_transaction(self) -> None:
        """Finish a wake whose state/journal two-file write was interrupted."""

        path = self._wake_transaction_path()
        if not path.exists():
            return
        try:
            payload = self._read_nofollow_json(path)
            if not isinstance(payload, dict) or payload.get("schema") != "chitra.wake-transaction.v1":
                raise ValueError("wake transaction schema changed")
            candidate = JoinedLaneRecord.from_dict(payload.get("record"))
            receipt = WakeReceipt.from_dict(payload.get("wake"))
        except (ContractValidationError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryStateError(f"wake transaction is invalid: {exc}") from exc
        if candidate.lane_id != self.lane_id or receipt.lane_id != self.lane_id:
            raise RecoveryStateError("wake transaction lane identity changed")
        current = self._store.load(self.lane_id)
        if current is None:
            self._store.create(candidate)
        elif current.revision < candidate.revision:
            self._store.save(candidate, expected_revision=current.revision)
        EventJournal(self.root, self.lane_id).append_wakes((receipt,))
        self._clear_wake_transaction()

    def persist_wake(self, record: JoinedLaneRecord, receipt: WakeReceipt) -> JoinedLaneRecord:
        """Commit the wake receipt and joined-lane snapshot as one recoverable transaction."""

        if record.lane_id != self.lane_id or receipt.lane_id != self.lane_id:
            raise RecoveryStateError("wake transaction lane identity changed")
        self._recover_wake_transaction()
        current = self._store.load(self.lane_id)
        next_revision = record.revision if current is None else current.revision + 1
        candidate = record.model_copy(update={"revision": next_revision})
        marker = {"schema": "chitra.wake-transaction.v1", "record": candidate.to_dict(), "wake": receipt.to_dict()}
        marker_path = self._wake_transaction_path()
        if marker_path.exists():
            try:
                existing = self._read_nofollow_json(marker_path)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RecoveryStateError("wake transaction marker is unreadable") from exc
            if existing != marker:
                raise RecoveryStateError("wake transaction identity changed")
        else:
            write_json_atomic(marker_path, marker, fsync=True)
        saved = (
            self._store.create(candidate)
            if current is None
            else self._store.save(candidate, expected_revision=current.revision)
        )
        EventJournal(self.root, self.lane_id).append_wakes((receipt,))
        self._clear_wake_transaction()
        return cast(JoinedLaneRecord, saved)

    def load(self) -> JoinedLaneRecord | None:
        self._recover_wake_transaction()
        try:
            return cast(JoinedLaneRecord | None, self._store.load(self.lane_id))
        except JoinedLaneCorruptError as exc:
            raise RecoveryStateError(str(exc)) from exc

    def save(self, record: JoinedLaneRecord, *, transition: RecordTransitionKind = "steady") -> JoinedLaneRecord:
        if record.lane_id != self.lane_id:
            raise ValueError("joined lane record lane_id does not match this store")
        current = self.load()
        if current is None:
            return cast(JoinedLaneRecord, self._store.create(record))
        if record.revision != current.revision:
            raise RecoveryStateError(f"stale joined-lane recovery snapshot for {self.lane_id!r}")
        candidate = record.model_copy(update={"revision": max(record.revision, current.revision + 1)})
        try:
            return cast(
                JoinedLaneRecord,
                self._store.save(candidate, expected_revision=current.revision, transition=transition),
            )
        except (TypeError, ValueError) as exc:
            raise RecoveryStateError(f"canonical joined-lane save failed: {exc}") from exc


LaneRecordStore = RecoveryStateStore
JoinedLaneStore = CanonicalJoinedLaneStore


def _now(value: datetime | str | None = None) -> datetime:
    current = (
        datetime.now(UTC)
        if value is None
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if current.tzinfo is None:
        raise ValueError("recovery timestamps must include a timezone")
    return current.astimezone(UTC)


RECOVERY_HANDOFF_SCHEMA = "chitra.recovery-handoff.v1"
RECOVERY_REFRAME_AFTER_ATTEMPTS = 5


def _mapping(value: object, name: str) -> Mapping[str, object]:
    """Project a provider boundary result without accepting arbitrary objects."""

    if isinstance(value, Mapping):
        return value
    for method_name in ("model_dump", "to_dict"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            dumped = method(mode="json") if method_name == "model_dump" else method()
        except TypeError:
            dumped = method()
        if isinstance(dumped, Mapping):
            return dumped
    raise ValueError(f"{name} must be an object")


def _sign_mapping(payload: Mapping[str, object], state_root: Path) -> str:
    """Sign Chitra-owned evidence with the local checkpoint key."""

    return sign_checkpoint_receipt(dict(payload), key=load_or_create_checkpoint_key(state_root))


def _verify_mapping_signature(payload: Mapping[str, object], state_root: Path) -> bool:
    try:
        return verify_checkpoint_receipt_signature(dict(payload), state_root=state_root)
    except (OSError, TypeError, ValueError):
        return False


def _resume_receipt_hmac(receipt: Mapping[str, object], token: str) -> str:
    """Bind a Fleet response to the bearer challenge without shared keys."""

    unsigned = {
        key: value for key, value in receipt.items() if key not in {"receipt_hmac", "signature"}
    }
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(token.encode(), encoded, "sha256").hexdigest()


def _close_session(record: JoinedLaneRecord) -> str:
    session = record.provider.provider_session_id
    if not session:
        raise RecoveryStateError("governed close requires an exact physical provider session ID")
    return session


def _close_receipt_matches(
    record: JoinedLaneRecord,
    operation: PendingProviderOperation,
    result: CloseArchiveResult,
    state_root: Path | None = None,
) -> bool:
    if result.state not in {"closed", "archived"}:
        return False
    try:
        validate_close_result(operation, result)
    except (ContractValidationError, TypeError, ValueError):
        return False
    return (
        result.provider_thread_ref == record.provider.handle
        and result.provider_session_id == _close_session(record)
        and result.checkpoint_ref == record.checkpoint_reference
        and (
            state_root is None
            or (result.signature is not None and _verify_mapping_signature(result.to_dict(), state_root))
        )
    )


def _resume_receipt_matches(
    record: JoinedLaneRecord,
    close: CloseArchiveResult,
    operation: PendingProviderOperation,
    result: ProviderOperationResult,
    *,
    state_root: Path,
) -> bool:
    """Require positive evidence for the exact reopened physical session."""

    receipt = result.reopen_receipt
    if receipt is None or result.status != "consumed":
        return False
    # A legacy receipt without a closed-owner binding or Chitra signature is
    # not proof of a same-session reopen. It must remain pending.
    if close.owner_process is None or receipt.signature is None:
        return False
    if not _verify_mapping_signature(receipt.to_dict(), state_root):
        return False
    token = _resume_auth_token(record, close, operation, state_root=state_root)
    if receipt.receipt_hmac is None or receipt.receipt_hmac != _resume_receipt_hmac(receipt.to_dict(), token):
        return False
    return (
        receipt.operation_id == operation.operation_id
        and receipt.close_operation_id == close.operation_id
        and receipt.lane_id == record.lane_id
        and receipt.session_ref == record.session_ref
        and receipt.provider_session_id == _close_session(record)
        and receipt.provider_handle == record.provider.handle
        and receipt.provider_instance_id == record.provider.instance_id
        and receipt.provider_generation == record.provider.generation
        and receipt.checkpoint_ref == record.checkpoint_reference
        and receipt.prior_owner_process == close.owner_process
        and receipt.created_new_lane is False
        and receipt.created_new_session is False
        and receipt.owner_process != receipt.prior_owner_process
        and receipt.auth_token == token
    )


def _resume_auth_token(
    record: JoinedLaneRecord,
    close: CloseArchiveResult,
    operation: PendingProviderOperation,
    *,
    state_root: Path | None = None,
) -> str:
    """Create a deterministic challenge bound to one close/resume envelope."""

    root = state_root
    if root is None:
        raise RecoveryStateError("resume authentication requires Chitra state")
    body = {
        "schema": "chitra.lane-resume-auth.v1",
        "operation_id": operation.operation_id,
        "close_operation_id": close.operation_id,
        "lane_id": record.lane_id,
        "session_ref": record.session_ref,
        "provider_session_id": record.provider.provider_session_id,
        "provider_handle": record.provider.handle,
        "provider_instance_id": record.provider.instance_id,
        "provider_generation": record.provider.generation,
        "checkpoint_ref": record.checkpoint_reference,
        "owner_process": close.owner_process.model_dump(mode="json") if close.owner_process is not None else None,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(load_or_create_checkpoint_key(root), encoded, "sha256").hexdigest()


class RecoveryEngine:
    """Advance one bounded action through canonical provider and lane state."""

    def __init__(
        self,
        *,
        provider: Provider | None = None,
        state_root: Path | None = None,
        state_store: RecoveryStateStore | None = None,
        goal_root: Path | None = None,
        journal: EventJournal | None = None,
        facts_reader: RecoveryFactsReader | None = None,
        response_ladder: ResponseLadder | None = None,
        reconciler: JoinedLaneReconciler | None = None,
        ownership_probe: OwnershipProbe | None = None,
        check_interval: timedelta = timedelta(minutes=5),
        wait_interval: timedelta = timedelta(minutes=30),
    ) -> None:
        self.provider = provider
        self.state_root = state_root
        self._state_store = state_store
        self.goal_root = goal_root
        self.journal = journal
        self.facts_reader = facts_reader
        self.response_ladder = response_ladder
        self.reconciler = reconciler
        self.ownership_probe = ownership_probe
        self.check_interval = check_interval
        self.wait_interval = wait_interval

    def store_for(self, lane_id: str) -> RecoveryStateStore | None:
        return self._state_store or (RecoveryStateStore(self.state_root, lane_id) if self.state_root is not None else None)

    def load(self, lane_id: str) -> JoinedLaneRecord | None:
        store = self.store_for(lane_id)
        return store.load() if store is not None else None

    def _goal_for(self, record: JoinedLaneRecord) -> GoalRecord | None:
        if self.goal_root is None:
            return None
        direct = get_goal(self.goal_root, record.session_ref)
        if direct is not None:
            return direct
        return next(
            (
                goal
                for goal in load_goals(self.goal_root)
                if (goal.goal_id, goal.lane_id) == (record.goal_id, record.lane_id)
            ),
            None,
        )

    def _persist(
        self,
        record: JoinedLaneRecord,
        *,
        persist: bool,
        transition: RecordTransitionKind = "steady",
    ) -> JoinedLaneRecord:
        store = self.store_for(record.lane_id)
        return store.save(record, transition=transition) if persist and store is not None else record

    def _invoke_close(
        self,
        record: JoinedLaneRecord,
        operation: PendingProviderOperation,
        checkpoint: Mapping[str, object],
    ) -> CloseArchiveResult | None:
        """Send one explicit Chitra checkpoint handoff to the provider."""

        if self.provider is None:
            return None
        request = CloseRequest(
            operation=operation,
            archive=True,
            checkpoint_receipt=checkpoint,
            checkpoint_receipt_sha256=canonical_digest(checkpoint),
            checkpoint_verifier="chitra.detect.rescue.verify_checkpoint_receipt_signature",
        )
        try:
            if isinstance(getattr(self.provider, "capabilities", None), Mapping):
                raw = self.provider.close(
                    cast(
                        Any,
                        {
                            "operation": operation.model_dump(mode="json"),
                            "archive": True,
                            "payload": json.loads(operation.payload),
                            "checkpoint_ref": checkpoint.get("checkpoint_ref"),
                            "checkpoint_receipt": dict(checkpoint),
                            "checkpoint_receipt_sha256": canonical_digest(checkpoint),
                            "checkpoint_verifier": "chitra.detect.rescue.verify_checkpoint_receipt_signature",
                        },
                    )
                )
            else:
                raw = self.provider.close(request)
            values = dict(_mapping(raw, "provider close result"))
            values.pop("kind", None)
            result = CloseArchiveResult.from_dict(values)
            if self.state_root is None:
                return None
            if result.signature is not None and not _verify_mapping_signature(result.to_dict(), self.state_root):
                return None
            if result.signature is None:
                unsigned = {key: value for key, value in result.to_dict().items() if key != "signature"}
                result = result.model_copy(update={"signature": _sign_mapping(unsigned, self.state_root)})
            if not _close_receipt_matches(record, operation, result, self.state_root):
                return None
            return result
        except (ContractValidationError, TypeError, ValueError, OSError):
            return None
        except Exception:  # provider outage or lost response remains pending
            return None

    def _finish_close(
        self, record: JoinedLaneRecord, result: CloseArchiveResult, *, persist: bool
    ) -> JoinedLaneRecord:
        closed = record.model_copy(
            update={
                "lifecycle": "inactive",
                "pending_operation": None,
                "last_close_result": result,
                "recovery": record.recovery.model_copy(
                    update={"stage": "complete", "pending_payload": None}
                ),
                "next_check": None,
            }
        )
        return self._persist(closed, persist=persist)

    @staticmethod
    def _close_wait(
        record: JoinedLaneRecord,
        reason: str,
        operation: PendingProviderOperation | None = None,
        result: CloseArchiveResult | None = None,
    ) -> GovernedCloseDecision:
        return GovernedCloseDecision(
            action="waiting", record=record, reason=reason, operation=operation, close_result=result
        )

    def _cycle_id(self, record: JoinedLaneRecord, signature: str, current: datetime) -> str:
        return "cycle-" + canonical_digest(
            (
                record.lane_id,
                record.goal_id,
                record.goal_version,
                signature,
                record.revision,
                len(record.operation_history),
                current.isoformat(),
            )
        )[:24]

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
        signature = failure_signature.strip()
        if not signature:
            raise ValueError("failure_signature must be non-empty")
        current = _now(now)
        same = record.recovery.stage not in {"none", "complete"} and record.recovery.failure_signature == signature
        if record.pending_operation is not None:
            # A new detector signature is not permission to replace the
            # operation already in flight.  Keep its exact tactical action
            # and payload so a restart can reconcile the same mutation.
            recovery = record.recovery.model_copy(
                update={
                    "failure_signature": signature,
                    "next_allowed_attempt": current.isoformat(),
                }
            )
        elif same:
            # Scheduling another check does not start a new recovery cycle.
            # Preserve the tactical overlay, pending payload, and handoff
            # anchor so a restart resumes the same work.
            recovery = record.recovery.model_copy(
                update={
                    "stage": "confirm",
                    "failure_signature": signature,
                    "next_allowed_attempt": current.isoformat(),
                }
            )
        else:
            recovery = RecoveryState(
                stage="confirm",
                cycle_id=self._cycle_id(record, signature, current),
                failure_signature=signature,
                next_allowed_attempt=current.isoformat(),
            )
        default_condition = "a material update for the same logical lane"
        default_reason = "Confirm whether the lane made useful progress"
        condition = wake_condition.strip() or default_condition
        if same and condition == default_condition and record.next_check is not None:
            condition = record.next_check.wake_condition or condition
        check_reason = reason
        if same and reason == default_reason and record.next_check is not None:
            check_reason = record.next_check.reason or reason
        candidate = record.model_copy(
            update={
                "recovery": recovery,
                "next_check": NextCheck(at=current.isoformat(), reason=check_reason, wake_condition=condition),
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
        events: Sequence[CanonicalEvent] = (),
        progress_rows: Sequence[ProgressClassification] = (),
        wake_event: bool = False,
        wake_id: str | None = None,
        observed_wake_condition: str | None = None,
        wake_event_sequence: int | None = None,
        goal: GoalRecord | None = None,
        finding: Finding | None = None,
        persist: bool = True,
    ) -> RecoveryDecision:
        current = _now(now)
        working = record
        if failure_signature is not None and (
            not working.recovery.failure_signature
            or working.recovery.stage in {"none", "complete"}
            or working.recovery.failure_signature != failure_signature
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
            return self._decision("noop", working, "lane is not active")
        if working.recovery.stage not in {"none", "complete"} and working.recovery.cycle_id is None:
            migrated = working.recovery.model_copy(
                update={
                    "cycle_id": self._cycle_id(working, working.recovery.failure_signature or "legacy-recovery", current),
                }
            )
            working = self._persist(working.model_copy(update={"recovery": migrated}), persist=persist)
        named_wake = self._named_wake(working, wake_id, observed_wake_condition, wake_event_sequence)
        if working.next_check is not None and _now(working.next_check.at) > current and not named_wake:
            return self._decision("noop", working, "scheduled check is not due")
        del wake_event
        if named_wake:
            assert wake_id is not None and observed_wake_condition is not None and wake_event_sequence is not None
            # Record exact wake evidence before consulting the goal store. A
            # goal-store outage must not discard a proven wake or make the
            # supervisor rediscover it on every pass.
            working = self._record_wake(working, wake_id, observed_wake_condition, wake_event_sequence, current, persist)
            recovery = working.recovery.model_copy(
                update={
                    "stage": "confirm",
                    "next_allowed_attempt": current.isoformat(),
                }
            )
            working = self._persist(
                working.model_copy(
                    update={
                        "recovery": recovery,
                        "next_check": NextCheck(
                            at=current.isoformat(),
                            reason="Named wake condition changed; confirm useful progress",
                            wake_condition=observed_wake_condition,
                        ),
                    }
                ),
                persist=persist,
            )
        resolved_goal = goal or self._goal_for(working)
        if resolved_goal is None:
            return self._wait(working, current, "the enrolled goal record becomes readable", persist)
        if (resolved_goal.lane_id, resolved_goal.goal_id, resolved_goal.goal_version) != (
            working.lane_id,
            working.goal_id,
            working.goal_version,
        ):
            return self._wait(working, current, "the exact enrolled lane, goal, and goal version reconcile", persist)
        refreshed_facts = tuple(facts)
        if not refreshed_facts and self.facts_reader is not None:
            try:
                refreshed_facts = tuple(self.facts_reader(working))
            except Exception as exc:  # noqa: BLE001
                logger.warning("recovery_facts_unavailable", lane_id=working.lane_id, error=str(exc))
        evidence = self._latest_progress(working, events, progress_rows)
        if evidence is not None and self._new_progress(working.last_useful_progress, evidence):
            recovery = working.recovery.model_copy(update={"stage": "complete", "next_allowed_attempt": None})
            candidate = self._persist(
                working.model_copy(update={"last_useful_progress": evidence, "recovery": recovery, "next_check": None}),
                persist=persist,
            )
            return RecoveryDecision("progress_confirmed", "complete", candidate, "material progress confirmed")
        if working.pending_operation is not None:
            return self._reconcile_pending(working, resolved_goal, current, refreshed_facts, persist)
        stage = working.recovery.stage
        if stage == "waiting":
            if (
                working.recovery.attempt_count >= RECOVERY_REFRAME_AFTER_ATTEMPTS
                and not working.recovery.execution_plan
                and working.recovery.failure_signature
            ):
                return self._reframe(working, resolved_goal, current, refreshed_facts, persist)
            return self._wait(working, current, working.wake_condition or "a material lane update is observed", persist)
        if stage in {"none", "complete", "confirm"}:
            return self._ladder_hold(working, finding, "nudge") or self._send(
                working, resolved_goal, current, "nudge", refreshed_facts, persist
            )
        if stage == "correct":
            return self._ladder_hold(working, finding, "correct") or self._send(
                working, resolved_goal, current, "correct", refreshed_facts, persist
            )
        if stage == "relaunch" and working.recovery.attempted_remedy == "reframe" and working.checkpoint_reference is None:
            return self._compress(working, resolved_goal, current, refreshed_facts, persist)
        if stage == "relaunch" and working.checkpoint_reference is None:
            return self._ladder_hold(working, finding, "checkpoint") or self._run_operation(
                working, current, "checkpoint", f"recovery-{working.lane_id}-{working.recovery.cycle_id}", refreshed_facts, persist
            )
        if stage == "relaunch":
            if working.recovery.execution_plan and not self._handoff_valid(working, resolved_goal):
                return self._wait(working, current, "the durable context handoff validates for this enrolled goal", persist)
            return self._ladder_hold(working, finding, "relaunch") or self._run_operation(
                working,
                current,
                "relaunch",
                f"Resume from governed checkpoint {working.checkpoint_reference}.",
                refreshed_facts,
                persist,
            )
        if stage == "diagnostic":
            return self._run_operation(
                working,
                current,
                "diagnostic",
                "Run one bounded diagnostic in this lane through the normal provider send operation. Do not ask the user.",
                refreshed_facts,
                persist,
            )
        return self._wait(working, current, "a valid recovery stage is observed", persist)

    check = run_once
    tick = run_once

    def _decision(
        self,
        action: RecoveryAction,
        record: JoinedLaneRecord,
        reason: str,
        operation: PendingProviderOperation | None = None,
        message: str = "",
        facts: tuple[OperatingFact, ...] = (),
    ) -> RecoveryDecision:
        return RecoveryDecision(action, record.recovery.stage, record, reason, operation, message, facts, record.wake_condition)

    def _check(self, current: datetime, reason: str, condition: str, waiting: bool = False) -> NextCheck:
        return NextCheck(
            at=(current + (self.wait_interval if waiting else self.check_interval)).isoformat(),
            reason=reason,
            wake_condition=condition,
        )

    def _wait(self, record: JoinedLaneRecord, current: datetime, condition: str, persist: bool) -> RecoveryDecision:
        check = self._check(current, "No new safe tactic; keep the goal open", condition, True)
        recovery = record.recovery.model_copy(update={"stage": "waiting", "next_allowed_attempt": check.at})
        candidate = self._persist(record.model_copy(update={"recovery": recovery, "next_check": check}), persist=persist)
        return self._decision("waiting", candidate, "no new safe tactic; durable waiting is armed")

    def _pending(self, record: JoinedLaneRecord, current: datetime, reason: str, persist: bool) -> RecoveryDecision:
        operation = record.pending_operation
        assert operation is not None
        condition = f"exact consumption evidence for recovery operation {operation.operation_id}"
        candidate = self._persist(
            record.model_copy(update={"next_check": self._check(current, reason, condition)}),
            persist=persist,
        )
        return self._decision(cast(RecoveryAction, record.recovery.attempted_remedy), candidate, reason, operation)

    def _named_wake(
        self,
        record: JoinedLaneRecord,
        wake_id: str | None,
        condition: str | None,
        sequence: int | None,
    ) -> bool:
        known = {receipt.wake_id for receipt in record.wake_receipts if receipt.goal_version == record.goal_version}
        journal = self.journal or (EventJournal(self.state_root, record.lane_id) if self.state_root is not None else None)
        if journal is None:
            return False
        try:
            # A journal row without the corresponding joined-lane snapshot
            # is an interrupted transaction, not an already-consumed wake.
            # Older rows remain covered after inline compaction because their
            # event sequence is at or below the newest committed inline wake.
            committed_sequence = max(
                (receipt.event_sequence for receipt in record.wake_receipts if receipt.goal_version == record.goal_version),
                default=0,
            )
            known.update(
                receipt.wake_id
                for receipt in journal.load_wakes()
                if receipt.goal_version == record.goal_version and receipt.event_sequence <= committed_sequence
            )
            proven = bool(
                wake_id
                and condition
                and sequence is not None
                and journal.proves_named_wake(
                    wake_id=wake_id,
                    event_sequence=sequence,
                    goal_id=record.goal_id,
                    session_ref=record.session_ref,
                    goal_version=record.goal_version,
                    wake_condition=condition,
                )
            )
        except (OSError, ValueError):
            return False
        return bool(
            wake_id
            and condition
            and condition == record.wake_condition
            and isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence > 0
            and wake_id not in known
            and proven
        )

    def _record_wake(
        self,
        record: JoinedLaneRecord,
        wake_id: str,
        condition: str,
        sequence: int,
        current: datetime,
        persist: bool,
    ) -> JoinedLaneRecord:
        receipt = WakeReceipt(
            wake_id=wake_id,
            lane_id=record.lane_id,
            goal_id=record.goal_id,
            session_ref=record.session_ref,
            goal_version=record.goal_version,
            wake_condition=condition,
            event_sequence=sequence,
            observed_at=current.isoformat(),
        )
        intervention = InterventionEvidence(
            operation_id=wake_id,
            action="Wake condition observed",
            consumed=True,
            useful_work_resumed=None,
            observed_at=current.isoformat(),
        )
        journal = self.journal or (EventJournal(self.state_root, record.lane_id) if self.state_root is not None else None)
        if persist and journal is None:
            raise RecoveryStateError("named wake requires durable canonical journal storage")
        receipts = (*record.wake_receipts, receipt)
        archive_count = record.wake_archive_count
        archive_digest = record.wake_archive_digest
        if len(receipts) > MAX_INLINE_WAKE_RECEIPTS:
            removed = receipts[: len(receipts) - MAX_INLINE_WAKE_RECEIPTS]
            receipts = receipts[len(removed) :]
            for archived in removed:
                archive_digest = extend_wake_archive_digest(archive_digest, archived)
            archive_count += len(removed)
        candidate = record.model_copy(
            update={
                "wake_receipts": receipts,
                "wake_archive_count": archive_count,
                "wake_archive_digest": archive_digest,
                "last_intervention": intervention,
            }
        )
        if persist:
            store = self.store_for(record.lane_id)
            if store is None:
                raise RecoveryStateError("named wake requires durable canonical lane storage")
            return store.persist_wake(candidate, receipt)
        return candidate

    def _journal_events(self, supplied: Sequence[CanonicalEvent] = ()) -> tuple[CanonicalEvent, ...]:
        if supplied:
            return tuple(supplied)
        if self.journal is None:
            return ()
        try:
            return tuple(self.journal.load())
        except (OSError, ValueError):
            return ()

    def _latest_progress(
        self,
        record: JoinedLaneRecord,
        events: Sequence[CanonicalEvent],
        rows: Sequence[ProgressClassification],
    ) -> ProgressEvidence | None:
        source = self._journal_events(events)
        if not rows and self.journal is not None:
            try:
                rows = tuple(self.journal.load_progress())
            except (OSError, ValueError):
                rows = ()
        progress_ids = {
            event_id
            for row in rows
            if row.classification is ProgressClass.PROGRESS and row.goal_version == str(record.goal_version)
            for event_id in row.source_event_ids
        }
        candidates: list[tuple[int, CanonicalEvent]] = []
        for sequence, event in enumerate(source, 1):
            if not isinstance(event, CanonicalEvent):
                continue
            if (event.lane, event.goal_ref, event.session_id) != (record.lane_id, record.goal_id, record.session_ref):
                continue
            if event.goal_version != record.goal_version:
                continue
            marker = event.payload.get("progress_evidence")
            explicit = isinstance(marker, dict) and any(
                marker.get(key) is True
                for key in (
                    "artifact_changed",
                    "diagnostic_changed",
                    "required_item_verified",
                    "targeted_check_flipped",
                    "live_boundary_exercised",
                )
            )
            if explicit or event.event_id in progress_ids:
                candidates.append((sequence, event))
        if not candidates:
            return None
        sequence, event = candidates[-1]
        summary = event.payload.get("summary")
        return ProgressEvidence(
            update_sequence=sequence,
            summary=summary.strip()[:500] if isinstance(summary, str) and summary.strip() else "material progress in the canonical journal",
            observed_at=event.observed_at,
            evidence_ref=event.event_id,
        )

    @staticmethod
    def _new_progress(previous: ProgressEvidence | None, current: ProgressEvidence) -> bool:
        return previous is None or (current.evidence_ref != previous.evidence_ref and current.update_sequence > previous.update_sequence)

    @staticmethod
    def _immutable_goal_payload(goal: GoalRecord) -> dict[str, object]:
        payload = goal.to_dict()
        return {
            field: payload.get(field)
            for field in (
                "goal_id",
                "lane_id",
                "session_ref",
                "goal_version",
                "status",
                "goal",
                "done_when",
                "source",
                "enrolled_at",
                "created_at",
                "intent",
                "scope",
                "needs",
                "enrolled_done_when",
                "enrolled_done_when_items",
                "interview_receipt",
            )
        }

    @staticmethod
    def _roadmap_snapshot(record: JoinedLaneRecord) -> dict[str, object] | None:
        return record.current_update.to_dict() if record.current_update is not None else None

    @staticmethod
    def _operation_snapshot(operation: PendingProviderOperation | None) -> dict[str, object] | None:
        return operation.to_dict() if operation is not None else None

    @staticmethod
    def _next_check_snapshot(record: JoinedLaneRecord) -> dict[str, object] | None:
        return record.next_check.to_dict() if record.next_check is not None else None

    @staticmethod
    def _recovery_snapshot(record: JoinedLaneRecord) -> dict[str, object]:
        recovery = record.recovery
        return {
            "stage": recovery.stage,
            "cycle_id": recovery.cycle_id,
            "event_sequence": recovery.event_sequence,
            "failure_signature": recovery.failure_signature,
            "attempted_remedy": recovery.attempted_remedy,
            "attempt_count": recovery.attempt_count,
            "next_allowed_attempt": recovery.next_allowed_attempt,
            "pending_payload": recovery.pending_payload,
            "execution_objective": recovery.execution_objective,
            "execution_plan": list(recovery.execution_plan),
        }

    @staticmethod
    def _handoff_id(record: JoinedLaneRecord) -> str:
        cycle_id = record.recovery.cycle_id
        if cycle_id is None:
            raise RecoveryStateError("context compression requires a recovery cycle")
        return f"{record.lane_id}-{cycle_id}-context"

    @classmethod
    def _handoff_reference_for_id(cls, handoff_id: str) -> str:
        return f"recovery-handoffs/{handoff_id}.json"

    @classmethod
    def _handoff_reference(cls, record: JoinedLaneRecord) -> str:
        return cls._handoff_reference_for_id(cls._handoff_id(record))

    def _handoff_path(self, reference: str) -> Path:
        if self.state_root is None:
            raise RecoveryStateError("context compression requires a durable state root")
        parts = Path(reference).parts
        if len(parts) != 2 or parts[0] != "recovery-handoffs" or parts[1] in {"", ".", ".."}:
            raise RecoveryStateError("recovery handoff reference is not a safe relative path")
        root = self.state_root.resolve()
        directory = (root / parts[0]).resolve()
        path = (directory / parts[1]).resolve()
        if directory.parent != root or path.parent != directory:
            raise RecoveryStateError("recovery handoff path escaped the state root")
        return path

    def _handoff_binding(
        self, record: JoinedLaneRecord, goal: GoalRecord, *, handoff_id: str
    ) -> dict[str, object]:
        immutable_goal = self._immutable_goal_payload(goal)
        roadmap = self._roadmap_snapshot(record)
        provider = record.provider.to_dict()
        return {
            "schema": RECOVERY_HANDOFF_SCHEMA,
            "handoff_id": handoff_id,
            "lane_id": record.lane_id,
            "goal_id": record.goal_id,
            "goal_version": record.goal_version,
            "session_ref": record.session_ref,
            "physical_session_generation": record.physical_session_generation,
            "cycle_id": record.recovery.cycle_id,
            "immutable_goal": immutable_goal,
            "immutable_goal_digest": canonical_digest(immutable_goal),
            "execution_objective": record.recovery.execution_objective,
            "execution_plan": list(record.recovery.execution_plan),
            "roadmap_snapshot": roadmap,
            "roadmap_digest": canonical_digest(roadmap),
            "provider_identity": provider,
            "provider_digest": canonical_digest(provider),
            "checkpoint_reference": record.checkpoint_reference,
            "pending_operation": self._operation_snapshot(record.pending_operation),
            "next_check": self._next_check_snapshot(record),
            "wake_receipts": [item.to_dict() for item in record.wake_receipts],
            "wake_archive_count": record.wake_archive_count,
            "wake_archive_digest": record.wake_archive_digest,
            "plan_assessment": record.plan_assessment.to_dict(),
            "last_useful_progress": (
                record.last_useful_progress.to_dict() if record.last_useful_progress is not None else None
            ),
            "recovery_state": self._recovery_snapshot(record),
        }

    def _handoff_payload(self, record: JoinedLaneRecord, goal: GoalRecord, current: datetime, handoff_id: str) -> dict[str, object]:
        return {
            **self._handoff_binding(record, goal, handoff_id=handoff_id),
            "resume": record.to_dict(),
            "created_at": current.isoformat(),
        }

    def _handoff_body_valid(
        self,
        document: dict[str, object],
        record: JoinedLaneRecord,
        goal: GoalRecord,
        *,
        handoff_id: str,
        expected_digest: str | None = None,
    ) -> bool:
        stored_digest = document.get("handoff_sha256")
        body = dict(document)
        body.pop("handoff_sha256", None)
        if not isinstance(stored_digest, str) or stored_digest != canonical_digest(body):
            return False
        if expected_digest is not None and stored_digest != expected_digest:
            return False
        binding = self._handoff_binding(record, goal, handoff_id=handoff_id)
        stable = (
            "schema",
            "handoff_id",
            "lane_id",
            "goal_id",
            "goal_version",
            "session_ref",
            "physical_session_generation",
            "cycle_id",
            "immutable_goal",
            "immutable_goal_digest",
            "execution_objective",
            "execution_plan",
            "roadmap_snapshot",
            "roadmap_digest",
            "provider_identity",
            "provider_digest",
        )
        if any(body.get(key) != binding[key] for key in stable):
            return False
        resume = body.get("resume")
        if not isinstance(resume, dict):
            return False
        try:
            snapshot = JoinedLaneRecord.from_dict(resume)
        except (ContractValidationError, TypeError, ValueError):
            return False
        if (
            snapshot.lane_id,
            snapshot.goal_id,
            snapshot.goal_version,
            snapshot.session_ref,
            snapshot.physical_session_generation,
        ) != (
            record.lane_id,
            record.goal_id,
            record.goal_version,
            body.get("session_ref"),
            body.get("physical_session_generation"),
        ):
            return False
        if snapshot.provider.to_dict() != body.get("provider_identity"):
            return False
        if self._roadmap_snapshot(snapshot) != body.get("roadmap_snapshot"):
            return False
        if snapshot.checkpoint_reference != body.get("checkpoint_reference"):
            return False
        if self._operation_snapshot(snapshot.pending_operation) != body.get("pending_operation"):
            return False
        if self._next_check_snapshot(snapshot) != body.get("next_check"):
            return False
        if [item.to_dict() for item in snapshot.wake_receipts] != body.get("wake_receipts"):
            return False
        if snapshot.wake_archive_count != body.get("wake_archive_count"):
            return False
        if snapshot.wake_archive_digest != body.get("wake_archive_digest"):
            return False
        if snapshot.plan_assessment.to_dict() != body.get("plan_assessment"):
            return False
        if (snapshot.last_useful_progress.to_dict() if snapshot.last_useful_progress is not None else None) != body.get(
            "last_useful_progress"
        ):
            return False
        return self._recovery_snapshot(snapshot) == body.get("recovery_state")

    def _write_context_handoff(
        self, record: JoinedLaneRecord, goal: GoalRecord, current: datetime, persist: bool
    ) -> JoinedLaneRecord:
        if self.state_root is None:
            raise RecoveryStateError("context compression requires a durable state root")
        handoff_id = record.recovery.handoff_id or self._handoff_id(record)
        if handoff_id != self._handoff_id(record):
            raise RecoveryStateError("recovery handoff ID does not match its recovery cycle")
        reference = record.recovery.handoff_reference or self._handoff_reference_for_id(handoff_id)
        if reference != self._handoff_reference_for_id(handoff_id):
            raise RecoveryStateError("recovery handoff reference does not match its handoff ID")
        path = self._handoff_path(reference)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError) as exc:
                raise RecoveryStateError(f"durable context handoff cannot be read: {exc}") from exc
            if not isinstance(existing, dict) or not self._handoff_body_valid(
                existing, record, goal, handoff_id=handoff_id, expected_digest=record.recovery.handoff_digest
            ):
                raise RecoveryStateError("durable context handoff identity or contents changed")
            digest = existing.get("handoff_sha256")
        else:
            if record.recovery.handoff_id is not None or record.recovery.handoff_digest is not None:
                raise RecoveryStateError("durable context handoff reference is missing")
            payload = self._handoff_payload(record, goal, current, handoff_id)
            digest = canonical_digest(payload)
            write_json_atomic(path, {**payload, "handoff_sha256": digest}, fsync=True)
        if not isinstance(digest, str):
            raise RecoveryStateError("durable context handoff has no digest")
        recovery = record.recovery.model_copy(
            update={"handoff_id": handoff_id, "handoff_reference": reference, "handoff_digest": digest}
        )
        return self._persist(record.model_copy(update={"recovery": recovery}), persist=persist)

    def _handoff_valid(self, record: JoinedLaneRecord, goal: GoalRecord) -> bool:
        handoff_id = record.recovery.handoff_id
        reference = record.recovery.handoff_reference
        expected_digest = record.recovery.handoff_digest
        if (
            not handoff_id
            or not reference
            or not expected_digest
            or self.state_root is None
            or handoff_id != self._handoff_id(record)
            or reference != self._handoff_reference_for_id(handoff_id)
        ):
            return False
        try:
            payload = json.loads(self._handoff_path(reference).read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, RecoveryStateError):
            return False
        return isinstance(payload, dict) and self._handoff_body_valid(
            payload, record, goal, handoff_id=handoff_id, expected_digest=expected_digest
        )

    def _reframe(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        current: datetime,
        facts: tuple[OperatingFact, ...],
        persist: bool,
    ) -> RecoveryDecision:
        if record.recovery.execution_plan:
            return self._wait(record, current, "material progress after the existing tactical reframe", persist)
        if not persist or self.store_for(record.lane_id) is None:
            raise RecoveryStateError("goal reframe requires durable joined-lane storage")
        next_action = (
            record.current_update.next_action.strip()
            if record.current_update is not None and record.current_update.next_action.strip()
            else "the next in-scope action recorded by the lane"
        )
        current_action = (
            record.current_update.current_action.strip()
            if record.current_update is not None and record.current_update.current_action.strip()
            else "Re-read the current lane state"
        )
        objective = f"Unblock the enrolled lane by completing the next in-scope action: {next_action}"
        plan = (
            f"Re-read the stalled lane state after {current_action}",
            f"Complete the next in-scope action: {next_action}",
            "Verify material progress against the enrolled done-when conditions",
        )
        check = self._check(
            current,
            "Write a durable context handoff before relaunch",
            "Chitra's context handoff is durable and validated",
        )
        recovery = record.recovery.model_copy(
            update={
                "stage": "relaunch",
                "attempted_remedy": "reframe",
                "attempt_count": record.recovery.attempt_count + 1,
                "next_allowed_attempt": current.isoformat(),
                "pending_payload": None,
                "execution_objective": objective,
                "execution_plan": plan,
                "handoff_id": None,
                "handoff_reference": None,
                "handoff_digest": None,
            }
        )
        candidate = self._persist(
            record.model_copy(update={"checkpoint_reference": None, "recovery": recovery, "next_check": check}),
            persist=persist,
        )
        return self._decision(
            "reframe", candidate, "Chitra rewrote the tactical execution plan without changing the enrolled goal", facts=facts
        )

    def _compress(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        current: datetime,
        facts: tuple[OperatingFact, ...],
        persist: bool,
    ) -> RecoveryDecision:
        if not persist or self.store_for(record.lane_id) is None:
            raise RecoveryStateError("context compression requires durable joined-lane storage")
        try:
            prepared = self._write_context_handoff(record, goal, current, persist)
        except RecoveryStateError:
            return self._wait(record, current, "the durable context handoff is valid and untampered", persist)
        payload = self._action_payload(prepared, goal, "compress", facts)
        if payload is None:
            raise RecoveryStateError("context compression did not produce a provider payload")
        return self._run_operation(prepared, current, "compress", payload, facts, persist)

    def _ladder_hold(self, record: JoinedLaneRecord, finding: Finding | None, action: str) -> RecoveryDecision | None:
        if self.response_ladder is None:
            return None
        expected = {"nudge": "nudge", "correct": "redirect", "checkpoint": "rescue", "relaunch": "relaunch"}[action]
        if finding is None:
            incident = self.response_ladder.store.latest(record.recovery.failure_signature)
            if incident is None or incident.lane != record.lane_id:
                return self._decision("waiting", record, "recovery requires a matching canonical ladder incident")
            return (
                None
                if incident.stage == expected and self.response_ladder.stage_action_proven(incident)
                else self._decision(
                    "waiting",
                    record,
                    f"canonical response ladder lacks proven entry to {expected} stage",
                )
            )
        marker = f"recovery-{record.lane_id}-{record.recovery.cycle_id}-{action}"
        try:
            decision = self.response_ladder.evaluate(lane=record.lane_id, finding=finding, order_marker=marker)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("recovery_ladder_unavailable", lane_id=record.lane_id, error=str(exc))
            return self._decision("waiting", record, "the canonical response ladder is unavailable")
        return self._decision("waiting", record, decision.reason) if decision.action == "hold" or decision.stage != expected else None

    def _event_sequence(self, record: JoinedLaneRecord) -> int:
        events = self._journal_events()
        return len(events) if events else record.current_update.sequence if record.current_update is not None else 1

    @staticmethod
    def _payload_digest(record: JoinedLaneRecord, action: str, payload: str, sequence: int) -> str:
        return canonical_digest(
            {
                "lane_id": record.lane_id,
                "goal_id": record.goal_id,
                "goal_version": record.goal_version,
                "session_ref": record.session_ref,
                "cycle_id": record.recovery.cycle_id,
                "action": action,
                "provider_handle": record.provider.handle,
                "provider_instance_id": record.provider.instance_id,
                "provider_generation": record.provider.generation,
                "event_sequence": sequence,
                "payload": payload,
            }
        )

    def _operation(self, record: JoinedLaneRecord, action: str, payload: str, current: datetime) -> PendingProviderOperation:
        if action in {"close", "resume"}:
            provider = record.provider
            session = _close_session(record)
            if provider.instance_id is None or provider.generation is None:
                raise RecoveryStateError(f"{action} operation lacks a complete provider identity")
            identity = {
                "lane_id": record.lane_id,
                "goal_id": record.goal_id,
                "goal_version": record.goal_version,
                "session_ref": record.session_ref,
                "provider_handle": provider.handle,
                "provider_session_id": session,
                "provider_instance_id": provider.instance_id,
                "provider_generation": provider.generation,
                "payload": payload,
            }
            operation_id = f"{action}-" + canonical_digest(identity)[:32]
            return PendingProviderOperation(
                operation_id=operation_id,
                kind="close" if action == "close" else "create_or_resume",
                lane_id=record.lane_id,
                provider_handle=provider.handle,
                provider_session_id=session,
                idempotency_key=f"{operation_id}-idem",
                payload_digest=canonical_digest(identity),
                payload=payload,
                provider_instance_id=provider.instance_id,
                provider_generation=provider.generation,
                created_at=current.isoformat(),
            )
        cycle_id = record.recovery.cycle_id
        if cycle_id is None:
            raise RecoveryStateError("recovery cycle identity is missing")
        sequence = self._event_sequence(record)
        operation_id = f"recovery-{cycle_id}-{action}-{sequence}"
        if operation_id in {item.operation_id for item in record.operation_history}:
            operation_id = f"{operation_id}-attempt-{record.recovery.attempt_count + 1}"
        digest = self._payload_digest(record, action, payload, sequence)
        return PendingProviderOperation(
            operation_id=operation_id,
            kind=(
                "checkpoint"
                if action in {"checkpoint", "compress"}
                else "create_or_resume"
                if action == "relaunch"
                else "send"
            ),
            lane_id=record.lane_id,
            provider_handle=record.provider.handle,
            idempotency_key=f"{operation_id}-idem",
            payload_digest=digest,
            provider_session_id=record.provider.provider_session_id or record.session_ref,
            payload=payload,
            provider_instance_id=record.provider.instance_id,
            provider_generation=record.provider.generation,
            created_at=current.isoformat(),
        )

    def _begin(
        self,
        record: JoinedLaneRecord,
        action: str,
        payload: str,
        current: datetime,
        persist: bool,
    ) -> tuple[JoinedLaneRecord, PendingProviderOperation]:
        operation = self._operation(record, action, payload, current)
        if operation.operation_id in {item.operation_id for item in record.operation_history}:
            raise RecoveryStateError("fresh recovery action reused an operation identity")
        history = (
            *record.operation_history,
            OperationReference(
                operation_id=operation.operation_id,
                idempotency_key=operation.idempotency_key,
                payload_digest=operation.payload_digest,
                kind=operation.kind,
                created_at=operation.created_at,
            ),
        )
        stage = action if action in {"nudge", "correct", "diagnostic"} else "relaunch"
        recovery = record.recovery.model_copy(
            update={
                "stage": stage,
                "attempted_remedy": action,
                "attempt_count": record.recovery.attempt_count + 1,
                "event_sequence": self._event_sequence(record),
                "next_allowed_attempt": current.isoformat(),
                "pending_payload": payload,
            }
        )
        candidate = record.model_copy(
            update={
                "pending_operation": operation,
                "last_operation_result": None,
                "operation_history": history,
                "recovery": recovery,
            }
        )
        return self._persist(candidate, persist=persist), operation

    @staticmethod
    def _unknown(operation: PendingProviderOperation, current: datetime, evidence: str) -> ProviderOperationResult:
        return ProviderOperationResult(
            operation_id=operation.operation_id,
            kind=operation.kind,
            lane_id=operation.lane_id,
            provider_handle=operation.provider_handle,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            provider_session_id=operation.provider_session_id,
            provider_instance_id=operation.provider_instance_id,
            provider_generation=operation.provider_generation,
            status="unknown",
            observed_at=current.isoformat(),
            evidence=evidence,
        )

    def _invoke(
        self,
        record: JoinedLaneRecord,
        operation: PendingProviderOperation,
        action: str,
        payload: str,
        current: datetime,
    ) -> ProviderOperationResult:
        if self.provider is None:
            return self._unknown(operation, current, "no provider is registered for the lane")
        try:
            if action in {"checkpoint", "compress"}:
                result = self.provider.checkpoint(CheckpointRequest(operation=operation, label=payload))
            elif action in {"relaunch", "resume"}:
                close = record.last_close_result if action == "resume" else None
                resume_token = (
                    _resume_auth_token(record, close, operation, state_root=self.state_root)
                    if action == "resume" and close is not None and close.owner_process is not None and self.state_root is not None
                    else None
                )
                result = self.provider.create_or_resume(
                    CreateOrResumeRequest(
                        operation=operation,
                        session_ref=record.session_ref,
                        provider_session_id=operation.provider_session_id or record.session_ref,
                        context_ref=record.checkpoint_reference,
                        goal_id=record.goal_id,
                        goal_version=record.goal_version,
                        resume_after_close=(action == "resume" and close is not None and close.owner_process is not None),
                        close_operation_id=(close.operation_id if close is not None and close.owner_process is not None else None),
                        owner_process=(close.owner_process if close is not None and close.owner_process is not None else None),
                        resume_token=resume_token,
                    )
                )
            else:
                result = self.provider.send(SendRequest(operation=operation, text=payload))
        except Exception as exc:  # noqa: BLE001
            return self._unknown(operation, current, f"provider call failed: {exc}")
        if not isinstance(result, ProviderOperationResult):
            return self._unknown(operation, current, "provider returned a noncanonical operation result")
        try:
            validate_operation_result(operation, result)
        except ContractValidationError as exc:
            return self._unknown(operation, current, f"provider result identity rejected: {exc}")
        return result

    def _run_operation(
        self,
        record: JoinedLaneRecord,
        current: datetime,
        action: str,
        payload: str,
        facts: tuple[OperatingFact, ...],
        persist: bool,
    ) -> RecoveryDecision:
        if not persist or self.store_for(record.lane_id) is None:
            raise RecoveryStateError("provider recovery actions require durable joined-lane storage")
        pending_record, operation = self._begin(record, action, payload, current, persist)
        result = self._invoke(pending_record, operation, action, payload, current)
        observed = self._persist(pending_record.model_copy(update={"last_operation_result": result}), persist=persist)
        if result.status == "consumed":
            return self._finish_consumed(observed, current, facts, persist)
        return self._pending(observed, current, f"{action} is not backed by exact consumption evidence", persist)

    def _send(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        current: datetime,
        action: Literal["nudge", "correct"],
        facts: tuple[OperatingFact, ...],
        persist: bool,
    ) -> RecoveryDecision:
        message = self._action_payload(record, goal, action, facts)
        assert message is not None
        return self._run_operation(record, current, action, message, facts, persist)

    def _action_payload(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        action: str,
        facts: tuple[OperatingFact, ...],
    ) -> str | None:
        next_action = record.current_update.next_action if record.current_update is not None else "the next in-scope action"
        if action == "nudge":
            return f"Continue enrolled goal {goal.goal.strip()!r}. Take the next in-scope action: {next_action}."
        if action == "correct":
            states = ", ".join(f"{fact.name}={fact.state}" for fact in facts) or "no current operating fact"
            return f"Correct this stall without changing goal {goal.goal.strip()!r}. Take: {next_action}. Facts: {states}."
        if action == "checkpoint":
            return f"recovery-{record.lane_id}-{record.recovery.cycle_id}"
        if action == "compress":
            reference = record.recovery.handoff_reference or self._handoff_reference(record)
            return (
                f"Compress this lane's context into the durable Chitra handoff {reference}. "
                "Preserve the enrolled goal, scope, constraints, and done conditions."
            )
        if action == "relaunch":
            handoff = (
                f" Restore handoff {record.recovery.handoff_reference}."
                if record.recovery.execution_plan and record.recovery.handoff_reference
                else ""
            )
            return f"Resume from governed checkpoint {record.checkpoint_reference}.{handoff}"
        if action == "diagnostic":
            return "Run one bounded diagnostic in this lane through the normal provider send operation. Do not ask the user."
        return None

    def _checkpoint_binding(self, record: JoinedLaneRecord, operation: PendingProviderOperation) -> RecoveryCheckpointBinding:
        cycle_id = record.recovery.cycle_id
        sequence = record.recovery.event_sequence
        if cycle_id is None or sequence is None or operation.provider_instance_id is None or operation.provider_generation is None:
            raise RecoveryStateError("checkpoint operation lacks exact recovery/provider identity")
        return RecoveryCheckpointBinding(
            lane_id=record.lane_id,
            goal_id=record.goal_id,
            goal_version=record.goal_version,
            session_ref=record.session_ref,
            cycle_id=cycle_id,
            operation_id=operation.operation_id,
            provider_handle=operation.provider_handle,
            provider_session_id=operation.provider_session_id,
            provider_instance_id=operation.provider_instance_id,
            provider_generation=operation.provider_generation,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            event_sequence=sequence,
        )

    def _finish_consumed(
        self,
        record: JoinedLaneRecord,
        current: datetime,
        facts: tuple[OperatingFact, ...],
        persist: bool,
    ) -> RecoveryDecision:
        operation, result = record.pending_operation, record.last_operation_result
        if operation is None or result is None:
            raise RecoveryStateError("consumed recovery result has no pending operation")
        validate_operation_result(operation, result)
        action = record.recovery.attempted_remedy
        if action in {"checkpoint", "compress"}:
            reference = (
                find_recovery_checkpoint_receipt(self.state_root, self._checkpoint_binding(record, operation))
                if self.state_root is not None
                else None
            )
            if reference is None:
                return self._pending(record, current, "recovery compression lacks a signed, sealed RESCUE receipt", persist)
            check = self._check(
                current,
                "Relaunch from the compressed governed checkpoint",
                "the rotated physical session is observed",
            )
            candidate = self._persist(
                record.model_copy(
                    update={
                        "pending_operation": None,
                        "checkpoint_reference": reference,
                        "next_check": check,
                        "recovery": record.recovery.model_copy(update={"pending_payload": None}),
                    }
                ),
                persist=persist,
            )
            return self._decision(cast(RecoveryAction, action), candidate, "governed checkpoint validated", operation, facts=facts)
        if action == "relaunch":
            status = self._provider_status(record)
            if status is None or status.provider_session_id is None or status.provider_session_id == record.session_ref:
                return self._pending(record, current, "relaunch lacks a rotated physical session observation", persist)
            candidate = self._persist(self._rotate_session(record, status, current), persist=persist, transition="provider-transfer")
            return self._decision("relaunch", candidate, "logical lane relaunched with rotated session identity", operation)
        next_stage = "correct" if action == "nudge" else "relaunch" if action == "correct" else "waiting"
        condition = (
            "a current operating fact or material lane update"
            if action == "nudge"
            else "a governed checkpoint for the same logical lane"
            if action == "correct"
            else "a material diagnostic result or provider fact revision for the same logical lane"
        )
        check = self._check(current, f"Advance after consumed {action}", condition, action == "diagnostic")
        recovery = record.recovery.model_copy(update={"stage": next_stage, "next_allowed_attempt": check.at})
        recovery = recovery.model_copy(update={"pending_payload": None})
        intervention = InterventionEvidence(
            operation_id=operation.operation_id,
            action=action,
            consumed=True,
            useful_work_resumed=None,
            observed_at=result.observed_at,
        )
        candidate = self._persist(
            record.model_copy(
                update={
                    "pending_operation": None,
                    "recovery": recovery,
                    "next_check": check,
                    "last_intervention": intervention,
                }
            ),
            persist=persist,
        )
        return self._decision(cast(RecoveryAction, action), candidate, f"{action} consumption is proven", operation, facts=facts)

    def _provider_status(self, record: JoinedLaneRecord | None = None) -> ProviderStatus | None:
        if self.provider is None:
            return None
        try:
            status = self.provider.status()
        except Exception:  # noqa: BLE001
            return None
        if (
            not isinstance(status, ProviderStatus)
            or not status.fresh
            or not status.provider_available
            or status.unknown
            or status.state in {ProviderState.OUTAGE, ProviderState.STALE}
            or status.context_available is False
            or (
                record is not None
                and (
                    str(status.provider) != str(record.provider.kind)
                    or status.generation < (record.provider.generation or 0)
                )
            )
        ):
            return None
        return status

    def _rotate_session(self, record: JoinedLaneRecord, status: ProviderStatus, current: datetime) -> JoinedLaneRecord:
        if status.provider_session_id is None:
            raise RecoveryStateError("relaunch rotation lacks session identity")
        physical_generation = max((record.physical_session_generation or 0) + 1, status.generation)
        provider = record.provider
        if status.generation > (provider.generation or 0):
            provider = provider.model_copy(update={"generation": status.generation})
        update = record.current_update
        if update is not None:
            update = update.model_copy(update={"session_ref": status.provider_session_id})
        check = self._check(current, "Check useful progress after relaunch", "a material update after relaunch")
        recovery = record.recovery.model_copy(
            update={"stage": "diagnostic", "next_allowed_attempt": check.at, "pending_payload": None}
        )
        # Wake receipts are historical evidence for the old physical session.
        # Compact them before rebinding the joined record to the rotated
        # session; rewriting their session identity would forge the evidence.
        archive_digest = record.wake_archive_digest
        for receipt in record.wake_receipts:
            archive_digest = extend_wake_archive_digest(archive_digest, receipt)
        return record.model_copy(
            update={
                "session_ref": status.provider_session_id,
                "physical_session_generation": physical_generation,
                "chitra_ownership_epoch": record.chitra_ownership_epoch + 1,
                "provider": provider.model_copy(update={"provider_session_id": status.provider_session_id}),
                "current_update": update,
                "pending_operation": None,
                "last_operation_result": None,
                "wake_receipts": (),
                "wake_archive_count": record.wake_archive_count + len(record.wake_receipts),
                "wake_archive_digest": archive_digest,
                "recovery": recovery,
                "next_check": check,
            }
        )

    def _provider_probe(self, record: JoinedLaneRecord) -> ProviderOperationResult | None:
        pending = record.pending_operation
        if pending is None:
            return None
        stored = record.last_operation_result
        if self.provider is None or not self.provider.capabilities.read_updates:
            return stored
        try:
            batch = self.provider.read_updates(record.update_cursor or None)
        except Exception:  # noqa: BLE001
            return stored
        # Legacy pending rows predate the separate provider session field.
        # Their typed migration source is the provider identity, then the
        # joined lane session_ref.  The provider operation handle is never a
        # substitute for either session identity.
        expected_session_id = pending.provider_session_id or record.provider.provider_session_id or record.session_ref
        for update in reversed(batch.updates):
            actual = (
                update.operation_id,
                update.lane_id,
                update.provider_session_id,
                update.provider_handle,
                update.idempotency_key,
                update.payload_digest,
                update.provider_instance_id,
                update.provider_generation,
            )
            expected = (
                pending.operation_id,
                pending.lane_id,
                expected_session_id,
                pending.provider_handle,
                pending.idempotency_key,
                pending.payload_digest,
                pending.provider_instance_id,
                pending.provider_generation,
            )
            if actual != expected:
                continue
            consumed = update.kind in {
                UpdateKind.STEER_CONSUMED,
                UpdateKind.CHECKPOINT_CREATED,
                UpdateKind.SESSION_CREATED,
                UpdateKind.SESSION_RESUMED,
            }
            if not consumed and update.kind is not UpdateKind.STEER_ACCEPTED:
                continue
            return ProviderOperationResult(
                operation_id=pending.operation_id,
                kind=pending.kind,
                lane_id=pending.lane_id,
                provider_handle=pending.provider_handle,
                idempotency_key=pending.idempotency_key,
                payload_digest=pending.payload_digest,
                provider_session_id=pending.provider_session_id,
                provider_instance_id=pending.provider_instance_id,
                provider_generation=pending.provider_generation,
                status="consumed" if consumed else "accepted",
                accepted=True,
                consumed=consumed,
                observed_at=update.observed_at,
                evidence=update.event_id,
            )
        return stored

    def _reconciler_for(self) -> JoinedLaneReconciler | None:
        if self.reconciler is not None:
            return self.reconciler
        if self.state_root is None:
            return None
        key_path = self.state_root / "ledger.key"
        return JoinedLaneReconciler(
            CanonicalJoinedLaneStore(self.state_root),
            provider_probe=self._provider_probe,
            journal_probe=journal_provider_probe(self.state_root),
            ledger_probe=ledger_provider_probe(self.state_root / "ledger.jsonl", key_path) if key_path.exists() else None,
            ownership_probe=self.ownership_probe or ownership_provider_probe(),
        )

    def _reconcile_pending(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        current: datetime,
        facts: tuple[OperatingFact, ...],
        persist: bool,
    ) -> RecoveryDecision:
        if record.last_operation_result is not None and record.last_operation_result.status == "consumed":
            return self._finish_consumed(record, current, facts, persist)
        observed = self._provider_probe(record)
        if observed is not None and observed.status == "consumed":
            # The provider read is already an exact, cursor-addressable
            # consumption observation.  Preserve it before applying the
            # normal stage transition; do not downgrade it to transport-only
            # acceptance through the dispatch reconciler.
            recorded = self._persist(record.model_copy(update={"last_operation_result": observed}), persist=persist)
            return self._finish_consumed(recorded, current, facts, persist)
        if observed is None or observed.status in {"unknown", "lost-response"}:
            pending = record.pending_operation
            action = record.recovery.attempted_remedy
            # Retry the exact payload allocated with this operation. Rebuilding
            # it from the current update is unsafe: a changed ``next_action``
            # changes the digest and otherwise leaves a lost operation wedged
            # forever even though the same idempotent send is still retryable.
            payload = (
                pending.payload
                if pending is not None and pending.payload
                else record.recovery.pending_payload
                or self._action_payload(record, goal, action, facts)
            )
            sequence = record.recovery.event_sequence
            if not persist or self.store_for(record.lane_id) is None:
                return self._pending(
                    record,
                    current,
                    "pending recovery operation requires durable joined-lane storage before retry",
                    persist,
                )
            if (
                pending is not None
                and payload is not None
                and sequence is not None
                and pending.payload_digest == self._payload_digest(record, action, payload, sequence)
            ):
                retried = self._invoke(record, pending, action, payload, current)
                record = self._persist(record.model_copy(update={"last_operation_result": retried}), persist=persist)
                if retried.status == "consumed":
                    return self._finish_consumed(record, current, facts, persist)
        reconciler = self._reconciler_for()
        if reconciler is None:
            return self._pending(record, current, "pending operation requires canonical reconciliation", persist)
        outcome = reconciler.reconcile(record)
        reconciled = cast(JoinedLaneRecord, outcome.record or record)
        if reconciled.last_operation_result is not None and reconciled.last_operation_result.status == "consumed":
            return self._finish_consumed(reconciled, current, facts, persist)
        return self._pending(reconciled, current, outcome.reason or "pending operation is not consumed", persist)

    def _governed_close(
        self,
        record: JoinedLaneRecord | None,
        *,
        lane_id: str | None,
        now: datetime | None,
        persist: bool,
    ) -> GovernedCloseDecision:
        lane = lane_id or (record.lane_id if record is not None else None)
        if lane is None or self.state_root is None or not persist:
            return self._governed_close_unlocked(record, lane_id=lane_id, now=now, persist=persist)
        store = self.store_for(lane)
        if store is None:
            return self._governed_close_unlocked(record, lane_id=lane_id, now=now, persist=persist)
        # Hold the lane-wide lock across provider status, stop, and evidence
        # writes. A second supervisor must observe the first close result,
        # never issue a second physical stop.
        with store.lane_control_lock():
            return self._governed_close_unlocked(record, lane_id=lane_id, now=now, persist=persist)

    def _governed_close_unlocked(
        self,
        record: JoinedLaneRecord | None,
        *,
        lane_id: str | None,
        now: datetime | None,
        persist: bool,
    ) -> GovernedCloseDecision:
        """Run the close state machine on the canonical lane store.

        The close module owns checkpoint and evidence documents.  Recovery
        owns operation identity, provider invocation, pending retry, and the
        final lane transition so close cannot grow a second recovery engine.
        """

        from .governed_close import (
            _close_payload,
            _ensure_checkpoint,
            _read_checkpoint_payload,
        )

        if self.state_root is None or not persist:
            if record is None:
                raise RecoveryStateError("governed close requires a durable joined-lane store")
            return self._close_wait(record, "governed close requires durable Chitra storage")
        goal_root = self.goal_root
        if goal_root is None:
            if record is None:
                raise RecoveryStateError("governed close requires a completion goal root")
            return self._close_wait(record, "governed close requires an explicit completion goal root")
        store = self.store_for(lane_id or (record.lane_id if record else ""))
        if store is None:
            raise RecoveryStateError("governed close requires a durable joined-lane store")
        current = store.load()
        if current is not None:
            record = current
        if record is None:
            if not lane_id:
                raise RecoveryStateError("governed close requires a lane record or lane_id")
            raise RecoveryStateError(f"no joined lane record for {lane_id!r}")
        if record.last_close_result is not None:
            if record.lifecycle != "inactive":
                return self._close_wait(record, "close evidence exists but the lane is not inactive")
            if not store.has_durable_close_evidence(record, record.last_close_result):
                return self._close_wait(
                    record,
                    "lane close state is not backed by durable Chitra evidence",
                )
            return GovernedCloseDecision(
                action="closed",
                record=record,
                reason="lane already has immutable close evidence",
                close_result=record.last_close_result,
            )
        if record.lifecycle != "active":
            return self._close_wait(record, "lane is not active and has no close evidence")
        try:
            goal = get_goal(goal_root, record.session_ref)
        except Exception as exc:  # an unreadable goal cannot authorize close
            return self._close_wait(record, f"completion goal is unavailable: {exc}")
        if (
            goal is None
            or goal.status != "done-pending-close"
            or (goal.session_ref, goal.lane_id, goal.goal_id, goal.goal_version)
            != (record.session_ref, record.lane_id, record.goal_id, record.goal_version)
        ):
            return self._close_wait(record, "exact enrolled goal is not done-pending-close")
        if self.provider is None:
            return self._close_wait(record, "provider adapter is unavailable")
        provider_kind = str(getattr(getattr(self.provider, "provider_name", ""), "value", getattr(self.provider, "provider_name", "")))
        if provider_kind != str(record.provider.kind):
            return self._close_wait(record, "provider kind does not match the joined lane")
        capabilities = getattr(self.provider, "capabilities", {})
        for capability in ("status", "close"):
            supported = (
                capabilities.get(capability) is True
                if isinstance(capabilities, Mapping)
                else bool(getattr(capabilities, capability, False))
            )
            if not supported or not record.provider.capabilities.supports(cast(Any, capability)):
                return self._close_wait(record, f"provider lacks required {capability} capability")
        try:
            working = _ensure_checkpoint(self.state_root, record)
        except (OSError, RecoveryStateError, TypeError, ValueError) as exc:
            return self._close_wait(record, f"Chitra checkpoint is not durable: {exc}")
        checkpoint = _read_checkpoint_payload(
            self.state_root, working, working.checkpoint_reference or ""
        )
        if checkpoint is None:
            return self._close_wait(working, "Chitra checkpoint could not be read for the explicit provider handoff")
        pending = working.pending_operation
        if pending is not None and pending.kind != "close":
            return self._close_wait(working, "another provider operation is still pending", pending)
        current_time = _now(now)
        try:
            expected = self._operation(working, "close", _close_payload(working), current_time)
        except (RecoveryStateError, TypeError, ValueError) as exc:
            return self._close_wait(working, str(exc), pending)
        if pending is None:
            try:
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
                working = self._persist(
                    working.model_copy(update={"pending_operation": expected, "operation_history": history}),
                    persist=True,
                )
            except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as exc:
                return self._close_wait(working, f"close operation could not be durably recorded: {exc}")
        elif pending.model_dump(exclude={"created_at", "attempt"}) != expected.model_dump(
            exclude={"created_at", "attempt"}
        ):
            return self._close_wait(working, "pending close identity or payload changed", pending)
        recovered = store.read_close_evidence(pending, working)
        if recovered is not None:
            try:
                closed = self._finish_close(working, recovered, persist=True)
            except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as exc:
                return self._close_wait(
                    working,
                    f"durable close evidence exists but Chitra could not persist close state: {exc}",
                    pending,
                    recovered,
                )
            return GovernedCloseDecision(
                action="closed",
                record=closed,
                reason="reconciled durable provider close evidence",
                operation=pending,
                close_result=recovered,
            )
        status = self._provider_status(working)
        if (
            status is None
            or str(status.provider) != str(working.provider.kind)
            or status.provider_session_id != _close_session(working)
            or status.provider_instance_id != working.provider.instance_id
            or status.generation != working.provider.generation
            or status.current_turn_id is not None
            or status.state not in {ProviderState.IDLE, ProviderState.CLOSED, ProviderState.ARCHIVED}
        ):
            return self._close_wait(working, "provider status does not prove the exact idle or already-closed physical session", pending)
        result = self._invoke_close(working, pending, checkpoint)
        if result is None:
            return self._close_wait(working, "provider close response is unknown or failed exact identity validation", pending)
        try:
            store.write_close_evidence(pending, result)
            closed = self._finish_close(working, result, persist=True)
        except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as exc:
            return self._close_wait(
                working,
                f"provider close succeeded but Chitra could not persist close evidence: {exc}",
                pending,
                result,
            )
        return GovernedCloseDecision(
            action="closed",
            record=closed,
            reason="provider close is bound to Chitra's checkpoint and physical session",
            operation=pending,
            close_result=result,
        )

    @staticmethod
    def _resume_wait(
        record: JoinedLaneRecord,
        reason: str,
        operation: PendingProviderOperation | None = None,
        result: ProviderOperationResult | None = None,
    ) -> ResumeDecision:
        return ResumeDecision(
            action="waiting", record=record, reason=reason, operation=operation, result=result
        )

    def resume_after_close(
        self,
        record: JoinedLaneRecord | None = None,
        *,
        lane_id: str | None = None,
        now: datetime | str | None = None,
        persist: bool = True,
    ) -> ResumeDecision:
        lane = lane_id or (record.lane_id if record is not None else None)
        if lane is None or self.state_root is None or not persist:
            return self._resume_after_close_unlocked(record, lane_id=lane_id, now=now, persist=persist)
        store = self.store_for(lane)
        if store is None:
            return self._resume_after_close_unlocked(record, lane_id=lane_id, now=now, persist=persist)
        # Resume is a physical provider mutation too. Serialize it with close
        # and with other resume supervisors for this exact lane.
        with store.lane_control_lock():
            return self._resume_after_close_unlocked(record, lane_id=lane_id, now=now, persist=persist)

    def _resume_after_close_unlocked(
        self,
        record: JoinedLaneRecord | None = None,
        *,
        lane_id: str | None = None,
        now: datetime | str | None = None,
        persist: bool = True,
    ) -> ResumeDecision:
        """Resume the same archived provider thread through one durable operation."""
        if self.state_root is None or not persist:
            if record is None:
                raise RecoveryStateError("resume requires a durable joined-lane store")
            return self._resume_wait(record, "resume requires durable Chitra storage")
        store = self.store_for(lane_id or (record.lane_id if record else ""))
        if store is None:
            raise RecoveryStateError("resume requires a durable joined-lane store")
        current_record = store.load()
        if current_record is not None:
            record = current_record
        if record is None:
            if not lane_id:
                raise RecoveryStateError("resume requires a lane record or lane_id")
            raise RecoveryStateError(f"no joined lane record for {lane_id!r}")
        close = record.last_close_result
        if record.lifecycle != "inactive" or close is None:
            return self._resume_wait(record, "lane has no inactive close state to resume")
        if close.state not in {"closed", "archived"} or close.later_resume_supported is not True:
            return self._resume_wait(record, "provider close evidence does not permit later resume")
        if (
            close.provider_thread_ref != record.provider.handle
            or close.provider_instance_id != record.provider.instance_id
            or close.provider_generation != record.provider.generation
            or close.provider_session_id != record.provider.provider_session_id
        ):
            return self._resume_wait(record, "close evidence is not bound to the current provider identity")
        from .governed_close import _read_checkpoint_payload

        if (
            self.state_root is None
            or record.checkpoint_reference is None
            or _read_checkpoint_payload(self.state_root, record, record.checkpoint_reference) is None
        ):
            return self._resume_wait(record, "signed Chitra checkpoint is unavailable for same-session resume")
        if self.provider is None:
            return self._resume_wait(record, "provider adapter is unavailable")
        capabilities = getattr(self.provider, "capabilities", {})
        supported = (
            all(capabilities.get(name) is True for name in ("status", "create_or_resume", "resume_after_close"))
            if isinstance(capabilities, Mapping)
            else all(bool(getattr(capabilities, name, False)) for name in ("status", "create_or_resume", "resume_after_close"))
        )
        if not supported or not all(
            record.provider.capabilities.supports(name)
            for name in ("status", "create_or_resume", "resume_after_close")
        ):
            return self._resume_wait(record, "provider lacks exact same-session resume capability")
        status = self._provider_status(record)
        if (
            status is None
            or status.provider_session_id != record.provider.provider_session_id
            or status.provider_instance_id != record.provider.instance_id
            or status.generation != record.provider.generation
            or status.state not in {ProviderState.CLOSED, ProviderState.ARCHIVED, ProviderState.IDLE}
        ):
            return self._resume_wait(record, "provider status does not prove the closed session identity")
        current_time = _now(now)
        payload = canonical_digest(
            {
                "resume_after_close": True,
                "close_operation_id": close.operation_id,
                "provider_session_id": record.provider.provider_session_id,
                "owner_process": (
                    close.owner_process.model_dump(mode="json")
                    if close.owner_process is not None
                    else None
                ),
            }
        )
        try:
            expected = self._operation(record, "resume", payload, current_time)
        except (RecoveryStateError, TypeError, ValueError) as exc:
            return self._resume_wait(record, str(exc))
        pending = record.pending_operation
        if pending is None:
            history = (
                *record.operation_history,
                OperationReference(
                    operation_id=expected.operation_id,
                    idempotency_key=expected.idempotency_key,
                    payload_digest=expected.payload_digest,
                    kind=expected.kind,
                    created_at=expected.created_at,
                ),
            )
            try:
                pending = expected
                record = self._persist(
                    record.model_copy(
                        update={
                            "pending_operation": pending,
                            "last_operation_result": None,
                            "operation_history": history,
                        }
                    ),
                    persist=True,
                )
            except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as exc:
                return self._resume_wait(record, f"resume operation could not be durably recorded: {exc}")
        elif (
            pending.kind != "create_or_resume"
            or pending.model_dump(exclude={"created_at", "attempt"})
            != expected.model_dump(exclude={"created_at", "attempt"})
        ):
            return self._resume_wait(record, "pending resume identity or payload changed", pending)
        stored = record.last_operation_result
        if stored is not None and stored.operation_id == pending.operation_id and stored.status == "consumed":
            result = stored
        else:
            result = self._invoke(record, pending, "resume", pending.payload, current_time)
            record = self._persist(record.model_copy(update={"last_operation_result": result}), persist=True)
        if result.status != "consumed":
            return self._resume_wait(record, "same-session resume lacks exact consumption evidence", pending, result)
        # Consumption of the resume command is not proof that the provider
        # actually left its archived state.  Require a fresh live observation
        # for the exact physical session before changing Chitra to active.
        live = self._provider_status(record)
        if (
            live is None
            or live.provider_session_id != record.provider.provider_session_id
            or live.provider_instance_id != record.provider.instance_id
            or live.generation != record.provider.generation
            or live.state not in {ProviderState.IDLE, ProviderState.RUNNING}
        ):
            return self._resume_wait(
                record,
                "same-session resume was consumed but no fresh live provider status was observed",
                pending,
                result,
            )
        receipt = result.reopen_receipt
        if receipt is None or receipt.auth_token != _resume_auth_token(
            record, close, pending, state_root=self.state_root
        ):
            return self._resume_wait(record, "same-session resume lacks its authenticated reopen receipt", pending, result)
        if receipt.receipt_hmac is None or receipt.receipt_hmac != _resume_receipt_hmac(
            receipt.to_dict(), _resume_auth_token(record, close, pending, state_root=self.state_root)
        ):
            return self._resume_wait(record, "same-session resume lacks its Fleet receipt HMAC", pending, result)
        if receipt.signature is None:
            receipt = receipt.model_copy(
                update={"signature": _sign_mapping(receipt.to_dict(), self.state_root)}
            )
            result = result.model_copy(update={"reopen_receipt": receipt})
            record = self._persist(record.model_copy(update={"last_operation_result": result}), persist=True)
        if not _resume_receipt_matches(record, close, pending, result, state_root=self.state_root):
            return self._resume_wait(record, "same-session resume lacks exact authenticated reopen evidence", pending, result)
        check = self._check(current_time, "Check useful progress after resume", "a material update after the same-session resume")
        resumed = record.model_copy(
            update={
                "lifecycle": "active",
                "last_close_result": None,
                "pending_operation": None,
                "next_check": check,
                "recovery": record.recovery.model_copy(
                    update={"stage": "diagnostic", "attempted_remedy": "resume", "pending_payload": None}
                ),
                "current_update": (
                    None
                    if record.current_update is not None
                    and record.current_update.operation_id
                    else record.current_update
                ),
            }
        )
        try:
            resumed = self._persist(resumed, persist=True, transition="resume")
        except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as exc:
            return self._resume_wait(record, f"resume succeeded but Chitra could not persist active state: {exc}", pending, result)
        return ResumeDecision(
            action="resumed",
            record=resumed,
            reason="same provider session resumed with durable consumption evidence",
            operation=pending,
            result=result,
        )

    def governed_close(
        self,
        record: JoinedLaneRecord | None = None,
        *,
        lane_id: str | None = None,
        now: datetime | None = None,
        persist: bool = True,
    ) -> GovernedCloseDecision:
        return self._governed_close(record, lane_id=lane_id, now=now, persist=persist)

    close = governed_close

    def run_for_lane(self, lane_id: str, **kwargs: Any) -> RecoveryDecision:
        record = self.load(lane_id)
        if record is None:
            raise RecoveryStateError(f"no joined lane record for {lane_id!r}")
        return self.run_once(record, **kwargs)


class RecoverySupervisor:
    """Production pass over every due canonical joined-lane recovery record."""

    def __init__(
        self,
        state_root: Path,
        provider_resolver: RecoveryProviderResolver,
        *,
        goal_root: Path | None = None,
        ledger_key_path: Path | None = None,
        facts_reader: RecoveryFactsReader | None = None,
    ) -> None:
        self.state_root = state_root
        self.provider_resolver = provider_resolver
        self.goal_root = goal_root or state_root
        self.ledger_key_path = ledger_key_path or state_root / "ledger.key"
        self.facts_reader = facts_reader

    @staticmethod
    def _named_wake(
        record: JoinedLaneRecord,
        journal: EventJournal,
        events: Sequence[CanonicalEvent],
    ) -> tuple[str, str, int] | None:
        """Return the newest exact wake event that the record has not consumed."""

        condition = record.wake_condition
        if not condition:
            return None
        known = {receipt.wake_id for receipt in record.wake_receipts if receipt.goal_version == record.goal_version}
        committed_sequence = max(
            (receipt.event_sequence for receipt in record.wake_receipts if receipt.goal_version == record.goal_version),
            default=0,
        )
        known.update(
            receipt.wake_id
            for receipt in journal.load_wakes()
            if receipt.goal_version == record.goal_version and receipt.event_sequence <= committed_sequence
        )
        for sequence, event in reversed(tuple(enumerate(events, 1))):
            if (
                event.lane == record.lane_id
                and event.goal_ref == record.goal_id
                and event.session_id == record.session_ref
                and event.goal_version == record.goal_version
                and event.event_id not in known
                and event.payload.get("wake_condition") == condition
                and event.payload.get("wake_condition_changed") is True
            ):
                return event.event_id, condition, sequence
        return None

    def run_once(self, *, now: datetime | None = None) -> tuple[SupervisorDecision, ...]:
        decisions: list[SupervisorDecision] = []
        store = CanonicalJoinedLaneStore(self.state_root)
        for value in store.unfinished():
            record = cast(JoinedLaneRecord, value)
            try:
                goal = get_goal(self.goal_root, record.session_ref)
            except Exception as exc:  # an unreadable goal cannot authorize close
                logger.warning("governed_close_goal_unavailable", lane_id=record.lane_id, error=str(exc))
                goal = None
            if (
                goal is not None
                and goal.status == "done-pending-close"
                and (goal.session_ref, goal.lane_id, goal.goal_id, goal.goal_version)
                == (record.session_ref, record.lane_id, record.goal_id, record.goal_version)
            ):
                try:
                    provider = self.provider_resolver(record)
                    decision = RecoveryEngine(
                        provider=provider,
                        state_root=self.state_root,
                        goal_root=self.goal_root,
                    ).governed_close(record, now=now)
                    if decision.action == "closed":
                        try:
                            close_goal(self.goal_root, record.session_ref)
                        except GoalNotFoundError:
                            pass
                        except (GoalValidationError, OSError, TypeError, ValueError) as exc:
                            retry_at = _now(now).isoformat()
                            retry_check = NextCheck(
                                at=retry_at,
                                reason="Retry completion-goal close",
                                wake_condition="the completion goal can be closed",
                            )
                            retry = decision.record.model_copy(
                                update={
                                    "recovery": decision.record.recovery.model_copy(
                                        update={
                                            "stage": "waiting",
                                            "attempted_remedy": "close-goal",
                                            "next_allowed_attempt": retry_at,
                                        }
                                    ),
                                    "next_check": retry_check,
                                }
                            )
                            try:
                                retry_store = RecoveryStateStore(self.state_root, record.lane_id)
                                with retry_store.lane_control_lock():
                                    latest = retry_store.load()
                                    if latest is not None:
                                        retry = latest.model_copy(
                                            update={
                                                "recovery": latest.recovery.model_copy(
                                                    update={
                                                        "stage": "waiting",
                                                        "attempted_remedy": "close-goal",
                                                        "next_allowed_attempt": retry_at,
                                                    }
                                                ),
                                                "next_check": retry_check,
                                            }
                                        )
                                    retry = retry_store.save(retry)
                            except (ContractValidationError, OSError, RecoveryStateError, TypeError, ValueError) as save_exc:
                                logger.warning(
                                    "governed_close_goal_retry_not_persisted",
                                    lane_id=record.lane_id,
                                    error=str(save_exc),
                                )
                            decision = GovernedCloseDecision(
                                action="waiting",
                                record=retry,
                                reason=f"provider close is durable; completion goal remains open: {exc}",
                                operation=decision.operation,
                                close_result=decision.close_result,
                            )
                    decisions.append(decision)
                except Exception as exc:  # one close lane must not abort the pass
                    logger.warning("governed_close_supervision_lane_failed", lane_id=record.lane_id, error=str(exc))
                    decisions.append(GovernedCloseDecision("waiting", record, f"governed close failed: {exc}"))
                continue
            if record.recovery.stage in {"none", "complete"}:
                continue
            try:
                provider = self.provider_resolver(record)
                journal = EventJournal(self.state_root, record.lane_id)
                journal_events = tuple(journal.load())
                try:
                    key = self.ledger_key_path.read_bytes()
                except OSError:
                    key = None
                ladder = ResponseLadder(
                    IncidentStore(self.state_root, record.lane_id),
                    journal_events=journal_events,
                    ledger_key=key,
                )
                wake = self._named_wake(record, journal, journal_events)
                kwargs: dict[str, Any] = {"now": now}
                if wake is not None:
                    kwargs.update(
                        wake_id=wake[0],
                        observed_wake_condition=wake[1],
                        wake_event_sequence=wake[2],
                    )
                decisions.append(
                    RecoveryEngine(
                        provider=provider,
                        state_root=self.state_root,
                        goal_root=self.goal_root,
                        journal=journal,
                        response_ladder=ladder,
                    ).run_once(
                        record,
                        **kwargs,
                        facts=tuple(self.facts_reader(record)) if self.facts_reader is not None else (),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one lane must not abort the pass
                logger.warning(
                    "recovery_supervision_lane_failed_closed",
                    lane_id=record.lane_id,
                    session_ref=record.session_ref,
                    error=str(exc),
                )
                decisions.append(
                    RecoveryDecision(
                        "waiting",
                        record.recovery.stage,
                        record,
                        f"recovery supervision failed closed for this lane: {exc}",
                    )
                )
        return tuple(decisions)


def run_recovery_supervision(supervisor: RecoverySupervisor) -> tuple[SupervisorDecision, ...]:
    """Named production seam used by dispatch before it sends new work."""

    return supervisor.run_once()


def confirm_useful_progress(
    record: JoinedLaneRecord,
    *,
    journal: EventJournal | None = None,
    events: Sequence[CanonicalEvent] = (),
    progress_rows: Sequence[ProgressClassification] = (),
) -> ProgressEvidence | None:
    return RecoveryEngine(journal=journal)._latest_progress(record, events, progress_rows)


def schedule_recovery_check(
    record: JoinedLaneRecord,
    failure_signature: str,
    *,
    state_root: Path | None = None,
    now: datetime | str | None = None,
    reason: str = "Confirm whether the lane made useful progress",
    wake_condition: str = "a material update for the same logical lane",
) -> JoinedLaneRecord:
    return RecoveryEngine(state_root=state_root).schedule(
        record,
        failure_signature,
        now=now,
        reason=reason,
        wake_condition=wake_condition,
    )


def run_recovery_check(record: JoinedLaneRecord, **kwargs: Any) -> RecoveryDecision:
    engine = RecoveryEngine(
        provider=kwargs.pop("provider", None),
        state_root=kwargs.pop("state_root", None),
        goal_root=kwargs.pop("goal_root", None),
        journal=kwargs.pop("journal", None),
        facts_reader=kwargs.pop("facts_reader", None),
        response_ladder=kwargs.pop("response_ladder", None),
        reconciler=kwargs.pop("reconciler", None),
        ownership_probe=kwargs.pop("ownership_probe", None),
    )
    return engine.run_once(record, **kwargs)


RecoveryManager = RecoveryEngine

__all__ = [
    "JoinedLaneStore",
    "LaneRecordStore",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryEngine",
    "RecoveryFactsReader",
    "RecoveryManager",
    "RecoveryProviderResolver",
    "RecoveryRecord",
    "RecoveryStateError",
    "RecoveryStateStore",
    "RecoverySupervisor",
    "GovernedCloseAction",
    "GovernedCloseDecision",
    "ResumeAction",
    "ResumeDecision",
    "SupervisorDecision",
    "confirm_useful_progress",
    "load_recovery_records",
    "record_pause_recovery",
    "recovery_records_path",
    "run_recovery_check",
    "run_recovery_supervision",
    "schedule_recovery_check",
]

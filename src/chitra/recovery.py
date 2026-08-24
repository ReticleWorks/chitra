"""Durable pause records and canonical session recovery."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, cast

import structlog

from ._fsio import write_json_atomic
from .detect.detectors import Finding
from .detect.ladder import IncidentStore, ResponseLadder
from .detect.rescue import RecoveryCheckpointBinding, find_recovery_checkpoint_receipt
from .goals import DONE_STATUSES, LOAD_SHED_HOLD_REASON_PREFIX, GoalRecord, done_when_with_delta, get_goal, load_goals
from .dispatch import LaneLock, LaneLockError
from .initial_launch import (
    top_hand_bootstrap_record,
    top_hand_create_operation,
    top_hand_identity_from_facts,
)
from .joined_lane import JoinedLaneCorruptError
from .joined_lane import JoinedLaneStore as CanonicalJoinedLaneStore
from .journal.models import CanonicalEvent, ProgressClass, ProgressClassification
from .journal.store import EventJournal
from .operating_facts import read_operating_facts
from .provider_protocol import (
    CheckpointRequest,
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
    ContractValidationError,
    InterventionEvidence,
    JoinedLaneRecord,
    NextCheck,
    OperatingFact,
    OperationReference,
    PendingProviderOperation,
    ProgressEvidence,
    ProviderOperationResult,
    ProviderIdentity,
    RecordTransitionKind,
    RecoveryState,
    WakeReceipt,
    extend_wake_archive_digest,
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


RecoveryAction = Literal["noop", "progress_confirmed", "nudge", "correct", "checkpoint", "relaunch", "diagnostic", "waiting", "wake"]


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

    def load(self) -> JoinedLaneRecord | None:
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


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


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
        # Retained as compatibility-only arguments for callers that still
        # construct the engine directly.  RecoveryEngine is the sole
        # production mutation owner; neither collaborator is invoked.
        reconciler: object | None = None,
        ownership_probe: object | None = None,
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

    def _cycle_id(self, record: JoinedLaneRecord, signature: str, current: datetime) -> str:
        return "cycle-" + _digest(
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
        recovery = RecoveryState(
            stage="confirm",
            cycle_id=record.recovery.cycle_id if same else self._cycle_id(record, signature, current),
            failure_signature=signature,
            attempted_remedy=record.recovery.attempted_remedy if same else "",
            attempt_count=record.recovery.attempt_count if same else 0,
            next_allowed_attempt=current.isoformat(),
        )
        condition = wake_condition.strip() or "a material update for the same logical lane"
        candidate = record.model_copy(
            update={"recovery": recovery, "next_check": NextCheck(at=current.isoformat(), reason=reason, wake_condition=condition)}
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
            signature = working.recovery.failure_signature or f"named-wake:{observed_wake_condition}"
            recovery = working.recovery.model_copy(
                update={
                    "stage": "confirm",
                    "cycle_id": self._cycle_id(working, signature, current),
                    "attempted_remedy": "",
                    "attempt_count": 0,
                    "event_sequence": None,
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
            return self._wait(working, current, working.wake_condition or "a material lane update is observed", persist)
        if stage in {"none", "complete", "confirm"}:
            return self._ladder_hold(working, finding, "nudge") or self._send(
                working, resolved_goal, current, "nudge", refreshed_facts, persist
            )
        if stage == "correct":
            return self._ladder_hold(working, finding, "correct") or self._send(
                working, resolved_goal, current, "correct", refreshed_facts, persist
            )
        if stage == "relaunch" and working.checkpoint_reference is None:
            return self._ladder_hold(working, finding, "checkpoint") or self._run_operation(
                working, current, "checkpoint", f"recovery-{working.lane_id}-{working.recovery.cycle_id}", refreshed_facts, persist
            )
        if stage == "relaunch":
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
            known.update(
                receipt.wake_id for receipt in journal.load_wakes() if receipt.goal_version == record.goal_version
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
        candidate = self._persist(
            record.model_copy(
                update={
                    "wake_receipts": receipts,
                    "wake_archive_count": archive_count,
                    "wake_archive_digest": archive_digest,
                    "last_intervention": intervention,
                }
            ),
            persist=persist,
        )
        if persist and journal is not None:
            journal.append_wakes((receipt,))
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
        return _digest(
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
        cycle_id = record.recovery.cycle_id
        if cycle_id is None:
            raise RecoveryStateError("recovery cycle identity is missing")
        sequence = self._event_sequence(record)
        operation_id = f"recovery-{cycle_id}-{action}-{sequence}"
        digest = self._payload_digest(record, action, payload, sequence)
        return PendingProviderOperation(
            operation_id=operation_id,
            kind="checkpoint" if action == "checkpoint" else "create_or_resume" if action == "relaunch" else "send",
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
            provider_session_id=operation.provider_session_id,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
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
            if action == "checkpoint":
                result = self.provider.checkpoint(CheckpointRequest(operation=operation, label=payload))
            elif action == "relaunch":
                result = self.provider.create_or_resume(
                    CreateOrResumeRequest(
                        operation=operation,
                        session_ref=record.session_ref,
                        provider_session_id=operation.provider_session_id or record.session_ref,
                        context_ref=record.checkpoint_reference,
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

    @staticmethod
    def _action_payload(
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
        if action == "relaunch":
            return f"Resume from governed checkpoint {record.checkpoint_reference}."
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

    @staticmethod
    def _is_initial_create(record: JoinedLaneRecord) -> bool:
        """Return true for the one pending operation created before joining."""

        return bool(
            record.provider.kind == "tophand"
            and record.current_update is None
            and record.pending_operation is not None
            and record.pending_operation.kind == "create_or_resume"
            and record.recovery.attempted_remedy == "bootstrap"
        )

    @staticmethod
    def _initial_ownership_failure(
        record: JoinedLaneRecord,
        operation: PendingProviderOperation,
        result: ProviderOperationResult,
    ) -> str | None:
        """Check the complete receipt returned by the restricted Tophand call.

        A handle, session string, or PID alone is not an ownership proof.  The
        receipt must echo the entire pending envelope and the observed process
        identity.  The instance and generation fence PID reuse.
        """

        try:
            validate_operation_result(operation, result)
        except ContractValidationError as exc:
            return str(exc)
        if result.provider_session_id != record.session_ref:
            return "initial Tophand result provider_session_id does not match the enrolled session"
        ownership = result.ownership
        if not isinstance(ownership, Mapping):
            return "initial Tophand result lacks canonical ownership evidence"
        if ownership.get("authoritative") is not True:
            return "initial Tophand ownership is not authoritative"
        if ownership.get("status") not in {"owned", "authoritative", "ok"}:
            return "initial Tophand ownership is not owned"
        expected_fields: tuple[tuple[str, object], ...] = (
            ("operation_id", operation.operation_id),
            ("lane_id", record.lane_id),
            ("provider_handle", operation.provider_handle),
            ("provider_instance_id", operation.provider_instance_id),
            ("provider_generation", operation.provider_generation),
        )
        for field, expected in expected_fields:
            if ownership.get(field) != expected:
                return f"initial Tophand ownership {field} does not match the pending envelope"
        session_value = ownership.get("session_ref", ownership.get("provider_session_id"))
        if session_value != record.session_ref or ownership.get("session_ref") not in (None, record.session_ref):
            return "initial Tophand ownership session_ref does not match the enrolled session"
        if ownership.get("provider_session_id") not in (None, record.session_ref):
            return "initial Tophand ownership provider_session_id does not match the enrolled session"

        provider_pid = result.provider_pid
        owner_pid = result.owner_pid
        if isinstance(provider_pid, bool) or not isinstance(provider_pid, int) or provider_pid < 1:
            return "initial Tophand result lacks a positive provider_pid"
        if isinstance(owner_pid, bool) or owner_pid != provider_pid:
            return "initial Tophand owner_pid does not match provider_pid"
        if ownership.get("provider_pid") != provider_pid or ownership.get("owner_pid") != owner_pid:
            return "initial Tophand ownership PID does not match the result"
        observed_process = ownership.get("observed_process")
        if not isinstance(observed_process, Mapping) or observed_process.get("pid") != provider_pid:
            return "initial Tophand ownership lacks matching observed process evidence"
        if result.observed_process is not None and dict(result.observed_process) != dict(observed_process):
            return "initial Tophand observed process changed between result and ownership"
        return None

    def _finish_initial_create(
        self,
        record: JoinedLaneRecord,
        current: datetime,
        facts: tuple[OperatingFact, ...],
        persist: bool,
    ) -> RecoveryDecision:
        operation, result = record.pending_operation, record.last_operation_result
        if operation is None or result is None:
            raise RecoveryStateError("initial create result has no pending operation")
        if result.status not in {"accepted", "consumed"}:
            return self._pending(record, current, "initial Tophand create has no accepted ownership receipt", persist)
        failure = self._initial_ownership_failure(record, operation, result)
        if failure is not None:
            return self._pending(record, current, failure, persist)
        check = self._check(current, "Continue the enrolled Tophand goal", "a material Tophand lane update")
        recovery = record.recovery.model_copy(
            update={"stage": "diagnostic", "next_allowed_attempt": check.at, "pending_payload": None}
        )
        candidate = self._persist(
            record.model_copy(update={"pending_operation": None, "recovery": recovery, "next_check": check}),
            persist=persist,
        )
        return self._decision("relaunch", candidate, "exact Tophand launch ownership is proven", operation, facts=facts)

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
        if action == "checkpoint":
            reference = (
                find_recovery_checkpoint_receipt(self.state_root, self._checkpoint_binding(record, operation))
                if self.state_root is not None
                else None
            )
            if reference is None:
                return self._pending(record, current, "checkpoint lacks a signed, sealed RESCUE receipt", persist)
            check = self._check(current, "Relaunch from the governed checkpoint", "the rotated physical session is observed")
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
            return self._decision("checkpoint", candidate, "governed checkpoint validated", operation, facts=facts)
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
                provider_session_id=expected_session_id,
                idempotency_key=pending.idempotency_key,
                payload_digest=pending.payload_digest,
                provider_instance_id=pending.provider_instance_id,
                provider_generation=pending.provider_generation,
                status="consumed" if consumed else "accepted",
                accepted=True,
                consumed=consumed,
                observed_at=update.observed_at,
                evidence=update.event_id,
            )
        return stored

    def _reconcile_pending(
        self,
        record: JoinedLaneRecord,
        goal: GoalRecord,
        current: datetime,
        facts: tuple[OperatingFact, ...],
        persist: bool,
    ) -> RecoveryDecision:
        if self._is_initial_create(record):
            # The pending envelope was allocated before any provider call.  A
            # stored accepted/consumed result is a crash-window recovery, not
            # permission to allocate a new operation.  Keep it pending until
            # the same receipt proves ownership exactly.
            stored = record.last_operation_result
            if stored is not None and stored.status in {"accepted", "consumed", "rejected"}:
                return self._finish_initial_create(record, current, facts, persist)
            pending = record.pending_operation
            if pending is None:
                raise RecoveryStateError("initial Tophand create is missing its pending operation")
            payload = pending.payload or record.recovery.pending_payload or ""
            retried = self._invoke(record, pending, "relaunch", payload, current)
            recorded = self._persist(record.model_copy(update={"last_operation_result": retried}), persist=persist)
            return self._finish_initial_create(recorded, current, facts, persist)

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
        # RecoveryEngine owns the complete mutation path.  A legacy mutable
        # JoinedLaneReconciler may still be passed by an old direct caller, but
        # it is deliberately not consulted here: doing so would create a
        # second recovery state machine and could replay a provider call.
        return self._pending(record, current, "pending operation lacks exact provider evidence", persist)

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
        lane_id: str | None = None,
        identity_resolver: Callable[[GoalRecord, Sequence[OperatingFact]], ProviderIdentity | None] | None = None,
        operating_facts_reader: Callable[[], Sequence[OperatingFact]] | None = None,
        lane_lock_timeout_seconds: float = 5.0,
    ) -> None:
        if lane_lock_timeout_seconds <= 0:
            raise ValueError("lane_lock_timeout_seconds must be positive")
        self.state_root = state_root
        self.provider_resolver = provider_resolver
        self.goal_root = goal_root or state_root
        self.ledger_key_path = ledger_key_path or state_root / "ledger.key"
        self.facts_reader = facts_reader
        self.lane_id = lane_id
        self.identity_resolver = identity_resolver or top_hand_identity_from_facts
        self.operating_facts_reader = operating_facts_reader or (lambda: read_operating_facts().facts)
        self.lane_lock_timeout_seconds = lane_lock_timeout_seconds

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
        known.update(
            receipt.wake_id for receipt in journal.load_wakes() if receipt.goal_version == record.goal_version
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

    def _facts_for_bootstrap(self) -> tuple[OperatingFact, ...]:
        try:
            return tuple(self.operating_facts_reader())
        except Exception as exc:  # noqa: BLE001 - unavailable facts are a wait, not a guess
            logger.warning("recovery_bootstrap_facts_unavailable", error=str(exc))
            return ()

    def _bootstrap_goals(
        self,
        store: CanonicalJoinedLaneStore,
        *,
        now: datetime | None,
    ) -> tuple[tuple[RecoveryDecision, ...], set[str]]:
        """Persist a pending Tophand create before any provider call.

        The returned lane set prevents the new row from being processed again
        in the same pass.  A second supervisor wins or loses the same lane
        lock and always rereads the canonical row before making a decision.
        """

        decisions: list[RecoveryDecision] = []
        created_lanes: set[str] = set()
        try:
            goals = tuple(load_goals(self.goal_root))
        except Exception as exc:  # noqa: BLE001 - goal store outage is fail closed
            logger.warning("recovery_bootstrap_goals_unavailable", error=str(exc))
            return (), created_lanes
        for goal in goals:
            if (
                not goal.goal_id.strip()
                or not goal.enrolled_done_when.strip()
                or goal.status in DONE_STATUSES
                or goal.status == "held"
                or not goal.session_ref.startswith("tophand:")
                or (self.lane_id is not None and goal.lane_id != self.lane_id)
            ):
                continue
            lock = LaneLock(goal.session_ref, lock_dir=self.state_root / "locks")
            acquired = False
            try:
                acquired = lock.acquire(blocking=True, timeout_seconds=self.lane_lock_timeout_seconds)
                if not acquired:
                    continue
                if store.load(goal.lane_id) is not None:
                    continue
                facts = self._facts_for_bootstrap()
                if not facts:
                    continue
                identity = self.identity_resolver(goal, facts)
                if identity is None:
                    continue
                operation = top_hand_create_operation(goal, identity, now=now)
                pending = top_hand_bootstrap_record(goal, identity, operation, now=now)
                try:
                    stored = cast(JoinedLaneRecord, store.create(pending))
                except Exception as exc:  # noqa: BLE001 - a concurrent winner is harmless
                    if store.load(goal.lane_id) is not None:
                        continue
                    raise exc
                created_lanes.add(stored.lane_id)
                provider = self.provider_resolver(stored)
                journal = EventJournal(self.state_root, stored.lane_id)
                journal_events = tuple(journal.load())
                try:
                    key = self.ledger_key_path.read_bytes()
                except OSError:
                    key = None
                ladder = ResponseLadder(
                    IncidentStore(self.state_root, stored.lane_id),
                    journal_events=journal_events,
                    ledger_key=key,
                )
                decisions.append(
                    RecoveryEngine(
                        provider=provider,
                        state_root=self.state_root,
                        goal_root=self.goal_root,
                        journal=journal,
                        response_ladder=ladder,
                    ).run_once(
                        stored,
                        now=now,
                        goal=goal,
                        facts=facts,
                    )
                )
            except LaneLockError as exc:
                logger.warning("recovery_bootstrap_lane_lock_unavailable", lane_id=goal.lane_id, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - one goal must not abort the pass
                logger.warning(
                    "recovery_bootstrap_failed_closed",
                    lane_id=goal.lane_id,
                    session_ref=goal.session_ref,
                    error=str(exc),
                )
            finally:
                if acquired:
                    lock.release()
        return tuple(decisions), created_lanes

    def _run_record_locked(
        self,
        store: CanonicalJoinedLaneStore,
        snapshot: JoinedLaneRecord,
        *,
        now: datetime | None,
    ) -> RecoveryDecision:
        lock = LaneLock(snapshot.session_ref, lock_dir=self.state_root / "locks")
        acquired = False
        try:
            acquired = lock.acquire(blocking=True, timeout_seconds=self.lane_lock_timeout_seconds)
            if not acquired:
                raise LaneLockError(f"lane lock unavailable for {snapshot.session_ref}")
            record = cast(JoinedLaneRecord, store.require(snapshot.lane_id))
            if record.recovery.stage in {"none", "complete"}:
                return self._decision_for(record, "lane is no longer unfinished")
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
                kwargs.update(wake_id=wake[0], observed_wake_condition=wake[1], wake_event_sequence=wake[2])
            return RecoveryEngine(
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
        finally:
            if acquired:
                lock.release()

    @staticmethod
    def _decision_for(record: JoinedLaneRecord, reason: str) -> RecoveryDecision:
        return RecoveryDecision("noop", record.recovery.stage, record, reason)

    def run_once(self, *, now: datetime | None = None) -> tuple[RecoveryDecision, ...]:
        decisions, created_lanes = self._bootstrap_goals(CanonicalJoinedLaneStore(self.state_root), now=now)
        store = CanonicalJoinedLaneStore(self.state_root)
        decisions = list(decisions)
        for value in store.unfinished():
            snapshot = cast(JoinedLaneRecord, value)
            if snapshot.lane_id in created_lanes or snapshot.recovery.stage in {"none", "complete"}:
                continue
            try:
                decisions.append(self._run_record_locked(store, snapshot, now=now))
            except LaneLockError as exc:
                logger.warning(
                    "recovery_supervision_lane_lock_unavailable",
                    lane_id=snapshot.lane_id,
                    session_ref=snapshot.session_ref,
                    error=str(exc),
                )
                decisions.append(RecoveryDecision("waiting", snapshot.recovery.stage, snapshot, f"recovery lane lock unavailable: {exc}"))
            except Exception as exc:  # noqa: BLE001 - one lane must not abort the pass
                logger.warning(
                    "recovery_supervision_lane_failed_closed",
                    lane_id=snapshot.lane_id,
                    session_ref=snapshot.session_ref,
                    error=str(exc),
                )
                decisions.append(
                    RecoveryDecision(
                        "waiting",
                        snapshot.recovery.stage,
                        snapshot,
                        f"recovery supervision failed closed for this lane: {exc}",
                    )
                )
        return tuple(decisions)


def run_recovery_supervision(supervisor: RecoverySupervisor) -> tuple[RecoveryDecision, ...]:
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
    "confirm_useful_progress",
    "load_recovery_records",
    "record_pause_recovery",
    "recovery_records_path",
    "run_recovery_check",
    "run_recovery_supervision",
    "schedule_recovery_check",
]

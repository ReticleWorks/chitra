"""Durable per-lane state for the persistent supervision loop.

This module records only supervision state transitions.  Delivery proof stays
in :mod:`chitra.ledger`, incidents stay in ``detect.ladder``, and the queue
owns delivery retries.  Keeping those concerns separate makes a restart able
to reconcile the state without inventing a second queue or recovery system.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA = "chitra.supervision.v1"
SupervisionState = Literal[
    "observing",
    "action_pending",
    "action_queued",
    "awaiting_progress",
    "blocked",
    "completion_verified",
]

_LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ALLOWED_TRANSITIONS: dict[SupervisionState | None, frozenset[SupervisionState]] = {
    None: frozenset({"observing", "action_pending", "blocked", "completion_verified"}),
    "observing": frozenset(
        {"observing", "action_pending", "action_queued", "awaiting_progress", "blocked", "completion_verified"}
    ),
    "action_pending": frozenset({"action_pending", "action_queued", "blocked", "completion_verified"}),
    "action_queued": frozenset(
        {"action_queued", "awaiting_progress", "action_pending", "blocked", "completion_verified"}
    ),
    "awaiting_progress": frozenset(
        {"awaiting_progress", "observing", "action_pending", "blocked", "completion_verified"}
    ),
    "blocked": frozenset(
        {"blocked", "observing", "action_pending", "action_queued", "awaiting_progress", "completion_verified"}
    ),
    "completion_verified": frozenset({"completion_verified"}),
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class SupervisionRecord(BaseModel):
    """One immutable, hash-addressed supervision transition."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_name: Literal["chitra.supervision.v1"] = Field(default="chitra.supervision.v1", alias="schema")
    revision: int = Field(ge=1)
    event_id: str = ""
    at: str = Field(default_factory=_utc_now, min_length=1)
    lane: str = Field(min_length=1)
    session_ref: str = Field(min_length=1)
    goal_version: int = Field(ge=1)
    goal_digest: str = Field(min_length=1)
    state: SupervisionState
    reason: str = Field(min_length=1)
    finding_fingerprint: str = ""
    stage: str = ""
    order_id: str = ""
    order_marker: str = ""
    observed_event_id: str = ""
    turn_boundary_event_id: str = ""
    attempt: int = Field(default=0, ge=0)
    next_retry_at: str = ""
    obstacle: str = ""
    recovery_count: int = Field(default=0, ge=0)

    @field_validator("lane")
    @classmethod
    def _safe_lane(cls, value: str) -> str:
        if _LANE_RE.fullmatch(value) is None:
            raise ValueError(f"unsafe lane name: {value!r}")
        return value

    @field_validator("at")
    @classmethod
    def _timestamp_has_timezone(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("at must be an ISO8601 datetime") from exc
        if parsed.tzinfo is None:
            raise ValueError("at must include a timezone")
        return value


def _canonical_record(record: SupervisionRecord) -> bytes:
    payload = record.model_dump(mode="json", by_alias=True)
    payload.pop("event_id", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _event_id(record: SupervisionRecord) -> str:
    return f"sha256:{hashlib.sha256(_canonical_record(record)).hexdigest()}"


def _with_verified_event_id(record: SupervisionRecord) -> SupervisionRecord:
    expected = _event_id(record)
    if not record.event_id:
        return record.model_copy(update={"event_id": expected})
    if record.event_id != expected:
        raise ValueError(f"supervision event_id does not match its row: {record.event_id!r}")
    return record


def _goal_value(goal: object, name: str, default: Any = "") -> Any:
    value = getattr(goal, name, default)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def goal_digest(goal: object) -> str:
    """Hash the exact frozen identity and completion contract of ``goal``.

    Tactical fields such as ``status``, ``now``, and open asks are deliberately
    excluded.  The enrolled completion items are represented field by field so
    the digest does not depend on Pydantic's serialization details.
    """
    raw_items = _goal_value(goal, "enrolled_done_when_items", ()) or ()
    items = [
        {
            "id": _goal_value(item, "id"),
            "text": _goal_value(item, "text"),
            "validator": _goal_value(item, "validator"),
            "required_receipt": _goal_value(item, "required_receipt"),
        }
        for item in raw_items
    ]
    frozen = {
        "session_ref": _goal_value(goal, "session_ref"),
        "lane_id": _goal_value(goal, "lane_id"),
        "goal_version": _goal_value(goal, "goal_version"),
        "goal": _goal_value(goal, "goal"),
        "intent": _goal_value(goal, "intent"),
        "scope": _goal_value(goal, "scope"),
        "done_when": _goal_value(goal, "done_when"),
        "enrolled_done_when": _goal_value(goal, "enrolled_done_when"),
        "enrolled_done_when_items": items,
    }
    encoded = json.dumps(frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_order_id(
    session_ref: str,
    goal_version: int,
    fingerprint: str,
    stage: str,
    *,
    retry_attempt: int = 0,
) -> str:
    """Return the stable queue identity for one incident stage."""
    if not session_ref.strip() or not fingerprint.strip() or not stage.strip():
        raise ValueError("session_ref, fingerprint, and stage must be non-empty")
    if isinstance(goal_version, bool) or not isinstance(goal_version, int) or goal_version < 1:
        raise ValueError("goal_version must be a positive integer")
    if isinstance(retry_attempt, bool) or not isinstance(retry_attempt, int) or retry_attempt < 0:
        raise ValueError("retry_attempt must be a non-negative integer")
    encoded = json.dumps(
        [session_ref, goal_version, fingerprint, stage, retry_attempt],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    suffix = f"-r{retry_attempt}" if retry_attempt else ""
    return f"chitra-supervision-{hashlib.sha256(encoded).hexdigest()[:24]}{suffix}"


class SupervisionLedger:
    """Locked append-only transition log for one safe lane."""

    def __init__(self, state_root: Path, lane: str) -> None:
        if _LANE_RE.fullmatch(lane) is None:
            raise ValueError(f"unsafe lane name: {lane!r}")
        self.lane = lane
        self.directory = state_root / "supervision"
        self.path = self.directory / f"{lane}.jsonl"
        self.lock_path = self.directory / f"{lane}.lock"

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load_unlocked(self) -> list[SupervisionRecord]:
        try:
            raw_lines = self.path.read_bytes().splitlines(keepends=True)
        except FileNotFoundError:
            return []
        records: list[SupervisionRecord] = []
        for number, raw_line in enumerate(raw_lines, 1):
            if not raw_line.endswith((b"\n", b"\r")):
                # A process can die after writing only part of its final row.
                # It is safe to discard that one row because the prior row is
                # still authoritative and restart reconciliation will append a
                # new transition.
                continue
            if not raw_line.strip():
                continue
            try:
                record = _with_verified_event_id(SupervisionRecord.model_validate_json(raw_line))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f"invalid supervision row {self.path}:{number}: {exc}") from exc
            if record.lane != self.lane:
                raise ValueError(f"supervision row lane mismatch at {self.path}:{number}")
            records.append(record)
        for previous, current in zip(records, records[1:], strict=False):
            if current.revision != previous.revision + 1:
                raise ValueError(f"supervision revisions are not contiguous at {self.path}")
            if current.session_ref != previous.session_ref:
                raise ValueError(f"supervision session changed at {self.path}")
            if current.state not in _ALLOWED_TRANSITIONS[previous.state]:
                raise ValueError(f"invalid supervision transition {previous.state!r} -> {current.state!r}")
        return records

    def _repair_incomplete_tail_unlocked(self) -> None:
        """Drop only a non-newline-terminated final row before appending."""
        try:
            payload = self.path.read_bytes()
        except FileNotFoundError:
            return
        if not payload or payload.endswith((b"\n", b"\r")):
            return
        last_newline = max(payload.rfind(b"\n"), payload.rfind(b"\r"))
        fd = os.open(str(self.path), os.O_WRONLY)
        try:
            os.ftruncate(fd, last_newline + 1)
            os.fsync(fd)
        finally:
            os.close(fd)

    def load(self) -> list[SupervisionRecord]:
        """Load rows, ignoring only a non-newline-terminated final row."""
        with self._lock():
            return self._load_unlocked()

    def latest(self) -> SupervisionRecord | None:
        records = self.load()
        return records[-1] if records else None

    def latest_for_action(
        self,
        finding_fingerprint: str,
        stage: str,
        *,
        goal_digest_value: str | None = None,
    ) -> SupervisionRecord | None:
        """Return the newest row for one action, despite sibling actions.

        A lane can have a corrective finding and a routine question in flight
        at the same time.  The lane's newest row is therefore not a safe
        retry cursor for either action.  Callers use this keyed lookup to
        preserve attempt counts and delivery state across fair scheduling.
        """
        for record in reversed(self.load()):
            if record.finding_fingerprint != finding_fingerprint or record.stage != stage:
                continue
            if goal_digest_value is not None and record.goal_digest != goal_digest_value:
                continue
            return record
        return None

    def latest_consumed_boundary(
        self,
        *,
        goal_digest_value: str | None = None,
    ) -> SupervisionRecord | None:
        """Return the newest current-goal row with a proven turn boundary."""
        for record in reversed(self.load()):
            if record.turn_boundary_event_id and (
                goal_digest_value is None or record.goal_digest == goal_digest_value
            ):
                return record
        return None

    def append(self, record: SupervisionRecord) -> SupervisionRecord:
        """Append one verified row with an fsync and monotonic revision."""
        with self._lock():
            return self._append_unlocked(record)

    def _append_unlocked(self, record: SupervisionRecord) -> SupervisionRecord:
        """Validate and append one row. The caller must hold ``_lock``."""
        records = self._load_unlocked()
        self._repair_incomplete_tail_unlocked()
        previous = records[-1] if records else None
        if record.lane != self.lane:
            raise ValueError(f"supervision row lane {record.lane!r} does not match {self.lane!r}")
        expected_revision = (previous.revision + 1) if previous is not None else 1
        if record.revision != expected_revision:
            raise ValueError(f"supervision revision must be {expected_revision}, got {record.revision}")
        if previous is not None:
            if record.session_ref != previous.session_ref:
                raise ValueError("supervision session cannot change within one lane ledger")
            if record.state not in _ALLOWED_TRANSITIONS[previous.state]:
                raise ValueError(f"invalid supervision transition {previous.state!r} -> {record.state!r}")
        record = _with_verified_event_id(record)
        encoded = (record.model_dump_json(by_alias=True) + "\n").encode("utf-8")
        fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return record

    def transition(
        self,
        *,
        state: SupervisionState,
        session_ref: str,
        goal_version: int,
        goal_digest_value: str,
        reason: str,
        at: str | None = None,
        finding_fingerprint: str | None = None,
        stage: str | None = None,
        order_id: str | None = None,
        order_marker: str | None = None,
        observed_event_id: str | None = None,
        turn_boundary_event_id: str | None = None,
        attempt: int | None = None,
        next_retry_at: str | None = None,
        obstacle: str | None = None,
        recovery_count: int | None = None,
    ) -> SupervisionRecord:
        """Build and append a transition, carrying forward omitted metadata."""
        with self._lock():
            previous_records = self._load_unlocked()
            previous = previous_records[-1] if previous_records else None
            if previous is not None and session_ref != previous.session_ref:
                raise ValueError("supervision session cannot change within one lane ledger")

            def carry(name: str, supplied: Any) -> Any:
                return getattr(previous, name) if supplied is None and previous is not None else (supplied if supplied is not None else "")

            record = SupervisionRecord(
                revision=(previous.revision + 1) if previous is not None else 1,
                at=at or _utc_now(),
                lane=self.lane,
                session_ref=session_ref,
                goal_version=goal_version,
                goal_digest=goal_digest_value,
                state=state,
                reason=reason,
                finding_fingerprint=carry("finding_fingerprint", finding_fingerprint),
                stage=carry("stage", stage),
                order_id=carry("order_id", order_id),
                order_marker=carry("order_marker", order_marker),
                observed_event_id=carry("observed_event_id", observed_event_id),
                turn_boundary_event_id=carry("turn_boundary_event_id", turn_boundary_event_id),
                attempt=carry("attempt", attempt) if attempt is not None or previous is not None else 0,
                next_retry_at=carry("next_retry_at", next_retry_at),
                obstacle=carry("obstacle", obstacle),
                recovery_count=carry("recovery_count", recovery_count) if recovery_count is not None or previous is not None else 0,
            )
            return self._append_unlocked(record)

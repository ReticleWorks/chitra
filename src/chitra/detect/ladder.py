"""Incident store and the nudge → redirect → RESCUE → relaunch ladder.

The store is durable, append-only per lane, and keyed by
``(lane, finding fingerprint)``. The ladder advances only when the *same*
fingerprint recurs after proven consumption of the prior stage's order —
a signed delivery-ledger entry plus a bound user event and turn boundary in
the journal (the PR #93 receipt semantics). Elapsed time never establishes
or advances anything.
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from chitra.journal.models import CanonicalEvent, CanonicalType
from chitra.ledger import LedgerEntry, message_hash, verify_entry

from .detectors import Finding

LADDER_STAGES: tuple[str, ...] = ("nudge", "redirect", "rescue", "relaunch")

_LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

IncidentStage = Literal["nudge", "redirect", "rescue", "relaunch"]


class ConsumptionProof(BaseModel):
    """Receipt that an issued ladder order was actually consumed.

    ``ledger_entry`` is the signed delivery-ledger row proving the text was
    pasted; ``user_event_id`` names the journal user-turn event whose payload
    hash binds the same marker text, and ``turn_event_id`` names the next
    FINAL_RESPONSE boundary after it.
    """

    model_config = ConfigDict(frozen=True)

    ledger_entry: LedgerEntry
    ledger_key_hex: str = ""
    user_event_id: str
    turn_event_id: str


class IncidentRecord(BaseModel):
    """One durable incident keyed ``(lane, finding fingerprint)``."""

    model_config = ConfigDict(frozen=True)

    lane: str
    fingerprint: str
    detector: str
    stage: IncidentStage
    order_marker: str
    opened_at: str
    event_refs: tuple[str, ...]
    unmet_item: str
    expected_next_progress: str
    detail: str
    consumption: ConsumptionProof | None = None


class LadderDecision(BaseModel):
    """Outcome of advancing one finding through the ladder."""

    model_config = ConfigDict(frozen=True)

    action: Literal["open", "hold", "advance"]
    stage: IncidentStage
    record: IncidentRecord
    reason: str


class IncidentStore:
    """Append-only per-lane incident log under ``<state_root>/incidents``."""

    def __init__(self, state_root: Path, lane: str) -> None:
        if _LANE_RE.fullmatch(lane) is None:
            raise ValueError(f"unsafe lane name: {lane!r}")
        self.lane = lane
        self.directory = state_root / "incidents"
        self.path = self.directory / f"{lane}.jsonl"
        self.lock_path = self.directory / f"{lane}.lock"

    def load(self) -> list[IncidentRecord]:
        if not self.path.exists():
            return []
        records: list[IncidentRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    records.append(IncidentRecord.model_validate_json(line))
                except ValueError as exc:
                    raise ValueError(f"invalid incident row {self.path}:{line_number}: {exc}") from exc
        return records

    def latest(self, fingerprint: str) -> IncidentRecord | None:
        for record in reversed(self.load()):
            if record.fingerprint == fingerprint:
                return record
        return None

    def _append(self, record: IncidentRecord) -> IncidentRecord:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    encoded = (record.model_dump_json() + "\n").encode()
                    view = memoryview(encoded)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return record

    def open_incident(self, *, lane: str, finding: Finding, order_marker: str) -> IncidentRecord:
        record = IncidentRecord(
            lane=lane,
            fingerprint=finding.fingerprint,
            detector=finding.detector,
            stage="nudge",
            order_marker=order_marker,
            opened_at=_utc_now(),
            event_refs=finding.event_refs,
            unmet_item=finding.unmet_item,
            expected_next_progress=finding.expected_next_progress,
            detail=finding.detail,
        )
        return self._append(record)

    def attach_consumption(
        self,
        *,
        fingerprint: str,
        order_marker: str,
        proof: ConsumptionProof,
    ) -> IncidentRecord:
        """Bind proven consumption to the newest open record for a fingerprint."""
        records = self.load()
        target = next(
            (record for record in reversed(records) if record.fingerprint == fingerprint and record.order_marker == order_marker),
            None,
        )
        if target is None:
            raise KeyError(f"no open incident for fingerprint {fingerprint!r} at marker {order_marker!r}")
        updated = target.model_copy(update={"consumption": proof})
        return self._append(updated)

    def advance(self, *, fingerprint: str) -> IncidentRecord:
        """Move the newest consumed record for a fingerprint to its next stage."""
        records = self.load()
        target = next((record for record in reversed(records) if record.fingerprint == fingerprint), None)
        if target is None:
            raise KeyError(f"no incident for fingerprint {fingerprint!r}")
        if target.consumption is None:
            raise ValueError("ladder cannot advance without proven consumption")
        index = LADDER_STAGES.index(target.stage)
        if index >= len(LADDER_STAGES) - 1:
            raise ValueError("incident already reached relaunch")
        advanced = target.model_copy(update={"stage": LADDER_STAGES[index + 1], "opened_at": _utc_now(), "consumption": None})
        return self._append(advanced)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ResponseLadder:
    """Decide the response to one recurring finding.

    ``evaluate`` returns exactly one of:

    - ``open``: first sighting; a nudge order should be dispatched;
    - ``hold``: recurrence observed but the previous order's consumption was
      never proven — nothing advances;
    - ``advance``: the same fingerprint recurred after proven consumption of
      the prior stage's order, so the incident moves to the next stage.
    """

    def __init__(
        self,
        store: IncidentStore,
        *,
        journal_events: Sequence[CanonicalEvent] = (),
        ledger_key: bytes | None = None,
    ) -> None:
        self.store = store
        self._events = tuple(journal_events)
        self._ledger_key = ledger_key

    def evaluate(self, *, lane: str, finding: Finding, order_marker: str) -> LadderDecision:
        existing = self.store.latest(finding.fingerprint)
        if existing is None:
            record = self.store.open_incident(lane=lane, finding=finding, order_marker=order_marker)
            return LadderDecision(action="open", stage=record.stage, record=record, reason="first sighting of this finding fingerprint")
        if existing.stage == "relaunch":
            return LadderDecision(action="hold", stage=existing.stage, record=existing, reason="incident already reached relaunch")
        if existing.consumption is None or not self._consumption_proven(existing):
            return LadderDecision(
                action="hold",
                stage=existing.stage,
                record=existing,
                reason="prior order consumption is not proven; elapsed time never advances the ladder",
            )
        advanced = self.store.advance(fingerprint=finding.fingerprint)
        return LadderDecision(
            action="advance", stage=advanced.stage, record=advanced, reason="same fingerprint recurred after proven consumption"
        )

    def _consumption_proven(self, record: IncidentRecord) -> bool:
        proof = record.consumption
        if proof is None:
            return False
        if self._ledger_key is not None and not verify_entry(proof.ledger_entry, key=self._ledger_key):
            return False
        events_by_id = {event.event_id: event for event in self._events}
        user_event = events_by_id.get(proof.user_event_id)
        turn_event = events_by_id.get(proof.turn_event_id)
        if user_event is None or turn_event is None:
            return False
        user_text = _payload_text(user_event)
        if record.order_marker not in user_text:
            return False
        if proof.ledger_entry.message_hash != message_hash(user_text):
            return False
        if turn_event.normalized_type is not CanonicalType.FINAL_RESPONSE:
            return False
        return not (
            self._events and _position_of(self._events, turn_event.event_id) <= _position_of(self._events, user_event.event_id)
        )


def _payload_text(event: CanonicalEvent) -> str:
    value = event.payload.get("text")
    return value if isinstance(value, str) else ""


def _position_of(events: tuple[CanonicalEvent, ...], event_id: str) -> int:
    for position, event in enumerate(events):
        if event.event_id == event_id:
            return position
    return -1


__all__ = [
    "ConsumptionProof",
    "IncidentRecord",
    "IncidentStore",
    "LADDER_STAGES",
    "LadderDecision",
    "ResponseLadder",
]

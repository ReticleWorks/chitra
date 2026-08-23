"""Durable per-lane journal storage and progress derivation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..session_contract import WakeReceipt
from .models import (
    CanonicalEvent,
    CanonicalType,
    ProgressClass,
    ProgressClassification,
)

CLASSIFIER_VERSION = "chitra-progress-classifier.v1"
_LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_PROGRESS_KEYS = frozenset(
    {
        "artifact_changed",
        "diagnostic_changed",
        "required_item_verified",
        "targeted_check_flipped",
        "live_boundary_exercised",
    }
)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def classify_progress(
    event: CanonicalEvent,
    *,
    goal_version: str,
    related_events: Sequence[CanonicalEvent] = (),
) -> ProgressClassification:
    """Classify only evidence the canonical stream actually establishes."""

    if event.goal_version is not None and goal_version != str(event.goal_version):
        raise ValueError("progress classification goal_version does not match its source event")

    evidence = event.payload.get("progress_evidence")
    if isinstance(evidence, dict) and any(evidence.get(key) is True for key in _PROGRESS_KEYS):
        classification = ProgressClass.PROGRESS
        reason = "event carries an explicit scoped state-change evidence marker"
    elif event.payload.get("unchanged") is True:
        classification = ProgressClass.NON_PROGRESS
        reason = "event explicitly reports an unchanged result"
    elif event.normalized_type is CanonicalType.TOOL_CALL:
        classification = ProgressClass.NON_PROGRESS
        reason = "a tool invocation alone does not establish changed state"
    elif event.normalized_type in {
        CanonicalType.FINAL_RESPONSE,
        CanonicalType.COMPACTION,
        CanonicalType.RESUME,
    }:
        classification = ProgressClass.NON_PROGRESS
        reason = f"{event.normalized_type.value} is lifecycle or narration, not work evidence"
    elif event.normalized_type in {CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR}:
        joined_call = next(
            (
                candidate
                for candidate in reversed(related_events)
                if candidate.normalized_type is CanonicalType.TOOL_CALL and candidate.native_join_id == event.native_join_id
            ),
            None,
        )
        classification = ProgressClass.UNKNOWN
        reason = (
            "tool result needs scoped state comparison before it can count as progress"
            if joined_call is not None
            else "tool result has no supplied joined call or scoped state comparison"
        )
    else:
        classification = ProgressClass.UNKNOWN
        reason = "native record does not establish progress or non-progress"

    source_ids = (event.event_id,)
    derivation_id = _digest(
        {
            "classification": classification.value,
            "classifier_version": CLASSIFIER_VERSION,
            "goal_version": goal_version,
            "reason": reason,
            "source_event_ids": source_ids,
        }
    )
    return ProgressClassification(
        derivation_id=derivation_id,
        classification=classification,
        reason=reason,
        source_event_ids=source_ids,
        goal_version=goal_version,
        classifier_version=CLASSIFIER_VERSION,
    )


class EventJournal:
    """An append-only JSONL event journal and derivation log for one lane."""

    def __init__(self, state_root: Path, lane: str) -> None:
        if _LANE_RE.fullmatch(lane) is None:
            raise ValueError(f"unsafe lane name: {lane!r}")
        self.lane = lane
        self.directory = state_root / "journal"
        self.path = self.directory / f"{lane}.jsonl"
        self.progress_path = self.directory / f"{lane}.progress.jsonl"
        self.wake_path = self.directory / f"{lane}.wake.jsonl"
        self.lock_path = self.directory / f"{lane}.lock"

    def load(self) -> list[CanonicalEvent]:
        if not self.path.exists():
            return []
        events: list[CanonicalEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    events.append(CanonicalEvent.model_validate_json(line))
                except ValueError as exc:
                    raise ValueError(f"invalid journal row {self.path}:{line_number}: {exc}") from exc
        return events

    def load_progress(self) -> list[ProgressClassification]:
        if not self.progress_path.exists():
            return []
        rows: list[ProgressClassification] = []
        with self.progress_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    rows.append(ProgressClassification.model_validate_json(line))
                except ValueError as exc:
                    raise ValueError(f"invalid progress row {self.progress_path}:{line_number}: {exc}") from exc
        return rows

    def load_wakes(self) -> list[WakeReceipt]:
        """Load the append-only wake archive used after inline compaction."""

        if not self.wake_path.exists():
            return []
        rows: list[WakeReceipt] = []
        with self.wake_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    receipt = WakeReceipt.model_validate_json(line)
                except ValueError as exc:
                    raise ValueError(f"invalid wake row {self.wake_path}:{line_number}: {exc}") from exc
                if receipt.lane_id != self.lane:
                    raise ValueError(f"wake row lane {receipt.lane_id!r} does not match journal lane {self.lane!r}")
                rows.append(receipt)
        return rows

    def append(self, events: Iterable[CanonicalEvent]) -> tuple[CanonicalEvent, ...]:
        candidates = tuple(events)
        if not candidates:
            return ()
        for event in candidates:
            if event.lane != self.lane:
                raise ValueError(f"event lane {event.lane!r} does not match journal lane {self.lane!r}")
        return self._append_unique(self.path, candidates, "event_id")

    def append_progress(self, rows: Iterable[ProgressClassification]) -> tuple[ProgressClassification, ...]:
        return self._append_unique(self.progress_path, tuple(rows), "derivation_id")

    def append_wakes(self, rows: Iterable[WakeReceipt]) -> tuple[WakeReceipt, ...]:
        candidates = tuple(rows)
        for receipt in candidates:
            if receipt.lane_id != self.lane:
                raise ValueError(f"wake lane {receipt.lane_id!r} does not match journal lane {self.lane!r}")
        return self._append_unique(self.wake_path, candidates, ("wake_id", "goal_version"))

    def proves_named_wake(
        self,
        *,
        wake_id: str,
        event_sequence: int,
        goal_id: str,
        session_ref: str,
        goal_version: int,
        wake_condition: str,
    ) -> bool:
        """Require one exact lane event proving that a named condition changed."""

        events = self.load()
        if event_sequence < 1 or event_sequence > len(events):
            return False
        event = events[event_sequence - 1]
        return (
            event.event_id == wake_id
            and event.lane == self.lane
            and event.goal_ref == goal_id
            and event.session_id == session_ref
            and event.goal_version == goal_version
            and event.payload.get("wake_condition") == wake_condition
            and event.payload.get("wake_condition_changed") is True
        )

    def _append_unique[T: CanonicalEvent | ProgressClassification | WakeReceipt](
        self,
        path: Path,
        candidates: tuple[T, ...],
        id_field: str | tuple[str, ...],
    ) -> tuple[T, ...]:
        if not candidates:
            return ()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                def identity(value: object) -> object:
                    if isinstance(id_field, tuple):
                        return tuple(
                            value.get(field) if isinstance(value, dict) else getattr(value, field)
                            for field in id_field
                        )
                    return value.get(id_field) if isinstance(value, dict) else getattr(value, id_field)

                existing: set[object] = set()
                if path.exists():
                    with path.open("r", encoding="utf-8") as current:
                        for line in current:
                            if not line.strip():
                                continue
                            value = json.loads(line)
                            row_identity = identity(value)
                            if isinstance(id_field, tuple) or isinstance(row_identity, str):
                                existing.add(row_identity)
                new_rows: list[T] = []
                for candidate in candidates:
                    candidate_identity = identity(candidate)
                    if candidate_identity not in existing:
                        new_rows.append(candidate)
                        existing.add(candidate_identity)
                if new_rows:
                    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                    try:
                        os.fchmod(fd, 0o600)
                        encoded = "".join(row.model_dump_json() + "\n" for row in new_rows).encode()
                        view = memoryview(encoded)
                        while view:
                            written = os.write(fd, view)
                            view = view[written:]
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                return tuple(new_rows)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

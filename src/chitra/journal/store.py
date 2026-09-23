"""Durable per-lane journal storage and progress derivation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from chitra._fsio import exclusive_lock

from .models import (
    CanonicalEvent,
    CanonicalType,
    ProgressClass,
    ProgressClassification,
)
from .tools import (
    call_signature,
    check_signature,
    result_class,
    result_fingerprint,
    shell_write_targets,
    tool_class,
)

CLASSIFIER_VERSION = "chitra-progress-classifier.v2"
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
        joined_call = _joined_call(event, related_events)
        prior = _prior_outcome_fingerprints(joined_call, related_events) if joined_call is not None else set()
        classification, reason = _classify_tool_outcome(event, joined_call, prior)
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


def derive_progress_rows(
    events: Sequence[CanonicalEvent],
    *,
    goal_version: str,
) -> tuple[ProgressClassification, ...]:
    """Classify every event in one pass with shared join/signature indexes.

    Produces the same rows as calling :func:`classify_progress` per event but
    builds the join map, call signatures, and prior-outcome fingerprints once
    instead of rescanning the journal for each tool result.
    """
    if not events:
        return ()
    positions = {event.event_id: index for index, event in enumerate(events)}
    calls_by_join: dict[str, CanonicalEvent] = {}
    signature_by_join: dict[str, str] = {}
    fingerprints_by_join: dict[str, set[str]] = {}
    for event in events:
        join = event.native_join_id
        if not isinstance(join, str) or not join:
            continue
        if event.normalized_type is CanonicalType.TOOL_CALL:
            calls_by_join[join] = event
            signature_by_join[join] = call_signature(event)
        elif event.normalized_type in {CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR}:
            fingerprints_by_join.setdefault(join, set()).add(result_fingerprint(event))
    calls_by_signature: dict[str, list[tuple[int, str]]] = {}
    for join, signature in signature_by_join.items():
        calls_by_signature.setdefault(signature, []).append((positions[calls_by_join[join].event_id], join))
    prior_by_join: dict[str, set[str]] = {}
    for entries in calls_by_signature.values():
        entries.sort()
        running: set[str] = set()
        for _position, join in entries:
            prior_by_join[join] = set(running)
            running |= fingerprints_by_join.get(join, set())
    rows: list[ProgressClassification] = []
    for event in events:
        if event.normalized_type in {CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR}:
            join = event.native_join_id if isinstance(event.native_join_id, str) else ""
            classification, reason = _classify_tool_outcome(
                event,
                calls_by_join.get(join),
                prior_by_join.get(join, set()),
            )
            rows.append(_progress_row(event, classification, reason, goal_version=goal_version))
        else:
            rows.append(classify_progress(event, goal_version=goal_version))
    return tuple(rows)


def _progress_row(
    event: CanonicalEvent,
    classification: ProgressClass,
    reason: str,
    *,
    goal_version: str,
) -> ProgressClassification:
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


def _joined_call(
    event: CanonicalEvent,
    related_events: Sequence[CanonicalEvent],
) -> CanonicalEvent | None:
    return next(
        (
            candidate
            for candidate in reversed(related_events)
            if candidate.normalized_type is CanonicalType.TOOL_CALL and candidate.native_join_id == event.native_join_id
        ),
        None,
    )


def _classify_tool_outcome(
    event: CanonicalEvent,
    joined_call: CanonicalEvent | None,
    prior_fingerprints: set[str],
) -> tuple[ProgressClass, str]:
    """Classify a tool result from observable outcome signals.

    A write result without error is artifact progress; a check result
    that is new or differs from the prior run of the same command is
    diagnostic progress; a re-run of the identical call returning the
    identical outcome is repeated work — non-progress.
    """
    if joined_call is None:
        return (
            ProgressClass.UNKNOWN,
            "tool result has no supplied joined call or scoped state comparison",
        )
    outcome = result_class(event)
    cls = tool_class(joined_call.payload.get("tool_name"))
    if cls == "write":
        if outcome in {"error", "fail"}:
            return ProgressClass.NON_PROGRESS, "write call failed"
        return ProgressClass.PROGRESS, "write call completed without an error result"
    if cls == "shell":
        if outcome == "error":
            return ProgressClass.NON_PROGRESS, "command result was an error"
        check = check_signature(joined_call)
        fingerprint = result_fingerprint(event)
        if check is not None:
            if not prior_fingerprints:
                return ProgressClass.PROGRESS, "first check run produced a new result"
            if fingerprint not in prior_fingerprints:
                return ProgressClass.PROGRESS, "check run flipped or differed from its prior result"
            return ProgressClass.NON_PROGRESS, "check rerun returned the same outcome as its prior run"
        if outcome == "fail":
            return ProgressClass.NON_PROGRESS, "command exited nonzero"
        if shell_write_targets(joined_call):
            return ProgressClass.PROGRESS, "command wrote files or patched the worktree"
        return ProgressClass.UNKNOWN, "command output does not establish a state change"
    return ProgressClass.UNKNOWN, "tool result needs scoped state comparison before it can count as progress"


def _prior_outcome_fingerprints(
    call: CanonicalEvent,
    events: Sequence[CanonicalEvent],
) -> set[str]:
    """Result fingerprints produced by earlier calls with the same call signature."""
    if call.native_join_id is None:
        return set()
    signature = call_signature(call)
    earlier_joins: set[str] = set()
    for candidate in events:
        if candidate.event_id == call.event_id:
            break
        if (
            candidate.normalized_type is CanonicalType.TOOL_CALL
            and candidate.native_join_id is not None
            and call_signature(candidate) == signature
        ):
            earlier_joins.add(candidate.native_join_id)
    if not earlier_joins:
        return set()
    return {
        result_fingerprint(candidate)
        for candidate in events
        if candidate.normalized_type in (CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR)
        and candidate.native_join_id in earlier_joins
    }


class EventJournal:
    """An append-only JSONL event journal and derivation log for one lane."""

    def __init__(self, state_root: Path, lane: str) -> None:
        if _LANE_RE.fullmatch(lane) is None:
            raise ValueError(f"unsafe lane name: {lane!r}")
        self.lane = lane
        self.directory = state_root / "journal"
        self.path = self.directory / f"{lane}.jsonl"
        self.progress_path = self.directory / f"{lane}.progress.jsonl"
        self.lock_path = self.directory / f"{lane}.lock"
        # Per-file scan watermarks for append dedupe: (inode, offset, mtime_ns, ids).
        # The journal only grows by appends under this directory's lock, so an
        # id seen below the watermark cannot reappear above it; an inode swap,
        # a shrink, or a same-size rewrite (mtime moved) forces a full rescan.
        self._id_scans: dict[str, tuple[int, int, int, set[str]]] = {}

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

    def load_from(
        self,
        offset: int,
        *,
        inode: int | None = None,
    ) -> tuple[list[CanonicalEvent], int, int, os.stat_result | None]:
        """Read rows appended after ``offset`` bytes.

        Returns ``(events, start_offset, end_offset, fd_stat)`` where
        ``fd_stat`` describes the descriptor actually read. The caller owns
        the watermark and may pass the inode that watermark belongs to; when
        the live path has since been replaced, the descriptor's inode differs
        and the read restarts at byte zero (``start_offset`` is 0) so a cache
        built on the old file is never mixed with the new one's tail.
        Journal writes append whole lines under the lane lock, so a stored
        offset always lands on a line boundary.
        """
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if not self.path.exists():
            return [], 0, 0, None
        events: list[CanonicalEvent] = []
        with self.path.open("rb") as handle:
            fd_stat = os.fstat(handle.fileno())
            if inode is not None and fd_stat.st_ino != inode:
                offset = 0
            handle.seek(offset)
            start = offset
            position = offset
            for line in handle:
                position += len(line)
                if not line.strip():
                    continue
                try:
                    events.append(CanonicalEvent.model_validate_json(line))
                except ValueError as exc:
                    raise ValueError(f"invalid journal row {self.path} after byte {offset}: {exc}") from exc
        return events, start, position, fd_stat

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

    def _scanned_identities(self, path: Path, id_field: str) -> set[str]:
        """Return every ``id_field`` value already stored, reading only new bytes.

        Append dedupe calls this on every write; keeping a byte watermark per
        file makes a steady-state append O(new rows) instead of O(file). The
        caller must hold ``self.lock_path``. A malformed tail raises the same
        way the previous full-file scan did.
        """
        if not path.exists():
            self._id_scans.pop(str(path), None)
            return set()
        stat = path.stat()
        key = str(path)
        entry = self._id_scans.get(key)
        if (
            entry is None
            or entry[0] != stat.st_ino
            or entry[1] > stat.st_size
            or (stat.st_size == entry[1] and stat.st_mtime_ns != entry[2])
        ):
            offset, ids = 0, set()
        else:
            offset, ids = entry[1], entry[3]
        with path.open("rb") as current:
            current.seek(offset)
            for line in current:
                offset += len(line)
                if not line.strip():
                    continue
                value = json.loads(line)
                identity = value.get(id_field)
                if isinstance(identity, str):
                    ids.add(identity)
        self._id_scans[key] = (stat.st_ino, offset, stat.st_mtime_ns, ids)
        return ids

    def _append_unique[T: CanonicalEvent | ProgressClassification](
        self,
        path: Path,
        candidates: tuple[T, ...],
        id_field: str,
    ) -> tuple[T, ...]:
        if not candidates:
            return ()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with exclusive_lock(self.lock_path, mode=0o600):
            existing = self._scanned_identities(path, id_field)
            new_rows: list[T] = []
            for candidate in candidates:
                identity = getattr(candidate, id_field)
                if identity not in existing:
                    new_rows.append(candidate)
                    existing.add(identity)
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
                    final_stat = os.fstat(fd)
                finally:
                    os.close(fd)
                self._id_scans[str(path)] = (
                    final_stat.st_ino,
                    final_stat.st_size,
                    final_stat.st_mtime_ns,
                    existing,
                )
            return tuple(new_rows)

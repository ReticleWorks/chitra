"""Durable per-lane journal storage and progress derivation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from chitra._fsio import exclusive_lock

from .models import (
    CanonicalEvent,
    CanonicalType,
    ProgressClass,
    ProgressClassification,
)
from .normalizers import _native_key
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


def event_dedupe_key(event: CanonicalEvent) -> tuple[str, str, str, str]:
    """Return the replay-identity of one normalized journal row.

    The key pairs the native record identity with the normalized type and
    payload digest -- the (native key, payload digest) pair scoped by client
    and type -- so rows replayed byte-for-byte collapse while a rewrite of
    the same bytes under another harness name, or a distinct normalization,
    stays a separate event. Events with no native row (for example
    receipt-bound lifecycle events) key on their own event ID and are never
    deduplicated.
    """
    raw_sha = event.raw_sha256
    native = (
        _native_key(event.raw_record or {}, raw_sha)
        if isinstance(raw_sha, str) and raw_sha
        else f"event:{event.event_id}"
    )
    return native, event.normalized_type.value, str(event.client), event.payload_digest


def journal_row_dedupe_key(row: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    """Return the dedupe key for one raw journal line, or None to always keep.

    Mirrors :func:`event_dedupe_key` on unvalidated JSON so compaction can run
    without constructing CanonicalEvent models for every retained row.
    """
    payload_digest = row.get("payload_digest")
    if not isinstance(payload_digest, str) or not payload_digest:
        return None
    raw_sha = row.get("raw_sha256")
    if isinstance(raw_sha, str) and raw_sha:
        record = row.get("raw_record")
        native = _native_key(record if isinstance(record, dict) else {}, raw_sha)
    else:
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return None
        native = f"event:{event_id}"
    normalized_type = row.get("normalized_type")
    client = row.get("client")
    return (
        native,
        normalized_type if isinstance(normalized_type, str) else "",
        client if isinstance(client, str) else str(client),
        payload_digest,
    )


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
    results_by_join: dict[str, list[tuple[CanonicalEvent, str]]] = {}
    for event in events:
        join = event.native_join_id
        if not isinstance(join, str) or not join:
            continue
        if event.normalized_type is CanonicalType.TOOL_CALL:
            calls_by_join[join] = event
            signature_by_join[join] = call_signature(event)
        elif event.normalized_type in {CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR}:
            results_by_join.setdefault(join, []).append((event, result_fingerprint(event)))
    calls_by_signature: dict[str, list[tuple[int, str]]] = {}
    for join, signature in signature_by_join.items():
        calls_by_signature.setdefault(signature, []).append((positions[calls_by_join[join].event_id], join))
    classification_by_event: dict[int, tuple[ProgressClass, str]] = {}
    for entries in calls_by_signature.values():
        entries.sort()
        running: set[str] = set()
        for _position, join in entries:
            call = calls_by_join[join]
            results = results_by_join.get(join, ())
            for result, fingerprint in results:
                classification_by_event[id(result)] = _classify_tool_outcome(result, call, running)
            running.update(fingerprint for _result, fingerprint in results)
    rows: list[ProgressClassification] = []
    for event in events:
        if event.normalized_type in {CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR}:
            classified = classification_by_event.get(id(event))
            if classified is None:
                join = event.native_join_id if isinstance(event.native_join_id, str) else ""
                classified = _classify_tool_outcome(event, calls_by_join.get(join), set())
            classification, reason = classified
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

    def __init__(self, state_root: Path, lane: str, *, lock_timeout: float = -1.0) -> None:
        if _LANE_RE.fullmatch(lane) is None:
            raise ValueError(f"unsafe lane name: {lane!r}")
        self.lane = lane
        self.directory = state_root / "journal"
        self.path = self.directory / f"{lane}.jsonl"
        self.progress_path = self.directory / f"{lane}.progress.jsonl"
        self.lock_path = self.directory / f"{lane}.lock"
        self.lock_timeout = lock_timeout

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
        consume: Callable[[CanonicalEvent, int], None] | None = None,
        max_bytes: int | None = None,
        max_row_bytes: int | None = None,
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
            for line in iter(lambda: handle.readline(max_row_bytes + 1 if max_row_bytes is not None else -1), b""):
                position += len(line)
                if max_row_bytes is not None and len(line) > max_row_bytes:
                    raise ValueError(f"journal row exceeds {max_row_bytes} bytes at {self.path}:{position - len(line)}")
                if not line.strip():
                    continue
                try:
                    event = CanonicalEvent.model_validate_json(line)
                    if consume is None:
                        events.append(event)
                    else:
                        consume(event, len(line))
                except ValueError as exc:
                    raise ValueError(f"invalid journal row {self.path} after byte {offset}: {exc}") from exc
                if max_bytes is not None and position - start >= max_bytes:
                    break
        return events, start, position, fd_stat

    def append(self, events: Iterable[CanonicalEvent]) -> tuple[CanonicalEvent, ...]:
        candidates = tuple(events)
        if not candidates:
            return ()
        for event in candidates:
            if event.lane != self.lane:
                raise ValueError(f"event lane {event.lane!r} does not match journal lane {self.lane!r}")
        return self._append_unique(self.path, candidates, "event_id", content_key=event_dedupe_key)

    def append_progress(self, rows: Iterable[ProgressClassification]) -> tuple[ProgressClassification, ...]:
        return self._append_unique(self.progress_path, tuple(rows), "derivation_id")

    def _scanned_identities(
        self,
        path: Path,
        id_field: str,
        index: sqlite3.Connection,
    ) -> None:
        """Incrementally rebuild a disposable disk index under the journal lock."""
        index.execute("CREATE TABLE IF NOT EXISTS identities (identity TEXT, content TEXT, UNIQUE(identity, content))")
        index.execute("CREATE INDEX IF NOT EXISTS content_keys ON identities(content)")
        index.execute("CREATE TABLE IF NOT EXISTS watermark (inode INTEGER, offset INTEGER, mtime INTEGER)")
        entry = index.execute("SELECT inode, offset, mtime FROM watermark").fetchone()
        stat = path.stat() if path.exists() else None
        if stat is None or entry is None or (
            entry[0] != stat.st_ino or entry[1] > stat.st_size
            or (stat.st_size == entry[1] and entry[2] != stat.st_mtime_ns)
        ):
            index.execute("DELETE FROM identities")
            offset = 0
        else:
            offset = entry[1]
        if stat is not None:
            with path.open("rb") as current:
                current.seek(offset)
                for line in current:
                    offset += len(line)
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    identity = value.get(id_field)
                    if isinstance(identity, str):
                        key = journal_row_dedupe_key(value)
                        index.execute(
                            "INSERT OR IGNORE INTO identities VALUES (?, ?)",
                            (identity, json.dumps(key) if key is not None else None),
                        )
        index.execute("DELETE FROM watermark")
        if stat is not None:
            index.execute("INSERT INTO watermark VALUES (?, ?, ?)", (stat.st_ino, offset, stat.st_mtime_ns))

    def _append_unique[T: CanonicalEvent | ProgressClassification](
        self,
        path: Path,
        candidates: tuple[T, ...],
        id_field: str,
        *,
        content_key: Callable[[T], tuple[str, str, str, str]] | None = None,
    ) -> tuple[T, ...]:
        if not candidates:
            return ()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with exclusive_lock(self.lock_path, mode=0o600, timeout=self.lock_timeout) as held:
            if not held:
                raise BlockingIOError(f"journal writer lock is held: {self.lock_path}")
            index_path = path.with_suffix(".index.sqlite3")
            fd = os.open(index_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            with contextlib.closing(sqlite3.connect(index_path)) as index, index:
                index.execute("PRAGMA cache_size=-1024")
                self._scanned_identities(path, id_field, index)
                return self._append_indexed(index, path, candidates, id_field, content_key)

    def _append_indexed[T: CanonicalEvent | ProgressClassification](
        self,
        index: sqlite3.Connection,
        path: Path,
        candidates: tuple[T, ...],
        id_field: str,
        content_key: Callable[[T], tuple[str, str, str, str]] | None,
    ) -> tuple[T, ...]:
        new_rows: list[T] = []
        for candidate in candidates:
            identity = getattr(candidate, id_field)
            dedupe_key = content_key(candidate) if content_key is not None else None
            encoded_key = json.dumps(dedupe_key) if dedupe_key is not None else None
            if index.execute(
                "SELECT 1 FROM identities WHERE identity=? OR content=? LIMIT 1", (identity, encoded_key)
            ).fetchone():
                continue
            new_rows.append(candidate)
            index.execute("INSERT INTO identities VALUES (?, ?)", (identity, encoded_key))
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
            index.execute("DELETE FROM watermark")
            index.execute("INSERT INTO watermark VALUES (?, ?, ?)",
                          (final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns))
        return tuple(new_rows)

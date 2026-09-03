"""Durable recovery, lane-lifecycle, and worktree checkpoint records.

The single document this module owns deliberately keeps rate-limit recovery and
the lane lifecycle together.  A lane does not acquire a second supervisor or a
second state writer merely because it is paused for longer than a usage hold.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from ._fsio import write_json_atomic
from .goals import LOAD_SHED_HOLD_REASON_PREFIX, GoalRecord, done_when_with_delta, get_goal
from .rate_limit_state import Transaction
from .state_paths import state_dir

logger = structlog.get_logger(__name__)

SCHEMA = "chitra.pause_recovery.v1"
LaneState = Literal["active", "paused", "shelved", "closed"]
LANE_STATES: frozenset[str] = frozenset({"active", "paused", "shelved", "closed"})
_ALLOWED_LANE_TRANSITIONS: dict[LaneState | None, frozenset[LaneState]] = {
    None: frozenset({"active"}),
    "active": frozenset({"paused", "shelved", "closed"}),
    "paused": frozenset({"active", "shelved", "closed"}),
    "shelved": frozenset({"active", "closed"}),
    "closed": frozenset(),
}


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


@dataclass(frozen=True, slots=True)
class WorktreeBinding:
    """The exact repository identity a lane is allowed to resume against."""

    repo_root: str
    git_common_dir: str
    worktree_realpath: str
    branch: str
    upstream: str
    base_sha: str
    head_sha: str
    dirty_digest: str
    untracked_digest: str
    transcript_path: str
    transcript_cursor: str
    transcript_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "repo_root": self.repo_root,
            "git_common_dir": self.git_common_dir,
            "worktree_realpath": self.worktree_realpath,
            "branch": self.branch,
            "upstream": self.upstream,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "dirty_digest": self.dirty_digest,
            "untracked_digest": self.untracked_digest,
            "transcript_path": self.transcript_path,
            "transcript_cursor": self.transcript_cursor,
            "transcript_sha256": self.transcript_sha256,
        }

    @classmethod
    def from_dict(cls, payload: object) -> WorktreeBinding:
        if not isinstance(payload, dict):
            raise ValueError("worktree binding must be an object")
        required = (
            "repo_root",
            "git_common_dir",
            "worktree_realpath",
            "branch",
            "upstream",
            "base_sha",
            "head_sha",
            "dirty_digest",
            "untracked_digest",
            "transcript_path",
            "transcript_cursor",
            "transcript_sha256",
        )
        values: dict[str, str] = {}
        for field in required:
            value = payload.get(field)
            if not isinstance(value, str):
                raise ValueError(f"worktree binding {field} must be a string")
            if field not in {"upstream", "transcript_path", "transcript_cursor", "transcript_sha256"} and not value:
                raise ValueError(f"worktree binding {field} must be non-empty")
            values[field] = value
        return cls(**values)


@dataclass(frozen=True, slots=True)
class WorktreeCheckpoint:
    """An append-only record made immediately before a lifecycle change."""

    checkpoint_id: str
    session_ref: str
    action: str
    recorded_at: str
    resume_note: str
    binding: WorktreeBinding

    def to_dict(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "session_ref": self.session_ref,
            "action": self.action,
            "recorded_at": self.recorded_at,
            "resume_note": self.resume_note,
            "binding": self.binding.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> WorktreeCheckpoint:
        if not isinstance(payload, dict):
            raise ValueError("worktree checkpoint must be an object")
        values: dict[str, str] = {}
        for field in ("checkpoint_id", "session_ref", "action", "recorded_at", "resume_note"):
            value = payload.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"worktree checkpoint {field} must be a non-empty string")
            values[field] = value
        return cls(binding=WorktreeBinding.from_dict(payload.get("binding")), **values)


@dataclass(frozen=True, slots=True)
class LaneLifecycleRecord:
    """One durable transition in Chitra's four-state lane lifecycle."""

    lifecycle_id: str
    session_ref: str
    state: LaneState
    previous_state: LaneState | None
    checkpoint_id: str
    changed_at: str
    resume_note: str
    request_id: str = ""

    @property
    def enforcement_enabled(self) -> bool:
        return self.state == "active"

    @property
    def provider_session_retained(self) -> bool:
        return self.state in {"active", "paused"}

    def to_dict(self) -> dict[str, str | None]:
        return {
            "lifecycle_id": self.lifecycle_id,
            "session_ref": self.session_ref,
            "state": self.state,
            "previous_state": self.previous_state,
            "checkpoint_id": self.checkpoint_id,
            "changed_at": self.changed_at,
            "resume_note": self.resume_note,
            "request_id": self.request_id,
        }

    @classmethod
    def from_dict(cls, payload: object) -> LaneLifecycleRecord:
        if not isinstance(payload, dict):
            raise ValueError("lane lifecycle record must be an object")
        state = payload.get("state")
        previous = payload.get("previous_state")
        if state not in LANE_STATES:
            raise ValueError("lane lifecycle state must be active, paused, shelved, or closed")
        if previous is not None and previous not in LANE_STATES:
            raise ValueError("lane lifecycle previous_state must be a known state or null")
        values: dict[str, str] = {}
        for field in ("lifecycle_id", "session_ref", "checkpoint_id", "changed_at", "resume_note"):
            value = payload.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"lane lifecycle record {field} must be a non-empty string")
            values[field] = value
        request_id = payload.get("request_id", "")
        if not isinstance(request_id, str):
            raise ValueError("lane lifecycle record request_id must be a string")
        return cls(state=state, previous_state=previous, request_id=request_id, **values)


def recovery_records_path(root: Path | None = None) -> Path:
    """Return the consolidated recovery and lifecycle document for ``root``."""
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


def _empty_document() -> dict[str, object]:
    return {"schema": SCHEMA, "records": [], "lifecycle": [], "checkpoints": []}


def _load_document(root: Path | None) -> dict[str, object]:
    path = recovery_records_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_document()
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("pause_recovery.json is not a chitra.pause_recovery.v1 document")
    for field in ("records", "lifecycle", "checkpoints"):
        raw = payload.get(field, [])
        if not isinstance(raw, list):
            raise ValueError(f"pause_recovery.json {field} must be a list")
        payload[field] = raw
    return payload


def _write_document(root: Path | None, document: dict[str, object]) -> None:
    write_json_atomic(recovery_records_path(root), document, fsync=True)


def load_recovery_records(root: Path | None = None) -> list[RecoveryRecord]:
    """Load every recorded rate-limit pause in insertion order."""
    document = _load_document(root)
    return [RecoveryRecord.from_dict(item) for item in cast(list[object], document["records"])]


def _write_recovery_records(root: Path | None, records: list[RecoveryRecord]) -> None:
    document = _load_document(root)
    document["records"] = [record.to_dict() for record in records]
    _write_document(root, document)


def _resume_note(goal: GoalRecord) -> str:
    current_work = goal.now.strip() or goal.intent.strip() or goal.goal.strip()
    return f"Goal at pause: {goal.goal.strip()} Current work: {current_work} Done when: {done_when_with_delta(goal).strip()}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _git(workdir: Path, *args: str, required: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(workdir), *args], check=False, capture_output=True, text=True
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if required:
        detail = result.stderr.strip() or result.stdout.strip() or f"git exited {result.returncode}"
        raise ValueError(f"cannot checkpoint worktree {workdir}: {detail}")
    return ""


def _digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def capture_worktree_binding(
    workdir: Path,
    *,
    transcript_path: Path | None = None,
    transcript_cursor: str = "",
    transcript_sha256: str = "",
) -> WorktreeBinding:
    """Capture worktree and optional transcript identity without changing either.

    The resulting binding is intentionally exact.  A later resume compares it
    to the last durable checkpoint before Chitra gives the lane another turn.
    """
    resolved_workdir = workdir.resolve()
    repo_root = Path(_git(resolved_workdir, "rev-parse", "--show-toplevel")).resolve()
    common_raw = _git(resolved_workdir, "rev-parse", "--git-common-dir")
    common_dir = Path(common_raw)
    if not common_dir.is_absolute():
        common_dir = (resolved_workdir / common_dir).resolve()
    branch = _git(resolved_workdir, "rev-parse", "--abbrev-ref", "HEAD")
    upstream = _git(resolved_workdir, "rev-parse", "--abbrev-ref", "@{upstream}", required=False)
    head_sha = _git(resolved_workdir, "rev-parse", "HEAD")
    base_sha = _git(resolved_workdir, "merge-base", "HEAD", "@{upstream}", required=False) or head_sha
    porcelain = _git(resolved_workdir, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    dirty = [line for line in porcelain if not line.startswith("??")]
    untracked = [line for line in porcelain if line.startswith("??")]
    transcript = transcript_path.resolve() if transcript_path is not None else None
    if transcript is not None and transcript.is_file():
        transcript_bytes = transcript.read_bytes()
        # The checkpoint hash covers exactly the recorded byte prefix.  A
        # live retained session may append while paused; resume verifies that
        # prefix rather than treating normal append-only progress as drift.
        if not transcript_cursor:
            transcript_cursor = str(len(transcript_bytes))
        if not transcript_sha256:
            transcript_sha256 = hashlib.sha256(transcript_bytes).hexdigest()
    return WorktreeBinding(
        repo_root=str(repo_root),
        git_common_dir=str(common_dir),
        worktree_realpath=str(resolved_workdir),
        branch=branch,
        upstream=upstream,
        base_sha=base_sha,
        head_sha=head_sha,
        dirty_digest=_digest(dirty),
        untracked_digest=_digest(untracked),
        transcript_path=str(transcript) if transcript is not None else "",
        transcript_cursor=transcript_cursor,
        transcript_sha256=transcript_sha256,
    )


def load_worktree_checkpoints(root: Path | None = None, *, session_ref: str | None = None) -> list[WorktreeCheckpoint]:
    """Load append-only worktree checkpoints, optionally for one lane."""
    document = _load_document(root)
    checkpoints = [WorktreeCheckpoint.from_dict(item) for item in cast(list[object], document["checkpoints"])]
    return checkpoints if session_ref is None else [item for item in checkpoints if item.session_ref == session_ref]


def load_lane_lifecycle_records(root: Path | None = None, *, session_ref: str | None = None) -> list[LaneLifecycleRecord]:
    """Load append-only lifecycle transitions, optionally for one lane."""
    document = _load_document(root)
    records = [LaneLifecycleRecord.from_dict(item) for item in cast(list[object], document["lifecycle"])]
    return records if session_ref is None else [item for item in records if item.session_ref == session_ref]


def get_lane_lifecycle(root: Path | None, session_ref: str) -> LaneLifecycleRecord | None:
    """Return the latest lifecycle state for ``session_ref``."""
    records = load_lane_lifecycle_records(root, session_ref=session_ref)
    return records[-1] if records else None


def _transcript_drift(expected: WorktreeBinding, actual: WorktreeBinding) -> list[str]:
    """Return transcript identity drift while allowing verified appends."""
    if expected.transcript_path != actual.transcript_path:
        return ["transcript_path"]
    if not expected.transcript_path:
        return []
    if not expected.transcript_cursor:
        # Older checkpoints have a complete-file digest but no recorded byte
        # boundary, so only exact-file validation is safe for them.
        return [] if expected.transcript_sha256 == actual.transcript_sha256 else ["transcript_sha256"]
    try:
        cursor = int(expected.transcript_cursor)
    except ValueError:
        return ["transcript_cursor"]
    if cursor < 0:
        return ["transcript_cursor"]
    try:
        current = Path(actual.transcript_path).read_bytes()
    except OSError:
        return ["transcript_path"]
    if len(current) < cursor:
        return ["transcript_cursor"]
    prefix_sha256 = hashlib.sha256(current[:cursor]).hexdigest()
    return [] if prefix_sha256 == expected.transcript_sha256 else ["transcript_sha256"]


def _binding_drift(expected: WorktreeBinding, actual: WorktreeBinding) -> list[str]:
    changed = [
        field
        for field in expected.to_dict()
        if field not in {"transcript_path", "transcript_cursor", "transcript_sha256"}
        and getattr(expected, field) != getattr(actual, field)
    ]
    return [*changed, *_transcript_drift(expected, actual)]


def validate_lane_resume(root: Path | None, *, session_ref: str, binding: WorktreeBinding) -> WorktreeCheckpoint:
    """Refuse a resume if the saved lane identity or worktree has drifted."""
    checkpoints = load_worktree_checkpoints(root, session_ref=session_ref)
    if not checkpoints:
        raise ValueError(f"cannot resume {session_ref}: no worktree checkpoint exists")
    expected = checkpoints[-1]
    changed = _binding_drift(expected.binding, binding)
    if changed:
        raise ValueError(
            f"cannot resume {session_ref}: worktree binding drifted since checkpoint "
            f"{expected.checkpoint_id}: {', '.join(changed)}"
        )
    return expected


def _allowed_transition(previous: LaneState | None, target: LaneState) -> bool:
    return target in _ALLOWED_LANE_TRANSITIONS[previous]


def transition_lane_lifecycle(
    root: Path | None,
    *,
    session_ref: str,
    target: LaneState,
    binding: WorktreeBinding,
    resume_note: str,
    independently_completed: bool = False,
    unfinished_work: bool = True,
    changed_at: str | None = None,
    request_id: str | None = None,
) -> LaneLifecycleRecord:
    """Checkpoint then move a lane among active, paused, shelved, and closed.

    ``active`` is the only enforcement-enabled state.  ``paused`` retains the
    provider session but does not enforce.  ``shelved`` retains the lane's goal,
    questions, and worktree binding while the provider session is offline.
    ``closed`` is only legal after independent completion verification.
    """
    if target not in LANE_STATES:
        raise ValueError("lane lifecycle target must be active, paused, shelved, or closed")
    if not session_ref.strip() or not resume_note.strip():
        raise ValueError("lane lifecycle requires session_ref and resume_note")
    request_key = request_id.strip() if request_id is not None else ""
    if request_id is not None and not request_key:
        raise ValueError("lane lifecycle request_id must be non-empty when supplied")
    with _recovery_lock(root):
        document = _load_document(root)
        raw_lifecycle = document["lifecycle"]
        raw_checkpoints = document["checkpoints"]
        assert isinstance(raw_lifecycle, list)
        assert isinstance(raw_checkpoints, list)
        parsed_lifecycle = [LaneLifecycleRecord.from_dict(item) for item in raw_lifecycle]
        if request_key:
            prior = next((item for item in parsed_lifecycle if item.request_id == request_key), None)
            if prior is not None:
                checkpoints_by_id = {
                    item.checkpoint_id: item
                    for item in (WorktreeCheckpoint.from_dict(raw) for raw in raw_checkpoints)
                }
                checkpoint = checkpoints_by_id.get(prior.checkpoint_id)
                if (
                    prior.session_ref != session_ref
                    or prior.state != target
                    or checkpoint is None
                    or _binding_drift(checkpoint.binding, binding)
                ):
                    raise ValueError(
                        f"lane lifecycle request {request_key} conflicts with prior transition {prior.lifecycle_id}"
                    )
                return prior
        latest = [item for item in parsed_lifecycle if item.session_ref == session_ref]
        previous = latest[-1].state if latest else None
        if not _allowed_transition(previous, target):
            raise ValueError(f"cannot transition {session_ref} from {previous or 'untracked'} to {target}")
        action = "resume" if target == "active" and previous in {"paused", "shelved"} else target
        if action == "resume":
            checkpoints = [
                WorktreeCheckpoint.from_dict(item)
                for item in raw_checkpoints
                if isinstance(item, dict) and item.get("session_ref") == session_ref
            ]
            if not checkpoints:
                raise ValueError(f"cannot resume {session_ref}: no worktree checkpoint exists")
            expected = checkpoints[-1]
            changed = _binding_drift(expected.binding, binding)
            if changed:
                raise ValueError(
                    f"cannot resume {session_ref}: worktree binding drifted since checkpoint "
                    f"{expected.checkpoint_id}: {', '.join(changed)}"
                )
        if target == "closed" and (not independently_completed or unfinished_work):
            raise ValueError("cannot close a lane without independent completion verification and no unfinished work")
        timestamp = changed_at or _utc_now()
        checkpoint = WorktreeCheckpoint(
            checkpoint_id=uuid.uuid4().hex,
            session_ref=session_ref,
            action=action,
            recorded_at=timestamp,
            resume_note=resume_note,
            binding=binding,
        )
        record = LaneLifecycleRecord(
            lifecycle_id=uuid.uuid4().hex,
            session_ref=session_ref,
            state=target,
            previous_state=previous,
            checkpoint_id=checkpoint.checkpoint_id,
            changed_at=timestamp,
            resume_note=resume_note,
            request_id=request_key,
        )
        # A checkpoint without its paired lifecycle record is misleading on
        # recovery. Keep the two append-only records in one locked write.
        raw_checkpoints.append(checkpoint.to_dict())
        raw_lifecycle.append(record.to_dict())
        _write_document(root, document)
    logger.info("lane_lifecycle_transitioned", session_ref=session_ref, state=target, checkpoint_id=checkpoint.checkpoint_id)
    return record

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

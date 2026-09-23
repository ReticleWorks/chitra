"""monitord -- the single Chitra monitor entrypoint.

One daemon composes the observation plane that W2's architecture review
collapsed out of watchd, triaged, and sweepd:

1. **Journal** -- incrementally ingest each tracked lane's client transcript
   into its durable canonical journal.
2. **Detectors and ladder** -- run the deterministic failure-mode detectors
   over the observed events and feed every finding through the response
   ladder, which advances only on recurrence after proven consumption.
3. **Persistent action** -- record corrective intent before publishing a
   goal-bound order, reconcile queue and signed delivery proof after a crash,
   and wait for a completed agent turn before judging recurrence.
4. **Enrollment and receipts** -- run registered validators on any
   completion claim, structured or plain, isolate receipts by goal session, and close only after
   the stored evidence verifies independently. When the lane provably runs as
   another OS user, validators execute on a bounded worker pool (one run in
   flight per lane) and the pass consumes the recorded result once the
   worktree digest it tested still matches; a lane sharing Chitra's OS user
   keeps the synchronous run and second-execution check, because it could
   write the record itself. The isolated ``claude -p`` completion review runs
   on its own bounded pool behind a per-session ``review-runs/`` record: a
   failed round disputes and is relaunched only after its recorded backoff,
   and a ``running`` record with no live worker counts as lost.
5. **Presence** -- publish one advisory presence record per pass so peers can
   see which instance is observing which lanes.

The daemon never writes to tmux. It publishes durable orders to ``dispatchd``,
which remains the sole terminal writer. It also answers only questions that
the frozen goal settles exactly; protected or ambiguous questions hold the
goal and become explicit asks.

``watchd``, ``triaged``, and ``sweepd`` remain shipped for existing
declarations but are deprecated by this entrypoint; new deployments declare
one ``monitord`` process per instance instead of the three-daemon chain.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from chitra._fsio import locked_json_store, write_json_atomic
from chitra.canonical_choices import CanonicalChoicesPolicy, detect_canonical_choices
from chitra.completion_gate import (
    CompletionEvidence,
    EnrolledDoneWhenItemLike,
    extract_completion_evidence,
    has_structured_completion_line,
    is_completion_claim,
)
from chitra.decisions import DecisionEntry, read_decisions
from chitra.detect import (
    BlockerClaimStore,
    Finding,
    IncidentRecord,
    IncidentStore,
    LadderDecision,
    ResponseLadder,
    collect_rescue_bundle,
    detect_blocker_claims,
    detect_deferral_language,
    detect_document_dithering,
    detect_drift,
    detect_excessive_testing,
    detect_false_done,
    detect_stall,
    detect_unnecessary_steps,
    find_rescue_bundle,
    first_unmet_item,
    first_unmet_item_id,
    met_done_items,
    rescue_bundle_process_fresh,
    write_checkpoint_receipt,
    write_rescue_bundle,
)
from chitra.dispatch import capture, is_local_host, pane_pid, tmux_pane_target
from chitra.goal_enforcement import (
    BehaviorReviewer,
    ClaudeProcessReviewer,
    GoalReviewError,
    SessionReviewSignal,
    WatchedSessionBehavior,
    freeze_goal,
    load_latest_review_signal,
    review_log_path,
    review_watched_session,
)
from chitra.goals import (
    GoalNotFoundError,
    GoalRecord,
    GoalsSchemaNewerError,
    GoalValidationError,
    add_ask,
    add_foreground_task,
    get_goal,
    hold_goal,
    list_goals,
    mark_completion_gate_passed,
    update_now,
)
from chitra.journal import (
    CanonicalEvent,
    CanonicalType,
    JournalIngestor,
    NormalizationContext,
    ProgressClass,
    ProgressClassification,
    derive_progress_rows,
    native_session_identity,
)
from chitra.journal.store import EventJournal
from chitra.lane_config import LaneSpec
from chitra.orders import DispatchOrder
from chitra.policy_config import load_policy_config
from chitra.presence import append_presence
from chitra.question_handler import QuestionHandlerResult, extract_questions, handle_question
from chitra.queue_state import QueueSubdir
from chitra.recovery import get_lane_lifecycle, load_worktree_checkpoints
from chitra.run_pool import RunPool
from chitra.state_paths import state_dir as default_state_dir
from chitra.supervision import SupervisionLedger, goal_digest
from chitra.supervisor import (
    _retry_due,
    reconcile_corrective_action,
    reconcile_question_action,
    record_observing,
    record_terminal_pursuit_alert,
)
from chitra.systemd_notify import notify_ready, notify_watchdog
from chitra.transcript_bindings import DEFAULT_FILENAME, TranscriptBinding, load_transcript_bindings
from chitra.validation_receipts import (
    lane_worktree_path,
    list_receipts,
    load_verification_record,
    receipt_path,
    record_enrolled_validator_runs,
    recorded_result_lane,
    recorded_validator_run_proof,
    run_enrolled_validators_on_worker,
    run_receipt_verification_on_worker,
    stored_receipt_integrity_digest,
    validator_run_record,
    validator_runs_root,
    worktree_changed_files,
    worktree_git_digest,
)
from chitra.validator_registry import validators_path

logger = structlog.get_logger(__name__)

DEFAULT_POLL_SECONDS = 60.0
PRESENCE_INSTANCE = "chitra-monitord"
MONITORD_SCHEMA = "chitra.monitord.pass.v1"
IDLE_PURSUIT_SCHEMA = "chitra.monitord.idle-pursuit.v1"
_VALIDATOR_RUN_MAX_ATTEMPTS = 3
# At most two concurrent ``claude -p`` judge rounds across all lanes.
_REVIEW_POOL_MAX_WORKERS = 2
# A pending corrective order suppresses idle pursuit only for a bounded
# lease: long enough for dispatchd to claim and deliver it, short enough
# that a lane ignoring a consumed order — or an order the transport never
# delivers — falls back to the lane's own progress evidence.
_DELIVERY_PENDING_LEASE_PASSES = 3
_DETECTOR_ORDER = (
    "canonical_choices.deprecated_path",
    "drift",
    "unnecessary_steps",
    "excessive_testing",
    "document_dithering",
    "stall",
    "deferral",
    "false_blocker",
    "changed_excuse",
)
# Goal statuses that mean the lane's work is already settled; observability
# findings only fire while the goal is still unfinished.
_TERMINAL_GOAL_STATUSES = frozenset({"done-pending-verification", "done-pending-close"})
_UNFINISHED_OBSERVABLE_STATUSES = frozenset(
    {"working", "blocked", "turn-finished-unverified", "completion-disputed"}
)
# Every durable store (journal, incidents, supervision, blocker claims)
# rejects lane names outside this shape; an unsafe journal stem or binding
# lane is logged and skipped rather than crashing the pass for every lane.
_SAFE_LANE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class MonitordConfig:
    """All filesystem paths and timing required by one monitord pass."""

    state_dir: Path
    transcript_root: Path | None
    findings_path: Path
    poll_seconds: float
    shadow_mode: bool
    transcript_bindings_path: Path | None = None
    dispatch_queue_dir: Path | None = None
    ledger_path: Path | None = None
    ledger_key_path: Path | None = None
    retry_delay_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class LanePassResult:
    """The compact result of one monitoring pass over one lane."""

    lane: str
    ingested_events: int
    findings_opened: int
    ladder_actions: tuple[str, ...]
    completion_disputed: bool
    completion_verified: bool
    question_outcome: str
    validator_receipts_recorded: int


def resolve_config(
    *,
    state_dir: Path | None = None,
    transcript_root: Path | None = None,
    findings_path: Path | None = None,
    poll_seconds: float | None = None,
    shadow_mode: bool | None = None,
    transcript_bindings_path: Path | None = None,
    dispatch_queue_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    retry_delay_seconds: float = 60.0,
) -> MonitordConfig:
    """Resolve CLI arguments, then explicit environment overrides, then defaults."""
    resolved_state_dir = state_dir or default_state_dir()
    resolved_findings_path = findings_path or resolved_state_dir / "monitord-findings.jsonl"
    if poll_seconds is None:
        poll_seconds = DEFAULT_POLL_SECONDS
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be a positive number")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative")
    if shadow_mode is None:
        # Shadow mode is the safe default: findings are recorded but never
        # leave the monitor's own state until an operator turns the mode off.
        # An operator opts a unit out explicitly with CHITRA_MONITORD_SHADOW_MODE=0
        # (the shipped unit example pins it on).
        shadow_mode = os.environ.get("CHITRA_MONITORD_SHADOW_MODE", "").strip() != "0"
    resolved_bindings_path = transcript_bindings_path
    if resolved_bindings_path is None:
        resolved_bindings_path = (
            transcript_root / DEFAULT_FILENAME
            if transcript_root is not None
            else resolved_state_dir / DEFAULT_FILENAME
        )
    return MonitordConfig(
        state_dir=resolved_state_dir,
        transcript_root=transcript_root,
        findings_path=resolved_findings_path,
        poll_seconds=poll_seconds,
        shadow_mode=shadow_mode,
        transcript_bindings_path=resolved_bindings_path,
        dispatch_queue_dir=dispatch_queue_dir or resolved_state_dir / "queue",
        ledger_path=ledger_path or resolved_state_dir / "ledger.jsonl",
        ledger_key_path=ledger_key_path or resolved_state_dir / "ledger.key",
        retry_delay_seconds=retry_delay_seconds,
    )


def _lane_roots(state_dir: Path) -> list[Path]:
    """Return one journal path per journaled lane under the state root.

    ``EventJournal`` writes flat at ``journal/<lane>.jsonl``; discovery
    mirrors that layout and the caller recovers the lane from the stem.
    """
    journal_root = state_dir / "journal"
    if not journal_root.is_dir():
        return []
    return sorted(
        path
        for path in journal_root.glob("*.jsonl")
        if not path.name.endswith(".progress.jsonl")
    )


# Live ingestors keyed by (state_dir, transcript path). Keeping the reader
# and normalizer alive between passes makes each pass consume only the bytes
# appended since the last poll; the reader's own anchor/digest checks still
# fire on every poll, so a rotated or rewritten transcript replays in full.
_INGESTOR_POOL: dict[
    tuple[str, str],
    tuple[tuple[object, ...], JournalIngestor],
] = {}


def _pooled_ingestor(
    config: MonitordConfig,
    transcript_path: Path,
    context: NormalizationContext,
) -> JournalIngestor:
    """Return the live ingestor for this binding, rebuilding on context change."""
    key = (str(config.state_dir), str(transcript_path))
    wanted = (context.instance, context.lane, context.client, context.client_version, context.goal_ref)
    pooled = _INGESTOR_POOL.get(key)
    if pooled is not None and pooled[0] == wanted:
        return pooled[1]
    if pooled is not None:
        pooled[1].close()
    ingestor = JournalIngestor(
        state_root=config.state_dir,
        transcript_path=transcript_path,
        context=context,
    )
    _INGESTOR_POOL[key] = (wanted, ingestor)
    return ingestor


def _drop_pooled_ingestor(config: MonitordConfig, transcript_path: Path) -> None:
    pooled = _INGESTOR_POOL.pop((str(config.state_dir), str(transcript_path)), None)
    if pooled is not None:
        pooled[1].close()


def _bound_native_session_id(config: MonitordConfig, transcript_path: Path) -> str | None:
    """Return the bound transcript's native session id without a second replay.

    The pooled ingestor's normalizer already tracks the transcript's session
    id as records flow through it; only a lane whose ingestor is absent (for
    example after a failed poll evicted it) pays for the full replay.
    """
    pooled = _INGESTOR_POOL.get((str(config.state_dir), str(transcript_path)))
    if pooled is not None:
        return pooled[1].normalizer.session_id
    return native_session_identity(transcript_path)


def ingest_transcript_bindings(
    config: MonitordConfig,
    bindings: tuple[TranscriptBinding, ...],
) -> tuple[CanonicalEvent, ...]:
    """Ingest every explicitly bound JSONL transcript before journal discovery."""
    observed: list[CanonicalEvent] = []
    unknown: dict[str, Counter[str]] = {}
    manifest_path = config.transcript_bindings_path or config.state_dir / DEFAULT_FILENAME
    for binding in bindings:
        transcript_path = _resolved_binding_path(config, binding, manifest_path=manifest_path)
        context = NormalizationContext(
            instance=binding.instance,
            lane=binding.lane,
            client=binding.client,
            client_version=binding.client_version,
            goal_ref=binding.session_ref,
        )
        try:
            result = _pooled_ingestor(config, transcript_path, context).poll()
        except ValueError:
            # A true rotation can hand this path a different session's file:
            # the pooled normalizer fails on the old session id, so rebuild
            # the ingestor once and let a fresh stream adopt the new one. A
            # persistently malformed transcript fails the same way on the
            # retry, skipping only its own lane this pass.
            _drop_pooled_ingestor(config, transcript_path)
            try:
                result = _pooled_ingestor(config, transcript_path, context).poll()
            except ValueError as exc:
                _drop_pooled_ingestor(config, transcript_path)
                logger.error("monitord_binding_ingest_failed", lane=binding.lane, error=str(exc))
                continue
        observed.extend(result.observed)
        # Client versions are not gated. Newly appended records the normalizer
        # does not recognize are the drift signal.
        # Logging the native record types, not just a count, makes a new type stand out.
        for event in result.appended:
            if event.normalized_type is CanonicalType.UNKNOWN:
                unknown.setdefault(event.lane, Counter())[str(event.payload.get("native_type"))] += 1
    for lane, types in sorted(unknown.items()):
        logger.info("monitord_unknown_events_ingested", lane=lane, events=sum(types.values()), types=dict(sorted(types.items())))
    if bindings:
        logger.info("monitord_bound_transcripts_ingested", bindings=len(bindings), events=len(observed))
    return tuple(observed)


def _resolved_binding_path(
    config: MonitordConfig,
    binding: TranscriptBinding,
    *,
    manifest_path: Path | None = None,
) -> Path:
    """Return the canonical path used by ingestion and event filtering."""
    resolved_manifest = manifest_path or config.transcript_bindings_path or config.state_dir / DEFAULT_FILENAME
    return binding.resolved_path(
        manifest_path=resolved_manifest,
        transcript_root=config.transcript_root,
    ).expanduser().resolve(strict=False)


def _event_matches_binding(
    event: CanonicalEvent,
    binding: TranscriptBinding,
    *,
    transcript_path: Path,
    native_session_id: str | None,
) -> bool:
    """Accept only events from the complete current transcript binding.

    ``client_version`` is deliberately compared as observed, not gated: a
    mid-session CLI upgrade must not zero the lane's visible event set.
    """
    return bool(
        native_session_id
        and event.transcript.path == str(transcript_path)
        and event.session_id == native_session_id
        and event.lane == binding.lane
        and event.goal_ref == binding.session_ref
        and event.client == binding.client
        and event.instance == binding.instance
    )


def _idle_pursuit_path(config: MonitordConfig, lane: str) -> Path:
    return config.state_dir / "idle-pursuit" / f"{lane}.json"


def _progress_digest(
    events: tuple[CanonicalEvent, ...],
    *,
    progress_rows: Sequence[ProgressClassification] = (),
    worktree_digest: str | None = None,
) -> str:
    """Digest the lane's real progress evidence: classified progress rows plus
    the declared worktree's content digest, so a diff change or new file
    counts even when no journal event carried it."""
    progress_ids = sorted(
        {
            source
            for row in progress_rows
            if row.classification is ProgressClass.PROGRESS
            for source in row.source_event_ids
        }
    )
    encoded = json.dumps(
        {"events": progress_ids, "worktree": worktree_digest},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _pending_order_state(config: MonitordConfig, order_id: str) -> str:
    """Where a corrective order sits in the durable dispatch queue, if anywhere."""
    if not order_id or config.dispatch_queue_dir is None:
        return ""
    for subdir in QueueSubdir:
        if (config.dispatch_queue_dir / subdir.value / f"{order_id}.json").is_file():
            return subdir.value
    return "gone"


def _idle_pursuit_finding(
    config: MonitordConfig,
    lane: str,
    goal: GoalRecord | None,
    events: tuple[CanonicalEvent, ...],
    findings: list[Finding],
    question_outcome: str,
    validator_pending: bool = False,
    *,
    progress_rows: Sequence[ProgressClassification] = (),
) -> Finding | None:
    """Persist clean-pass count and emit one deterministic idle finding."""
    path = _idle_pursuit_path(config, lane)
    goal_digest_value = goal_digest(goal) if goal is not None else ""
    latest_supervision = SupervisionLedger(config.state_dir, lane).latest()
    delivery_pending = bool(
        latest_supervision is not None
        and latest_supervision.goal_digest == goal_digest_value
        and latest_supervision.state in {"action_pending", "action_queued", "awaiting_progress"}
    )
    actionable = (
        goal is not None
        and goal.status in {"working", "blocked", "turn-finished-unverified", "completion-disputed"}
        and bool(goal.enrolled_done_when_items)
        and bool(events)
        and not findings
        and question_outcome == "none"
        and not validator_pending
        and not goal.open_asks
        and not goal.needs
    )
    if not actionable:
        with locked_json_store(path):
            write_json_atomic(
                path,
                {"schema": IDLE_PURSUIT_SCHEMA, "lane": lane, "count": 0},
                fsync=True,
            )
        return None

    assert goal is not None
    digest = goal_digest_value
    declared = _declared_worktree(config, goal)
    worktree_digest = worktree_git_digest(Path(declared)) if declared else None
    progress_digest = _progress_digest(events, progress_rows=progress_rows, worktree_digest=worktree_digest)
    source = {
        "path": events[0].transcript.path,
        "native_session_id": events[0].session_id,
        "lane": events[0].lane,
        "goal_ref": events[0].goal_ref,
        "client": str(events[0].client),
        "client_version": events[0].client_version,
        "instance": events[0].instance,
    }
    payload: dict[str, Any] = {}
    with locked_json_store(path):
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
            if isinstance(loaded, dict):
                payload = loaded
        same_identity = (
            payload.get("schema") == IDLE_PURSUIT_SCHEMA
            and payload.get("lane") == lane
            and payload.get("session_ref") == goal.session_ref
            and payload.get("goal_version") == goal.goal_version
            and payload.get("goal_digest") == digest
            and payload.get("source") == source
        )
        anchor = payload.get("anchor_event_id") if same_identity else None
        if not isinstance(anchor, str) or not anchor:
            anchor = events[0].event_id
        suppressed_passes = 0
        if delivery_pending:
            # Bounded delivery lease: the supervision ledger says a corrective
            # order is owed to the lane, so give dispatchd and the lane a few
            # passes before the lane's own progress evidence judges it. Once
            # the lease lapses, normal counting resumes and the finding names
            # where the owed order actually sits.
            suppressed = payload.get("suppressed_passes") if same_identity else 0
            suppressed_passes = suppressed + 1 if isinstance(suppressed, int) else 1
            if suppressed_passes <= _DELIVERY_PENDING_LEASE_PASSES:
                write_json_atomic(
                    path,
                    {
                        "schema": IDLE_PURSUIT_SCHEMA,
                        "lane": lane,
                        "session_ref": goal.session_ref,
                        "goal_version": goal.goal_version,
                        "goal_digest": digest,
                        "source": source,
                        "progress_digest": progress_digest,
                        "anchor_event_id": anchor,
                        "count": payload.get("count") if isinstance(payload.get("count"), int) else 0,
                        "suppressed_passes": suppressed_passes,
                    },
                    fsync=True,
                )
                return None
            order_state = _pending_order_state(config, str(getattr(latest_supervision, "order_id", "") or ""))
            logger.warning(
                "monitord_delivery_pending_lease_expired",
                lane=lane,
                order_id=getattr(latest_supervision, "order_id", ""),
                order_state=order_state,
                suppressed_passes=suppressed_passes,
            )
        else:
            order_state = ""
        previous_progress_digest = payload.get("progress_digest") if same_identity else None
        previous_count = payload.get("count") if same_identity else 0
        count = previous_count + 1 if previous_progress_digest == progress_digest and isinstance(previous_count, int) else 1
        if previous_progress_digest is not None and previous_progress_digest != progress_digest:
            count = 0
        write_json_atomic(
            path,
            {
                "schema": IDLE_PURSUIT_SCHEMA,
                "lane": lane,
                "session_ref": goal.session_ref,
                "goal_version": goal.goal_version,
                "goal_digest": digest,
                "source": source,
                "progress_digest": progress_digest,
                "anchor_event_id": anchor,
                "count": count,
                "suppressed_passes": suppressed_passes,
            },
            fsync=True,
        )
    idle_pursuit_passes = goal.autonomy_policy.idle_pursuit_passes
    if count < idle_pursuit_passes:
        return None
    unmet_item = first_unmet_item(
        goal.enrolled_done_when_items,
        met_done_items(
            goal.enrolled_done_when_items,
            receipt_root=config.state_dir,
            session_ref=goal.session_ref,
        ),
    )
    if unmet_item is None:
        return None
    detail = f"the enrolled goal produced no new scoped progress for {idle_pursuit_passes} clean monitor passes"
    if order_state:
        detail += f"; the corrective order still sits in the {order_state} queue state"
    return Finding(
        detector="idle_pursuit",
        fingerprint_seed={
            "anchor_event_id": anchor,
            "session_ref": goal.session_ref,
            "done_when_item_id": unmet_item.id,
        },
        event_refs=tuple(event.event_id for event in events[-3:]),
        unmet_item=unmet_item.id,
        expected_next_progress=f"take the next reversible in-scope action toward: {unmet_item.text}",
        detail=detail,
    )


@dataclass
class _LaneJournalCache:
    """Parsed events plus the byte watermark they cover for one journal file."""

    events: list[CanonicalEvent]
    offset: int
    inode: int
    mtime_ns: int


_LANE_JOURNALS: dict[tuple[str, str], _LaneJournalCache] = {}


def load_lane_events(config: MonitordConfig, lane: str) -> tuple[CanonicalEvent, ...]:
    """Load one lane's durable canonical journal, parsing only newly appended rows.

    The journal is append-only under its lane lock, so a cached byte offset
    limits each monitor pass to the delta. An inode change, a shrink, or a
    same-size rewrite (mtime moved while size stayed put) discards the cache
    and reloads in full; a malformed tail fails the pass exactly as a full
    ``load()`` would.
    """
    journal = EventJournal(config.state_dir, lane)
    key = (str(config.state_dir), lane)
    try:
        stat = journal.path.stat()
    except OSError:
        _LANE_JOURNALS.pop(key, None)
        return ()
    cached = _LANE_JOURNALS.get(key)
    if cached is not None and (
        cached.inode != stat.st_ino
        or stat.st_size < cached.offset
        or (stat.st_size == cached.offset and stat.st_mtime_ns != cached.mtime_ns)
    ):
        cached = None
    if cached is not None and stat.st_size == cached.offset:
        return tuple(cached.events)
    parsed, start, end_offset, fd_stat = journal.load_from(
        cached.offset if cached is not None else 0,
        inode=cached.inode if cached is not None else None,
    )
    if fd_stat is None:
        _LANE_JOURNALS.pop(key, None)
        return ()
    if cached is None:
        cached = _LaneJournalCache(events=[], offset=0, inode=fd_stat.st_ino, mtime_ns=fd_stat.st_mtime_ns)
        _LANE_JOURNALS[key] = cached
    elif start == 0:
        # The path was replaced between the stat above and the open inside
        # load_from; the returned events cover the whole current file.
        cached.events.clear()
    cached.events.extend(parsed)
    cached.offset = end_offset
    cached.inode = fd_stat.st_ino
    cached.mtime_ns = fd_stat.st_mtime_ns
    return tuple(cached.events)


def _final_response(events: tuple[CanonicalEvent, ...]) -> CanonicalEvent | None:
    for event in reversed(events):
        if event.normalized_type is CanonicalType.FINAL_RESPONSE:
            return event
    return None


def _declared_worktree(config: MonitordConfig, goal: object) -> str:
    """Return the lane's declared worktree realpath from its latest durable checkpoint.

    The checkpoint binding is captured with ``git rev-parse --show-toplevel``
    when the lane is anchored, so the boundary detector judges file access
    against the real worktree rather than a placeholder.
    """
    session_ref = str(getattr(goal, "session_ref", "") or "")
    if not session_ref:
        return ""
    checkpoints = load_worktree_checkpoints(config.state_dir, session_ref=session_ref)
    return checkpoints[-1].binding.worktree_realpath if checkpoints else ""


def _worktree_dirty(worktree: Path) -> bool:
    """Live ``git status`` probe: any tracked, staged, or untracked delta is dirty.

    The probe fails closed — a worktree that cannot be examined cannot prove
    it is clean, so a completion claim on it is disputed rather than passed.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def run_detectors(
    config: MonitordConfig,
    lane: str,
    goal: object,
    events: tuple[CanonicalEvent, ...],
    *,
    canonical_choices_policy: CanonicalChoicesPolicy | None = None,
    deferral_phrases: Sequence[str] | None = None,
    progress_rows: Sequence[ProgressClassification] = (),
) -> list[Finding]:
    """Run the deterministic detector set over one lane's journal."""
    scope_text = str(getattr(goal, "scope", "") or "")
    intent_text = str(getattr(goal, "intent", "") or "")
    goal_text = str(getattr(goal, "goal", "") or "")
    goal_is_document = "documentation" in f"{intent_text}\n{goal_text}".lower()
    enrolled_items = tuple(getattr(goal, "enrolled_done_when_items", ()) or ())
    session_ref = str(getattr(goal, "session_ref", "") or "")
    met_items = met_done_items(enrolled_items, receipt_root=config.state_dir, session_ref=session_ref)
    policy_config = load_policy_config()
    policy = canonical_choices_policy or policy_config.canonical_choices
    declared_worktree = _declared_worktree(config, goal)
    changed_files: tuple[str, ...] = ()
    if declared_worktree:
        real_changed = worktree_changed_files(Path(declared_worktree))
        if real_changed is not None:
            changed_files = real_changed
    findings: list[Finding] = []
    findings.extend(detect_canonical_choices(events, policy, enrolled_items=enrolled_items, met_items=met_items))
    findings.extend(
        detect_drift(
            events,
            scope_text=scope_text,
            declared_worktree=declared_worktree,
            enrolled_items=enrolled_items,
            met_items=met_items,
            changed_files=changed_files,
        )
    )
    findings.extend(
        detect_unnecessary_steps(
            events,
            progress_rows=progress_rows,
            enrolled_items=enrolled_items,
            met_items=met_items,
        )
    )
    findings.extend(
        detect_excessive_testing(
            events,
            progress_rows=progress_rows,
            enrolled_items=enrolled_items,
            met_items=met_items,
        )
    )
    findings.extend(
        detect_document_dithering(
            events,
            goal_is_document=goal_is_document,
            enrolled_items=enrolled_items,
            met_items=met_items,
        )
    )
    findings.extend(detect_stall(events, enrolled_items=enrolled_items))
    findings.extend(
        detect_deferral_language(
            events,
            enrolled_items=enrolled_items,
            met_items=met_items,
            phrases=deferral_phrases if deferral_phrases is not None else policy_config.completion_gate.deferral_phrases,
        )
    )
    try:
        blocker_store = BlockerClaimStore(config.state_dir, lane)
    except ValueError:
        # An unusable lane name must not take the whole pass down; the lane
        # simply gets no blocker-claim history this pass.
        blocker_store = None
    if blocker_store is not None:
        blocker_findings, blocker_records = detect_blocker_claims(
            events,
            enrolled_items=enrolled_items,
            met_items=met_items,
            history=blocker_store.load(),
            goal_digest=goal_digest(goal) if goal is not None else "",
        )
        blocker_store.append(blocker_records)
        findings.extend(blocker_findings)
    return [finding for name in _DETECTOR_ORDER for finding in findings if finding.detector == name]


def _bind_findings_to_goal(findings: list[Finding], goal: GoalRecord) -> list[Finding]:
    """Bind detector identities to the exact frozen goal being observed.

    Incident pressure tracks are keyed on (goal digest, unmet item); the
    finding fingerprint is only a detail field on the record.  The monitor
    still incorporates the current goal version and digest into a fresh
    fingerprint so the stored detail and order markers stay scoped to the
    exact goal revision that produced the finding.
    """
    digest = goal_digest(goal)
    return [
        Finding(
            detector=finding.detector,
            fingerprint_seed={
                "finding_fingerprint": finding.fingerprint,
                "goal_version": goal.goal_version,
                "goal_digest": digest,
            },
            event_refs=finding.event_refs,
            unmet_item=finding.unmet_item,
            expected_next_progress=finding.expected_next_progress,
            detail=finding.detail,
        )
        for finding in findings
    ]


def evaluate_findings(
    config: MonitordConfig,
    lane: str,
    findings: list[Finding],
    *,
    goal_digest_value: str,
    order_marker: str = "[M] monitord",
    on_decision: Callable[[Finding, LadderDecision], None] | None = None,
    journal_events: tuple[CanonicalEvent, ...] = (),
    ledger_key: bytes | None = None,
) -> list[str]:
    """Feed every finding through the response ladder and return its actions."""
    ladder = ResponseLadder(
        IncidentStore(config.state_dir, lane),
        journal_events=journal_events,
        ledger_key=ledger_key,
    )
    actions: list[str] = []
    for finding in findings:
        marker = f"{order_marker}:{finding.fingerprint[:16]}"
        decision = ladder.evaluate(lane=lane, finding=finding, order_marker=marker, goal_digest=goal_digest_value)
        actions.append(decision.action)
        if on_decision is not None:
            on_decision(finding, decision)
        logger.info(
            "monitord_ladder_decision",
            lane=lane,
            detector=finding.detector,
            action=decision.action,
            stage=decision.stage,
            reason=decision.reason,
            shadow_mode=config.shadow_mode,
        )
    return actions


def _rescue_checkpoint_ref(record: IncidentRecord) -> str:
    """One deterministic receipt name per rescue-stage instance."""
    seed = f"{record.track_id}:{record.order_marker}:{record.opened_at}"
    return f"rescue-{hashlib.sha256(seed.encode()).hexdigest()[:40]}"


def _collect_lane_rescue_bundle(
    config: MonitordConfig,
    *,
    lane: str,
    goal: GoalRecord,
    store: IncidentStore,
    transcript_path: Path | None,
) -> Any | None:
    """Capture the RESCUE bundle while the lane's pane process is still alive.

    Every failure is a hold, not an exception: a bundle collected against a
    dead or unobservable process would fail its own identity checks at seal
    time, so the pass simply tries again while the lane still runs.
    """
    parts = goal.session_ref.split(":")
    if len(parts) != 3 or not is_local_host(parts[0]):
        # /proc identity is only meaningful for a local pane process.
        return None
    host, session, pane_field = parts
    pane = tmux_pane_target(session, pane_field)
    pid = pane_pid(pane, host=host)
    if pid is None:
        return None
    checkpoints = load_worktree_checkpoints(config.state_dir, session_ref=goal.session_ref)
    if not checkpoints:
        return None
    worktree = Path(checkpoints[-1].binding.worktree_realpath)
    try:
        receipt_paths = [
            receipt_path(config.state_dir, goal.session_ref, receipt.receipt_name)
            for receipt in list_receipts(config.state_dir, goal.session_ref)
        ]
        pane_lines = capture(host, pane, 200)
        bundle = collect_rescue_bundle(
            lane=lane,
            session_ref=goal.session_ref,
            worktree=worktree,
            transcript_path=transcript_path,
            pane_capture="\n".join(pane_lines),
            receipt_paths=receipt_paths,
            contract_text=json.dumps(goal.to_dict(), sort_keys=True, ensure_ascii=False),
            incidents=store.load(),
            open_asks=goal.open_asks,
            process_identity={"target_pid": pid},
        )
    except (RuntimeError, OSError, ValueError) as exc:
        logger.warning("monitord_rescue_capture_failed", lane=lane, error=str(exc))
        return None
    write_rescue_bundle(bundle, config.state_dir)
    logger.info("monitord_rescue_bundle_captured", lane=lane, bundle_sha256=bundle.bundle_sha256)
    return bundle


def reconcile_rescue_checkpoint(
    config: MonitordConfig,
    *,
    lane: str,
    goal: GoalRecord,
    track_id: str,
    transcript_path: Path | None,
) -> None:
    """Drive one rescue-stage incident from bundle capture to a sealed checkpoint.

    The bundle is collected while the lane process is still alive — the
    captured ``target_pid`` identity is re-observed when the checkpoint
    receipt is written, so a process that already exited can never produce a
    sealable bundle. Sealing waits for proven consumption of the rescue
    order; the next recurrence then advances the track to relaunch.
    """
    store = IncidentStore(config.state_dir, lane)
    record = store.latest(track_id)
    if record is None or record.stage != "rescue" or record.checkpoint_ref:
        return
    bundle = find_rescue_bundle(config.state_dir, record, session_ref=goal.session_ref)
    if bundle is not None and not rescue_bundle_process_fresh(bundle):
        bundle = None
    if bundle is None:
        # Bundle capture runs tmux pane_pid/capture plus the worktree git
        # reads inside collect_rescue_bundle — slow calls that must not block
        # the pass. The worker's durable record is the bundle file itself:
        # a later pass finds it and continues to sealing.
        rescue_key = f"{config.state_dir}:{lane}:{track_id}:rescue"
        if _RUN_POOL.in_flight(rescue_key):
            return
        _RUN_POOL.submit(
            rescue_key,
            lambda: _collect_lane_rescue_bundle(
                config,
                lane=lane,
                goal=goal,
                store=store,
                transcript_path=transcript_path,
            ),
        )
        return
    if record.consumption is None:
        # The bundle now sits on disk; the rescue order's consumption is the
        # only remaining gate before the checkpoint receipt can be sealed.
        return
    checkpoint_ref = _rescue_checkpoint_ref(record)
    receipt_file = config.state_dir / "checkpoints" / f"{checkpoint_ref}.json"
    if not receipt_file.exists():
        try:
            write_checkpoint_receipt(
                bundle=bundle,
                record=record,
                state_root=config.state_dir,
                checkpoint_ref=checkpoint_ref,
            )
        except (RuntimeError, ValueError) as exc:
            logger.warning("monitord_rescue_checkpoint_receipt_failed", lane=lane, error=str(exc))
            return
    try:
        store.seal_rescue_checkpoint(
            track_id=track_id,
            order_marker=record.order_marker,
            bundle_sha256=bundle.bundle_sha256,
            checkpoint_ref=checkpoint_ref,
        )
    except ValueError as exc:
        logger.warning("monitord_rescue_checkpoint_seal_failed", lane=lane, error=str(exc))
        return
    logger.info("monitord_rescue_checkpoint_sealed", lane=lane, checkpoint_ref=checkpoint_ref)


def _lane_work_in_flight(
    config: MonitordConfig,
    session_ref: str,
    events: tuple[CanonicalEvent, ...],
) -> tuple[str, ...]:
    """Name this lane's work still in flight, for the completion reviewer.

    Mirrors watchd's turn-end list: a ``run_in_background`` tool call in the
    journal's latest session with no joined result or error is still
    running, and an order dispatchd has claimed under ``in_flight/`` for
    this session is being delivered right now.
    """
    running: list[str] = []
    if events:
        current_session = events[-1].session_id
        answered = {
            event.native_join_id
            for event in events
            if event.normalized_type in (CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR)
        }
        for event in events:
            call_input = event.payload.get("input")
            if (
                event.normalized_type is CanonicalType.TOOL_CALL
                and event.native_join_id is not None
                and event.session_id == current_session
                and event.native_join_id not in answered
                and isinstance(call_input, dict)
                and call_input.get("run_in_background") is True
            ):
                tool_name = event.payload.get("tool_name")
                suffix = f" ({tool_name})" if isinstance(tool_name, str) and tool_name else ""
                running.append(f"background tool call {event.native_join_id}{suffix} is still running")
    in_flight_dir = config.dispatch_queue_dir / "in_flight" if config.dispatch_queue_dir is not None else None
    if in_flight_dir is not None and in_flight_dir.is_dir():
        for order_path in sorted(in_flight_dir.glob("*.json")):
            try:
                order = DispatchOrder.model_validate_json(order_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if order.session_ref == session_ref:
                running.append(f"dispatch order {order.order_id} is being delivered to the lane")
    return tuple(running)


_MONITOR_WAKE = threading.Event()


def _wake_monitor() -> None:
    """Wake the monitor loop when any worker finishes so its record is read now."""
    _MONITOR_WAKE.set()


# One bounded pool carries every slow call the pass must not wait on: the
# enrolled validators, the receipt re-verification (git digests and target
# hashing), the rescue-bundle capture, and the gate close that re-verifies
# inside the goal lock. Kind-suffixed lane keys keep one operation in flight
# per lane per kind; each worker lands a durable record
# (``validation-runs/``, ``verification-runs/``, the goal store itself) that
# a later pass consumes instead of waiting.
_RUN_POOL = RunPool(
    max_workers=4,
    thread_name_prefix="chitra-monitor-run",
    on_complete=_wake_monitor,
)
# Kept under the historical name: tests and operators already know the
# validator pool by it, and it is the same shared pool now.
_VALIDATOR_RUN_POOL = _RUN_POOL
# The isolated ``claude -p`` completion judge gets its own bounded pool so a
# twenty-minute round never holds a validator, verification, rescue, or
# close worker; the cap is the concurrent spend ceiling on judge processes.
_REVIEW_POOL = RunPool(
    max_workers=_REVIEW_POOL_MAX_WORKERS,
    thread_name_prefix="chitra-monitor-review",
    on_complete=_wake_monitor,
)


def _op_key(config: MonitordConfig, goal: GoalRecord, session_ref: str, kind: str) -> str:
    """Return the one-in-flight key for one lane and one slow-operation kind."""
    return f"{config.state_dir}:{goal.lane_id or session_ref}:{kind}"


_REVIEW_RUN_SCHEMA = "chitra.monitord.review-run.v1"
# A failed judge round is relaunched on a doubling backoff instead of on
# every pass: the claim disputes with the unchanged "review could not run"
# finding while the recorded retry time is still in the future.
_REVIEW_RETRY_BASE_SECONDS = 60.0
_REVIEW_RETRY_MAX_SECONDS = 900.0


def _review_run_path(config: MonitordConfig, session_ref: str) -> Path:
    """Return the one in-progress and result record for a goal session's judge rounds."""
    session_key = hashlib.sha256(session_ref.encode("utf-8")).hexdigest()
    return config.state_dir / "review-runs" / f"{session_key}.json"


def _read_review_run(path: Path) -> dict[str, Any]:
    """Read a review-run record; anything unreadable or foreign counts as absent."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict) or loaded.get("schema") != _REVIEW_RUN_SCHEMA:
        return {}
    return loaded


def _write_review_run(path: Path, record: dict[str, Any], *, expected_op_id: object = None) -> None:
    """Persist a review-run record under its lock.

    A worker finishing late passes ``expected_op_id`` so its result cannot
    overwrite the record of a round relaunched after the worker was lost.
    """
    with locked_json_store(path):
        if expected_op_id is not None and _read_review_run(path).get("op_id") != expected_op_id:
            return
        write_json_atomic(path, {**record, "schema": _REVIEW_RUN_SCHEMA}, fsync=True)


def _run_review_on_worker(
    config: MonitordConfig,
    path: Path,
    record: dict[str, Any],
    behavior: WatchedSessionBehavior,
    reviewer: BehaviorReviewer,
) -> None:
    """Run one isolated review round off the monitor loop and write its outcome back.

    Success is the signal ``review_watched_session`` appends to the review
    log; the record only names it. A failure records the error and the
    earliest retry, so a dead judge is relaunched on a backoff instead of on
    every pass.
    """
    try:
        signal = review_watched_session(
            config.state_dir,
            record["session_ref"],
            behavior,
            reviewer=reviewer,
        )
    except Exception as exc:
        finished = datetime.now(UTC)
        delay = min(
            _REVIEW_RETRY_BASE_SECONDS * 2 ** (int(record["attempt"]) - 1),
            _REVIEW_RETRY_MAX_SECONDS,
        )
        _write_review_run(
            path,
            {
                **record,
                "state": "failed",
                "error": str(exc),
                "finished_at": finished.isoformat(),
                "next_retry_at": (finished + timedelta(seconds=delay)).isoformat(),
            },
            expected_op_id=record["op_id"],
        )
        return
    _write_review_run(
        path,
        {
            **record,
            "state": "done",
            "signal_id": signal.signal_id,
            "finished_at": datetime.now(UTC).isoformat(),
        },
        expected_op_id=record["op_id"],
    )


def _completion_review(
    config: MonitordConfig,
    goal: GoalRecord,
    behavior: WatchedSessionBehavior,
    contract_id: str,
    reviewer: BehaviorReviewer,
) -> tuple[SessionReviewSignal | None, str, bool]:
    """Return ``(signal, error, pending)`` for a claim without waiting on the judge.

    A stored signal for this exact behavior and contract is reused, as
    before. Otherwise the in-memory pool decides whether a round is running:
    a ``running`` record with no live worker died with an earlier process or
    a job cancelled at shutdown, so it counts as lost and is relaunched. A
    failed round reports its recorded error until its backoff expires, then
    relaunches.
    """
    session_ref = behavior.session_ref
    signal = load_latest_review_signal(review_log_path(config.state_dir), session_ref)
    key = _op_key(config, goal, session_ref, "review")
    if (
        signal is not None
        and signal.behavior_sha256 == behavior.behavior_sha256
        and signal.goal_contract_id == contract_id
        # An "insufficient" verdict only holds while lane work is still in
        # flight; once it finishes, the same claim is judged afresh.
        and not (signal.verdict == "insufficient" and not behavior.still_running)
    ):
        _REVIEW_POOL.reset(key)
        return signal, "", False
    if _REVIEW_POOL.in_flight(key):
        return None, "", True
    path = _review_run_path(config, session_ref)
    run = _read_review_run(path)
    same = (
        run.get("behavior_sha256") == behavior.behavior_sha256
        and run.get("goal_contract_id") == contract_id
    )
    if same and run.get("state") == "failed":
        next_retry_at = run.get("next_retry_at")
        if not _retry_due(next_retry_at if isinstance(next_retry_at, str) else ""):
            return None, str(run.get("error", "")), False
    if same and run.get("state") == "running":
        # The record outlived its worker: the monitor restarted mid-call or
        # the job was cancelled at shutdown, so the round is lost.
        logger.warning("monitord_review_run_lost", session_ref=session_ref, op_id=run.get("op_id"))
    previous_attempt = run.get("attempt")
    record: dict[str, Any] = {
        "session_ref": session_ref,
        "behavior_sha256": behavior.behavior_sha256,
        "goal_contract_id": contract_id,
        "op_id": uuid.uuid4().hex,
        "owner_pid": os.getpid(),
        "attempt": (
            previous_attempt + 1
            if same and run.get("state") != "done" and isinstance(previous_attempt, int)
            else 1
        ),
        "state": "running",
        "started_at": datetime.now(UTC).isoformat(),
    }
    _write_review_run(path, record)
    _REVIEW_POOL.submit(
        key,
        lambda: _run_review_on_worker(config, path, record, behavior, reviewer),
    )
    return None, "", True


def _verify_evidence_state(
    root: Path,
    session_ref: str,
    items: tuple[EnrolledDoneWhenItemLike, ...],
) -> str:
    """Classify worker evidence as ``runs``, ``verify``, or ``fresh``.

    ``runs`` means the validator run records are missing, bound to another
    session/validator, or straddle a worktree move — the lane must be
    re-validated. ``verify`` means the run records are self-consistent but
    the recorded verification is missing or no longer describes the stored
    receipts. ``fresh`` means the pass can consume the recorded verdicts
    without running git or re-executing anything itself.
    """
    afters: set[str] = set()
    for item in items:
        run = validator_run_record(root, session_ref, item.required_receipt)
        if run is None or run.session_ref != session_ref or run.validator != item.validator:
            return "runs"
        if run.tree_digest_before is None or run.tree_digest_before != run.tree_digest_after:
            return "runs"
        afters.add(run.tree_digest_after)
    if len(afters) != 1:
        # The recorded runs straddle a worktree move: they never described a
        # single tree, so the whole evidence set re-queues.
        return "runs"
    digest = afters.pop()
    verify = load_verification_record(root, session_ref)
    if verify is None or verify.session_ref != session_ref:
        return "verify"
    if verify.tree_digest != digest:
        # The tree moved between the recorded runs and the verification
        # sample: the run evidence is stale, not merely unverified.
        return "runs"
    for item in items:
        name = item.required_receipt
        if verify.results.get(name) is None:
            return "verify"
        current = stored_receipt_integrity_digest(root, session_ref, name) or ""
        if verify.receipt_digests.get(name, "") != current:
            return "verify"
    return "fresh"


def _claimed_run_evidence(
    config: MonitordConfig,
    goal: GoalRecord,
    session_ref: str,
    items: tuple[EnrolledDoneWhenItemLike, ...],
    *,
    lane: LaneSpec | None,
) -> tuple[tuple[CompletionEvidence, ...], dict[str, str] | None] | None:
    """Return validator evidence for a claim, or None while worker runs settle.

    When the lane provably runs as another OS user, the enrolled validators
    and the receipt re-verification execute on the worker pool — one run in
    flight per lane per kind — and their durable records stand in for the
    pass's own git digests and re-executions. When the lane shares Chitra's
    user a record file proves nothing, so both the run and the verification
    stay synchronous, exactly as before.
    """
    if lane is None:
        return record_enrolled_validator_runs(config.state_dir, session_ref, items), None
    # The worker runs bind to the checkpoint-recorded worktree, not the
    # manifest's declared workdir; with no recorded tree the claim stays on
    # the synchronous path that resolves the same binding itself.
    worktree = lane_worktree_path(config.state_dir, session_ref)
    if worktree is None:
        return record_enrolled_validator_runs(config.state_dir, session_ref, items), None
    run_key = _op_key(config, goal, session_ref, "validators")
    verify_key = _op_key(config, goal, session_ref, "verify")
    if _RUN_POOL.in_flight(run_key) or _RUN_POOL.in_flight(verify_key):
        # A running worker may be rewriting these receipts and records right
        # now: neither read them nor start a second run beside it.
        return None
    state = _verify_evidence_state(config.state_dir, session_ref, items)
    if state == "fresh":
        _RUN_POOL.reset(run_key)
        _RUN_POOL.reset(verify_key)
        verify = load_verification_record(config.state_dir, session_ref)
        assert verify is not None  # _verify_evidence_state just proved it
        return (
            tuple(recorded_validator_run_proof(config.state_dir, session_ref, item) for item in items),
            dict(verify.results),
        )
    if _RUN_POOL.attempts(run_key) + _RUN_POOL.attempts(verify_key) >= _VALIDATOR_RUN_MAX_ATTEMPTS:
        # Worker runs that keep failing, or keep landing stale records, are
        # surfaced through the same synchronous call the inline path made
        # instead of queueing forever. The next claim tries the pool again.
        _RUN_POOL.reset(run_key)
        _RUN_POOL.reset(verify_key)
        return record_enrolled_validator_runs(config.state_dir, session_ref, items), None
    if state == "verify":
        _RUN_POOL.submit(
            verify_key,
            lambda: run_receipt_verification_on_worker(
                config.state_dir,
                session_ref,
                items,
                workdir=worktree,
            ),
        )
    else:
        _RUN_POOL.submit(
            run_key,
            lambda: run_enrolled_validators_on_worker(
                config.state_dir,
                session_ref,
                items,
                workdir=worktree,
            ),
        )
    return None


_GATE_CLOSE_RUN_SCHEMA = "chitra.gate-close-run.v1"


def _close_record_path(root: Path, session_ref: str) -> Path:
    """Return the rejection-record path for one session's async gate close."""
    session_key = hashlib.sha256(session_ref.encode("utf-8")).hexdigest()
    return root / "gate-close-runs" / session_key / "rejection.json"


def _close_rejection_detail(
    root: Path,
    session_ref: str,
    *,
    behavior_sha256: str,
    goal_contract_id: str,
) -> str | None:
    """Return a worker's recorded close rejection for this exact claim, if any.

    The record binds to the reviewed behavior and goal contract, so a stale
    rejection from an earlier claim can never dispute a fresh one.
    """
    try:
        raw = json.loads(_close_record_path(root, session_ref).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != _GATE_CLOSE_RUN_SCHEMA:
        return None
    if raw.get("session_ref") != session_ref:
        return None
    if raw.get("behavior_sha256") != behavior_sha256 or raw.get("goal_contract_id") != goal_contract_id:
        return None
    detail = raw.get("detail")
    return detail if isinstance(detail, str) else "gate close rejected"


def _close_completion_on_worker(
    config: MonitordConfig,
    session_ref: str,
    *,
    run_evidence: tuple[CompletionEvidence, ...],
    last_verified: str,
    behavior_sha256: str,
    goal_contract_id: str,
) -> None:
    """Close the completion gate from a worker and record only a rejection.

    Success needs no record: the goal's own ``done-pending-close`` status is
    the durable result the next pass observes. A ``GoalValidationError`` is
    the claim-level rejection the pass turns into a dispute; anything else
    propagates to the pool log and is retried within the attempt bound, then
    falls back to the inline call.
    """
    try:
        mark_completion_gate_passed(
            config.state_dir,
            session_ref,
            now="independent enrolled validators passed for the exact completion claim",
            last_verified=last_verified,
            completion_evidence=run_evidence,
        )
    except GoalValidationError as exc:
        write_json_atomic(
            _close_record_path(config.state_dir, session_ref),
            {
                "schema": _GATE_CLOSE_RUN_SCHEMA,
                "session_ref": session_ref,
                "behavior_sha256": behavior_sha256,
                "goal_contract_id": goal_contract_id,
                "detail": str(exc),
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )


_ACTIONABLE_GOAL_STATUSES = frozenset(
    {"working", "blocked", "turn-finished-unverified", "completion-disputed"}
)
_CLAIM_CHECK_SCHEMA = "chitra.monitord.claim-check.v1"


def _goal_lookup_finding(session_ref: str, exc: Exception) -> Finding:
    """Surface a failed goal-store read as a finding instead of a silent skip."""
    return Finding(
        detector="monitor-internal-error",
        fingerprint_seed={
            "session_ref": session_ref,
            "reason": "goal-lookup-failed",
            "error_type": type(exc).__name__,
        },
        event_refs=(),
        unmet_item="goal store",
        expected_next_progress="restore the goal store so this lane's contract can be read",
        detail=f"goal lookup for session {session_ref!r} failed: {type(exc).__name__}: {exc}",
    )


def _event_text(event: CanonicalEvent) -> str:
    value = event.payload.get("text")
    return value if isinstance(value, str) else ""


def _claim_check_path(config: MonitordConfig, session_ref: str) -> Path:
    session_key = hashlib.sha256(session_ref.encode("utf-8")).hexdigest()
    return validator_runs_root(config.state_dir) / session_key / "claim-check.json"


def _registry_file_digest(config: MonitordConfig) -> str | None:
    """Digest the validator registry bytes; ``None`` when a read fails mid-check."""
    try:
        return hashlib.sha256(validators_path(config.state_dir).read_bytes()).hexdigest()
    except FileNotFoundError:
        return ""
    except OSError:
        return None


def _claim_check_key(
    config: MonitordConfig,
    goal: GoalRecord,
    claim_event: CanonicalEvent,
    items: tuple[EnrolledDoneWhenItemLike, ...],
    material_questions: tuple[str, ...],
    still_running: tuple[str, ...],
) -> dict[str, Any] | None:
    """Identify the logical check a claim marker may safely stand in for.

    A stored outcome is reused only while the claim event, goal version,
    enrolled items, validator registry bytes, open material questions, and
    in-flight lane work are all unchanged. ``None`` disables reuse entirely
    when the registry itself cannot be read.
    """
    registry_digest = _registry_file_digest(config)
    if registry_digest is None:
        return None
    lane = recorded_result_lane(config.state_dir, goal.session_ref)
    worktree_digest = worktree_git_digest(lane.workdir) if lane is not None else None
    if lane is not None and worktree_digest is None:
        return None
    return {
        "claim_event_id": claim_event.event_id,
        "goal_version": goal.goal_version,
        "registry_sha256": registry_digest,
        "items": [[item.id, item.validator, item.required_receipt] for item in items],
        "material_questions": list(material_questions),
        "still_running": sorted(still_running),
        "worktree_digest": worktree_digest,
    }


def _receipt_file_digests(
    config: MonitordConfig,
    session_ref: str,
    items: tuple[EnrolledDoneWhenItemLike, ...],
) -> dict[str, str | None]:
    """Digest each enrolled receipt's stored bytes; ``None`` when unreadable."""
    digests: dict[str, str | None] = {}
    for item in items:
        try:
            digests[item.required_receipt] = hashlib.sha256(
                receipt_path(config.state_dir, session_ref, item.required_receipt).read_bytes()
            ).hexdigest()
        except (OSError, ValueError):
            digests[item.required_receipt] = None
    return digests


def _finding_payload(finding: Finding) -> dict[str, Any]:
    return {
        "detector": finding.detector,
        "fingerprint_seed": finding.fingerprint_seed,
        "event_refs": list(finding.event_refs),
        "unmet_item": finding.unmet_item,
        "expected_next_progress": finding.expected_next_progress,
        "detail": finding.detail,
    }


def _finding_from_payload(payload: object) -> Finding | None:
    if not isinstance(payload, dict):
        return None
    detector = payload.get("detector")
    seed = payload.get("fingerprint_seed")
    refs = payload.get("event_refs")
    unmet_item = payload.get("unmet_item")
    expected_next_progress = payload.get("expected_next_progress")
    detail = payload.get("detail")
    if not (
        isinstance(detector, str)
        and isinstance(seed, dict)
        and isinstance(refs, list)
        and all(isinstance(ref, str) for ref in refs)
        and isinstance(unmet_item, str)
        and isinstance(expected_next_progress, str)
        and isinstance(detail, str)
    ):
        return None
    return Finding(
        detector=detector,
        fingerprint_seed=seed,
        event_refs=tuple(refs),
        unmet_item=unmet_item,
        expected_next_progress=expected_next_progress,
        detail=detail,
    )


def _load_claim_checks(config: MonitordConfig, session_ref: str) -> dict[str, Any]:
    """Read the session's claim-check marker; malformed state reads as absent."""
    path = _claim_check_path(config, session_ref)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != _CLAIM_CHECK_SCHEMA
        or payload.get("session_ref") != session_ref
    ):
        return {}
    checks = payload.get("checks")
    return dict(checks) if isinstance(checks, dict) else {}


def _stored_claim_outcome(
    entry: object,
    key: dict[str, Any],
    receipt_digests: dict[str, str | None],
) -> tuple[int, bool, list[Finding], bool] | None:
    """Return the recorded outcome only while it still covers this exact check."""
    if not isinstance(entry, dict):
        return None
    if entry.get("key") != key or entry.get("receipt_digests") != receipt_digests:
        return None
    outcome = entry.get("outcome")
    if not isinstance(outcome, dict):
        return None
    receipts = outcome.get("receipts_recorded")
    disputed = outcome.get("disputed")
    pending = outcome.get("pending")
    raw_findings = outcome.get("findings")
    if not (
        isinstance(receipts, int)
        and not isinstance(receipts, bool)
        and isinstance(disputed, bool)
        and isinstance(pending, bool)
        and isinstance(raw_findings, list)
    ):
        return None
    findings = [_finding_from_payload(item) for item in raw_findings]
    if any(finding is None for finding in findings):
        return None
    return receipts, disputed, [finding for finding in findings if finding is not None], pending


def _store_claim_check(
    config: MonitordConfig,
    session_ref: str,
    claim_event_id: str,
    *,
    key: dict[str, Any],
    receipt_digests: dict[str, str | None],
    outcome: tuple[int, bool, list[Finding], bool],
) -> None:
    """Record one completion claim's outcome durably.

    The marker is dedupe state, not proof: a write failure only means the
    next pass re-evaluates, so it is logged and never fatal.
    """
    path = _claim_check_path(config, session_ref)
    entry = {
        "key": key,
        "receipt_digests": receipt_digests,
        "outcome": {
            "receipts_recorded": outcome[0],
            "disputed": outcome[1],
            "pending": outcome[3],
            "findings": [_finding_payload(finding) for finding in outcome[2]],
        },
    }
    try:
        with locked_json_store(path):
            checks = _load_claim_checks(config, session_ref)
            checks[claim_event_id] = entry
            write_json_atomic(
                path,
                {
                    "schema": _CLAIM_CHECK_SCHEMA,
                    "session_ref": session_ref,
                    "checks": checks,
                },
                fsync=True,
            )
    except Exception as exc:
        logger.warning(
            "monitord_claim_check_store_failed",
            session_ref=session_ref,
            error=str(exc),
        )


def _evaluate_completion_claim(
    config: MonitordConfig,
    goal: GoalRecord,
    session_ref: str,
    claim_event: CanonicalEvent,
    *,
    reviewer: BehaviorReviewer | None,
    still_running: tuple[str, ...],
) -> tuple[int, bool, list[Finding], bool]:
    """Evaluate one completion claim event against its enrolled contract.

    The durable claim-check marker under ``validation-runs/`` holds the
    outcome keyed on the claim event, goal version, enrolled items, registry
    bytes, open material questions, and in-flight lane work, plus the stored
    receipt digests produced by the run. While all of those still match, a
    later pass replays the recorded outcome instead of re-executing the same
    validators for the same logical check.
    """
    final_text = _event_text(claim_event)
    items = tuple(getattr(goal, "enrolled_done_when_items", ()) or ())
    if not items:
        findings = [
            Finding(
                detector="false_done",
                fingerprint_seed={"session_ref": session_ref, "reason": "unenrolled-completion"},
                event_refs=(claim_event.event_id,),
                unmet_item="frozen completion contract",
                expected_next_progress="enroll exact done conditions and their independent validators before claiming completion",
                detail="completion was claimed without frozen enrolled done items",
            )
        ]
        if not config.shadow_mode:
            update_now(
                config.state_dir,
                session_ref,
                now=findings[0].detail,
                status="completion-disputed",
            )
        return 0, True, findings, False

    if config.shadow_mode:
        # Shadow mode is observe-only: it never spawns lane-triggered
        # validators and never re-executes a stored one, so a claim simply
        # has nothing to be checked against on this pass.
        return 0, False, [], False

    material_questions = (*goal.open_asks, *((goal.needs,) if goal.needs else ()))
    check_key = _claim_check_key(
        config, goal, claim_event, items, material_questions, still_running
    )
    if check_key is not None:
        stored = _load_claim_checks(config, session_ref).get(claim_event.event_id)
        outcome = _stored_claim_outcome(
            stored, check_key, _receipt_file_digests(config, session_ref, items)
        )
        if outcome is not None:
            if outcome[1] and not config.shadow_mode:
                update_now(
                    config.state_dir,
                    session_ref,
                    now="; ".join(finding.detail for finding in outcome[2]),
                    status="completion-disputed",
                )
            return outcome

    claimed_evidence = tuple(extract_completion_evidence(final_text))
    claim_bindings: dict[str, str] = {}
    for item in items:
        if any(
            proof.done_when_item_id == item.id
            and proof.receipt_name == item.required_receipt
            and proof.validator == item.validator
            for proof in claimed_evidence
        ):
            claim_bindings[item.id] = item.required_receipt

    # Probe the lane's tree before any validator run touches it: a validator
    # may legitimately write caches into the worktree, and the dirty check
    # judges the state the claim was made in, not the run's side effects.
    declared_worktree = _declared_worktree(config, goal)
    target_dirty = bool(declared_worktree) and _worktree_dirty(Path(declared_worktree))

    lane = recorded_result_lane(config.state_dir, session_ref)
    claimed = _claimed_run_evidence(config, goal, session_ref, items, lane=lane)
    if claimed is None:
        # A worker run is in flight or was just queued: the claim
        # neither passes nor disputes until the recorded result lands.
        return 0, False, [], True
    run_evidence, verified_results = claimed
    if not has_structured_completion_line(final_text):
        # A plain-language claim binds no item to a receipt, so bind every
        # enrolled item to the receipt Chitra just stored and let those
        # receipts decide the claim.
        for item in items:
            claim_bindings[item.id] = item.required_receipt

    findings = detect_false_done(
        final_response=claim_event,
        enrolled_items=items,
        receipt_names_by_item=claim_bindings,
        receipt_roots={session_ref: config.state_dir},
        session_ref=session_ref,
        material_questions=material_questions,
        target_dirty=target_dirty,
        # Live proof means a fresh Chitra-executed run for every enrolled
        # item this pass; a worker in flight returned pending above, so a
        # claim reaching here has all of them.
        live_proof_required=True,
        live_proof_present=len(run_evidence) >= len(items),
        verified_results=verified_results,
    )
    pending = False
    cacheable = True
    if not findings and not config.shadow_mode:
        # The same isolated reviewer that gates completion claims under watchd
        # now gates them here: a deterministic pass alone does not release a
        # claimed "done". A stored signal for this exact behavior and contract
        # is reused so an unchanged disputed claim does not pay for a fresh
        # review round every pass. The review runs on its own bounded pool —
        # its durable record is the review-log signal this pass already reads,
        # and a ``review-runs/`` record carries the round's running, done, or
        # failed state — so ``claude -p`` never blocks the loop; a queued or
        # running review reports the claim as pending, like the validator
        # stages.
        behavior = WatchedSessionBehavior.from_turn(session_ref, final_text, still_running=still_running)
        try:
            contract_id = freeze_goal(goal).contract_id
        except GoalReviewError:
            contract_id = ""
        signal, review_error, review_pending = _completion_review(
            config,
            goal,
            behavior,
            contract_id,
            reviewer if reviewer is not None else ClaudeProcessReviewer(),
        )
        if review_pending:
            return len(run_evidence), False, [], True
        if review_error:
            logger.warning(
                "monitord_completion_review_unavailable",
                session_ref=session_ref,
                error=review_error,
            )
            cacheable = False
            findings = [
                Finding(
                    detector="false_done",
                    fingerprint_seed={"session_ref": session_ref, "reason": "review-unavailable"},
                    event_refs=(claim_event.event_id,),
                    unmet_item="isolated completion review",
                    expected_next_progress="restore the isolated reviewer and re-claim completion",
                    detail=f"the isolated completion review could not run: {review_error}",
                )
            ]
        if not findings and signal is not None and signal.verdict == "insufficient":
            # The reviewers could not decide while lane work is still in
            # flight: like watchd, leave the goal untouched and report the
            # claim as pending rather than disputing it.
            pending = True
        elif not findings and signal is not None and signal.verdict != "accept":
            review_detail = "; ".join(f"{item.code}: {item.detail}" for item in signal.findings)
            findings = [
                Finding(
                    detector="false_done",
                    fingerprint_seed={
                        "session_ref": session_ref,
                        "reason": "review-rejected",
                        "signal_id": signal.signal_id,
                    },
                    event_refs=(claim_event.event_id,),
                    unmet_item="isolated completion review",
                    expected_next_progress="resolve the cited review findings before claiming completion again",
                    detail=(
                        "isolated reviewers rejected the completion claim"
                        + (f": {review_detail}" if review_detail else "")
                    ),
                )
            ]
    if not findings and not pending and not config.shadow_mode:
        # The close key carries the claim's behavior hash so the attempt
        # bound counts retries of THIS claim — a later, different claim
        # starts at zero instead of inheriting exhausted attempts.
        close_key = _op_key(config, goal, session_ref, f"close:{behavior.behavior_sha256[:16]}")
        rejected: str | None = None
        if lane is not None:
            # The gate close re-verifies every receipt inside the goal lock —
            # git digests and trusted-validator re-execution included — so for
            # a lane whose records are trustworthy it runs on the worker pool.
            # A success is observable as the goal's own status change; a
            # rejection lands as a durable record this pass consumes.
            rejected = _close_rejection_detail(
                config.state_dir,
                session_ref,
                behavior_sha256=behavior.behavior_sha256,
                goal_contract_id=contract_id,
            )
            if rejected is None and not _RUN_POOL.in_flight(close_key):
                if _RUN_POOL.attempts(close_key) >= _VALIDATOR_RUN_MAX_ATTEMPTS:
                    _RUN_POOL.reset(close_key)
                else:
                    _RUN_POOL.submit(
                        close_key,
                        lambda: _close_completion_on_worker(
                            config,
                            session_ref,
                            run_evidence=run_evidence,
                            last_verified=claim_event.event_id,
                            behavior_sha256=behavior.behavior_sha256,
                            goal_contract_id=contract_id,
                        ),
                    )
                    return len(run_evidence), False, [], True
            if rejected is None and _RUN_POOL.in_flight(close_key):
                return len(run_evidence), False, [], True
            if rejected is not None:
                findings = [
                    Finding(
                        detector="false_done",
                        fingerprint_seed={"session_ref": session_ref, "reason": "completion-store-rejected"},
                        event_refs=(claim_event.event_id,),
                        unmet_item="verified completion receipts",
                        expected_next_progress="produce current verified receipts bound to this exact goal session",
                        detail=f"the completion store rejected the claimed evidence: {rejected}",
                    )
                ]
        if lane is None or (rejected is None and not _RUN_POOL.in_flight(close_key) and not findings):
            # Same-OS-user lanes keep the synchronous close — a worker record
            # they could write proves nothing — and the attempts-exhausted
            # fallback lands here too.
            try:
                mark_completion_gate_passed(
                    config.state_dir,
                    session_ref,
                    now="independent enrolled validators passed for the exact completion claim",
                    last_verified=claim_event.event_id,
                    completion_evidence=run_evidence,
                )
            except GoalValidationError as exc:
                findings = [
                    Finding(
                        detector="false_done",
                        fingerprint_seed={"session_ref": session_ref, "reason": "completion-store-rejected"},
                        event_refs=(claim_event.event_id,),
                        unmet_item="verified completion receipts",
                        expected_next_progress="produce current verified receipts bound to this exact goal session",
                        detail=f"the completion store rejected the claimed evidence: {exc}",
                    )
                ]

    disputed = bool(findings)
    if disputed and not config.shadow_mode:
        update_now(
            config.state_dir,
            session_ref,
            now="; ".join(finding.detail for finding in findings),
            status="completion-disputed",
        )
    outcome = (len(run_evidence), disputed, findings, pending)
    if cacheable and check_key is not None:
        _store_claim_check(
            config,
            session_ref,
            claim_event.event_id,
            key=check_key,
            receipt_digests=_receipt_file_digests(config, session_ref, items),
            outcome=outcome,
        )
    return outcome


def check_enrollment_and_receipts(
    config: MonitordConfig,
    session_ref: str,
    final_response: CanonicalEvent | None = None,
    *,
    final_responses: Sequence[CanonicalEvent] | None = None,
    turn_ended: bool = False,
    turn_end_event: CanonicalEvent | None = None,
    reviewer: BehaviorReviewer | None = None,
    still_running: tuple[str, ...] = (),
) -> tuple[int, bool, list[Finding], bool]:
    """Verify every completion claim in the unconsumed window against its contract.

    Validators run on any completion claim in an unconsumed final response,
    whether or not it carries a structured completion line — every claim event
    in ``final_responses`` is checked, not only the latest, so a claim cannot
    be erased by a later ordinary response. Each claim's outcome is recorded
    in a durable claim-check marker keyed on the exact logical check, so a
    repeated pass replays the recorded outcome instead of re-executing the
    same validators. A newer claim event, a changed registry or enrollment, a
    moved receipt, or resolved in-flight work each reopen the check.

    The lane's claimed result is ignored; Chitra executes and stores each
    enrolled validator itself. A missing or held goal and a turn without a
    completion claim are silent, while a goal-store failure surfaces as a
    monitor-internal-error finding rather than reading as a quiet pass. When
    ``turn_ended`` says the turn finished with no final response at all, the
    existing exit-before-contract finding fires for an enrolled goal. For a
    lane that runs as another OS user the execution happens on the worker
    pool and a claim is neither passed nor disputed while that run is in
    flight; the fourth return value reports that pending state so the pass
    does not treat a lane under active validation as idle. ``still_running``
    names lane work still in flight so the isolated reviewer can answer
    "insufficient", which is reported the same way: pending, neither passed
    nor disputed.
    """
    try:
        goal = get_goal(config.state_dir, session_ref)
    except Exception as exc:
        # A broken goal store is not an absent goal: surface the failure so an
        # operator sees this lane is running without its contract check.
        logger.error(
            "monitord_goal_lookup_failed",
            session_ref=session_ref,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return 0, True, [_goal_lookup_finding(session_ref, exc)], False
    if goal is None or goal.status not in _ACTIONABLE_GOAL_STATUSES:
        return 0, False, [], False
    responses = (
        tuple(final_responses)
        if final_responses is not None
        else (() if final_response is None else (final_response,))
    )
    claims = [event for event in responses if is_completion_claim(_event_text(event))]
    items = tuple(getattr(goal, "enrolled_done_when_items", ()) or ())
    if not claims:
        if turn_ended and not responses and items:
            # The lane's lifecycle says its turn ended with no final response
            # at all, so no claim can ever bind the contract this turn made.
            material_questions = (*goal.open_asks, *((goal.needs,) if goal.needs else ()))
            findings = detect_false_done(
                final_response=None,
                enrolled_items=items,
                receipt_names_by_item={},
                receipt_roots={session_ref: config.state_dir},
                session_ref=session_ref,
                material_questions=material_questions,
            )
            if turn_end_event is not None:
                # Cite the event that ended the turn so a recurrence after a
                # consumed order reads as new evidence to the ladder, not as
                # an eternally unprovable condition.
                findings = [
                    Finding(
                        detector=f.detector,
                        fingerprint_seed=f.fingerprint_seed,
                        event_refs=(turn_end_event.event_id,),
                        unmet_item=f.unmet_item,
                        expected_next_progress=f.expected_next_progress,
                        detail=f.detail,
                    )
                    for f in findings
                ]
            if not config.shadow_mode:
                update_now(
                    config.state_dir,
                    session_ref,
                    now="; ".join(finding.detail for finding in findings),
                )
            return 0, True, findings, False
        return 0, False, [], False

    total_recorded = 0
    disputed = False
    all_findings: list[Finding] = []
    seen_fingerprints: set[str] = set()
    for claim_event in claims:
        recorded, claim_disputed, claim_findings, pending = _evaluate_completion_claim(
            config,
            goal,
            session_ref,
            claim_event,
            reviewer=reviewer,
            still_running=still_running,
        )
        total_recorded += recorded
        disputed = disputed or claim_disputed
        # Two claims can raise the same finding (its seed names the failed
        # item, not the event): the ladder track is fingerprint-keyed, so a
        # duplicate must not count as a recurrence in one pass.
        for finding in claim_findings:
            if finding.fingerprint not in seen_fingerprints:
                seen_fingerprints.add(finding.fingerprint)
                all_findings.append(finding)
        if pending:
            return total_recorded, disputed, all_findings, True
        try:
            refreshed = get_goal(config.state_dir, session_ref)
        except Exception as exc:
            logger.error(
                "monitord_goal_lookup_failed",
                session_ref=session_ref,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            all_findings.append(_goal_lookup_finding(session_ref, exc))
            return total_recorded, True, all_findings, False
        if refreshed is None or refreshed.status not in _ACTIONABLE_GOAL_STATUSES:
            break
        goal = refreshed
    return total_recorded, disputed, all_findings, False


_QUESTION_OUTCOME_PRIORITY: tuple[str, ...] = (
    "operator_required",
    "reasoning_required",
    "answer_queued",
    "answer_blocked",
    "blocked",
    "answer_awaiting_progress",
    "shadow_answer",
    "shadow",
)


def _aggregate_question_outcome(outcomes: Sequence[str]) -> str:
    """Fold per-question outcomes into one deterministic summary token."""
    for outcome in _QUESTION_OUTCOME_PRIORITY:
        if outcome in outcomes:
            return outcome
    return "none"


def _mark_question_boundary(config: MonitordConfig, lane: str, goal: GoalRecord, event_id: str) -> None:
    """Advance the lane's consumed boundary past a fully handled question turn.

    The row carries forward the in-flight action's fingerprint, retry, and
    consumption fields; only the turn boundary moves. A lane whose ledger
    belongs to another session is left alone rather than crashed over.
    """
    ledger = SupervisionLedger(config.state_dir, lane)
    latest = ledger.latest()
    if latest is not None and (
        latest.turn_boundary_event_id == event_id or latest.session_ref != goal.session_ref
    ):
        return
    ledger.transition(
        state=latest.state if latest is not None else "observing",
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest_value=goal_digest(goal),
        reason="the question turn is fully handled; its events are accounted for",
        turn_boundary_event_id=event_id,
    )


def _apply_question_result(
    config: MonitordConfig,
    goal: GoalRecord,
    result: QuestionHandlerResult,
    *,
    journal_events: tuple[CanonicalEvent, ...],
    lane: str,
) -> tuple[bool, str]:
    """Persist the durable side of one handled question.

    Returns ``(handled, outcome)``. ``handled`` is False only while an
    ``answered`` question's order is still pending delivery or consumption,
    so the asking event stays in the detection window for a later pass to
    finish the reconcile.
    """
    if result.disposition == "residual":
        if not config.shadow_mode:
            add_foreground_task(
                config.state_dir,
                goal.session_ref,
                kind="question",
                text=f"{result.reason}. Question: {result.question}",
                source="monitord",
            )
        return True, "reasoning_required"
    if result.disposition == "operator_required":
        if not config.shadow_mode:
            gates = f" Gates: {', '.join(result.gate_reasons)}." if result.gate_reasons else ""
            reason = f"the question requests operator-controlled authority: {result.reason}{gates}"
            add_ask(config.state_dir, goal.session_ref, f"{reason}. Question: {result.question}")
            hold_goal(config.state_dir, goal.session_ref, reason=f"operator-required question: {reason}")
        return True, "operator_required"
    assert result.answer is not None
    if config.shadow_mode:
        return True, "shadow_answer"
    if config.dispatch_queue_dir is None:
        raise ValueError("dispatch queue is required for autonomous goal answers")
    action = reconcile_question_action(
        state_root=config.state_dir,
        queue_dir=config.dispatch_queue_dir,
        lane=lane,
        goal=goal,
        question_result=result,
        journal_events=journal_events,
        ledger_path=config.ledger_path,
        ledger_key_path=config.ledger_key_path,
        retry_delay_seconds=config.retry_delay_seconds,
    )
    if action.state == "action_queued":
        return False, "answer_queued"
    if action.state == "awaiting_progress":
        latest = SupervisionLedger(config.state_dir / "question-actions", lane).latest_for_action(
            f"question:{result.request_id}", "question"
        )
        consumed = latest is not None and bool(latest.turn_boundary_event_id)
        return consumed, "answer_awaiting_progress"
    if action.state == "blocked":
        return False, "answer_blocked"
    return False, action.state


def handle_agent_question(
    config: MonitordConfig,
    goal: GoalRecord,
    final_responses: Sequence[CanonicalEvent],
    *,
    journal_events: tuple[CanonicalEvent, ...] = (),
    lane: str | None = None,
) -> str:
    """Handle every unanswered question in the unconsumed turn window.

    Each final-response event in the window contributes the questions
    ``extract_questions`` finds in it, and each is classified independently:
    a deterministic answer is reconciled through the queue, an unsettled one
    becomes a durable foreground task, and a protected-authority ask holds
    the lane. An event's turn is marked consumed once every question on it
    has reached durable state; a still-pending answer keeps its event in the
    window so the next pass finishes the reconcile, and a re-ask in a later
    event is a new occurrence-bound request, never a dropped duplicate.
    """
    lane_key = lane or goal.lane_id or (final_responses[-1].lane.replace(":", ".") if final_responses else goal.session_ref)
    decisions: list[DecisionEntry] | None = None
    outcomes: list[str] = []
    handled_boundary = ""
    boundary_complete = True
    for event in final_responses:
        payload_text = event.payload.get("text")
        if not isinstance(payload_text, str):
            continue
        questions = extract_questions(payload_text)
        if not questions:
            continue
        if decisions is None:
            decisions = read_decisions(config.state_dir / "decisions.jsonl")
        event_handled = True
        for question in questions:
            result = handle_question(goal, question, decisions=decisions, occurrence=event.event_id)
            handled, outcome = _apply_question_result(
                config,
                goal,
                result,
                journal_events=journal_events,
                lane=lane_key,
            )
            outcomes.append(outcome)
            event_handled = event_handled and handled
        # The consumed boundary is a contiguous claim: once one event's
        # answer is still pending, later events are processed but the
        # boundary must not jump past the unfinished one.
        if event_handled and boundary_complete:
            handled_boundary = event.event_id
        elif not event_handled:
            boundary_complete = False
    if handled_boundary and not config.shadow_mode:
        _mark_question_boundary(config, lane_key, goal, handled_boundary)
    return _aggregate_question_outcome(outcomes)


def append_finding_records(config: MonitordConfig, lane: str, findings: list[Finding]) -> int:
    """Append one JSONL record per finding to the monitor findings log."""
    if not findings:
        return 0
    config.findings_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    with config.findings_path.open("a", encoding="utf-8") as handle:
        for finding in findings:
            record = {
                "schema": MONITORD_SCHEMA,
                "recorded_at": now,
                "lane": lane,
                "shadow_mode": config.shadow_mode,
                "detector": finding.detector,
                "fingerprint": finding.fingerprint,
                "event_refs": list(finding.event_refs),
                "unmet_item": finding.unmet_item,
                "expected_next_progress": finding.expected_next_progress,
                "detail": finding.detail,
            }
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return len(findings)


def run_once(config: MonitordConfig) -> dict[str, Any]:
    """Run one full observe-classify-record-publish pass and return its summary."""
    bindings = load_transcript_bindings(
        config.transcript_bindings_path,
        transcript_root=config.transcript_root,
    )
    ingest_transcript_bindings(config, bindings)
    bindings_by_lane = {binding.lane: binding for binding in bindings}
    binding_paths = {
        binding.lane: _resolved_binding_path(config, binding)
        for binding in bindings
    }
    binding_native_session_ids = {
        lane: _bound_native_session_id(config, path)
        for lane, path in binding_paths.items()
    }
    try:
        goals_by_session = {goal.session_ref: goal for goal in list_goals(config.state_dir)}
    except GoalsSchemaNewerError as exc:
        # Keep the service alive so it can recover after an operator installs
        # a compatible reader, but never supervise against a partial reading
        # of a goal contract written by a newer schema.
        blocked_summary: dict[str, Any] = {
            "schema": MONITORD_SCHEMA,
            "lanes_observed": 0,
            "findings_opened": 0,
            "completion_disputed": False,
            "completion_verified": False,
            "question_answers_queued": 0,
            "questions_operator_required": 0,
            "validator_receipts_recorded": 0,
            "shadow_mode": config.shadow_mode,
            "blocked_reason": "goals-schema-newer-than-installed",
            "results": [],
        }
        logger.error("monitord_goals_schema_newer_than_installed", error=str(exc), **blocked_summary)
        return blocked_summary
    results: list[LanePassResult] = []
    enrolled_goals_by_lane: dict[str, GoalRecord] = {}
    for enrolled_goal in goals_by_session.values():
        if not enrolled_goal.lane_id:
            continue
        existing = enrolled_goals_by_lane.get(enrolled_goal.lane_id)
        if existing is None or (
            existing.status in _TERMINAL_GOAL_STATUSES and enrolled_goal.status not in _TERMINAL_GOAL_STATUSES
        ):
            enrolled_goals_by_lane[enrolled_goal.lane_id] = enrolled_goal
    # Observation iterates the union of journaled, bound, and enrolled lanes:
    # a lane whose transcript was never ingested or whose binding no longer
    # resolves is an explicit observation failure, not an absent lane.
    discovered_lanes = (
        {path.stem for path in _lane_roots(config.state_dir)}
        | set(bindings_by_lane)
        | set(enrolled_goals_by_lane)
    )
    unsafe_lanes = sorted(lane for lane in discovered_lanes if _SAFE_LANE_NAME.fullmatch(lane) is None)
    if unsafe_lanes:
        logger.error("monitord_unsafe_lane_names_skipped", lanes=unsafe_lanes)
    observed_lanes = sorted(discovered_lanes - set(unsafe_lanes))
    for lane in observed_lanes:
        binding = bindings_by_lane.get(lane)
        loaded_events = load_lane_events(config, lane)
        if binding is not None:
            events = tuple(
                event
                for event in loaded_events
                if _event_matches_binding(
                    event,
                    binding,
                    transcript_path=binding_paths[lane],
                    native_session_id=binding_native_session_ids.get(lane),
                )
            )
        else:
            events = loaded_events
        goal = goals_by_session.get(binding.session_ref) if binding is not None else enrolled_goals_by_lane.get(lane)
        session_ref = (
            binding.session_ref
            if binding is not None
            else goal.session_ref
            if goal is not None
            else lane
        )
        lifecycle = get_lane_lifecycle(config.state_dir, session_ref)
        if lifecycle is not None and not lifecycle.enforcement_enabled:
            results.append(
                LanePassResult(
                    lane=lane,
                    ingested_events=len(events),
                    findings_opened=0,
                    ladder_actions=(),
                    completion_disputed=False,
                    completion_verified=False,
                    question_outcome="none",
                    validator_receipts_recorded=0,
                )
            )
            continue
        mismatched_goal: GoalRecord | None = None
        if binding is not None and goal is not None and (not goal.lane_id or goal.lane_id != binding.lane):
            logger.warning(
                "monitord_goal_binding_mismatch",
                lane=lane,
                session_ref=binding.session_ref,
                goal_lane_id=goal.lane_id,
                binding_lane=binding.lane,
            )
            mismatched_goal = goal
            goal = None
        if binding is not None and goal is None:
            logger.warning(
                "monitord_goal_binding_unresolved",
                lane=lane,
                session_ref=binding.session_ref,
                binding_lane=binding.lane,
            )
        observability_findings: list[Finding] = []
        goal_unfinished = goal is not None and goal.status in _UNFINISHED_OBSERVABLE_STATUSES
        unmet_for_observation = (
            first_unmet_item_id(
                goal.enrolled_done_when_items,
                met_done_items(
                    goal.enrolled_done_when_items,
                    receipt_root=config.state_dir,
                    session_ref=goal.session_ref,
                ),
            )
            if goal is not None
            else ""
        )
        if binding is not None and goal_unfinished:
            if binding_native_session_ids.get(lane) is None:
                observability_findings.append(
                    Finding(
                        detector="unobservable_lane",
                        fingerprint_seed={
                            "lane": lane,
                            "session_ref": binding.session_ref,
                            "reason": "transcript-identity-unresolved",
                        },
                        event_refs=(),
                        unmet_item=unmet_for_observation,
                        expected_next_progress="restore a readable bound transcript for this lane",
                        detail=(
                            f"bound transcript {binding_paths[lane]} yields no native session identity "
                            "(missing, unreadable, truncated, or foreign); the lane cannot be observed"
                        ),
                    )
                )
            elif not events:
                observability_findings.append(
                    Finding(
                        detector="binding_unmatched",
                        fingerprint_seed={
                            "lane": lane,
                            "session_ref": binding.session_ref,
                            "transcript_path": str(binding_paths[lane]),
                        },
                        event_refs=(),
                        unmet_item=unmet_for_observation,
                        expected_next_progress="re-align the transcript binding so journaled events match it",
                        detail=(
                            "bound transcript resolves an identity but no journaled events match the "
                            "complete binding (lane, session, path, client, instance)"
                        ),
                    )
                )
        elif binding is None and goal_unfinished and not loaded_events:
            assert goal is not None
            observability_findings.append(
                Finding(
                    detector="unobservable_lane",
                    fingerprint_seed={
                        "lane": lane,
                        "session_ref": goal.session_ref,
                        "reason": "enrolled-lane-unbound",
                    },
                    event_refs=(),
                    unmet_item=unmet_for_observation,
                    expected_next_progress="bind a transcript for this enrolled lane so it can be observed",
                    detail="enrolled lane has no transcript binding and no journal; its goal is unobservable",
                )
            )
        if binding is not None and goal is None:
            observability_findings.append(
                Finding(
                    detector="unresolved_binding",
                    fingerprint_seed={
                        "lane": lane,
                        "session_ref": binding.session_ref,
                        "reason": "goal-lane-mismatch" if mismatched_goal is not None else "goal-missing",
                    },
                    event_refs=(),
                    unmet_item="",
                    expected_next_progress="reconcile the transcript binding or the goal's lane assignment",
                    detail=(
                        f"binding for lane {lane!r} resolves to session {binding.session_ref!r} "
                        + (
                            f"whose goal is assigned to lane {mismatched_goal.lane_id or '(unassigned)'!r}"
                            if mismatched_goal is not None
                            else "which has no goal record"
                        )
                    ),
                )
            )
            if not config.shadow_mode:
                anchor_goal = mismatched_goal or enrolled_goals_by_lane.get(lane)
                if anchor_goal is not None:
                    # A goal deleted between the pass snapshot and this write
                    # drops the alert; the finding record still stands.
                    with contextlib.suppress(GoalNotFoundError):
                        add_foreground_task(
                            config.state_dir,
                            anchor_goal.session_ref,
                            kind="investigate",
                            source="monitord",
                            text=(
                                f"transcript binding for lane {lane} resolves to session {binding.session_ref} "
                                "but no goal on this lane matches it; reconcile the binding manifest or the "
                                "goal's lane assignment"
                            ),
                        )
        if observability_findings and goal is not None and not config.shadow_mode:
            with contextlib.suppress(GoalNotFoundError):
                add_foreground_task(
                    config.state_dir,
                    goal.session_ref,
                    kind="investigate",
                    source="monitord",
                    text=(
                        f"lane {lane} cannot be observed: {observability_findings[0].detail}"
                    ),
                )
        if not events and not observability_findings:
            continue
        detector_events = events
        supervision = SupervisionLedger(config.state_dir, lane)
        if goal is not None:
            latest_supervision = supervision.latest_consumed_boundary(
                goal_digest_value=goal_digest(goal)
            )
            if latest_supervision is not None and latest_supervision.turn_boundary_event_id:
                positions = {
                    event.event_id: index for index, event in enumerate(events)
                }
                boundary = positions.get(latest_supervision.turn_boundary_event_id)
                if boundary is not None:
                    detector_events = events[boundary + 1 :]
        # Derive progress evidence from the whole journal — writes, new check
        # results, and worktree-visible changes — then persist the new rows so
        # detectors and idle pursuit judge the lane on real signals, not
        # narration.
        progress_rows = derive_progress_rows(
            events,
            goal_version=str(goal.goal_version) if goal is not None else "",
        )
        EventJournal(config.state_dir, lane).append_progress(progress_rows)
        if goal is not None:
            # Every unconsumed final response is a claim candidate, not only
            # the latest one; a turn that ended without any final response is
            # the exit-before-contract case.
            final_responses = tuple(
                event
                for event in detector_events
                if event.normalized_type is CanonicalType.FINAL_RESPONSE
            )
            final_response = final_responses[-1] if final_responses else None
            turn_ended = goal.status == "turn-finished-unverified" or bool(
                detector_events and detector_events[-1].normalized_type is CanonicalType.RESUME
            )
            receipts_recorded, completion_disputed, enrollment_findings, validator_pending = check_enrollment_and_receipts(
                config,
                goal.session_ref,
                final_response,
                final_responses=final_responses,
                turn_ended=turn_ended,
                turn_end_event=detector_events[-1] if detector_events else None,
                still_running=_lane_work_in_flight(config, goal.session_ref, events),
            )
            try:
                refreshed_goal = get_goal(config.state_dir, goal.session_ref)
            except Exception as exc:
                logger.error(
                    "monitord_goal_lookup_failed",
                    lane=lane,
                    session_ref=goal.session_ref,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                enrollment_findings.append(_goal_lookup_finding(goal.session_ref, exc))
            else:
                if refreshed_goal is not None:
                    goal = refreshed_goal
        else:
            # Legacy or unresolved bindings remain observable, but no other
            # goal may be borrowed for detector context or receipt checks.
            receipts_recorded, completion_disputed, enrollment_findings, validator_pending = 0, False, [], False
            final_response = None

        completion_verified = bool(goal is not None and goal.status == "done-pending-close")
        if completion_verified and goal is not None:
            latest_supervision = supervision.latest()
            if latest_supervision is None or latest_supervision.state != "completion_verified":
                final_response = _final_response(detector_events)
                supervision.transition(
                    state="completion_verified",
                    session_ref=goal.session_ref,
                    goal_version=goal.goal_version,
                    goal_digest_value=goal_digest(goal),
                    reason="exact completion claim passed independently executed enrolled validators",
                    finding_fingerprint="",
                    stage="",
                    order_id="",
                    order_marker="",
                    observed_event_id=final_response.event_id if final_response is not None else "",
                    turn_boundary_event_id=final_response.event_id if final_response is not None else "",
                    attempt=0,
                    next_retry_at="",
                    obstacle="",
                )

        detection_findings = (
            []
            if goal is not None and goal.status in {"held", "done-pending-verification", "done-pending-close"}
            else run_detectors(config, lane, goal, detector_events, progress_rows=progress_rows)
        )
        if detection_findings and goal is not None:
            # While a corrective order is owed or freshly consumed, the ladder
            # already owns the escalation cadence for a narrating lane; stall
            # only flags lanes with no pending corrective window.
            latest_supervision_state = supervision.latest()
            if (
                latest_supervision_state is not None
                and latest_supervision_state.goal_digest == goal_digest(goal)
                and latest_supervision_state.state
                in {"action_pending", "action_queued", "awaiting_progress"}
            ):
                detection_findings = [f for f in detection_findings if f.detector != "stall"]
        findings = observability_findings + detection_findings + [
            finding for finding in enrollment_findings if finding.detector == "false_done"
        ]
        # Monitor-internal errors are surfaced on the findings log and suppress
        # a clean "observing" record, but they never enter the incident ladder:
        # no corrective order to the lane can repair the monitor's own store.
        internal_findings = [
            finding for finding in enrollment_findings if finding.detector != "false_done"
        ]
        question_outcome = (
            handle_agent_question(
                config,
                goal,
                tuple(
                    event for event in detector_events if event.normalized_type is CanonicalType.FINAL_RESPONSE
                ),
                journal_events=events,
                lane=lane,
            )
            if goal is not None
            and goal.status not in {"held", "done-pending-verification", "done-pending-close"}
            else "none"
        )
        if question_outcome == "operator_required" and goal is not None and not config.shadow_mode:
            try:
                refreshed_goal = get_goal(config.state_dir, goal.session_ref)
            except Exception as exc:
                logger.error(
                    "monitord_goal_lookup_failed",
                    lane=lane,
                    session_ref=goal.session_ref,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                internal_findings.append(_goal_lookup_finding(goal.session_ref, exc))
            else:
                if refreshed_goal is not None:
                    goal = refreshed_goal
        idle_finding = _idle_pursuit_finding(
            config,
            lane,
            goal,
            events,
            findings + internal_findings,
            question_outcome,
            validator_pending=validator_pending,
            progress_rows=progress_rows,
        )
        if idle_finding is not None:
            findings.append(idle_finding)
        if goal is not None:
            findings = _bind_findings_to_goal(findings, goal)
        def supervise_finding(
            finding: Finding,
            decision: LadderDecision,
            active_goal: GoalRecord | None = goal,
            active_lane: str = lane,
            active_events: tuple[CanonicalEvent, ...] = events,
        ) -> None:
            if active_goal is None:
                return
            if active_goal.status in {"held", "done-pending-verification", "done-pending-close"}:
                return
            if config.dispatch_queue_dir is None:
                raise ValueError("dispatch queue is required for persistent supervision")
            reconcile_corrective_action(
                state_root=config.state_dir,
                queue_dir=config.dispatch_queue_dir,
                lane=active_lane,
                goal=active_goal,
                finding=finding,
                decision=decision,
                shadow_mode=config.shadow_mode,
                journal_events=active_events,
                ledger_path=config.ledger_path,
                ledger_key_path=config.ledger_key_path,
                retry_delay_seconds=config.retry_delay_seconds,
            )
            # RESCUE is evidence work, not another corrective order: capture
            # the bundle while the lane process is still alive, then seal the
            # checkpoint once the rescue order's consumption is proven. The
            # next recurrence on this track can then advance to relaunch.
            reconcile_rescue_checkpoint(
                config,
                lane=active_lane,
                goal=active_goal,
                track_id=decision.record.track_id,
                transcript_path=binding_paths.get(active_lane),
            )
            if decision.record.stage == "relaunch" and not config.shadow_mode:
                # The ladder's top rung issues no further orders; without a
                # surfaced alert the incident would hold forever in silence.
                record_terminal_pursuit_alert(
                    config.state_dir,
                    active_goal,
                    detail=(
                        f"Track {decision.record.track_id} is at the relaunch stage; "
                        "the ladder issues no further corrective orders for it."
                    ),
                )

        if goal is None:
            # The journal remains observable for diagnosis, but an unresolved
            # or mismatched binding is not allowed to create or mutate an
            # incident that a later goal could accidentally inherit.
            scheduled_findings = []
            actions: list[str] = []
        else:
            # Every present finding is pursued in deterministic detector order.
            scheduled_findings = findings
            actions = evaluate_findings(
                config,
                lane,
                scheduled_findings,
                goal_digest_value=goal_digest(goal),
                on_decision=supervise_finding,
                journal_events=events,
                ledger_key=(
                    config.ledger_key_path.read_bytes()
                    if config.ledger_key_path is not None and config.ledger_key_path.is_file()
                    else None
                ),
            )
        if goal is not None and not findings and not internal_findings and question_outcome == "none":
            record_observing(
                state_root=config.state_dir,
                lane=lane,
                goal=goal,
                reason="exact bound goal observed with no corrective finding",
            )
        append_finding_records(config, lane, findings + internal_findings)
        results.append(
            LanePassResult(
                lane=lane,
                ingested_events=len(events),
                findings_opened=len(scheduled_findings),
                ladder_actions=tuple(actions),
                completion_disputed=completion_disputed,
                completion_verified=completion_verified,
                question_outcome=question_outcome,
                validator_receipts_recorded=receipts_recorded,
            )
        )
        append_presence(
            PRESENCE_INSTANCE,
            f"chitra-journal:{lane}",
            session=session_ref,
            lanes=(lane,),
            mode="using",
            purpose="persistent oversight monitor pass",
            root=config.state_dir,
        )
    summary: dict[str, Any] = {
        "schema": MONITORD_SCHEMA,
        "lanes_observed": len(results),
        "findings_opened": sum(result.findings_opened for result in results),
        "completion_disputed": any(result.completion_disputed for result in results),
        "completion_verified": any(result.completion_verified for result in results),
        "question_answers_queued": sum(result.question_outcome == "answer_queued" for result in results),
        "questions_reasoning_required": sum(result.question_outcome == "reasoning_required" for result in results),
        "questions_operator_required": sum(result.question_outcome == "operator_required" for result in results),
        "validator_receipts_recorded": sum(result.validator_receipts_recorded for result in results),
        "shadow_mode": config.shadow_mode,
        "results": [
            {
                "lane": result.lane,
                "ingested_events": result.ingested_events,
                "findings_opened": result.findings_opened,
                "ladder_actions": list(result.ladder_actions),
                "completion_disputed": result.completion_disputed,
                "completion_verified": result.completion_verified,
                "question_outcome": result.question_outcome,
                "validator_receipts_recorded": result.validator_receipts_recorded,
            }
            for result in results
        ],
    }
    logger.info("monitord_pass_complete", **summary)
    return summary


def run_forever(config: MonitordConfig, *, stop_event: threading.Event | None = None) -> None:
    """Run the composed monitor passes until a service signal stops the process."""
    active_stop_event = stop_event or threading.Event()
    logger.info("monitord_started", state_dir=str(config.state_dir), poll_seconds=config.poll_seconds)
    notify_ready()
    try:
        while not active_stop_event.is_set():
            # A completion landing during the pass still counts: the wake is
            # cleared before the pass so its signal is not consumed early.
            _MONITOR_WAKE.clear()
            run_once(config)
            notify_watchdog()
            # Sleep in short slices so a finished worker wakes the loop for
            # the pass that consumes its record, while the stop event keeps
            # its prompt shutdown response.
            deadline = time.monotonic() + config.poll_seconds
            while not _MONITOR_WAKE.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or active_stop_event.wait(min(remaining, 0.25)):
                    break
    finally:
        # Running workers keep their threads; an unfinished run simply never
        # records a result and the next daemon start re-queues it.
        _RUN_POOL.shutdown()
        _REVIEW_POOL.shutdown()
        for _key, (_context, ingestor) in _INGESTOR_POOL.items():
            ingestor.close()
        _INGESTOR_POOL.clear()


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the intentionally small daemon CLI."""
    parser = argparse.ArgumentParser(
        prog="chitra-monitord",
        description="Persistently supervise exact goal-bound agent sessions through verified completion.",
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--transcript-root", type=Path, default=None)
    parser.add_argument("--transcript-bindings-path", type=Path, default=None)
    parser.add_argument("--dispatch-queue-dir", type=Path, default=None)
    parser.add_argument("--ledger-path", type=Path, default=None)
    parser.add_argument("--ledger-key-path", type=Path, default=None)
    parser.add_argument("--retry-delay-seconds", type=float, default=60.0)
    parser.add_argument("--findings-path", type=Path, default=None)
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--no-shadow-mode", dest="shadow_mode", action="store_false", help="Record findings outside shadow mode.")
    parser.add_argument("--once", action="store_true", help="Run one pass and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the daemon; malformed persisted input deliberately terminates it."""
    args = build_arg_parser().parse_args(argv)
    config = resolve_config(
        state_dir=args.state_dir,
        transcript_root=args.transcript_root,
        transcript_bindings_path=args.transcript_bindings_path,
        dispatch_queue_dir=args.dispatch_queue_dir,
        ledger_path=args.ledger_path,
        ledger_key_path=args.ledger_key_path,
        retry_delay_seconds=args.retry_delay_seconds,
        findings_path=args.findings_path,
        poll_seconds=args.poll_seconds,
        shadow_mode=args.shadow_mode,
    )
    if args.once:
        print(json.dumps(run_once(config), indent=2, sort_keys=True))
        return 0

    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_forever(config, stop_event=stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

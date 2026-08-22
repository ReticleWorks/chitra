"""watchd — deterministic semantic pane-status emitter for ``chitra.triaged``.

The events log remains a small wire contract consumed by ``chitra.triaged``.
At a detected turn-end, this watcher also forces the deterministic completion
boundary. Isolated watched-session reviewers see a turn end that carries a
completion claim, asks a question, made zero observable tool calls in the
turn itself, or followed a delivered dispatch order -- the turn shapes that
carry deferral, idle, and false-blocker defections, not just completion
claims. Zero-tool activity is decided at the current turn boundary from the
structured journal record when one exists, else exact rendered tool-call
markers among only the pane lines added since the previous reviewed turn
end -- or, when no boundary exists yet, over a marker-free capture;
scrollback chrome from earlier turns never suppresses review.
Review metadata is written only to Chitra-owned ledgers and never to pane text.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import signal
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog

from chitra._fsio import parse_iso8601
from chitra.agent_runtime import AgentStatusBroker, PaneStatus, StatusRuntimeError
from chitra.agent_status import AgentState, ManifestRepository
from chitra.completion_gate import (
    CompletionEvidence,
    CompletionReviewRecord,
    TodoItem,
    TurnEndAudit,
    append_completion_review,
    evaluate_turn_end,
    extract_completion_evidence,
    is_completion_claim,
)
from chitra.dispatch import enqueue_dispatch_order
from chitra.goal_enforcement import (
    BehaviorReviewer,
    ClaudeProcessReviewer,
    SessionReviewSignal,
    WatchedSessionBehavior,
    review_watched_session,
)
from chitra.goals import (
    GoalStatus,
    add_ask,
    get_goal,
    lane_id_from_session_ref,
    list_goals,
    mark_completion_gate_passed,
    session_host,
    update_now,
)
from chitra.journal import CanonicalType, EventJournal
from chitra.lane_activity import LaneActivity, LaneBackend, load_lane_activity, upsert_lane_activity
from chitra.lane_config import enabled_lanes
from chitra.live_handoff import perform_live_handoff
from chitra.orders import DispatchResult, DispatchStatus
from chitra.policy_config import load_policy_config
from chitra.reasoned_dispatch import abstaining_oracle, build_reasoned_dispatch
from chitra.reasoning import Oracle, PrinciplesIndex
from chitra.socket_api import ApiRuntime, ControlServer, default_socket_path
from chitra.state_paths import state_dir as default_state_dir
from chitra.systemd_notify import notify_ready, notify_watchdog

logger = structlog.get_logger(__name__)

EVENT_LOG_ENV_VAR = "CHITRA_WATCHD_EVENT_LOG"
INTERVAL_ENV_VAR = "CHITRA_WATCHD_INTERVAL"
PANES_ENV_VAR = "CHITRA_WATCHD_PANES"
SESSION_PREFIXES_ENV_VAR = "CHITRA_WATCHD_SESSION_PREFIXES"
SESSION_NAMES_ENV_VAR = "CHITRA_WATCHD_SESSION_NAMES"
EXCLUDED_SESSION_PREFIXES_ENV_VAR = "CHITRA_WATCHD_EXCLUDE_SESSION_PREFIXES"
TMUX_SOCKET_ENV_VAR = "CHITRA_WATCHD_TMUX_SOCKET"
IDLE_THRESHOLD_ENV_VAR = "CHITRA_WATCHD_IDLE_THRESHOLD_SECONDS"
MANIFEST_DIR_ENV_VAR = "CHITRA_AGENT_MANIFEST_DIR"
MAX_LOG_BYTES_ENV_VAR = "CHITRA_WATCHD_MAX_LOG_BYTES"
REVIEWER_COUNT_ENV_VAR = "CHITRA_WATCHD_REVIEWER_COUNT"
REVIEWER_COMMAND_ENV_VAR = "CHITRA_WATCHD_REVIEWER_COMMAND"
REVIEWER_MODEL_ENV_VAR = "CHITRA_WATCHD_REVIEWER_MODEL"
REASONED_DISPATCH_ENV_VAR = "CHITRA_WATCHD_REASONED_DISPATCH_ENABLED"
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_IDLE_THRESHOLD_SECONDS = 300.0
DEFAULT_MAX_LOG_BYTES = 5 * 1024 * 1024
DEFAULT_REVIEWER_COUNT = 2
DEFAULT_REVIEWER_COMMAND = "claude"
# Default to None so the reviewer inherits the ambient monitor model (ruling 3A:
# same model as the monitor, different context). Operators may still pin a
# cheaper model via --reviewer-model / CHITRA_WATCHD_REVIEWER_MODEL.
DEFAULT_REVIEWER_MODEL: str | None = None
DEFAULT_REVIEW_MAX_WORKERS = 2
DEFAULT_REASONED_DISPATCH_ENABLED = True
TRANSCRIPT_ROOT_ENV_VAR = "CHITRA_WATCHD_TRANSCRIPT_ROOT"
TRANSCRIPT_STALE_SECONDS_ENV_VAR = "CHITRA_WATCHD_TRANSCRIPT_STALE_SECONDS"
# Fifteen minutes. Long enough that an ordinary thinking pause is not a fault,
# short enough that a dead pipe surfaces within a sweep or two rather than the
# twenty-five hours the atlas-v5 one went unnoticed.
DEFAULT_TRANSCRIPT_STALE_SECONDS = 900
TRANSCRIPT_NAME = "tmux-transcript.log"
LANE_LAUNCH_NAME = "lane-launch.json"
JOURNAL_ROOT_ENV_VAR = "CHITRA_WATCHD_JOURNAL_ROOT"
CAPTURE_LINES = 60
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 10.0

_VOLATILE_LINE_RE = re.compile(
    r"^[\s]*[·✻✽✳✢✶*●○◐◯]|tokens\b|🪟|⏵⏵|esc to interrupt|ctrl\+b|^─+$|^[\s]*$|Press up to edit|globalVersion: [0-9.]+"
)
_TIMING_CHROME_RE = re.compile(r"\([0-9]+m? ?[0-9]*s?[^)]*\)")
# Exact rendered tool-call lines. Claude Code draws each tool call as
# "⏺ Tool(...)" with its result under "⎿  Tool(...)"; Codex draws each action
# as "• verb ...". The glyph must be followed by a non-space character so a
# bare rule line never counts as a call, and the generic "•" prose bullet is
# excluded entirely -- Codex answers legitimately begin prose lines with "•",
# so only its verb-shaped action line is tool activity. A Unicode-bullet list
# ("• first item") is answer prose, not tool activity. These markers decide
# only lines added since the previous reviewed turn end; the structured
# journal record takes precedence when the lane has one.
_RENDERED_TOOL_CALL_RE = re.compile(r"^\s*(?:⏺\s*\S|⎿\s*\S|•\s*(?:ran|read|edited|search|bash|shell|exec|patch)\b)")
CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
ReviewKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class Pane:
    """A live tmux pane, identified by the server-unique ``pane_id``."""

    pane_id: str
    target: str
    attached: bool = True
    backend: LaneBackend = "unknown"
    # Whether tmux currently has pipe-pane running for this pane. A lane whose
    # respawn did not re-arm the pipe reads exactly like a healthy one from the
    # pane alone; this is the field that tells them apart.
    pipe_armed: bool = False


@dataclass(frozen=True, slots=True)
class WatchdConfig:
    state_dir: Path
    events_log: Path
    lane_id: str | None = None
    tmux_socket: Path | None = None
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    panes_override: tuple[str, ...] | None = None
    session_names: tuple[str, ...] | None = None
    session_prefixes: tuple[str, ...] | None = None
    excluded_session_prefixes: tuple[str, ...] = ()
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    goals_root: Path | None = None
    completion_review_log: Path | None = None
    reviewer_count: int = DEFAULT_REVIEWER_COUNT
    reviewer_command: str = DEFAULT_REVIEWER_COMMAND
    reviewer_model: str | None = DEFAULT_REVIEWER_MODEL
    queue_dir: Path | None = None
    reasoned_dispatch_enabled: bool = DEFAULT_REASONED_DISPATCH_ENABLED
    manifest_dir: Path | None = None
    socket_path: Path = field(default_factory=default_socket_path)
    handoff_from: Path | None = None
    idle_threshold_seconds: float = DEFAULT_IDLE_THRESHOLD_SECONDS
    # Root of the governed-lanes tree, under which each lane's transcript lives
    # at <host>/<lane>/tmux-transcript.log. Unset means the check is off.
    transcript_root: Path | None = None
    transcript_stale_seconds: int = DEFAULT_TRANSCRIPT_STALE_SECONDS
    # Root of the W1 canonical event journals, one per lane at
    # <root>/journal/<lane>.jsonl. Unset (or a lane with no journal) means
    # tool-call evidence falls back to the pane capture's rendered markers.
    journal_root: Path | None = None

    def __post_init__(self) -> None:
        if self.reviewer_count < 1:
            raise ValueError("reviewer_count must be a positive integer")
        if self.idle_threshold_seconds <= 0:
            raise ValueError("idle_threshold_seconds must be a positive number")
        if self.transcript_stale_seconds < 1:
            raise ValueError("transcript_stale_seconds must be a positive integer")


@dataclass(frozen=True, slots=True)
class PendingCompletionReview:
    """Poll-thread-owned state for one isolated review running off-thread."""

    pane_id: str
    session_ref: str
    behavior_sha256: str
    turn_audit: TurnEndAudit
    completion_evidence: tuple[CompletionEvidence, ...]
    last_verified: str
    future: Future[SessionReviewSignal]


def normalize(content: str) -> list[str]:
    """Remove volatile pane chrome and the live operator input box.

    The final line beginning with ``❯`` starts the active input box.  It and
    everything below it are intentionally excluded so an operator typing in a
    pane cannot look like a lane state transition.
    """
    lines = content.splitlines()
    prompt_indices = [index for index, line in enumerate(lines) if line.lstrip().startswith(("❯", "›"))]
    if prompt_indices:
        lines = lines[: prompt_indices[-1]]

    normalized: list[str] = []
    for line in lines:
        if _VOLATILE_LINE_RE.search(line):
            continue
        line = _TIMING_CHROME_RE.sub("", line).rstrip()
        if line:
            normalized.append(line)
    return normalized


def pane_turn_finished(content: str, *, previous_state: AgentState | None, current_state: AgentState) -> bool:
    """Recognize a semantic working-to-idle boundary at a visible input row."""
    return previous_state == "working" and current_state == "idle" and pane_at_input_row(content) and bool(normalize(content))


def pane_at_input_row(content: str) -> bool:
    """Return true when a Claude or Codex input row is visible."""
    lines = content.splitlines()
    return any(line.lstrip().startswith(("❯", "›")) for line in lines[-12:])


def _run_command(command: Sequence[str], *, timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    """Run one tmux subprocess without allowing it to wedge the poll loop."""
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("watchd_subprocess_timeout", command=list(command), timeout_seconds=timeout)
        return subprocess.CompletedProcess(args=list(command), returncode=124, stdout="", stderr=f"timed out after {timeout}s")


def _tmux_command(command: Sequence[str], tmux_socket: Path | None) -> list[str]:
    """Add a lane's tmux socket while preserving the legacy default command."""
    values = list(command)
    if tmux_socket is None or not values or values[0] != "tmux":
        return values
    return [values[0], "-S", str(tmux_socket), *values[1:]]


def _pane_backend(command: str) -> LaneBackend:
    """Classify only allowlisted executable names; unknown commands stay unknown."""
    token = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    executable = Path(token).name.lower()
    if executable == "codex":
        return "codex"
    if executable in ("claude", "claude-code"):
        return "claude"
    if executable == "opencode":
        return "opencode"
    return "unknown"


def list_panes(
    *,
    runner: CommandRunner = _run_command,
    panes_override: Sequence[str] | None = None,
    session_names: Sequence[str] | None = None,
    session_prefixes: Sequence[str] | None = None,
    excluded_session_prefixes: Sequence[str] = (),
    tmux_socket: Path | None = None,
) -> list[Pane]:
    """Enumerate live tmux panes, deduplicated by server-assigned pane ID.

    ``panes_override`` is only for controlled tests or deployments that need
    to restrict observation temporarily; normal operation always uses
    ``tmux list-panes -a``. ``session_prefixes`` narrows live discovery to
    names beginning with one of the supplied prefixes; an empty value keeps
    the historical all-session behavior. ``excluded_session_prefixes`` wins
    over inclusion, so a broad legacy observer can explicitly leave an
    isolated instance's namespace alone.
    """
    if panes_override is not None:
        return [Pane(pane_id=target, target=target) for target in dict.fromkeys(panes_override) if target]

    allowed = frozenset(name.strip() for name in (session_names or ()) if name.strip())
    included = tuple(prefix.strip() for prefix in (session_prefixes or ()) if prefix.strip())
    excluded = tuple(prefix.strip() for prefix in excluded_session_prefixes if prefix.strip())

    result = runner(
        _tmux_command(
            [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{session_attached}\t#{pane_current_command}\t#{pane_pipe}",
            ],
            tmux_socket,
        )
    )
    if result.returncode != 0:
        logger.warning("watchd_list_panes_failed", stderr=result.stderr.strip())
        return []

    panes: list[Pane] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        pane_id = fields[0] if fields else ""
        target = fields[1] if len(fields) >= 2 else ""
        separator = "\t" if len(fields) >= 2 else ""
        if not separator or not pane_id or not target or pane_id in seen:
            continue
        session_name, _separator, _pane = target.partition(":")
        if (allowed or included) and session_name not in allowed and not any(
            session_name.startswith(prefix) for prefix in included
        ):
            continue
        if any(session_name.startswith(prefix) for prefix in excluded):
            continue
        seen.add(pane_id)
        attached = len(fields) < 3 or fields[2] != "0"
        backend = _pane_backend(fields[3]) if len(fields) >= 4 else "unknown"
        pipe_armed = len(fields) >= 5 and fields[4] == "1"
        panes.append(Pane(pane_id=pane_id, target=target, attached=attached, backend=backend, pipe_armed=pipe_armed))
    return panes


def capture_pane(
    pane: Pane, *, runner: CommandRunner = _run_command, tmux_socket: Path | None = None
) -> str | None:
    """Capture one pane, returning ``None`` when it vanished or tmux failed."""
    result = runner(
        _tmux_command(
            ["tmux", "capture-pane", "-p", "-J", "-t", pane.target, "-S", f"-{CAPTURE_LINES}"],
            tmux_socket,
        )
    )
    if result.returncode != 0:
        logger.info("watchd_capture_failed", pane_id=pane.pane_id, stderr=result.stderr.strip())
        return None
    return result.stdout


def event_line(lane_id: str, normalized_tail: Sequence[str], *, now: datetime | None = None) -> str:
    """Format one event exactly as ``triaged.parse_event_line`` consumes it."""
    timestamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    text = "CHANGE DETECTED: " + " | ".join(normalized_tail)
    return f"{timestamp} {lane_id} {text}\n"


def status_event_line(status: PaneStatus, *, now: datetime | None = None) -> str:
    """Format one semantic status transition for the legacy triaged log."""
    timestamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    source = status.source or "none"
    matched_rule = status.explain.matched_rule or "none"
    fallback = status.explain.fallback_reason or "none"
    attention = " needs operator input" if status.state == "blocked" else ""
    # The resume time is the one fact the response protocol cannot reconstruct
    # from the pane later, because the banner scrolls away. Carry it on the
    # event or lose it.
    resume = f" resume_at={status.explain.resume_at}" if status.explain.resume_at else ""
    text = (
        f"AGENT_STATUS state={status.state}{attention}{resume} pane_id={status.pane_id} target={status.target} "
        f"agent={status.agent} authority={status.authority} source={source} rule={matched_rule} fallback={fallback}"
    )
    return f"{timestamp} {status.lane_id} {text}\n"


def lane_dir(root: Path, session_ref: str) -> Path:
    """Return the governed-lane state directory for one session reference."""
    return root / session_host(session_ref) / lane_id_from_session_ref(session_ref)


def transcript_path(root: Path, session_ref: str) -> Path:
    """Return the governed-lane transcript path for one session reference."""
    return lane_dir(root, session_ref) / TRANSCRIPT_NAME


def transcript_pipe_fault(
    *,
    lane_directory: Path,
    pipe_armed: bool,
    last_change_at: str,
    now: datetime,
    stale_seconds: int = DEFAULT_TRANSCRIPT_STALE_SECONDS,
) -> str:
    """Return why this lane's transcript pipe is broken, or "" when it is fine.

    The lane-launch record is the lane's own declaration that it is governed,
    and a governed lane is supposed to have a growing transcript. A directory
    with no launch record gets no opinion; nothing is inferred about whether an
    unenrolled pane ought to be piped.

    Keying on the launch record rather than on the transcript matters. Measured
    2026-08-16, tophand:atlas-v5 has a launch record and *no transcript file at
    all* -- so a check that treated a missing transcript as "this lane is not
    piped" would have stayed silent on the exact lane it was written for. Its
    respawn dropped pipe-pane on 2026-08-15 and file-based liveness monitoring
    was blind for twenty-five hours.

    An unarmed pipe is reported whether or not the lane is currently busy. An
    idle lane with a dead pipe is not fine; it is a lane whose next output goes
    nowhere.
    """
    if not (lane_directory / LANE_LAUNCH_NAME).is_file():
        return ""
    transcript = lane_directory / TRANSCRIPT_NAME
    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        return "the lane is governed but has no transcript file, so nothing it has ever printed was recorded"
    if not pipe_armed:
        return "tmux has no pipe-pane running for this pane, so nothing is writing the transcript"
    transcript_age = int(now.timestamp() - mtime)
    if transcript_age <= stale_seconds:
        return ""
    if not last_change_at:
        return ""
    try:
        change_age = int(
            (now - parse_iso8601(last_change_at, require_timezone=True, normalize_utc=True)).total_seconds()
        )
    except ValueError:
        return ""
    if change_age > stale_seconds:
        # The lane is quiet, so a quiet transcript is agreement, not a fault.
        return ""
    return (
        f"the pane changed {change_age}s ago but the transcript has not grown for {transcript_age}s, "
        "so the pipe is armed and writing nowhere useful"
    )


def transcript_event_line(
    session_ref: str,
    pane: Pane,
    *,
    transcript: Path,
    reason: str,
    now: datetime | None = None,
) -> str:
    """Format one transcript-pipe fault exactly as triaged consumes it."""
    timestamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    text = (
        f"TRANSCRIPT_PIPE_STALE lane={session_ref} pane_id={pane.pane_id} target={pane.target} "
        f"pipe_armed={'1' if pane.pipe_armed else '0'} transcript={transcript}: {reason}"
    )
    return f"{timestamp} {pane.pane_id} {text}\n"


def append_event(event_log: Path, line: str, *, max_log_bytes: int = DEFAULT_MAX_LOG_BYTES) -> None:
    """Append under an exclusive lock, rotating the legacy-sized log first."""
    event_log.parent.mkdir(parents=True, exist_ok=True)
    lock_path = event_log.with_name(event_log.name + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if event_log.exists() and event_log.stat().st_size >= max_log_bytes:
                event_log.replace(event_log.with_name(event_log.name + ".1"))
            with event_log.open("a", encoding="utf-8") as output:
                output.write(line)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(slots=True)
class Watchd:
    """In-memory per-pane baselines for one long-lived watcher process."""

    config: WatchdConfig
    runner: CommandRunner = _run_command
    reviewer: BehaviorReviewer | None = None
    principles: PrinciplesIndex = field(default_factory=PrinciplesIndex)
    reasoning_oracle: Oracle = abstaining_oracle
    status_broker: AgentStatusBroker | None = None
    status_revisions: dict[str, int] = field(default_factory=dict)
    status_states: dict[str, AgentState] = field(default_factory=dict)
    transcript_faults: dict[str, str] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    reviewed_turns: set[ReviewKey] = field(default_factory=set)
    pending_reviews: dict[ReviewKey, PendingCompletionReview] = field(default_factory=dict)
    # pane_id -> ISO timestamp of that pane's previous reviewed turn end. A
    # dispatch order sent after this watermark preceded the current turn.
    turn_end_watermarks: dict[str, str] = field(default_factory=dict)
    # pane_id -> multiset of normalized lines on screen at that pane's
    # previous reviewed turn end. Lines on screen now that were not there
    # then belong to the current turn; that difference is the only turn
    # boundary a pane capture carries.
    _last_turn_end_capture: dict[str, Counter[str]] = field(default_factory=dict)
    _started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(), init=False, repr=False)
    _review_executor: ThreadPoolExecutor = field(init=False, repr=False)
    _review_executor_shutdown: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.status_broker is None:
            self.status_broker = AgentStatusBroker(
                self.config.state_dir,
                ManifestRepository(self.config.manifest_dir),
            )
        self._review_executor = ThreadPoolExecutor(
            max_workers=DEFAULT_REVIEW_MAX_WORKERS,
            thread_name_prefix="chitra-watchd-review",
        )

    def _raw_capture_path(self, pane_id: str) -> Path:
        """Return a filesystem-safe diagnostic capture path for one pane."""
        safe_id = hashlib.sha256(pane_id.encode("utf-8")).hexdigest()
        return self.config.state_dir / "watchd" / f"{safe_id}.raw"

    def _save_raw_capture(self, pane_id: str, content: str) -> None:
        raw_path = self._raw_capture_path(pane_id)
        try:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.warning("watchd_raw_capture_write_failed", pane_id=pane_id, path=str(raw_path), error=str(exc))

    def _session_ref(self, pane: Pane) -> str | None:
        root = self.config.goals_root or self.config.state_dir
        suffix = f":{pane.target}"
        matches = [record.session_ref for record in list_goals(root) if record.session_ref.endswith(suffix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning("watchd_ambiguous_goal_mapping", pane_id=pane.pane_id, target=pane.target, matches=matches)
        return None

    def _finalize_turn_review(
        self,
        pending: PendingCompletionReview,
        *,
        review_signal: SessionReviewSignal | None,
        review_error: str = "",
    ) -> None:
        """Apply one completed review result on the poll thread."""
        root = self.config.goals_root or self.config.state_dir
        review_log = self.config.completion_review_log or self.config.state_dir / "completion_reviews.jsonl"
        completion_verdict = pending.turn_audit.completion.verdict if pending.turn_audit.completion is not None else None
        ask = ""
        if review_signal is None and not review_error:
            # The turn-end gate decided this shape needs no isolated review.
            status: GoalStatus = "turn-finished-unverified"
            summary = f"{pending.turn_audit.summary}; isolated review was not run for this turn end"
            review_verdict: Literal["accept", "reject", "unavailable"] = "unavailable"
        elif review_signal is None:
            status = "blocked"
            summary = f"turn-end review unavailable: {review_error}"
            ask = "Review the lane manually because isolated watched-session review could not complete."
            review_verdict = "unavailable"
        elif review_signal.verdict == "reject":
            status = "blocked"
            summary = "watched-session direction or completion posture was rejected against the frozen goal"
            ask = "Review the lane's rejected direction or completion posture against its frozen goal."
            review_verdict = "reject"
        elif completion_verdict == "CLEAN":
            status = "done-pending-close"
            summary = pending.turn_audit.summary
            review_verdict = "accept"
        elif pending.turn_audit.condition == "completion_claim":
            status = "completion-disputed"
            summary = pending.turn_audit.summary
            ask = "Resolve the cited completion-gate gaps before treating this lane as complete."
            review_verdict = "accept"
        else:
            status = "turn-finished-unverified"
            summary = f"{pending.turn_audit.summary}; isolated review accepted the turn end with no completion claim"
            review_verdict = "accept"

        if status == "done-pending-close":
            mark_completion_gate_passed(
                root,
                pending.session_ref,
                now=summary,
                last_verified=datetime.now(UTC).isoformat(),
                completion_evidence=pending.completion_evidence,
            )
            assert self.status_broker is not None
            current = next(
                (item for item in self.status_broker.statuses() if item.pane_id == pending.pane_id),
                None,
            )
            self.status_broker.report_completion(
                pane_id=pending.pane_id,
                session_ref=pending.session_ref,
                agent=current.agent if current is not None else "unknown",
            )
        else:
            update_now(
                root,
                pending.session_ref,
                now=summary,
                status=status,
                last_verified=pending.last_verified,
            )
        if ask:
            add_ask(root, pending.session_ref, ask)
        append_completion_review(
            review_log,
            CompletionReviewRecord(
                session_ref=pending.session_ref,
                pane_id=pending.pane_id,
                behavior_sha256=pending.behavior_sha256,
                condition=pending.turn_audit.condition,
                completion_verdict=completion_verdict,
                review_signal_id=review_signal.signal_id if review_signal is not None else None,
                review_verdict=review_verdict,
                status=status,
                summary=summary,
            ),
        )
        if self.config.reasoned_dispatch_enabled and review_signal is not None and review_signal.verdict == "reject":
            goal = get_goal(root, pending.session_ref)
            if goal is None:
                raise RuntimeError(f"reviewed goal disappeared before reasoned dispatch: {pending.session_ref}")
            order = build_reasoned_dispatch(
                goal,
                review_signal,
                principles=self.principles,
                oracle=self.reasoning_oracle,
                review_rejection_confirmed=True,
            )
            if order is not None:
                queue_dir = self.config.queue_dir or self.config.state_dir / "queue"
                order_path = enqueue_dispatch_order(queue_dir, order)
                logger.info(
                    "watchd_reasoned_dispatch_enqueued",
                    session_ref=order.session_ref,
                    order_id=order.order_id,
                    message_kind=order.message_kind,
                    path=str(order_path),
                )

    def _drain_completed_reviews(self) -> None:
        """Collect ready futures without waiting; all shared-state writes stay here."""
        assert self.status_broker is not None
        if self.status_broker.frozen:
            return
        for key, pending in list(self.pending_reviews.items()):
            if not pending.future.done():
                continue
            try:
                review_signal = pending.future.result()
            except Exception as exc:  # noqa: BLE001 - any reviewer failure must fail closed
                review_error = str(exc) or type(exc).__name__
                try:
                    self._finalize_turn_review(pending, review_signal=None, review_error=review_error)
                except StatusRuntimeError:
                    continue
            else:
                try:
                    self._finalize_turn_review(pending, review_signal=review_signal)
                except StatusRuntimeError:
                    continue
            del self.pending_reviews[key]

    def _turn_followed_delivered_order(self, session_ref: str, *, since: str) -> bool:
        """Whether dispatchd recorded a sent order for this session after ``since``."""
        queue_dir = self.config.queue_dir or self.config.state_dir / "queue"
        results = queue_dir / "results"
        if not results.is_dir():
            return False
        for path in sorted(results.glob("*.json")):
            try:
                result = DispatchResult.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if result.session_ref == session_ref and result.status == DispatchStatus.SENT and result.at > since:
                return True
        return False

    def _turn_made_tool_calls(self, session_ref: str, pane: Pane) -> bool | None:
        """Whether the CURRENT turn made an observable tool call.

        The structured W1 journal is the primary source when it exists for
        this lane: a TOOL_CALL event observed since the previous reviewed
        turn end is proof the current turn called a tool, and its absence is
        proof it did not. With no journal the pane capture's exact rendered
        call markers are the only remaining evidence -- and because that
        capture spans many turns, an unknown result means the capture cannot
        attribute chrome to this turn and the caller must not treat the turn
        as zero-tool.
        """
        journal_root = self.config.journal_root
        if journal_root is not None:
            lane = lane_id_from_session_ref(session_ref)
            events = EventJournal(journal_root, lane).load()
            if events:
                watermark = self.turn_end_watermarks.get(pane.pane_id)
                recent = [
                    event
                    for event in events
                    if event.normalized_type in (CanonicalType.TOOL_CALL, CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR)
                    and (watermark is None or (event.observed_at or "") > watermark)
                ]
                return bool(recent)
        return None

    def _turn_had_rendered_tool_calls(self, pane_id: str, text: str) -> bool | None:
        """Whether the current turn's own lines contain rendered tool calls.

        A capture with no rendered call markers anywhere proves the current
        turn made none, whatever older scrollback sits above it. When markers
        are on screen, the previous reviewed turn end is the only boundary a
        pane capture carries: lines added since then belong to the current
        turn and only they may count as activity. With markers on screen but
        no such boundary the chrome cannot be attributed to any one turn and
        the result is unknown.
        """
        if not any(_RENDERED_TOOL_CALL_RE.match(line) for line in text.splitlines()):
            return False
        boundary = self._last_turn_end_capture.get(pane_id)
        if boundary is None:
            return None
        fresh_lines = Counter(text.splitlines()) - boundary
        return any(_RENDERED_TOOL_CALL_RE.match(line) for line in fresh_lines)

    def _turn_end_requires_review(self, session_ref: str, pane: Pane, text: str, *, since: str) -> bool:
        """Structural triggers that send one enrolled lane's turn end to review.

        A completion claim always reviews. So does a turn that asks a question
        and a turn that answers a delivered dispatch order -- the shapes that
        carry deferral, idle, and false-blocker defections a completion-claim
        regex never sees.

        The zero-tool trigger reads only the CURRENT turn: structured tool-call
        records since the previous reviewed turn end when the journal has
        them, else exact rendered tool-call markers attributed to only the
        pane lines added since that turn end. Scrollback chrome from earlier
        turns never counts as activity in this one, so prior tool chrome does
        not suppress review of a quiet deferral turn; and when on-screen
        chrome cannot be attributed to any turn, the trigger abstains rather
        than classifying the turn as zero-tool.
        """
        if is_completion_claim(text):
            return True
        if "?" in text:
            return True
        if self._turn_followed_delivered_order(session_ref, since=since):
            return True
        structured = self._turn_made_tool_calls(session_ref, pane)
        if structured is not None:
            return not structured
        rendered = self._turn_had_rendered_tool_calls(pane.pane_id, text)
        if rendered is None:
            return False
        return not rendered

    def _review_turn_end(self, pane: Pane, content: str) -> None:
        """Run the cheap gate inline and schedule completion review off-thread."""
        text = "\n".join(normalize(content)).strip()
        behavior_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = (pane.pane_id, behavior_sha256)
        if key in self.reviewed_turns:
            return
        root = self.config.goals_root or self.config.state_dir
        review_log = self.config.completion_review_log or self.config.state_dir / "completion_reviews.jsonl"
        session_ref = self._session_ref(pane)
        if session_ref is None:
            self.reviewed_turns.add(key)
            append_completion_review(
                review_log,
                CompletionReviewRecord(
                    session_ref=pane.target,
                    pane_id=pane.pane_id,
                    behavior_sha256=behavior_sha256,
                    condition="turn_end_without_completion_claim",
                    review_verdict="unavailable",
                    status="untracked",
                    summary="turn-end review failed closed: no unique frozen goal maps to this pane",
                ),
            )
            return

        goal = next(record for record in list_goals(root) if record.session_ref == session_ref)
        if goal.interview_receipt is None or not goal.enrolled_done_when_items:
            self.reviewed_turns.add(key)
            append_completion_review(
                review_log,
                CompletionReviewRecord(
                    session_ref=session_ref,
                    pane_id=pane.pane_id,
                    behavior_sha256=behavior_sha256,
                    condition="completion_claim" if is_completion_claim(text) else "turn_end_without_completion_claim",
                    completion_verdict="COMPLETION_DISPUTE" if is_completion_claim(text) else None,
                    review_verdict="unavailable",
                    status="unenrolled",
                    summary="turn-end review failed closed: the goal has no interview receipt or frozen done items",
                ),
            )
            return
        policy = load_policy_config().completion_gate
        completion_evidence = tuple(extract_completion_evidence(text))
        enrolled_todos = [
            TodoItem(
                id=item.id,
                text=item.text,
                status="done",
                validator=item.validator,
                required_receipt=item.required_receipt,
            )
            for item in goal.enrolled_done_when_items
        ]
        if not enrolled_todos:
            enrolled_todos = [TodoItem(text="interview enrollment receipt and frozen done items", status="missing")]
        turn_audit = evaluate_turn_end(
            text,
            todo_items=enrolled_todos,
            evidence=completion_evidence,
            policy=policy,
            open_asks=goal.open_asks,
            blockers=(goal.needs,) if goal.needs else (),
        )
        pending = PendingCompletionReview(
            pane_id=pane.pane_id,
            session_ref=session_ref,
            behavior_sha256=behavior_sha256,
            turn_audit=turn_audit,
            completion_evidence=completion_evidence,
            last_verified=goal.last_verified,
            future=Future(),
        )
        since = self.turn_end_watermarks.get(pane.pane_id, self._started_at)
        self.turn_end_watermarks[pane.pane_id] = datetime.now(UTC).isoformat()
        requires_review = self._turn_end_requires_review(session_ref, pane, text, since=since)
        self._last_turn_end_capture[pane.pane_id] = Counter(normalize(content))
        if not requires_review:
            self.reviewed_turns.add(key)
            self._finalize_turn_review(pending, review_signal=None)
            return

        if len(self.pending_reviews) >= DEFAULT_REVIEW_MAX_WORKERS:
            update_now(
                root,
                session_ref,
                now=f"{turn_audit.summary}; isolated review is waiting for bounded reviewer capacity",
                status="turn-finished-unverified",
            )
            return

        behavior = WatchedSessionBehavior.from_turn(session_ref, text)
        reviewer = self.reviewer or ClaudeProcessReviewer(
            command=self.config.reviewer_command,
            model=self.config.reviewer_model,
        )
        future = self._review_executor.submit(
            review_watched_session,
            root,
            session_ref,
            behavior,
            reviewer=reviewer,
            reviewer_count=self.config.reviewer_count,
        )
        pending = PendingCompletionReview(
            pane_id=pane.pane_id,
            session_ref=session_ref,
            behavior_sha256=behavior_sha256,
            turn_audit=turn_audit,
            completion_evidence=completion_evidence,
            last_verified=goal.last_verified,
            future=future,
        )
        self.pending_reviews[key] = pending
        self.reviewed_turns.add(key)
        update_now(
            root,
            session_ref,
            now=f"{turn_audit.summary}; isolated review is in flight",
            status="turn-finished-unverified",
        )

    def _check_transcript_pipe(self, pane: Pane, session_ref: str, *, last_change_at: str) -> int:
        """Emit a transcript-pipe fault for one pane, once per fault.

        Repeated emission is deliberately suppressed: the poll loop runs every
        few seconds, and a fault that logs on every pass buries itself. The
        memo clears when the pipe recovers, so a recurrence is reported again.
        """
        if self.config.transcript_root is None:
            return 0
        transcript = transcript_path(self.config.transcript_root, session_ref)
        reason = transcript_pipe_fault(
            lane_directory=lane_dir(self.config.transcript_root, session_ref),
            pipe_armed=pane.pipe_armed,
            last_change_at=last_change_at,
            now=datetime.now(UTC),
            stale_seconds=self.config.transcript_stale_seconds,
        )
        if not reason:
            self.transcript_faults.pop(pane.pane_id, None)
            return 0
        if self.transcript_faults.get(pane.pane_id) == reason:
            return 0
        self.transcript_faults[pane.pane_id] = reason
        append_event(
            self.config.events_log,
            transcript_event_line(session_ref, pane, transcript=transcript, reason=reason),
            max_log_bytes=self.config.max_log_bytes,
        )
        logger.warning(
            "watchd_transcript_pipe_stale",
            session_ref=session_ref,
            pane_id=pane.pane_id,
            transcript=str(transcript),
            pipe_armed=pane.pipe_armed,
            reason=reason,
        )
        return 1

    def poll_once(self) -> int:
        """Capture panes and emit only semantic status transitions."""
        self._drain_completed_reviews()
        assert self.status_broker is not None
        if self.status_broker.frozen:
            return 0
        emitted = 0
        root = self.config.goals_root or self.config.state_dir
        existing_activity = {record.session_ref: record for record in load_lane_activity(root)}
        activity_updates: list[LaneActivity] = []
        for pane in list_panes(
            runner=self.runner,
            panes_override=self.config.panes_override,
            session_names=self.config.session_names,
            session_prefixes=self.config.session_prefixes,
            excluded_session_prefixes=self.config.excluded_session_prefixes,
            tmux_socket=self.config.tmux_socket,
        ):
            content = capture_pane(pane, runner=self.runner, tmux_socket=self.config.tmux_socket)
            if content is None:
                continue
            self._save_raw_capture(pane.pane_id, content)
            observed_at = datetime.now(UTC).isoformat()
            session_ref = self._session_ref(pane)
            try:
                self.status_broker.observe(
                    pane_id=pane.pane_id,
                    target=pane.target,
                    session_ref=session_ref,
                    lane_id=self.config.lane_id or pane.target,
                    detected_agent=pane.backend,
                    snapshot=content,
                    tmux_socket=self.config.tmux_socket,
                )
            except StatusRuntimeError:
                break
            status = next(item for item in self.status_broker.statuses() if item.pane_id == pane.pane_id)
            previous_state = self.status_states.get(pane.pane_id)
            previous_revision = self.status_revisions.get(pane.pane_id)
            changed = previous_revision is None or previous_revision != status.revision
            if pane_turn_finished(content, previous_state=previous_state, current_state=status.state):
                self._review_turn_end(pane, content)
            if session_ref is not None:
                prior_activity = existing_activity.get(session_ref)
                activity_updates.append(
                    LaneActivity(
                        session_ref=session_ref,
                        pane_id=pane.pane_id,
                        last_change_at=(observed_at if changed or prior_activity is None else prior_activity.last_change_at),
                        last_seen_at=observed_at,
                        attached=pane.attached,
                        backend=pane.backend
                        if pane.backend != "unknown"
                        else ("unknown" if prior_activity is None else prior_activity.backend),
                    )
                )
            if session_ref is not None:
                emitted += self._check_transcript_pipe(
                    pane,
                    session_ref,
                    last_change_at=activity_updates[-1].last_change_at,
                )
            self.status_states[pane.pane_id] = status.state
            self.status_revisions[pane.pane_id] = status.revision
            if previous_revision is None:
                continue
            if changed:
                append_event(
                    self.config.events_log,
                    status_event_line(status),
                    max_log_bytes=self.config.max_log_bytes,
                )
                emitted += 1
        self._drain_completed_reviews()
        upsert_lane_activity(root, activity_updates)
        return emitted

    def shutdown(self) -> None:
        """Finish running reviews, cancel queued work, and collect every result."""
        if self._review_executor_shutdown:
            return
        self._review_executor_shutdown = True
        self._review_executor.shutdown(wait=True, cancel_futures=True)
        self._drain_completed_reviews()


def _env_value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _positive_float(value: str, *, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def _positive_int(value: str, *, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _boolean(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _split_prefixes(value: str | None) -> tuple[str, ...]:
    """Normalize a comma-separated namespace filter without inventing values."""
    if not value:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def resolve_config(
    *,
    state_dir: Path | None = None,
    events_log: Path | None = None,
    interval_seconds: float | None = None,
    panes_override: Sequence[str] | None = None,
    tmux_socket: Path | None = None,
    session_names: Sequence[str] | None = None,
    session_prefixes: Sequence[str] | None = None,
    excluded_session_prefixes: Sequence[str] | None = None,
    max_log_bytes: int | None = None,
    reviewer_count: int | None = None,
    reviewer_command: str | None = None,
    reviewer_model: str | None = None,
    reasoned_dispatch_enabled: bool | None = None,
    manifest_dir: Path | None = None,
    socket_path: Path | None = None,
    handoff_from: Path | None = None,
    idle_threshold_seconds: float | None = None,
    transcript_root: Path | None = None,
    transcript_stale_seconds: int | None = None,
    journal_root: Path | None = None,
) -> WatchdConfig:
    """Resolve CLI values, then ``CHITRA_*`` overrides, then generic defaults."""
    configured_state_dir = state_dir or default_state_dir()
    configured_events_log = events_log or Path(_env_value(EVENT_LOG_ENV_VAR) or configured_state_dir / "events.log")
    configured_interval = interval_seconds
    if configured_interval is None:
        raw_interval = _env_value(INTERVAL_ENV_VAR)
        configured_interval = _positive_float(raw_interval, name=INTERVAL_ENV_VAR) if raw_interval else DEFAULT_INTERVAL_SECONDS
    if configured_interval <= 0:
        raise ValueError("interval_seconds must be a positive number")
    configured_max_log_bytes = max_log_bytes
    if configured_max_log_bytes is None:
        raw_max_log_bytes = _env_value(MAX_LOG_BYTES_ENV_VAR)
        configured_max_log_bytes = (
            _positive_int(raw_max_log_bytes, name=MAX_LOG_BYTES_ENV_VAR) if raw_max_log_bytes else DEFAULT_MAX_LOG_BYTES
        )
    if configured_max_log_bytes <= 0:
        raise ValueError("max_log_bytes must be a positive integer")
    configured_panes = panes_override
    if configured_panes is None:
        raw_panes = _env_value(PANES_ENV_VAR)
        configured_panes = tuple(item.strip() for item in raw_panes.split(",") if item.strip()) if raw_panes else None
    configured_tmux_socket = tmux_socket or (Path(raw_socket) if (raw_socket := _env_value(TMUX_SOCKET_ENV_VAR)) else None)
    configured_session_names = (
        tuple(name.strip() for name in session_names if name.strip())
        if session_names is not None
        else _split_prefixes(_env_value(SESSION_NAMES_ENV_VAR))
    )
    configured_session_prefixes = (
        tuple(prefix.strip() for prefix in session_prefixes if prefix.strip())
        if session_prefixes is not None
        else _split_prefixes(_env_value(SESSION_PREFIXES_ENV_VAR))
    )
    configured_excluded_session_prefixes = (
        tuple(prefix.strip() for prefix in excluded_session_prefixes if prefix.strip())
        if excluded_session_prefixes is not None
        else _split_prefixes(_env_value(EXCLUDED_SESSION_PREFIXES_ENV_VAR))
    )
    configured_reviewer_count = reviewer_count
    if configured_reviewer_count is None:
        raw_reviewer_count = _env_value(REVIEWER_COUNT_ENV_VAR)
        configured_reviewer_count = (
            _positive_int(raw_reviewer_count, name=REVIEWER_COUNT_ENV_VAR) if raw_reviewer_count else DEFAULT_REVIEWER_COUNT
        )
    if configured_reviewer_count < 1:
        raise ValueError("reviewer_count must be a positive integer")
    configured_reviewer_command = (
        _env_value(REVIEWER_COMMAND_ENV_VAR) or DEFAULT_REVIEWER_COMMAND if reviewer_command is None else reviewer_command.strip()
    )
    if not configured_reviewer_command:
        raise ValueError("reviewer_command must be non-empty")
    # None means "inherit the ambient monitor model" (ruling 3A); only a
    # non-empty override pins a specific model. An explicit empty value falls
    # back to the ambient model rather than erroring.
    if reviewer_model is not None:
        configured_reviewer_model = reviewer_model.strip() or None
    else:
        configured_reviewer_model = _env_value(REVIEWER_MODEL_ENV_VAR) or DEFAULT_REVIEWER_MODEL
    configured_reasoned_dispatch = reasoned_dispatch_enabled
    if configured_reasoned_dispatch is None:
        raw_reasoned_dispatch = _env_value(REASONED_DISPATCH_ENV_VAR)
        configured_reasoned_dispatch = (
            _boolean(raw_reasoned_dispatch, name=REASONED_DISPATCH_ENV_VAR)
            if raw_reasoned_dispatch is not None
            else DEFAULT_REASONED_DISPATCH_ENABLED
        )
    configured_idle_threshold = idle_threshold_seconds
    if configured_idle_threshold is None:
        raw_idle_threshold = _env_value(IDLE_THRESHOLD_ENV_VAR)
        configured_idle_threshold = (
            _positive_float(raw_idle_threshold, name=IDLE_THRESHOLD_ENV_VAR)
            if raw_idle_threshold
            else DEFAULT_IDLE_THRESHOLD_SECONDS
        )
    configured_manifest_dir = manifest_dir
    if configured_manifest_dir is None and (raw_manifest_dir := _env_value(MANIFEST_DIR_ENV_VAR)) is not None:
        configured_manifest_dir = Path(raw_manifest_dir)
    configured_transcript_root = transcript_root
    if configured_transcript_root is None and (raw_transcript_root := _env_value(TRANSCRIPT_ROOT_ENV_VAR)) is not None:
        configured_transcript_root = Path(raw_transcript_root)
    configured_transcript_stale = transcript_stale_seconds
    if configured_transcript_stale is None:
        raw_transcript_stale = _env_value(TRANSCRIPT_STALE_SECONDS_ENV_VAR)
        configured_transcript_stale = (
            _positive_int(raw_transcript_stale, name=TRANSCRIPT_STALE_SECONDS_ENV_VAR)
            if raw_transcript_stale
            else DEFAULT_TRANSCRIPT_STALE_SECONDS
        )
    configured_journal_root = journal_root
    if configured_journal_root is None and (raw_journal_root := _env_value(JOURNAL_ROOT_ENV_VAR)) is not None:
        configured_journal_root = Path(raw_journal_root)
    return WatchdConfig(
        state_dir=configured_state_dir,
        events_log=configured_events_log,
        interval_seconds=configured_interval,
        panes_override=tuple(configured_panes) if configured_panes is not None else None,
        tmux_socket=configured_tmux_socket,
        session_names=configured_session_names or None,
        session_prefixes=configured_session_prefixes or None,
        excluded_session_prefixes=configured_excluded_session_prefixes,
        max_log_bytes=configured_max_log_bytes,
        reviewer_count=configured_reviewer_count,
        reviewer_command=configured_reviewer_command,
        reviewer_model=configured_reviewer_model,
        reasoned_dispatch_enabled=configured_reasoned_dispatch,
        manifest_dir=configured_manifest_dir,
        socket_path=socket_path or default_socket_path(),
        handoff_from=handoff_from,
        idle_threshold_seconds=configured_idle_threshold,
        transcript_root=configured_transcript_root,
        transcript_stale_seconds=configured_transcript_stale,
        journal_root=configured_journal_root,
    )


def _start_control_server(
    broker: AgentStatusBroker,
    config: WatchdConfig,
    stop_event: threading.Event,
) -> ControlServer:
    runtime = ApiRuntime(broker)
    if config.handoff_from is None:
        server = ControlServer(config.socket_path, runtime)
        server.start()
    else:
        temporary_socket = config.handoff_from.with_name(
            f".{config.handoff_from.name}.handoff-new-{os.getpid()}"
        )
        server = ControlServer(temporary_socket, runtime)
        perform_live_handoff(
            canonical_socket=config.handoff_from,
            replacement_server=server,
            replacement_runtime=runtime,
        )

    def stop_replacement() -> None:
        stop_event.set()
        server.shutdown()

    runtime.set_shutdown_callback(stop_replacement)
    return server


def run_forever(watchd: Watchd, *, stop_event: threading.Event | None = None) -> None:
    """Run until a SIGTERM/SIGINT handler (or caller) requests a clean stop."""
    stop_event = stop_event or threading.Event()
    assert watchd.status_broker is not None
    server = _start_control_server(watchd.status_broker, watchd.config, stop_event)
    logger.info("watchd_started", events_log=str(watchd.config.events_log), interval_seconds=watchd.config.interval_seconds)
    notify_ready()
    try:
        while not stop_event.is_set():
            watchd.poll_once()
            notify_watchdog()
            stop_event.wait(watchd.config.interval_seconds)
    finally:
        watchd.shutdown()
        server.shutdown()


def build_lane_watchers(
    lanes_file: Path | None,
    base_config: WatchdConfig,
    *,
    status_broker: AgentStatusBroker | None = None,
) -> tuple[Watchd, ...]:
    """Build one in-memory watcher per declared lane for one shared process."""
    watchers: list[Watchd] = []
    for lane in enabled_lanes(lanes_file):
        watchers.append(
            Watchd(
                replace(
                    base_config,
                    lane_id=lane.identifier,
                    state_dir=lane.state_dir,
                    events_log=lane.events_log,
                    tmux_socket=lane.tmux_socket,
                    session_names=None,
                    session_prefixes=(lane.tmux_session,),
                    excluded_session_prefixes=(),
                    goals_root=lane.state_dir,
                    completion_review_log=lane.state_dir / "completion_reviews.jsonl",
                    queue_dir=lane.queue_dir,
                ),
                status_broker=status_broker,
            )
        )
    return tuple(watchers)


def run_lanes_once(lanes_file: Path | None, base_config: WatchdConfig) -> int:
    """Poll every enabled lane once and release the short-lived reviewers."""
    watchers = build_lane_watchers(lanes_file, base_config)
    try:
        return sum(watcher.poll_once() for watcher in watchers)
    finally:
        for watcher in watchers:
            watcher.shutdown()


def run_lanes_forever(
    lanes_file: Path | None,
    base_config: WatchdConfig,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Run one shared watcher process over all enabled lane sockets."""
    active_stop_event = stop_event or threading.Event()
    broker = AgentStatusBroker(base_config.state_dir, ManifestRepository(base_config.manifest_dir))
    watchers = build_lane_watchers(lanes_file, base_config, status_broker=broker)
    server = _start_control_server(broker, base_config, active_stop_event)
    logger.info("watchd_started", lanes_file=str(lanes_file), lane_count=len(watchers))
    notify_ready()
    try:
        while not active_stop_event.is_set():
            for watcher in watchers:
                watcher.poll_once()
            notify_watchdog()
            active_stop_event.wait(base_config.interval_seconds)
    finally:
        for watcher in watchers:
            watcher.shutdown()
        server.shutdown()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watchd", description="Deterministic tmux-pane change emitter for triaged.")
    parser.add_argument("--state-dir", type=Path, default=None, help="Watcher state root (default: CHITRA_STATE_DIR or /var/lib/chitra).")
    parser.add_argument(
        "--tmux-socket", type=Path, default=None, help="Explicit tmux socket (default: CHITRA_WATCHD_TMUX_SOCKET)."
    )
    parser.add_argument(
        "--session-name",
        action="append",
        default=None,
        help="Observe only this exact tmux session (repeatable; default: CHITRA_WATCHD_SESSION_NAMES).",
    )
    parser.add_argument(
        "--lanes-file",
        type=Path,
        default=None,
        help="Rendered lane declaration; when set, observe every enabled lane socket.",
    )
    parser.add_argument(
        "--events-log", type=Path, default=None, help="Events log (default: CHITRA_WATCHD_EVENT_LOG or <state-dir>/events.log)."
    )
    parser.add_argument("--interval-seconds", type=float, default=None, help="Poll interval (default: CHITRA_WATCHD_INTERVAL or 5).")
    parser.add_argument(
        "--panes", default=None, help="Comma-separated tmux targets for a controlled override (default: live tmux enumeration)."
    )
    parser.add_argument(
        "--session-prefix",
        action="append",
        default=None,
        help="Observe only tmux sessions with this prefix (repeatable; default: CHITRA_WATCHD_SESSION_PREFIXES).",
    )
    parser.add_argument(
        "--exclude-session-prefix",
        action="append",
        default=None,
        help="Never observe tmux sessions with this prefix (repeatable; default: CHITRA_WATCHD_EXCLUDE_SESSION_PREFIXES).",
    )
    parser.add_argument(
        "--max-log-bytes", type=int, default=None, help="Rotate at this size (default: CHITRA_WATCHD_MAX_LOG_BYTES or 5 MiB)."
    )
    parser.add_argument(
        "--idle-threshold-seconds",
        type=float,
        default=None,
        help="Deprecated compatibility option; semantic manifests now author idle status.",
    )
    parser.add_argument(
        "--transcript-root",
        type=Path,
        default=None,
        help=(
            "Governed-lanes root holding <host>/<lane>/tmux-transcript.log. Enables the transcript-pipe "
            "liveness check (default: CHITRA_WATCHD_TRANSCRIPT_ROOT; unset disables it)."
        ),
    )
    parser.add_argument(
        "--transcript-stale-seconds",
        type=int,
        default=None,
        help="Age at which a transcript counts as not growing (default: CHITRA_WATCHD_TRANSCRIPT_STALE_SECONDS or 900).",
    )
    parser.add_argument(
        "--journal-root",
        type=Path,
        default=None,
        help=(
            "Canonical event journal root holding <root>/journal/<lane>.jsonl. Gives the zero-tool review trigger "
            "structured tool-call evidence for the current turn (default: CHITRA_WATCHD_JOURNAL_ROOT; unset falls "
            "back to exact rendered pane markers)."
        ),
    )
    parser.add_argument(
        "--agent-manifest-dir",
        type=Path,
        default=None,
        help="Local TOML manifest overrides (default: CHITRA_AGENT_MANIFEST_DIR).",
    )
    parser.add_argument(
        "--socket-path",
        type=Path,
        default=None,
        help="Local NDJSON control socket (default: CHITRA_SOCKET_PATH or /run/chitra/chitra.sock).",
    )
    parser.add_argument(
        "--handoff-from",
        type=Path,
        default=None,
        help="Replace the running watchd server at this socket after a verified live handoff.",
    )
    parser.add_argument(
        "--reviewer-count",
        type=int,
        default=None,
        help="Reviewers in the normal completion-claim round (default: CHITRA_WATCHD_REVIEWER_COUNT or 2).",
    )
    parser.add_argument(
        "--reviewer-command",
        default=None,
        help="Isolated reviewer command (default: CHITRA_WATCHD_REVIEWER_COMMAND or claude).",
    )
    parser.add_argument(
        "--reviewer-model",
        default=None,
        help="Pinned isolated reviewer model (default: CHITRA_WATCHD_REVIEWER_MODEL, else the ambient monitor model).",
    )
    parser.add_argument(
        "--reasoned-dispatch",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enqueue reasoned corrections for rejected reviews (default: enabled; env CHITRA_WATCHD_REASONED_DISPATCH_ENABLED).",
    )
    parser.add_argument("--once", action="store_true", help="Capture and compare once, then exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    panes_override = tuple(item.strip() for item in args.panes.split(",") if item.strip()) if args.panes is not None else None
    config = resolve_config(
        state_dir=args.state_dir,
        events_log=args.events_log,
        interval_seconds=args.interval_seconds,
        panes_override=panes_override,
        tmux_socket=args.tmux_socket,
        session_names=args.session_name,
        session_prefixes=args.session_prefix,
        excluded_session_prefixes=args.exclude_session_prefix,
        max_log_bytes=args.max_log_bytes,
        reviewer_count=args.reviewer_count,
        reviewer_command=args.reviewer_command,
        reviewer_model=args.reviewer_model,
        reasoned_dispatch_enabled=args.reasoned_dispatch,
        manifest_dir=args.agent_manifest_dir,
        socket_path=args.socket_path,
        handoff_from=args.handoff_from,
        idle_threshold_seconds=args.idle_threshold_seconds,
        transcript_root=args.transcript_root,
        transcript_stale_seconds=args.transcript_stale_seconds,
        journal_root=args.journal_root,
    )
    if args.lanes_file is not None:
        if args.once:
            print(f'{{"emitted": {run_lanes_once(args.lanes_file, config)}}}')
            return 0
        stop_event = threading.Event()

        def request_lane_stop(_signum: int, _frame: object) -> None:
            stop_event.set()

        signal.signal(signal.SIGTERM, request_lane_stop)
        signal.signal(signal.SIGINT, request_lane_stop)
        run_lanes_forever(args.lanes_file, config, stop_event=stop_event)
        return 0
    watcher = Watchd(config)
    if args.once:
        try:
            print(f'{{"emitted": {watcher.poll_once()}}}')
        finally:
            watcher.shutdown()
        return 0

    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_forever(watcher, stop_event=stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

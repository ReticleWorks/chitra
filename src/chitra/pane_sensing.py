"""Pane sensing for monitord — the pane-side checks a governed lane still needs.

The transcript journal carries most of what monitord needs, but three facts
exist only at the pane: the semantic status classification (which feeds the
local status socket and the lane-activity facts the rate-limit guard's
quiescence check reads), the rate-limit banner an agent renders when it hits
a provider cap, and the health of the tmux pipe that feeds the transcript.
This is the retired watchd's sensing path, scoped to the lanes one monitord
instance is declared to own.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from chitra._fsio import parse_iso8601
from chitra.agent_runtime import AgentStatusBroker, StatusRuntimeError
from chitra.agent_status import RATE_LIMITED_STATES
from chitra.dispatch import TmuxRunner, run_on_host
from chitra.lane_activity import LaneActivity, LaneBackend, load_lane_activity, upsert_lane_activity
from chitra.lane_config import LaneSpec

logger = structlog.get_logger(__name__)

TRANSCRIPT_NAME = "tmux-transcript.log"
LANE_LAUNCH_NAME = "lane-launch.json"
# Fifteen minutes. Long enough that an ordinary thinking pause is not a fault,
# short enough that a dead pipe surfaces within a sweep or two rather than the
# twenty-five hours the atlas-v5 one went unnoticed.
DEFAULT_TRANSCRIPT_STALE_SECONDS = 900
CAPTURE_LINES = 60
SUBPROCESS_TIMEOUT_SECONDS = 10

_PANE_FIELDS = "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{session_attached}\t#{pane_current_command}\t#{pane_pipe}"

PaneAlerter = Callable[[str, str], None]


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


@dataclass(slots=True)
class PaneSenseState:
    """Per-process sensing memory carried across monitor passes.

    ``status_revisions`` detects a real semantic transition; ``transcript_faults``
    memoizes a reported pipe fault so a broken pipe logs once per breakage, not
    once per pass, and clears when the pipe recovers so a recurrence reports again.
    """

    status_revisions: dict[str, int] = field(default_factory=dict)
    transcript_faults: dict[str, str] = field(default_factory=dict)


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


def list_session_panes(
    session: str,
    *,
    tmux_socket: Path | None = None,
    runner: TmuxRunner | None = None,
) -> list[Pane]:
    """Enumerate the live panes of one session on one lane's tmux socket."""
    result = run_on_host(
        "",
        ["tmux", "list-panes", "-s", "-t", session, "-F", _PANE_FIELDS],
        runner=runner,
        tmux_socket=tmux_socket,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.warning("pane_sensing_list_panes_failed", session=session, stderr=result.stderr.strip())
        return []
    panes: list[Pane] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        pane_id = fields[0] if fields else ""
        target = fields[1] if len(fields) >= 2 else ""
        if not pane_id or not target or pane_id in seen:
            continue
        seen.add(pane_id)
        attached = len(fields) < 3 or fields[2] != "0"
        backend = _pane_backend(fields[3]) if len(fields) >= 4 else "unknown"
        pipe_armed = len(fields) >= 5 and fields[4] == "1"
        panes.append(Pane(pane_id=pane_id, target=target, attached=attached, backend=backend, pipe_armed=pipe_armed))
    return panes


def capture_pane(
    pane: Pane,
    *,
    tmux_socket: Path | None = None,
    runner: TmuxRunner | None = None,
) -> str | None:
    """Capture one pane, returning ``None`` when it vanished or tmux failed."""
    result = run_on_host(
        "",
        ["tmux", "capture-pane", "-p", "-J", "-t", pane.target, "-S", f"-{CAPTURE_LINES}"],
        runner=runner,
        tmux_socket=tmux_socket,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.info("pane_sensing_capture_failed", pane_id=pane.pane_id, stderr=result.stderr.strip())
        return None
    return result.stdout


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


def _session_ref_for(pane: Pane, known_session_refs: Sequence[str]) -> str | None:
    """Match one pane's ``session:window.pane`` target to a known session_ref."""
    suffix = f":{pane.target}"
    matches = [ref for ref in known_session_refs if ref.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning("pane_sensing_ambiguous_session_ref", pane_id=pane.pane_id, target=pane.target, matches=matches)
    return None


def _save_raw_capture(state_dir: Path, pane_id: str, content: str) -> None:
    """Keep the last raw capture per pane for classifier forensics."""
    safe_id = hashlib.sha256(pane_id.encode("utf-8")).hexdigest()
    raw_path = state_dir / "monitord" / f"{safe_id}.raw"
    try:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("pane_sensing_raw_capture_failed", path=str(raw_path), error=str(exc))


def sense_lane_panes(
    lane: LaneSpec,
    *,
    broker: AgentStatusBroker,
    state: PaneSenseState,
    known_session_refs: Sequence[str],
    activity_root: Path,
    transcript_stale_seconds: int = DEFAULT_TRANSCRIPT_STALE_SECONDS,
    alert: PaneAlerter | None = None,
    runner: TmuxRunner | None = None,
    now: datetime | None = None,
) -> int:
    """Run one sensing pass over one lane's tmux session.

    Per pane: classify the capture through the status broker (which also feeds
    the local status socket), refresh the lane-activity facts the rate-limit
    guard's quiescence check reads, and alert on a rate-limit banner or a
    transcript pipe that is armed but writing nowhere. Returns the number of
    alerts raised.
    """
    observed_at = (now or datetime.now(UTC)).isoformat()
    prior_activity = {record.session_ref: record for record in load_lane_activity(activity_root)}
    activity_updates: list[LaneActivity] = []
    emitted = 0
    for pane in list_session_panes(lane.tmux_session, tmux_socket=lane.tmux_socket, runner=runner):
        content = capture_pane(pane, tmux_socket=lane.tmux_socket, runner=runner)
        if content is None:
            continue
        _save_raw_capture(lane.state_dir, pane.pane_id, content)
        session_ref = _session_ref_for(pane, known_session_refs)
        try:
            broker.observe(
                pane_id=pane.pane_id,
                target=pane.target,
                session_ref=session_ref,
                lane_id=lane.identifier,
                detected_agent=pane.backend,
                snapshot=content,
                tmux_socket=lane.tmux_socket,
            )
        except StatusRuntimeError:
            # A live handoff owns the mutation barrier; sensing resumes after it.
            break
        status = next((item for item in broker.statuses() if item.pane_id == pane.pane_id), None)
        if status is None:
            continue
        changed = state.status_revisions.get(pane.pane_id) != status.revision
        state.status_revisions[pane.pane_id] = status.revision
        if session_ref is not None:
            prior = prior_activity.get(session_ref)
            last_change_at = observed_at if changed or prior is None else prior.last_change_at
            activity_updates.append(
                LaneActivity(
                    session_ref=session_ref,
                    pane_id=pane.pane_id,
                    last_change_at=last_change_at,
                    last_seen_at=observed_at,
                    attached=pane.attached,
                    backend=pane.backend if pane.backend != "unknown" else (prior.backend if prior is not None else "unknown"),
                )
            )
            reason = transcript_pipe_fault(
                lane_directory=lane.state_dir,
                pipe_armed=pane.pipe_armed,
                last_change_at=last_change_at,
                now=now or datetime.now(UTC),
                stale_seconds=transcript_stale_seconds,
            )
            if not reason:
                state.transcript_faults.pop(pane.pane_id, None)
            elif state.transcript_faults.get(pane.pane_id) != reason:
                state.transcript_faults[pane.pane_id] = reason
                logger.warning(
                    "monitord_transcript_pipe_stale",
                    session_ref=session_ref,
                    pane_id=pane.pane_id,
                    transcript=str(lane.state_dir / TRANSCRIPT_NAME),
                    pipe_armed=pane.pipe_armed,
                    reason=reason,
                )
                if alert is not None:
                    alert(
                        session_ref,
                        f"transcript pipe fault on pane {pane.target}: {reason}",
                    )
                    emitted += 1
        if changed and session_ref is not None and status.state == "rate_limited_hard":
            resume = f" The banner names a resume time: {status.explain.resume_at}." if status.explain.resume_at else ""
            logger.warning(
                "monitord_rate_limit_banner",
                session_ref=session_ref,
                pane_id=pane.pane_id,
                state=status.state,
                resume_at=status.explain.resume_at,
            )
            if alert is not None:
                alert(
                    session_ref,
                    f"lane shows a hard rate-limit banner; the lane cannot produce work until the window passes.{resume}",
                )
                emitted += 1
        elif changed and status.state in RATE_LIMITED_STATES:
            logger.info(
                "monitord_rate_limit_warn",
                session_ref=session_ref,
                pane_id=pane.pane_id,
                state=status.state,
            )
    upsert_lane_activity(activity_root, activity_updates)
    return emitted

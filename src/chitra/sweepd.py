"""sweepd -- deterministic, delta-only fleet state digest producer.

The monitor consumes ``sweep-digest.json`` once per sweep instead of rebuilding
fleet state in its foreground context.  This daemon reads only already-digested
state: goals, rate-limit transactions, account registry entries, and a bounded
tail of triaged flags.  It never reads conversation transcripts or invokes
an LLM.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from chitra._fsio import env_path, write_json_atomic
from chitra.account_registry import RegistryEntry, load_registry
from chitra.goals import (
    GOALS_SCHEMA_NEWER_MESSAGE,
    LOAD_SHED_HOLD_REASON_PREFIX,
    GoalRecord,
    GoalStatus,
    check_specification,
    due_goals,
    goals_schema_newer_than_installed,
    list_goals,
    session_host,
    session_name,
)
from chitra.goals import (
    SCHEMA as GOALS_INSTALLED_SCHEMA,
)
from chitra.lane_config import enabled_lanes
from chitra.rate_limit_state import Transaction, TransactionPhase, load_load_states, load_transactions
from chitra.state_paths import state_dir as default_state_dir
from chitra.systemd_notify import notify_ready, notify_watchdog

logger = structlog.get_logger(__name__)

DEFAULT_POLL_SECONDS = 60.0
DEFAULT_FLAG_TAIL_BYTES = 32 * 1024
DEFAULT_FLAG_TAIL_LINES = 100
DIGEST_FILENAME = "sweep-digest.json"
SNAPSHOT_FILENAME = "sweep-digest-state.json"
FLAGS_FILENAME = "flags.log"
FLAG_SEVERITIES: frozenset[str] = frozenset({"CRIT", "IDLE"})

# Roots whose newer-than-installed goals.json has already been journaled; a
# long-running daemon notices once instead of writing the same warning into
# the journal on every sweep.
_SCHEMA_NOTICED_ROOTS: set[str] = set()


def note_goals_schema_state(state_dir: Path) -> None:
    """Journal one read-only notice when this store's file schema is newer.

    The digest reads goal state only, so a newer goals.json keeps sweeping;
    the notice records that this installed package must not write it instead
    of exiting into a supervisor restart loop (the chitra.goals.v4 outage
    class).
    """
    file_schema = goals_schema_newer_than_installed(state_dir)
    if file_schema is None:
        return
    key = str(state_dir)
    if key in _SCHEMA_NOTICED_ROOTS:
        return
    _SCHEMA_NOTICED_ROOTS.add(key)
    print(
        f"{GOALS_SCHEMA_NEWER_MESSAGE} state_dir={key} file_schema={file_schema} "
        f"installed_schema={GOALS_INSTALLED_SCHEMA}"
    )


class FlagRecord(BaseModel):
    """One latest documented flag signal emitted by ``triaged``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: str
    lane_id: str
    rule: str
    message: str

    @property
    def key(self) -> str:
        """Return the durable identity for this lane/rule signal."""
        return f"{self.lane_id}\x1f{self.rule}"


class LaneState(BaseModel):
    """The compact monitor-relevant state for one tracked lane."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_ref: str
    goal_status: GoalStatus | None
    due: bool
    hold_reason: str
    resume_at: str
    rate_limit_phase: TransactionPhase | None
    rate_limit_escalated: bool
    rate_limit_attempts: int
    load_level: int
    load_shed: bool
    account: str
    account_updated_at: str
    pending_decisions: tuple[str, ...]
    needs: str
    specification_failures: tuple[str, ...]


class SweepSnapshot(BaseModel):
    """Full private baseline used to calculate the next public delta."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal["chitra.sweep-snapshot.v1"] = Field(default="chitra.sweep-snapshot.v1", alias="schema")
    lanes: dict[str, LaneState] = Field(default_factory=dict)
    flags: dict[str, FlagRecord] = Field(default_factory=dict)
    load_level: dict[str, int] = Field(default_factory=dict)
    shed_lanes: tuple[str, ...] = ()


class LaneChange(BaseModel):
    """A lane whose compact state is new or changed since the prior sweep."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change: Literal["new", "changed"]
    lane: LaneState


class DisappearedLane(BaseModel):
    """A previously tracked lane that no longer exists in the current state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change: Literal["disappeared"] = "disappeared"
    session_ref: str


class FlagChange(BaseModel):
    """A newly observed or changed latest CRIT signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change: Literal["new", "changed"]
    flag: FlagRecord


class SweepDigest(BaseModel):
    """Small public payload consumed by the foreground monitor once per sweep."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal["chitra.sweep-digest.v1"] = Field(default="chitra.sweep-digest.v1", alias="schema")
    generated_at: str
    changed_lanes: tuple[LaneChange, ...]
    disappeared_lanes: tuple[DisappearedLane, ...]
    unchanged_lane_count: int
    changed_flags: tuple[FlagChange, ...]
    unchanged_flag_count: int
    total_lane_count: int
    due_goal_count: int
    pending_decision_count: int
    specification_failure_count: int
    load_level: dict[str, int]
    shed_lanes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SweepdConfig:
    """All filesystem paths and timing required by a sweepd process."""

    state_dir: Path
    digest_path: Path
    snapshot_path: Path
    flags_path: Path
    poll_seconds: float


def _env_positive_float(name: str, default: float) -> float:
    """Resolve a positive interval from the environment or use ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def resolve_config(
    *,
    state_dir: Path | None = None,
    digest_path: Path | None = None,
    snapshot_path: Path | None = None,
    flags_path: Path | None = None,
    poll_seconds: float | None = None,
) -> SweepdConfig:
    """Resolve CLI arguments, then explicit environment overrides, then defaults."""
    resolved_state_dir = state_dir or default_state_dir()
    resolved_digest_path = digest_path or env_path("CHITRA_SWEEP_DIGEST_PATH", resolved_state_dir / DIGEST_FILENAME)
    resolved_snapshot_path = snapshot_path or env_path(
        "CHITRA_SWEEP_SNAPSHOT_PATH", resolved_state_dir / SNAPSHOT_FILENAME
    )
    resolved_flags_path = flags_path or env_path("CHITRA_SWEEP_FLAGS_PATH", resolved_state_dir / FLAGS_FILENAME)
    resolved_poll_seconds = poll_seconds if poll_seconds is not None else _env_positive_float(
        "CHITRA_SWEEP_POLL_SECONDS", DEFAULT_POLL_SECONDS
    )
    if resolved_poll_seconds <= 0:
        raise ValueError("poll_seconds must be a positive number")
    return SweepdConfig(
        state_dir=resolved_state_dir,
        digest_path=resolved_digest_path,
        snapshot_path=resolved_snapshot_path,
        flags_path=resolved_flags_path,
        poll_seconds=resolved_poll_seconds,
    )


def _index_goals(records: list[GoalRecord]) -> dict[str, GoalRecord]:
    """Index goals by their durable session reference, rejecting corruption."""
    indexed: dict[str, GoalRecord] = {}
    for record in records:
        if record.session_ref in indexed:
            raise ValueError(f"goals.json contains duplicate session_ref: {record.session_ref}")
        indexed[record.session_ref] = record
    return indexed


def _index_transactions(records: list[Transaction]) -> dict[str, Transaction]:
    """Index rate-limit transactions by lane, rejecting duplicate in-flight rows."""
    indexed: dict[str, Transaction] = {}
    for record in records:
        if record.session_ref in indexed:
            raise ValueError(f"rate_limit_state.json contains duplicate session_ref: {record.session_ref}")
        indexed[record.session_ref] = record
    return indexed


def _index_registry(records: list[RegistryEntry]) -> dict[str, RegistryEntry]:
    """Index account identities by tmux session, rejecting ambiguous ownership."""
    indexed: dict[str, RegistryEntry] = {}
    for record in records:
        if record.tmux_session in indexed:
            raise ValueError(f"account_registry.json contains duplicate tmux_session: {record.tmux_session}")
        indexed[record.tmux_session] = record
    return indexed


def _read_tail(path: Path, *, max_bytes: int) -> str:
    """Read only the final complete records of an append-only text file."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            start = max(size - max_bytes, 0)
            starts_mid_record = False
            if start:
                source.seek(start - 1)
                starts_mid_record = source.read(1) != b"\n"
            source.seek(start)
            payload = source.read()
    except FileNotFoundError:
        return ""
    if starts_mid_record:
        separator = payload.find(b"\n")
        if separator == -1:
            raise ValueError(f"{path} has no complete flag record in the bounded read window")
        payload = payload[separator + 1 :]
    return payload.decode("utf-8")


def parse_flag_line(line: str) -> FlagRecord:
    """Parse one strict ``triaged`` flag line with a documented severity."""
    stripped = line.strip()
    severity, separator, remainder = stripped.partition(" ")
    if severity not in FLAG_SEVERITIES or not separator:
        allowed = ", ".join(sorted(FLAG_SEVERITIES))
        raise ValueError(f"flags.log line must begin with a documented severity ({allowed}): {line!r}")
    timestamp, separator, remainder = remainder.partition(" ")
    if not timestamp or not separator:
        raise ValueError(f"flags.log line has no timestamp/lane: {line!r}")
    lane_id, separator, remainder = remainder.partition(" ")
    if not lane_id or not separator:
        raise ValueError(f"flags.log line has no lane/rule: {line!r}")
    rule, separator, message = remainder.partition(":")
    if not rule or not separator or not message.strip():
        raise ValueError(f"flags.log line has malformed rule/message: {line!r}")
    return FlagRecord(timestamp=timestamp, lane_id=lane_id, rule=rule, message=message.strip())


def load_latest_flags(
    path: Path,
    *,
    max_bytes: int = DEFAULT_FLAG_TAIL_BYTES,
    max_lines: int = DEFAULT_FLAG_TAIL_LINES,
) -> dict[str, FlagRecord]:
    """Return one newest flag signal per ``lane_id``/rule from a bounded tail."""
    if max_lines <= 0:
        raise ValueError("max_lines must be positive")
    payload = _read_tail(path, max_bytes=max_bytes)
    latest: dict[str, FlagRecord] = {}
    for line in payload.splitlines()[-max_lines:]:
        if not line.strip():
            continue
        flag = parse_flag_line(line)
        latest[flag.key] = flag
    return dict(sorted(latest.items()))


def _lane_state(
    session_ref: str,
    *,
    goal: GoalRecord | None,
    transaction: Transaction | None,
    registry_entry: RegistryEntry | None,
    due: bool,
    load_level: int,
) -> LaneState:
    """Combine canonical state records for a tracked lane without inference."""
    return LaneState(
        session_ref=session_ref,
        goal_status=None if goal is None else goal.status,
        due=due,
        hold_reason=("" if goal is None else goal.hold_reason) or ("" if transaction is None else transaction.hold_reason),
        resume_at=("" if goal is None else goal.resume_at) or ("" if transaction is None else transaction.resume_at),
        rate_limit_phase=None if transaction is None else transaction.phase,
        rate_limit_escalated=False if transaction is None else transaction.escalated,
        rate_limit_attempts=0 if transaction is None else transaction.attempts,
        load_level=load_level,
        load_shed=(goal is not None and goal.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX)),
        account="" if registry_entry is None else registry_entry.account,
        account_updated_at="" if registry_entry is None else registry_entry.updated_at,
        pending_decisions=() if goal is None else goal.open_asks,
        needs="" if goal is None else goal.needs,
        specification_failures=() if goal is None else tuple(check_specification(goal)),
    )


def build_snapshot(
    state_dir: Path,
    *,
    flags_path: Path | None = None,
    now: datetime | None = None,
) -> SweepSnapshot:
    """Build the full current baseline from existing compact state stores."""
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    # Sweepd publishes a read-only digest. It may display fields understood by
    # this package from a newer store, but it never uses this opt-in to write.
    goals = _index_goals(list_goals(state_dir, allow_newer=True))
    transactions = _index_transactions(load_transactions(state_dir))
    registry = _index_registry(load_registry(state_dir))
    load_states = {state.host: state for state in load_load_states(state_dir)}
    load_levels = {host: state.load_level for host, state in sorted(load_states.items())}
    shed_lanes = tuple(session_ref for host in sorted(load_states) for session_ref in load_states[host].shed_lanes)
    due_refs = {record.session_ref for record in due_goals(state_dir, now=current, allow_newer=True)}
    lanes: dict[str, LaneState] = {}
    for session_ref in sorted(set(goals) | set(transactions)):
        lanes[session_ref] = _lane_state(
            session_ref,
            goal=goals.get(session_ref),
            transaction=transactions.get(session_ref),
            registry_entry=registry.get(session_name(session_ref)),
            due=session_ref in due_refs,
            load_level=load_levels.get(session_host(session_ref), 0),
        )
    return SweepSnapshot(
        lanes=lanes,
        flags=load_latest_flags(flags_path or state_dir / FLAGS_FILENAME),
        load_level=load_levels,
        shed_lanes=shed_lanes,
    )


def load_snapshot(path: Path) -> SweepSnapshot:
    """Load the prior full baseline; a first sweep starts from an empty baseline."""
    try:
        return SweepSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SweepSnapshot()


def _write_model(path: Path, model: BaseModel) -> None:
    """Atomically persist one typed JSON document."""
    write_json_atomic(
        path,
        model.model_dump(mode="json", by_alias=True),
        temporary_path=path.with_name(f".{path.name}.tmp"),
        cleanup_on_error=False,
    )


def save_snapshot(path: Path, snapshot: SweepSnapshot) -> None:
    """Persist the full baseline used by the next delta calculation."""
    _write_model(path, snapshot)


def save_digest(path: Path, digest: SweepDigest) -> None:
    """Persist the compact public delta consumed by the monitor."""
    _write_model(path, digest)


def _utc_timestamp(now: datetime) -> str:
    """Format an explicit UTC timestamp for the public digest."""
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def compute_delta(previous: SweepSnapshot, current: SweepSnapshot, *, now: datetime) -> SweepDigest:
    """Return only changed current state, with unchanged lane/flag counts."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    changed_lanes: list[LaneChange] = []
    unchanged_lane_count = 0
    for session_ref, lane in current.lanes.items():
        prior_lane = previous.lanes.get(session_ref)
        if prior_lane is None:
            changed_lanes.append(LaneChange(change="new", lane=lane))
        elif prior_lane == lane:
            unchanged_lane_count += 1
        else:
            changed_lanes.append(LaneChange(change="changed", lane=lane))
    disappeared_lanes = tuple(
        DisappearedLane(session_ref=session_ref)
        for session_ref in sorted(set(previous.lanes) - set(current.lanes))
    )

    changed_flags: list[FlagChange] = []
    unchanged_flag_count = 0
    for key, flag in current.flags.items():
        prior_flag = previous.flags.get(key)
        if prior_flag is None:
            changed_flags.append(FlagChange(change="new", flag=flag))
        elif prior_flag == flag:
            unchanged_flag_count += 1
        else:
            changed_flags.append(FlagChange(change="changed", flag=flag))

    return SweepDigest(
        generated_at=_utc_timestamp(now),
        changed_lanes=tuple(changed_lanes),
        disappeared_lanes=disappeared_lanes,
        unchanged_lane_count=unchanged_lane_count,
        changed_flags=tuple(changed_flags),
        unchanged_flag_count=unchanged_flag_count,
        total_lane_count=len(current.lanes),
        due_goal_count=sum(lane.due for lane in current.lanes.values()),
        pending_decision_count=sum(len(lane.pending_decisions) for lane in current.lanes.values()),
        specification_failure_count=sum(len(lane.specification_failures) for lane in current.lanes.values()),
        load_level=current.load_level,
        shed_lanes=current.shed_lanes,
    )


def run_once(config: SweepdConfig, *, now: datetime | None = None) -> SweepDigest:
    """Generate, publish, and then persist one delta baseline transaction."""
    current_now = datetime.now(UTC) if now is None else now
    note_goals_schema_state(config.state_dir)
    previous = load_snapshot(config.snapshot_path)
    current = build_snapshot(config.state_dir, flags_path=config.flags_path, now=current_now)
    digest = compute_delta(previous, current, now=current_now)
    save_digest(config.digest_path, digest)
    save_snapshot(config.snapshot_path, current)
    logger.info(
        "sweep_digest_written",
        digest_path=str(config.digest_path),
        changed_lanes=len(digest.changed_lanes),
        disappeared_lanes=len(digest.disappeared_lanes),
        unchanged_lanes=digest.unchanged_lane_count,
        changed_flags=len(digest.changed_flags),
    )
    return digest


def run_forever(config: SweepdConfig, *, stop_event: threading.Event | None = None) -> None:
    """Run deterministic sweep digestion until a service signal stops the process."""
    active_stop_event = stop_event or threading.Event()
    logger.info(
        "sweepd_started",
        state_dir=str(config.state_dir),
        digest_path=str(config.digest_path),
        poll_seconds=config.poll_seconds,
    )
    notify_ready()
    while not active_stop_event.is_set():
        run_once(config)
        notify_watchdog()
        active_stop_event.wait(config.poll_seconds)


def run_lanes_once(lanes_file: Path | None) -> dict[str, SweepDigest]:
    """Build one digest per enabled lane from the shared lane declaration."""
    results: dict[str, SweepDigest] = {}
    for lane in enabled_lanes(lanes_file):
        results[lane.identifier] = run_once(
            SweepdConfig(
                state_dir=lane.state_dir,
                digest_path=lane.sweep_digest_path,
                snapshot_path=lane.sweep_snapshot_path,
                flags_path=lane.flags_path,
                poll_seconds=DEFAULT_POLL_SECONDS,
            )
        )
    return results


def run_lanes_forever(lanes_file: Path | None, *, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
    """Run one shared sweep process over every enabled lane state root."""
    stop_event = threading.Event()
    notify_ready()
    while not stop_event.is_set():
        run_lanes_once(lanes_file)
        notify_watchdog()
        stop_event.wait(poll_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the intentionally small daemon CLI."""
    parser = argparse.ArgumentParser(prog="chitra-sweepd", description="Produce compact Chitra fleet-state deltas.")
    parser.add_argument("--lanes-file", type=Path, default=None, help="Rendered lane declaration; when set, sweep every enabled lane.")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--digest-path", type=Path, default=None)
    parser.add_argument("--snapshot-path", type=Path, default=None)
    parser.add_argument("--flags-path", type=Path, default=None)
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--once", action="store_true", help="Write one digest and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    warnings.warn(
        "sweepd is deprecated by chitra-monitord and will be removed "
        "in a future release; declare one monitord instance instead",
        DeprecationWarning,
        stacklevel=2,
    )
    """Run the daemon; malformed persisted input deliberately terminates it."""
    args = build_arg_parser().parse_args(argv)
    if args.lanes_file is not None:
        if args.once:
            print(json.dumps(
                {
                    key: value.model_dump(mode="json", by_alias=True)
                    for key, value in run_lanes_once(args.lanes_file).items()
                }, indent=2))
            return 0
        poll_seconds = args.poll_seconds if args.poll_seconds is not None else DEFAULT_POLL_SECONDS
        if poll_seconds <= 0:
            raise SystemExit("--poll-seconds must be positive")
        run_lanes_forever(args.lanes_file, poll_seconds=poll_seconds)
        return 0
    config = resolve_config(
        state_dir=args.state_dir,
        digest_path=args.digest_path,
        snapshot_path=args.snapshot_path,
        flags_path=args.flags_path,
        poll_seconds=args.poll_seconds,
    )
    if args.once:
        digest = run_once(config)
        print(digest.model_dump_json(indent=2, by_alias=True))
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

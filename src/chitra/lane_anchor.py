"""Launch and inspect governed, interactive tmux lanes on Tophand."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from chitra._fsio import write_json_atomic
from chitra.goals import RATE_LIMIT_HOLD_REASON_PREFIX, GoalRecord, check_specification, get_goal
from chitra.lane_config import LaneSpec, load_lanes
from chitra.rate_limit_state import get_transaction

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
SANCTIONED_HOST = "tophand"
CLAUDE_MODELS = ("sonnet", "opus")
BACKENDS = ("claude", "codex")


class LaneLaunchRefused(RuntimeError):
    """A mechanical launch gate refused an unsafe lane start."""


def session_ref(lane: LaneSpec, host: str = SANCTIONED_HOST) -> str:
    return f"{host}:{lane.tmux_session}:0.0"


def ingestion_gate(lane: LaneSpec, *, host: str = SANCTIONED_HOST) -> GoalRecord:
    """Return the frozen goal source only when every launch gate passes."""
    if host != SANCTIONED_HOST:
        raise LaneLaunchRefused("lane launch refused: governed lanes must run on tophand")
    ref = session_ref(lane, host)
    try:
        goal = get_goal(lane.state_dir, ref)
    except (OSError, ValueError) as exc:
        raise LaneLaunchRefused(f"lane launch refused: goal state is UNKNOWN: {exc}") from exc
    if goal is None:
        raise LaneLaunchRefused("lane launch refused: no chitra-goals ingestion record")
    issues = check_specification(goal)
    if issues:
        raise LaneLaunchRefused("lane launch refused: ingestion gate failed: " + "; ".join(issues))
    if goal.lane_id != lane.tmux_session or goal.enrolled_done_when != goal.done_when:
        raise LaneLaunchRefused("lane launch refused: lane identity or frozen done_when snapshot does not match")
    if goal.status == "held" or goal.hold_reason.startswith(RATE_LIMIT_HOLD_REASON_PREFIX):
        raise LaneLaunchRefused("lane launch refused: usage-pause hold is active")
    try:
        transaction = get_transaction(lane.state_dir, ref)
    except (OSError, ValueError) as exc:
        raise LaneLaunchRefused(f"lane launch refused: usage-pause state is UNKNOWN: {exc}") from exc
    if transaction is not None:
        raise LaneLaunchRefused(f"lane launch refused: usage-pause lifecycle is active ({transaction.phase})")
    return goal


def _agent_command(backend: str, model: str | None) -> list[str]:
    if backend == "claude":
        if model not in CLAUDE_MODELS:
            raise LaneLaunchRefused("lane launch refused: Claude model must be sonnet or opus")
        return ["claude", "--model", model]
    if backend == "codex":
        if model:
            return ["codex", "--model", model]
        return ["codex"]
    raise LaneLaunchRefused(f"lane launch refused: unsupported backend {backend!r}")


def _write_launch_receipt(lane: LaneSpec, goal: GoalRecord, *, backend: str, model: str | None) -> None:
    snapshot = {name: getattr(goal, name) for name in ("goal", "done_when", "intent", "scope", "source")}
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    payload = {
        "schema": "chitra.lane-launch.v1",
        "lane_id": goal.lane_id,
        "session_ref": goal.session_ref,
        "goal_version": goal.goal_version,
        "enrolled_at": goal.enrolled_at,
        "goal_snapshot": snapshot,
        "goal_snapshot_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "backend": backend,
        "model": model or "backend-default",
        "lifecycle": ["dispatchd", "watchd", "completion_gate", "goal_enforcement", "draft_scanner", "rate_limit_guard"],
    }
    path = lane.state_dir / "lane-launch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, payload)


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _run_as_lane(lane: LaneSpec, command: Sequence[str]) -> list[str]:
    environment = [
        f"HOME={lane.home}",
        f"CLAUDE_CONFIG_DIR={lane.config_dir}",
    ]
    return ["runuser", "--user", lane.account, "--", "env", *environment, *command]


def _tmux(lane: LaneSpec, *arguments: str) -> list[str]:
    return ["tmux", "-S", str(lane.tmux_socket), *arguments]


def start_lane(
    lane: LaneSpec,
    *,
    backend: str = "claude",
    model: str | None = "sonnet",
    host: str = SANCTIONED_HOST,
    runner: CommandRunner = _run,
) -> bool:
    """Ensure the lane session exists; return whether this call created it."""
    goal = ingestion_gate(lane, host=host)
    agent_command = _agent_command(backend, model)
    existing = runner(_run_as_lane(lane, _tmux(lane, "has-session", "-t", lane.tmux_session)))
    if existing.returncode == 0:
        return False
    command = _run_as_lane(
        lane,
        _tmux(
            lane,
            "new-session",
            "-d",
            "-s",
            lane.tmux_session,
            "-c",
            str(lane.workdir),
            *agent_command,
        ),
    )
    result = runner(command)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"tmux exited {result.returncode}"
        raise RuntimeError(f"could not create lane {lane.identifier} session: {detail}")
    _write_launch_receipt(lane, goal, backend=backend, model=model)
    return True


def stop_lane(lane: LaneSpec, *, runner: CommandRunner = _run) -> bool:
    """Stop the declared session when an operator explicitly stops the unit."""
    result = runner(_run_as_lane(lane, _tmux(lane, "kill-session", "-t", lane.tmux_session)))
    if result.returncode == 0:
        return True
    if "session not found" in (result.stderr or "").lower():
        return False
    detail = result.stderr.strip() or result.stdout.strip() or f"tmux exited {result.returncode}"
    raise RuntimeError(f"could not stop lane {lane.identifier} session: {detail}")


def lane_for_identifier(lanes: Sequence[LaneSpec], identifier: str) -> LaneSpec:
    for lane in lanes:
        if lane.identifier == identifier:
            return lane
    raise ValueError(f"lane is not declared: {identifier}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chitra-lane-anchor")
    parser.add_argument("--lanes-file", type=Path, default=None)
    parser.add_argument("--lane", required=True, dest="identifier")
    parser.add_argument("--host", default=SANCTIONED_HOST)
    parser.add_argument("--backend", choices=BACKENDS, default="claude")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("action", choices=("start", "stop", "status"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    lane = lane_for_identifier(load_lanes(args.lanes_file), args.identifier)
    if not lane.enabled and args.action == "start":
        raise SystemExit(f"lane is declared disabled: {lane.identifier}")
    if args.action == "start":
        start_lane(lane, backend=args.backend, model=args.model, host=args.host)
        return 0
    if args.action == "stop":
        stop_lane(lane)
        return 0
    result = _run(_run_as_lane(lane, _tmux(lane, "has-session", "-t", lane.tmux_session)))
    if result.returncode == 0:
        print("active")
        return 0
    combined = (result.stderr + result.stdout).lower()
    if result.returncode == 1 and ("session not found" in combined or "no server running" in combined):
        print("inactive")
        return 1
    print("UNKNOWN: tmux session state could not be verified")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

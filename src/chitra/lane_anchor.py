"""Launch and inspect governed, interactive tmux lanes on Tophand."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from chitra._fsio import write_json_atomic
from chitra.goals import RATE_LIMIT_HOLD_REASON_PREFIX, GoalRecord, check_specification, get_goal
from chitra.lane_config import LaneSpec, load_lanes
from chitra.rate_limit_state import get_transaction
from chitra.socket_api import default_socket_path

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
SANCTIONED_HOST = "tophand"
SANCTIONED_HOSTS = frozenset({"tophand", "trinity"})
# The name chitra.watchd's transcript-pipe liveness check looks for. The two
# must agree: the watcher reads this file to decide whether a governed lane is
# still being recorded.
TRANSCRIPT_NAME = "tmux-transcript.log"
CLAUDE_MODELS = ("sonnet", "opus")
BACKENDS = ("claude", "codex")
CLAUDE_EFFORTS = ("low", "medium", "high", "max")
CODEX_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
LANE_ID_ENV_VAR = "CHITRA_LANE_ID"
SESSION_REF_ENV_VAR = "CHITRA_SESSION_REF"
PANE_ID_ENV_VAR = "CHITRA_PANE_ID"
PANE_TARGET_ENV_VAR = "CHITRA_PANE_TARGET"
SOCKET_PATH_ENV_VAR = "CHITRA_SOCKET_PATH"
PYTHONPATH_ENV_VAR = "PYTHONPATH"
STARTUP_SURVIVAL_CHECKS = 5
STARTUP_SURVIVAL_INTERVAL_SECONDS = 0.5
TEMPORARY_FAILURE_EXIT = 75


class LaneLaunchRefused(RuntimeError):
    """A mechanical launch gate refused an unsafe lane start."""


class LaneStartupFailed(RuntimeError):
    """The agent process exited before its lane session became durable."""


def session_ref(lane: LaneSpec, host: str = SANCTIONED_HOST) -> str:
    return f"{host}:{lane.tmux_session}:0.0"


def ingestion_gate(lane: LaneSpec, *, host: str = SANCTIONED_HOST) -> GoalRecord:
    """Return the frozen goal source only when every launch gate passes."""
    if host not in SANCTIONED_HOSTS:
        raise LaneLaunchRefused("lane launch refused: governed lanes must run on tophand or trinity")
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


def _agent_command(backend: str, model: str | None, effort: str | None) -> list[str]:
    if backend == "claude":
        if model not in CLAUDE_MODELS:
            raise LaneLaunchRefused("lane launch refused: Claude model must be sonnet or opus")
        if effort not in CLAUDE_EFFORTS:
            raise LaneLaunchRefused(
                "lane launch refused: Claude effort must be low, medium, high, or max"
            )
        return ["claude", "--model", model, "--effort", effort]
    if backend == "codex":
        if effort not in CODEX_EFFORTS:
            raise LaneLaunchRefused("lane launch refused: unsupported Codex effort")
        command = ["codex"]
        if model:
            command.extend(["--model", model])
        if effort != "none":
            command.extend(["--config", f'model_reasoning_effort="{effort}"'])
        return command
    raise LaneLaunchRefused(f"lane launch refused: unsupported backend {backend!r}")


def _write_launch_receipt(
    lane: LaneSpec,
    goal: GoalRecord,
    *,
    backend: str,
    model: str | None,
    effort: str | None,
    socket_path: Path,
) -> None:
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
        "effort": effort or "backend-default",
        "identity_env": {
            LANE_ID_ENV_VAR: lane.identifier,
            SESSION_REF_ENV_VAR: goal.session_ref,
            PANE_ID_ENV_VAR: "runtime:TMUX_PANE",
            PANE_TARGET_ENV_VAR: f"{lane.tmux_session}:0.0",
            SOCKET_PATH_ENV_VAR: str(socket_path),
        },
        "lifecycle": ["dispatchd", "watchd", "completion_gate", "goal_enforcement", "draft_scanner", "rate_limit_guard"],
    }
    path = lane.state_dir / "lane-launch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, payload)


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _run_as_lane(lane: LaneSpec, command: Sequence[str], *, extra_environment: Sequence[str] = ()) -> list[str]:
    environment = [
        f"HOME={lane.home}",
        f"CLAUDE_CONFIG_DIR={lane.config_dir}",
        *extra_environment,
    ]
    if os.geteuid() == lane.uid:
        return ["env", *environment, *command]
    return ["runuser", "--user", lane.account, "--", "env", *environment, *command]


def _tmux(lane: LaneSpec, *arguments: str) -> list[str]:
    return ["tmux", "-S", str(lane.tmux_socket), *arguments]


def _session_absent(result: subprocess.CompletedProcess[str]) -> bool:
    combined = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return result.returncode == 1 and any(
        marker in combined
        for marker in ("can't find session", "session not found", "no server running")
    )


def _require_startup_survival(lane: LaneSpec, *, runner: CommandRunner) -> None:
    probe = _run_as_lane(lane, _tmux(lane, "has-session", "-t", lane.tmux_session))
    for check in range(STARTUP_SURVIVAL_CHECKS):
        result = runner(probe)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"tmux exited {result.returncode}"
            if _session_absent(result):
                raise LaneStartupFailed(
                    f"lane {lane.identifier} session exited during startup; "
                    "no launch receipt was written; retry is safe"
                )
            raise RuntimeError(f"could not verify lane {lane.identifier} startup: {detail}")
        if check + 1 < STARTUP_SURVIVAL_CHECKS:
            time.sleep(STARTUP_SURVIVAL_INTERVAL_SECONDS)


def _pane_pythonpath() -> str:
    """Keep the launched pane on the same Chitra runtime as this process."""
    package_root = str(Path(__file__).resolve().parent.parent)
    inherited = os.environ.get(PYTHONPATH_ENV_VAR, "")
    return f"{package_root}{os.pathsep}{inherited}" if inherited else package_root


def lane_transcript_path(lane: LaneSpec) -> Path:
    """Return where this lane's tmux transcript is recorded."""
    return lane.state_dir / TRANSCRIPT_NAME


def arm_transcript_pipe(lane: LaneSpec, *, runner: CommandRunner = _run) -> None:
    """Point pipe-pane at this lane's transcript, safely on every call.

    ``pipe-pane -o`` opens a pipe only when the pane has none, so calling this
    on an already-piped lane is a no-op rather than a second writer.

    This runs on the respawn path as well as at creation. Measured 2026-08-16,
    tophand:atlas-v5 was running with a launch receipt, no transcript file, and
    tmux reporting no pipe for its pane -- so its output went unrecorded from
    2026-08-15 13:15Z onward and nothing that reads that file noticed for
    twenty-five hours. Arming only at creation is what let that happen.
    """
    transcript = lane_transcript_path(lane)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    target = f"{lane.tmux_session}:0.0"
    result = runner(
        _run_as_lane(
            lane,
            _tmux(lane, "pipe-pane", "-o", "-t", target, f"cat >> {shlex.quote(str(transcript))}"),
        )
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"tmux exited {result.returncode}"
        raise RuntimeError(f"could not arm the transcript pipe for lane {lane.identifier}: {detail}")


def start_lane(
    lane: LaneSpec,
    *,
    backend: str = "claude",
    model: str | None = "sonnet",
    effort: str | None = "high",
    host: str = SANCTIONED_HOST,
    socket_path: Path | None = None,
    runner: CommandRunner = _run,
) -> bool:
    """Ensure the lane session exists; return whether this call created it."""
    goal = ingestion_gate(lane, host=host)
    agent_command = _agent_command(backend, model, effort)
    control_socket = socket_path or default_socket_path()
    existing = runner(_run_as_lane(lane, _tmux(lane, "has-session", "-t", lane.tmux_session)))
    if existing.returncode == 0:
        # A lane that is already up still gets its pipe re-armed. This early
        # return is the respawn path: the session survives, and before this it
        # returned here having done nothing, so a lane that came back without
        # its pipe stayed unrecorded until somebody looked.
        arm_transcript_pipe(lane, runner=runner)
        return False
    identity_environment = (
        f"{LANE_ID_ENV_VAR}={lane.identifier}",
        f"{SESSION_REF_ENV_VAR}={goal.session_ref}",
        f"{PANE_TARGET_ENV_VAR}={lane.tmux_session}:0.0",
        f"{SOCKET_PATH_ENV_VAR}={control_socket}",
    )
    pane_environment = (*identity_environment, f"{PYTHONPATH_ENV_VAR}={_pane_pythonpath()}")
    tmux_environment = tuple(
        argument
        for assignment in pane_environment
        for argument in ("-e", assignment)
    )
    supervised_command = [sys.executable, "-m", "chitra.pane_exec", "--", *agent_command]
    command = _run_as_lane(
        lane,
        _tmux(
            lane,
            "new-session",
            "-d",
            *tmux_environment,
            "-s",
            lane.tmux_session,
            "-c",
            str(lane.workdir),
            *supervised_command,
        ),
    )
    result = runner(command)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"tmux exited {result.returncode}"
        raise RuntimeError(f"could not create lane {lane.identifier} session: {detail}")
    _require_startup_survival(lane, runner=runner)
    arm_transcript_pipe(lane, runner=runner)
    _write_launch_receipt(
        lane,
        goal,
        backend=backend,
        model=model,
        effort=effort,
        socket_path=control_socket,
    )
    return True


def stop_lane(lane: LaneSpec, *, runner: CommandRunner = _run) -> bool:
    """Stop the declared session when an operator explicitly stops the unit."""
    result = runner(_run_as_lane(lane, _tmux(lane, "kill-session", "-t", lane.tmux_session)))
    if result.returncode == 0:
        return True
    if _session_absent(result):
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
    parser.add_argument("--effort", default="high")
    parser.add_argument("--socket-path", type=Path, default=None)
    parser.add_argument("action", choices=("start", "stop", "status"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    lane = lane_for_identifier(load_lanes(args.lanes_file), args.identifier)
    if not lane.enabled and args.action == "start":
        raise SystemExit(f"lane is declared disabled: {lane.identifier}")
    if args.action == "start":
        try:
            start_lane(
                lane,
                backend=args.backend,
                model=args.model,
                effort=args.effort,
                host=args.host,
                socket_path=args.socket_path,
            )
        except LaneStartupFailed as exc:
            print(f"LaneStartupFailed: {exc}", file=sys.stderr)
            return TEMPORARY_FAILURE_EXIT
        return 0
    if args.action == "stop":
        stop_lane(lane)
        return 0
    result = _run(_run_as_lane(lane, _tmux(lane, "has-session", "-t", lane.tmux_session)))
    if result.returncode == 0:
        print("active")
        return 0
    if _session_absent(result):
        print("inactive")
        return 1
    print("UNKNOWN: tmux session state could not be verified")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Create and inspect one lane's tmux session anchor.

The anchor opens a shell only.  It never selects or starts an agent model.
The operator starts the chosen model inside the session.
"""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from chitra.lane_config import LaneSpec, load_lanes

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


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


def start_lane(lane: LaneSpec, *, runner: CommandRunner = _run) -> bool:
    """Ensure the lane session exists; return whether this call created it."""
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
        ),
    )
    result = runner(command)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"tmux exited {result.returncode}"
        raise RuntimeError(f"could not create lane {lane.identifier} session: {detail}")
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
    parser.add_argument("action", choices=("start", "stop", "status"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    lane = lane_for_identifier(load_lanes(args.lanes_file), args.identifier)
    if not lane.enabled and args.action == "start":
        raise SystemExit(f"lane is declared disabled: {lane.identifier}")
    if args.action == "start":
        start_lane(lane)
        return 0
    if args.action == "stop":
        stop_lane(lane)
        return 0
    result = _run(_run_as_lane(lane, _tmux(lane, "has-session", "-t", lane.tmux_session)))
    print("active" if result.returncode == 0 else "inactive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

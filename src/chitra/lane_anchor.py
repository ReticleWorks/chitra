"""Create and inspect one lane's tmux session anchor.

The default ``start`` action opens a shell only.  The explicit ``launch``
action may start one executable from the fixed backend allowlist in the lane
manifest.  No action accepts an arbitrary command or model from the manifest.
"""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from chitra.lane_config import LANE_BACKENDS, LaneBackend, LaneSpec, load_lanes

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


_BACKEND_COMMANDS: dict[LaneBackend, tuple[str, ...]] = {
    "shell": (),
    "claude": ("claude",),
    "codex": ("codex",),
    "opencode": ("opencode",),
}


def backend_command(lane: LaneSpec) -> tuple[str, ...]:
    """Return the fixed argv for ``lane.backend``.

    The lane manifest carries only the backend enum.  Keeping this mapping in
    code prevents a manifest or queue order from injecting a shell command.
    """
    try:
        return _BACKEND_COMMANDS[lane.backend]
    except KeyError as exc:
        choices = ", ".join(LANE_BACKENDS)
        raise ValueError(f"lane backend must be one of: {choices}") from exc


def _run_as_lane(lane: LaneSpec, command: Sequence[str], *, backend: LaneBackend | None = None) -> list[str]:
    environment = [
        f"HOME={lane.home}",
        f"CLAUDE_CONFIG_DIR={lane.config_dir}",
    ]
    if backend == "opencode":
        # Keep OpenCode's XDG stores inside this lane's declared roots.  The
        # API key is deliberately not placed in the manifest or argv.
        environment.extend(
            [
                f"XDG_CONFIG_HOME={lane.config_dir / 'xdg'}",
                f"XDG_DATA_HOME={lane.state_dir / 'xdg-data'}",
                f"XDG_STATE_HOME={lane.state_dir / 'xdg-state'}",
            ]
        )
    return ["runuser", "--user", lane.account, "--", "env", *environment, *command]


def _tmux(lane: LaneSpec, *arguments: str) -> list[str]:
    return ["tmux", "-S", str(lane.tmux_socket), *arguments]


def _ensure_session(
    lane: LaneSpec,
    *,
    launch_argv: Sequence[str] = (),
    backend: LaneBackend | None = None,
    runner: CommandRunner = _run,
) -> bool:
    """Ensure one lane session exists, optionally with a fixed backend argv."""
    existing = runner(_run_as_lane(lane, _tmux(lane, "has-session", "-t", lane.tmux_session), backend=backend))
    if existing.returncode == 0:
        return False
    new_session = [
        "new-session",
        "-d",
        "-s",
        lane.tmux_session,
        "-c",
        str(lane.workdir),
        *launch_argv,
    ]
    launch_command = _run_as_lane(
        lane,
        _tmux(
            lane,
            *new_session,
        ),
        backend=backend,
    )
    result = runner(launch_command)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"tmux exited {result.returncode}"
        raise RuntimeError(f"could not create lane {lane.identifier} session: {detail}")
    return True


def start_lane(lane: LaneSpec, *, runner: CommandRunner = _run) -> bool:
    """Ensure the lane session exists with its original shell-only behavior."""
    return _ensure_session(lane, runner=runner)


def launch_lane(lane: LaneSpec, *, runner: CommandRunner = _run) -> bool:
    """Ensure a lane session exists with its fixed, allowlisted backend."""
    command = backend_command(lane)
    if not command:
        return start_lane(lane, runner=runner)
    return _ensure_session(lane, launch_argv=command, backend=lane.backend, runner=runner)


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
    parser.add_argument("action", choices=("start", "launch", "stop", "status"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    lane = lane_for_identifier(load_lanes(args.lanes_file), args.identifier)
    if not lane.enabled and args.action in ("start", "launch"):
        raise SystemExit(f"lane is declared disabled: {lane.identifier}")
    if args.action == "start":
        start_lane(lane)
        return 0
    if args.action == "launch":
        launch_lane(lane)
        return 0
    if args.action == "stop":
        stop_lane(lane)
        return 0
    result = _run(_run_as_lane(lane, _tmux(lane, "has-session", "-t", lane.tmux_session)))
    print("active" if result.returncode == 0 else "inactive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

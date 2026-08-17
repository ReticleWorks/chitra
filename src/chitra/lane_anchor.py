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
from chitra.lane_selftest import (
    SELFTEST_ENV_PUBLISH_PREFIX,
    SELFTEST_ENV_SSH_TARGET,
    LanePermissionRefused,
    LaneSelfTestUnavailable,
    SelfTestReport,
    refusal_message,
    run_self_test,
)
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
# A permission defect is not temporary. The governed wrapper retries a lane
# start on TEMPORARY_FAILURE_EXIT, and retrying this would just start the same
# lane in the same mode four more times, so it gets its own code.
LAUNCH_DEFECT_EXIT = 78
# The flags that put a governed lane at full permissions. A lane runs unattended
# in a pane nobody is watching, so a permission prompt or a classifier refusal
# there is a launch defect, not something the lane can answer.
CLAUDE_FULL_PERMISSION_FLAG = "--dangerously-skip-permissions"
CODEX_FULL_ACCESS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
FULL_PERMISSION_FLAGS = {
    "claude": CLAUDE_FULL_PERMISSION_FLAG,
    "codex": CODEX_FULL_ACCESS_FLAG,
}


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
    """Build the agent command a governed lane runs, at full permissions.

    Every other Chitra entrypoint that starts an agent already does this. The
    adapter's chitra, chitra-brief, chitra-artifact-publish and
    chitra-watchd-reviewer all pass --dangerously-skip-permissions, and the
    fleet converge asserts that every lane's settings.json has accepted Bypass
    Permissions mode so none of them stops on the acceptance screen.

    This launcher passed no permission flag at all, so a lane started here fell
    back to whatever permissions.defaultMode its config directory declared. On
    tophand that is "auto", which hands every tool call a lane makes to the
    Claude Code auto-mode classifier to judge one at a time. Measured on tophand
    2026-08-16/17: infra-followup, access-broker and starchamber were each told
    "Denied by auto mode classifier - Blocked by classifier" on their own core
    work -- a package publish, a pull-request write, ssh to Renegade -- and one
    refusal landed on a read-only ``git status``. All three reported those
    refusals to the operator as needing a person. None of them was a policy
    decision.
    """
    if backend == "claude":
        if model not in CLAUDE_MODELS:
            raise LaneLaunchRefused("lane launch refused: Claude model must be sonnet or opus")
        if effort not in CLAUDE_EFFORTS:
            raise LaneLaunchRefused(
                "lane launch refused: Claude effort must be low, medium, high, or max"
            )
        return ["claude", "--model", model, "--effort", effort, CLAUDE_FULL_PERMISSION_FLAG]
    if backend == "codex":
        if effort not in CODEX_EFFORTS:
            raise LaneLaunchRefused("lane launch refused: unsupported Codex effort")
        command = ["codex"]
        if model:
            command.extend(["--model", model])
        if effort != "none":
            command.extend(["--config", f'model_reasoning_effort="{effort}"'])
        command.append(CODEX_FULL_ACCESS_FLAG)
        return command
    raise LaneLaunchRefused(f"lane launch refused: unsupported backend {backend!r}")


def _require_full_permissions(backend: str, agent_command: Sequence[str]) -> None:
    """Refuse to start a lane whose command would leave it asking for approval.

    This is the cheap half of the launch self-test, and it runs before anything
    is created. It catches the exact defect measured on 2026-08-17, where the
    command was built without a permission flag, without spending a model call
    to find out.
    """
    flag = FULL_PERMISSION_FLAGS.get(backend)
    if flag and flag not in agent_command:
        raise LaneLaunchRefused(
            f"lane launch refused: the {backend} command carries no {flag}, so the lane "
            "would run at partial permissions and be refused its own work"
        )


def _write_launch_receipt(
    lane: LaneSpec,
    goal: GoalRecord,
    *,
    backend: str,
    model: str | None,
    effort: str | None,
    socket_path: Path,
    self_test: SelfTestReport | None = None,
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
        # What the launch proved about this lane's permissions, so a reader
        # weeks later can tell a lane that was tested from one that was not.
        "permission_self_test": (self_test.as_dict() if self_test else {"live": False, "passed": False, "detail": "not run"}),
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


def _prove_lane_permissions(
    lane: LaneSpec,
    *,
    backend: str,
    agent_command: Sequence[str],
    enabled: bool,
    ssh_target: str | None,
    publish_prefix: str | None,
    runner: CommandRunner,
    probe_runner: CommandRunner | None,
) -> SelfTestReport:
    """Run the launch self-test and stop the lane if it was refused anything.

    A lane that is refused its own work is worse than a lane that never started:
    it looks healthy, it burns a shift, and it hands the operator blockers that
    are not real. So a refusal stops the session and writes no launch receipt.

    A self-test that could not run is a different answer again, and it is not
    treated as a pass. The launch fails, and it says the lane is unproven rather
    than claiming it was proved good.
    """
    if not enabled:
        return SelfTestReport(
            backend=backend,
            live=False,
            detail="self-test disabled for this launch",
            unprobed=("file_write", "gh_api_write", "fleet_ssh", "package_publish"),
        )
    target = ssh_target or os.environ.get(SELFTEST_ENV_SSH_TARGET) or None
    prefix = publish_prefix or os.environ.get(SELFTEST_ENV_PUBLISH_PREFIX) or None
    try:
        report = run_self_test(
            backend=backend,
            agent_command=agent_command,
            workdir=lane.workdir,
            ssh_target=target,
            publish_prefix=prefix,
            as_lane=lambda command: _run_as_lane(lane, command),
            **({"runner": probe_runner} if probe_runner else {}),
        )
    except LaneSelfTestUnavailable:
        stop_lane(lane, runner=runner)
        raise
    if not report.passed:
        message = refusal_message(lane.identifier, report)
        print(message, file=sys.stderr)
        stop_lane(lane, runner=runner)
        raise LanePermissionRefused(message)
    if report.unprobed:
        print(
            f"lane {lane.identifier} self-test did not probe: {', '.join(report.unprobed)}",
            file=sys.stderr,
        )
    return report


def start_lane(
    lane: LaneSpec,
    *,
    backend: str = "claude",
    model: str | None = "sonnet",
    effort: str | None = "high",
    host: str = SANCTIONED_HOST,
    socket_path: Path | None = None,
    runner: CommandRunner = _run,
    self_test: bool = True,
    self_test_ssh_target: str | None = None,
    self_test_publish_prefix: str | None = None,
    self_test_runner: CommandRunner | None = None,
) -> bool:
    """Ensure the lane session exists; return whether this call created it."""
    goal = ingestion_gate(lane, host=host)
    agent_command = _agent_command(backend, model, effort)
    if self_test:
        # The cheap half of the self-test, before anything is created.
        _require_full_permissions(backend, agent_command)
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
    report = _prove_lane_permissions(
        lane,
        backend=backend,
        agent_command=agent_command,
        enabled=self_test,
        ssh_target=self_test_ssh_target,
        publish_prefix=self_test_publish_prefix,
        runner=runner,
        probe_runner=self_test_runner,
    )
    _write_launch_receipt(
        lane,
        goal,
        backend=backend,
        model=model,
        effort=effort,
        socket_path=control_socket,
        self_test=report,
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
    # Which host the SSH probe reaches, and where the publish probe writes, are
    # site facts rather than properties of this package, so there is no default
    # for either. Without one the class is reported as unprobed rather than
    # counted as a pass it never measured.
    parser.add_argument(
        "--selftest-ssh-target",
        default=None,
        help=(
            "host the launch self-test reaches over SSH, for example user@host; "
            f"falls back to ${SELFTEST_ENV_SSH_TARGET}"
        ),
    )
    parser.add_argument(
        "--selftest-publish-prefix",
        default=None,
        help=(
            "object-store prefix the launch self-test publishes a probe object to, "
            f"for example s3://bucket/path; falls back to ${SELFTEST_ENV_PUBLISH_PREFIX}"
        ),
    )
    parser.add_argument(
        "--no-self-test",
        dest="self_test",
        action="store_false",
        help="skip the launch self-test; the lane's permissions are then unproven",
    )
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
                self_test=args.self_test,
                self_test_ssh_target=args.selftest_ssh_target,
                self_test_publish_prefix=args.selftest_publish_prefix,
            )
        except LaneStartupFailed as exc:
            print(f"LaneStartupFailed: {exc}", file=sys.stderr)
            return TEMPORARY_FAILURE_EXIT
        except LanePermissionRefused:
            # The message is already on stderr in full. Retrying will not help:
            # the lane would come back in the same permission mode and be
            # refused the same work, so this is a hard failure, not a tempfail.
            return LAUNCH_DEFECT_EXIT
        except LaneSelfTestUnavailable as exc:
            print(f"LaneSelfTestUnavailable: {exc}", file=sys.stderr)
            return LAUNCH_DEFECT_EXIT
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

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
from typing import Any, Literal

from chitra._fsio import write_json_atomic
from chitra.dispatch import enqueue_dispatch_order
from chitra.goals import RATE_LIMIT_HOLD_REASON_PREFIX, GoalRecord, check_specification, get_goal, render_done_when_items
from chitra.knowledge import KnowledgeBundle
from chitra.lane_config import LaneSpec, load_lanes
from chitra.lane_selftest import (
    SELFTEST_ENV_SSH_TARGET,
    LanePermissionRefused,
    LaneSelfTestUnavailable,
    SelfTestReport,
    refusal_message,
    run_self_test,
)
from chitra.orders import DispatchOrder
from chitra.queue_state import QueueLayout, locate_order
from chitra.rate_limit_state import get_transaction
from chitra.recovery import (
    LANE_STATES,
    LaneState,
    capture_worktree_binding,
    get_lane_lifecycle,
    transition_lane_lifecycle,
    validate_lane_resume,
)
from chitra.socket_api import default_socket_path
from chitra.supervision import goal_digest

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
SANCTIONED_HOST = "tophand"
SANCTIONED_HOSTS = frozenset({"tophand", "trinity", "twinridge"})
# The name chitra.watchd's transcript-pipe liveness check looks for. The two
# must agree: the watcher reads this file to decide whether a governed lane is
# still being recorded.
TRANSCRIPT_NAME = "tmux-transcript.log"
CLAUDE_MODELS = ("sonnet", "opus")
BACKENDS = ("claude", "codex", "opencode")
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
SETUP_NOTE_NAME = "session-setup.md"
NATIVE_CONTROLS_NAME = "native-controls.json"
AGENTTRAIL_PLAN_NAME = "PLAN.md"
LaneLifecycle = LaneState
LaneLifecycleAction = Literal["pause", "shelve", "close", "resume", "relaunch"]


class LaneLaunchRefused(RuntimeError):
    """A mechanical launch gate refused an unsafe lane start."""


class LaneStartupFailed(RuntimeError):
    """The agent process exited before its lane session became durable."""


def session_ref(lane: LaneSpec, host: str = SANCTIONED_HOST) -> str:
    return f"{host}:{lane.tmux_session}:0.0"


def ingestion_gate(lane: LaneSpec, *, host: str = SANCTIONED_HOST) -> GoalRecord:
    """Return the frozen goal source only when every launch gate passes."""
    if host not in SANCTIONED_HOSTS:
        allowed = ", ".join(sorted(SANCTIONED_HOSTS))
        raise LaneLaunchRefused(f"lane launch refused: governed lanes must run on one of: {allowed}")
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
    rendered_done_when = render_done_when_items(goal.enrolled_done_when_items)
    if (
        goal.interview_receipt is None
        or not goal.enrolled_done_when_items
        or goal.lane_id != lane.tmux_session
        or goal.enrolled_done_when != rendered_done_when
        or goal.done_when != rendered_done_when
    ):
        raise LaneLaunchRefused("lane launch refused: interview receipt, frozen done items, or lane identity does not match")
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
    if backend == "opencode":
        # OpenCode owns provider and tool-permission behavior in its own
        # config.  Chitra supplies only the explicit model selector and keeps
        # the lane's XDG roots isolated below.
        command = ["opencode"]
        if model:
            command.extend(["--model", model])
        return command
    raise LaneLaunchRefused(f"lane launch refused: unsupported backend {backend!r}")


def _backend_environment(lane: LaneSpec, backend: str) -> tuple[str, ...]:
    """Return per-backend state roots without placing secrets in argv."""
    if backend != "opencode":
        return ()
    return (
        f"XDG_CONFIG_HOME={lane.config_dir / 'xdg'}",
        f"XDG_DATA_HOME={lane.state_dir / 'xdg-data'}",
        f"XDG_STATE_HOME={lane.state_dir / 'xdg-state'}",
    )


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


def _goal_snapshot(goal: GoalRecord) -> dict[str, object]:
    return {name: getattr(goal, name) for name in ("goal", "done_when", "intent", "scope", "source")}


def _goal_snapshot_sha256(goal: GoalRecord) -> str:
    canonical = json.dumps(_goal_snapshot(goal), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def session_setup_note_path(lane: LaneSpec) -> Path:
    """Return the generated setup note consumed by the session's provider."""
    return lane.state_dir / SETUP_NOTE_NAME


def native_controls_path(lane: LaneSpec) -> Path:
    """Return the durable declaration of native goal and loop controls."""
    return lane.state_dir / NATIVE_CONTROLS_NAME


def _native_goal_command(goal: GoalRecord) -> str:
    return f"/goal {goal.goal}"


def _loop_hook_id(lane: LaneSpec, goal: GoalRecord) -> str:
    """Return a stable Claude loop identity for this lane and goal lineage."""
    value = f"{lane.identifier}\0{goal.session_ref}\0{goal.goal_version}".encode()
    return f"chitra-loop-{hashlib.sha256(value).hexdigest()[:16]}"


def _claude_loop_command(lane: LaneSpec, goal: GoalRecord) -> str:
    hook_id = _loop_hook_id(lane, goal)
    return (
        f"/loop {goal.autonomy_policy.loop_interval_minutes}m "
        f"Chitra hook id {hook_id}. First read {native_controls_path(lane)}. "
        "If its lifecycle is not active, call CronList, find this hook id, call CronDelete for it, "
        "and do no further work. If active, re-read the Chitra setup note, inspect the current goal "
        "and evidence, then continue the next useful action or replan around the blocker. "
        "Do not stop while the goal remains open."
    )


def render_session_setup_note(lane: LaneSpec, goal: GoalRecord, *, backend: str) -> str:
    """Render the provider-visible, hash-bound setup note for a launch or resume."""
    bundle: KnowledgeBundle = lane.knowledge_bundle
    goal_sha256 = _goal_snapshot_sha256(goal)
    native_goal = _native_goal_command(goal)
    agenttrail_plan = lane.workdir / AGENTTRAIL_PLAN_NAME
    lines = [
        "# Chitra session setup",
        f"Lane: {lane.identifier}",
        f"Backend: {backend}",
        f"Goal snapshot SHA-256: {goal_sha256}",
        f"Knowledge bundle SHA-256: {bundle.sha256}",
        "",
        "## Goal",
        goal.goal,
        "",
        "## Done when",
        goal.done_when,
        "",
        "## Current unfinished work",
    ]
    if goal.now.strip():
        lines.append(f"- Current state: {goal.now.strip()}")
    else:
        lines.append("- No current tactical update is recorded.")
    foreground_tasks = tuple(getattr(goal, "foreground_tasks", ()) or ())
    if foreground_tasks:
        for task in foreground_tasks:
            lines.append(
                f"- Foreground {task.kind} [{task.task_id}] from {task.source}: {task.text}"
            )
    else:
        lines.append("- No durable foreground tasks are pending.")
    lines.extend(
        (
            "",
            "## Session operating rules",
            "- Treat this note and its goal as the canonical session setup.",
            "- Pursue the goal until independently verified completion. Investigate and replan. "
            "Take successive useful actions when evidence calls for it.",
            "- Record evidence and surface a real missing capability or authority precisely.",
            "- Do not convert routine uncertainty into a user question.",
            "- At session start, establish the native goal with the command below.",
            f"- Native goal command: {native_goal}",
        )
    )
    if backend == "claude":
        lines.extend(
            (
                "- After establishing the goal, create the recurring native enforcement loop below.",
                f"- Native recurring enforcement command: {_claude_loop_command(lane, goal)}",
                "- Reconcile or replace that loop whenever this setup note changes. Remove it when the lane is paused, shelved, or closed.",
            )
        )
    elif backend == "codex":
        lines.append("- Re-establish the native goal whenever the session resumes or the goal changes.")
    lines.extend(
        (
            "",
            "## AgentTrail lane plan",
            f"- Maintain this lane's plan at {agenttrail_plan}.",
            "- Continue a reviewed plan already at that path. If none exists, create one from the canonical goal, "
            "done-when conditions, and current unfinished work before the first implementation action.",
            "- Use stable `## Card title {#card-id}` headings, `needs: [card-id]` dependencies, `files: [path/**]` scopes, "
            "and child tasks marked `[ ]`, `[~]`, `[!]`, or `[x]`.",
            "- Mark a task `[~]` before acting, `[!]` only for a real blocker, and `[x]` only after concrete evidence. "
            "Give each completed task an indented `tech:` evidence line.",
            "- Update the file only when the plan, task state, or evidence changes. Preserve stable IDs and leave an "
            "identical plan untouched during a no-change loop.",
            "- Treat this plan as an advisory progress view. The frozen Chitra goal and independently verified completion "
            "receipts remain authoritative; the plan cannot change the goal or prove completion.",
        )
    )
    lines.extend(("", bundle.render().rstrip(), ""))
    return "\n".join(lines)


def write_session_setup_note(lane: LaneSpec, goal: GoalRecord, *, backend: str) -> Path:
    """Write a fresh setup note before provider startup and return its path."""
    path = session_setup_note_path(lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The state path is Chitra-owned. Atomic replacement avoids a provider
    # observing a partially generated system prompt during launch/relaunch.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(render_session_setup_note(lane, goal, backend=backend), encoding="utf-8")
    temporary.replace(path)
    return path


def _native_controls_payload(lane: LaneSpec, goal: GoalRecord, *, backend: str, setup_note: Path) -> dict[str, object]:
    controls: dict[str, object] = {
        "native_goal": {
            "provider": backend,
            "command": _native_goal_command(goal),
            "state": "required",
        },
        "recurring_enforcement": {
            "provider": "claude",
            "hook_id": _loop_hook_id(lane, goal),
            "command": _claude_loop_command(lane, goal),
            "state": "armed" if backend == "claude" else "not-applicable",
        },
    }
    return {
        "schema": "chitra.native-controls.v1",
        "lifecycle": "active",
        "lane_id": lane.identifier,
        "session_ref": goal.session_ref,
        "goal_snapshot_sha256": _goal_snapshot_sha256(goal),
        "knowledge_bundle_sha256": lane.knowledge_bundle.sha256,
        "setup_note": str(setup_note),
        "controls": controls,
    }


def _durable_lifecycle(lane: LaneSpec, session_ref: str) -> LaneLifecycle:
    """Read the sole lifecycle authority from recovery state."""
    record = get_lane_lifecycle(lane.state_dir, session_ref)
    if record is None:
        raise LaneLaunchRefused(f"lane native controls are unavailable: lifecycle is untracked for {session_ref}")
    return record.state


def write_native_controls(lane: LaneSpec, goal: GoalRecord, *, backend: str, setup_note: Path) -> Path:
    """Persist native delivery details after recovery owns an active transition."""
    path = native_controls_path(lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle = _durable_lifecycle(lane, goal.session_ref)
    if lifecycle != "active":
        raise LaneLaunchRefused(
            f"lane launch refused: lifecycle is {lifecycle}; use the lifecycle resume action first"
        )
    write_json_atomic(path, _native_controls_payload(lane, goal, backend=backend, setup_note=setup_note))
    return path


def set_native_controls_lifecycle(
    lane: LaneSpec,
    lifecycle: LaneLifecycle,
    *,
    completion_verified: bool = False,
) -> dict[str, object]:
    """Change native-control intent without inventing a second scheduler.

    The lifecycle owner performs the provider action.  This durable declaration
    makes the required action unambiguous: pause retains the definition but
    stops wakeups, shelve removes hooks and puts the provider session offline,
    and close removes hooks only after independent completion verification.
    """
    if lifecycle not in LANE_STATES:
        raise ValueError(f"unsupported lane lifecycle: {lifecycle}")
    path = native_controls_path(lane)
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"native controls are missing for lane {lane.identifier}") from exc
    controls = payload.get("controls")
    if not isinstance(controls, dict):
        raise ValueError(f"native controls are invalid for lane {lane.identifier}")
    recurring = controls.get("recurring_enforcement")
    if not isinstance(recurring, dict):
        raise ValueError(f"native controls are invalid for lane {lane.identifier}")
    session_ref = payload.get("session_ref")
    if not isinstance(session_ref, str) or not session_ref:
        raise ValueError(f"native controls are invalid for lane {lane.identifier}")
    durable_lifecycle = _durable_lifecycle(lane, session_ref)
    if durable_lifecycle != lifecycle:
        raise ValueError(
            f"native controls lifecycle is derived from recovery ({durable_lifecycle}), not {lifecycle}"
        )
    if lifecycle == "closed" and not completion_verified:
        raise ValueError("lane close requires independently verified completion")
    payload["lifecycle"] = lifecycle
    if lifecycle == "active":
        recurring["state"] = "armed" if recurring.get("provider") == "claude" else "not-applicable"
        payload["provider_session_action"] = "retain-or-resume"
    elif lifecycle == "paused":
        recurring["state"] = "stopped-definition-retained"
        payload["provider_session_action"] = "retain"
    elif lifecycle == "shelved":
        recurring["state"] = "removed"
        payload["provider_session_action"] = "offline"
    else:
        recurring["state"] = "removed-after-verified-completion"
        payload["provider_session_action"] = "leave-to-lifecycle-owner"
        payload["completion_verified"] = True
    write_json_atomic(path, payload)
    return payload


def _write_codex_developer_config(lane: LaneSpec, setup_note: Path) -> str:
    """Write a dedicated profile beside the lane's existing Codex config.

    Codex resolves profiles from ``$CODEX_HOME/<profile>.config.toml``.  The
    lane keeps its normal ``CODEX_HOME`` and authentication material, while the
    profile adds only Chitra's developer instructions.
    """
    inherited_home = os.environ.get("CODEX_HOME", "").strip()
    codex_home = Path(inherited_home).expanduser() if inherited_home else lane.home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    profile = f"chitra-{lane.identifier}"
    config = codex_home / f"{profile}.config.toml"
    # A JSON string is valid TOML basic-string syntax and avoids hand-rolled
    # escaping while keeping the generated content out of argv.
    instructions = setup_note.read_text(encoding="utf-8")
    config.write_text(f"developer_instructions = {json.dumps(instructions)}\n", encoding="utf-8")
    return profile


def _with_session_setup(
    lane: LaneSpec,
    goal: GoalRecord,
    *,
    backend: str,
    agent_command: Sequence[str],
) -> tuple[list[str], Path]:
    """Generate provider setup artifacts without replacing provider auth state."""
    setup_note = write_session_setup_note(lane, goal, backend=backend)
    command = list(agent_command)
    if backend == "claude":
        command.extend(("--append-system-prompt-file", str(setup_note)))
    elif backend == "codex":
        command.extend(("--profile", _write_codex_developer_config(lane, setup_note)))
    return command, setup_note


def _write_launch_receipt(
    lane: LaneSpec,
    goal: GoalRecord,
    *,
    backend: str,
    model: str | None,
    effort: str | None,
    socket_path: Path,
    setup_note: Path,
    native_controls: Path,
    self_test: SelfTestReport | None = None,
) -> None:
    snapshot = _goal_snapshot(goal)
    payload = {
        "schema": "chitra.lane-launch.v2",
        "lane_id": goal.lane_id,
        "session_ref": goal.session_ref,
        "goal_version": goal.goal_version,
        "enrolled_at": goal.enrolled_at,
        "goal_snapshot": snapshot,
        "goal_snapshot_sha256": _goal_snapshot_sha256(goal),
        "knowledge_bundle": lane.knowledge_bundle.to_dict(),
        "knowledge_bundle_sha256": lane.knowledge_bundle.sha256,
        "setup_note": str(setup_note),
        "native_controls": str(native_controls),
        "worktree_binding": capture_worktree_binding(
            lane.workdir,
            transcript_path=lane_transcript_path(lane),
        ).to_dict(),
        "native_session_identity": {
            "backend": backend,
            "lane_session_ref": goal.session_ref,
            "tmux_session": lane.tmux_session,
            "tmux_pane_target": f"{lane.tmux_session}:0.0",
        },
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


def _native_order_id(
    lane: LaneSpec,
    goal: GoalRecord,
    *,
    control: str,
    lifecycle: LaneLifecycle,
    lifecycle_id: str,
    setup_note: Path,
    request_id: str | None = None,
) -> str:
    payload = {
        "lane": lane.identifier,
        "session_ref": goal.session_ref,
        "control": control,
        "lifecycle": lifecycle,
        "lifecycle_id": lifecycle_id,
        "goal": _goal_snapshot_sha256(goal),
        "knowledge": lane.knowledge_bundle.sha256,
        "setup": hashlib.sha256(setup_note.read_bytes()).hexdigest(),
        "request_id": request_id,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"native-{control}-{digest[:24]}"


def _load_native_controls(lane: LaneSpec) -> dict[str, Any]:
    try:
        payload = json.loads(native_controls_path(lane).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LaneLaunchRefused(f"lane native controls are unavailable: {exc}") from exc
    if not isinstance(payload, dict):
        raise LaneLaunchRefused("lane native controls are invalid")
    session_ref = payload.get("session_ref")
    if not isinstance(session_ref, str) or not session_ref:
        raise LaneLaunchRefused("lane native controls are invalid")
    # The file carries provider commands only. Its lifecycle field is a
    # refreshed view of recovery, never an authority that can re-enable work.
    payload["lifecycle"] = _durable_lifecycle(lane, session_ref)
    return payload


def enqueue_native_controls(
    lane: LaneSpec,
    goal: GoalRecord,
    *,
    request_id: str | None = None,
) -> tuple[str, ...]:
    """Queue provider-native controls through dispatchd; never write a pane directly."""
    controls = _load_native_controls(lane)
    if controls.get("session_ref") != goal.session_ref:
        raise LaneLaunchRefused("lane native controls do not match the enrolled session")
    record = get_lane_lifecycle(lane.state_dir, goal.session_ref)
    if record is None:
        raise LaneLaunchRefused("lane native controls are unavailable: lifecycle is untracked")
    lifecycle = record.state
    setup_note = Path(str(controls["setup_note"]))
    control_set = controls.get("controls")
    if not isinstance(control_set, dict):
        raise LaneLaunchRefused("lane native controls are invalid")
    native_goal = control_set.get("native_goal")
    recurring = control_set.get("recurring_enforcement")
    if not isinstance(native_goal, dict) or not isinstance(recurring, dict):
        raise LaneLaunchRefused("lane native controls are invalid")
    queued: list[str] = []

    def enqueue(control: str, nudge: str) -> None:
        order_id = _native_order_id(
            lane,
            goal,
            control=control,
            lifecycle=lifecycle,
            lifecycle_id=record.lifecycle_id,
            setup_note=setup_note,
            request_id=request_id,
        )
        if locate_order(QueueLayout(lane.queue_dir), order_id).exists:
            if request_id is not None:
                queued.append(order_id)
            return
        enqueue_dispatch_order(
            lane.queue_dir,
            DispatchOrder(
                order_id=order_id,
                session_ref=goal.session_ref,
                nudge=nudge,
                task_type="native-control-pause-prune" if control == "prune" and lifecycle == "paused" else "native-control",
                goal_version=goal.goal_version,
                goal_digest=goal_digest(goal),
            ),
        )
        queued.append(order_id)

    if lifecycle == "active":
        command = native_goal.get("command")
        if not isinstance(command, str) or not command:
            raise LaneLaunchRefused("lane native goal command is invalid")
        enqueue("goal", command)
        if recurring.get("provider") == "claude":
            command = recurring.get("command")
            if not isinstance(command, str) or not command:
                raise LaneLaunchRefused("lane Claude loop command is invalid")
            enqueue("loop", command)
    elif recurring.get("provider") == "claude":
        hook_id = recurring.get("hook_id")
        if not isinstance(hook_id, str) or not hook_id:
            raise LaneLaunchRefused("lane Claude loop hook id is invalid")
        enqueue(
            "prune",
            f"Read {native_controls_path(lane)} now. If its lifecycle is not active, "
            f"call CronList, find Chitra hook id {hook_id}, call CronDelete for it, and do no further work. "
            "If it is active, this control is stale: do nothing and continue the active goal.",
        )
    return tuple(queued)


def rearm_native_controls(lane: LaneSpec, goal: GoalRecord, *, request_id: str) -> tuple[str, ...]:
    """Idempotently reissue the current provider goal and recurring loop controls."""
    if not request_id.strip():
        raise ValueError("native-control rearm requires request_id")
    if _durable_lifecycle(lane, goal.session_ref) != "active":
        raise LaneLaunchRefused("native controls may be rearmed only for an active lane")
    return enqueue_native_controls(lane, goal, request_id=request_id)


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


def _transition_for_lane(
    lane: LaneSpec,
    goal: GoalRecord,
    *,
    target: LaneLifecycle,
    resume_note: str,
    independently_completed: bool = False,
    unfinished_work: bool = True,
    request_id: str | None = None,
) -> LaneLifecycle:
    """Make recovery the sole lifecycle authority, then reconcile controls."""
    binding = capture_worktree_binding(lane.workdir, transcript_path=lane_transcript_path(lane))
    transition_lane_lifecycle(
        lane.state_dir,
        session_ref=goal.session_ref,
        target=target,
        binding=binding,
        resume_note=resume_note,
        independently_completed=independently_completed,
        unfinished_work=unfinished_work,
        request_id=request_id,
    )
    # This state file supplies provider details only. The durable recovery
    # record above remains the one lifecycle authority.
    if native_controls_path(lane).is_file():
        set_native_controls_lifecycle(
            lane,
            target,
            completion_verified=independently_completed and not unfinished_work,
        )
    return target


def _pending_activation(lane: LaneSpec, goal: GoalRecord, *, resume: bool) -> bool:
    """Validate lifecycle without recording active before a session survives."""
    record = get_lane_lifecycle(lane.state_dir, goal.session_ref)
    if record is None:
        if resume:
            raise LaneLaunchRefused("lane resume refused: lifecycle is untracked")
        return True
    if record.state == "active":
        return False
    if resume and record.state in {"paused", "shelved"}:
        return True
    raise LaneLaunchRefused(
        f"lane launch refused: lifecycle is {record.state}; use chitra-lane-anchor resume"
    )


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
            unprobed=("file_write", "gh_api_write", "fleet_ssh"),
        )
    target = ssh_target or os.environ.get(SELFTEST_ENV_SSH_TARGET) or None
    try:
        report = run_self_test(
            backend=backend,
            agent_command=agent_command,
            workdir=lane.workdir,
            ssh_target=target,
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
    self_test_runner: CommandRunner | None = None,
    _resume: bool = False,
    _request_id: str | None = None,
) -> bool:
    """Ensure the lane session exists; return whether this call created it."""
    goal = ingestion_gate(lane, host=host)
    pending_activation = _pending_activation(lane, goal, resume=_resume)
    agent_command, setup_note = _with_session_setup(
        lane,
        goal,
        backend=backend,
        agent_command=_agent_command(backend, model, effort),
    )
    native_controls = native_controls_path(lane)
    if not pending_activation:
        native_controls = write_native_controls(lane, goal, backend=backend, setup_note=setup_note)
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
        if pending_activation:
            try:
                _transition_for_lane(
                    lane,
                    goal,
                    target="active",
                    resume_note="Resumed an already-live governed lane." if _resume else "Recorded an already-live governed lane.",
                    request_id=_request_id,
                )
            except ValueError as exc:
                # The lane's workdir is not a usable git worktree (deleted,
                # moved, or misdeclared in lanes.yaml). Restore the prior
                # respawn behaviour: the pipe is still re-armed above, but the
                # lifecycle transition and native-controls enqueue -- which
                # both require a worktree binding or a tracked lifecycle --
                # are skipped rather than crashing the respawn path.
                print(
                    f"lane {lane.identifier} respawn: skipping lifecycle transition, "
                    f"workdir is not a git worktree: {exc}",
                    file=sys.stderr,
                )
                return False
            native_controls = write_native_controls(lane, goal, backend=backend, setup_note=setup_note)
        enqueue_native_controls(lane, goal)
        return False
    identity_environment = (
        f"{LANE_ID_ENV_VAR}={lane.identifier}",
        f"{SESSION_REF_ENV_VAR}={goal.session_ref}",
        f"{PANE_TARGET_ENV_VAR}={lane.tmux_session}:0.0",
        f"{SOCKET_PATH_ENV_VAR}={control_socket}",
    )
    pane_environment = (
        *identity_environment,
        *_backend_environment(lane, backend),
        f"{PYTHONPATH_ENV_VAR}={_pane_pythonpath()}",
    )
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
        extra_environment=_backend_environment(lane, backend),
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
        runner=runner,
        probe_runner=self_test_runner,
    )
    if pending_activation:
        _transition_for_lane(
            lane,
            goal,
            target="active",
            resume_note="Resumed from the exact saved worktree checkpoint." if _resume else "Initial governed lane launch succeeded.",
            request_id=_request_id,
        )
        native_controls = write_native_controls(lane, goal, backend=backend, setup_note=setup_note)
    _write_launch_receipt(
        lane,
        goal,
        backend=backend,
        model=model,
        effort=effort,
        socket_path=control_socket,
        setup_note=setup_note,
        native_controls=native_controls,
        self_test=report,
    )
    enqueue_native_controls(lane, goal)
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


def pause_lane(
    lane: LaneSpec,
    *,
    host: str = SANCTIONED_HOST,
    request_id: str | None = None,
) -> tuple[str, ...]:
    """Checkpoint a live lane and stop its recurring enforcement hook."""
    goal = ingestion_gate(lane, host=host)
    _transition_for_lane(
        lane,
        goal,
        target="paused",
        resume_note="Paused by the governed lane lifecycle.",
        request_id=request_id,
    )
    return enqueue_native_controls(lane, goal) if native_controls_path(lane).is_file() else ()


def shelve_lane(
    lane: LaneSpec,
    *,
    host: str = SANCTIONED_HOST,
    runner: CommandRunner = _run,
    request_id: str | None = None,
) -> tuple[str, ...]:
    """Checkpoint, disable enforcement, request hook deletion, and take a lane offline."""
    goal = ingestion_gate(lane, host=host)
    _transition_for_lane(
        lane,
        goal,
        target="shelved",
        resume_note="Shelved with its worktree, goal, questions, and checkpoint retained.",
        request_id=request_id,
    )
    queued: tuple[str, ...] = ()
    stop_lane(lane, runner=runner)
    return queued


def close_lane(
    lane: LaneSpec,
    *,
    host: str = SANCTIONED_HOST,
    runner: CommandRunner = _run,
    request_id: str | None = None,
) -> tuple[str, ...]:
    """Close only after Chitra's independent completion gate has passed."""
    goal = ingestion_gate(lane, host=host)
    if goal.status != "done-pending-close":
        raise LaneLaunchRefused("lane close refused: independent completion is not verified")
    _transition_for_lane(
        lane,
        goal,
        target="closed",
        resume_note="Independent completion passed; lane closed with worktree retained.",
        independently_completed=True,
        unfinished_work=False,
        request_id=request_id,
    )
    queued: tuple[str, ...] = ()
    stop_lane(lane, runner=runner)
    return queued


def resume_lane(
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
    request_id: str | None = None,
) -> bool:
    """Validate the checkpoint, reactivate controls, and restore a lane."""
    goal = ingestion_gate(lane, host=host)
    record = get_lane_lifecycle(lane.state_dir, goal.session_ref)
    if record is not None and record.state == "active" and request_id == record.request_id:
        return start_lane(
            lane,
            backend=backend,
            model=model,
            effort=effort,
            host=host,
            socket_path=socket_path,
            runner=runner,
            self_test=self_test,
            self_test_ssh_target=self_test_ssh_target,
            _resume=True,
            _request_id=request_id,
        )
    if record is None or record.state not in {"paused", "shelved"}:
        state = record.state if record is not None else "untracked"
        raise LaneLaunchRefused(f"lane resume refused: lifecycle is {state}")
    validate_lane_resume(
        lane.state_dir,
        session_ref=goal.session_ref,
        binding=capture_worktree_binding(lane.workdir, transcript_path=lane_transcript_path(lane)),
    )
    return start_lane(
        lane,
        backend=backend,
        model=model,
        effort=effort,
        host=host,
        socket_path=socket_path,
        runner=runner,
        self_test=self_test,
        self_test_ssh_target=self_test_ssh_target,
        _resume=True,
        _request_id=request_id,
    )


def lane_lifecycle_status(lane: LaneSpec, *, host: str = SANCTIONED_HOST) -> LaneLifecycle:
    """Return exactly one public lane lifecycle state."""
    goal = ingestion_gate(lane, host=host)
    record = get_lane_lifecycle(lane.state_dir, goal.session_ref)
    if record is None:
        raise LaneLaunchRefused("lane lifecycle is untracked; start the lane to enroll its lifecycle")
    return record.state


def execute_lane_lifecycle(
    lane: LaneSpec,
    *,
    action: LaneLifecycleAction,
    request_id: str | None = None,
    backend: str = "claude",
    model: str | None = "sonnet",
    effort: str | None = "high",
    host: str = SANCTIONED_HOST,
    socket_path: Path | None = None,
    runner: CommandRunner = _run,
    self_test: bool = True,
    self_test_ssh_target: str | None = None,
) -> LaneLifecycle:
    """Apply one idempotent bridge lifecycle command to its real lane.

    ``request_id`` is persisted by recovery for state-changing actions. A
    retry with the same request finishes provider work after the durable
    transition without appending another checkpoint. ``relaunch`` is not a
    restart transaction: it only ensures an already-active lane is live.
    """
    if action == "pause":
        pause_lane(lane, host=host, request_id=request_id)
    elif action == "shelve":
        shelve_lane(lane, host=host, runner=runner, request_id=request_id)
    elif action == "close":
        close_lane(lane, host=host, runner=runner, request_id=request_id)
    elif action == "resume":
        resume_lane(
            lane,
            backend=backend,
            model=model,
            effort=effort,
            host=host,
            socket_path=socket_path,
            runner=runner,
            self_test=self_test,
            self_test_ssh_target=self_test_ssh_target,
            request_id=request_id,
        )
    elif action == "relaunch":
        if lane_lifecycle_status(lane, host=host) != "active":
            raise LaneLaunchRefused("lane relaunch refused: only an active lane may be ensured live")
        start_lane(
            lane,
            backend=backend,
            model=model,
            effort=effort,
            host=host,
            socket_path=socket_path,
            runner=runner,
            self_test=self_test,
            self_test_ssh_target=self_test_ssh_target,
        )
    else:
        raise ValueError(f"unsupported lane lifecycle action: {action}")
    return lane_lifecycle_status(lane, host=host)


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
    parser.add_argument(
        "--model",
        default=None,
        help="model selector; Claude defaults to sonnet when omitted",
    )
    parser.add_argument("--effort", default="high")
    parser.add_argument("--socket-path", type=Path, default=None)
    # Which host the SSH probe reaches is a site fact, not a property of this
    # package, so there is no default target here. Without one the SSH class is
    # reported as unprobed rather than counted as a pass it never measured.
    parser.add_argument(
        "--selftest-ssh-target",
        default=None,
        help=(
            "host the launch self-test reaches over SSH, for example user@host; "
            f"falls back to ${SELFTEST_ENV_SSH_TARGET}"
        ),
    )
    parser.add_argument(
        "--no-self-test",
        dest="self_test",
        action="store_false",
        help="skip the launch self-test; the lane's permissions are then unproven",
    )
    parser.add_argument("action", choices=("start", "pause", "resume", "shelve", "close", "status"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    lane = lane_for_identifier(load_lanes(args.lanes_file), args.identifier)
    if not lane.enabled and args.action == "start":
        raise SystemExit(f"lane is declared disabled: {lane.identifier}")
    if args.action == "start":
        try:
            model = args.model if args.model is not None else ("sonnet" if args.backend == "claude" else None)
            start_lane(
                lane,
                backend=args.backend,
                model=model,
                effort=args.effort,
                host=args.host,
                socket_path=args.socket_path,
                self_test=args.self_test,
                self_test_ssh_target=args.selftest_ssh_target,
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
    if args.action == "pause":
        pause_lane(lane, host=args.host)
        return 0
    if args.action == "resume":
        model = args.model if args.model is not None else ("sonnet" if args.backend == "claude" else None)
        resume_lane(
            lane,
            backend=args.backend,
            model=model,
            effort=args.effort,
            host=args.host,
            socket_path=args.socket_path,
            self_test=args.self_test,
            self_test_ssh_target=args.selftest_ssh_target,
        )
        return 0
    if args.action == "shelve":
        shelve_lane(lane, host=args.host)
        return 0
    if args.action == "close":
        close_lane(lane, host=args.host)
        return 0
    print(lane_lifecycle_status(lane, host=args.host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

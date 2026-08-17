"""Prove at launch that a governed lane's own work is not permission-refused.

Measured on tophand over the 2026-08-16/17 shift: three governed lanes were
refused their own core work by the Claude Code auto-mode classifier, with the
message "Denied by auto mode classifier - Blocked by classifier". infra-followup
lost its package publish, access-broker lost a pull-request write, and
starchamber lost ``ssh agent@renegade``. One refusal landed on a read-only
``git status``. Each lane read the refusal as a policy decision and told the
operator a person was needed, which cost hours across one shift.

Nothing said this at launch. The lanes started clean, ran for hours, and only
discovered the defect when they reached for the tool they could not use. This
module closes that gap: the launcher runs one representative command of each
class a lane needs, and refuses to record the launch when any of them is turned
down by the permission layer.

The distinction this module draws is the one that matters. A command that runs
and fails is a fleet fact -- GitHub is down, a host is unreachable -- and it is
reported, not fatal. A command the agent was not allowed to attempt is a launch
defect, and it fails the launch.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

SELFTEST_ENV_SSH_TARGET = "CHITRA_LANE_SELFTEST_SSH_TARGET"
PROBE_TIMEOUT_SECONDS = 300
PROBE_MARKER = "chitra-lane-selftest"

# The permission layer announces a refusal in prose, not in a status code. These
# are the phrases Claude Code uses when it turns a tool call down; the launch
# fails on any of them even when the JSON denial list comes back empty, because
# a refusal reported only in the assistant's own text is still a refusal.
REFUSAL_MARKERS = (
    "auto mode classifier",
    "blocked by classifier",
    "permission for this action was denied",
    "requested permissions",
    "claude requested permissions to use",
)


class LanePermissionRefused(RuntimeError):
    """A launched lane was refused a command class it needs to do its job."""


class LaneSelfTestUnavailable(RuntimeError):
    """The self-test could not be run, so it proved nothing either way."""


@dataclass(frozen=True, slots=True)
class Probe:
    """One representative piece of work from a class the lane depends on.

    ``instruction`` is what the agent is told to do, and it is not always a
    shell command. A lane writes a file with its file-writing tool, not with a
    shell redirect, and on a managed host a shell redirect is separately fenced,
    so a probe written as ``printf x > file`` would measure the fence rather
    than the lane's own file-write path. Measured on tophand 2026-08-17: the
    first live run of this self-test failed on exactly that, and the probe was
    wrong, not the lane.
    """

    name: str
    purpose: str
    instruction: str


@dataclass(frozen=True, slots=True)
class SelfTestReport:
    """What the launch-time self-test observed, in a form a receipt can hold."""

    backend: str
    probed: tuple[str, ...] = ()
    unprobed: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    live: bool = False
    detail: str = ""
    commands: tuple[str, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return not self.refusals

    def as_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "live": self.live,
            "passed": self.passed,
            "probed": list(self.probed),
            "unprobed": list(self.unprobed),
            "refusals": list(self.refusals),
            "detail": self.detail,
        }


def probe_path(workdir: Path) -> Path:
    """Return the file the write probe creates, inside the lane's own worktree."""
    return workdir / ".chitra-lane-selftest.probe"


def build_probes(workdir: Path, *, ssh_target: str | None) -> tuple[tuple[Probe, ...], tuple[str, ...]]:
    """Return the probes to run and the names of the classes left unprobed.

    The file write lands in the lane's worktree, which is where a lane does its
    work, and it is made with the agent's file-writing tool rather than a shell
    redirect, because that is how a lane writes files.

    The GitHub call is a POST -- the same shape as the pull-request and package
    writes that were refused -- against the ``/markdown`` endpoint, so it
    exercises an authenticated write path without changing a repository. It
    proves the lane may make an authenticated write call. It does not prove the
    token carries push rights on any given repository, and it is not a
    substitute for that check.

    The SSH target is a site fact, not a property of this package, so it is
    supplied by the caller. With no target there is no honest SSH probe, and the
    class is reported as unprobed rather than quietly counted as passing.
    """
    probes = [
        Probe(
            name="file_write",
            purpose="write a file in the lane's own worktree",
            instruction=(
                f"Using your file-writing tool, write the single line {PROBE_MARKER} "
                f"to the file {probe_path(workdir)}"
            ),
        ),
        Probe(
            name="gh_api_write",
            purpose="make an authenticated GitHub API write call",
            instruction=(
                "Run this shell command: "
                f"gh api --method POST /markdown --field text={PROBE_MARKER}"
            ),
        ),
    ]
    unprobed: list[str] = []
    if ssh_target:
        probes.append(
            Probe(
                name="fleet_ssh",
                purpose="reach another fleet host over SSH",
                instruction=(
                    "Run this shell command: "
                    f"ssh -o BatchMode=yes -o ConnectTimeout=10 {ssh_target} true"
                ),
            )
        )
    else:
        unprobed.append("fleet_ssh")
    return tuple(probes), tuple(unprobed)


def build_prompt(probes: Sequence[Probe]) -> str:
    """Ask for each command once, and for a verdict this module can read.

    The agent is told to report a refusal rather than route around it. Routing
    around a refusal is the behaviour that hid this defect for a full shift: a
    lane that finds another way to do the work also stops anyone finding out it
    was refused.
    """
    lines = [
        "You are a launch-time self-test for a governed Chitra lane.",
        "Do each of the following exactly once, in order.",
        "Do not modify an instruction. Do not substitute a different one.",
        "If something runs and fails, that is a normal result: report it and carry on.",
        "If you are not permitted to attempt one, do not look for another way to do it.",
        "Report the refusal instead, quoting the refusal text you were given.",
        "",
    ]
    for index, probe in enumerate(probes, start=1):
        lines.append(f"{index}. [{probe.name}] {probe.instruction}")
    lines.extend(
        [
            "",
            "Then reply with one line per item, in this exact form:",
            "<name>: RAN <one-line outcome>   or   <name>: REFUSED <the refusal text>",
            "",
            "The names, in order, are: " + ", ".join(probe.name for probe in probes) + ".",
        ]
    )
    return "\n".join(lines)


def _final_result(stdout: str) -> dict[str, object]:
    """Return the result object from a headless JSON transcript."""
    payload = json.loads(stdout)
    if isinstance(payload, list):
        if not payload:
            raise ValueError("headless output carried no result object")
        payload = payload[-1]
    if not isinstance(payload, dict):
        raise ValueError("headless output was not a result object")
    return payload


def read_refusals(stdout: str, probes: Sequence[Probe]) -> tuple[str, ...]:
    """Return one description per class the permission layer turned down.

    Two independent signals are read. ``permission_denials`` is the structured
    record Claude Code keeps of every tool call it declined. The assistant's own
    text is read as well, because a lane that is refused mid-turn describes the
    refusal in prose, and a self-test that trusted only the structured field
    would pass a lane that had just been told no.
    """
    result = _final_result(stdout)
    refusals: list[str] = []

    denials = result.get("permission_denials")
    if isinstance(denials, list):
        for denial in denials:
            if isinstance(denial, dict):
                tool = denial.get("tool_name") or denial.get("tool") or "tool"
                detail = json.dumps(denial.get("tool_input", {}), sort_keys=True)[:200]
                refusals.append(f"permission layer declined {tool}: {detail}")
            else:
                refusals.append(f"permission layer declined: {denial}")

    text = result.get("result")
    if isinstance(text, str):
        lowered = text.lower()
        if any(marker in lowered for marker in REFUSAL_MARKERS):
            refusals.append(f"agent reported a refusal: {text.strip()[:400]}")
        for probe in probes:
            if f"{probe.name}: refused" in lowered:
                refusals.append(f"{probe.name} was refused ({probe.purpose})")

    seen: set[str] = set()
    ordered: list[str] = []
    for refusal in refusals:
        if refusal not in seen:
            seen.add(refusal)
            ordered.append(refusal)
    return tuple(ordered)


def _run(command: Sequence[str], *, timeout: int = PROBE_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)


def claude_probe_command(agent_command: Sequence[str], prompt: str) -> list[str]:
    """Return the headless form of the very command the lane itself will run.

    The self-test runs the lane's own command, flags and all, so it tests the
    permission mode the lane actually starts in rather than a reconstruction of
    it. Only the headless output options are added.
    """
    return [*agent_command, "--output-format", "json", "-p", prompt]


def run_self_test(
    *,
    backend: str,
    agent_command: Sequence[str],
    workdir: Path,
    ssh_target: str | None,
    as_lane: Callable[[Sequence[str]], list[str]],
    runner: CommandRunner = _run,
) -> SelfTestReport:
    """Run one representative command per class and report every refusal.

    Codex lanes get the deterministic half only. Codex reports its approval
    decisions in a different transcript shape, and a self-test that guessed at
    that shape would report a pass it had not measured. The caller still checks
    that a Codex lane was launched with full access; what is missing is the live
    probe, and this says so rather than implying coverage it does not have.
    """
    if backend != "claude":
        return SelfTestReport(
            backend=backend,
            unprobed=("file_write", "gh_api_write", "fleet_ssh"),
            live=False,
            detail=(
                "live probes run for the Claude backend only; a Codex lane is checked "
                "for its full-access flag but its command classes are not exercised"
            ),
        )

    probes, unprobed = build_probes(workdir, ssh_target=ssh_target)
    prompt = build_prompt(probes)
    command = as_lane(claude_probe_command(agent_command, prompt))
    started = time.monotonic()
    try:
        result = runner(command)
    except subprocess.TimeoutExpired as exc:
        raise LaneSelfTestUnavailable(
            f"lane self-test did not finish within {PROBE_TIMEOUT_SECONDS}s; "
            "the launch is unproven, not proven bad"
        ) from exc
    elapsed = time.monotonic() - started

    if result.returncode != 0 and not result.stdout.strip():
        detail = result.stderr.strip() or f"agent exited {result.returncode}"
        raise LaneSelfTestUnavailable(f"lane self-test could not run: {detail}")

    try:
        refusals = read_refusals(result.stdout, probes)
    except (ValueError, json.JSONDecodeError) as exc:
        raise LaneSelfTestUnavailable(f"lane self-test output could not be read: {exc}") from exc

    return SelfTestReport(
        backend=backend,
        probed=tuple(probe.name for probe in probes),
        unprobed=unprobed,
        refusals=refusals,
        live=True,
        detail=f"probed {len(probes)} command classes in {elapsed:.0f}s",
        commands=tuple(probe.instruction for probe in probes),
    )


def refusal_message(lane_identifier: str, report: SelfTestReport) -> str:
    """Say what was refused, in words an operator can act on without context."""
    lines = [
        f"LAUNCH DEFECT: lane {lane_identifier} was refused its own core work at launch.",
        "A governed lane runs at full permissions. A refusal here means the lane was",
        "started in the wrong permission mode, and it would have spent hours reporting",
        "false blockers to the operator. The lane has been stopped and no launch",
        "receipt was written.",
        "",
    ]
    lines.extend(f"  refused: {refusal}" for refusal in report.refusals)
    if report.unprobed:
        lines.append("")
        lines.append("  not probed: " + ", ".join(report.unprobed))
    return "\n".join(lines)

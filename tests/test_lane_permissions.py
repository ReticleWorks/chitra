"""Tests for the full-permission launch contract and its launch-time self-test.

Measured on tophand across the 2026-08-16/17 shift: three governed lanes were
refused their own core work by the Claude Code auto-mode classifier. The
launcher built ``claude --model M --effort E`` and passed no permission flag, so
each lane fell back to the config directory's ``permissions.defaultMode``, which
on that host is ``auto``. Every tool call a lane made was then judged one at a
time, and refusals landed on a package publish, a pull-request write, ssh to
Renegade, and a read-only ``git status``. Each lane reported the refusal to the
operator as needing a person.

These tests hold two lines. A lane command must carry the full-permission flag
for its backend, and a lane that is refused a command class at launch must not
be recorded as launched.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from chitra import lane_anchor
from chitra.lane_anchor import (
    CLAUDE_FULL_PERMISSION_FLAG,
    CODEX_FULL_ACCESS_FLAG,
    LaneLaunchRefused,
    _agent_command,
    _require_full_permissions,
)
from chitra.lane_selftest import (
    LanePermissionRefused,
    LaneSelfTestUnavailable,
    Probe,
    build_probes,
    build_prompt,
    claude_probe_command,
    probe_path,
    read_refusals,
    run_self_test,
)

CLASSIFIER_REFUSAL = (
    "Permission for this action was denied by the Claude Code auto mode "
    "classifier. Reason: Blocked by classifier."
)


def _headless(result_text: str, *, denials: list[dict[str, object]] | None = None) -> str:
    """Return the JSON a headless agent run prints, in the shape 2.1.x uses."""
    return json.dumps(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "is_error": False,
                "permission_denials": denials or [],
                "result": result_text,
            },
        ]
    )


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


# --- the launch command carries full permissions -------------------------------


def test_a_claude_lane_starts_with_permissions_bypassed() -> None:
    """The flag whose absence sent three lanes to the classifier."""
    assert _agent_command("claude", "opus", "high") == [
        "claude",
        "--model",
        "opus",
        "--effort",
        "high",
        CLAUDE_FULL_PERMISSION_FLAG,
    ]


def test_a_codex_lane_starts_with_full_access() -> None:
    command = _agent_command("codex", "gpt-5.6-sol", "high")
    assert command[-1] == CODEX_FULL_ACCESS_FLAG
    assert command[:2] == ["codex", "--model"]


def test_a_codex_lane_at_backend_default_effort_still_gets_full_access() -> None:
    assert CODEX_FULL_ACCESS_FLAG in _agent_command("codex", None, "none")


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_a_command_without_the_flag_is_refused_before_anything_is_created(backend: str) -> None:
    """The cheap half of the self-test, which costs no model call."""
    with pytest.raises(LaneLaunchRefused, match="partial permissions"):
        _require_full_permissions(backend, [backend, "--model", "sonnet"])


def test_the_flag_check_passes_the_command_the_launcher_builds() -> None:
    _require_full_permissions("claude", _agent_command("claude", "sonnet", "high"))
    _require_full_permissions("codex", _agent_command("codex", "gpt-5.6-sol", "high"))


# --- the probes ----------------------------------------------------------------


def test_the_probes_cover_the_classes_the_blocked_lanes_needed(tmp_path: Path) -> None:
    probes, unprobed = build_probes(
        tmp_path,
        ssh_target="tiptap@renegade",
        publish_prefix="s3://polyphony-fleet-packages/selftest",
    )
    assert [probe.name for probe in probes] == [
        "file_write",
        "gh_api_write",
        "fleet_ssh",
        "package_publish",
    ]
    assert unprobed == ()
    assert str(probe_path(tmp_path)) in probes[0].instruction
    assert "gh api --method POST" in probes[1].instruction
    assert "tiptap@renegade" in probes[2].instruction
    assert "s3://polyphony-fleet-packages/selftest" in probes[3].instruction


def test_the_publish_probe_is_shaped_like_a_write_not_a_read(tmp_path: Path) -> None:
    """The publish class is the one that cost infra-followup its warden-guard publish.

    The classifier judges what a command does, so a read-only ``aws`` call can
    pass while a write is turned down. A read-only probe would therefore miss
    exactly the refusal this class exists to catch.
    """
    probes, _ = build_probes(
        tmp_path, ssh_target=None, publish_prefix="s3://bucket/prefix/"
    )
    publish = probes[-1]
    assert publish.name == "package_publish"
    assert "aws s3 cp" in publish.instruction
    # A trailing slash on the prefix must not produce a doubled separator.
    assert "s3://bucket/prefix/chitra-lane-selftest.txt" in publish.instruction


def test_an_unset_publish_prefix_is_reported_as_unprobed_not_as_a_pass(tmp_path: Path) -> None:
    probes, unprobed = build_probes(tmp_path, ssh_target="tiptap@renegade")
    assert [probe.name for probe in probes] == ["file_write", "gh_api_write", "fleet_ssh"]
    assert unprobed == ("package_publish",)


def test_the_write_probe_uses_the_file_tool_not_a_shell_redirect(tmp_path: Path) -> None:
    """Measured on tophand 2026-08-17, on this self-test's first live run.

    The probe was written as ``printf x > file`` and was refused, because a
    managed host separately fences shell redirects. That refusal was the probe's
    fault, not the lane's: a lane writes files with its file-writing tool.
    """
    probes, _ = build_probes(tmp_path, ssh_target=None)
    write_probe = probes[0]
    assert "file-writing tool" in write_probe.instruction
    assert ">" not in write_probe.instruction
    assert probe_path(tmp_path).parent == tmp_path


def test_an_unset_ssh_target_is_reported_as_unprobed_not_as_a_pass(tmp_path: Path) -> None:
    """Silence about a class is the failure mode this whole file exists to end."""
    probes, unprobed = build_probes(tmp_path, ssh_target=None)
    assert [probe.name for probe in probes] == ["file_write", "gh_api_write"]
    assert unprobed == ("fleet_ssh", "package_publish")


def test_the_prompt_forbids_routing_around_a_refusal(tmp_path: Path) -> None:
    """A lane that works around a refusal also hides it, which is how a shift was lost."""
    probes, _ = build_probes(tmp_path, ssh_target="temp-twinridge")
    prompt = build_prompt(probes)
    assert "do not look for another way to do it" in prompt
    for probe in probes:
        assert probe.instruction in prompt


def test_the_probe_runs_the_lane_s_own_command() -> None:
    agent_command = _agent_command("claude", "opus", "high")
    probe = claude_probe_command(agent_command, "PROMPT")
    assert probe[: len(agent_command)] == agent_command
    assert probe[-2:] == ["-p", "PROMPT"]
    assert "--output-format" in probe


# --- reading the verdict -------------------------------------------------------


def test_a_clean_run_reports_no_refusals(tmp_path: Path) -> None:
    probes, _ = build_probes(tmp_path, ssh_target="temp-twinridge")
    stdout = _headless("file_write: RAN exit=0\ngh_api_write: RAN exit=0\nfleet_ssh: RAN exit=0")
    assert read_refusals(stdout, probes) == ()


def test_a_command_that_runs_and_fails_is_not_a_refusal(tmp_path: Path) -> None:
    """GitHub being down is a fleet fact. It is not a launch defect."""
    probes, _ = build_probes(tmp_path, ssh_target="temp-twinridge")
    stdout = _headless("file_write: RAN exit=0\ngh_api_write: RAN exit=1\nfleet_ssh: RAN exit=255")
    assert read_refusals(stdout, probes) == ()


def test_the_structured_denial_record_is_read(tmp_path: Path) -> None:
    probes, _ = build_probes(tmp_path, ssh_target="temp-twinridge")
    stdout = _headless(
        "file_write: RAN exit=0",
        denials=[{"tool_name": "Bash", "tool_input": {"command": "gh api --method POST /markdown"}}],
    )
    refusals = read_refusals(stdout, probes)
    assert any("declined Bash" in refusal for refusal in refusals)


def test_a_refusal_reported_only_in_prose_is_still_a_refusal(tmp_path: Path) -> None:
    """The verbatim text three tophand lanes were given on 2026-08-16/17."""
    probes, _ = build_probes(tmp_path, ssh_target="temp-twinridge")
    stdout = _headless(f"file_write: RAN exit=0\ngh_api_write: REFUSED {CLASSIFIER_REFUSAL}")
    refusals = read_refusals(stdout, probes)
    assert refusals
    assert any("auto mode classifier" in refusal for refusal in refusals)


def test_a_named_class_marked_refused_is_caught_without_the_classifier_wording(tmp_path: Path) -> None:
    probes, _ = build_probes(tmp_path, ssh_target="temp-twinridge")
    stdout = _headless("file_write: RAN exit=0\nfleet_ssh: REFUSED a permission guard blocked me")
    assert any("fleet_ssh was refused" in refusal for refusal in read_refusals(stdout, probes))


def test_unreadable_output_is_an_error_not_a_pass(tmp_path: Path) -> None:
    probes, _ = build_probes(tmp_path, ssh_target="temp-twinridge")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        read_refusals("not json at all", probes)


# --- the self-test as the launcher calls it ------------------------------------


def _as_lane(command: Sequence[str]) -> list[str]:
    return ["env", "HOME=/home/lane", *command]


def test_a_clean_self_test_reports_what_it_probed(tmp_path: Path) -> None:
    report = run_self_test(
        backend="claude",
        agent_command=_agent_command("claude", "sonnet", "high"),
        workdir=tmp_path,
        ssh_target="temp-twinridge",
        as_lane=_as_lane,
        runner=lambda command: _completed(_headless("file_write: RAN exit=0")),
    )
    assert report.passed
    assert report.live
    assert report.probed == ("file_write", "gh_api_write", "fleet_ssh")


def test_a_refused_self_test_does_not_pass(tmp_path: Path) -> None:
    report = run_self_test(
        backend="claude",
        agent_command=_agent_command("claude", "sonnet", "high"),
        workdir=tmp_path,
        ssh_target="temp-twinridge",
        as_lane=_as_lane,
        runner=lambda command: _completed(_headless(f"gh_api_write: REFUSED {CLASSIFIER_REFUSAL}")),
    )
    assert not report.passed
    assert report.refusals


def test_an_agent_that_cannot_run_leaves_the_lane_unproven(tmp_path: Path) -> None:
    """Unproven is its own answer. It is not reported as a pass."""
    with pytest.raises(LaneSelfTestUnavailable):
        run_self_test(
            backend="claude",
            agent_command=_agent_command("claude", "sonnet", "high"),
            workdir=tmp_path,
            ssh_target="temp-twinridge",
            as_lane=_as_lane,
            runner=lambda command: _completed("", returncode=1, stderr="claude: command not found"),
        )


def test_a_codex_lane_reports_that_its_classes_were_not_exercised(tmp_path: Path) -> None:
    report = run_self_test(
        backend="codex",
        agent_command=_agent_command("codex", "gpt-5.6-sol", "high"),
        workdir=tmp_path,
        ssh_target="temp-twinridge",
        as_lane=_as_lane,
        runner=lambda command: pytest.fail("a Codex lane must not spend a live probe"),
    )
    assert not report.live
    assert report.unprobed
    assert report.passed


# --- what the launcher does with the verdict -----------------------------------


class _Lane:
    """The launcher only reads these fields on the self-test path."""

    identifier = "probe-lane"
    account = "ubuntu"
    uid = 1000

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.home = Path("/home/ubuntu")
        self.workdir = Path("/home/ubuntu")
        self.config_dir = Path("/home/ubuntu/.claude")
        self.tmux_socket = state_dir / "tmux.sock"
        self.tmux_session = "probe-lane"


def test_a_refusal_stops_the_lane_and_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lane that cannot do its work must not be left running and recorded."""
    stopped: list[str] = []
    monkeypatch.setattr(lane_anchor, "stop_lane", lambda lane, runner=None: stopped.append(lane.identifier))
    with pytest.raises(LanePermissionRefused, match="LAUNCH DEFECT"):
        lane_anchor._prove_lane_permissions(
            _Lane(tmp_path),
            backend="claude",
            agent_command=_agent_command("claude", "sonnet", "high"),
            enabled=True,
            ssh_target="temp-twinridge",
            runner=lambda command: _completed(""),
            probe_runner=lambda command: _completed(
                _headless(f"gh_api_write: REFUSED {CLASSIFIER_REFUSAL}")
            ),
        )
    assert stopped == ["probe-lane"]


def test_a_clean_probe_leaves_the_lane_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lane_anchor, "stop_lane", lambda lane, runner=None: pytest.fail("a clean lane must not be stopped")
    )
    report = lane_anchor._prove_lane_permissions(
        _Lane(tmp_path),
        backend="claude",
        agent_command=_agent_command("claude", "sonnet", "high"),
        enabled=True,
        ssh_target="temp-twinridge",
        runner=lambda command: _completed(""),
        probe_runner=lambda command: _completed(_headless("file_write: RAN exit=0")),
    )
    assert report.passed and report.live


def test_the_ssh_target_can_come_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_LANE_SELFTEST_SSH_TARGET", "tiptap@renegade")
    seen: list[Sequence[str]] = []

    def _record(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return _completed(_headless("file_write: RAN exit=0"))

    lane_anchor._prove_lane_permissions(
        _Lane(tmp_path),
        backend="claude",
        agent_command=_agent_command("claude", "sonnet", "high"),
        enabled=True,
        ssh_target=None,
        runner=lambda command: _completed(""),
        probe_runner=_record,
    )
    assert any("tiptap@renegade" in argument for argument in seen[0])


def test_the_receipt_records_what_the_self_test_proved() -> None:
    probe = Probe(name="file_write", purpose="write a file", instruction="write /tmp/x")
    assert probe.name in build_prompt([probe])

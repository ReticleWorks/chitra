"""Tests for chitra.dispatch kept in the wave-1 burn-down.

What remains here is deliberate:
- the transcript-verification contract -- which transcript records count as
  proof that a nudge actually reached the lane (marker + real turn start,
  codex/claude envelopes, hook delivery, mid-turn queued commands, and every
  shape that must NOT confirm);
- LaneLock single-writer enforcement on real lock files;
- the governed-remote permission boundary -- remote dispatch only ever goes
  through ssh and the narrow chitra-tmux-capture / chitra-lane-steer verbs;
- SAFETY deny paths no other test asserts: host allowlist, unsubmitted-draft
  protection, malformed session_ref, transcript-glob traversal, fail-closed
  pane/composer handling, ssh run-as validation, session-qualified pane
  targeting (a bare pane spec must never reach tmux), honest
  FAILED/UNCONFIRMED reporting;
- one real-tmux roundtrip, skipped where tmux is absent.

Removed: scripted-tmux paste/find internals (pane_in_mode, paste -p flag,
remote find commands, pane-capture verification, TUI fallback internals,
directive-voice mechanics -- its deny path is covered by test_dispatchd).
Restored verbatim: the tmux_pane_target regression that keeps every -t
target session-qualified.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from chitra.dispatch import (
    _CODEX_KITTY_ENTER_SEQUENCE,
    DispatchOrder,
    DispatchStatus,
    LaneLock,
    LaneLockError,
    capture_dispatch_pane,
    dispatch_to_tmux,
    ensure_nudge_submitted,
    is_local_host,
    pane_in_mode,
    pane_input_check,
    paste_nudge_to_local_tmux,
    ssh_command,
    transcript_confirms_nudge,
    transcript_glob,
)

HAS_TMUX = shutil.which("tmux") is not None
# A remote host has to be one this machine cannot be. Naming a real fleet host
# means the tests pass everywhere except on that host, where they quietly take
# the local path and stop testing the remote behaviour they are named for. That
# is how three governed-remote tests read as failures on tophand and passes in
# CI. `.invalid` is reserved and never resolves.
REMOTE_HOST = "not-the-local-host.invalid"

def user_turn_jsonl(text: str, *, with_followup: bool = True) -> str:
    """Build a structural JSONL transcript fixture.

    ``transcript_confirms_nudge`` now requires the marker to land in a
    user-role record with a later agent/tool record proving the turn actually
    started (see ``_structural_transcript_confirms``) -- a
    plain ``{"text": ...}`` line with no role is no longer confirmation.
    """
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": text}})]
    if with_followup:
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Working on it."}}))
    return "\n".join(lines) + "\n"


def fake_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build a real ``subprocess.CompletedProcess[str]`` for a scripted fake
    runner — matches the ``TmuxRunner``/``TmuxInputRunner`` protocol's return
    type exactly, unlike a hand-rolled duck-typed stand-in."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Records every command it's asked to run and returns scripted results."""

    def __init__(
        self,
        script: dict[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
        default: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.script = script or {}
        self.default = default or fake_completed(0, "", "")

    def __call__(self, cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return self.script.get(tuple(cmd), self.default)


class FakeInputRunner:
    def __init__(self, default: subprocess.CompletedProcess[str] | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.default = default or fake_completed(0, "", "")

    def __call__(self, cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, payload))
        return self.default

# --- (3) transcript-grep verification against a synthetic fixture --------


def test_transcript_confirms_nudge_finds_marker(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text(user_turn_jsonl("please check lane f3 status now"), encoding="utf-8")

    confirmed, path = transcript_confirms_nudge(
        "please check lane f3 status now",
        projects_root=projects_root,
        now_ts=time.time(),
    )
    assert confirmed is True
    assert path == transcript


def test_transcript_confirms_nudge_rejects_marker_with_no_turn_start(tmp_path: Path) -> None:
    """A user-role record carrying the marker with no later assistant/tool
    record is not confirmation -- the paste may have landed with nothing
    ever picking it up."""
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text(user_turn_jsonl("please check lane f3 status now", with_followup=False), encoding="utf-8")

    confirmed, path = transcript_confirms_nudge(
        "please check lane f3 status now",
        projects_root=projects_root,
        now_ts=time.time(),
    )
    assert confirmed is False
    assert path is None


def test_transcript_confirms_nudge_rejects_generic_system_followup(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "check this lane"}}),
                json.dumps({"type": "system", "message": {"role": "system", "content": "turn metadata updated"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    confirmed, path = transcript_confirms_nudge("check this lane", projects_root=projects_root, now_ts=time.time())

    assert confirmed is False
    assert path is None


def test_transcript_confirms_nudge_requires_marker_and_activity_in_same_lane_transcript(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    lane_a = projects_root / "lane-a"
    lane_b = projects_root / "lane-b"
    lane_a.mkdir(parents=True)
    lane_b.mkdir(parents=True)
    (lane_a / "session.jsonl").write_text(user_turn_jsonl("check this lane", with_followup=False), encoding="utf-8")
    (lane_b / "session.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Working on it."}}) + "\n",
        encoding="utf-8",
    )

    confirmed, path = transcript_confirms_nudge("check this lane", projects_root=projects_root, now_ts=time.time())

    assert confirmed is False
    assert path is None


@pytest.mark.parametrize("activity_type", ["agent_message", "function_call", "function_call_output"])
def test_transcript_confirms_nudge_accepts_codex_agent_and_tool_envelopes(tmp_path: Path, activity_type: str) -> None:
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "codex-lane"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": "check this lane"},
                    }
                ),
                json.dumps({"type": "event_msg", "payload": {"type": activity_type, "message": "working"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    confirmed, path = transcript_confirms_nudge("check this lane", projects_root=projects_root, now_ts=time.time())

    assert confirmed is True
    assert path == transcript


def test_transcript_confirms_nudge_accepts_posttooluse_hook_delivery(tmp_path: Path) -> None:
    """A nudge a PostToolUse hook injected lands as a ``hook_success``
    attachment, not a user record — the delivered order text in its stdout
    JSON still confirms delivery."""
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    nudge = "please check lane f3 status now"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "attachment",
                        "sessionId": "abc123",
                        "attachment": {
                            "type": "hook_success",
                            "hook_name": "PostToolUse:chitra-order",
                            "stdout": json.dumps(
                                {
                                    "hookSpecificOutput": {
                                        "hookEventName": "PostToolUse",
                                        "additionalContext": nudge,
                                    }
                                }
                            ),
                        },
                    }
                ),
                json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Working on it."}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    confirmed, path = transcript_confirms_nudge(nudge, projects_root=projects_root, now_ts=time.time())

    assert confirmed is True
    assert path == transcript


def test_transcript_confirms_nudge_rejects_hook_output_without_context(tmp_path: Path) -> None:
    """Hook stdout that carries no additionalContext is ordinary hook output,
    not delivered input — the marker elsewhere in the record cannot confirm."""
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    nudge = "please check lane f3 status now"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "attachment",
                        "sessionId": "abc123",
                        "attachment": {
                            "type": "hook_success",
                            "stdout": json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse"}}),
                        },
                    }
                ),
                # The marker shows up only inside an assistant echo.
                json.dumps({"type": "assistant", "message": {"role": "assistant", "content": nudge}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    confirmed, path = transcript_confirms_nudge(nudge, projects_root=projects_root, now_ts=time.time())

    assert confirmed is False
    assert path is None


def test_transcript_confirms_nudge_rejects_marker_only_in_an_assistant_echo(tmp_path: Path) -> None:
    """A marker that only ever appears inside an assistant reply (an echo of
    the instruction back, not chitra's own paste) must not confirm delivery
    -- it was never persisted as a user-role record."""
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "status check"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "Got it: please check lane f3 status now"},
            }
        ),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    confirmed, path = transcript_confirms_nudge(
        "please check lane f3 status now",
        projects_root=projects_root,
        now_ts=time.time(),
    )
    assert confirmed is False
    assert path is None


def test_transcript_confirms_nudge_excludes_given_path(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text("marker-text-here", encoding="utf-8")

    confirmed, path = transcript_confirms_nudge(
        "marker-text-here",
        projects_root=projects_root,
        exclude_paths={transcript},
        now_ts=time.time(),
    )
    assert confirmed is False
    assert path is None


def test_transcript_confirms_nudge_expected_path_ignores_newer_unrelated_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound transcript without the marker must not fall back to a newer
    unrelated transcript that happens to contain the same marker."""
    projects_root = tmp_path / "projects"
    bound_dir = projects_root / "bound-lane"
    unrelated_dir = projects_root / "unrelated-lane"
    bound_dir.mkdir(parents=True)
    unrelated_dir.mkdir(parents=True)
    bound = bound_dir / "bound.jsonl"
    unrelated = unrelated_dir / "newer.jsonl"
    bound.write_text(user_turn_jsonl("a different nudge"), encoding="utf-8")
    unrelated.write_text(user_turn_jsonl("check the bound lane"), encoding="utf-8")
    newer = time.time() + 1
    os.utime(unrelated, (newer, newer))

    def unexpected_discovery() -> str:
        raise AssertionError("expected transcript verification must not glob")

    monkeypatch.setattr("chitra.dispatch.transcript_glob", unexpected_discovery)
    confirmed, path = transcript_confirms_nudge(
        "check the bound lane",
        projects_root=projects_root,
        expected_transcript_path=bound,
        now_ts=time.time(),
    )

    assert confirmed is False
    assert path is None


def test_transcript_confirms_nudge_expected_path_accepts_only_bound_match(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    bound_dir = projects_root / "bound-lane"
    wrong_dir = projects_root / "wrong-lane"
    bound_dir.mkdir(parents=True)
    wrong_dir.mkdir(parents=True)
    bound = bound_dir / "bound.jsonl"
    wrong = wrong_dir / "wrong.jsonl"
    bound.write_text(user_turn_jsonl("check the bound lane"), encoding="utf-8")
    wrong.write_text(user_turn_jsonl("a different nudge"), encoding="utf-8")

    confirmed, path = transcript_confirms_nudge(
        "check the bound lane",
        projects_root=projects_root,
        expected_transcript_path=bound,
        now_ts=time.time(),
    )
    assert confirmed is True
    assert path == bound

    confirmed, path = transcript_confirms_nudge(
        "check the bound lane",
        projects_root=projects_root,
        expected_transcript_path=wrong,
        now_ts=time.time(),
    )
    assert confirmed is False
    assert path is None

# --- (8) LaneLock: single-writer enforcement ------------------------------


def test_lane_lock_second_acquire_fails_non_blocking(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    lock_a = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    lock_b = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    assert lock_a.acquire(blocking=False) is True
    assert lock_b.acquire(blocking=False) is False
    lock_a.release()
    assert lock_b.acquire(blocking=False) is True
    lock_b.release()


def test_lane_lock_blocking_raises_after_timeout(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    lock_a = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    lock_b = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    assert lock_a.acquire(blocking=False) is True
    with pytest.raises(LaneLockError):
        lock_b.acquire(blocking=True, poll_seconds=0.01, timeout_seconds=0.05)
    lock_a.release()


def test_lane_lock_reclaims_stale_lock_from_dead_pid(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True)
    stale = lock_dir / "lane-examplehost_f3_0.0.lock"
    # A pid that is (almost certainly) not alive.
    stale.write_text(json.dumps({"pid": 999999, "session_ref": "examplehost:f3:0.0", "at": "x"}), encoding="utf-8")
    lock = LaneLock("examplehost:f3:0.0", lock_dir=lock_dir)
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_lane_lock_context_manager_releases_on_exit(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    session_ref = "examplehost:f3:0.0"
    with LaneLock(session_ref, lock_dir=lock_dir) as lock:
        assert lock.acquired is True
        other = LaneLock(session_ref, lock_dir=lock_dir)
        assert other.acquire(blocking=False) is False
    other2 = LaneLock(session_ref, lock_dir=lock_dir)
    assert other2.acquire(blocking=False) is True
    other2.release()




def test_pane_input_check_fails_closed_for_an_unrecognizable_pane_shape() -> None:
    check = pane_input_check(["Claude Code output", "❯ ", "status line"])

    assert check.ok is False
    assert check.reason == "blocked: unsubmitted operator draft detected"
    assert check.last_line == "status line"

def test_transcript_glob_is_relative_and_rejects_parent_or_absolute_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_TRANSCRIPT_GLOB", "runs/**/*.jsonl")
    assert transcript_glob() == "runs/**/*.jsonl"
    monkeypatch.setenv("CHITRA_TRANSCRIPT_GLOB", "../outside/*.jsonl")
    with pytest.raises(ValueError):
        transcript_glob()

def test_ssh_command_reads_the_configurable_host_key_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_SSH_STRICT_HOST_KEY_CHECKING", "yes")
    monkeypatch.setenv("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS", "7")
    command = ssh_command("example", "true")
    assert "StrictHostKeyChecking=yes" in command
    assert "ConnectTimeout=7" in command
    monkeypatch.setenv("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError):
        ssh_command("example", "true")


def test_ssh_command_can_use_the_narrow_grant_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_SSH_RUN_AS", "chitra")
    command = ssh_command("tophand", "chitra-tmux-capture monitor-probe:0.0")
    assert command[:6] == ["sudo", "-n", "-u", "chitra", "--", "ssh"]

    monkeypatch.setenv("CHITRA_SSH_RUN_AS", "../../root")
    with pytest.raises(ValueError, match="valid local account"):
        ssh_command("tophand", "true")

def test_the_remote_host_these_tests_use_is_never_the_local_one() -> None:
    """Keeps the governed-remote tests from testing the local path by accident.

    Without this, a test that names a real host reads as a pass on every
    machine except that one, and on that one it silently stops exercising the
    remote branch. Whichever way it then reports, it is not measuring what its
    name says.
    """
    assert not is_local_host(REMOTE_HOST)
    assert not is_local_host(REMOTE_HOST, set())

def test_governed_remote_capture_uses_fixed_visibility_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_REMOTE_LANE_GRANT", "codexman")
    runner = FakeRunner(
        default=fake_completed(
            0,
            json.dumps({"ok": True, "content": "old output\n› Use /skills to list available skills\n"}),
            "",
        )
    )

    captured = capture_dispatch_pane(REMOTE_HOST, "monitor-probe:0.0", runner=runner, local_extra=set())

    assert captured[-1] == "› Use /skills to list available skills"
    assert runner.calls[0][-1] == "chitra-tmux-capture monitor-probe:0:0"

# --- dispatch_to_tmux end-to-end (fake runner) ----------------------------


def test_dispatch_to_tmux_blocks_on_unsubmitted_draft() -> None:
    calls: list[list[str]] = []

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(
                0,
                "Claude Code output\n──────────────────────\n❯ operator draft\n"
                "──────────────────────\n⏵⏵ accept edits on (shift+tab to cycle)\n",
                "",
            )
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="localhost:s:0.0", nudge="hello")
    result = dispatch_to_tmux(order, runner=runner, local_extra={"localhost"})
    assert result.status == DispatchStatus.BLOCKED
    assert result.reason == "blocked: unsubmitted operator draft detected"
    assert calls == [["tmux", "capture-pane", "-e", "-p", "-t", "s:0.0", "-S", "-12"]]


def test_dispatch_to_tmux_rejects_unsupported_session_ref() -> None:
    order = DispatchOrder(order_id="o1", session_ref="not-three-parts", nudge="hello")
    result = dispatch_to_tmux(order)
    assert result.status == DispatchStatus.FAILED
    assert "unsupported" in result.reason


def test_dispatch_to_tmux_blocks_host_not_in_allowlist() -> None:
    order = DispatchOrder(order_id="o1", session_ref="untrusted-host:s:0.0", nudge="hello")
    result = dispatch_to_tmux(order, allowed_hosts=set(), local_extra=set())
    assert result.status == DispatchStatus.BLOCKED
    assert "not in allowlist" in result.reason

def test_dispatch_to_tmux_sends_a_clean_order_to_a_remote_host() -> None:
    """End-to-end: chitra's real deployment dispatches FROM one host (e.g.
    host-a) and delivers over ssh into another (e.g. otherhost). Every step
    -- copy-mode check, paste, and transcript verification -- must run
    against the remote host, never the local one, for a remote target to
    ever legitimately reach SENT."""

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "ssh", f"remote target must never shell out locally: {cmd}"
        assert cmd[-2] == "otherhost"
        remote_cmd = cmd[-1]
        if "capture-pane" in remote_cmd:
            return fake_completed(0, "ubuntu@otherhost:~$ ", "")
        if "display-message" in remote_cmd:
            return fake_completed(0, "0\n", "")
        if "paste-buffer" in remote_cmd:
            return fake_completed(0, "", "")
        if "find " in remote_cmd:
            return fake_completed(0, "1720000000 /remote/projects/foo/abc.jsonl\n", "")
        if "tail -c" in remote_cmd:
            return fake_completed(0, user_turn_jsonl("Stop editing main and open a PR."), "")
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="otherhost:f3:0.0", nudge="Stop editing main and open a PR.")
    result = dispatch_to_tmux(
        order,
        runner=runner,
        local_extra={"localhost"},
        allowed_hosts={"otherhost"},
        sleep=lambda _seconds: None,
    )
    assert result.status == DispatchStatus.SENT
    assert result.transcript_path == "/remote/projects/foo/abc.jsonl"

# --- structural transcript consumption (accepts a genuine turn start) ----


def test_transcript_confirms_nudge_accepts_a_real_user_record_plus_turn_start(tmp_path: Path) -> None:
    """The positive case this fix is for: a user-role record carrying the
    marker followed by a genuine assistant-role turn-start record."""
    projects_root = tmp_path / "projects"
    session_dir = projects_root / "some-project"
    session_dir.mkdir(parents=True)
    transcript = session_dir / "abc123.jsonl"
    transcript.write_text(user_turn_jsonl("diagnose the failing build"), encoding="utf-8")

    confirmed, path = transcript_confirms_nudge(
        "diagnose the failing build",
        projects_root=projects_root,
        now_ts=time.time(),
    )
    assert confirmed is True
    assert path == transcript

def test_ensure_nudge_submitted_fails_closed_when_codex_fallback_does_not_clear() -> None:
    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "• Ready for input\n\x1b[1m›\x1b[0m diagnose the failing build\n  ? for shortcuts", "")
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return fake_completed(0, "", "")

    ok, detail = ensure_nudge_submitted(
        "localhost",
        "f3:0.0",
        "f3",
        "diagnose the failing build",
        governed_remote=False,
        runner=runner,
        input_runner=input_runner,
        local_extra={"localhost"},
        tmux_socket=None,
        sleep=lambda _seconds: None,
    )
    assert ok is False
    assert detail == "submit-failed-composer-still-holds-text"

def test_ensure_nudge_submitted_never_sends_fallback_during_an_active_turn() -> None:
    """A composer that still shows the marker while active-turn chrome ("esc
    to interrupt") is visible must never get an ESC-shaped fallback byte --
    that would cancel a running turn instead of submitting stale text."""
    sent: list[list[str]] = []

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(
                0,
                "• Investigating rendering code (0s • esc to interrupt)\n\x1b[1m›\x1b[0m diagnose the failing build\n  ? for shortcuts",
                "",
            )
        sent.append(cmd)
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        sent.append(cmd)
        return fake_completed(0, "", "")

    ok, detail = ensure_nudge_submitted(
        "localhost",
        "f3:0.0",
        "f3",
        "diagnose the failing build",
        governed_remote=False,
        runner=runner,
        input_runner=input_runner,
        local_extra={"localhost"},
        tmux_socket=None,
    )
    assert ok is True
    assert "active-turn chrome visible" in detail
    assert sent == []

def test_ensure_nudge_submitted_governed_remote_fallback_reuses_lane_steer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The governed grant exposes no raw ``tmux send-keys`` verb -- the
    fallback must reuse the same ``chitra-lane-steer`` transport the paste
    itself went through, carrying the kitty-Enter bytes as its payload."""
    monkeypatch.setenv("CHITRA_REMOTE_LANE_GRANT", "codexman")
    input_calls: list[tuple[list[str], str]] = []

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        # Stuck through the submit grace window; cleared once the lane-steer
        # fallback has delivered the kitty-Enter payload.
        content = "ready\n› \n" if input_calls else "ready\n› diagnose the failing build\n"
        return fake_completed(0, json.dumps({"ok": True, "content": content, "truncated": False}), "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        input_calls.append((cmd, payload))
        return fake_completed(0, "", "")

    ok, detail = ensure_nudge_submitted(
        REMOTE_HOST,
        "monitor-probe:0.0",
        "monitor-probe",
        "diagnose the failing build",
        governed_remote=True,
        runner=runner,
        input_runner=input_runner,
        local_extra=set(),
        tmux_socket=None,
        sleep=lambda _seconds: None,
    )
    assert ok is True
    assert input_calls[0][0][-1] == "chitra-lane-steer monitor-probe"
    assert input_calls[0][1] == _CODEX_KITTY_ENTER_SEQUENCE

def test_dispatch_to_tmux_pane_evidence_ignores_a_marker_already_on_screen(tmp_path: Path) -> None:
    """A repeated canned nudge leaves its earlier copy in scrollback. When
    that copy was visible before the paste, the same pane after the paste
    cannot prove the new order landed, so it must not report SENT."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    pane_text = (
        "❯ diagnose the failing build\n"
        "✻ Cogitated for 0s\n"
        "Done.\n"
        "──────────────────────\n"
        "❯ \n"
        "──────────────────────\n"
        "⏵⏵ accept edits on (shift+tab to cycle)\n"
    )

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, pane_text, "")
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o2", session_ref="localhost:f3:0.0", nudge="diagnose the failing build")
    result = dispatch_to_tmux(
        order,
        runner=runner,
        input_runner=FakeInputRunner(),
        local_extra={"localhost"},
        projects_root=projects_root,
        sleep=lambda _seconds: None,
    )
    assert result.status == DispatchStatus.DELIVERY_UNCONFIRMED

def test_dispatch_to_tmux_reports_failed_when_composer_never_clears(tmp_path: Path) -> None:
    """End-to-end: a Codex pane that is idle at pre-dispatch time (so the new
    nudge is genuinely pasted), but whose composer still holds the pasted
    nudge even after the kitty-Enter submit fallback, must report FAILED,
    never SENT. Pre-existing drafts remain blocked before this post-paste
    check can run."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    idle_codex = "• Ready for input\n\x1b[1m›\x1b[0m \n  ? for shortcuts"
    stuck_codex = "• Ready for input\n\x1b[1m›\x1b[0m diagnose the failing build\n  ? for shortcuts"
    captures = {"n": 0}

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["tmux", "capture-pane"]:
            captures["n"] += 1
            # 1: pre-dispatch idle check (empty composer -- paste proceeds).
            # 2+: every capture after the real paste shows the composer still
            # holding the just-pasted nudge, even after the fallback fires.
            return fake_completed(0, idle_codex if captures["n"] == 1 else stuck_codex, "")
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="localhost:f3:0.0", nudge="diagnose the failing build")
    result = dispatch_to_tmux(
        order,
        runner=runner,
        input_runner=input_runner,
        local_extra={"localhost"},
        projects_root=projects_root,
        sleep=lambda _seconds: None,
    )
    assert result.status == DispatchStatus.FAILED
    assert result.reason == "submit-failed-composer-still-holds-text"

# --- optional real-tmux integration test (skipped if tmux is unavailable) -


@pytest.mark.skipif(not HAS_TMUX, reason="tmux binary not available in this sandbox")
def test_real_tmux_paste_and_pane_in_mode_roundtrip() -> None:
    session_name = f"pytest-chitra-{uuid.uuid4().hex[:8]}"
    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-x", "80", "-y", "24"], check=True)
    try:
        pane = f"{session_name}:0.0"
        assert pane_in_mode(pane) is False
        proc = paste_nudge_to_local_tmux(pane, "echo hi-from-chitra-test")
        assert proc.returncode == 0
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)


# --- mid-turn delivery: Claude records a busy-lane paste as queued_command --

_MID_TURN_NUDGE = "please run the lint step next and report the result"
_ASSISTANT_FOLLOWUP = {"type": "assistant", "message": {"role": "assistant", "content": "Working on it."}}


def _queued_command(prompt: str, *, origin: bool) -> dict[str, object]:
    attachment: dict[str, object] = {"type": "queued_command", "prompt": prompt, "commandMode": "prompt"}
    if origin:
        attachment["origin"] = {"kind": "human"}
    else:
        attachment["commandMode"] = "task-notification"
    return {"type": "attachment", "attachment": attachment}


def _confirms(tmp_path: Path, delivered: dict[str, object]) -> bool:
    session_dir = tmp_path / "projects" / "some-project"
    session_dir.mkdir(parents=True)
    rows = [delivered, _ASSISTANT_FOLLOWUP]
    (session_dir / "abc123.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    confirmed, _path = transcript_confirms_nudge(_MID_TURN_NUDGE, projects_root=tmp_path / "projects", now_ts=time.time())
    return confirmed


def test_transcript_confirms_a_mid_turn_queued_command_with_origin(tmp_path: Path) -> None:
    assert _confirms(tmp_path, _queued_command(_MID_TURN_NUDGE, origin=True)) is True


def test_transcript_confirms_needs_the_full_message_not_just_the_marker(tmp_path: Path) -> None:
    first_line_only = _MID_TURN_NUDGE + "\nsecond line the lane never received"
    session_dir = tmp_path / "projects" / "some-project"
    session_dir.mkdir(parents=True)
    (session_dir / "abc123.jsonl").write_text(user_turn_jsonl(_MID_TURN_NUDGE), encoding="utf-8")
    confirmed, _path = transcript_confirms_nudge(first_line_only, projects_root=tmp_path / "projects", now_ts=time.time())
    assert confirmed is False


@pytest.mark.parametrize(
    "delivered",
    [
        _queued_command(f"<task-notification><result>{_MID_TURN_NUDGE}</result></task-notification>", origin=False),
        {
            "type": "user",
            "message": {"role": "user", "content": f"<task-notification><result>{_MID_TURN_NUDGE}</result></task-notification>"},
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu-1", "content": _MID_TURN_NUDGE}],
            },
        },
        {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": f"Summary: {_MID_TURN_NUDGE}"}},
        {"type": "assistant", "message": {"role": "assistant", "content": f"You said: {_MID_TURN_NUDGE}"}},
    ],
    ids=["notification-attachment", "notification-user-record", "tool-result-echo", "compaction-summary", "assistant-echo"],
)
def test_transcript_confirms_rejects_marker_outside_operator_input(tmp_path: Path, delivered: dict[str, object]) -> None:
    assert _confirms(tmp_path, delivered) is False


def test_dispatch_to_tmux_qualifies_pane_with_session_before_any_tmux_call() -> None:
    """Regression test: capture/paste/etc must never receive a bare pane
    spec — on a host running more than one tmux session, that resolves
    against whichever session tmux considers 'current', not the session
    named in session_ref."""
    seen_targets: list[str] = []

    def runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if "-t" in cmd:
            seen_targets.append(cmd[cmd.index("-t") + 1])
        if cmd[:2] == ["tmux", "capture-pane"]:
            return fake_completed(0, "ubuntu@host:~$ ", "")
        return fake_completed(0, "", "")

    def input_runner(cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return fake_completed(0, "", "")

    order = DispatchOrder(order_id="o1", session_ref="localhost:f3:0.0", nudge="hello")
    dispatch_to_tmux(order, runner=runner, input_runner=input_runner, local_extra={"localhost"})

    assert seen_targets, "expected at least one -t target to have been recorded"
    assert all(t == "f3:0.0" for t in seen_targets), seen_targets

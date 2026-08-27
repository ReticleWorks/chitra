from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra.goals import EnrolledScopeImmutableError, GoalRecord, hold_goal, redirect_goal, upsert_goal
from chitra.lane_anchor import LaneLaunchRefused, LaneStartupFailed, _pane_pythonpath, ingestion_gate, start_lane
from chitra.lane_config import load_lanes
from chitra.recovery import get_lane_lifecycle


def _manifest(tmp_path):
    workdir = tmp_path / "worktree"
    if not workdir.exists():
        workdir.mkdir()
        subprocess.run(["git", "-C", str(workdir), "init"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(workdir), "config", "user.email", "lane-test@example.test"], check=True)
        subprocess.run(["git", "-C", str(workdir), "config", "user.name", "Lane Test"], check=True)
        (workdir / "README.md").write_text("lane test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(workdir), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(workdir), "commit", "-m", "initial"], check=True, capture_output=True, text=True)
    return {
        "lanes": [
            {
                "id": "alpha",
                "account": "alpha",
                "uid": 2109,
                "home": "/home/alpha",
                "workdir": str(workdir),
                "config_dir": "/home/alpha/.claude-alpha",
                "state_dir": str(tmp_path / "alpha-state"),
                "tmux_socket": str(tmp_path / "alpha.sock"),
                "tmux_session": "alpha",
                "credentials": {
                    "claude_credentials": "/home/alpha/.claude-alpha/.credentials.json",
                    "ssh_dispatch_key": str(tmp_path / "alpha-state/.ssh/id_ed25519_tophand"),
                },
                "enabled": True,
            }
        ]
    }


@pytest.fixture(autouse=True)
def _fast_startup_survival_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chitra.lane_anchor.STARTUP_SURVIVAL_INTERVAL_SECONDS", 0)


def test_lane_manifest_is_one_explicit_contract(tmp_path):
    import yaml

    manifest_path = tmp_path / "lanes.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(manifest_path)[0]

    assert lane.identifier == "alpha"
    assert lane.account == "alpha"
    assert lane.queue_dir == tmp_path / "alpha-state/queue"
    assert lane.tmux_socket == tmp_path / "alpha.sock"
    assert not hasattr(lane, "model")


def test_lane_manifest_rejects_model_at_any_depth(tmp_path):
    import yaml

    manifest = _manifest(tmp_path)
    manifest["lanes"][0]["credentials"]["model"] = "operator-selected"
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="model"):
        load_lanes(path)


def test_lane_anchor_selects_lane_socket_and_starts_only_a_shell(tmp_path):
    import yaml

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]
    upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref="tophand:alpha:0.0",
            goal="Implement the governed lane launch contract safely",
            done_when="All guarded lane launch probes pass locally",
            intent="Ensure every work lane remains observable and governed throughout execution",
            scope="Chitra lane launcher and lifecycle integration",
            source="task-file:lane-architecture",
            status="working",
            **enrollment_fields("All guarded lane launch probes pass locally"),
        ),
    )
    calls: list[list[str]] = []

    def runner(command):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0, "", "")

    control_socket = tmp_path / "chitra.sock"
    # The launch self-test spends a live agent call, which this test does not
    # need: it is checking the command the launcher builds, and the self-test
    # has its own tests in test_lane_permissions.py.
    assert start_lane(lane, runner=runner, socket_path=control_socket, self_test=False)
    assert calls[0] == [
        "runuser",
        "--user",
        "alpha",
        "--",
        "env",
        "HOME=/home/alpha",
        "CLAUDE_CONFIG_DIR=/home/alpha/.claude-alpha",
        "tmux",
        "-S",
        str(tmp_path / "alpha.sock"),
        "has-session",
        "-t",
        "alpha",
    ]
    expected_new_session = [
        "runuser",
        "--user",
        "alpha",
        "--",
        "env",
        "HOME=/home/alpha",
        "CLAUDE_CONFIG_DIR=/home/alpha/.claude-alpha",
        "tmux",
        "-S",
        str(tmp_path / "alpha.sock"),
        "new-session",
        "-d",
        "-e",
        "CHITRA_LANE_ID=alpha",
        "-e",
        "CHITRA_SESSION_REF=tophand:alpha:0.0",
        "-e",
        "CHITRA_PANE_TARGET=alpha:0.0",
        "-e",
        f"CHITRA_SOCKET_PATH={control_socket}",
        "-e",
        f"PYTHONPATH={_pane_pythonpath()}",
        "-s",
        "alpha",
        "-c",
        str(tmp_path / "worktree"),
        __import__("sys").executable,
        "-m",
        "chitra.pane_exec",
        "--",
        "claude",
        "--model",
        "sonnet",
        "--effort",
        "high",
        # A governed lane runs at full permissions. Without this flag the
        # lane falls back to the config directory's permissions.defaultMode
        # and its own work gets refused; see test_lane_permissions.py.
        "--dangerously-skip-permissions",
    ]
    assert calls[1][:-2] == expected_new_session
    assert calls[1][-2:] == ["--append-system-prompt-file", str(lane.state_dir / "session-setup.md")]
    assert calls[2:-1] == [calls[0]] * 5
    # The launch arms the transcript pipe before it writes the receipt, so a
    # lane is never recorded as governed while nothing is recording it.
    assert calls[-1][-5:] == [
        "pipe-pane",
        "-o",
        "-t",
        "alpha:0.0",
        f"cat >> {lane.state_dir / 'tmux-transcript.log'}",
    ]
    receipt = __import__("json").loads((lane.state_dir / "lane-launch.json").read_text())
    assert receipt["schema"] == "chitra.lane-launch.v2"
    assert receipt["session_ref"] == "tophand:alpha:0.0"
    assert receipt["goal_snapshot"]["source"] == "task-file:lane-architecture"
    assert "rate_limit_guard" in receipt["lifecycle"]
    assert receipt["effort"] == "high"
    assert receipt["identity_env"] == {
        "CHITRA_LANE_ID": "alpha",
        "CHITRA_SESSION_REF": "tophand:alpha:0.0",
        "CHITRA_PANE_ID": "runtime:TMUX_PANE",
        "CHITRA_PANE_TARGET": "alpha:0.0",
        "CHITRA_SOCKET_PATH": str(control_socket),
    }
    assert receipt["knowledge_bundle_sha256"] == lane.knowledge_bundle.sha256
    assert receipt["native_session_identity"]["lane_session_ref"] == "tophand:alpha:0.0"
    assert (lane.state_dir / "session-setup.md").exists()
    assert (lane.state_dir / "native-controls.json").exists()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required for the environment delivery proof")
def test_lane_identity_reaches_a_new_session_on_an_existing_tmux_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    account = pwd.getpwuid(os.geteuid()).pw_name
    manifest = _manifest(tmp_path)
    manifest["lanes"][0].update(
        {
            "account": account,
            "uid": os.geteuid(),
            "home": str(tmp_path / "home"),
            "workdir": str(tmp_path / "workdir"),
            "config_dir": str(tmp_path / "config"),
        }
    )
    for directory in (tmp_path / "home", tmp_path / "workdir", tmp_path / "config"):
        directory.mkdir()
    subprocess.run(["git", "-C", str(tmp_path / "workdir"), "init"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(tmp_path / "workdir"), "config", "user.email", "lane-test@example.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path / "workdir"), "config", "user.name", "Lane Test"], check=True)
    (tmp_path / "workdir" / "README.md").write_text("lane test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path / "workdir"), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path / "workdir"), "commit", "-m", "initial"], check=True, capture_output=True, text=True)
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    lane = load_lanes(path)[0]
    upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref="tophand:alpha:0.0",
            goal="Prove identity reaches a new pane on an existing tmux server",
            done_when="The pane writes every injected identity variable and its runtime pane ID",
            intent="Keep governed agent identity exact across tmux server reuse",
            scope="One disposable local tmux server",
            source="task-file:tmux-environment",
            status="working",
            **enrollment_fields("The pane writes every injected identity variable and its runtime pane ID"),
        ),
    )
    proof_path = tmp_path / "pane-environment.json"
    keys = [
        "CHITRA_LANE_ID",
        "CHITRA_SESSION_REF",
        "CHITRA_PANE_ID",
        "CHITRA_PANE_TARGET",
        "CHITRA_SOCKET_PATH",
        "TMUX_PANE",
    ]
    proof_code = (
        "import json,os,pathlib,time; "
        f"pathlib.Path({str(proof_path)!r}).write_text(json.dumps({{key: os.environ.get(key) for key in {keys!r}}})); "
        "time.sleep(5)"
    )
    monkeypatch.setattr("chitra.lane_anchor._agent_command", lambda *_args: [sys.executable, "-c", proof_code])
    monkeypatch.delenv("PYTHONPATH", raising=False)
    control_socket = tmp_path / "control.sock"
    subprocess.run(
        ["tmux", "-S", str(lane.tmux_socket), "new-session", "-d", "-s", "keeper", "sleep 30"],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        # This test replaces the agent command outright with a Python probe, so
        # the full-permission check and the live self-test have nothing real to
        # measure here. Both are covered in test_lane_permissions.py.
        assert start_lane(lane, socket_path=control_socket, self_test=False)
        deadline = time.monotonic() + 5.0
        while not proof_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert proof_path.exists()
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        assert proof == {
            "CHITRA_LANE_ID": "alpha",
            "CHITRA_SESSION_REF": "tophand:alpha:0.0",
            "CHITRA_PANE_ID": proof["TMUX_PANE"],
            "CHITRA_PANE_TARGET": "alpha:0.0",
            "CHITRA_SOCKET_PATH": str(control_socket),
            "TMUX_PANE": proof["TMUX_PANE"],
        }
        assert proof["TMUX_PANE"].startswith("%")
    finally:
        subprocess.run(
            ["tmux", "-S", str(lane.tmux_socket), "kill-server"],
            check=False,
            capture_output=True,
            text=True,
        )


def test_lane_launch_refuses_without_passing_ingestion_record(tmp_path):
    import yaml

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]

    with pytest.raises(LaneLaunchRefused, match="no chitra-goals ingestion record"):
        ingestion_gate(lane)


def test_lane_launch_refuses_legacy_record_without_interview_receipt(tmp_path):
    import yaml

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]
    legacy = GoalRecord(
        session_ref="tophand:alpha:0.0",
        goal="Implement the governed lane launch contract safely",
        done_when="All guarded lane launch probes pass locally",
        intent="Ensure every work lane remains observable and governed throughout execution",
        scope="Chitra lane launcher and lifecycle integration",
        source="task-file:lane-architecture",
        status="working",
        now="waiting for enrollment",
        created_at="2026-08-21T12:00:00+00:00",
        updated_at="2026-08-21T12:00:00+00:00",
    )
    lane.state_dir.mkdir(parents=True)
    (lane.state_dir / "goals.json").write_text(
        json.dumps({"schema": "chitra.goals.v2", "updated_at": legacy.updated_at, "goals": [legacy.to_dict()]}),
        encoding="utf-8",
    )

    with pytest.raises(LaneLaunchRefused, match="interview_receipt is required"):
        ingestion_gate(lane)


def test_lane_startup_death_returns_temporary_failure_without_receipt(tmp_path, monkeypatch, capsys):
    import yaml

    from chitra import lane_anchor

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]
    upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref="tophand:alpha:0.0",
            goal="Retry a lane whose agent database is temporarily locked",
            done_when="No false receipt is written and the caller receives temporary failure",
            intent="Make governed lane startup retry behavior safe and explicit",
            scope="One disposable governed test lane",
            source="task-file:startup-death-test",
            status="working",
            **enrollment_fields("No false receipt is written and the caller receives temporary failure"),
        ),
    )
    results = iter(
        [
            subprocess.CompletedProcess([], 1, "", "can't find session: alpha"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "can't find session: alpha"),
        ]
    )

    with pytest.raises(LaneStartupFailed, match="no launch receipt was written; retry is safe"):
        start_lane(lane, runner=lambda _command: next(results))
    assert not (lane.state_dir / "lane-launch.json").exists()
    assert get_lane_lifecycle(lane.state_dir, "tophand:alpha:0.0") is None

    monkeypatch.setattr(lane_anchor, "start_lane", lambda *_args, **_kwargs: (_ for _ in ()).throw(LaneStartupFailed("locked")))
    assert lane_anchor.main(["--lanes-file", str(path), "--lane", "alpha", "start"]) == 75
    assert capsys.readouterr().err == "LaneStartupFailed: locked\n"


def test_trinity_uses_the_same_host_qualified_goal_convention(tmp_path):
    import yaml

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]
    upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref="trinity:alpha:0.0",
            goal="Prove Trinity shares governed lane launch semantics",
            done_when="The host-qualified goal passes the ingestion gate",
            intent="Keep the offline development host aligned with Tophand governance",
            scope="Trinity lane launch configuration only",
            source="task-file:trinity-parity",
            status="working",
            **enrollment_fields("The host-qualified goal passes the ingestion gate"),
        ),
    )

    assert ingestion_gate(lane, host="trinity").session_ref == "trinity:alpha:0.0"


def test_done_when_redirect_is_refused_and_original_contract_still_launches(tmp_path):
    import yaml

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]
    enrolled = upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref="tophand:alpha:0.0",
            goal="Implement the governed lane launch contract safely",
            done_when="All guarded lane launch probes pass locally",
            intent="Ensure every work lane remains observable and governed throughout execution",
            scope="Chitra lane launcher and lifecycle integration",
            source="task-file:lane-architecture",
            status="working",
            **enrollment_fields("All guarded lane launch probes pass locally"),
        ),
    )

    with pytest.raises(EnrolledScopeImmutableError, match="done_when is frozen"):
        redirect_goal(
            lane.state_dir,
            enrolled.session_ref,
            reason="operator revised the live acceptance condition",
            done_when="The redirected live launch and completion probes pass",
        )

    assert ingestion_gate(lane) == enrolled


def test_lane_launch_refuses_active_usage_pause(tmp_path):
    import yaml

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]
    upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref="tophand:alpha:0.0",
            goal="Implement the governed lane launch contract safely",
            done_when="All guarded lane launch probes pass locally",
            intent="Ensure every work lane remains observable and governed throughout execution",
            scope="Chitra lane launcher and lifecycle integration",
            source="task-file:lane-architecture",
            status="working",
            **enrollment_fields("All guarded lane launch probes pass locally"),
        ),
    )
    hold_goal(lane.state_dir, "tophand:alpha:0.0", reason="rate-limit:provider-window")

    with pytest.raises(LaneLaunchRefused, match="usage-pause hold is active"):
        ingestion_gate(lane)


@pytest.mark.parametrize("model", ["sonnet", "opus"])
def test_governed_claude_model_selection(model, tmp_path):
    import yaml

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]
    upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref="tophand:alpha:0.0",
            goal="Implement the governed lane launch contract safely",
            done_when="All guarded lane launch probes pass locally",
            intent="Ensure every work lane remains observable and governed throughout execution",
            scope="Chitra lane launcher and lifecycle integration",
            source="task-file:lane-architecture",
            status="working",
            **enrollment_fields("All guarded lane launch probes pass locally"),
        ),
    )
    calls = []
    def runner(command):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0, "", "")
    assert start_lane(lane, backend="claude", model=model, effort="max", runner=runner, self_test=False)
    new_session = next(command for command in calls if "new-session" in command)
    assert new_session[-8:-2] == [
        "claude",
        "--model",
        model,
        "--effort",
        "max",
        "--dangerously-skip-permissions",
    ]
    assert new_session[-2:] == ["--append-system-prompt-file", str(lane.state_dir / "session-setup.md")]


def test_governed_codex_effort_is_explicit_and_receipted(tmp_path, monkeypatch: pytest.MonkeyPatch):
    import json

    import yaml

    manifest = _manifest(tmp_path)
    manifest["lanes"][0]["home"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    inherited_codex_home = tmp_path / "existing-codex-home"
    monkeypatch.setenv("CODEX_HOME", str(inherited_codex_home))
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    lane = load_lanes(path)[0]
    upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref="tophand:alpha:0.0",
            goal="Exercise one explicit governed Codex model route",
            done_when="The command and receipt name model and effort",
            intent="Keep every governed model routing choice explicit and auditable",
            scope="One disposable governed test lane",
            source="task-file:codex-effort-test",
            status="working",
            **enrollment_fields("The command and receipt name model and effort"),
        ),
    )
    calls = []

    def runner(command):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0, "", "")

    assert start_lane(
        lane,
        backend="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        runner=runner,
        self_test=False,
    )
    new_session = next(command for command in calls if "new-session" in command)
    assert new_session[-8:-2] == [
        "codex",
        "--model",
        "gpt-5.6-sol",
        "--config",
        'model_reasoning_effort="xhigh"',
        # Full access for Codex, the other half of the standing order that a
        # governed lane never stops on an approval nobody is there to answer.
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    assert new_session[-2:] == ["--profile", "chitra-alpha"]
    assert "CODEX_HOME=" not in new_session
    assert "--profile" in new_session
    codex_config = (inherited_codex_home / "chitra-alpha.config.toml").read_text(encoding="utf-8")
    assert "developer_instructions" in codex_config
    assert "/goal Exercise one explicit governed Codex model route" in codex_config
    receipt = json.loads((lane.state_dir / "lane-launch.json").read_text())
    assert receipt["model"] == "gpt-5.6-sol"
    assert receipt["effort"] == "xhigh"


def test_governed_opencode_model_is_explicit_and_state_is_lane_local(tmp_path):
    import json

    import yaml

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]
    upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref="tophand:alpha:0.0",
            goal="Exercise one explicit governed OpenCode model route",
            done_when="The OpenCode command, model, and lane state roots are explicit",
            intent="Keep OpenCode sessions isolated, selectable, and auditable across every governed lane",
            scope="One disposable governed OpenCode lane",
            source="task-file:opencode-route-test",
            status="working",
            **enrollment_fields("The OpenCode command, model, and lane state roots are explicit"),
        ),
    )
    calls = []

    def runner(command):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0, "", "")

    assert start_lane(
        lane,
        backend="opencode",
        model="opencode/x-preview-f-free",
        effort="high",
        runner=runner,
        self_test=False,
    )
    new_session = next(command for command in calls if "new-session" in command)
    assert new_session[-3:] == ["opencode", "--model", "opencode/x-preview-f-free"]
    assert f"XDG_CONFIG_HOME={lane.config_dir / 'xdg'}" in new_session
    assert f"XDG_DATA_HOME={lane.state_dir / 'xdg-data'}" in new_session
    assert f"XDG_STATE_HOME={lane.state_dir / 'xdg-state'}" in new_session
    receipt = json.loads((lane.state_dir / "lane-launch.json").read_text())
    assert receipt["backend"] == "opencode"
    assert receipt["model"] == "opencode/x-preview-f-free"


def test_same_account_launch_does_not_require_runuser(tmp_path, monkeypatch):
    import yaml

    manifest = _manifest(tmp_path)
    manifest["lanes"][0]["uid"] = 1000
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    lane = load_lanes(path)[0]
    monkeypatch.setattr("chitra.lane_anchor.os.geteuid", lambda: 1000)
    from chitra.lane_anchor import _run_as_lane

    command = _run_as_lane(lane, ["tmux", "list-sessions"])
    assert command[:3] == ["env", "HOME=/home/alpha", "CLAUDE_CONFIG_DIR=/home/alpha/.claude-alpha"]
    assert "runuser" not in command


def test_shared_dispatch_wrapper_uses_only_enabled_lane_roots(tmp_path, monkeypatch):
    import yaml

    manifest = _manifest(tmp_path)
    disabled = dict(manifest["lanes"][0])
    disabled["id"] = "disabled"
    disabled["account"] = "disabled"
    disabled["uid"] = 2110
    disabled["tmux_session"] = "disabled"
    disabled["state_dir"] = str(tmp_path / "disabled-state")
    disabled["tmux_socket"] = str(tmp_path / "disabled.sock")
    disabled["enabled"] = False
    manifest["lanes"].append(disabled)
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    from chitra import dispatchd

    calls = []

    def fake_run_once(queue_dir, **kwargs):
        calls.append((Path(queue_dir), kwargs))
        return []

    monkeypatch.setattr(dispatchd, "run_once", fake_run_once)
    assert dispatchd.run_lanes_once(path) == {"alpha": []}
    assert calls[0][0] == tmp_path / "alpha-state/queue"
    assert calls[0][1]["tmux_socket"] == tmp_path / "alpha.sock"
    assert calls[0][1]["goals_root"] == tmp_path / "alpha-state"


def test_package_units_define_four_shared_daemons_and_one_anchor_template():
    package_root = Path(__file__).parents[1] / "packaging/systemd"
    shared = {
        "chitra-dispatchd.service": "chitra.dispatchd",
        "chitra-watchd.service": "chitra.watchd",
        "chitra-triaged.service": "chitra.triaged",
        "chitra-sweepd.service": "chitra.sweepd",
    }
    for filename, module in shared.items():
        content = (package_root / filename).read_text(encoding="utf-8")
        assert f"-m {module} --lanes-file /etc/chitra/lanes.yaml" in content
        assert "Restart=on-failure" in content
        assert "RestartSec=30s" in content
        assert "StartLimitIntervalSec=10min" in content
        assert "StartLimitBurst=5" in content

    anchor = (package_root / "chitra@.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/chitra/venv/bin/chitra-lane-anchor" in anchor
    assert "--lane %i --host tophand --backend claude --model sonnet start" in anchor
    assert "--model sonnet" in anchor

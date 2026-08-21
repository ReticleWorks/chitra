from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chitra.lane_anchor import backend_command, launch_lane, start_lane
from chitra.lane_config import load_lanes


def _manifest(tmp_path):
    return {
        "lanes": [
            {
                "id": "alpha",
                "account": "alpha",
                "uid": 2109,
                "home": "/home/alpha",
                "workdir": "/srv/chitra/lanes/alpha",
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


def test_lane_manifest_is_one_explicit_contract(tmp_path):
    import yaml

    manifest_path = tmp_path / "lanes.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(manifest_path)[0]

    assert lane.identifier == "alpha"
    assert lane.account == "alpha"
    assert lane.queue_dir == tmp_path / "alpha-state/queue"
    assert lane.tmux_socket == tmp_path / "alpha.sock"
    assert lane.backend == "shell"
    assert not hasattr(lane, "model")


def test_lane_manifest_rejects_model_at_any_depth(tmp_path):
    import yaml

    manifest = _manifest(tmp_path)
    manifest["lanes"][0]["credentials"]["model"] = "operator-selected"
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="model"):
        load_lanes(path)


def test_lane_manifest_accepts_only_allowlisted_backends(tmp_path):
    import yaml

    manifest = _manifest(tmp_path)
    manifest["lanes"][0]["backend"] = "opencode"
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    lane = load_lanes(path)[0]

    assert lane.backend == "opencode"
    assert backend_command(lane) == ("opencode",)

    manifest["lanes"][0]["backend"] = "sh -c 'rm -rf /'"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="backend must be one of"):
        load_lanes(path)


def test_lane_manifest_rejects_arbitrary_launcher_field(tmp_path):
    import yaml

    manifest = _manifest(tmp_path)
    manifest["lanes"][0]["command"] = ["opencode", "--model", "untrusted"]
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported fields: command"):
        load_lanes(path)


def test_lane_anchor_selects_lane_socket_and_starts_only_a_shell(tmp_path):
    import yaml

    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(_manifest(tmp_path)), encoding="utf-8")
    lane = load_lanes(path)[0]
    calls: list[list[str]] = []

    def runner(command):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0, "", "")

    assert start_lane(lane, runner=runner)
    assert calls == [
        [
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
        ],
        [
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
            "-s",
            "alpha",
            "-c",
            "/srv/chitra/lanes/alpha",
        ],
    ]


def test_lane_launcher_uses_fixed_opencode_argv_and_lane_xdg_roots(tmp_path):
    import yaml

    manifest = _manifest(tmp_path)
    manifest["lanes"][0]["backend"] = "opencode"
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    lane = load_lanes(path)[0]
    calls: list[list[str]] = []

    def runner(command):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0, "", "")

    assert launch_lane(lane, runner=runner)
    assert calls == [
        [
            "runuser",
            "--user",
            "alpha",
            "--",
            "env",
            "HOME=/home/alpha",
            "CLAUDE_CONFIG_DIR=/home/alpha/.claude-alpha",
            "XDG_CONFIG_HOME=/home/alpha/.claude-alpha/xdg",
            f"XDG_DATA_HOME={tmp_path / 'alpha-state/xdg-data'}",
            f"XDG_STATE_HOME={tmp_path / 'alpha-state/xdg-state'}",
            "tmux",
            "-S",
            str(tmp_path / "alpha.sock"),
            "has-session",
            "-t",
            "alpha",
        ],
        [
            "runuser",
            "--user",
            "alpha",
            "--",
            "env",
            "HOME=/home/alpha",
            "CLAUDE_CONFIG_DIR=/home/alpha/.claude-alpha",
            "XDG_CONFIG_HOME=/home/alpha/.claude-alpha/xdg",
            f"XDG_DATA_HOME={tmp_path / 'alpha-state/xdg-data'}",
            f"XDG_STATE_HOME={tmp_path / 'alpha-state/xdg-state'}",
            "tmux",
            "-S",
            str(tmp_path / "alpha.sock"),
            "new-session",
            "-d",
            "-s",
            "alpha",
            "-c",
            "/srv/chitra/lanes/alpha",
            "opencode",
        ],
    ]


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
    assert "--lane %i start" in anchor
    assert "model" not in anchor.lower()

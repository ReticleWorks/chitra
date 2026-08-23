"""Tests for bounded tmux calls and systemd watchdog integration."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from chitra.systemd_notify import notify_ready, notify_watchdog, watchdog_usec
from chitra.watchd import _run_command

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_run_command_timeout_is_a_loud_failed_result() -> None:
    result = _run_command(["sleep", "2"], timeout=0.05)

    assert result.returncode == 124
    assert result.stderr == "timed out after 0.05s"


def test_ready_and_watchdog_datagrams_reach_systemd_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_path = tmp_path / "notify.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(str(socket_path))
        receiver.settimeout(1)
        monkeypatch.setenv("NOTIFY_SOCKET", str(socket_path))
        monkeypatch.setenv("WATCHDOG_USEC", "6000000")
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))

        assert notify_ready() is True
        assert receiver.recv(128) == b"READY=1"
        assert notify_watchdog() is True
        assert receiver.recv(128) == b"WATCHDOG=1"


def test_watchdog_usec_must_be_positive() -> None:
    with pytest.raises(ValueError, match="WATCHDOG_USEC must be a positive integer"):
        watchdog_usec(env={"WATCHDOG_USEC": "0"})


def test_watchdog_for_another_pid_is_disabled() -> None:
    assert watchdog_usec(env={"WATCHDOG_USEC": "6000000", "WATCHDOG_PID": "999"}, pid=1000) is None


@pytest.mark.parametrize(
    ("unit_name", "watchdog_seconds"),
    [
        ("chitra-watchd.service", "15"),
        ("chitra-sweepd.service", "180"),
        ("chitra-triaged.service", "6"),
    ],
)
def test_daemon_units_enable_systemd_watchdog(unit_name: str, watchdog_seconds: str) -> None:
    unit = (REPO_ROOT / "packaging" / "systemd" / unit_name).read_text(encoding="utf-8")

    assert "Type=notify" in unit
    assert "NotifyAccess=main" in unit
    assert f"WatchdogSec={watchdog_seconds}" in unit


@pytest.mark.parametrize("module_name", ["watchd.py", "sweepd.py", "triaged.py"])
def test_daemon_loops_notify_ready_and_watchdog_without_heartbeat_files(module_name: str) -> None:
    source = (REPO_ROOT / "src" / "chitra" / module_name).read_text(encoding="utf-8")

    assert "notify_ready()" in source
    assert "notify_watchdog()" in source
    assert "write_heartbeat" not in source

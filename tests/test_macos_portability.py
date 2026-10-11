"""Host-pressure, rescue identity and socket contracts on native macOS."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra import load_shed, socket_api
from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import ManifestRepository
from chitra.detect.rescue import _observe_process_identity
from chitra.ownership_provider import request_json_line, serve_unix_json_lines
from chitra.policy_config import LoadPolicy
from chitra.socket_api import ApiRuntime, ControlServer, SocketClient, default_socket_path, portable_socket_path


def test_portable_pressure_and_long_socket_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "darwin":
        native = load_shed.sample_pressure()
        assert 0 <= native.mem_available_pct <= 100
        assert 0 <= native.cpu_some_avg60 <= 100
        print(f"Native pressure sample: {native}")
    policy = LoadPolicy()
    now = datetime.now(UTC)
    with monkeypatch.context() as patch:
        patch.setattr(load_shed.sys, "platform", "darwin")
        patch.delenv("CHITRA_SOCKET_PATH", raising=False)
        assert default_socket_path() == Path("/tmp") / f"chitra-{os.geteuid()}" / "chitra.sock"
        patch.setattr(load_shed.os, "cpu_count", lambda: 4)
        patch.setattr(load_shed.os, "getloadavg", lambda: (1.0, 1.0, 1.0))
        for available, level in ((7, 3), (14, 2), (24, 1), (40, 0)):
            patch.setattr(load_shed.subprocess, "run", lambda *a, value=available, **kw: subprocess.CompletedProcess(a, 0, str(value)))
            sample = load_shed.sample_pressure()
            first = load_shed.advance_load_state(None, host="test", sample=sample, policy=policy, now=now)
            assert first.load_level == 0
            second = load_shed.advance_load_state(first, host="test", sample=sample, policy=policy, now=now)
            assert second.load_level == level
            if level == 3:
                held = second
        patch.setattr(load_shed.os, "getloadavg", lambda: (40.0, 1.0, 1.0))
        cpu = load_shed.sample_pressure()
        assert cpu.cpu_some_avg60 == 90.0
        assert load_shed.pressure_level(cpu, policy) == 1
        assert not load_shed.pressure_is_clear(cpu, policy)
        patch.setattr(load_shed.os, "getloadavg", lambda: (0.0, 0.0, 0.0))
        clear = load_shed.sample_pressure()
        assert load_shed.pressure_is_clear(clear, policy)
        first = load_shed.advance_load_state(held, host="test", sample=clear, policy=policy, now=now)
        assert first.load_level == 3
        assert load_shed.advance_load_state(first, host="test", sample=clear, policy=policy, now=now).load_level == 0

    meminfo, memory, cpu_path = (tmp_path / name for name in ("meminfo", "memory", "cpu"))
    meminfo.write_text("MemTotal: 100 kB\nMemAvailable: 80 kB\n", encoding="utf-8")
    memory.write_text("some avg60=1.0\nfull avg60=0.0\n", encoding="utf-8")
    cpu_path.write_text("some avg60=2.0\n", encoding="utf-8")
    replay = load_shed.sample_pressure(meminfo_path=meminfo, memory_pressure_path=memory, cpu_pressure_path=cpu_path)
    assert replay == load_shed.PressureSample(80, 1, 0, 2)

    boundary = Path("/" + "s" * 102)
    assert len(os.fsencode(boundary)) + 1 == 104
    assert portable_socket_path(boundary) == boundary
    assert portable_socket_path(Path(str(boundary) + "s")) != Path(str(boundary) + "s")
    logical = tmp_path / ("é" * 60) / "control.sock"
    monkeypatch.setenv("CHITRA_SOCKET_PATH", str(logical))
    server = ControlServer(logical, ApiRuntime(AgentStatusBroker(tmp_path / "state", ManifestRepository())))
    try:
        server.start()
        assert server.socket_path == default_socket_path() == portable_socket_path(logical)
        assert portable_socket_path(server.socket_path) == server.socket_path
        assert len(os.fsencode(server.socket_path)) + 1 <= 104
        assert stat.S_IMODE(server.socket_path.stat().st_mode) == 0o600
        assert server.socket_path.stat().st_uid == os.geteuid()
        with SocketClient(logical) as client:
            assert client.request("long-path", "ping", {})["type"] == "pong"
        response = request_json_line(logical, {"id": "ownership-client", "method": "ping", "params": {}})
        assert isinstance(response, dict) and response["result"]["type"] == "pong"
        assert not logical.parent.exists()
    finally:
        server.shutdown()
    assert not server.socket_path.exists()


def test_portability_refuses_unknown_pressure_process_and_socket_owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(load_shed.sys, "platform", "darwin")
        for output in ("garbled", "nan", "-1", "101"):
            patch.setattr(load_shed.subprocess, "run", lambda *a, value=output, **kw: subprocess.CompletedProcess(a, 0, value))
            with pytest.raises(ValueError):
                load_shed.sample_pressure()

        def stalled(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired("sysctl", 5)

        patch.setattr(load_shed.subprocess, "run", stalled)
        with pytest.raises(ValueError, match="not observable"):
            load_shed.sample_pressure()
        patch.setattr(load_shed.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "40"))
        patch.setattr(load_shed.os, "cpu_count", lambda: None)
        with pytest.raises(ValueError, match="not observable"):
            load_shed.sample_pressure()

    with pytest.raises(RuntimeError, match="not observable"):
        _observe_process_identity(2_147_483_647)
    with pytest.raises(ValueError, match="NUL"):
        portable_socket_path(Path("bad\0socket"))
    logical = tmp_path / ("long-" * 25) / "control.sock"
    selected = portable_socket_path(logical)
    protected = tmp_path / "preserved.txt"
    protected.write_text("preserve", encoding="utf-8")
    selected.symlink_to(protected)
    server = ControlServer(logical, ApiRuntime(AgentStatusBroker(tmp_path / "state", ManifestRepository())))
    try:
        with pytest.raises(RuntimeError, match="non-socket"):
            server.bind()
        assert selected.is_symlink() and protected.read_text(encoding="utf-8") == "preserve"
    finally:
        selected.unlink()

    server.start()
    try:
        other = ControlServer(logical, server.runtime)
        with pytest.raises(OSError, match="live server"):
            other.bind()
        with pytest.raises(OSError, match="live server"):
            serve_unix_json_lines(logical, lambda value: value)
        uid = os.geteuid()
        with monkeypatch.context() as patch:
            patch.setattr(socket_api.os, "geteuid", lambda: uid + 1)
            with pytest.raises(RuntimeError, match="another user's"):
                other.bind()
            with pytest.raises(PermissionError, match="owned by this user"), SocketClient(logical):
                pass
        with SocketClient(logical) as client:
            assert client.request("owner-preserved", "ping", {})["type"] == "pong"
    finally:
        server.shutdown()

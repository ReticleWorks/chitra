from __future__ import annotations

from pathlib import Path

import pytest

from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import ManifestRepository
from chitra.live_handoff import LiveHandoffError, perform_live_handoff
from chitra.socket_api import ApiRuntime, ControlServer, SocketClient


def test_live_handoff_transfers_status_and_socket_without_touching_panes(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    old_broker = AgentStatusBroker(state_dir, ManifestRepository())
    old_broker.report_agent(
        pane_id="%1", session_ref="host:lane:0.0", source="integration:test", agent="codex", state="working"
    )
    canonical = tmp_path / "chitra.sock"
    old_runtime = ApiRuntime(old_broker, pane_verifier=lambda _status: True, process_id=111)
    old_server = ControlServer(canonical, old_runtime)
    old_server.start()

    new_broker = AgentStatusBroker(state_dir, ManifestRepository())
    new_runtime = ApiRuntime(new_broker, pane_verifier=lambda _status: True, process_id=222)
    new_server = ControlServer(tmp_path / "chitra.sock.new", new_runtime)
    try:
        receipt = perform_live_handoff(
            canonical_socket=canonical,
            replacement_server=new_server,
            replacement_runtime=new_runtime,
        )
        with SocketClient(canonical) as client:
            pong = client.request("ping-new", "ping", {})
        assert pong["pid"] == 222
        assert receipt["status"] == "transferred"
        assert receipt["verified_pane_ids"] == ["%1"]
        assert new_broker.statuses()[0].state == "working"
        assert old_broker.statuses()[0].state == "working"
    finally:
        old_server.shutdown()
        new_server.shutdown()


def test_live_handoff_leaves_old_server_owner_when_pane_is_unverifiable(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    old_broker = AgentStatusBroker(state_dir, ManifestRepository())
    old_broker.report_agent(pane_id="%1", source="test", agent="codex", state="working")
    canonical = tmp_path / "chitra.sock"
    old_server = ControlServer(canonical, ApiRuntime(old_broker, pane_verifier=lambda _status: False, process_id=111))
    old_server.start()
    new_broker = AgentStatusBroker(state_dir, ManifestRepository())
    new_runtime = ApiRuntime(new_broker, pane_verifier=lambda _status: True, process_id=222)
    new_server = ControlServer(tmp_path / "chitra.sock.new", new_runtime)
    try:
        with pytest.raises(LiveHandoffError, match="UNKNOWN"):
            perform_live_handoff(
                canonical_socket=canonical,
                replacement_server=new_server,
                replacement_runtime=new_runtime,
            )
        with SocketClient(canonical) as client:
            pong = client.request("ping-old", "ping", {})
        assert pong["pid"] == 111
        assert not new_server.socket_path.exists()
        assert new_broker.statuses() == ()
    finally:
        old_server.shutdown()
        new_server.shutdown()


def test_replacement_verification_failure_aborts_and_thaws_the_source(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    old_broker = AgentStatusBroker(state_dir, ManifestRepository())
    old_broker.report_agent(pane_id="%1", source="test", agent="codex", state="working")
    canonical = tmp_path / "chitra.sock"
    old_server = ControlServer(canonical, ApiRuntime(old_broker, pane_verifier=lambda _status: True, process_id=111))
    old_server.start()
    new_broker = AgentStatusBroker(state_dir, ManifestRepository())
    new_runtime = ApiRuntime(new_broker, pane_verifier=lambda _status: False, process_id=222)
    new_server = ControlServer(tmp_path / "chitra.sock.new", new_runtime)
    try:
        with pytest.raises(LiveHandoffError, match="UNKNOWN"):
            perform_live_handoff(
                canonical_socket=canonical,
                replacement_server=new_server,
                replacement_runtime=new_runtime,
            )
        assert old_broker.report_agent(pane_id="%1", source="test", agent="codex", state="idle") is not None
        with SocketClient(canonical) as client:
            assert client.request("ping-old", "ping", {})["pid"] == 111
    finally:
        old_server.shutdown()
        new_server.shutdown()


def test_failed_handoff_can_retry_with_the_same_replacement(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    old_broker = AgentStatusBroker(state_dir, ManifestRepository())
    old_broker.report_agent(pane_id="%1", source="test", agent="codex", state="working")
    canonical = tmp_path / "chitra.sock"
    old_server = ControlServer(canonical, ApiRuntime(old_broker, pane_verifier=lambda _status: True, process_id=111))
    old_server.start()
    new_broker = AgentStatusBroker(state_dir, ManifestRepository())
    new_runtime = ApiRuntime(new_broker, pane_verifier=lambda _status: False, process_id=222)
    new_server = ControlServer(tmp_path / "chitra.sock.new", new_runtime)
    try:
        with pytest.raises(LiveHandoffError, match="UNKNOWN"):
            perform_live_handoff(
                canonical_socket=canonical,
                replacement_server=new_server,
                replacement_runtime=new_runtime,
            )
        assert new_broker.statuses() == ()

        new_runtime.pane_verifier = lambda _status: True
        receipt = perform_live_handoff(
            canonical_socket=canonical,
            replacement_server=new_server,
            replacement_runtime=new_runtime,
        )

        assert receipt["status"] == "transferred"
        assert new_broker.statuses()[0].pane_id == "%1"
        with SocketClient(canonical) as client:
            assert client.request("ping-retry", "ping", {})["pid"] == 222
    finally:
        old_server.shutdown()
        new_server.shutdown()


def test_post_commit_ownership_readback_failure_does_not_stop_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    old_broker = AgentStatusBroker(state_dir, ManifestRepository())
    old_broker.report_agent(pane_id="%1", source="test", agent="codex", state="working")
    canonical = tmp_path / "chitra.sock"
    old_server = ControlServer(canonical, ApiRuntime(old_broker, pane_verifier=lambda _status: True, process_id=111))
    old_server.start()
    new_broker = AgentStatusBroker(state_dir, ManifestRepository())
    new_runtime = ApiRuntime(new_broker, pane_verifier=lambda _status: True, process_id=222)
    new_server = ControlServer(tmp_path / "chitra.sock.new", new_runtime)
    original_read_text = Path.read_text

    def fail_only_post_commit_readback(path: Path, *args, **kwargs):
        if path.name == "server-ownership.json" and path.exists():
            raise OSError("injected post-commit readback failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_only_post_commit_readback)
    try:
        receipt = perform_live_handoff(
            canonical_socket=canonical,
            replacement_server=new_server,
            replacement_runtime=new_runtime,
        )
        assert receipt["status"] == "transferred"
        with SocketClient(canonical) as client:
            assert client.request("ping-after-commit", "ping", {})["pid"] == 222
    finally:
        old_server.shutdown()
        new_server.shutdown()

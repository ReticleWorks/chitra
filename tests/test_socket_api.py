from __future__ import annotations

import json
import socket
import stat
import threading
from pathlib import Path

import pytest

from chitra import socket_api
from chitra.agent_runtime import AgentStatusBroker, StatusRuntimeError
from chitra.agent_status import ManifestRepository
from chitra.api_protocol import EventSubscription, ProtocolError, parse_predicate
from chitra.socket_api import ApiRuntime, ControlServer, SocketClient


def _server(tmp_path: Path) -> tuple[AgentStatusBroker, ControlServer, Path]:
    broker = AgentStatusBroker(tmp_path / "state", ManifestRepository())
    socket_path = tmp_path / "chitra.sock"
    server = ControlServer(socket_path, ApiRuntime(broker, pane_verifier=lambda _status: True))
    server.start()
    return broker, server, socket_path


def test_ndjson_responses_echo_ids_and_schema_covers_all_wire_shapes(tmp_path: Path) -> None:
    _broker, server, socket_path = _server(tmp_path)
    try:
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            client.sendall(b'{"id":"req-1","method":"api.schema","params":{}}\n')
            response = json.loads(client.makefile("rb").readline())
        assert response["id"] == "req-1"
        schema = response["result"]["schema"]
        assert schema["transport"]["framing"] == "newline-delimited JSON"
        assert "success_response" in schema
        assert "error_response" in schema
        assert "emitted_event" in schema
        assert "subscription_event" in schema
    finally:
        server.shutdown()


def test_invalid_request_error_retains_available_request_id(tmp_path: Path) -> None:
    _broker, server, socket_path = _server(tmp_path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            reader = client.makefile("rb")
            client.sendall(b'{"id":"bad-1","method":"missing","params":{}}\n')
            response = json.loads(reader.readline())
        assert response == {
            "id": "bad-1",
            "error": {"code": "method_not_found", "message": "unknown method: missing"},
        }
    finally:
        server.shutdown()


def test_typed_subscription_filters_blocked_by_pane_and_echoes_subscription_id(tmp_path: Path) -> None:
    broker, server, socket_path = _server(tmp_path)
    received: list[dict[str, object]] = []

    def subscribe() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(socket_path))
            reader = client.makefile("rb")
            request = {
                "id": "sub-1",
                "method": "events.subscribe",
                "params": {
                    "subscriptions": [
                        {
                            "type": "pane.agent_status_changed",
                            "pane_id": "%1",
                            "agent_status": "blocked",
                        }
                    ]
                },
            }
            client.sendall(json.dumps(request).encode() + b"\n")
            received.append(json.loads(reader.readline()))
            received.append(json.loads(reader.readline()))

    thread = threading.Thread(target=subscribe)
    thread.start()
    try:
        while not received:
            threading.Event().wait(0.01)
        broker.report_agent(pane_id="%2", source="test", agent="codex", state="blocked")
        broker.report_agent(pane_id="%1", source="test", agent="codex", state="working")
        broker.report_agent(pane_id="%1", source="test", agent="codex", state="blocked")
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert received[0]["id"] == "sub-1"
        assert received[0]["result"]["type"] == "event_subscription"
        assert received[1]["id"] == "sub-1"
        assert received[1]["event"]["pane_id"] == "%1"
        assert received[1]["event"]["agent_status"] == "blocked"
    finally:
        server.shutdown()


def test_agent_wait_done_is_semantic_and_event_driven(tmp_path: Path) -> None:
    broker, server, socket_path = _server(tmp_path)
    result: list[dict[str, object]] = []

    def wait() -> None:
        with SocketClient(socket_path) as client:
            result.append(client.request("wait-1", "agent.wait", {"pane_id": "%1", "until": "done", "timeout_ms": 5000}))

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        broker.report_agent(
            pane_id="%1", session_ref="host:lane:0.0", source="integration:test", agent="codex", state="working"
        )
        broker.report_completion(pane_id="%1", session_ref="host:lane:0.0", agent="codex")
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert result[0]["type"] == "agent_wait"
        assert result[0]["pane"]["agent_status"] == "done"  # type: ignore[index]
    finally:
        server.shutdown()


def test_recursive_predicates_support_all_any_not_eq_in_and_exists(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path, ManifestRepository())
    event = broker.report_agent(
        pane_id="%1", session_ref="host:lane:0.0", source="test", agent="codex", state="blocked"
    )
    assert event is not None
    predicate = parse_predicate(
        {
            "op": "all",
            "filters": [
                {"op": "eq", "field": "agent_status", "value": "blocked"},
                {"op": "in", "field": "agent", "values": ["codex", "claude"]},
                {"op": "exists", "field": "session_ref"},
                {
                    "op": "not",
                    "filter": {"op": "any", "filters": [{"op": "eq", "field": "lane_id", "value": "other"}]},
                },
            ],
        }
    )
    subscription = EventSubscription(event_type="pane.agent_status_changed", where=predicate)
    assert subscription.matches(event)


def test_abandoned_handoff_expires_and_unfreezes_without_an_api_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket_api, "HANDOFF_TTL_SECONDS", 0.02)
    broker = AgentStatusBroker(tmp_path / "state", ManifestRepository())
    runtime = ApiRuntime(broker, pane_verifier=lambda _status: True, process_id=111)

    runtime.dispatch(
        "server.handoff.prepare",
        {"target_pid": 222, "state_dir": str(broker.state_dir.resolve()), "expected_protocol": 1},
    )
    with pytest.raises(StatusRuntimeError, match="frozen"):
        broker.report_agent(pane_id="%1", source="test", agent="codex", state="working")

    threading.Event().wait(0.1)
    assert broker.report_agent(pane_id="%1", source="test", agent="codex", state="working") is not None


def test_handoff_prepare_error_thaws_status_authority(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path / "state", ManifestRepository())
    runtime = ApiRuntime(broker, pane_verifier=lambda _status: True, process_id=111)
    runtime.ownership_path.parent.mkdir(parents=True)
    runtime.ownership_path.write_text("not json\n", encoding="utf-8")

    with pytest.raises(ProtocolError, match="UNKNOWN"):
        runtime.dispatch(
            "server.handoff.prepare",
            {"target_pid": 222, "state_dir": str(broker.state_dir.resolve()), "expected_protocol": 1},
        )
    assert broker.report_agent(pane_id="%1", source="test", agent="codex", state="working") is not None


def test_control_server_refuses_to_unlink_an_existing_owner_socket(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path / "state", ManifestRepository())
    socket_path = tmp_path / "chitra.sock"
    first = ControlServer(socket_path, ApiRuntime(broker))
    first.start()
    second = ControlServer(socket_path, ApiRuntime(AgentStatusBroker(tmp_path / "other", ManifestRepository())))
    try:
        with pytest.raises(OSError):
            second.start()
        with SocketClient(socket_path) as client:
            assert client.request("still-first", "ping", {})["type"] == "pong"
    finally:
        first.shutdown()
        second.shutdown()

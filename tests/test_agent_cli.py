from __future__ import annotations

import json
from pathlib import Path

from chitra.agent_cli import build_arg_parser, main
from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import ManifestRepository
from chitra.socket_api import MAX_WAIT_MS, ApiRuntime, ControlServer


def test_wait_cli_default_matches_server_maximum() -> None:
    args = build_arg_parser().parse_args(["wait", "--pane-id", "%1", "--until", "done"])

    assert args.timeout_ms == MAX_WAIT_MS


def test_offline_explain_and_schema_output(tmp_path: Path, capsys) -> None:
    screen = tmp_path / "screen.txt"
    screen.write_text("Allow command?\nYes\nNo\n", encoding="utf-8")
    assert main(["explain", "--file", str(screen), "--agent", "codex"]) == 0
    explained = json.loads(capsys.readouterr().out)
    assert explained["state"] == "blocked"
    assert explained["matched_rule"] == "permission_prompt"

    schema_path = tmp_path / "schema.json"
    assert main(["schema", "--output", str(schema_path)]) == 0
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["protocol_version"] == 1


def test_report_explain_and_wait_use_injected_identity_env(tmp_path: Path, monkeypatch, capsys) -> None:
    broker = AgentStatusBroker(tmp_path / "state", ManifestRepository())
    socket_path = tmp_path / "chitra.sock"
    server = ControlServer(socket_path, ApiRuntime(broker, pane_verifier=lambda _status: True))
    server.start()
    monkeypatch.setenv("CHITRA_SOCKET_PATH", str(socket_path))
    monkeypatch.setenv("CHITRA_PANE_ID", "%1")
    monkeypatch.setenv("CHITRA_SESSION_REF", "host:lane:0.0")
    try:
        assert main(["report", "--source", "integration:test", "--agent", "codex", "--state", "blocked"]) == 0
        reported = json.loads(capsys.readouterr().out)
        assert reported["changed"] is True

        assert main(["explain"]) == 0
        explained = json.loads(capsys.readouterr().out)
        assert explained["explain"]["authority"] == "integration"

        assert main(["wait", "--until", "blocked", "--timeout-ms", "100"]) == 0
        waited = json.loads(capsys.readouterr().out)
        assert waited["pane"]["agent_status"] == "blocked"
    finally:
        server.shutdown()

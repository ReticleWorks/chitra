"""Kept unit guard: ``AgentStatusBroker.report_completion`` has no CLI or
daemon caller in ``src/`` -- it is reachable only through the in-process broker
API, so the completion-ownership deny path stays under a unit test.
"""

from __future__ import annotations

from pathlib import Path

from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import ManifestRepository


def test_done_is_completion_owned_and_plain_idle_does_not_erase_it(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path, ManifestRepository())
    broker.observe(
        pane_id="%1",
        target="lane:0.0",
        session_ref="host:lane:0.0",
        lane_id="lane",
        detected_agent="codex",
        snapshot="Working... esc to interrupt\n",
        tmux_socket=None,
    )
    broker.report_completion(pane_id="%1", session_ref="host:lane:0.0", agent="codex")
    broker.observe(
        pane_id="%1",
        target="lane:0.0",
        session_ref="host:lane:0.0",
        lane_id="lane",
        detected_agent="codex",
        snapshot="› Add a task\n",
        tmux_socket=None,
    )

    assert broker.statuses()[0].state == "done"
    assert broker.statuses()[0].authority == "completion"

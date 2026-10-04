"""Tests for monitord's pane sensing kept in the wave-1 burn-down.

Kept:
- the two monitord-pass tests -- the daemon-surface end of sensing: a capped
  lane produces a foreground task carrying the resume time, and shadow mode
  records but never raises one;
- the ownership boundary -- a pane that is not bound to a known session_ref
  still feeds the status socket but must never write lane facts.

Removed: scripted-tmux sensing internals (transcript_pipe_fault units,
list-panes parsing, activity timestamp bookkeeping, dead-server tolerance).
The pipe-fault detection itself is still exercised end to end through the
monitord-pass tests below.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import ManifestRepository
from chitra.lane_activity import load_lane_activity
from chitra.lane_config import LaneCredentials, LaneSpec
from chitra.pane_sensing import (
    PaneSenseState,
    sense_lane_panes,
)

NOW = datetime(2026, 8, 16, 14, 15, tzinfo=UTC)
LANE = "tophand:atlas-v5:0.0"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HARD_CAP = (FIXTURES / "codex_weekly_hard_cap_20260814.txt").read_text(encoding="utf-8")
LANE_TIMEZONE = "America/New_York"

@pytest.fixture
def lane_timezone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", LANE_TIMEZONE)
    time.tzset()
    yield
    time.tzset()


def _governed_lane(root: Path) -> Path:
    """Create the lane-launch record that declares a lane governed."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "lane-launch.json").write_text("{}", encoding="utf-8")
    return root


def _transcript(root: Path, *, age_seconds: int, at: datetime = NOW) -> Path:
    _governed_lane(root)
    path = root / "tmux-transcript.log"
    path.write_text("lane output\n", encoding="utf-8")
    stamp = at.timestamp() - age_seconds
    os.utime(path, (stamp, stamp))
    return path

def _lane_spec(state_dir: Path, tmux_socket: Path) -> LaneSpec:
    return LaneSpec(
        identifier="atlas-v5",
        account="atlas-v5",
        uid=2109,
        home=state_dir,
        workdir=state_dir,
        config_dir=state_dir,
        state_dir=state_dir,
        tmux_socket=tmux_socket,
        tmux_session="atlas-v5",
        credentials=LaneCredentials(
            claude_credentials=state_dir / "credentials.json",
            ssh_dispatch_key=state_dir / "id_ed25519",
        ),
    )


def _sensing_runner(
    *,
    pane_line: str = "%1\tatlas-v5:0.0\t1\tcodex\t1\n",
    capture: str = "• Working (12s · esc to interrupt)\n›\n",
) -> tuple[list[list[str]], object]:
    """One fake tmux server answering list-panes and capture-pane."""
    calls: list[list[str]] = []

    def runner(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if "list-panes" in command:
            return subprocess.CompletedProcess(list(command), 0, pane_line, "")
        if "capture-pane" in command:
            return subprocess.CompletedProcess(list(command), 0, capture, "")
        return subprocess.CompletedProcess(list(command), 1, "", "unsupported")

    return calls, runner


def _sense(
    lane: LaneSpec,
    *,
    runner,
    alerts: list[tuple[str, str]] | None = None,
    now: datetime | None = None,
    state: PaneSenseState | None = None,
    broker: AgentStatusBroker | None = None,
) -> int:
    return sense_lane_panes(
        lane,
        broker=broker or AgentStatusBroker(lane.state_dir, ManifestRepository(lane.state_dir / "no-manifests")),
        state=state or PaneSenseState(),
        known_session_refs=(LANE,),
        activity_root=lane.state_dir,
        alert=None if alerts is None else lambda session_ref, text: alerts.append((session_ref, text)),
        runner=runner,
        now=now,
    )

def test_an_unmatched_pane_is_classified_but_not_recorded(tmp_path: Path) -> None:
    """Foreign panes still feed the socket; only bound panes write lane facts."""
    lane = _lane_spec(tmp_path / "lane-state", tmp_path / "tmux.sock")
    _calls, runner = _sensing_runner(pane_line="%9\tother-lane:0.0\t1\tclaude\t1\n")
    broker = AgentStatusBroker(lane.state_dir, ManifestRepository(tmp_path / "no-manifests"))
    alerts: list[tuple[str, str]] = []

    emitted = _sense(lane, runner=runner, broker=broker, alerts=alerts, now=NOW)

    assert emitted == 0
    [status] = broker.statuses()
    assert status.pane_id == "%9"
    assert status.session_ref is None
    assert load_lane_activity(lane.state_dir) == []



def test_monitord_pass_raises_a_foreground_task_for_a_capped_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane_timezone: None
) -> None:
    """End to end at the daemon surface: capped pane in, task on the goal out."""
    import yaml
    from _goal_fixtures import enrollment_fields

    from chitra.goals import GoalRecord, get_goal, upsert_goal
    from chitra.monitord import resolve_config, run_once

    state_dir = tmp_path / "lane-state"
    state_dir.mkdir(parents=True)
    _transcript(state_dir, age_seconds=5, at=datetime.now(UTC))
    upsert_goal(
        state_dir,
        GoalRecord(
            session_ref=LANE,
            goal="Keep the broker lane observable through provider caps",
            done_when="The cap is raised as a foreground task",
            intent="Prove pane sensing survived the daemon consolidation",
            scope="monitord pane sensing",
            source="task-file:pane-sensing",
            status="working",
            **enrollment_fields("The cap is raised as a foreground task"),
        ),
    )
    manifest = {
        "lanes": [
            {
                "id": "atlas-v5",
                "account": "atlas-v5",
                "uid": 2109,
                "home": str(state_dir),
                "workdir": str(state_dir),
                "config_dir": str(state_dir),
                "state_dir": str(state_dir),
                "tmux_socket": str(tmp_path / "atlas.sock"),
                "tmux_session": "atlas-v5",
                "credentials": {
                    "claude_credentials": str(state_dir / "credentials.json"),
                    "ssh_dispatch_key": str(state_dir / "id_ed25519"),
                },
                "enabled": True,
            }
        ]
    }
    lanes_file = tmp_path / "lanes.yaml"
    lanes_file.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    def runner(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if "list-panes" in command:
            return subprocess.CompletedProcess(list(command), 0, "%1\tatlas-v5:0.0\t1\tcodex\t1\n", "")
        if "capture-pane" in command:
            return subprocess.CompletedProcess(list(command), 0, HARD_CAP, "")
        return subprocess.CompletedProcess(list(command), 1, "", "unsupported")

    monkeypatch.setattr("chitra.dispatch.run_cmd", runner)
    config = resolve_config(
        state_dir=state_dir,
        lanes_file=lanes_file,
        shadow_mode=False,
        transcript_bindings_path=tmp_path / "no-bindings.json",
        findings_path=tmp_path / "findings.jsonl",
    )

    run_once(config)

    goal = get_goal(state_dir, LANE)
    assert goal is not None
    rate_limit_tasks = [task for task in goal.foreground_tasks if "rate-limit" in task.text]
    assert len(rate_limit_tasks) == 1
    task = rate_limit_tasks[0]
    assert task.kind == "investigate"
    assert task.source == "monitord"
    assert "2026-08-20T03:37:00Z" in task.text


def test_monitord_shadow_mode_logs_but_does_not_raise_a_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane_timezone: None
) -> None:
    """Shadow mode records the observation but never lands a task."""
    import yaml
    from _goal_fixtures import enrollment_fields

    from chitra.goals import GoalRecord, get_goal, upsert_goal
    from chitra.monitord import resolve_config, run_once

    state_dir = tmp_path / "lane-state"
    state_dir.mkdir(parents=True)
    _transcript(state_dir, age_seconds=5, at=datetime.now(UTC))
    upsert_goal(
        state_dir,
        GoalRecord(
            session_ref=LANE,
            goal="Keep the broker lane observable through provider caps",
            done_when="The cap is raised as a foreground task",
            intent="Prove pane sensing survived the daemon consolidation",
            scope="monitord pane sensing",
            source="task-file:pane-sensing",
            status="working",
            **enrollment_fields("The cap is raised as a foreground task"),
        ),
    )
    manifest = {
        "lanes": [
            {
                "id": "atlas-v5",
                "account": "atlas-v5",
                "uid": 2109,
                "home": str(state_dir),
                "workdir": str(state_dir),
                "config_dir": str(state_dir),
                "state_dir": str(state_dir),
                "tmux_socket": str(tmp_path / "atlas.sock"),
                "tmux_session": "atlas-v5",
                "credentials": {
                    "claude_credentials": str(state_dir / "credentials.json"),
                    "ssh_dispatch_key": str(state_dir / "id_ed25519"),
                },
                "enabled": True,
            }
        ]
    }
    lanes_file = tmp_path / "lanes.yaml"
    lanes_file.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    def runner(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if "list-panes" in command:
            return subprocess.CompletedProcess(list(command), 0, "%1\tatlas-v5:0.0\t1\tcodex\t1\n", "")
        if "capture-pane" in command:
            return subprocess.CompletedProcess(list(command), 0, HARD_CAP, "")
        return subprocess.CompletedProcess(list(command), 1, "", "unsupported")

    monkeypatch.setattr("chitra.dispatch.run_cmd", runner)
    config = resolve_config(
        state_dir=state_dir,
        lanes_file=lanes_file,
        shadow_mode=True,
        transcript_bindings_path=tmp_path / "no-bindings.json",
        findings_path=tmp_path / "findings.jsonl",
    )

    run_once(config)

    goal = get_goal(state_dir, LANE)
    assert goal is not None
    assert [task for task in goal.foreground_tasks if "rate-limit" in task.text] == []

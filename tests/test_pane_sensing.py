"""Tests for monitord's pane sensing — the checks that lived in watchd.

On 2026-08-15 an atlas-v5 respawn did not re-arm ``tmux pipe-pane``. The pane
stayed healthy, its transcript stopped growing at 13:15Z, and every file-based
liveness check that read that transcript was blind for twenty-five hours. The
pane alone cannot tell the two apart, which is why this reads tmux's own
``pane_pipe`` and the transcript's mtime rather than the screen.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import ManifestRepository
from chitra.lane_activity import load_lane_activity
from chitra.lane_config import LaneCredentials, LaneSpec
from chitra.pane_sensing import (
    PaneSenseState,
    list_session_panes,
    sense_lane_panes,
    transcript_pipe_fault,
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


def _iso(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def test_an_unarmed_pipe_is_a_fault_even_while_the_lane_is_busy(tmp_path: Path) -> None:
    """The atlas-v5 shape, measured live on 2026-08-16: pane_pipe was 0."""
    _transcript(tmp_path, age_seconds=90_000)

    reason = transcript_pipe_fault(
        lane_directory=tmp_path,
        pipe_armed=False,
        last_change_at=_iso(5),
        now=NOW,
    )

    assert "no pipe-pane running" in reason


def test_an_unarmed_pipe_is_a_fault_even_while_the_lane_is_quiet(tmp_path: Path) -> None:
    """An idle lane with a dead pipe is not fine; its next output is lost."""
    _transcript(tmp_path, age_seconds=90_000)

    reason = transcript_pipe_fault(
        lane_directory=tmp_path,
        pipe_armed=False,
        last_change_at=_iso(90_000),
        now=NOW,
    )

    assert "no pipe-pane running" in reason


def test_an_armed_pipe_writing_nowhere_is_a_fault(tmp_path: Path) -> None:
    _transcript(tmp_path, age_seconds=3_600)

    reason = transcript_pipe_fault(
        lane_directory=tmp_path,
        pipe_armed=True,
        last_change_at=_iso(10),
        now=NOW,
    )

    assert "has not grown for 3600s" in reason
    assert "the pane changed 10s ago" in reason


def test_a_quiet_lane_with_a_quiet_transcript_is_agreement_not_a_fault(tmp_path: Path) -> None:
    _transcript(tmp_path, age_seconds=3_600)

    reason = transcript_pipe_fault(
        lane_directory=tmp_path,
        pipe_armed=True,
        last_change_at=_iso(3_500),
        now=NOW,
    )

    assert reason == ""


def test_a_growing_transcript_is_healthy(tmp_path: Path) -> None:
    _transcript(tmp_path, age_seconds=5)

    reason = transcript_pipe_fault(
        lane_directory=tmp_path,
        pipe_armed=True,
        last_change_at=_iso(5),
        now=NOW,
    )

    assert reason == ""


def test_a_governed_lane_with_no_transcript_at_all_is_a_fault(tmp_path: Path) -> None:
    """The measured atlas-v5 shape on 2026-08-16: launch record, no transcript.

    Keying on the transcript's existence instead would have stayed silent on
    the exact lane this check was written for.
    """
    _governed_lane(tmp_path)

    reason = transcript_pipe_fault(
        lane_directory=tmp_path,
        pipe_armed=False,
        last_change_at=_iso(5),
        now=NOW,
    )

    assert "no transcript file" in reason


def test_an_ungoverned_lane_gets_no_opinion(tmp_path: Path) -> None:
    """The lane-launch record is the declaration; without it, no claim."""
    reason = transcript_pipe_fault(
        lane_directory=tmp_path,
        pipe_armed=False,
        last_change_at=_iso(5),
        now=NOW,
    )

    assert reason == ""


def test_an_unreadable_change_time_is_not_treated_as_a_fault(tmp_path: Path) -> None:
    _transcript(tmp_path, age_seconds=3_600)

    for last_change_at in ("", "tuesday", "2026-08-16T14:00:00"):
        assert (
            transcript_pipe_fault(
                lane_directory=tmp_path,
                pipe_armed=True,
                last_change_at=last_change_at,
                now=NOW,
            )
            == ""
        )


def test_list_session_panes_reads_the_pipe_state_from_tmux() -> None:
    def runner(_command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(_command),
            returncode=0,
            stdout="%1\tatlas-v5:0.0\t1\tclaude\t0\n%2\tatlas-v5:0.1\t1\tcodex\t1\n",
            stderr="",
        )

    panes = list_session_panes("atlas-v5", runner=runner)

    assert [(pane.target, pane.pipe_armed, pane.backend) for pane in panes] == [
        ("atlas-v5:0.0", False, "claude"),
        ("atlas-v5:0.1", True, "codex"),
    ]


def test_a_pane_line_without_the_pipe_field_reads_as_unarmed() -> None:
    """An older tmux, or a truncated line, must not read as healthy."""

    def runner(_command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(_command), returncode=0, stdout="%1\tatlas-v5:0.0\n", stderr="")

    assert list_session_panes("atlas-v5", runner=runner)[0].pipe_armed is False


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


def test_sensing_publishes_status_and_activity_facts(tmp_path: Path) -> None:
    """The rate-limit guard reads lane_activity.json; the socket reads the broker."""
    lane = _lane_spec(tmp_path / "lane-state", tmp_path / "tmux.sock")
    _transcript(lane.state_dir, age_seconds=5)
    calls, runner = _sensing_runner()
    broker = AgentStatusBroker(lane.state_dir, ManifestRepository(tmp_path / "no-manifests"))

    emitted = _sense(lane, runner=runner, broker=broker, now=NOW)

    assert emitted == 0
    [status] = broker.statuses()
    assert status.pane_id == "%1"
    assert status.session_ref == LANE
    assert status.lane_id == "atlas-v5"
    assert status.state == "working"
    [activity] = load_lane_activity(lane.state_dir)
    assert activity.session_ref == LANE
    assert activity.pane_id == "%1"
    assert activity.backend == "codex"
    assert activity.attached is True
    assert activity.last_change_at == NOW.isoformat()


def test_activity_last_change_tracks_semantic_transitions(tmp_path: Path) -> None:
    """A second identical capture advances last_seen_at, not last_change_at."""
    lane = _lane_spec(tmp_path / "lane-state", tmp_path / "tmux.sock")
    _transcript(lane.state_dir, age_seconds=5)
    _calls, runner = _sensing_runner()
    state = PaneSenseState()
    broker = AgentStatusBroker(lane.state_dir, ManifestRepository(tmp_path / "no-manifests"))
    later = NOW + timedelta(seconds=60)

    _sense(lane, runner=runner, broker=broker, state=state, now=NOW)
    _sense(lane, runner=runner, broker=broker, state=state, now=later)

    [activity] = load_lane_activity(lane.state_dir)
    assert activity.last_change_at == NOW.isoformat()
    assert activity.last_seen_at == later.isoformat()


def test_a_hard_rate_limit_banner_alerts_with_the_resume_time(tmp_path: Path, lane_timezone: None) -> None:
    """End to end: capped pane in, operator alert carrying the resume time out."""
    lane = _lane_spec(tmp_path / "lane-state", tmp_path / "tmux.sock")
    _transcript(lane.state_dir, age_seconds=5)
    _calls, runner = _sensing_runner(capture=HARD_CAP)
    alerts: list[tuple[str, str]] = []

    emitted = _sense(lane, runner=runner, alerts=alerts, now=NOW)

    assert emitted == 1
    assert alerts[0][0] == LANE
    assert "rate-limit" in alerts[0][1]
    assert "2026-08-20T03:37:00Z" in alerts[0][1]


def test_a_transcript_pipe_fault_alerts_once_per_breakage(tmp_path: Path) -> None:
    """A dead pipe reports once, not once per pass, then re-reports on relapse."""
    lane = _lane_spec(tmp_path / "lane-state", tmp_path / "tmux.sock")
    _governed_lane(lane.state_dir)
    _calls, dead_runner = _sensing_runner(pane_line="%1\tatlas-v5:0.0\t1\tcodex\t0\n")
    state = PaneSenseState()
    broker = AgentStatusBroker(lane.state_dir, ManifestRepository(tmp_path / "no-manifests"))
    alerts: list[tuple[str, str]] = []

    assert _sense(lane, runner=dead_runner, state=state, broker=broker, alerts=alerts, now=NOW) == 1
    assert _sense(lane, runner=dead_runner, state=state, broker=broker, alerts=alerts, now=NOW) == 0
    assert "no transcript file" in alerts[0][1]

    _transcript(lane.state_dir, age_seconds=5)
    _calls2, armed_runner = _sensing_runner(pane_line="%1\tatlas-v5:0.0\t1\tcodex\t1\n")
    assert _sense(lane, runner=armed_runner, state=state, broker=broker, alerts=alerts, now=NOW) == 0

    os.remove(lane.state_dir / "tmux-transcript.log")
    assert _sense(lane, runner=dead_runner, state=state, broker=broker, alerts=alerts, now=NOW) == 1
    assert len(alerts) == 2


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


def test_sensing_survives_a_dead_tmux_server(tmp_path: Path) -> None:
    lane = _lane_spec(tmp_path / "lane-state", tmp_path / "tmux.sock")

    def runner(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(command), 1, "", "no server running")

    assert _sense(lane, runner=runner, now=NOW) == 0


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

"""Tests for transcript-pipe liveness.

On 2026-08-15 an atlas-v5 respawn did not re-arm ``tmux pipe-pane``. The pane
stayed healthy, its transcript stopped growing at 13:15Z, and every file-based
liveness check that read that transcript was blind for twenty-five hours. The
pane alone cannot tell the two apart, which is why this reads tmux's own
``pane_pipe`` and the transcript's mtime rather than the screen.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra.triaged import critical_hits, parse_event_line
from chitra.watchd import (
    Pane,
    lane_dir,
    list_panes,
    transcript_event_line,
    transcript_path,
    transcript_pipe_fault,
)

NOW = datetime(2026, 8, 16, 14, 15, tzinfo=UTC)
LANE = "tophand:atlas-v5:0.0"


def _governed_lane(root: Path) -> Path:
    """Create the lane-launch record that declares a lane governed."""
    directory = lane_dir(root, LANE)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "lane-launch.json").write_text("{}", encoding="utf-8")
    return directory


def _transcript(root: Path, *, age_seconds: int) -> Path:
    _governed_lane(root)
    path = transcript_path(root, LANE)
    path.write_text("lane output\n", encoding="utf-8")
    stamp = NOW.timestamp() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def _iso(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def test_transcript_path_follows_the_governed_lane_layout(tmp_path: Path) -> None:
    assert transcript_path(tmp_path, LANE) == tmp_path / "tophand" / "atlas-v5" / "tmux-transcript.log"


def test_an_unarmed_pipe_is_a_fault_even_while_the_lane_is_busy(tmp_path: Path) -> None:
    """The atlas-v5 shape, measured live on 2026-08-16: pane_pipe was 0."""
    _transcript(tmp_path, age_seconds=90_000)

    reason = transcript_pipe_fault(
        lane_directory=lane_dir(tmp_path, LANE),
        pipe_armed=False,
        last_change_at=_iso(5),
        now=NOW,
    )

    assert "no pipe-pane running" in reason


def test_an_unarmed_pipe_is_a_fault_even_while_the_lane_is_quiet(tmp_path: Path) -> None:
    """An idle lane with a dead pipe is not fine; its next output is lost."""
    _transcript(tmp_path, age_seconds=90_000)

    reason = transcript_pipe_fault(
        lane_directory=lane_dir(tmp_path, LANE),
        pipe_armed=False,
        last_change_at=_iso(90_000),
        now=NOW,
    )

    assert "no pipe-pane running" in reason


def test_an_armed_pipe_writing_nowhere_is_a_fault(tmp_path: Path) -> None:
    _transcript(tmp_path, age_seconds=3_600)

    reason = transcript_pipe_fault(
        lane_directory=lane_dir(tmp_path, LANE),
        pipe_armed=True,
        last_change_at=_iso(10),
        now=NOW,
    )

    assert "has not grown for 3600s" in reason
    assert "the pane changed 10s ago" in reason


def test_a_quiet_lane_with_a_quiet_transcript_is_agreement_not_a_fault(tmp_path: Path) -> None:
    _transcript(tmp_path, age_seconds=3_600)

    reason = transcript_pipe_fault(
        lane_directory=lane_dir(tmp_path, LANE),
        pipe_armed=True,
        last_change_at=_iso(3_500),
        now=NOW,
    )

    assert reason == ""


def test_a_growing_transcript_is_healthy(tmp_path: Path) -> None:
    _transcript(tmp_path, age_seconds=5)

    reason = transcript_pipe_fault(
        lane_directory=lane_dir(tmp_path, LANE),
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
        lane_directory=lane_dir(tmp_path, LANE),
        pipe_armed=False,
        last_change_at=_iso(5),
        now=NOW,
    )

    assert "no transcript file" in reason


def test_an_unenrolled_pane_gets_no_opinion(tmp_path: Path) -> None:
    """The lane-launch record is the declaration; without it, no claim."""
    lane_dir(tmp_path, LANE).mkdir(parents=True)

    reason = transcript_pipe_fault(
        lane_directory=lane_dir(tmp_path, LANE),
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
                lane_directory=lane_dir(tmp_path, LANE),
                pipe_armed=True,
                last_change_at=last_change_at,
                now=NOW,
            )
            == ""
        )


def test_list_panes_reads_the_pipe_state_from_tmux() -> None:
    def runner(_command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(_command),
            returncode=0,
            stdout="%1\tatlas-v5:0.0\t1\tclaude\t0\n%2\tgct-resilience:0.0\t1\tclaude\t1\n",
            stderr="",
        )

    panes = list_panes(runner=runner)

    assert [(pane.target, pane.pipe_armed) for pane in panes] == [
        ("atlas-v5:0.0", False),
        ("gct-resilience:0.0", True),
    ]


def test_a_pane_line_without_the_pipe_field_reads_as_unarmed() -> None:
    """An older tmux, or a truncated line, must not read as healthy."""

    def runner(_command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(_command), returncode=0, stdout="%1\tatlas-v5:0.0\n", stderr="")

    assert list_panes(runner=runner)[0].pipe_armed is False


def test_the_fault_reaches_triaged_as_critical(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, age_seconds=90_000)
    pane = Pane(pane_id="%7", target="atlas-v5:0.0", backend="claude", pipe_armed=False)
    reason = transcript_pipe_fault(
        lane_directory=lane_dir(tmp_path, LANE), pipe_armed=False, last_change_at=_iso(5), now=NOW
    )

    parsed = parse_event_line(transcript_event_line(LANE, pane, transcript=transcript, reason=reason))

    assert parsed is not None
    _timestamp, lane_id, text = parsed
    assert lane_id == "%7"
    assert "TRANSCRIPT_PIPE_STALE" in text
    assert f"lane={LANE}" in text
    assert "pipe_armed=0" in text
    assert [rule for rule, _ in critical_hits(text)] == ["transcript_pipe_stale"]


def test_an_already_running_lane_still_gets_its_pipe_re_armed(tmp_path: Path) -> None:
    """The respawn path. Before this it returned having done nothing.

    A lane whose session survived but whose pipe did not is exactly the
    atlas-v5 shape, and the old early return is where it slipped through.
    """
    import yaml

    from chitra.goals import GoalRecord, upsert_goal
    from chitra.lane_anchor import start_lane
    from chitra.lane_config import load_lanes

    manifest = {
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
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
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

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        # returncode 0 on has-session: the session is already up.
        return subprocess.CompletedProcess(list(command), 0, "", "")

    created = start_lane(lane, runner=runner, socket_path=tmp_path / "chitra.sock")

    assert created is False
    assert [call for call in calls if "pipe-pane" in call], "a respawned lane must have its pipe re-armed"
    assert calls[-1][-4:] == ["-o", "-t", "alpha:0.0", f"cat >> {lane.state_dir / 'tmux-transcript.log'}"]


@pytest.mark.parametrize("stale_seconds", [1, 60, 900])
def test_the_staleness_ceiling_is_configurable(tmp_path: Path, stale_seconds: int) -> None:
    _transcript(tmp_path, age_seconds=stale_seconds + 10)

    reason = transcript_pipe_fault(
        lane_directory=lane_dir(tmp_path, LANE),
        pipe_armed=True,
        last_change_at=_iso(0),
        now=NOW,
        stale_seconds=stale_seconds,
    )

    assert "has not grown" in reason

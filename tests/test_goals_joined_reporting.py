"""Production-path tests for the joined-lane goals roster."""

from __future__ import annotations

from pathlib import Path

from _goal_fixtures import enrollment_fields
from _joined_report_fixtures import joined_report_record

from chitra.goals import GoalRecord, main, upsert_goal
from chitra.joined_lane import JoinedLaneStore


def test_roster_renders_canonical_joined_report_from_the_shared_state_root(tmp_path: Path, capsys) -> None:
    goal = upsert_goal(
        tmp_path,
        GoalRecord(
            session_ref="roundtop:ramble-build",
            goal="Ship the joined report through the production board.",
            done_when="The board shows the current roadmap and next check.",
            source="task-file:/tmp/report.md",
            status="working",
            **enrollment_fields("The board shows the current roadmap and next check."),
        ),
    )
    JoinedLaneStore(tmp_path).create(
        joined_report_record(
            lane_id=goal.lane_id,
            goal_id=goal.goal_id,
            session_ref=goal.session_ref,
        )
    )

    assert main(["roster", "--root", str(tmp_path)]) == 0

    rendered = capsys.readouterr().out
    assert "Goal: Ship the joined report through the production board." in rendered
    assert "Road map position: Run the proof (active)" in rendered
    assert "NOW: Running the proof" in rendered
    assert "NEXT: Publish the proof result" in rendered
    assert "CHECK: 2026-08-23T14:15:00+00:00 — Check for the proof result" in rendered
    assert "Provider: tophand — tophand-ramble" in rendered
    assert "Open problems:" in rendered
    assert "Resolved problems:" in rendered
    assert "Recovery action: checkpoint" in rendered
    assert "tmux" not in rendered.lower()
    assert "pid" not in rendered.lower()

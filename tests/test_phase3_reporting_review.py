"""Phase 3 contracts for the read-only Chitra reporting surface."""

from __future__ import annotations

from dataclasses import replace

from test_session_view import _record, _update

from chitra.goals import GoalRecord
from chitra.session_contract import RecoveryState
from chitra.session_view import build_joined_session_view


def _goal() -> GoalRecord:
    return GoalRecord(
        session_ref="tophand:lane-a-1",
        lane_id="lane-a",
        goal_id="goal-123",
        goal_version=2,
        goal="Ship the joined session report",
        done_when="The report shows current work and its next check",
        enrolled_done_when="The report shows current work and its next check",
        source="phase3-review-fixture",
        status="working",
        intent="Keep the lane moving without inventing progress",
        scope="Read-only reporting only",
        enrolled_at="2026-08-23T13:00:00+00:00",
        created_at="2026-08-23T13:00:00+00:00",
        updated_at="2026-08-23T14:00:00+00:00",
    )


def test_report_keeps_roadmap_position_problems_recovery_and_full_goal_binding() -> None:
    record = _record(update=_update()).model_copy(
        update={"recovery": RecoveryState(stage="relaunch", attempted_remedy="checkpoint")}
    )
    goal = _goal()
    before_record = record.to_dict()
    before_goal = goal.to_dict()

    report = build_joined_session_view(record, goal=goal).to_dict()

    assert report["plan_version"] == 2
    assert report["current_step"]["id"] == "implement"
    assert report["open_problems"][0]["id"] == "provider-latency"
    assert report["resolved_problems"][0]["id"] == "old-schema"
    assert report["recovery"]["attempted_remedy"] == "checkpoint"
    assert report["goal_snapshot"] == before_goal
    report["goal_snapshot"]["goal"] = "caller changed only its copy"
    assert record.to_dict() == before_record
    assert goal.to_dict() == before_goal


def test_report_does_not_fabricate_a_goal_snapshot_when_the_goal_is_unjoined() -> None:
    report = build_joined_session_view(_record(update=_update())).to_dict()

    assert report["goal_snapshot"] is None


def test_report_drops_a_goal_snapshot_with_mismatched_version() -> None:
    mismatched_goal = replace(_goal(), goal_version=3)

    report = build_joined_session_view(_record(update=_update()), goal=mismatched_goal).to_dict()

    assert report["goal_snapshot"] is None

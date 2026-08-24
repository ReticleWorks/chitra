"""Deterministic Phase 3 contracts for lane-authored reporting evidence."""

from __future__ import annotations

from pathlib import Path

from chitra.goals import GoalRecord
from chitra.joined_lane import JoinedLaneStore
from chitra.session_contract import (
    JoinedLaneRecord,
    LaneUpdate,
    PlanAssessment,
    Problem,
    ProblemHistoryEvent,
    ProviderCapabilities,
    ProviderIdentity,
    RoadmapStep,
)
from chitra.session_view import build_joined_session_view


def _record() -> JoinedLaneRecord:
    lane_id = "phase3-lane"
    goal_id = "phase3-goal"
    session_ref = "tophand:phase3-lane:0.0"
    update = LaneUpdate(
        lane_id=lane_id,
        goal_id=goal_id,
        session_ref=session_ref,
        goal_version=2,
        sequence=7,
        observed_at="2026-08-24T12:00:00+00:00",
        plan_version=3,
        revision_note="Add the provider readback step",
        steps=(
            RoadmapStep(id="launch", status="done", title="Launch the lane", owner="lane-manager"),
            RoadmapStep(id="readback", status="active", title="Read back the provider result", owner="lane-manager"),
            RoadmapStep(id="close", status="pending", title="Close the lane", owner="chitra"),
        ),
        current_action="Read back the provider result",
        next_action="Record the material result and close",
        problems=(
            Problem(
                id="lost-reply",
                summary="The provider reply was lost",
                owner="chitra",
                state="open",
                need="Reconcile the durable operation before retrying",
            ),
            Problem(
                id="old-target",
                summary="The first target was stale",
                owner="lane-manager",
                state="resolved",
                history=(
                    ProblemHistoryEvent(
                        event_id="old-target-resolved",
                        kind="resolved",
                        observed_at="2026-08-24T11:59:00+00:00",
                        note="Adopted the exact provider session",
                    ),
                ),
            ),
        ),
    )
    return JoinedLaneRecord(
        lane_id=lane_id,
        goal_id=goal_id,
        goal_version=2,
        session_ref=session_ref,
        provider=ProviderIdentity(
            kind="tophand",
            handle="tophand-phase3",
            provider_session_id=session_ref,
            instance_id="tophand-instance-phase3",
            generation=4,
            capabilities=ProviderCapabilities.from_supported(
                ("create_or_resume", "status", "send", "read_updates")
            ),
        ),
        current_update=update,
        plan_assessment=PlanAssessment(state="valid", reason="fixture plan is complete"),
    )


def _goal(record: JoinedLaneRecord) -> GoalRecord:
    return GoalRecord(
        session_ref=record.session_ref,
        lane_id=record.lane_id,
        goal_id=record.goal_id,
        goal_version=record.goal_version,
        goal="Keep the exact provider lane moving",
        done_when="The material result is recorded and the lane is closed",
        source="phase3-fixture",
        status="working",
        enrolled_done_when="The material result is recorded and the lane is closed",
        enrolled_at="2026-08-24T11:50:00+00:00",
        intent="Reconcile the provider without asking the user to intervene",
        scope="One logical lane and one physical provider session",
        created_at="2026-08-24T11:45:00+00:00",
        updated_at="2026-08-24T12:00:00+00:00",
        needs="",
    )


def test_lane_authored_roadmap_progress_and_problem_history_survive_restart(tmp_path: Path) -> None:
    """A fresh report must read the lane-authored snapshot, not infer it from activity."""

    store = JoinedLaneStore(tmp_path)
    store.create(_record())

    restarted = JoinedLaneStore(tmp_path).require("phase3-lane")
    report = build_joined_session_view(restarted).to_dict()

    assert report["schema"] == "chitra.joined-session-view.v1"
    assert report["goal_id"] == "phase3-goal"
    assert report["goal_version"] == 2
    assert report["update_sequence"] == 7
    assert report["current_step"]["id"] == "readback"
    assert report["progress"] == {
        "percentage": 33.33333333333333,
        "completed_steps": 1,
        "total_steps": 3,
        "reason": "available",
    }
    assert [problem["id"] for problem in report["open_problems"]] == ["lost-reply"]
    assert [problem["id"] for problem in report["resolved_problems"]] == ["old-target"]
    assert report["resolved_problems"][0]["resolution"] == "Adopted the exact provider session"


def test_phase3_report_binds_the_complete_authoritative_goal_snapshot() -> None:
    """The user-facing receipt must retain the full goal record, not five fields."""

    record = _record()
    goal = _goal(record)
    report = build_joined_session_view(record, goal=goal).to_dict()

    # This is deliberately a contract test for the missing unified binding.
    # The current view exposes a projection, but does not carry the immutable
    # GoalRecord snapshot required to compare a later reopen or close.
    assert report["goal_snapshot"] == goal.to_dict()

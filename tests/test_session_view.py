"""Deterministic read-only joined-session progress views."""

from __future__ import annotations

from dataclasses import dataclass

from chitra.session_contract import (
    InterventionEvidence,
    JoinedLaneRecord,
    LaneUpdate,
    PlanAssessment,
    Problem,
    ProblemHistoryEvent,
    ProviderCapabilities,
    ProviderIdentity,
    RoadmapStep,
)
from chitra.session_view import (
    build_joined_session_view,
    project_joined_session,
    render_joined_session_view,
)


@dataclass(frozen=True, slots=True)
class _Goal:
    goal_id: str = "goal-123"
    lane_id: str = "lane-a"
    goal: str = "Ship the joined session report"
    done_when: str = "The report shows current work and its next check"
    status: str = "working"


def _update(*, sequence: int = 3, plan_version: int = 2) -> LaneUpdate:
    return LaneUpdate(
        lane_id="lane-a",
        goal_id="goal-123",
        session_ref="tophand:lane-a-1",
        goal_version=2,
        sequence=sequence,
        observed_at="2026-08-23T14:00:00+00:00",
        plan_version=plan_version,
        revision_note="Split implementation from proof" if plan_version == 2 else "",
        steps=(
            RoadmapStep(id="design", status="done", title="Design the view", owner="lane"),
            RoadmapStep(id="implement", status="active", title="Implement the view", owner="lane"),
        ),
        current_action="Implementing the read-only projection",
        next_action="Run the deterministic view tests",
        problems=(
            Problem(
                id="provider-latency",
                summary="Provider status is slower than the check interval",
                owner="chitra",
                state="open",
                need="Refresh before retrying",
            ),
            Problem(
                id="old-schema",
                summary="The previous update used an old schema",
                owner="lane",
                state="resolved",
                history=(
                    ProblemHistoryEvent(
                        event_id="old-schema-resolved",
                        kind="resolved",
                        observed_at="2026-08-23T13:59:00+00:00",
                        note="Published a versioned update",
                    ),
                ),
            ),
        ),
    )


def _record(*, update: LaneUpdate | None = None) -> JoinedLaneRecord:
    return JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-123",
        goal_version=2,
        session_ref="tophand:lane-a-1",
        provider=ProviderIdentity(
            kind="tophand",
            handle="tophand-lane-a",
            capabilities=ProviderCapabilities.from_supported(("create_or_resume", "send", "read_updates")),
        ),
        current_update=update,
        plan_assessment=PlanAssessment(state="valid", reason="update passed validation"),
        last_intervention=InterventionEvidence(
            operation_id="nudge-1",
            action="Nudged the lane after a missed check",
            consumed=True,
            useful_work_resumed=True,
            observed_at="2026-08-23T14:01:00+00:00",
        ),
        next_check={
            "at": "2026-08-23T14:15:00+00:00",
            "reason": "Check for a material update",
            "wake_condition": "A newer lane update is observed",
        },
    )


def test_projection_joins_goal_roadmap_progress_owner_problems_and_next_check() -> None:
    view = build_joined_session_view(_record(update=_update()), goal=_Goal())

    assert view.goal == "Ship the joined session report"
    assert view.plan_version == 2
    assert view.plan_revision_note == "Split implementation from proof"
    assert view.progress is not None
    assert view.progress.percentage == 50.0
    assert view.current_step is not None and view.current_step.id == "implement"
    assert view.current_work == "Implementing the read-only projection"
    assert view.owner == "lane"
    assert tuple(problem.id for problem in view.open_problems) == ("provider-latency",)
    assert tuple(problem.id for problem in view.resolved_problems) == ("old-schema",)
    assert view.chitra_action == "Nudged the lane after a missed check"
    assert view.next_action == "Run the deterministic view tests"
    assert view.next_check is not None and view.next_check.wake_condition == "A newer lane update is observed"

    payload = view.to_dict()
    assert payload["schema"] == "chitra.joined-session-view.v1"
    assert payload["progress"] == {
        "percentage": 50.0,
        "completed_steps": 1,
        "total_steps": 2,
        "reason": "available",
    }
    assert payload["open_problems"][0]["id"] == "provider-latency"
    assert payload["resolved_problems"][0]["resolution"] == "Published a versioned update"


def test_projection_keeps_progress_unknown_when_plan_assessment_is_missing() -> None:
    record = _record(update=_update()).model_copy(update={"plan_assessment": PlanAssessment(state="missing")})
    view = build_joined_session_view(record)

    assert view.progress is not None
    assert view.progress.percentage is None
    assert view.progress.reason == "plan-missing"
    rendered = render_joined_session_view(view)
    assert "Progress: unavailable (plan-missing)." in rendered
    assert "<button" not in rendered
    assert "command" not in rendered.lower()


def test_projection_does_not_invent_work_or_progress_without_a_lane_update() -> None:
    view = project_joined_session(_record())

    assert view.progress is None
    assert view.plan_version is None
    assert view.current_work is None
    assert view.owner is None
    assert view.next_action is None
    rendered = render_joined_session_view(view, goal=_Goal())
    assert "Goal: Ship the joined session report" in rendered
    assert "Road map: unavailable" in rendered
    assert "Progress: unavailable (no lane update has been observed)." in rendered
    assert "Current work: unknown" in rendered


def test_all_blocked_projection_shows_blocked_position_without_fake_active_work() -> None:
    update = LaneUpdate(
        lane_id="lane-a",
        goal_id="goal-123",
        session_ref="tophand:lane-a-1",
        goal_version=2,
        sequence=3,
        observed_at="2026-08-23T14:00:00+00:00",
        plan_version=1,
        steps=(
            RoadmapStep(id="first", status="blocked", title="First", owner="lane"),
            RoadmapStep(id="second", status="blocked", title="Second", owner="chitra"),
        ),
        next_action="Wait for the durable check",
    )
    view = build_joined_session_view(_record(update=update))

    assert view.current_step is not None and view.current_step.id == "first"
    assert view.owner == "lane"
    assert view.current_work is None
    assert view.progress is not None and view.progress.percentage == 0.0

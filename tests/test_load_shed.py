"""Tests for deterministic host-pressure sampling and load-shed planning."""

from __future__ import annotations

from _goal_fixtures import enrollment_fields

from chitra.account_registry import RegistryEntry
from chitra.goals import GoalRecord
from chitra.lane_activity import LaneActivity
from chitra.load_shed import build_shed_candidates


def _goal(session_ref: str, status: str = "working") -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Keep the tracked lane within safe host capacity limits.",
        done_when="The lane finishes without unsafe host pressure.",
        source="task",
        status=status,  # type: ignore[arg-type]
        **enrollment_fields("The lane finishes without unsafe host pressure."),
    )


def test_load_shed_does_not_treat_opencode_as_a_claude_pause_target() -> None:
    goal = _goal("h:opencode:0.0")
    activity = LaneActivity(
        session_ref=goal.session_ref,
        pane_id="%1",
        last_change_at="2026-07-14T12:00:00+00:00",
        last_seen_at="2026-07-14T12:00:00+00:00",
        attached=True,
        backend="opencode",
    )

    assert build_shed_candidates([goal], activities=[activity], registry=[], host="h") == []

    registry = [RegistryEntry(tmux_session="opencode", session_id="s1", kind="opencode", account="", updated_at="")]
    assert build_shed_candidates([goal], activities=[], registry=registry, host="h") == []

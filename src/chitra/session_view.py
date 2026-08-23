"""Read-only projections of the canonical joined lane record.

The lane manager authors :class:`~chitra.session_contract.LaneUpdate` and
Chitra stores it in :class:`~chitra.session_contract.JoinedLaneRecord`.  This
module only joins those already-validated values for status and progress
surfaces.  It does not write state, send provider operations, or infer
progress from activity.

The optional goal argument is the existing goal-store record.  The joined
record keeps only its stable ``goal_id`` by design; a report can supply the
canonical goal record when it needs to show the user's goal text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal, Protocol, cast

from chitra.session_contract import (
    InterventionEvidence,
    JoinedLaneRecord,
    LaneLifecycle,
    LaneUpdate,
    NextCheck,
    PlanState,
    Problem,
    Progress,
    ProgressEvidence,
    ProviderIdentity,
    RecoveryState,
    RoadmapMilestone,
    RoadmapStep,
)

JOINED_SESSION_VIEW_SCHEMA: Literal["chitra.joined-session-view.v1"] = "chitra.joined-session-view.v1"
SESSION_VIEW_SCHEMA = JOINED_SESSION_VIEW_SCHEMA


class GoalProjection(Protocol):
    """The goal-store fields a read-only report may add to the projection."""

    goal_id: str
    lane_id: str
    goal: str
    done_when: str
    status: str


def _json_value(value: object) -> object:
    """Convert a canonical contract value to JSON-compatible data."""

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return value


def _problem_value(problem: Problem) -> dict[str, object]:
    """Return full history plus convenient current resolution fields."""

    value = cast(dict[str, object], _json_value(problem))
    value["resolution"] = problem.resolution
    value["reopen_event"] = problem.reopen_event
    return value


def _current_step(update: LaneUpdate | None) -> RoadmapStep | None:
    """Return the one step that explains the lane's current work.

    A recovery snapshot may have no active step when every unfinished step is
    blocked.  In that case the first blocked step in lane-authored order is
    the position shown by the report.  The choice is deterministic and does
    not claim that the blocked step is being worked.
    """

    if update is None:
        return None
    for step in update.all_steps:
        if step.status == "active":
            return step
    for step in update.all_steps:
        if step.status == "blocked":
            return step
    return None


def _goal_fields(record: JoinedLaneRecord, goal: GoalProjection | None) -> tuple[str | None, str | None, str | None]:
    if goal is None:
        return None, None, None
    for field_name, expected, actual in (
        ("goal_id", record.goal_id, goal.goal_id),
        ("lane_id", record.lane_id, goal.lane_id),
    ):
        if actual and actual != expected:
            raise ValueError(f"goal {field_name} does not match joined lane record")
    return goal.goal, goal.done_when, goal.status


@dataclass(frozen=True, slots=True)
class JoinedSessionView:
    """One immutable, report-ready view of a joined lane record.

    ``progress`` is ``None`` only when no lane update exists.  When an update
    exists but its plan is forming, stale, missing, invalid, or conflicting,
    ``progress`` is present with ``percentage=None`` and the contract's
    reason.  A report therefore never turns unknown progress into zero.
    """

    schema: Literal["chitra.joined-session-view.v1"]
    lane_id: str
    goal_id: str
    goal_version: int
    session_ref: str
    lifecycle: LaneLifecycle
    provider: ProviderIdentity
    physical_session_generation: int | None
    chitra_ownership_epoch: int
    update_cursor: str
    revision: int
    plan_state: PlanState
    plan_version: int | None
    plan_revision_note: str | None
    progress: Progress | None
    steps: tuple[RoadmapStep, ...]
    milestones: tuple[RoadmapMilestone, ...]
    current_step: RoadmapStep | None
    current_work: str | None
    owner: str | None
    problems: tuple[Problem, ...]
    open_problems: tuple[Problem, ...]
    resolved_problems: tuple[Problem, ...]
    chitra_action: str | None
    last_intervention: InterventionEvidence | None
    next_action: str | None
    next_check: NextCheck | None
    wake_condition: str | None
    observed_at: str | None
    update_sequence: int | None
    last_useful_progress: ProgressEvidence | None
    recovery: RecoveryState
    goal: str | None
    done_when: str | None
    goal_status: str | None

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON representation without any control fields."""

        return {
            "schema": self.schema,
            "lane_id": self.lane_id,
            "goal_id": self.goal_id,
            "goal_version": self.goal_version,
            "session_ref": self.session_ref,
            "lifecycle": self.lifecycle,
            "provider": _json_value(self.provider),
            "physical_session_generation": self.physical_session_generation,
            "chitra_ownership_epoch": self.chitra_ownership_epoch,
            "update_cursor": self.update_cursor,
            "revision": self.revision,
            "plan_state": self.plan_state,
            "plan_version": self.plan_version,
            "plan_revision_note": self.plan_revision_note,
            "progress": _json_value(self.progress) if self.progress is not None else None,
            "steps": [_json_value(step) for step in self.steps],
            "milestones": [_json_value(milestone) for milestone in self.milestones],
            "current_step": _json_value(self.current_step) if self.current_step is not None else None,
            "current_work": self.current_work,
            "owner": self.owner,
            "problems": [_problem_value(problem) for problem in self.problems],
            "open_problems": [_problem_value(problem) for problem in self.open_problems],
            "resolved_problems": [_problem_value(problem) for problem in self.resolved_problems],
            "chitra_action": self.chitra_action,
            "last_intervention": _json_value(self.last_intervention) if self.last_intervention is not None else None,
            "next_action": self.next_action,
            "next_check": _json_value(self.next_check) if self.next_check is not None else None,
            "wake_condition": self.wake_condition,
            "observed_at": self.observed_at,
            "update_sequence": self.update_sequence,
            "last_useful_progress": _json_value(self.last_useful_progress) if self.last_useful_progress is not None else None,
            "recovery": _json_value(self.recovery),
            "goal": self.goal,
            "done_when": self.done_when,
            "goal_status": self.goal_status,
        }


JoinedSessionProjection = JoinedSessionView
JoinedLaneView = JoinedSessionView


def build_joined_session_view(
    record: JoinedLaneRecord,
    *,
    goal: GoalProjection | None = None,
) -> JoinedSessionView:
    """Build one read-only view from a validated joined lane record."""

    update = record.current_update
    step = _current_step(update)
    problems = record.problems
    goal_text, done_when, goal_status = _goal_fields(record, goal)
    chitra_action: str | None = None
    last_intervention: InterventionEvidence | None = record.last_intervention
    if record.last_intervention is not None:
        chitra_action = record.last_intervention.action

    return JoinedSessionView(
        schema=JOINED_SESSION_VIEW_SCHEMA,
        lane_id=record.lane_id,
        goal_id=record.goal_id,
        goal_version=record.goal_version,
        session_ref=record.session_ref,
        lifecycle=record.lifecycle,
        provider=record.provider,
        physical_session_generation=record.physical_session_generation,
        chitra_ownership_epoch=record.chitra_ownership_epoch,
        update_cursor=record.update_cursor,
        revision=record.revision,
        plan_state=record.plan_assessment.state,
        plan_version=update.plan_version if update is not None else None,
        plan_revision_note=update.revision_note if update is not None else None,
        progress=record.progress(),
        steps=update.all_steps if update is not None else (),
        milestones=update.milestones if update is not None else (),
        current_step=step,
        current_work=update.current_action if update is not None and update.current_action else None,
        owner=step.owner or None if step is not None else None,
        problems=problems,
        open_problems=tuple(problem for problem in problems if problem.state == "open"),
        resolved_problems=tuple(problem for problem in problems if problem.state == "resolved"),
        chitra_action=chitra_action,
        last_intervention=last_intervention,
        next_action=update.next_action if update is not None else None,
        next_check=record.next_check,
        wake_condition=record.next_check.wake_condition if record.next_check is not None else None,
        observed_at=update.observed_at if update is not None else None,
        update_sequence=update.sequence if update is not None else None,
        last_useful_progress=record.last_useful_progress,
        recovery=record.recovery,
        goal=goal_text,
        done_when=done_when,
        goal_status=goal_status,
    )


def project_joined_session(
    source: JoinedLaneRecord | Mapping[str, object],
    *,
    goal: GoalProjection | None = None,
) -> JoinedSessionView:
    """Project either a validated record or a strict ``chitra.lanes.v1`` map."""

    record = source if isinstance(source, JoinedLaneRecord) else JoinedLaneRecord.from_dict(source)
    return build_joined_session_view(record, goal=goal)


def joined_session_view(
    source: JoinedLaneRecord | Mapping[str, object],
    *,
    goal: GoalProjection | None = None,
) -> JoinedSessionView:
    """Compatibility spelling for callers that name the result directly."""

    return project_joined_session(source, goal=goal)


def _progress_line(view: JoinedSessionView) -> str:
    if view.progress is None:
        return "Progress: unavailable (no lane update has been observed)."
    if view.progress.percentage is None:
        return f"Progress: unavailable ({view.progress.reason})."
    return f"Progress: {view.progress.percentage:g}% ({view.progress.completed_steps}/{view.progress.total_steps} non-dropped steps)."


def _problem_lines(label: str, problems: tuple[Problem, ...]) -> list[str]:
    if not problems:
        return [f"{label}: none recorded."]
    lines = [f"{label}:"]
    for problem in problems:
        detail = f"{problem.id}: {problem.summary} (owner: {problem.owner})"
        if problem.need:
            detail += f"; need: {problem.need}"
        if problem.resolution:
            detail += f"; resolution: {problem.resolution}"
        if problem.reopen_event:
            detail += f"; reopened: {problem.reopen_event}"
        lines.append(f"- {detail}")
    return lines


def render_joined_session_view(
    source: JoinedSessionView | JoinedLaneRecord | Mapping[str, object],
    *,
    goal: GoalProjection | None = None,
    fmt: Literal["text", "markdown"] = "text",
) -> str:
    """Render a deterministic, read-only text report.

    This renderer deliberately emits no links, forms, commands, or mutation
    controls.  It is suitable for an existing terminal or report surface;
    HTML clients can consume :meth:`JoinedSessionView.to_dict` as data.
    """

    if fmt not in ("text", "markdown"):
        raise ValueError(f"unknown joined-session view format: {fmt}")
    if isinstance(source, JoinedSessionView):
        view = source
        if goal is not None and view.goal is None:
            if goal.goal_id and goal.goal_id != view.goal_id:
                raise ValueError("goal goal_id does not match joined session view")
            if goal.lane_id and goal.lane_id != view.lane_id:
                raise ValueError("goal lane_id does not match joined session view")
            view = replace(view, goal=goal.goal, done_when=goal.done_when, goal_status=goal.status)
    else:
        view = project_joined_session(source, goal=goal)
    title = f"Lane {view.lane_id}"
    lines = [f"# {title}" if fmt == "markdown" else title]
    lines.append(f"Goal: {view.goal if view.goal is not None else 'unavailable (goal record not joined)'}")
    lines.append(f"Lifecycle: {view.lifecycle}")
    lines.append(f"Physical session generation: {view.physical_session_generation} (continuity context)")
    if view.plan_version is None:
        lines.append("Road map: unavailable (no lane update has been observed).")
    else:
        revision = f"; {view.plan_revision_note}" if view.plan_revision_note else ""
        lines.append(f"Road map: version {view.plan_version}, assessment {view.plan_state}{revision}")
    if view.current_step is None:
        lines.append("Road map position: unknown (no active or blocked step is reported).")
    else:
        position = view.current_step.title or view.current_step.id
        lines.append(f"Road map position: {position} ({view.current_step.status})")
    lines.append(_progress_line(view))
    if view.steps:
        lines.append("Steps:")
        for step in view.steps:
            title_text = step.title or step.id
            owner = f"; owner: {step.owner}" if step.owner else ""
            status = "done (lane-reported; Chitra verification not implied)" if step.status == "done" else step.status
            lines.append(f"- [{status}] {title_text} ({step.id}{owner})")
    else:
        lines.append("Steps: unavailable (no lane plan has been observed).")
    lines.append(f"NOW: {view.current_work or 'unknown (no current action reported).'}")
    lines.append(f"Owner: {view.owner or 'unknown (no current step owner reported).'}")
    lines.append(f"Provider: {view.provider.kind} — {view.provider.handle}")
    lines.extend(_problem_lines("Open problems", view.open_problems))
    lines.extend(_problem_lines("Resolved problems", view.resolved_problems))
    lines.append(f"Chitra action: {view.chitra_action or 'none recorded.'}")
    lines.append(f"Recovery action: {view.recovery.attempted_remedy or 'none recorded.'}")
    lines.append(f"NEXT: {view.next_action or 'unknown (no next action reported).'}")
    if view.next_check is None:
        lines.append("CHECK: unknown (no durable check is recorded).")
    else:
        wake = f"; wake condition: {view.next_check.wake_condition}" if view.next_check.wake_condition else ""
        lines.append(f"CHECK: {view.next_check.at} — {view.next_check.reason}{wake}")
    return "\n".join(lines)


render_joined_session = render_joined_session_view
render_progress_view = render_joined_session_view


__all__ = [
    "GoalProjection",
    "JOINED_SESSION_VIEW_SCHEMA",
    "SESSION_VIEW_SCHEMA",
    "JoinedLaneView",
    "JoinedSessionProjection",
    "JoinedSessionView",
    "build_joined_session_view",
    "joined_session_view",
    "project_joined_session",
    "render_joined_session",
    "render_joined_session_view",
    "render_progress_view",
]

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
    CloseArchiveResult,
    InterventionEvidence,
    JoinedLaneRecord,
    LaneLifecycle,
    LaneUpdate,
    NextCheck,
    PlanState,
    PendingProviderOperation,
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


def _pending_operation_value(operation: PendingProviderOperation | None) -> dict[str, object] | None:
    """Expose the pending action without exposing its provider payload."""

    if operation is None:
        return None
    return {
        "operation_id": operation.operation_id,
        "kind": operation.kind,
        "lane_id": operation.lane_id,
        "provider_handle": operation.provider_handle,
        "provider_session_id": operation.provider_session_id,
        "created_at": operation.created_at,
        "attempt": operation.attempt,
    }


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
    tactical_objective: str | None
    tactical_plan: tuple[str, ...]
    recovery_stage: str
    recovery_cycle_id: str | None
    recovery_attempt_count: int
    handoff_status: str
    handoff_id: str | None
    handoff_reference: str | None
    handoff_digest: str | None
    plan_assessment_reason: str
    plan_assessed_at: str | None
    goal: str | None
    done_when: str | None
    goal_status: str | None
    checkpoint_reference: str | None = None
    pending_operation: PendingProviderOperation | None = None
    close_evidence: CloseArchiveResult | None = None
    resume_state: str = "unknown"

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
            "tactical_plan": {
                "objective": self.tactical_objective,
                "steps": list(self.tactical_plan),
                "stage": self.recovery_stage,
                "attempt_count": self.recovery_attempt_count,
            },
            "reframe_progress": {
                "active": bool(self.tactical_objective or self.tactical_plan),
                "stage": self.recovery_stage,
                "attempt_count": self.recovery_attempt_count,
                "objective": self.tactical_objective,
                "steps": list(self.tactical_plan),
            },
            "handoff": {
                "status": self.handoff_status,
                "id": self.handoff_id,
                "reference": self.handoff_reference,
                "digest": self.handoff_digest,
            },
            "plan_assessment": {
                "state": self.plan_state,
                "assessed_at": self.plan_assessed_at,
                "reason": self.plan_assessment_reason,
            },
            "goal": self.goal,
            "done_when": self.done_when,
            "goal_status": self.goal_status,
            "checkpoint_reference": self.checkpoint_reference,
            "pending_operation": _pending_operation_value(self.pending_operation),
            "close_evidence": _json_value(self.close_evidence) if self.close_evidence is not None else None,
            "resume_state": self.resume_state,
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
    pending_resume = (
        record.pending_operation is not None
        and record.pending_operation.kind == "create_or_resume"
        and record.lifecycle == "inactive"
    )
    if pending_resume:
        resume_state = "resume pending"
    elif record.lifecycle == "active":
        resume_state = "active"
    elif record.last_close_result is not None and record.last_close_result.later_resume_supported is True:
        resume_state = "closed; same-session resume available"
    elif record.last_close_result is not None:
        resume_state = "closed"
    else:
        resume_state = "inactive"

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
        tactical_objective=record.recovery.execution_objective or None,
        tactical_plan=record.recovery.execution_plan,
        recovery_stage=record.recovery.stage,
        recovery_cycle_id=record.recovery.cycle_id,
        recovery_attempt_count=record.recovery.attempt_count,
        handoff_status=(
            "durable"
            if record.recovery.handoff_id and record.recovery.handoff_reference and record.recovery.handoff_digest
            else "not-recorded"
        ),
        handoff_id=record.recovery.handoff_id,
        handoff_reference=record.recovery.handoff_reference,
        handoff_digest=record.recovery.handoff_digest,
        plan_assessment_reason=record.plan_assessment.reason,
        plan_assessed_at=record.plan_assessment.assessed_at,
        goal=goal_text,
        done_when=done_when,
        goal_status=goal_status,
        checkpoint_reference=record.checkpoint_reference,
        pending_operation=record.pending_operation,
        close_evidence=record.last_close_result,
        resume_state=resume_state,
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
        assessment = f"; reason: {view.plan_assessment_reason}" if view.plan_assessment_reason else ""
        lines.append(f"Road map: version {view.plan_version}, assessment {view.plan_state}{revision}{assessment}")
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
    if view.tactical_objective or view.tactical_plan:
        lines.append(
            f"Reframe progress: stage {view.recovery_stage}, attempt {view.recovery_attempt_count}; "
            f"objective: {view.tactical_objective or 'none recorded.'}"
        )
        lines.append("Tactical steps:")
        lines.extend(f"- {step}" for step in view.tactical_plan)
    else:
        lines.append("Reframe progress: none recorded.")
    handoff = f"{view.handoff_status} ({view.handoff_id})" if view.handoff_id else view.handoff_status
    lines.append(f"Handoff: {handoff}")
    lines.append(f"Checkpoint: {view.checkpoint_reference or 'none recorded.'}")
    if view.pending_operation is None:
        lines.append("Pending recovery action: none.")
    else:
        lines.append(
            f"Pending recovery action: {view.pending_operation.operation_id} ({view.pending_operation.kind})"
        )
    if view.close_evidence is not None:
        lines.append(
            f"Close evidence: {view.close_evidence.state}; same provider thread: "
            f"{view.close_evidence.same_provider_thread}; later resume: "
            f"{view.close_evidence.later_resume_supported}"
        )
    lines.append(f"Resume state: {view.resume_state}")
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

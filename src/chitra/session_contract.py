"""Small, versioned data contracts shared by Chitra lane providers.

This module deliberately contains data and pure validation only.  It does not
own goals, transcript journals, delivery ledgers, or checkpoint files.  Those
stores remain the sources of truth named by the campaign contract; a joined
lane record contains references to their evidence instead of copying them.

The models are strict at the JSON boundary.  ``to_dict`` and ``from_dict`` are
the public persistence helpers for the two versioned documents in this module:
``chitra.session-update.v1`` and ``chitra.lanes.v1``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

SESSION_UPDATE_SCHEMA: Literal["chitra.session-update.v1"] = "chitra.session-update.v1"
LANE_RECORD_SCHEMA: Literal["chitra.lanes.v1"] = "chitra.lanes.v1"

# The aliases are intentionally public.  A caller should not have to repeat
# string literals when checking compatibility with the adapter.
SESSION_UPDATE_VERSION = SESSION_UPDATE_SCHEMA
LANE_RECORD_VERSION = LANE_RECORD_SCHEMA

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Timestamp = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

StepStatus = Literal["pending", "active", "blocked", "done", "dropped"]
PlanState = Literal["forming", "valid", "invalid", "missing", "stale", "conflicting"]
ProblemState = Literal["open", "resolved"]
FactState = Literal["known", "missing", "stale", "conflicting", "inaccessible"]
FactFreshness = Literal["current", "fresh", "stale", "unknown"]
ProviderKind = Literal["tophand", "amp"]
LaneLifecycle = Literal["active", "inactive", "closed", "archived"]
OperationKind = Literal[
    "create_or_resume",
    "status",
    "send",
    "read_updates",
    "checkpoint",
    "usage",
    "cancel_current_turn",
    "close",
]
OperationStatus = Literal["accepted", "consumed", "rejected", "unknown", "lost-response"]
RecoveryStage = Literal["none", "confirm", "nudge", "correct", "relaunch", "diagnostic", "waiting", "complete"]
CloseState = Literal["closed", "archived", "unknown", "failed"]
CapabilityName = Literal[
    "create_or_resume",
    "status",
    "send",
    "read_updates",
    "checkpoint",
    "usage",
    "cancel_current_turn",
    "close",
    "resume_after_close",
    "subagents",
    "parent_child_usage",
]


class ContractValidationError(ValueError):
    """Raised when a session contract would lose identity or evidence."""


def _timestamp(value: str, field_name: str) -> str:
    """Validate an ISO-8601 timestamp without rewriting the supplied value."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


def _timestamp_or_none(value: str | None, field_name: str) -> str | None:
    if value is not None:
        _timestamp(value, field_name)
    return value


def _immutable_json(value: object) -> object:
    """Turn JSON arrays into immutable tuples without coercing scalars."""

    if isinstance(value, list):
        return tuple(_immutable_json(item) for item in value)
    if isinstance(value, Mapping):
        return {key: _immutable_json(item) for key, item in value.items()}
    return value


class _ContractModel(BaseModel):
    """Strict immutable base for nested contract values."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    def to_dict(self) -> dict[str, object]:
        return cast(dict[str, object], self.model_dump(mode="json"))

    @classmethod
    def from_dict(cls, payload: object) -> Self:
        """Validate a nested JSON value without allowing scalar coercion."""

        if not isinstance(payload, Mapping):
            raise ValueError(f"{cls.__name__} document must be an object")
        return cls.model_validate(_immutable_json(payload), strict=True)


def _validate_payload(payload: object, *, schema: str, model: type[_ContractModel]) -> _ContractModel:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{schema} document must be an object")
    if payload.get("schema") != schema:
        raise ValueError(f"document is not a {schema} document")
    # JSON arrays are lists, while the in-memory contract uses tuples so a
    # caller cannot mutate a validated snapshot.  Convert only the container
    # shape; ``strict=True`` still rejects scalar coercions such as ``1`` to a
    # string or ``true`` to an integer.
    return model.model_validate(_immutable_json(payload), strict=True)


class LaneIdentity(_ContractModel):
    """Stable logical lane and goal reference plus the current physical session."""

    lane_id: Identifier
    goal_id: Identifier
    session_ref: Identifier
    goal_version: int = Field(ge=1)

    @field_validator("goal_version")
    @classmethod
    def reject_bool_version(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("goal_version must be an integer")
        return value


class RoadmapStep(_ContractModel):
    """One roadmap step whose ``id`` never changes within a plan lineage."""

    id: Identifier
    status: StepStatus
    title: str = ""
    owner: str = ""
    milestone_id: str | None = None


class RoadmapMilestone(_ContractModel):
    """Optional grouping metadata; steps reference it by ``milestone_id``."""

    id: Identifier
    title: str = ""


class NextCheck(_ContractModel):
    """A durable check time and the condition that wakes a waiting lane."""

    at: Timestamp
    reason: Text
    wake_condition: str | None = None

    @field_validator("at")
    @classmethod
    def validate_at(cls, value: str) -> str:
        return _timestamp(value, "next_check.at")


class Problem(_ContractModel):
    """One stable problem entry authored by the lane manager."""

    id: Identifier
    summary: Text
    owner: Text
    state: ProblemState
    need: str = ""
    resolution: str | None = None
    reopen_event: str | None = None

    @model_validator(mode="after")
    def validate_reopen_event(self) -> Self:
        if self.reopen_event is not None and self.state != "open":
            raise ValueError("reopen_event is only valid on an open problem")
        return self


class PlanAssessment(_ContractModel):
    """Chitra-owned assessment of whether a lane plan is usable."""

    state: PlanState = "missing"
    assessed_at: Timestamp | None = None
    reason: str = ""

    @field_validator("assessed_at")
    @classmethod
    def validate_assessed_at(cls, value: str | None) -> str | None:
        return _timestamp_or_none(value, "plan_assessment.assessed_at")


ChildRetention = Literal["retained", "released", "missing", "unknown"]


class ChildRosterEntry(_ContractModel):
    """Observed child identity and material-result evidence for usage roll-up."""

    child_id: Identifier
    parent_id: Identifier
    ancestry: tuple[Identifier, ...]
    retained_state: ChildRetention
    material_result: bool
    material_result_ref: Text | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if not self.ancestry or self.ancestry[0] != self.parent_id or self.ancestry[-1] != self.child_id:
            raise ValueError("child ancestry must start at parent_id and end at child_id")
        if self.material_result and self.material_result_ref is None:
            raise ValueError("material child result requires material_result_ref")
        return self


class LaneUpdate(_ContractModel):
    """A complete ``chitra.session-update.v1`` lane-authored snapshot."""

    schema: Literal["chitra.session-update.v1"] = SESSION_UPDATE_SCHEMA  # type: ignore[assignment]
    lane_id: Identifier
    goal_id: Identifier
    session_ref: Identifier
    goal_version: int = Field(ge=1)
    sequence: int = Field(ge=0)
    observed_at: Timestamp
    plan_version: int = Field(ge=1)
    revision_note: str = ""
    steps: tuple[RoadmapStep, ...] = ()
    milestones: tuple[RoadmapMilestone, ...] = ()
    current_action: str = ""
    next_action: Text
    problems: tuple[Problem, ...] = ()
    child_roster: tuple[ChildRosterEntry, ...] = ()
    operation_id: Identifier | None = None
    idempotency_key: Identifier | None = None

    @field_validator("goal_version", "sequence", "plan_version")
    @classmethod
    def reject_bool_numbers(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("version and sequence fields must be integers")
        return value

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return _timestamp(value, "observed_at")

    @staticmethod
    def _all_steps(values: Sequence[RoadmapStep], milestones: Sequence[RoadmapMilestone]) -> tuple[RoadmapStep, ...]:
        flattened = tuple(values)
        ids = [step.id for step in flattened]
        if len(ids) != len(set(ids)):
            raise ValueError("roadmap step IDs must be unique")
        return flattened

    @model_validator(mode="after")
    def validate_structure(self) -> Self:
        steps = self._all_steps(self.steps, self.milestones)
        milestone_ids = [milestone.id for milestone in self.milestones]
        if len(milestone_ids) != len(set(milestone_ids)):
            raise ValueError("roadmap milestone IDs must be unique")
        known_milestones = set(milestone_ids)
        if any(step.milestone_id is not None and step.milestone_id not in known_milestones for step in steps):
            raise ValueError("roadmap step milestone_id must reference a declared milestone")
        problem_ids = [problem.id for problem in self.problems]
        if len(problem_ids) != len(set(problem_ids)):
            raise ValueError("problem IDs must be unique within a lane update")
        active = [step for step in steps if step.status == "active"]
        if len(active) > 1:
            raise ValueError("a lane roadmap may have at most one active step")
        non_terminal = [step for step in steps if step.status not in ("done", "dropped")]
        if non_terminal and len(active) != 1:
            raise ValueError("a roadmap with unfinished work must have one active step")
        if active and not self.current_action.strip():
            raise ValueError("a roadmap with an active step requires current_action")
        if (self.operation_id is None) != (self.idempotency_key is None):
            raise ValueError("operation_id and idempotency_key must be supplied together")
        child_ids = [entry.child_id for entry in self.child_roster]
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("lane update child roster IDs must be unique")
        return self

    @property
    def all_steps(self) -> tuple[RoadmapStep, ...]:
        """Return direct and milestone steps in their persisted order."""

        return self._all_steps(self.steps, self.milestones)

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(step.id for step in self.all_steps)

    @classmethod
    def from_dict(cls, payload: object) -> LaneUpdate:
        return cast(LaneUpdate, _validate_payload(payload, schema=SESSION_UPDATE_SCHEMA, model=cls))

    def progress(self, *, plan_state: PlanState = "missing") -> Progress:
        return calculate_progress(self, plan_state=plan_state)


class Progress(_ContractModel):
    """Honest progress projection; ``percentage=None`` means unavailable."""

    percentage: float | None
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=0)
    reason: str

    @field_validator("completed_steps", "total_steps")
    @classmethod
    def reject_bool_counts(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("progress counts must be integers")
        return value

    @field_validator("percentage")
    @classmethod
    def validate_percentage(cls, value: float | None) -> float | None:
        if value is not None and (isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError("progress percentage must be between zero and one hundred")
        return value


def calculate_progress(update: LaneUpdate, *, plan_state: PlanState = "missing") -> Progress:
    """Calculate progress only for a valid, current, non-conflicting plan."""

    steps = update.all_steps
    completed = sum(step.status == "done" for step in steps)
    eligible = tuple(step for step in steps if step.status != "dropped")
    unavailable_states = {"forming", "missing", "invalid", "stale", "conflicting"}
    if plan_state in unavailable_states:
        return Progress(
            percentage=None,
            completed_steps=completed,
            total_steps=len(eligible),
            reason=f"plan-{plan_state}",
        )
    if not eligible:
        return Progress(percentage=None, completed_steps=completed, total_steps=0, reason="plan-empty")
    return Progress(
        percentage=(completed / len(eligible)) * 100,
        completed_steps=completed,
        total_steps=len(eligible),
        reason="available",
    )


def _step_id_set(update: LaneUpdate) -> set[str]:
    return set(update.step_ids)


def validate_update(previous: LaneUpdate, current: LaneUpdate) -> None:
    """Raise when ``current`` cannot follow ``previous`` in one lane stream.

    Sequences may skip values because a provider can lose an update, but they
    may never move backward or repeat.  A plan revision is exactly one version
    higher, carries a note, and retains old step IDs (old work can be marked
    ``dropped``).  Updates within one plan version keep the exact step IDs.
    """

    errors: list[str] = []
    if previous.lane_id != current.lane_id:
        errors.append("lane_id changed")
    if previous.goal_id != current.goal_id:
        errors.append("goal_id changed")
    if current.goal_version < previous.goal_version:
        errors.append("goal_version decreased")
    if current.sequence <= previous.sequence:
        errors.append("sequence must increase")
    if current.plan_version < previous.plan_version:
        errors.append("plan_version decreased")
    elif current.plan_version == previous.plan_version:
        if current.step_ids != previous.step_ids:
            errors.append("step IDs changed without a plan revision")
        if current.revision_note.strip():
            errors.append("revision_note is only allowed when the plan changes")
    elif current.plan_version != previous.plan_version + 1:
        errors.append("plan_version must increase by one")
    elif not current.revision_note.strip():
        errors.append("plan revision requires revision_note")
    if current.goal_version > previous.goal_version and current.plan_version == previous.plan_version:
        errors.append("goal_version changes require a plan revision")
    previous_problem_ids = {problem.id for problem in previous.problems}
    current_problem_ids = {problem.id for problem in current.problems}
    if not previous_problem_ids.issubset(current_problem_ids):
        errors.append("problem IDs must remain in the full lane snapshot")
    previous_problems = {problem.id: problem for problem in previous.problems}
    current_problems = {problem.id: problem for problem in current.problems}
    for problem_id, old_problem in previous_problems.items():
        new_problem = current_problems.get(problem_id)
        if new_problem is None:
            continue
        if old_problem.state == "resolved" and new_problem.state == "open" and not new_problem.reopen_event:
            errors.append(f"resolved problem {problem_id} requires an explicit reopen_event")
        if old_problem.state == "resolved" and new_problem.state == "resolved" and new_problem != old_problem:
            errors.append(f"resolved problem {problem_id} history cannot be rewritten")
        if new_problem.summary != old_problem.summary or new_problem.owner != old_problem.owner:
            errors.append(f"problem {problem_id} summary and owner are immutable")
    if (
        current.operation_id is not None
        and current.operation_id == previous.operation_id
        and current.idempotency_key != previous.idempotency_key
    ):
        errors.append("operation idempotency key changed")
    if current.plan_version > previous.plan_version and not _step_id_set(previous).issubset(_step_id_set(current)):
        errors.append("plan revisions must retain prior step IDs")
    if errors:
        raise ContractValidationError("invalid lane update: " + "; ".join(errors))


def validate_lane_update(previous: LaneUpdate, current: LaneUpdate) -> None:
    """Compatibility spelling for callers that name the document explicitly."""

    validate_update(previous, current)


def is_valid_update(previous: LaneUpdate, current: LaneUpdate) -> bool:
    """Return whether ``current`` can follow ``previous`` without raising."""

    try:
        validate_update(previous, current)
    except ContractValidationError:
        return False
    return True


class ProviderCapabilities(_ContractModel):
    """Explicit provider capability declaration.

    ``False`` is meaningful: Chitra must report an unsupported operation and
    cannot silently substitute a weaker one.  Cross-field checks prevent a
    provider from advertising archive/resume or parent-child accounting while
    omitting the operation that makes that claim observable.
    """

    create_or_resume: bool = False
    status: bool = False
    send: bool = False
    read_updates: bool = False
    checkpoint: bool = False
    usage: bool = False
    cancel_current_turn: bool = False
    close: bool = False
    resume_after_close: bool = False
    subagents: bool = False
    parent_child_usage: bool = False

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        if self.resume_after_close and not (self.close and self.create_or_resume):
            raise ValueError("resume_after_close requires close and create_or_resume capabilities")
        if self.parent_child_usage and not self.usage:
            raise ValueError("parent_child_usage requires usage capability")
        return self

    def supports(self, capability: CapabilityName) -> bool:
        return bool(getattr(self, capability))

    @classmethod
    def from_supported(cls, capabilities: Iterable[CapabilityName]) -> ProviderCapabilities:
        supported = set(capabilities)
        known = set(cls.model_fields) - {"model_config"}
        unknown = supported - known
        if unknown:
            raise ValueError(f"unknown provider capabilities: {sorted(unknown)}")
        return cls(**{name: name in supported for name in known})


class ProviderIdentity(_ContractModel):
    """Provider identity and its observed capabilities."""

    kind: ProviderKind
    handle: Identifier
    capabilities: ProviderCapabilities
    # The instance and generation fence restart reconciliation.  A default is
    # retained for old provider records that only had one durable handle.
    instance_id: Identifier = "default"
    generation: int = Field(default=1, ge=1)
    parent_thread_ref: str | None = None
    project_ref: str | None = None
    provider_version: str = ""

    @field_validator("generation")
    @classmethod
    def reject_bool_generation(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("provider generation must be an integer")
        return value


class PendingProviderOperation(_ContractModel):
    """One operation that must be reconciled with the same stable ID."""

    operation_id: Identifier
    kind: OperationKind
    lane_id: Identifier
    provider_handle: Identifier
    idempotency_key: Identifier
    payload_digest: Text
    provider_instance_id: Identifier = "default"
    provider_generation: int = Field(default=1, ge=1)
    created_at: Timestamp
    attempt: int = Field(default=1, ge=1)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: str) -> str:
        return _timestamp(value, "created_at")

    @field_validator("attempt")
    @classmethod
    def reject_bool_attempt(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("attempt must be an integer")
        return value

    @field_validator("provider_generation")
    @classmethod
    def reject_bool_generation(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("provider generation must be an integer")
        return value


class ProviderOperationResult(_ContractModel):
    """Transport acceptance and observed lane consumption are separate.

    ``status='accepted'`` means the provider acknowledged transport only.  It
    is not evidence that the lane consumed the direction.  ``consumed`` is
    therefore required to be true only for ``status='consumed'``.
    """

    operation_id: Identifier
    kind: OperationKind
    lane_id: Identifier
    provider_handle: Identifier
    idempotency_key: Identifier
    payload_digest: Text
    provider_instance_id: Identifier = "default"
    provider_generation: int = Field(default=1, ge=1)
    status: OperationStatus
    accepted: bool | None = None
    consumed: bool | None = None
    observed_at: Timestamp
    evidence: str = ""

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return _timestamp(value, "operation_result.observed_at")

    @field_validator("provider_generation")
    @classmethod
    def reject_bool_generation(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("provider generation must be an integer")
        return value

    @model_validator(mode="after")
    def validate_disposition(self) -> Self:
        if self.status == "consumed" and (self.accepted is not True or self.consumed is not True):
            raise ValueError("consumed result requires accepted=true and consumed=true")
        if self.status == "accepted" and (self.accepted is not True or self.consumed is True):
            raise ValueError("accepted result requires accepted=true and no observed consumption")
        if self.status == "rejected" and (self.accepted is not False or self.consumed is True):
            raise ValueError("rejected result requires accepted=false and no observed consumption")
        if self.status in ("unknown", "lost-response") and (self.accepted is not None or self.consumed is not None):
            raise ValueError("unknown or lost response cannot claim transport acceptance or consumption")
        return self

    @property
    def transport_accepted(self) -> bool | None:
        """Naming that makes the transport/consumption boundary explicit."""

        return self.accepted


def validate_operation_result(pending: PendingProviderOperation, result: ProviderOperationResult) -> None:
    """Ensure a reconciliation result belongs to the pending operation."""

    errors: list[str] = []
    if pending.operation_id != result.operation_id:
        errors.append("operation_id changed")
    if pending.kind != result.kind:
        errors.append("operation kind changed")
    if pending.lane_id != result.lane_id:
        errors.append("lane_id changed")
    if pending.provider_handle != result.provider_handle:
        errors.append("provider handle changed")
    if pending.idempotency_key != result.idempotency_key:
        errors.append("idempotency key changed")
    if pending.payload_digest != result.payload_digest:
        errors.append("payload digest changed")
    if pending.provider_instance_id != result.provider_instance_id:
        errors.append("provider instance changed")
    if pending.provider_generation != result.provider_generation:
        errors.append("provider generation changed")
    if errors:
        raise ContractValidationError("operation result does not match pending operation: " + "; ".join(errors))


def validate_close_result(pending: PendingProviderOperation, result: CloseArchiveResult) -> None:
    """Bind close evidence to the exact retry envelope and provider generation."""

    if pending.kind != "close":
        raise ContractValidationError("close evidence requires a close pending operation")
    errors: list[str] = []
    fields = (
        "operation_id",
        "lane_id",
        "provider_handle",
        "provider_instance_id",
        "provider_generation",
        "idempotency_key",
        "payload_digest",
    )
    for field in fields:
        if getattr(pending, field) != getattr(result, field):
            errors.append(f"{field} changed")
    if errors:
        raise ContractValidationError("close result does not match pending operation: " + "; ".join(errors))


class OperatingFact(_ContractModel):
    """One read-only fact with explicit uncertainty and freshness."""

    name: Identifier
    value: object | None = None
    state: FactState
    source: Text
    revision: int | str
    observed_at: Timestamp
    freshness: FactFreshness = "unknown"
    fresh_until: str | None = None
    within_authority: bool = False

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return _timestamp(value, "operating_fact.observed_at")

    @field_validator("fresh_until")
    @classmethod
    def validate_fresh_until(cls, value: str | None) -> str | None:
        return _timestamp_or_none(value, "operating_fact.fresh_until")

    @field_validator("revision")
    @classmethod
    def reject_bool_revision(cls, value: int | str) -> int | str:
        if isinstance(value, bool):
            raise ValueError("fact revision must be an integer or string")
        return value

    def is_current(self, *, now: datetime | None = None) -> bool:
        """Return true only when Chitra may act on this fact.

        A known value without a freshness deadline is not treated as current.
        This keeps an absent freshness claim from becoming an invented fact.
        """

        if self.state != "known" or not self.within_authority or self.freshness not in ("current", "fresh"):
            return False
        if self.fresh_until is None:
            return False
        current = datetime.now(UTC) if now is None else now
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return datetime.fromisoformat(self.fresh_until.replace("Z", "+00:00")) >= current.astimezone(UTC)


class RecoveryState(_ContractModel):
    """Bounded recovery ladder state that never creates a user ask."""

    stage: RecoveryStage = "none"
    failure_signature: str = ""
    attempted_remedy: str = ""
    attempt_count: int = Field(default=0, ge=0)
    next_allowed_attempt: str | None = None
    last_intervention: str = ""
    intervention_consumed: bool | None = None
    useful_work_resumed: bool | None = None

    @field_validator("attempt_count")
    @classmethod
    def reject_bool_attempt_count(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("attempt_count must be an integer")
        return value

    @field_validator("next_allowed_attempt")
    @classmethod
    def validate_next_allowed_attempt(cls, value: str | None) -> str | None:
        return _timestamp_or_none(value, "recovery.next_allowed_attempt")


class ProgressEvidence(_ContractModel):
    """A reference to the last material progress, not a copied journal row."""

    update_sequence: int = Field(ge=0)
    summary: Text
    observed_at: Timestamp
    evidence_ref: str = ""

    @field_validator("update_sequence")
    @classmethod
    def reject_bool_sequence(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("update_sequence must be an integer")
        return value

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return _timestamp(value, "progress_evidence.observed_at")


class InterventionEvidence(_ContractModel):
    """Last Chitra intervention and the two observations needed for recovery."""

    operation_id: Identifier
    action: Text
    consumed: bool | None
    useful_work_resumed: bool | None
    observed_at: Timestamp

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return _timestamp(value, "intervention.observed_at")


class UsageComponent(_ContractModel):
    """Parent or child usage amount in one named unit."""

    name: Identifier
    amount: int | float = Field(ge=0)
    unit: Text

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: int | float) -> int | float:
        if isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
            raise ValueError("usage amount must be a finite non-negative number")
        return value


class UsageReport(_ContractModel):
    """Parent/children roll-up with no invented ceiling value."""

    parent: UsageComponent
    children: tuple[UsageComponent, ...] = ()
    child_roster: tuple[ChildRosterEntry, ...] = ()
    child_roster_complete: bool = False
    child_roster_evidence: Text | None = None
    total: UsageComponent
    evidence_source: Text
    observed_at: Timestamp
    complete: bool
    ceiling: int | float | None = Field(default=None, ge=0)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return _timestamp(value, "usage.observed_at")

    @field_validator("ceiling")
    @classmethod
    def validate_ceiling(cls, value: int | float | None) -> int | float | None:
        if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
            raise ValueError("usage ceiling must be a finite number or null")
        return value

    @model_validator(mode="after")
    def validate_rollup(self) -> Self:
        names = [self.parent.name, *(child.name for child in self.children)]
        if len(names) != len(set(names)):
            raise ValueError("usage component names must be unique")
        roster_ids = [entry.child_id for entry in self.child_roster]
        if len(roster_ids) != len(set(roster_ids)):
            raise ValueError("child roster IDs must be unique")
        child_names = set(child.name for child in self.children)
        if set(roster_ids) != child_names:
            raise ValueError("child roster must identify every reported child exactly once")
        if self.complete and (not self.child_roster_complete or self.child_roster_evidence is None):
            raise ValueError("complete usage requires an observed child roster and evidence")
        units = {self.parent.unit, *(child.unit for child in self.children), self.total.unit}
        if len(units) != 1:
            raise ValueError("parent, child, and total usage units must match")
        expected = float(self.parent.amount) + sum(float(child.amount) for child in self.children)
        if self.complete and not math.isclose(float(self.total.amount), expected, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("complete usage report total does not equal parent plus children")
        return self

    def complies_with_ceiling(self) -> bool | None:
        """Return None when no policy ceiling or complete evidence exists."""

        if self.ceiling is None or not self.complete:
            return None
        return float(self.total.amount) <= float(self.ceiling)

    @classmethod
    def from_dict(cls, payload: object) -> UsageReport:
        if not isinstance(payload, Mapping):
            raise ValueError("usage report must be an object")
        return cls.model_validate(_immutable_json(payload), strict=True)


class CloseArchiveResult(_ContractModel):
    """Observed close/archive state for the same provider thread."""

    operation_id: Identifier
    lane_id: Identifier
    provider_handle: Identifier
    provider_instance_id: Identifier
    provider_generation: int = Field(ge=1)
    idempotency_key: Identifier
    payload_digest: Text
    state: CloseState
    provider_thread_ref: Identifier
    same_provider_thread: bool | None = True
    later_resume_supported: bool | None = None
    checkpoint_ref: Identifier | None = None
    quiescent: bool | None = None
    observed_at: Timestamp
    evidence: Text

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return _timestamp(value, "close.observed_at")

    @field_validator("provider_generation")
    @classmethod
    def reject_bool_generation(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("provider generation must be an integer")
        return value

    @model_validator(mode="after")
    def validate_close_evidence(self) -> Self:
        if self.state in ("closed", "archived"):
            if self.same_provider_thread is not True:
                raise ValueError("a closed or archived result must identify the same provider thread")
            if self.checkpoint_ref is None or self.quiescent is not True:
                raise ValueError("a closed or archived result requires a checkpoint and provider quiescence")
        return self

    @property
    def closed(self) -> bool:
        return self.state == "closed"

    @property
    def archived(self) -> bool:
        return self.state == "archived"

    @property
    def lane_lifecycle(self) -> LaneLifecycle | None:
        if self.state == "closed":
            return "closed"
        if self.state == "archived":
            return "archived"
        return None


# Both names read naturally at different call sites and describe the same
# wire type.  ``CloseResult`` is the short public spelling used by providers.
CloseResult = CloseArchiveResult
ProviderResult = ProviderOperationResult
OperationResult = ProviderOperationResult
ProviderOperation = PendingProviderOperation
Step = RoadmapStep
SessionUpdate = LaneUpdate
RoadmapSnapshot = LaneUpdate
NextWake = NextCheck
ProblemRecord = Problem
Fact = OperatingFact
Usage = UsageReport
Recovery = RecoveryState


class JoinedLaneRecord(_ContractModel):
    """One durable ``chitra.lanes.v1`` projection for an unfinished lane.

    The record references ``current_update`` and evidence IDs.  It does not
    duplicate GoalRecord fields, transcript events, dispatch entries, or
    complete recovery journals.
    """

    schema: Literal["chitra.lanes.v1"] = LANE_RECORD_SCHEMA  # type: ignore[assignment]
    lane_id: Identifier
    goal_id: Identifier
    goal_version: int = Field(ge=1)
    session_ref: Identifier
    lifecycle: LaneLifecycle = "active"
    physical_session_generation: int = Field(default=1, ge=1)
    chitra_ownership_epoch: int = Field(default=1, ge=1)
    provider: ProviderIdentity
    update_cursor: str = ""
    send_deduplication_key: str = ""
    current_update: LaneUpdate | None = None
    plan_assessment: PlanAssessment = PlanAssessment()
    last_useful_progress: ProgressEvidence | None = None
    last_intervention: InterventionEvidence | None = None
    recovery: RecoveryState = RecoveryState()
    next_check: NextCheck | None = None
    wake_condition: str | None = None
    pending_operation: PendingProviderOperation | None = None
    last_operation_result: ProviderOperationResult | None = None
    last_close_result: CloseArchiveResult | None = None
    checkpoint_reference: str | None = None
    worktree_path: str | None = None
    repository_commit: str | None = None
    preserved_work_manifest: tuple[str, ...] = ()
    revision: int = Field(default=1, ge=1)

    @field_validator("goal_version", "physical_session_generation", "chitra_ownership_epoch", "revision")
    @classmethod
    def reject_bool_counts(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("lane record counters must be integers")
        return value

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if self.current_update is not None:
            update = self.current_update
            if (update.lane_id, update.goal_id, update.goal_version, update.session_ref) != (
                self.lane_id,
                self.goal_id,
                self.goal_version,
                self.session_ref,
            ):
                raise ValueError("current_update identity and goal reference must match joined lane record")
        for operation in (self.pending_operation, self.last_operation_result):
            if operation is not None and (
                operation.lane_id != self.lane_id
                or operation.provider_handle != self.provider.handle
                or operation.provider_instance_id != self.provider.instance_id
                or operation.provider_generation != self.provider.generation
            ):
                raise ValueError("provider operation does not belong to joined lane")
        if self.last_operation_result is not None and self.pending_operation is not None:
            validate_operation_result(self.pending_operation, self.last_operation_result)
        if self.last_close_result is not None:
            close_result = self.last_close_result
            if (
                close_result.lane_id != self.lane_id
                or close_result.provider_handle != self.provider.handle
                or close_result.provider_instance_id != self.provider.instance_id
                or close_result.provider_generation != self.provider.generation
            ):
                raise ValueError("close evidence does not belong to joined lane provider generation")
            if close_result.lane_lifecycle is not None and close_result.lane_lifecycle != self.lifecycle:
                raise ValueError("close evidence lifecycle does not match joined lane lifecycle")
            if self.pending_operation is not None:
                validate_close_result(self.pending_operation, close_result)
        if self.lifecycle in ("closed", "archived"):
            terminal_close = self.last_close_result
            if terminal_close is None or terminal_close.lane_lifecycle != self.lifecycle:
                raise ValueError("closed or archived lane requires matching close evidence")
        elif self.last_close_result is not None and self.last_close_result.lane_lifecycle is not None:
            raise ValueError("active or inactive lane cannot carry terminal close evidence")
        if self.current_update is not None and self.current_update.operation_id is not None:
            result = self.last_operation_result
            if result is None or (
                result.operation_id != self.current_update.operation_id
                or result.idempotency_key != self.current_update.idempotency_key
            ):
                raise ValueError("lane update operation evidence must bind to the observed provider result")
            if result.status != "consumed" or result.accepted is not True or result.consumed is not True:
                raise ValueError("lane update operation evidence must be consumed")
        return self

    @classmethod
    def from_dict(cls, payload: object) -> JoinedLaneRecord:
        return cast(JoinedLaneRecord, _validate_payload(payload, schema=LANE_RECORD_SCHEMA, model=cls))

    @property
    def identity(self) -> LaneIdentity:
        return LaneIdentity(
            lane_id=self.lane_id,
            goal_id=self.goal_id,
            session_ref=self.session_ref,
            goal_version=self.goal_version,
        )

    @property
    def problems(self) -> tuple[Problem, ...]:
        """Read problem history from the lane update; never copy it here."""

        return () if self.current_update is None else self.current_update.problems

    def progress(self) -> Progress | None:
        return None if self.current_update is None else calculate_progress(self.current_update, plan_state=self.plan_assessment.state)


LaneRecord = JoinedLaneRecord


__all__ = [
    "CapabilityName",
    "ChildRetention",
    "ChildRosterEntry",
    "CloseArchiveResult",
    "CloseResult",
    "CloseState",
    "ContractValidationError",
    "Fact",
    "FactFreshness",
    "FactState",
    "InterventionEvidence",
    "JoinedLaneRecord",
    "LaneRecord",
    "LANE_RECORD_SCHEMA",
    "LANE_RECORD_VERSION",
    "LaneIdentity",
    "LaneLifecycle",
    "LaneUpdate",
    "NextCheck",
    "NextWake",
    "OperationKind",
    "OperationResult",
    "OperationStatus",
    "PendingProviderOperation",
    "ProviderOperation",
    "PlanState",
    "PlanAssessment",
    "Problem",
    "ProblemRecord",
    "ProblemState",
    "Progress",
    "ProgressEvidence",
    "OperatingFact",
    "ProviderCapabilities",
    "ProviderIdentity",
    "ProviderKind",
    "ProviderOperationResult",
    "ProviderResult",
    "RecoveryStage",
    "RecoveryState",
    "Recovery",
    "RoadmapSnapshot",
    "RoadmapMilestone",
    "RoadmapStep",
    "SESSION_UPDATE_SCHEMA",
    "SESSION_UPDATE_VERSION",
    "SessionUpdate",
    "Step",
    "StepStatus",
    "UsageComponent",
    "UsageReport",
    "Usage",
    "calculate_progress",
    "is_valid_update",
    "validate_lane_update",
    "validate_close_result",
    "validate_operation_result",
    "validate_update",
]

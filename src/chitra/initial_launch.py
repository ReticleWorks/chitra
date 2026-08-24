"""Pure helpers for the first Tophand operation.

This module does not own a launch state machine or call a provider.  It only
turns one current, authoritative operating fact into the identity and pending
operation that :class:`chitra.recovery.RecoveryEngine` persists and executes.
Keeping this boundary pure makes the normal recovery engine the only writer
and gives restart/concurrency tests one operation envelope to exercise.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from .goals import GoalRecord
from .session_contract import (
    JoinedLaneRecord,
    NextCheck,
    OperationReference,
    OperatingFact,
    PendingProviderOperation,
    ProviderCapabilities,
    ProviderIdentity,
    RecoveryState,
)
from .tophand_wire import request_digest

TOPHAND_IDENTITY_FACT = "fleet.provider-capabilities.tophand"


class InitialLaunchError(ValueError):
    """The authoritative initial-launch fact cannot authorize a lane."""


def _timestamp(value: datetime | str | None) -> str:
    current = (
        datetime.now(UTC)
        if value is None
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if current.tzinfo is None:
        raise ValueError("initial launch timestamps must include a timezone")
    return current.astimezone(UTC).isoformat()


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _aliased(payload: Mapping[str, object], *names: str) -> object | None:
    values = [payload[name] for name in names if name in payload]
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise InitialLaunchError(f"identity fact has conflicting fields: {', '.join(names)}")
    return values[0]


def _required_text(payload: Mapping[str, object], *names: str) -> str:
    value = _aliased(payload, *names)
    if not isinstance(value, str) or not value.strip():
        raise InitialLaunchError(f"identity fact lacks {names[0]}")
    return value.strip()


def _required_generation(payload: Mapping[str, object]) -> int:
    value = _aliased(payload, "provider_generation", "generation")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InitialLaunchError("identity fact lacks a positive provider generation")
    return value


def _capabilities(payload: Mapping[str, object]) -> ProviderCapabilities:
    raw = payload.get("capabilities")
    try:
        if isinstance(raw, Mapping):
            result = ProviderCapabilities.model_validate(dict(raw), strict=True)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and all(
            isinstance(item, str) for item in raw
        ):
            result = ProviderCapabilities.from_supported(raw)
        else:
            raise ValueError("capabilities must be a mapping or a list")
    except (TypeError, ValueError) as exc:
        raise InitialLaunchError(f"identity fact capabilities are invalid: {exc}") from exc
    if not result.create_or_resume:
        raise InitialLaunchError("Tophand identity fact does not authorize create_or_resume")
    return result


def _candidate_value(goal: GoalRecord, facts: Sequence[OperatingFact], *, now: datetime | None) -> Mapping[str, object]:
    scoped_name = f"{TOPHAND_IDENTITY_FACT}.{goal.lane_id}"
    scoped = [fact for fact in facts if fact.name == scoped_name and fact.is_current(now=now)]
    if len(scoped) > 1:
        raise InitialLaunchError(f"multiple current Tophand identity facts exist for {goal.lane_id}")
    if scoped:
        value = _mapping(scoped[0].value)
        if value is None:
            raise InitialLaunchError("scoped Tophand identity fact is not an object")
        return value

    aggregate = [fact for fact in facts if fact.name == TOPHAND_IDENTITY_FACT and fact.is_current(now=now)]
    if len(aggregate) > 1:
        raise InitialLaunchError("multiple current aggregate Tophand identity facts exist")
    if not aggregate:
        raise InitialLaunchError("current authoritative Tophand identity fact is unavailable")
    value = _mapping(aggregate[0].value)
    if value is None:
        raise InitialLaunchError("aggregate Tophand identity fact is not an object")
    lanes = _mapping(value.get("lanes"))
    if lanes is not None:
        lane_value = _mapping(lanes.get(goal.lane_id))
        if lane_value is None:
            raise InitialLaunchError(f"aggregate Tophand identity fact has no lane {goal.lane_id}")
        return lane_value
    top_hand = _mapping(value.get("tophand"))
    if top_hand is not None:
        return top_hand
    identity = _mapping(value.get("identity"))
    if identity is not None:
        return identity
    if value.get("lane_id") not in (None, goal.lane_id):
        raise InitialLaunchError("Tophand identity fact lane does not match the enrolled goal")
    return value


def top_hand_identity_from_facts(
    goal: GoalRecord,
    facts: Sequence[OperatingFact],
    *,
    now: datetime | None = None,
) -> ProviderIdentity:
    """Resolve one exact Tophand identity without inferring missing fields."""

    payload = _candidate_value(goal, facts, now=now)
    provider_session_id = _required_text(payload, "provider_session_id")
    if provider_session_id != goal.session_ref:
        raise InitialLaunchError("Tophand identity fact session does not match the enrolled physical session")
    handle = _required_text(payload, "provider_handle", "handle")
    instance_id = _required_text(payload, "provider_instance_id", "instance_id")
    process_start_token = _required_text(payload, "process_start_token", "process_start", "start_token")
    generation = _required_generation(payload)
    provider_version = payload.get("provider_version", "")
    if not isinstance(provider_version, str):
        raise InitialLaunchError("Tophand identity fact provider_version must be a string")
    return ProviderIdentity(
        kind="tophand",
        handle=handle,
        provider_session_id=provider_session_id,
        instance_id=instance_id,
        process_start_token=process_start_token,
        generation=generation,
        capabilities=_capabilities(payload),
        parent_thread_ref=payload.get("parent_thread_ref") if isinstance(payload.get("parent_thread_ref"), str) else None,
        project_ref=payload.get("project_ref") if isinstance(payload.get("project_ref"), str) else None,
        profile_digest=payload.get("profile_digest") if isinstance(payload.get("profile_digest"), str) else None,
        provider_version=provider_version,
    )


# Short spelling retained for callers that describe this as a bootstrap.
top_hand_bootstrap_identity = top_hand_identity_from_facts


def top_hand_create_operation(
    goal: GoalRecord,
    identity: ProviderIdentity,
    *,
    now: datetime | str | None = None,
) -> PendingProviderOperation:
    """Build the stable create envelope used across a restart."""

    created_at = _timestamp(now)
    seed = {
        "goal_id": goal.goal_id,
        "goal_version": goal.goal_version,
        "lane_id": goal.lane_id,
        "session_ref": goal.session_ref,
        "provider_handle": identity.handle,
        "provider_session_id": identity.provider_session_id,
        "provider_instance_id": identity.instance_id,
        "provider_generation": identity.generation,
        "process_start_token": identity.process_start_token,
    }
    operation_id = "bootstrap-" + hashlib.sha256(
        json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    payload_fields = {
        "session_ref": goal.session_ref,
        "provider_session_id": identity.provider_session_id,
        "context_ref": None,
    }
    payload = json.dumps(payload_fields, sort_keys=True, separators=(",", ":"))
    return PendingProviderOperation(
        operation_id=operation_id,
        kind="create_or_resume",
        lane_id=goal.lane_id,
        provider_handle=identity.handle,
        provider_session_id=identity.provider_session_id,
        process_start_token=identity.process_start_token,
        provider_instance_id=identity.instance_id,
        provider_generation=identity.generation,
        idempotency_key=operation_id + "-idem",
        payload_digest=request_digest("create_or_resume", payload_fields),
        payload=payload,
        created_at=created_at,
    )


def top_hand_bootstrap_record(
    goal: GoalRecord,
    identity: ProviderIdentity,
    operation: PendingProviderOperation,
    *,
    now: datetime | str | None = None,
) -> JoinedLaneRecord:
    """Create the durable pending snapshot; this function performs no I/O."""

    if goal.lane_id != operation.lane_id or identity.kind != "tophand":
        raise InitialLaunchError("initial operation identity does not match the enrolled lane")
    current = _timestamp(now)
    check = NextCheck(
        at=current,
        reason="Prove the exact Tophand launch ownership envelope",
        wake_condition="an exact Tophand launch result or ownership observation",
    )
    recovery = RecoveryState(
        stage="relaunch",
        cycle_id="bootstrap-" + operation.operation_id.removeprefix("bootstrap-"),
        attempted_remedy="bootstrap",
        attempt_count=1,
        next_allowed_attempt=current,
        pending_payload=operation.payload,
    )
    return JoinedLaneRecord(
        lane_id=goal.lane_id,
        goal_id=goal.goal_id,
        goal_version=goal.goal_version,
        session_ref=goal.session_ref,
        provider=identity,
        next_check=check,
        recovery=recovery,
        pending_operation=operation,
        operation_history=(
            OperationReference(
                operation_id=operation.operation_id,
                idempotency_key=operation.idempotency_key,
                payload_digest=operation.payload_digest,
                kind=operation.kind,
                created_at=operation.created_at,
            ),
        ),
    )


__all__ = [
    "InitialLaunchError",
    "TOPHAND_IDENTITY_FACT",
    "top_hand_bootstrap_identity",
    "top_hand_create_operation",
    "top_hand_bootstrap_record",
    "top_hand_identity_from_facts",
]

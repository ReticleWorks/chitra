"""Fail-closed Amp launch and usage policy over canonical Chitra records."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .session_contract import (
    ContractValidationError,
    JoinedLaneRecord,
    UsageReport,
    validate_usage_against_lane,
)

UsagePolicyAction = Literal["allow", "hold", "cancel-and-hold", "unknown-and-hold"]
AmpCreatePolicyAction = Literal["create-once", "adopt", "unknown-and-hold", "ambiguous-and-hold"]


@dataclass(frozen=True, slots=True)
class UsagePolicyDecision:
    """One Chitra-owned decision about starting or continuing paid Amp work."""

    action: UsagePolicyAction
    mutation_allowed: bool
    cancel_required: bool
    reason: str
    report: UsageReport | None = None


@dataclass(frozen=True, slots=True)
class AmpCreateSearchEvidence:
    """One read-only exact-tag search made before an Amp root create."""

    operation_id: str
    create_tag: str
    match_count: int
    observed_at: str
    evidence: str

    def __post_init__(self) -> None:
        if not self.operation_id.strip() or not self.create_tag.strip() or not self.evidence.strip():
            raise ValueError("create search evidence fields must be non-empty")
        if isinstance(self.match_count, bool) or not isinstance(self.match_count, int) or self.match_count < 0:
            raise ValueError("create search match_count must be a non-negative integer")
        _observed_at(self.observed_at)


@dataclass(frozen=True, slots=True)
class AmpCreatePolicyDecision:
    """Chitra's declarative result for exact-tag reconciliation before create."""

    action: AmpCreatePolicyAction
    provider_reconciliation_allowed: bool
    create_allowed: bool
    reason: str
    search: AmpCreateSearchEvidence | None = None


def launch_policy_problem(record: JoinedLaneRecord) -> str | None:
    """Return the first reason the embedded Amp launch policy is not authoritative."""

    if record.provider.kind != "amp":
        return "Amp launch policy cannot govern a non-Amp provider"
    policy = record.launch_policy
    if policy is None:
        return "Amp lane has no Chitra launch policy"
    if (policy.lane_id, policy.goal_id, policy.goal_version) != (
        record.lane_id,
        record.goal_id,
        record.goal_version,
    ):
        return "launch policy lane or goal identity does not match the joined lane"
    if policy.provider_kind != record.provider.kind:
        return "launch policy provider kind does not match the provider identity"
    if policy.project_ref != record.provider.project_ref:
        return "launch policy project does not match the provider identity"
    if policy.profile_digest != record.provider.profile_digest:
        return "launch policy profile digest does not match the provider identity"
    if policy.provider_version != record.provider.provider_version:
        return "launch policy provider version does not match the provider identity"
    if not record.provider.capabilities.usage or not record.provider.capabilities.parent_child_usage:
        return "Amp provider does not declare complete parent-child usage capability"
    if not record.provider.capabilities.cancel_current_turn or not record.provider.capabilities.status:
        return "Amp provider does not declare cancellation and status capability for quiescence enforcement"
    return None


def _decision(
    action: UsagePolicyAction,
    reason: str,
    *,
    report: UsageReport | None = None,
) -> UsagePolicyDecision:
    return UsagePolicyDecision(
        action=action,
        mutation_allowed=action == "allow",
        cancel_required=action == "cancel-and-hold",
        reason=reason,
        report=report,
    )


def _observed_at(value: str) -> datetime:
    observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("evidence timestamp must be timezone-aware")
    return observed.astimezone(UTC)


def expected_amp_create_tag(record: JoinedLaneRecord) -> str:
    """Derive the source-controlled operation tag used by the Amp project plugin."""

    policy = record.launch_policy
    pending = record.pending_operation
    if policy is None or pending is None or pending.kind != "create_or_resume":
        raise ValueError("Amp create tag requires a launch policy and pending create operation")
    material = f"{policy.profile_digest}{pending.operation_id}{pending.idempotency_key}{pending.payload_digest}"
    return f"chitra-{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def evaluate_amp_create_policy(
    record: JoinedLaneRecord,
    search: AmpCreateSearchEvidence | None,
    *,
    now: datetime | None = None,
) -> AmpCreatePolicyDecision:
    """Reconcile the exact create tag before allowing one initial Amp create."""

    problem = launch_policy_problem(record)
    if problem is not None:
        return AmpCreatePolicyDecision("unknown-and-hold", False, False, problem, search)
    policy = record.launch_policy
    pending = record.pending_operation
    assert policy is not None
    if pending is None or pending.kind != "create_or_resume" or record.current_update is not None:
        return AmpCreatePolicyDecision(
            "unknown-and-hold",
            False,
            False,
            "initial create policy requires one pending create operation and no existing lane update",
            search,
        )
    if not record.provider.capabilities.create_or_resume:
        return AmpCreatePolicyDecision(
            "unknown-and-hold", False, False, "Amp provider does not declare create_or_resume capability", search
        )
    if float(policy.turn_reserve_usd) > float(policy.cost_ceiling_usd):
        return AmpCreatePolicyDecision("unknown-and-hold", False, False, "turn reserve exceeds the lane ceiling", search)
    if search is None:
        return AmpCreatePolicyDecision(
            "unknown-and-hold", False, False, "exact deterministic create-tag search is unavailable", search
        )
    if search.operation_id != pending.operation_id or search.create_tag != expected_amp_create_tag(record):
        return AmpCreatePolicyDecision(
            "unknown-and-hold", False, False, "create search evidence does not match the pending operation tag", search
        )
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("create policy clock must be timezone-aware")
    age_seconds = (current.astimezone(UTC) - _observed_at(search.observed_at)).total_seconds()
    if age_seconds < 0 or age_seconds > policy.usage_max_age_seconds:
        return AmpCreatePolicyDecision("unknown-and-hold", False, False, "create search evidence is not fresh", search)
    if search.match_count == 0:
        return AmpCreatePolicyDecision(
            "create-once", True, True, "fresh exact-tag search found no existing Amp lane thread", search
        )
    if search.match_count == 1:
        return AmpCreatePolicyDecision(
            "adopt", True, False, "fresh exact-tag search found one Amp lane thread that must be adopted", search
        )
    return AmpCreatePolicyDecision(
        "ambiguous-and-hold", False, False, "exact create-tag search found multiple Amp lane threads", search
    )


def evaluate_usage_policy(
    record: JoinedLaneRecord,
    report: UsageReport | None,
    *,
    now: datetime | None = None,
) -> UsagePolicyDecision:
    """Apply Chitra's profile, roster, freshness, reserve, and ceiling gates."""

    problem = launch_policy_problem(record)
    if problem is not None:
        return _decision("unknown-and-hold", problem)
    policy = record.launch_policy
    assert policy is not None
    if report is None:
        return _decision("unknown-and-hold", "Amp usage evidence is unavailable")
    if report.ceiling is not None and not math.isclose(
        float(report.ceiling),
        float(policy.cost_ceiling_usd),
        rel_tol=1e-9,
        abs_tol=1e-9,
    ):
        return _decision("unknown-and-hold", "usage report carries a ceiling that differs from Chitra launch policy")
    if report.parent.name != record.lane_id:
        return _decision("unknown-and-hold", "usage parent does not match the canonical lane")
    if report.total.unit != "USD":
        return _decision("unknown-and-hold", "Amp lane usage must be reported in USD")
    if not report.complete or not report.child_roster_complete:
        return _decision("unknown-and-hold", "usage report does not prove a complete child roster")
    if record.current_update is None:
        return _decision("unknown-and-hold", "joined lane has no canonical update for child-roster comparison")
    try:
        validate_usage_against_lane(record.current_update, report)
    except ContractValidationError as exc:
        return _decision("unknown-and-hold", str(exc))

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("usage policy clock must be timezone-aware")
    age_seconds = (current.astimezone(UTC) - _observed_at(report.observed_at)).total_seconds()
    if age_seconds < 0:
        return _decision("unknown-and-hold", "usage evidence is dated in the future")
    if age_seconds > policy.usage_max_age_seconds:
        return _decision("unknown-and-hold", "usage evidence is stale")

    evaluated_report = UsageReport.from_dict(
        {
            **report.to_dict(),
            "ceiling": policy.cost_ceiling_usd,
        }
    )
    total = float(evaluated_report.total.amount)
    ceiling = float(policy.cost_ceiling_usd)
    reserve = float(policy.turn_reserve_usd)
    if total >= ceiling:
        return _decision(
            "cancel-and-hold",
            "observed Amp usage reached or exceeded the lane ceiling",
            report=evaluated_report,
        )
    if total + reserve > ceiling:
        return _decision(
            "hold",
            "observed Amp usage plus the approved turn reserve exceeds the lane ceiling",
            report=evaluated_report,
        )
    return _decision("allow", "fresh complete usage fits within the lane ceiling and turn reserve", report=evaluated_report)


__all__ = [
    "AmpCreatePolicyAction",
    "AmpCreatePolicyDecision",
    "AmpCreateSearchEvidence",
    "UsagePolicyAction",
    "UsagePolicyDecision",
    "evaluate_amp_create_policy",
    "evaluate_usage_policy",
    "expected_amp_create_tag",
    "launch_policy_problem",
]

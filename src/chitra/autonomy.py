"""Frozen, goal-scoped authority for persistent foreground pursuit.

This module intentionally has no policy daemon or mutable authority store.
Enrollment supplies one immutable :class:`AutonomyPolicy`; decision paths ask
that policy whether the exact capabilities needed by an action are available.
Model output, transcript text, and learned memory can describe an action, but
none of them can add a grant.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Capability = Literal[
    "replan",
    "small_redesign",
    "dependency_change",
    "schema_change",
    "hook_change",
    "credential_use",
    "authentication",
    "security_change",
    "irreversible_action",
    "spend",
]
Initiative = Literal["aggressive", "steady", "restricted"]
AuthorizationDisposition = Literal["allowed", "foreground_residual", "operator_required"]


class CapabilityGrant(BaseModel):
    """One enrollment-issued capability, optionally narrowed by hard limits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")
    capability: Capability
    targets: tuple[str, ...] = ("goal",)
    max_amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    max_units: int | None = Field(default=None, ge=1)
    expires_at: AwareDatetime | None = None

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not target.strip() for target in value):
            raise ValueError("capability grant targets must contain non-empty values")
        if len(set(value)) != len(value):
            raise ValueError("capability grant targets must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if (self.max_amount is None) != (self.currency is None):
            raise ValueError("max_amount and currency must be supplied together")
        if self.max_amount is not None and self.capability != "spend":
            raise ValueError("max_amount is valid only for a spend grant")
        return self


class AutonomyPolicy(BaseModel):
    """The complete, frozen autonomy contract enrolled for one goal."""

    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True)

    contract_schema: Literal["chitra.autonomy.v1"] = Field(default="chitra.autonomy.v1", alias="schema")
    initiative: Initiative = "aggressive"
    idle_pursuit_passes: int = Field(default=1, ge=1)
    loop_interval_minutes: int = Field(default=5, ge=1, le=1440)
    grants: tuple[CapabilityGrant, ...] = ()

    @model_validator(mode="after")
    def validate_grant_ids(self) -> Self:
        grant_ids = [grant.grant_id for grant in self.grants]
        if len(set(grant_ids)) != len(grant_ids):
            raise ValueError("autonomy policy grant_id values must be unique")
        return self


class CapabilityUse(BaseModel):
    """The mechanically identified authority needed by one proposed action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: Capability
    target: str = Field(default="goal", min_length=1)
    amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    units: int | None = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_amount(self) -> Self:
        if (self.amount is None) != (self.currency is None):
            raise ValueError("amount and currency must be supplied together")
        if self.amount is not None and self.capability != "spend":
            raise ValueError("amount is valid only for spend capability use")
        return self


class AuthorizationDecision(BaseModel):
    """A compact policy result suitable for an attestation or residual task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: AuthorizationDisposition
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grant_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def autonomy_policy_sha256(policy: AutonomyPolicy) -> str:
    """Return the canonical digest bound into the frozen goal and decisions."""
    canonical = json.dumps(
        policy.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def autonomy_policy_json(policy: AutonomyPolicy) -> str:
    """Return canonical JSON suitable for redirect history and CLI receipts."""
    return json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_autonomy_policy_json(raw: str) -> AutonomyPolicy:
    """Strictly parse one policy JSON value without accepting extension keys."""
    return AutonomyPolicy.model_validate_json(raw, strict=True)


def capability_target_from_text(text: str) -> str:
    """Return the narrow environment target stated in action text, if any."""
    for target, pattern in (
        ("production", re.compile(r"\b(?:production|prod)\b", re.I)),
        ("staging", re.compile(r"\b(?:staging|stage)\b", re.I)),
    ):
        if pattern.search(text):
            return target
    return "goal"


def _goal_grant(capability: Capability) -> CapabilityGrant:
    return CapabilityGrant(grant_id=f"default-{capability.replace('_', '-')}", capability=capability)


# Old records had no policy field. Their compatibility default pursues the
# enrolled goal without categorical capability gates. Deployments that need a
# target, amount, unit, or time limit can freeze a narrower policy at enrollment.
DEFAULT_AUTONOMY_POLICY = AutonomyPolicy(
    initiative="aggressive",
    grants=tuple(
        _goal_grant(capability)
        for capability in (
            "replan",
            "small_redesign",
            "dependency_change",
            "schema_change",
            "hook_change",
            "credential_use",
            "authentication",
            "security_change",
            "irreversible_action",
            "spend",
        )
    ),
)


def _target_matches(grant: CapabilityGrant, target: str) -> bool:
    return any(fnmatch.fnmatchcase(target, pattern) for pattern in grant.targets)


def _grant_result(
    grant: CapabilityGrant,
    use: CapabilityUse,
    *,
    now: datetime,
) -> Literal["allowed", "incomplete", "denied"]:
    if grant.expires_at is not None and grant.expires_at <= now:
        return "denied"
    if not _target_matches(grant, use.target):
        return "denied"
    if grant.max_amount is not None:
        if use.amount is None or use.currency is None:
            return "incomplete"
        if use.currency != grant.currency or use.amount > grant.max_amount:
            return "denied"
    if grant.max_units is not None:
        if use.units is None:
            return "incomplete"
        if use.units > grant.max_units:
            return "denied"
    return "allowed"


def authorize_action(
    policy: AutonomyPolicy,
    requirements: tuple[CapabilityUse, ...] | list[CapabilityUse],
    *,
    evidence_complete: bool = True,
    changes_frozen_outcome: bool = False,
    now: datetime | None = None,
) -> AuthorizationDecision:
    """Apply the frozen policy to an action's typed capability requirements.

    Missing facts stay with the foreground Chitra agent. A person is needed
    only after the evaluator can prove that a grant is absent, expired, aimed
    at another target, over its limit, or the action changes the frozen outcome.
    """
    policy_digest = autonomy_policy_sha256(policy)
    if changes_frozen_outcome:
        return AuthorizationDecision(
            disposition="operator_required",
            policy_sha256=policy_digest,
            reasons=("the action changes the frozen goal outcome",),
        )
    if not evidence_complete:
        return AuthorizationDecision(
            disposition="foreground_residual",
            policy_sha256=policy_digest,
            reasons=("authority evidence is incomplete; investigate and replan",),
        )

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("authorization time must be timezone-aware")
    accepted: list[str] = []
    denied: list[str] = []
    incomplete: list[str] = []
    for requirement in requirements:
        grants = [grant for grant in policy.grants if grant.capability == requirement.capability]
        if not grants:
            denied.append(f"missing {requirement.capability} grant")
            continue
        outcomes = [(grant, _grant_result(grant, requirement, now=moment)) for grant in grants]
        allowed = next((grant for grant, outcome in outcomes if outcome == "allowed"), None)
        if allowed is not None:
            accepted.append(allowed.grant_id)
        elif any(outcome == "incomplete" for _, outcome in outcomes):
            incomplete.append(f"{requirement.capability} grant limit cannot be checked from current evidence")
        else:
            denied.append(f"no active {requirement.capability} grant covers target and limits")

    if denied:
        return AuthorizationDecision(
            disposition="operator_required",
            policy_sha256=policy_digest,
            grant_ids=tuple(dict.fromkeys(accepted)),
            reasons=tuple(dict.fromkeys(denied)),
        )
    if incomplete:
        return AuthorizationDecision(
            disposition="foreground_residual",
            policy_sha256=policy_digest,
            grant_ids=tuple(dict.fromkeys(accepted)),
            reasons=tuple(dict.fromkeys(incomplete)),
        )
    return AuthorizationDecision(
        disposition="allowed",
        policy_sha256=policy_digest,
        grant_ids=tuple(dict.fromkeys(accepted)),
    )


__all__ = [
    "AuthorizationDecision",
    "AuthorizationDisposition",
    "AutonomyPolicy",
    "Capability",
    "CapabilityGrant",
    "CapabilityUse",
    "DEFAULT_AUTONOMY_POLICY",
    "Initiative",
    "authorize_action",
    "autonomy_policy_json",
    "autonomy_policy_sha256",
    "capability_target_from_text",
    "load_autonomy_policy_json",
]

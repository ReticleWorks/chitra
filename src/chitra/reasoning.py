"""Immutable decision attestations for dispatched reasoned orders.

The deterministic Chitra package does not call a model. What survives of the
reasoning path is the contract every reasoned dispatch must satisfy: an
immutable, content-addressed record bound to the exact approved text and the
frozen goal it was decided against.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chitra.autonomy import DEFAULT_AUTONOMY_POLICY, Capability, autonomy_policy_sha256

AuthorityClass = Literal["routine", "diagnostic", "small_delta", "corrective", "operator_required"]
DecisionSource = Literal["goal", "principle", "oracle-escalated", "foreground-residual", "kai-delegate"]


class ReasoningContractError(ValueError):
    """Raised when a required reasoning contract is invalid or stale."""


class DelegatedAuthority(BaseModel):
    """Provenance from the bridge's verification of Kai's authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: Literal["kai"] = "kai"
    grant_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    satisfaction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class DecisionAttestation(BaseModel):
    """Immutable pre-dispatch decision record bound to exact approved text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attestation_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    outcome: Literal["answer", "abstain"]
    message_kind: Literal["reasoned_answer", "reasoned_nudge", "reasoned_action"]
    approved_text: str = Field(min_length=1)
    approved_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: DecisionSource
    delegated_authority: DelegatedAuthority | None = None
    authority_class: AuthorityClass = "routine"
    goal_contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    goal_version: int = Field(ge=1)
    goal_fields: tuple[str, ...]
    corpus_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    principle_ids: tuple[str, ...] = ()
    principle_citations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    oracle_escalated: bool = False
    confidence_basis: str = Field(min_length=1)
    insufficiency_reasons: tuple[str, ...] = ()
    review_signal_id: str | None = None
    review_verdict: Literal["accept", "reject"] | None = None
    reviewer_count: int = Field(default=0, ge=0)
    autonomy_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_grant_ids: tuple[str, ...] = ()
    capability_requirements: tuple[Capability, ...] = ()
    autonomy: Literal["autonomous", "foreground_residual", "operator_required"]
    operator_gate_reasons: tuple[str, ...] = ()
    operator_confirmation_required: bool
    operator_confirmed: bool = False

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if self.approved_text_sha256 != hashlib.sha256(self.approved_text.encode("utf-8")).hexdigest():
            raise ValueError("approved_text_sha256 does not match approved_text")
        if self.source == "kai-delegate" and self.delegated_authority is None:
            raise ValueError("Kai-delegated decisions require delegated_authority")
        if self.source != "kai-delegate" and self.delegated_authority is not None:
            raise ValueError("delegated_authority is valid only for Kai-delegated decisions")
        if self.operator_confirmed and not self.operator_confirmation_required:
            raise ValueError("operator confirmation cannot be attached to an autonomous decision")
        if self.autonomy == "operator_required" and not self.operator_confirmation_required:
            raise ValueError("operator-required decisions must request operator confirmation")
        if self.autonomy != "operator_required" and self.operator_confirmation_required:
            raise ValueError("only operator-required decisions can request operator confirmation")
        if self.autonomy == "foreground_residual" and (
            self.outcome != "abstain" or self.operator_confirmation_required or self.operator_confirmed
        ):
            raise ValueError("foreground residuals must remain with Chitra and cannot request operator confirmation")
        if self.autonomy == "autonomous" and (self.outcome != "answer" or self.operator_confirmation_required):
            raise ValueError("autonomous decisions must be answer outcomes without an operator gate")
        payload = self.model_dump(mode="json", exclude={"attestation_id"})
        if self.delegated_authority is None:
            payload.pop("delegated_authority")
        expected = f"sha256:{hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}"
        if self.attestation_id != expected:
            raise ValueError("attestation_id does not match the attestation record")
        return self

    @classmethod
    def create(cls, **values: object) -> DecisionAttestation:
        """Build a fully bound attestation; ``None`` can never become text."""
        approved_text = values.get("approved_text")
        if not isinstance(approved_text, str) or not approved_text.strip():
            raise ReasoningContractError("approved_text must be non-empty text")
        payload: dict[str, object] = {
            "principle_ids": (),
            "principle_citations": (),
            "evidence_refs": (),
            "oracle_escalated": False,
            "insufficiency_reasons": (),
            "review_signal_id": None,
            "review_verdict": None,
            "reviewer_count": 0,
            "operator_gate_reasons": (),
            "operator_confirmed": False,
            "authority_class": "routine",
            "autonomy_policy_sha256": autonomy_policy_sha256(DEFAULT_AUTONOMY_POLICY),
            "capability_grant_ids": (),
            "capability_requirements": (),
            **values,
            "approved_text": approved_text,
            "approved_text_sha256": hashlib.sha256(approved_text.encode("utf-8")).hexdigest(),
        }
        delegated_authority = payload.get("delegated_authority")
        if delegated_authority is not None:
            payload["delegated_authority"] = DelegatedAuthority.model_validate(delegated_authority).model_dump(mode="json")
        attestation_id = f"sha256:{hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}"
        return cls.model_validate({**payload, "attestation_id": attestation_id})

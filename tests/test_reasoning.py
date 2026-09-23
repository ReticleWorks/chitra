from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from chitra.reasoning import (
    DecisionAttestation,
    DelegatedAuthority,
    ReasoningContractError,
)


def _kai_attestation(**overrides: object) -> DecisionAttestation:
    values: dict[str, object] = {
        "outcome": "answer",
        "message_kind": "reasoned_answer",
        "approved_text": "Continue with the existing repository pattern.",
        "source": "kai-delegate",
        "delegated_authority": DelegatedAuthority(
            principal="kai",
            grant_id="sha256:" + "1" * 64,
            grant_sha256="2" * 64,
            satisfaction_sha256="3" * 64,
            request_id="sha256:" + "4" * 64,
        ),
        "goal_contract_id": "sha256:" + "5" * 64,
        "goal_version": 1,
        "goal_fields": ("questions", "pursuit"),
        "corpus_id": "sha256:" + "6" * 64,
        "confidence_basis": "Kai verified the delegated scope and current evidence.",
        "autonomy": "autonomous",
        "operator_confirmation_required": False,
    }
    values.update(overrides)
    return DecisionAttestation.create(**values)


def test_none_cannot_be_hashed_or_attested_as_approved_text() -> None:
    payload = _kai_attestation().model_dump(mode="python", exclude={"attestation_id", "approved_text", "approved_text_sha256"})
    with pytest.raises(ReasoningContractError, match="approved_text"):
        DecisionAttestation.create(approved_text=None, **payload)


def test_kai_delegate_attestation_binds_verified_delegation() -> None:
    attestation = _kai_attestation()

    assert attestation.source == "kai-delegate"
    assert attestation.delegated_authority is not None
    assert attestation.delegated_authority.grant_id == "sha256:" + "1" * 64
    assert DecisionAttestation.model_validate_json(attestation.model_dump_json()) == attestation
    tampered = attestation.model_dump(mode="python")
    tampered["delegated_authority"]["satisfaction_sha256"] = "9" * 64
    with pytest.raises(ValidationError, match="attestation_id"):
        DecisionAttestation.model_validate(tampered)


def test_kai_delegate_attestation_requires_and_exclusively_owns_delegation() -> None:
    with pytest.raises(ValidationError, match="require delegated_authority"):
        _kai_attestation(delegated_authority=None)

    authority = _kai_attestation().delegated_authority
    with pytest.raises(ValidationError, match="valid only for Kai"):
        _kai_attestation(source="goal", delegated_authority=authority)


def test_kai_delegate_mapping_input_gets_canonical_defaults() -> None:
    authority = _kai_attestation().delegated_authority
    assert authority is not None

    attestation = _kai_attestation(delegated_authority=authority.model_dump(exclude={"principal"}))

    assert attestation.delegated_authority is not None
    assert attestation.delegated_authority.principal == "kai"


def test_pre_019_attestation_hash_remains_readable() -> None:
    approved_text = "Use the existing typed boundary."
    payload: dict[str, object] = {
        "outcome": "answer",
        "message_kind": "reasoned_answer",
        "approved_text": approved_text,
        "approved_text_sha256": hashlib.sha256(approved_text.encode()).hexdigest(),
        "source": "goal",
        "authority_class": "routine",
        "goal_contract_id": "sha256:" + "1" * 64,
        "goal_version": 1,
        "goal_fields": ("scope",),
        "corpus_id": "sha256:" + "2" * 64,
        "principle_ids": (),
        "principle_citations": (),
        "evidence_refs": (),
        "oracle_escalated": False,
        "confidence_basis": "the frozen goal directly determines this answer",
        "insufficiency_reasons": (),
        "review_signal_id": None,
        "review_verdict": None,
        "reviewer_count": 0,
        "autonomy_policy_sha256": "3" * 64,
        "capability_grant_ids": (),
        "capability_requirements": (),
        "autonomy": "autonomous",
        "operator_gate_reasons": (),
        "operator_confirmation_required": False,
        "operator_confirmed": False,
    }
    attestation_id = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    parsed = DecisionAttestation.model_validate({**payload, "attestation_id": attestation_id})

    assert parsed.source == "goal"

"""Kept unit guard: ``DecisionAttestation.create`` is an in-process builder --
the dispatch boundary only ever parses serialized attestation documents, so
the None-payload guard is unreachable through the real boundary.
"""

from __future__ import annotations

import pytest

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

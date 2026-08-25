"""Focused tests for independently signed interview enrollment."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from chitra.goals import (
    INTERVIEW_QUESTION_SET_ID,
    EnrolledDoneWhenItem,
    GoalRecord,
    GoalValidationError,
    InterviewAttestation,
    InterviewReceipt,
    upsert_goal,
)

SESSION_REF = "host:synthetic-lane:0.0"
PRODUCER_ID = "producer-test"


def _record(receipt: InterviewReceipt) -> GoalRecord:
    item = EnrolledDoneWhenItem(
        id="done-1",
        text="The signed interview enrollment is accepted.",
        validator="pytest",
        required_receipt="tests-green",
    )
    return GoalRecord(
        session_ref=SESSION_REF,
        goal="Ship the signed interview enrollment test safely today.",
        done_when=item.text,
        source="task-file:synthetic-goal",
        status="working",
        intent="Verify a producer-authenticated enrollment contract for this test.",
        scope="Synthetic enrollment test only.",
        interview_receipt=receipt,
        enrolled_done_when_items=(item,),
    )


def _attestation(
    private_key: Ed25519PrivateKey,
    *,
    session_ref: str = SESSION_REF,
    request_nonce: str = "nonce-test",
    question_set_id: str = INTERVIEW_QUESTION_SET_ID,
    answer_digest: str = "a" * 64,
) -> InterviewAttestation:
    unsigned = InterviewAttestation(
        producer_id=PRODUCER_ID,
        session_ref=session_ref,
        request_nonce=request_nonce,
        question_set_id=question_set_id,
        answer_digest=answer_digest,
        signature="",
    )
    return unsigned.model_copy(update={"signature": base64.b64encode(private_key.sign(unsigned.signing_bytes())).decode("ascii")})


def _trust_store(tmp_path: Path, private_key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch) -> None:
    public_key = private_key.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    path = tmp_path / "interview-trust.json"
    path.write_text(
        json.dumps(
            {
                "schema": "chitra.goals.interview-trust.v1",
                "producers": {PRODUCER_ID: {"public_key": base64.b64encode(public_key).decode("ascii")}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHITRA_INTERVIEW_TRUST_STORE", str(path))


def _receipt(attestation: InterviewAttestation | None = None) -> InterviewReceipt:
    return InterviewReceipt(
        name="synthetic-interview",
        completed_at="2026-08-25T00:00:00+00:00",
        answers_sha256="a" * 64,
        provenance=("source:test", "source:test", "source:test", "source:test"),
        request_nonce="nonce-test",
        attestation=attestation,
    )


def test_public_upsert_rejects_the_exact_placeholder_without_attestation(tmp_path: Path) -> None:
    receipt = replace(_receipt(), name="NO INTERVIEW WAS CONDUCTED - this text is a placeholder")

    with pytest.raises(GoalValidationError, match="attestation is required"):
        upsert_goal(tmp_path, _record(receipt))


def test_public_upsert_accepts_an_independent_producer_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_key = Ed25519PrivateKey.generate()
    _trust_store(tmp_path, private_key, monkeypatch)
    attestation = _attestation(private_key)

    stored = upsert_goal(tmp_path, _record(_receipt(attestation)))

    assert stored.interview_receipt is not None
    assert stored.interview_receipt.attestation == attestation


@pytest.mark.parametrize(
    "attestation_kwargs",
    [
        {"session_ref": "host:other-lane:0.0"},
        {"question_set_id": "chitra.goals.interview.other"},
        {"answer_digest": "b" * 64},
    ],
)
def test_public_upsert_rejects_attestation_with_wrong_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attestation_kwargs: dict[str, str]
) -> None:
    private_key = Ed25519PrivateKey.generate()
    _trust_store(tmp_path, private_key, monkeypatch)
    attestation = _attestation(private_key, **attestation_kwargs)

    with pytest.raises(GoalValidationError, match="attestation"):
        upsert_goal(tmp_path, _record(_receipt(attestation)))

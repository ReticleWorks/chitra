from __future__ import annotations

from chitra.completion_gate import CompletionEvidence
from chitra.goals import EnrolledDoneWhenItem, InterviewReceipt

VALID_INTERVIEW_RECEIPT = InterviewReceipt(
    name="interview:test-goal",
    completed_at="2026-08-21T12:00:00+00:00",
    answers_sha256="a" * 64,
    provenance=(
        "operator:test intent",
        "operator:test done condition",
        "operator:test scope exclusion",
        "operator:test constraint",
    ),
)


def enrollment_fields(
    done_when: str,
    *,
    item_id: str = "done-1",
    validator: str = "pytest",
    required_receipt: str = "tests-green",
) -> dict[str, object]:
    return {
        "interview_receipt": VALID_INTERVIEW_RECEIPT,
        "enrolled_done_when_items": (
            EnrolledDoneWhenItem(
                id=item_id,
                text=done_when,
                validator=validator,
                required_receipt=required_receipt,
            ),
        ),
    }


def passing_completion_evidence(
    *,
    item_id: str = "done-1",
    validator: str = "pytest",
    receipt_name: str = "tests-green",
    kind: str = "artifact",
    citation: str = "proof /tmp/test-results.json",
) -> CompletionEvidence:
    return CompletionEvidence(
        kind=kind,  # type: ignore[arg-type]
        done_when_item_id=item_id,
        receipt_name=receipt_name,
        validator=validator,
        validator_result="pass",
        citation=citation,
    )

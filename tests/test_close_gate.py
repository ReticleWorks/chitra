"""Close-time inventory acceptance tests for the exact-receipt close gate."""

from __future__ import annotations

from chitra.close_gate import evaluate_structured_close_inventory
from chitra.completion_gate import CompletionEvidence
from chitra.goals import EnrolledDoneWhenItem


def test_structured_close_requires_exact_item_receipt_validator_result_and_citation() -> None:
    item = EnrolledDoneWhenItem(id="tests", text="The tests pass", validator="pytest", required_receipt="tests-green")
    wrong = CompletionEvidence(
        done_when_item_id="tests",
        receipt_name="wrong",
        validator="pytest",
        validator_result="pass",
        citation="proof /tmp/tests.json",
    )
    passing = wrong.model_copy(update={"receipt_name": "tests-green"})

    failed = evaluate_structured_close_inventory((item,), (wrong,))
    assert failed.verdict == "FAIL"
    assert "requires receipt 'tests-green'" in failed.summary
    assert evaluate_structured_close_inventory((item,), (passing,)).verdict == "PASS"

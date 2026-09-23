"""Close-time inventory acceptance tests for the exact-receipt close gate."""

from __future__ import annotations

from chitra.close_gate import (
    _recorded_descopes,
    evaluate_structured_close_inventory,
    parse_required_items,
)
from chitra.completion_gate import CompletionEvidence
from chitra.goals import EnrolledDoneWhenItem

F8_DONE_WHEN = "both the X client and the Y client pass live validation"


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


def test_parser_reads_atomic_conjunction_bullets_and_explicit_counts() -> None:
    assert [item.text for item in parse_required_items("The release artifact exists.")] == ["The release artifact exists"]
    assert [item.text for item in parse_required_items(F8_DONE_WHEN)] == [
        "the X client",
        "the Y client pass live validation",
    ]
    assert [item.text for item in parse_required_items("1. API deployed\n2. Probe passes\n- Docs updated")] == [
        "API deployed",
        "Probe passes",
        "Docs updated",
    ]
    counted = parse_required_items("2 live clients pass validation")
    assert counted[0].quantity == 2
    assert counted[0].counted_noun == "client"


def test_enrolled_scope_delta_is_visible_even_on_version_one_record() -> None:
    descopes = _recorded_descopes(
        F8_DONE_WHEN,
        "The X client passes live validation",
        goal_version=1,
        goal_history=(),
    )

    assert [item.text for item in descopes] == ["the Y client pass live validation"]

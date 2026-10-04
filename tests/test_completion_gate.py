"""Pipeline tests for the completion gate kept in the wave-1 burn-down.

The dispatchd boundary tests exercise the order -> gate -> dispatch path end
to end: an order file in, a result out, and -- on the deny paths -- proof that
``dispatch_to_tmux`` is never reached. Gate internals (taxonomy loading,
deferral-phrase scanning, exemplar audits) were removed; the dispute deny
path they feed is asserted here at the boundary. The registered-validator
section is restored verbatim from main: a stored disk result -- or its
absence -- decides the receipt, never the lane's claim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import chitra.dispatchd as dispatchd_mod
from chitra.completion_gate import (
    CompletionEvidence,
    TodoItem,
    completion_receipt_issues,
    extract_completion_evidence,
)
from chitra.dispatch import DispatchOrder, DispatchStatus
from chitra.dispatchd import process_one_order

GOOD_CLAIM = """The requested parser gate was completed and deployed at SHA abc1234.
It rejects unsupported completion claims before delivery. Live health probe status=200 with 12 requests."""
GOOD_EVIDENCE = [
    CompletionEvidence(kind="deploy", citation="deployed SHA abc1234"),
    CompletionEvidence(kind="live_verify", citation="live health probe status=200 with 12 requests"),
]


def test_dispatchd_blocks_delivery_on_completion_dispute_and_never_calls_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: object) -> None:  # pragma: no cover - must never be called
        call_count["n"] += 1
        raise AssertionError("dispatch_to_tmux must not be called for a disputed completion claim")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    (queue_dir / "results").mkdir(parents=True)
    (queue_dir / "processed").mkdir(parents=True)
    order = DispatchOrder(
        order_id="ord-audit-1",
        session_ref="localhost:s:0.0",
        nudge="the feature is done",
        completion_todo_items=[TodoItem(text="write tests", status="open")],
    )
    order_path = orders_dir / "ord-audit-1.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")

    result = process_one_order(
        order_path,
        orders_dir=orders_dir,
        results_dir=queue_dir / "results",
        processed_dir=queue_dir / "processed",
        lock_dir=tmp_path / "locks",
    )

    assert call_count["n"] == 0
    assert result is not None
    assert result.status == DispatchStatus.COMPLETION_DISPUTE
    assert "write tests" in result.reason
    assert (queue_dir / "processed" / "ord-audit-1.json").exists()


def test_legacy_true_evidence_booleans_cannot_auto_pass_without_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_dispatch(order: DispatchOrder, **kwargs: object) -> None:  # pragma: no cover - must never be called
        raise AssertionError("bare legacy booleans must not reach delivery")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    (queue_dir / "results").mkdir()
    (queue_dir / "processed").mkdir()
    payload = {
        "order_id": "legacy-bool",
        "session_ref": "localhost:s:0.0",
        "nudge": GOOD_CLAIM,
        "completion_todo_items": [],
        "completion_has_deploy_evidence": True,
        "completion_has_live_verify_evidence": True,
    }
    (orders_dir / "legacy-bool.json").write_text(json.dumps(payload), encoding="utf-8")

    result = process_one_order(
        orders_dir / "legacy-bool.json",
        orders_dir=orders_dir,
        results_dir=queue_dir / "results",
        processed_dir=queue_dir / "processed",
        lock_dir=tmp_path / "locks",
    )

    assert result is not None
    assert result.status == DispatchStatus.COMPLETION_DISPUTE
    assert "evidence citation" in result.reason


def test_dispatchd_proceeds_to_dispatch_on_clean_completion_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chitra.dispatch import DispatchResult

    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: object) -> DispatchResult:
        call_count["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT, reason="sent: test")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    (queue_dir / "results").mkdir(parents=True)
    (queue_dir / "processed").mkdir(parents=True)
    order = DispatchOrder(
        order_id="ord-audit-2",
        session_ref="localhost:s:0.0",
        nudge=GOOD_CLAIM,
        completion_todo_items=[],
        completion_evidence=GOOD_EVIDENCE,
    )
    order_path = orders_dir / "ord-audit-2.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")

    result = process_one_order(
        order_path,
        orders_dir=orders_dir,
        results_dir=queue_dir / "results",
        processed_dir=queue_dir / "processed",
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert call_count["n"] == 1
    assert result is not None
    assert result.status == DispatchStatus.SENT


def test_dispatchd_leaves_a_non_completion_nudge_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chitra.dispatch import DispatchResult

    def fake_dispatch(order: DispatchOrder, **kwargs: object) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    (queue_dir / "results").mkdir(parents=True)
    (queue_dir / "processed").mkdir(parents=True)
    order = DispatchOrder(order_id="ord-plain", session_ref="localhost:s:0.0", nudge="hi")
    order_path = orders_dir / "ord-plain.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")

    result = process_one_order(
        order_path,
        orders_dir=orders_dir,
        results_dir=queue_dir / "results",
        processed_dir=queue_dir / "processed",
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert result is not None
    assert result.status == DispatchStatus.SENT


# ---------------------------------------------------------------------------
# registered validators: Chitra's stored disk result replaces the claimed one
# ---------------------------------------------------------------------------


def _structured_claim_line(**overrides: object) -> str:
    payload: dict[str, object] = {
        "kind": "artifact",
        "done_when_item_id": "done-1",
        "receipt_name": "tests-green",
        "validator": "pytest",
        "validator_result": "pass",
        "citation": "proof /tmp/test-results.json",
    }
    payload.update(overrides)
    return "CHITRA-COMPLETION: " + json.dumps(payload, separators=(",", ":"))


def test_structured_completion_line_is_only_a_trigger_and_drops_the_claimed_result() -> None:
    evidence = extract_completion_evidence(_structured_claim_line())

    assert len(evidence) == 1
    assert evidence[0].validator_result is None
    assert evidence[0].receipt_name == "tests-green"


def test_has_structured_completion_line_detects_the_trigger_and_ignores_plain_text() -> None:
    from chitra.completion_gate import has_structured_completion_line

    assert has_structured_completion_line(_structured_claim_line())
    assert not has_structured_completion_line("the validator passed, trust me")
    assert not has_structured_completion_line("CHITRA-COMPLETION: {not json")


def _enrolled_item(validator: str = "pytest", receipt: str = "tests-green") -> Any:
    from chitra.goals import EnrolledDoneWhenItem

    return EnrolledDoneWhenItem(id="done-1", text="The suite passes.", validator=validator, required_receipt=receipt)


def test_disk_fail_result_disputes_a_claimed_pass() -> None:
    item = _enrolled_item()
    proof = CompletionEvidence(
        kind="artifact",
        done_when_item_id="done-1",
        receipt_name="tests-green",
        validator="pytest",
        validator_result=None,
        citation="proof /tmp/test-results.json",
    )

    issues = completion_receipt_issues([item], [proof], verified_results={"tests-green": "fail"})

    assert issues == ["done item 'done-1' has no passing validator result with a concrete citation"]


def test_disk_pass_result_closes_even_when_the_lane_claimed_failure() -> None:
    item = _enrolled_item()
    proof = CompletionEvidence(
        kind="artifact",
        done_when_item_id="done-1",
        receipt_name="tests-green",
        validator="pytest",
        validator_result="fail",
        citation="receipt validation-receipts/tests-green.json",
    )

    issues = completion_receipt_issues([item], [proof], verified_results={"tests-green": "pass"})

    assert issues == []


def test_missing_disk_result_for_a_required_receipt_stays_disputed() -> None:
    item = _enrolled_item()
    proof = CompletionEvidence(
        kind="artifact",
        done_when_item_id="done-1",
        receipt_name="tests-green",
        validator="pytest",
        validator_result="pass",
        citation="proof /tmp/test-results.json",
    )

    assert completion_receipt_issues([item], [proof], verified_results={})

"""Peer questions ride the governed session-message path with truthful receipts.

These are the two permanent W6f-refutation regressions, one per defect:
``test_presence_record_uses_the_contractual_mode_field`` pins DESIGN-v3
section 5's record contract (a ``mode`` of ``using`` or ``released``, never a
differently named field); ``test_peer_question_enters_the_existing_governed_dispatch_path``
pins that ``chitra.peer.say()`` enqueues a real ``DispatchOrder`` under the
configured Chitra queue for ``dispatchd`` to consume and verify. The third
test pins that locally recorded receipts only ever mirror dispatchd's own
durable artifacts and can never claim delivery that did not happen.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from chitra.orders import DispatchOrder
from chitra.peer import consume, inbox, say

_ORDER_ADAPTER = TypeAdapter(DispatchOrder)


def test_presence_record_uses_the_contractual_mode_field(tmp_path: Path) -> None:
    from chitra.presence import announce_released, announce_using, list_presence

    announce_using("monitor-a", "repo:shared", session="sess-a", root=tmp_path)
    line = (tmp_path / "presence" / "monitor-a.jsonl").read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["mode"] == "using"
    assert "state" not in payload

    announce_released("monitor-a", "repo:shared", session="sess-a", root=tmp_path)
    lines = (tmp_path / "presence" / "monitor-a.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(entry)["mode"] for entry in lines] == ["using", "released"]

    released = list_presence(root=tmp_path, include_released=True)
    assert [record.mode for record in released] == ["released"]
    assert list_presence(root=tmp_path) == []


def test_peer_question_enters_the_existing_governed_dispatch_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CHITRA_SHARED_DIR", str(tmp_path / "coordination"))
    monkeypatch.setenv("CHITRA_STATE_DIR", str(tmp_path / "state"))
    queue_dir = tmp_path / "state" / "queue"

    say("monitor-b", "Can you release repo:shared?", sender="monitor-a", message_id="q-1")

    orders = list((queue_dir / "orders").glob("*.json"))
    assert len(orders) == 1
    order = _ORDER_ADAPTER.validate_python(json.loads(orders[0].read_text(encoding="utf-8")))
    assert isinstance(order, DispatchOrder)
    assert order.nudge == "Can you release repo:shared?"
    assert order.order_id == "peer-q-1"
    host, session, pane = order.session_ref.split(":")
    assert session == "monitor-b"
    assert pane and host


def test_receipts_are_derived_from_the_governed_result(tmp_path: Path, monkeypatch) -> None:
    """The mirror can never claim delivery dispatchd did not perform."""
    monkeypatch.setenv("CHITRA_SHARED_DIR", str(tmp_path / "coordination"))
    monkeypatch.setenv("CHITRA_STATE_DIR", str(tmp_path / "state"))
    queue_dir = tmp_path / "state" / "queue"

    message = say("monitor-b", "Please coordinate.", sender="monitor-a", message_id="q-2")
    receipts = tmp_path / "coordination" / "inbox" / "monitor-b" / "receipts"
    assert (receipts / "dispatch" / "q-2.json").exists()
    assert not (receipts / "consumption" / "q-2.json").exists()

    result = {
        "order_id": message.order_id,
        "session_ref": message.session_ref,
        "status": "failed",
        "reason": "injected failure",
    }
    (queue_dir / "results").mkdir(parents=True, exist_ok=True)
    (queue_dir / "results" / f"{message.order_id}.json").write_text(json.dumps(result), encoding="utf-8")
    consume("monitor-b", root=tmp_path / "coordination", queue_dir=queue_dir)
    assert inbox("monitor-b", root=tmp_path / "coordination") != []
    assert not (receipts / "consumption" / "q-2.json").exists()

    result["status"] = "sent"
    (queue_dir / "results" / f"{message.order_id}.json").write_text(json.dumps(result), encoding="utf-8")
    consume("monitor-b", root=tmp_path / "coordination", queue_dir=queue_dir)
    consumption = json.loads((receipts / "consumption" / "q-2.json").read_text(encoding="utf-8"))
    assert consumption["kind"] == "consumption"
    assert consumption["order_id"] == message.order_id

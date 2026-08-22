"""Peer questions enter the governed queue; the mirror stays non-authoritative."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from chitra import peer_cli
from chitra.dispatch import enqueue_dispatch_order
from chitra.ledger import append_entry, load_or_create_signing_key
from chitra.orders import DispatchOrder, DispatchStatus
from chitra.peer import PeerMessageError, consume, inbox, say

_ORDER_ADAPTER = TypeAdapter(DispatchOrder)


def _sign_governed_delivery(order: DispatchOrder, text: str, state_dir: Path) -> None:
    """Record dispatchd's signed ledger proof for a delivered order."""
    key = load_or_create_signing_key(state_dir / "ledger.key")
    append_entry(
        state_dir / "ledger.jsonl",
        order_id=order.order_id,
        session_ref=order.session_ref,
        tag=order.tag,
        nudge=text,
        key=key,
    )


def _sent_result(order: DispatchOrder) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "session_ref": order.session_ref,
        "status": DispatchStatus.SENT.value,
        "reason": "sent: test",
    }


def test_say_enqueues_one_governed_order_per_question(tmp_path: Path) -> None:
    first_at = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=1)
    queue_dir = tmp_path / "queue"
    second = say(
        "monitor-c",
        "Can you release host:shared after the probe?",
        sender="monitor-b",
        message_id="message-2",
        sent_at=second_at,
        root=tmp_path / "coordination",
        queue_dir=queue_dir,
    )
    first = say(
        "monitor-c",
        "I am also using host:shared.",
        sender="monitor-a",
        message_id="message-1",
        sent_at=first_at,
        root=tmp_path / "coordination",
        queue_dir=queue_dir,
    )

    orders = sorted((queue_dir / "orders").glob("*.json"), key=lambda path: path.name)
    assert [path.stem for path in orders] == ["peer-message-1", "peer-message-2"]
    queued = [_ORDER_ADAPTER.validate_python(json.loads(path.read_text(encoding="utf-8"))) for path in orders]
    assert [order.nudge for order in queued] == [first.text, second.text]
    assert all(len(order.session_ref.split(":")) == 3 for order in queued)
    assert all(order.session_ref.split(":")[1] == "monitor-c" for order in queued)


def test_reusing_a_message_id_enqueues_the_order_only_once(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    coordination = tmp_path / "coordination"
    first = say("monitor-b", "first", sender="monitor-a", message_id="stable-id", root=coordination, queue_dir=queue_dir)
    retried = say("monitor-b", "first", sender="monitor-a", message_id="stable-id", root=coordination, queue_dir=queue_dir)

    assert retried == first
    assert [path.stem for path in (queue_dir / "orders").glob("*.json")] == ["peer-stable-id"]

    with pytest.raises(PeerMessageError, match="different content"):
        say("monitor-b", "changed", sender="monitor-a", message_id="stable-id", root=coordination, queue_dir=queue_dir)
    assert [path.stem for path in (queue_dir / "orders").glob("*.json")] == ["peer-stable-id"]


def test_peer_cli_says_and_reads_the_current_instances_inbox(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHITRA_SHARED_DIR", str(tmp_path / "coordination"))
    monkeypatch.setenv("CHITRA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHITRA_INSTANCE", "monitor-a")
    assert peer_cli.main(["say", "monitor-b", "Please coordinate repo:shared.", "--message-id", "cli-message"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["sender"] == "monitor-a"

    monkeypatch.setenv("CHITRA_INSTANCE", "monitor-b")
    assert peer_cli.main(["inbox"]) == 0
    messages = json.loads(capsys.readouterr().out)
    assert [(message["message_id"], message["text"]) for message in messages] == [
        ("cli-message", "Please coordinate repo:shared.")
    ]


def test_dispatch_receipt_records_entry_into_the_governed_queue(tmp_path: Path) -> None:
    message = say(
        "monitor-b",
        "coordinate repo:shared",
        sender="monitor-a",
        message_id="proof-1",
        sent_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        root=tmp_path / "coordination",
        queue_dir=tmp_path / "queue",
    )
    receipt = json.loads(
        (tmp_path / "coordination" / "inbox" / "monitor-b" / "receipts" / "dispatch" / "proof-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["kind"] == "dispatch"
    assert receipt["instance"] == "monitor-b"
    assert receipt["sender"] == "monitor-a"
    assert receipt["order_id"] == message.order_id
    assert receipt["queue_dir"] == str(tmp_path / "queue")
    assert receipt["text_sha256"] == hashlib.sha256(b"coordinate repo:shared").hexdigest()
    assert not (tmp_path / "coordination" / "inbox" / "monitor-b" / "receipts" / "consumption").exists()


def test_consumption_receipt_requires_a_sent_governed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHITRA_STATE_DIR", str(tmp_path / "state"))
    coordination = tmp_path / "coordination"
    queue_dir = tmp_path / "queue"
    message = say(
        "monitor-b", "coordinate repo:shared", sender="monitor-a", message_id="proof-2", root=coordination, queue_dir=queue_dir
    )
    receipts = coordination / "inbox" / "monitor-b" / "receipts"

    results = queue_dir / "results"
    results.mkdir(parents=True, exist_ok=True)
    result_path = results / f"{message.order_id}.json"
    result_path.write_text(
        json.dumps({"order_id": message.order_id, "status": DispatchStatus.FAILED.value}), encoding="utf-8"
    )
    consume("monitor-b", root=coordination, queue_dir=queue_dir)
    assert not (receipts / "consumption" / "proof-2.json").exists()
    assert inbox("monitor-b", root=coordination) != []

    order = _ORDER_ADAPTER.validate_python(
        json.loads((queue_dir / "orders" / f"{message.order_id}.json").read_text(encoding="utf-8"))
    )
    result_path.write_text(json.dumps(_sent_result(order)), encoding="utf-8")
    consume("monitor-b", root=coordination, queue_dir=queue_dir)
    assert not (receipts / "consumption" / "proof-2.json").exists()

    _sign_governed_delivery(order, "coordinate repo:shared", queue_dir.parent)
    result_path.write_text(
        json.dumps(dict(_sent_result(order), delivery_ledger_verified=True)), encoding="utf-8"
    )
    consume("monitor-b", root=coordination, queue_dir=queue_dir)
    consumption = json.loads((receipts / "consumption" / "proof-2.json").read_text(encoding="utf-8"))
    assert consumption["kind"] == "consumption"
    assert consumption["order_id"] == message.order_id
    assert consumption["text_sha256"] == hashlib.sha256(b"coordinate repo:shared").hexdigest()
    assert inbox("monitor-b", root=coordination) != []


def test_enqueue_is_the_only_delivery_writer_and_stays_idempotent(tmp_path: Path) -> None:
    order = DispatchOrder(order_id="peer-fixed-id", session_ref="localhost:monitor-b:main", nudge="hello")
    first = enqueue_dispatch_order(tmp_path, order)
    second = enqueue_dispatch_order(tmp_path, order)
    assert first == second

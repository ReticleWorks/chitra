"""Direct peer inbox delivery is atomic, ordered, and idempotent."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitra import peer_cli
from chitra.peer import PeerMessageError, inbox, say


def test_inbox_delivery_is_ordered_and_idempotent(tmp_path: Path) -> None:
    first_at = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=1)
    second = say(
        "monitor-c",
        "Can you release host:shared after the probe?",
        sender="monitor-b",
        message_id="message-2",
        sent_at=second_at,
        root=tmp_path,
    )
    first = say(
        "monitor-c",
        "I am also using host:shared.",
        sender="monitor-a",
        message_id="message-1",
        sent_at=first_at,
        root=tmp_path,
    )
    retried = say(
        "monitor-c",
        "Can you release host:shared after the probe?",
        sender="monitor-b",
        message_id="message-2",
        sent_at=second_at + timedelta(minutes=1),
        root=tmp_path,
    )

    assert retried == second
    assert inbox("monitor-c", root=tmp_path) == [first, second]
    assert inbox("monitor-c", root=tmp_path) == [first, second]
    inbox_dir = tmp_path / "inbox" / "monitor-c"
    assert sorted(path.name for path in inbox_dir.iterdir()) == ["message-1.json", "message-2.json"]


def test_reusing_a_message_id_for_different_content_is_rejected(tmp_path: Path) -> None:
    say("monitor-b", "first", sender="monitor-a", message_id="stable-id", root=tmp_path)
    with pytest.raises(PeerMessageError, match="different content"):
        say("monitor-b", "changed", sender="monitor-a", message_id="stable-id", root=tmp_path)


def test_peer_cli_says_and_reads_the_current_instances_inbox(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHITRA_SHARED_DIR", str(tmp_path))
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

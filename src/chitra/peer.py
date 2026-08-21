"""Atomic, file-per-message delivery to a peer monitor inbox."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chitra._fsio import write_json_atomic
from chitra.presence import shared_dir

_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class PeerMessageError(ValueError):
    """A peer message or inbox operation is invalid."""


@dataclass(frozen=True, slots=True)
class PeerMessage:
    """One durable direct message addressed to a monitor instance."""

    message_id: str
    instance: str
    sender: str
    text: str
    sent_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "message_id": self.message_id,
            "instance": self.instance,
            "sender": self.sender,
            "text": self.text,
            "sent_at": self.sent_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> PeerMessage:
        if not isinstance(payload, dict):
            raise PeerMessageError("peer message must be a JSON object")
        values = {key: payload.get(key) for key in ("message_id", "instance", "sender", "text", "sent_at")}
        if not all(isinstance(value, str) for value in values.values()):
            raise PeerMessageError("peer message fields must be strings")
        message = cls(
            message_id=str(values["message_id"]),
            instance=str(values["instance"]),
            sender=str(values["sender"]),
            text=str(values["text"]),
            sent_at=_normalize_time(str(values["sent_at"])),
        )
        _validate_component(message.message_id, "message id")
        _validate_component(message.instance, "instance")
        _validate_component(message.sender, "sender")
        if not message.text:
            raise PeerMessageError("message text must be non-empty")
        return message


def _validate_component(value: str, label: str) -> None:
    if _MESSAGE_ID_RE.fullmatch(value) is None:
        raise PeerMessageError(f"{label} must contain only letters, digits, dot, underscore, and hyphen")


def _normalize_time(value: datetime | str | None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PeerMessageError("sent_at must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PeerMessageError("sent_at must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _load_message(path: Path) -> PeerMessage:
    try:
        return PeerMessage.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise PeerMessageError(f"invalid peer message file: {path}") from exc


def say(
    instance: str,
    text: str,
    *,
    sender: str | None = None,
    message_id: str | None = None,
    sent_at: datetime | str | None = None,
    root: Path | None = None,
) -> PeerMessage:
    """Atomically deliver one message; reusing its ID delivers it only once."""
    _validate_component(instance, "instance")
    actual_sender = sender or os.environ.get("CHITRA_INSTANCE", "operator")
    _validate_component(actual_sender, "sender")
    if not text:
        raise PeerMessageError("message text must be non-empty")
    actual_id = message_id or uuid.uuid4().hex
    _validate_component(actual_id, "message id")
    inbox_dir = shared_dir(root) / "inbox" / instance
    destination = inbox_dir / f"{actual_id}.json"
    message = PeerMessage(actual_id, instance, actual_sender, text, _normalize_time(sent_at))
    if destination.exists():
        existing = _load_message(destination)
        if (existing.instance, existing.sender, existing.text) != (message.instance, message.sender, message.text):
            raise PeerMessageError(f"message id {actual_id!r} already names different content")
        return existing
    inbox_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(destination, message.to_dict(), fsync=True)
    delivered = _load_message(destination)
    if (delivered.instance, delivered.sender, delivered.text) != (message.instance, message.sender, message.text):
        raise PeerMessageError(f"message id {actual_id!r} was replaced by different content")
    return delivered


def inbox(instance: str, *, root: Path | None = None) -> list[PeerMessage]:
    """Read the addressed inbox in a stable order without consuming messages."""
    _validate_component(instance, "instance")
    inbox_dir = shared_dir(root) / "inbox" / instance
    if not inbox_dir.is_dir():
        return []
    messages: dict[str, PeerMessage] = {}
    for path in inbox_dir.glob("*.json"):
        message = _load_message(path)
        if message.instance != instance:
            raise PeerMessageError(f"message {message.message_id!r} is in the wrong inbox")
        messages.setdefault(message.message_id, message)
    return sorted(messages.values(), key=lambda message: (message.sent_at, message.message_id))


__all__ = ["PeerMessage", "PeerMessageError", "inbox", "say"]

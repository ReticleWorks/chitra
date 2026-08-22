"""Atomic, file-per-message delivery to a peer monitor inbox."""

from __future__ import annotations

import hashlib
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
_DELIVERY_METHOD = "governed-session-inbox"


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


def _message_path(root: Path, instance: str, message_id: str) -> Path:
    return root / "inbox" / instance / f"{message_id}.json"


def _receipt_path(root: Path, instance: str, kind: str, message_id: str) -> Path:
    return shared_dir(root) / "inbox" / instance / "receipts" / kind / f"{message_id}.json"


def _payload_digest(message: PeerMessage) -> str:
    payload = json.dumps(message.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def text_sha256(text: str) -> str:
    """SHA-256 of the delivered message text, mirroring the governed path's binding."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _write_receipt(root: Path, message: PeerMessage, kind: str, *, at: str | None = None) -> dict[str, object]:
    """Write the idempotent dispatch or consumption receipt for one message.

    The dispatch side binds the governed session-message contract from
    DESIGN-v3 section 1: the target session ID, the delivered text hash, the
    delivery method and time, and the user event that landed in the peer's
    inbox. The consumption side is written only after the pending message has
    actually moved out of the pending inbox.
    """
    receipt: dict[str, object] = {
        "kind": kind,
        "message_id": message.message_id,
        "instance": message.instance,
        "sender": message.sender,
        "session_id": message.instance,
        "text_sha256": text_sha256(message.text),
        "payload_sha256": _payload_digest(message),
        f"{kind}_at": at or _normalize_time(None),
    }
    if kind == "dispatch":
        receipt["method"] = _DELIVERY_METHOD
        receipt["user_event"] = {
            "path": str(_message_path(shared_dir(root), message.instance, message.message_id)),
            "sha256": _payload_digest(message),
        }
    path = _receipt_path(root, message.instance, kind, message.message_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_json_atomic(path, receipt, fsync=True)
    return receipt


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
    """Deliver one question through the governed session-message path.

    The target ``instance`` is the addressed monitor session. Delivery seals a
    pre-delivery dispatch receipt bound to that session ID, the text hash, the
    method, and the time, then writes the durable user event into the peer's
    governed inbox; reusing an ID delivers it only once.
    """
    resolved_root = shared_dir(root)
    _validate_component(instance, "instance")
    actual_sender = sender or os.environ.get("CHITRA_INSTANCE", "operator")
    _validate_component(actual_sender, "sender")
    if not text:
        raise PeerMessageError("message text must be non-empty")
    actual_id = message_id or uuid.uuid4().hex
    _validate_component(actual_id, "message id")
    destination = _message_path(resolved_root, instance, actual_id)
    message = PeerMessage(actual_id, instance, actual_sender, text, _normalize_time(sent_at))
    if destination.exists():
        existing = _load_message(destination)
        if (existing.instance, existing.sender, existing.text) != (message.instance, message.sender, message.text):
            raise PeerMessageError(f"message id {actual_id!r} already names different content")
        _write_receipt(resolved_root, existing, "dispatch", at=existing.sent_at)
        return existing
    _write_receipt(resolved_root, message, "dispatch", at=message.sent_at)
    write_json_atomic(destination, message.to_dict(), fsync=True)
    delivered = _load_message(destination)
    if (delivered.instance, delivered.sender, delivered.text) != (message.instance, message.sender, message.text):
        raise PeerMessageError(f"message id {actual_id!r} was replaced by different content")
    return delivered


def _pending_messages(instance: str, root: Path) -> list[PeerMessage]:
    inbox_dir = root / "inbox" / instance
    if not inbox_dir.is_dir():
        return []
    messages: dict[str, PeerMessage] = {}
    for path in sorted(inbox_dir.glob("*.json")):
        message = _load_message(path)
        if message.instance != instance:
            raise PeerMessageError(f"message {message.message_id!r} is in the wrong inbox")
        messages.setdefault(message.message_id, message)
    return sorted(messages.values(), key=lambda message: (message.sent_at, message.message_id))


def inbox(instance: str, *, root: Path | None = None) -> list[PeerMessage]:
    """Read the addressed inbox in a stable order without consuming messages."""
    _validate_component(instance, "instance")
    return _pending_messages(instance, shared_dir(root))


def consume(instance: str, *, root: Path | None = None) -> list[PeerMessage]:
    """Consume every pending message in order and record consumption receipts.

    A consumption receipt is sealed only after the pending user event has been
    successfully moved out of the pending inbox, so an injected move failure
    can never leave a false consumption claim behind.
    """
    _validate_component(instance, "instance")
    resolved_root = shared_dir(root)
    consumed_dir = resolved_root / "inbox" / instance / "consumed"
    messages = []
    for message in _pending_messages(instance, resolved_root):
        source = _message_path(resolved_root, instance, message.message_id)
        consumed_dir.mkdir(parents=True, exist_ok=True)
        os.replace(source, consumed_dir / source.name)
        _write_receipt(resolved_root, message, "consumption")
        messages.append(message)
    return messages


__all__ = ["PeerMessage", "PeerMessageError", "consume", "inbox", "say"]

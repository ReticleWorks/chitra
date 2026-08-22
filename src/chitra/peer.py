"""Direct peer questions routed through the governed session-message path.

DESIGN-v3 section 5: a direct question to a same-host peer monitor travels
the repository's existing governed session-message route -- a
``DispatchOrder`` enqueued under the configured Chitra queue for the running
``dispatchd`` to claim, deliver into the target session, and verify in the
session's own transcript. This module only manufactures that order plus a
non-authoritative mirror of it; every delivery or consumption claim recorded
here is derived from dispatchd's own durable artifacts, never asserted.
"""

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
from chitra.dispatch import enqueue_dispatch_order
from chitra.orders import DispatchOrder, DispatchStatus
from chitra.presence import shared_dir
from chitra.state_paths import default_queue_dir

_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_PEER_TASK_TYPE = "peer-question"
_LOCAL_HOST_ENV_VAR = "CHITRA_LOCAL_HOST"


class PeerMessageError(ValueError):
    """A peer message or queue operation is invalid."""


@dataclass(frozen=True, slots=True)
class PeerMessage:
    """One direct message addressed to a monitor instance's governed session."""

    message_id: str
    instance: str
    sender: str
    text: str
    sent_at: str
    session_ref: str
    order_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "message_id": self.message_id,
            "instance": self.instance,
            "sender": self.sender,
            "text": self.text,
            "sent_at": self.sent_at,
            "session_ref": self.session_ref,
            "order_id": self.order_id,
        }

    @classmethod
    def from_dict(cls, payload: object) -> PeerMessage:
        if not isinstance(payload, dict):
            raise PeerMessageError("peer message must be a JSON object")
        values = {
            key: payload.get(key)
            for key in ("message_id", "instance", "sender", "text", "sent_at", "session_ref", "order_id")
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise PeerMessageError("peer message fields must be strings")
        message = cls(
            message_id=str(values["message_id"]),
            instance=str(values["instance"]),
            sender=str(values["sender"]),
            text=str(values["text"]),
            sent_at=_normalize_time(str(values["sent_at"])),
            session_ref=str(values["session_ref"]),
            order_id=str(values["order_id"]),
        )
        _validate_component(message.message_id, "message id")
        _validate_component(message.instance, "instance")
        _validate_component(message.sender, "sender")
        _validate_session_ref(message.session_ref)
        _validate_component(message.order_id, "order id")
        if not message.text:
            raise PeerMessageError("message text must be non-empty")
        return message


def _validate_component(value: str, label: str) -> None:
    if _MESSAGE_ID_RE.fullmatch(value) is None:
        raise PeerMessageError(f"{label} must contain only letters, digits, dot, underscore, and hyphen")


def _validate_session_ref(session_ref: str) -> None:
    parts = session_ref.split(":")
    if len(parts) != 3:
        raise PeerMessageError("session_ref must follow host:session:pane")


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


def _mirror_path(root: Path, instance: str, message_id: str) -> Path:
    return root / "inbox" / instance / f"{message_id}.json"


def _receipt_path(root: Path, instance: str, kind: str, message_id: str) -> Path:
    return root / "inbox" / instance / "receipts" / kind / f"{message_id}.json"


def _payload_digest(message: PeerMessage) -> str:
    payload = json.dumps(message.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def text_sha256(text: str) -> str:
    """SHA-256 of the delivered message text, mirroring the governed path's binding."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _governed_result(queue_dir: Path, order_id: str) -> dict[str, object] | None:
    result_path = queue_dir / "results" / f"{order_id}.json"
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _record_derived_receipts(root: Path, queue_dir: Path, message: PeerMessage) -> None:
    """Mirror only what dispatchd already proved; never assert delivery.

    The dispatch receipt records that the question entered the governed
    queue. The consumption receipt is written only when dispatchd's own
    terminal result for the order exists with status ``sent`` -- a missing,
    failed, blocked, or pending delivery leaves no receipt behind.
    """
    receipt_dir = _receipt_path(root, message.instance, "dispatch", message.message_id).parent
    receipt_dir.mkdir(parents=True, exist_ok=True)
    dispatch_path = receipt_dir / f"{message.message_id}.json"
    if not dispatch_path.exists():
        write_json_atomic(
            dispatch_path,
            {
                "kind": "dispatch",
                "message_id": message.message_id,
                "order_id": message.order_id,
                "queue_dir": str(queue_dir),
                "instance": message.instance,
                "sender": message.sender,
                "session_ref": message.session_ref,
                "text_sha256": text_sha256(message.text),
                "payload_sha256": _payload_digest(message),
                "dispatched_at": message.sent_at,
            },
            fsync=True,
        )

    result = _governed_result(queue_dir, message.order_id)
    if isinstance(result, dict) and result.get("status") == DispatchStatus.SENT.value:
        consumed_path = _receipt_path(root, message.instance, "consumption", message.message_id)
        if not consumed_path.exists():
            consumed_path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(
                consumed_path,
                {
                    "kind": "consumption",
                    "message_id": message.message_id,
                    "order_id": message.order_id,
                    "result_path": str(queue_dir / "results" / f"{message.order_id}.json"),
                    "text_sha256": text_sha256(message.text),
                    "payload_sha256": _payload_digest(message),
                    "consumed_at": _normalize_time(None),
                },
                fsync=True,
            )


def say(
    instance: str,
    text: str,
    *,
    session: str | None = None,
    pane: str = "main",
    sender: str | None = None,
    message_id: str | None = None,
    sent_at: datetime | str | None = None,
    root: Path | None = None,
    queue_dir: Path | None = None,
) -> PeerMessage:
    """Deliver one question through the governed session-message path.

    A real ``DispatchOrder`` bound to the target session and the verbatim
    text is enqueued under the configured Chitra queue for ``dispatchd`` to
    consume, paste into that session, and verify externally; reusing an ID
    enqueues it only once. The shared-dir mirror is a non-authoritative copy
    of what was asked, never proof that it arrived.
    """
    resolved_root = shared_dir(root)
    _validate_component(instance, "instance")
    actual_sender = sender or os.environ.get("CHITRA_INSTANCE", "operator")
    _validate_component(actual_sender, "sender")
    if not text:
        raise PeerMessageError("message text must be non-empty")
    actual_id = message_id or uuid.uuid4().hex
    _validate_component(actual_id, "message id")

    host = os.environ.get(_LOCAL_HOST_ENV_VAR, "").strip().split(".", 1)[0] or "localhost"
    session_ref = f"{host}:{session or instance}:{pane}"
    _validate_session_ref(session_ref)

    resolved_queue = queue_dir if queue_dir is not None else default_queue_dir()
    order_id = f"peer-{actual_id}"
    existing_mirror = _mirror_path(resolved_root, instance, actual_id)
    if existing_mirror.exists():
        existing = _load_message(existing_mirror)
        if (existing.instance, existing.sender, existing.text) != (instance, actual_sender, text):
            raise PeerMessageError(f"message id {actual_id!r} already names different content")
        _record_derived_receipts(resolved_root, resolved_queue, existing)
        return existing

    order = DispatchOrder(order_id=order_id, session_ref=session_ref, nudge=text, task_type=_PEER_TASK_TYPE)
    enqueue_dispatch_order(resolved_queue, order)

    message = PeerMessage(
        actual_id,
        instance,
        actual_sender,
        text,
        _normalize_time(sent_at),
        session_ref,
        order_id,
    )
    destination = _mirror_path(resolved_root, instance, actual_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(destination, message.to_dict(), fsync=True)
    delivered = _load_message(destination)
    if (delivered.instance, delivered.sender, delivered.text) != (message.instance, message.sender, message.text):
        raise PeerMessageError(f"message id {actual_id!r} was replaced by different content")
    _record_derived_receipts(resolved_root, resolved_queue, delivered)
    return delivered


def _load_message(path: Path) -> PeerMessage:
    try:
        return PeerMessage.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise PeerMessageError(f"invalid peer message file: {path}") from exc


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
    """Read the addressed mirror inbox in stable order without consuming anything.

    Reading this surface consumes nothing and proves nothing; the governing
    record of a peer question is its ``DispatchOrder`` and dispatchd's own
    result for it.
    """
    _validate_component(instance, "instance")
    return _pending_messages(instance, shared_dir(root))


def consume(instance: str, *, root: Path | None = None, queue_dir: Path | None = None) -> list[PeerMessage]:
    """Refresh receipts from dispatchd's durable artifacts without hiding anything.

    This is not the governed consumer -- only ``dispatchd`` consumes orders.
    Messages stay listed regardless of delivery state, so nothing can
    disappear before its governed result exists.
    """
    _validate_component(instance, "instance")
    resolved_root = shared_dir(root)
    resolved_queue = queue_dir if queue_dir is not None else default_queue_dir()
    messages = []
    for message in _pending_messages(instance, resolved_root):
        _record_derived_receipts(resolved_root, resolved_queue, message)
        messages.append(message)
    return messages


__all__ = ["PeerMessage", "PeerMessageError", "consume", "inbox", "say"]

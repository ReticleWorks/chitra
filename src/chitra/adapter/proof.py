"""Backend-neutral read proof over canonical events.

The rule is the same one ``dispatch.transcript_confirms_nudge`` applies to raw
JSONL: the order's exact text must appear in a user-input record, and at least
one later consuming event (tool call, tool result, tool error, or final
response) must follow it in the same session's stream. A user record without
subsequent activity — or a harness ack alone — is ``accepted``, not
``consumed``.
"""

from __future__ import annotations

from collections.abc import Iterable

from chitra.adapter.contract import ReadLevel, ReadProof
from chitra.journal.models import CanonicalEvent, CanonicalType

_CONSUMING_TYPES = frozenset(
    {
        CanonicalType.TOOL_CALL,
        CanonicalType.TOOL_RESULT,
        CanonicalType.TOOL_ERROR,
        CanonicalType.FINAL_RESPONSE,
    }
)


def _is_user_input(event: CanonicalEvent) -> bool:
    """True when the event is an arriving order: native ``user`` type carrying text."""
    return event.native_type == "user" and event.payload.get("native_type") == "user"


def _user_text(event: CanonicalEvent) -> str | None:
    """The user record's text, wherever the normalizer kept it.

    Codex user messages carry ``payload["text"]``. Claude user records
    normalize to UNKNOWN events whose payload has no text, so the text comes
    back out of ``raw_record.message.content`` (string or text blocks).
    """
    text = event.payload.get("text")
    if isinstance(text, str):
        return text
    record = event.raw_record
    if not isinstance(record, dict):
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else record.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        if parts:
            return "\n".join(parts)
    return None


def prove_read_from_events(
    *,
    order_id: str,
    order_text: str,
    events: Iterable[CanonicalEvent],
    native_session_id: str | None = None,
    binding_ref: str | None = None,
    acked: bool = False,
) -> ReadProof:
    """Prove consumption of ``order_text`` over a canonical event stream.

    ``native_session_id`` pins the proof to one session so activity from
    another lane cannot satisfy it. ``acked`` records that the harness's own
    send path reported the write accepted (steer delivered, composer cleared):
    it lifts an unproven read to ``accepted`` instead of ``none``.
    """
    if not order_text:
        return ReadProof(order_id=order_id, level="none", detail="empty order text", binding_ref=binding_ref)

    stream = [
        event
        for event in events
        if native_session_id is None or event.session_id == native_session_id
    ]
    last_input_index: int | None = None
    for index, event in enumerate(stream):
        text = _user_text(event)
        if _is_user_input(event) and text is not None and order_text in text:
            last_input_index = index

    if last_input_index is None:
        level: ReadLevel = "accepted" if acked else "none"
        detail = (
            "harness acknowledged the write but the order text is not in the lane's event stream"
            if acked
            else "order text not found as a user-input record in the lane's event stream"
        )
        return ReadProof(order_id=order_id, level=level, detail=detail, binding_ref=binding_ref)

    for event in stream[last_input_index + 1 :]:
        if event.normalized_type in _CONSUMING_TYPES:
            return ReadProof(
                order_id=order_id,
                level="consumed",
                detail="order recorded as user input and agent activity followed",
                input_event_id=stream[last_input_index].event_id,
                consuming_event_id=event.event_id,
                native_session_id=native_session_id,
                binding_ref=binding_ref,
            )
    return ReadProof(
        order_id=order_id,
        level="accepted",
        detail="order recorded as user input but no agent/tool activity followed yet",
        input_event_id=stream[last_input_index].event_id,
        native_session_id=native_session_id,
        binding_ref=binding_ref,
    )

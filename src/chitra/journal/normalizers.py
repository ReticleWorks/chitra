"""Version-gated normalizers for observed Claude Code and Codex JSONL."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .models import (
    CanonicalEvent,
    CanonicalType,
    Client,
    LifecycleReceipt,
    RawRecord,
    TranscriptIdentity,
)

NORMALIZER_VERSION = "chitra-journal-normalizer.v1"
SUPPORTED_VERSIONS: dict[Client, frozenset[str]] = {
    Client.CLAUDE: frozenset({"2.1.229"}),
    Client.CODEX: frozenset({"0.149.0"}),
}


class UnsupportedClientVersion(ValueError):
    """The transcript has not passed the fixture gate for this client version."""


@dataclass(frozen=True)
class NormalizationContext:
    instance: str
    lane: str
    client: Client
    client_version: str
    process_id: str | None = None
    session_id: str | None = None
    resume_id: str | None = None
    observed_at: str | None = None
    goal_ref: str | None = None
    item_ref: str | None = None


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _native_key(record: dict[str, Any], raw_sha256: str) -> str:
    for key in ("uuid", "id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}:sha256:{raw_sha256}"
    payload = record.get("payload")
    if isinstance(payload, dict):
        for key in ("id", "call_id", "turn_id", "window_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return f"payload.{key}:{value}:sha256:{raw_sha256}"
    timestamp = record.get("timestamp")
    return f"timestamp:{timestamp!s}:sha256:{raw_sha256}"


def _nested_exit_code(output: Any) -> int | None:
    for block in _blocks(output):
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            value = decoded.get("exit_code")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


class TranscriptNormalizer:
    """Stateful normalizer for one transcript stream."""

    def __init__(self, context: NormalizationContext) -> None:
        supported = SUPPORTED_VERSIONS[context.client]
        if context.client_version not in supported:
            raise UnsupportedClientVersion(
                f"unsupported {context.client.value} version {context.client_version!r}; "
                f"fixture-gated versions: {', '.join(sorted(supported))}"
            )
        self.context = context
        self.session_id = context.session_id
        self.resume_id = context.resume_id
        self._native_occurrences: dict[str, int] = {}
        self._current_occurrence = 0

    def _begin_record(self, raw: RawRecord) -> None:
        key = _native_key(raw.record or {}, raw.raw_sha256)
        occurrence = self._native_occurrences.get(key, 0)
        self._native_occurrences[key] = occurrence + 1
        self._current_occurrence = occurrence

    def normalize(self, raw: RawRecord) -> tuple[CanonicalEvent, ...]:
        raise NotImplementedError

    def bind_resume(self, receipt: LifecycleReceipt, transcript: TranscriptIdentity) -> CanonicalEvent:
        if receipt.session_id != self.session_id:
            raise ValueError("resume receipt session_id does not match transcript session")
        self.resume_id = receipt.resume_id
        payload = {
            "receipt_id": receipt.receipt_id,
            "method": receipt.method,
            "evidence": receipt.evidence,
        }
        payload_digest = _canonical_digest(payload)
        event_id = _canonical_digest(
            {
                "client": self.context.client.value,
                "session_id": receipt.session_id,
                "receipt_id": receipt.receipt_id,
                "normalized_type": CanonicalType.RESUME.value,
            }
        )
        return CanonicalEvent(
            event_id=event_id,
            instance=self.context.instance,
            lane=self.context.lane,
            client=self.context.client,
            client_version=self.context.client_version,
            process_id=self.context.process_id,
            transcript=transcript,
            session_id=receipt.session_id,
            resume_id=receipt.resume_id,
            observed_at=receipt.occurred_at,
            native_time=None,
            native_type="lifecycle.resume",
            native_join_id=receipt.receipt_id,
            raw_byte_range=None,
            raw_sha256=None,
            lifecycle_receipt=receipt,
            normalized_type=CanonicalType.RESUME,
            goal_ref=self.context.goal_ref,
            item_ref=self.context.item_ref,
            payload_digest=payload_digest,
            normalizer_version=NORMALIZER_VERSION,
            payload=payload,
            raw_record=None,
        )

    def _event(
        self,
        raw: RawRecord,
        normalized_type: CanonicalType,
        *,
        slot: str,
        payload: dict[str, Any],
        native_join_id: str | None = None,
    ) -> CanonicalEvent:
        record = raw.record or {}
        native_time_value = record.get("timestamp")
        native_time = native_time_value if isinstance(native_time_value, str) else None
        native_type_value = record.get("type")
        native_type = native_type_value if isinstance(native_type_value, str) else "invalid_json"
        payload_digest = _canonical_digest(payload)
        event_id = _canonical_digest(
            {
                "client": self.context.client.value,
                "session_id": self.session_id or "unknown",
                "native_key": _native_key(record, raw.raw_sha256),
                "native_occurrence": self._current_occurrence,
                "normalized_type": normalized_type.value,
                "slot": slot,
                "payload_digest": payload_digest,
            }
        )
        observed_at = self.context.observed_at or native_time or datetime.now(UTC).isoformat()
        return CanonicalEvent(
            event_id=event_id,
            instance=self.context.instance,
            lane=self.context.lane,
            client=self.context.client,
            client_version=self.context.client_version,
            process_id=self.context.process_id,
            transcript=raw.transcript,
            session_id=self.session_id or "unknown",
            resume_id=self.resume_id,
            observed_at=observed_at,
            native_time=native_time,
            native_type=native_type,
            native_join_id=native_join_id,
            raw_byte_range=raw.byte_range,
            raw_sha256=raw.raw_sha256,
            normalized_type=normalized_type,
            goal_ref=self.context.goal_ref,
            item_ref=self.context.item_ref,
            payload_digest=payload_digest,
            normalizer_version=NORMALIZER_VERSION,
            payload=payload,
            raw_record=raw.record,
        )

    def _unknown(self, raw: RawRecord) -> CanonicalEvent:
        record = raw.record or {}
        return self._event(
            raw,
            CanonicalType.UNKNOWN,
            slot="record",
            payload={
                "native_type": record.get("type"),
                "native_subtype": record.get("subtype"),
                "decode_error": raw.decode_error,
            },
        )


class ClaudeNormalizer(TranscriptNormalizer):
    def __init__(self, context: NormalizationContext) -> None:
        if context.client is not Client.CLAUDE:
            raise ValueError("ClaudeNormalizer requires the claude client")
        super().__init__(context)
        self._pending_text: tuple[str, str | None] | None = None
        self._stop_hook_seen = False

    def normalize(self, raw: RawRecord) -> tuple[CanonicalEvent, ...]:
        self._begin_record(raw)
        if raw.record is None:
            return (self._unknown(raw),)
        record = raw.record
        version = record.get("version")
        if isinstance(version, str) and version != self.context.client_version:
            raise UnsupportedClientVersion(f"Claude record version changed to {version!r}")
        session_id = record.get("sessionId")
        if isinstance(session_id, str):
            if self.session_id is not None and self.session_id != session_id:
                raise ValueError("Claude sessionId changed within one transcript")
            self.session_id = session_id

        events: list[CanonicalEvent] = []
        record_type = record.get("type")
        message = record.get("message")
        if record_type == "assistant" and isinstance(message, dict):
            for index, block in enumerate(_blocks(message.get("content"))):
                if block.get("type") == "tool_use":
                    call_id = block.get("id")
                    if isinstance(call_id, str):
                        events.append(
                            self._event(
                                raw,
                                CanonicalType.TOOL_CALL,
                                slot=f"tool_use:{index}",
                                native_join_id=call_id,
                                payload={
                                    "call_id": call_id,
                                    "tool_name": block.get("name"),
                                    "input": block.get("input"),
                                    "cwd": record.get("cwd"),
                                },
                            )
                        )
                elif block.get("type") == "text" and isinstance(block.get("text"), str):
                    message_id = message.get("id")
                    self._pending_text = (
                        block["text"],
                        message_id if isinstance(message_id, str) else None,
                    )
                    self._stop_hook_seen = False
        elif record_type == "user" and isinstance(message, dict):
            for index, block in enumerate(_blocks(message.get("content"))):
                if block.get("type") != "tool_result":
                    continue
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str):
                    continue
                is_error = block.get("is_error") is True
                events.append(
                    self._event(
                        raw,
                        CanonicalType.TOOL_ERROR if is_error else CanonicalType.TOOL_RESULT,
                        slot=f"tool_result:{index}",
                        native_join_id=call_id,
                        payload={
                            "call_id": call_id,
                            "content": block.get("content"),
                            "is_error": is_error,
                            "tool_use_result": record.get("toolUseResult"),
                        },
                    )
                )
        elif record_type == "system" and record.get("subtype") == "stop_hook_summary":
            if self._pending_text is not None:
                self._stop_hook_seen = True
        elif record_type == "system" and record.get("subtype") == "turn_duration":
            if self._pending_text is not None and self._stop_hook_seen:
                text, message_id = self._pending_text
                events.append(
                    self._event(
                        raw,
                        CanonicalType.FINAL_RESPONSE,
                        slot="turn_boundary",
                        native_join_id=message_id,
                        payload={"text": text, "message_id": message_id},
                    )
                )
                self._pending_text = None
                self._stop_hook_seen = False
        elif record_type == "system" and record.get("subtype") == "compact_boundary":
            events.append(
                self._event(
                    raw,
                    CanonicalType.COMPACTION,
                    slot="compact_boundary",
                    payload={"compact_metadata": record.get("compactMetadata")},
                )
            )
        if not events:
            events.append(self._unknown(raw))
        return tuple(events)


class CodexNormalizer(TranscriptNormalizer):
    def __init__(self, context: NormalizationContext) -> None:
        if context.client is not Client.CODEX:
            raise ValueError("CodexNormalizer requires the codex client")
        super().__init__(context)
        self._pending_text: tuple[str, str | None] | None = None

    def normalize(self, raw: RawRecord) -> tuple[CanonicalEvent, ...]:
        self._begin_record(raw)
        if raw.record is None:
            return (self._unknown(raw),)
        record = raw.record
        record_type = record.get("type")
        payload = record.get("payload")
        if record_type == "session_meta" and isinstance(payload, dict):
            version = payload.get("cli_version")
            if version != self.context.client_version:
                raise UnsupportedClientVersion(f"Codex session version changed to {version!r}")
            candidate = payload.get("id")
            if isinstance(candidate, str):
                if self.session_id is not None and self.session_id != candidate:
                    raise ValueError("Codex session id changed within one transcript")
                self.session_id = candidate
            return (self._unknown(raw),)
        if record_type == "response_item" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type == "custom_tool_call":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    return (
                        self._event(
                            raw,
                            CanonicalType.TOOL_CALL,
                            slot="custom_tool_call",
                            native_join_id=call_id,
                            payload={
                                "call_id": call_id,
                                "tool_name": payload.get("name"),
                                "input": payload.get("input"),
                            },
                        ),
                    )
            elif payload_type == "custom_tool_call_output":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    exit_code = _nested_exit_code(payload.get("output"))
                    return (
                        self._event(
                            raw,
                            CanonicalType.TOOL_ERROR if exit_code not in (None, 0) else CanonicalType.TOOL_RESULT,
                            slot="custom_tool_call_output",
                            native_join_id=call_id,
                            payload={
                                "call_id": call_id,
                                "output": payload.get("output"),
                                "exit_code": exit_code,
                            },
                        ),
                    )
            elif payload_type == "message" and payload.get("role") == "assistant":
                texts = [block["text"] for block in _blocks(payload.get("content")) if isinstance(block.get("text"), str)]
                if texts:
                    message_id = payload.get("id")
                    self._pending_text = (
                        "\n".join(texts),
                        message_id if isinstance(message_id, str) else None,
                    )
                return (self._unknown(raw),)
        elif record_type == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "task_started":
                self._pending_text = None
            elif payload.get("type") == "task_complete" and self._pending_text is not None:
                text, message_id = self._pending_text
                self._pending_text = None
                turn_id = payload.get("turn_id")
                return (
                    self._event(
                        raw,
                        CanonicalType.FINAL_RESPONSE,
                        slot="task_complete",
                        native_join_id=turn_id if isinstance(turn_id, str) else message_id,
                        payload={
                            "text": text,
                            "message_id": message_id,
                            "turn_id": turn_id,
                        },
                    ),
                )
        elif record_type == "compacted" and isinstance(payload, dict):
            return (
                self._event(
                    raw,
                    CanonicalType.COMPACTION,
                    slot="compacted",
                    native_join_id=payload.get("window_id") if isinstance(payload.get("window_id"), str) else None,
                    payload={
                        "window_number": payload.get("window_number"),
                        "first_window_id": payload.get("first_window_id"),
                        "previous_window_id": payload.get("previous_window_id"),
                        "window_id": payload.get("window_id"),
                        "replacement_history_digest": _canonical_digest(payload.get("replacement_history")),
                    },
                ),
            )
        return (self._unknown(raw),)


def make_normalizer(context: NormalizationContext) -> TranscriptNormalizer:
    if context.client is Client.CLAUDE:
        return ClaudeNormalizer(context)
    return CodexNormalizer(context)

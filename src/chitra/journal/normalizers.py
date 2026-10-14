"""Normalizers for observed Claude Code, Codex, Devin, and Amp JSONL.

Client coverage is explicit: every known record type maps to a canonical
event type (tool evidence, user input, lifecycle, or ``context`` metadata),
and only genuinely unrecognized record types land on ``unknown`` -- unknown
remains the drift signal, never the default for recognized noise.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    CanonicalEvent,
    CanonicalType,
    Client,
    LifecycleReceipt,
    RawRecord,
    TranscriptIdentity,
)
from .reader import JsonlTailReader

NORMALIZER_VERSION = "chitra-journal-normalizer.v2"


@dataclass(frozen=True)
class NormalizationContext:
    instance: str
    lane: str
    # Known clients are Client members; a plug-owned harness name (e.g. "amp")
    # is kept as a string so strict bindings stay portable across backends.
    # Harnesses with no JSONL normalizer are ingested through plug event
    # readers instead of this path.
    client: Client | str
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


def queued_operator_prompt(record: object) -> str | None:
    """Return the text of input typed into a busy Claude session, else None.

    Claude Code records such input as an ``attachment`` record of type
    ``queued_command``, not a ``user`` record. Task notifications use the same
    shape without ``origin``, and their text can be written by the lane's own
    subagents, so only an origin-bearing attachment counts as input.
    """
    if not isinstance(record, dict) or record.get("type") != "attachment":
        return None
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return None
    if not isinstance(attachment.get("origin"), dict):
        return None
    prompt = attachment.get("prompt")
    return prompt if isinstance(prompt, str) else None


def hook_additional_context(record: object) -> str | None:
    """Return the context text a successful Claude hook injected, else None.

    A PostToolUse hook can deliver an order while a turn is running; Claude
    Code records it as an ``attachment`` record of type ``hook_success`` whose
    ``stdout`` is JSON carrying the injected text under
    ``hookSpecificOutput.additionalContext``. Only that exact shape counts as
    delivered input -- a malformed stdout payload or a hook result without
    additional context is ordinary hook output, not an operator message.
    """
    if not isinstance(record, dict) or record.get("type") != "attachment":
        return None
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "hook_success":
        return None
    stdout = attachment.get("stdout")
    if not isinstance(stdout, str):
        return None
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    hook_output = payload.get("hookSpecificOutput")
    if not isinstance(hook_output, dict):
        return None
    context = hook_output.get("additionalContext")
    return context if isinstance(context, str) and context else None


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


_TASK_NOTIFICATION_TASK_ID_RE = re.compile(r"<task-id>(.*?)</task-id>", re.S)
_TASK_NOTIFICATION_STATUS_RE = re.compile(r"<status>(.*?)</status>", re.S)
_TASK_NOTIFICATION_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)


def _parse_task_notification(content: str) -> dict[str, str | None] | None:
    """Parse a background-task notification's task id and terminal status.

    A launched background command's tool-use id is absent from roughly a
    third of real notifications (measured), so the caller must join back to
    the originating tool call by ``task-id``, learned from the launch
    placeholder -- never by tool-use id alone. A notification with no
    ``<status>`` is a mid-task ``<event>`` tick, not a completion; callers
    must not treat it as one.
    """
    if "<task-notification>" not in content:
        return None
    task_id_match = _TASK_NOTIFICATION_TASK_ID_RE.search(content)
    if task_id_match is None:
        return None
    status_match = _TASK_NOTIFICATION_STATUS_RE.search(content)
    summary_match = _TASK_NOTIFICATION_SUMMARY_RE.search(content)
    return {
        "task_id": task_id_match.group(1).strip(),
        "status": status_match.group(1).strip() if status_match else None,
        "summary": summary_match.group(1).strip() if summary_match else None,
    }


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
        self.context = context
        self.session_id = context.session_id
        self.resume_id = context.resume_id
        # SQLite's unnamed database lives on disk and is removed on close.
        self._occurrences = sqlite3.connect("", check_same_thread=False)
        self._occurrences.execute("PRAGMA cache_size=-256")
        self._occurrences.execute("CREATE TABLE occurrences (key TEXT PRIMARY KEY, count INTEGER)")
        self._occurrences.execute("CREATE TABLE heads (key TEXT PRIMARY KEY)")
        self._current_occurrence = 0

    def close(self) -> None:
        self._occurrences.close()

    def seen_generation_head(self, key: str) -> bool:
        seen = self._occurrences.execute("SELECT 1 FROM heads WHERE key=?", (key,)).fetchone() is not None
        self._occurrences.execute("INSERT OR IGNORE INTO heads VALUES (?)", (key,))
        return seen

    def begin_replay(self) -> None:
        """Restart per-incarnation stream state before a full-file replay.

        A detected rewrite -- the same inode mutated in place, or the path
        swapped for a new inode by an atomic replace -- replays the whole
        current transcript from byte zero. Occurrence numbering must restart
        with it so every unchanged record reproduces the event ID it already
        received; otherwise the journal would append duplicates of durable
        events.
        """
        self._occurrences.execute("DELETE FROM occurrences")
        self._current_occurrence = 0

    def _begin_record(self, raw: RawRecord) -> None:
        key = _native_key(raw.record or {}, raw.raw_sha256)
        row = self._occurrences.execute("SELECT count FROM occurrences WHERE key=?", (key,)).fetchone()
        occurrence = row[0] if row is not None else 0
        self._occurrences.execute("INSERT OR REPLACE INTO occurrences VALUES (?, ?)", (key, occurrence + 1))
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
                "client": str(self.context.client),
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
        native_type: str | None = None,
    ) -> CanonicalEvent:
        record = raw.record or {}
        native_time_value = record.get("timestamp")
        native_time = native_time_value if isinstance(native_time_value, str) else None
        if native_type is None:
            native_type_value = record.get("type")
            native_type = native_type_value if isinstance(native_type_value, str) else "invalid_json"
        payload_digest = _canonical_digest(payload)
        event_id = _canonical_digest(
            {
                "client": str(self.context.client),
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

    def _context(self, raw: RawRecord, *, subtype: str, slot: str = "record") -> CanonicalEvent:
        """A recognized non-evidence record: context, metadata, or noise.

        The record's type is known to the normalizer so it is classified
        CONTEXT rather than UNKNOWN; genuinely new record types still land on
        UNKNOWN, which remains the drift signal for unrecognized content.
        """
        record = raw.record or {}
        attachment = record.get("attachment")
        return self._event(
            raw,
            CanonicalType.CONTEXT,
            slot=slot,
            payload={
                "native_type": record.get("type"),
                "native_subtype": subtype,
                "attachment_type": attachment.get("type") if isinstance(attachment, dict) else None,
            },
        )


_CLAUDE_CONTEXT_TYPES = frozenset(
    {
        "ai-title",
        "agent-name",
        "summary",
        "artifact-autoreact-ledger",
        "artifact-comment-monitor",
        "atis-latch",
        "bridge-session",
        "cost-state",
        "custom-title",
        "file-history-delta",
        "file-history-snapshot",
        "frame-link",
        "last-prompt",
        "mode",
        "permission-mode",
        "pr-link",
    }
)


class ClaudeNormalizer(TranscriptNormalizer):
    def __init__(self, context: NormalizationContext) -> None:
        if context.client is not Client.CLAUDE:
            raise ValueError("ClaudeNormalizer requires the claude client")
        super().__init__(context)
        self._pending_text: tuple[str, str | None] | None = None
        self._stop_hook_seen = False
        self._occurrences.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, call_id TEXT)")

    def begin_replay(self) -> None:
        super().begin_replay()
        self._pending_text = None
        self._stop_hook_seen = False
        self._occurrences.execute("DELETE FROM tasks")

    def normalize(self, raw: RawRecord) -> tuple[CanonicalEvent, ...]:
        self._begin_record(raw)
        if raw.record is None:
            return (self._unknown(raw),)
        record = raw.record
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
                    if message.get("stop_reason") == "end_turn":
                        # The API's own turn-boundary signal: this message ends
                        # the turn (no further tool_use will follow it), so the
                        # text is the final response now. Older transcripts
                        # (and any client that omits stop_reason) fall through
                        # to the stop_hook_summary/turn_duration path below,
                        # which a Stop hook may or may not ever produce.
                        events.append(
                            self._event(
                                raw,
                                CanonicalType.FINAL_RESPONSE,
                                slot="turn_boundary",
                                native_join_id=message_id if isinstance(message_id, str) else None,
                                payload={"text": block["text"], "message_id": message_id},
                            )
                        )
                        self._pending_text = None
                        self._stop_hook_seen = False
                    else:
                        self._pending_text = (
                            block["text"],
                            message_id if isinstance(message_id, str) else None,
                        )
                        self._stop_hook_seen = False
        elif record_type == "user" and isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                notification = _parse_task_notification(content)
                if notification is None and not record.get("isCompactSummary") and content.strip():
                    # A real user turn: operator or harness text input. The
                    # consumption proof keys on native_type/payload "user".
                    events.append(
                        self._event(
                            raw,
                            CanonicalType.USER_INPUT,
                            slot="user_message",
                            native_type="user",
                            payload={"native_type": "user", "text": content},
                        )
                    )
                if notification is not None and notification["status"] is not None:
                    task_id = notification["task_id"] or ""
                    task = self._occurrences.execute("SELECT call_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                    self._occurrences.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
                    call_id = task[0] if task is not None else None
                    if call_id is not None:
                        is_error = notification["status"] != "completed"
                        events.append(
                            self._event(
                                raw,
                                CanonicalType.TOOL_ERROR if is_error else CanonicalType.TOOL_RESULT,
                                slot="task_notification",
                                native_join_id=call_id,
                                payload={
                                    "call_id": call_id,
                                    "task_id": notification["task_id"],
                                    "status": notification["status"],
                                    "summary": notification["summary"],
                                    "is_error": is_error,
                                },
                            )
                        )
                # A notification with no <status> is a mid-task <event> tick,
                # never a completion; an unmapped task id is left unknown.
            else:
                user_texts = [
                    block["text"]
                    for block in _blocks(content)
                    if block.get("type") == "text" and isinstance(block.get("text"), str)
                ]
                if user_texts and not record.get("isCompactSummary"):
                    events.append(
                        self._event(
                            raw,
                            CanonicalType.USER_INPUT,
                            slot="user_message",
                            native_type="user",
                            payload={"native_type": "user", "text": "\n".join(user_texts)},
                        )
                    )
                for index, block in enumerate(_blocks(content)):
                    if block.get("type") != "tool_result":
                        continue
                    call_id = block.get("tool_use_id")
                    if not isinstance(call_id, str):
                        continue
                    tool_use_result = record.get("toolUseResult")
                    background_task_id = (
                        tool_use_result.get("backgroundTaskId") if isinstance(tool_use_result, dict) else None
                    )
                    if isinstance(background_task_id, str):
                        # A background launch's immediate result is a
                        # "running" placeholder, not the tool's outcome. Track
                        # it as in progress; only the later terminal
                        # task-notification supplies the real result.
                        self._occurrences.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (background_task_id, call_id))
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
        elif record_type == "queue-operation":
            operation = record.get("operation")
            content = record.get("content")
            if operation == "enqueue" and isinstance(content, str) and content.strip():
                # Operator text queued while the lane was busy: real input.
                events.append(
                    self._event(
                        raw,
                        CanonicalType.USER_INPUT,
                        slot="queued_input",
                        native_type="queue-operation",
                        payload={"native_type": "user", "text": content, "source": "queue-operation"},
                    )
                )
            else:
                events.append(self._context(raw, subtype=str(operation or "queue")))
        elif record_type == "attachment":
            attachment_raw = record.get("attachment")
            attachment = attachment_raw if isinstance(attachment_raw, dict) else {}
            attachment_type = attachment.get("type")
            if queued_operator_prompt(record) is not None:
                events.append(
                    self._event(
                        raw,
                        CanonicalType.USER_INPUT,
                        slot="queued_command",
                        native_type="attachment",
                        payload={
                            "native_type": "user",
                            "text": queued_operator_prompt(record),
                            "source": "queued_command",
                        },
                    )
                )
            elif attachment_type == "hook_success" and hook_additional_context(record) is not None:
                events.append(
                    self._event(
                        raw,
                        CanonicalType.USER_INPUT,
                        slot="hook_context",
                        native_type="attachment",
                        payload={
                            "native_type": "user",
                            "text": hook_additional_context(record),
                            "source": "hook_additional_context",
                        },
                    )
                )
            else:
                events.append(
                    self._context(raw, subtype=str(attachment_type or "attachment"))
                )
        elif record_type in _CLAUDE_CONTEXT_TYPES:
            events.append(self._context(raw, subtype=str(record.get("subtype") or record_type)))
        if not events:
            if record.get("isCompactSummary"):
                events.append(self._context(raw, subtype="compact_summary"))
            elif record_type in _CLAUDE_CONTEXT_TYPES or record_type in ("user", "assistant", "system", "queue-operation", "attachment"):
                # A recognized record family whose particular shape carries no
                # evidence this pass (empty user turn, unhandled system or
                # attachment variant): context, never a false UNKNOWN.
                events.append(self._context(raw, subtype=str(record.get("subtype") or record_type or "record")))
            else:
                events.append(self._unknown(raw))
        return tuple(events)


class CodexNormalizer(TranscriptNormalizer):
    def __init__(self, context: NormalizationContext) -> None:
        if context.client is not Client.CODEX:
            raise ValueError("CodexNormalizer requires the codex client")
        super().__init__(context)
        self._pending_text: tuple[str, str | None] | None = None
        self._parent_thread_id: str | None = None

    def begin_replay(self) -> None:
        super().begin_replay()
        self._pending_text = None

    def normalize(self, raw: RawRecord) -> tuple[CanonicalEvent, ...]:
        self._begin_record(raw)
        if raw.record is None:
            return (self._unknown(raw),)
        record = raw.record
        record_type = record.get("type")
        payload = record.get("payload")
        if record_type == "session_meta" and isinstance(payload, dict):
            candidate = payload.get("id")
            # A subagent rollout replays its parent's session_meta after its own.
            if isinstance(candidate, str) and candidate != self._parent_thread_id:
                source = payload.get("source")
                # Built-in subagents write a bare string, e.g. {"subagent": "review"}.
                subagent = source.get("subagent") if isinstance(source, dict) else None
                spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                if isinstance(spawn, dict) and isinstance(spawn.get("parent_thread_id"), str):
                    self._parent_thread_id = spawn["parent_thread_id"]
                if self.session_id is not None and self.session_id != candidate:
                    raise ValueError("Codex session id changed within one transcript")
                self.session_id = candidate
            return (self._context(raw, subtype="session_meta"),)
        if record_type == "response_item" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type == "function_call":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    return (
                        self._event(
                            raw,
                            CanonicalType.TOOL_CALL,
                            slot="function_call",
                            native_join_id=call_id,
                            payload={
                                "call_id": call_id,
                                "tool_name": payload.get("name"),
                                "namespace": payload.get("namespace"),
                                "input": payload.get("arguments"),
                            },
                        ),
                    )
            elif payload_type == "function_call_output":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    return (
                        self._event(
                            raw,
                            CanonicalType.TOOL_RESULT,
                            slot="function_call_output",
                            native_join_id=call_id,
                            payload={"call_id": call_id, "output": payload.get("output")},
                        ),
                    )
            elif payload_type == "message" and payload.get("role") == "user":
                # Claude's user turns carry native_type "user". The ladder's
                # consumption proof keys on that and on payload text.
                texts = [block["text"] for block in _blocks(payload.get("content")) if isinstance(block.get("text"), str)]
                return (
                    self._event(
                        raw,
                        CanonicalType.USER_INPUT,
                        slot="user_message",
                        native_type="user",
                        payload={"native_type": "user", "text": "\n".join(texts)},
                    ),
                )
            elif payload_type == "custom_tool_call":
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
                return (self._context(raw, subtype="assistant_message"),)
            return (self._context(raw, subtype=str(payload_type or "response_item")),)
        elif record_type == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "task_started":
                self._pending_text = None
                return (self._context(raw, subtype="task_started"),)
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
            return (self._context(raw, subtype=str(payload.get("type") or "event_msg")),)
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
        elif record_type in ("world_state", "turn_context", "token_usage_record"):
            return (self._context(raw, subtype=record_type),)
        return (self._unknown(raw),)


class DevinNormalizer(TranscriptNormalizer):
    """Normalize Devin ATIF transcript documents and per-step mirror rows.

    An ATIF document (``schema_version`` ``ATIF-*``) arrives as one JSON
    record carrying ``steps[]``; each step expands into canonical events.
    A per-step mirror row (``{"type": "devin_step", ...}``) carries one step
    and is emitted identically, so a file binding and a database mirror
    produce the same canonical stream for the same session.
    """

    def __init__(self, context: NormalizationContext) -> None:
        if context.client is not Client.DEVIN:
            raise ValueError("DevinNormalizer requires the devin client")
        super().__init__(context)

    def normalize(self, raw: RawRecord) -> tuple[CanonicalEvent, ...]:
        self._begin_record(raw)
        if raw.record is None:
            return (self._unknown(raw),)
        record = raw.record
        if isinstance(record.get("steps"), list):
            return self._document_events(raw, record)
        if record.get("type") == "devin_step" and isinstance(record.get("step"), dict):
            self._identify(record.get("session_id"), record.get("schema_version"))
            return self._step_events(raw, record["step"])
        return (self._unknown(raw),)

    def _identify(self, session_id: object, schema_version: object) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Devin transcript has no session id")
        if self.session_id is None:
            self.session_id = session_id
        elif self.session_id != session_id:
            raise ValueError("Devin session id changed within one transcript")
        if isinstance(schema_version, str) and schema_version != self.context.client_version:
            raise ValueError(f"Devin ATIF schema changed to {schema_version!r}")

    def _document_events(self, raw: RawRecord, record: dict[str, Any]) -> tuple[CanonicalEvent, ...]:
        self._identify(record.get("session_id"), record.get("schema_version"))
        events: list[CanonicalEvent] = [
            self._context(raw, subtype="session_document", slot="header")
        ]
        for step in record["steps"]:
            if isinstance(step, dict):
                events.extend(self._step_events(raw, step))
        return tuple(events)

    def _step_events(self, raw: RawRecord, step: dict[str, Any]) -> tuple[CanonicalEvent, ...]:
        step_id = step.get("step_id")
        base = f"step:{step_id}" if step_id is not None else "step"
        source = str(step.get("source") or "unknown")
        events: list[CanonicalEvent] = []
        if source == "user":
            text = step.get("message")
            events.append(
                self._event(
                    raw,
                    CanonicalType.USER_INPUT,
                    slot=f"{base}:input",
                    native_type="user",
                    native_join_id=str(step_id) if step_id is not None else None,
                    payload={"native_type": "user", "text": text if isinstance(text, str) else ""},
                )
            )
        elif source in ("agent", "assistant", "devin"):
            reasoning = step.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                events.append(self._context(raw, subtype="reasoning", slot=f"{base}:reasoning"))
            calls = _blocks(step.get("tool_calls"))
            for index, call in enumerate(calls):
                call_id = call.get("tool_call_id") or call.get("id")
                if not isinstance(call_id, str):
                    continue
                events.append(
                    self._event(
                        raw,
                        CanonicalType.TOOL_CALL,
                        slot=f"{base}:tool_call:{index}",
                        native_join_id=call_id,
                        payload={
                            "call_id": call_id,
                            "tool_name": call.get("function_name") or call.get("name"),
                            "input": call.get("arguments"),
                        },
                    )
                )
            observation = step.get("observation")
            results = _blocks(observation.get("results")) if isinstance(observation, dict) else []
            for index, result in enumerate(results):
                call_id = result.get("source_call_id")
                if not isinstance(call_id, str):
                    continue
                events.append(
                    self._event(
                        raw,
                        CanonicalType.TOOL_RESULT,
                        slot=f"{base}:tool_result:{index}",
                        native_join_id=call_id,
                        payload={"call_id": call_id, "output": result.get("content")},
                    )
                )
            text = step.get("message")
            if isinstance(text, str) and text.strip():
                if calls:
                    events.append(
                        self._context(raw, subtype="assistant_message", slot=f"{base}:message")
                    )
                else:
                    events.append(
                        self._event(
                            raw,
                            CanonicalType.FINAL_RESPONSE,
                            slot=f"{base}:response",
                            native_join_id=str(step_id) if step_id is not None else None,
                            payload={"text": text, "step_id": step_id},
                        )
                    )
        if not events:
            events.append(self._context(raw, subtype=source, slot=f"{base}:context"))
        return tuple(events)


class AmpNormalizer(TranscriptNormalizer):
    """Normalize Amp thread-export JSONL (``amp_thread``/``amp_message`` rows)."""

    def __init__(self, context: NormalizationContext) -> None:
        if context.client is not Client.AMP:
            raise ValueError("AmpNormalizer requires the amp client")
        super().__init__(context)

    def normalize(self, raw: RawRecord) -> tuple[CanonicalEvent, ...]:
        self._begin_record(raw)
        if raw.record is None:
            return (self._unknown(raw),)
        record = raw.record
        if record.get("version") != self.context.client_version:
            raise ValueError(f"Amp record version changed to {record.get('version')!r}")
        thread_id = record.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.startswith("T-"):
            raise ValueError("Amp record has no thread ID")
        if self.session_id is not None and self.session_id != thread_id:
            raise ValueError("Amp thread ID changed within one transcript")
        self.session_id = thread_id
        if record.get("type") == "amp_thread":
            return (self._context(raw, subtype="thread_header"),)
        message = record.get("message")
        if record.get("type") != "amp_message" or not isinstance(message, dict):
            return (self._unknown(raw),)
        events: list[CanonicalEvent] = []
        role = message.get("role")
        for index, block in enumerate(_blocks(message.get("content"))):
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                events.append(
                    self._event(
                        raw, CanonicalType.TOOL_CALL, slot=f"tool_use:{index}",
                        native_join_id=block["id"],
                        payload={"call_id": block["id"], "tool_name": block.get("name"), "input": block.get("input")},
                    )
                )
            elif block.get("type") == "tool_result" and isinstance(block.get("toolUseID"), str):
                run_raw = block.get("run")
                run = run_raw if isinstance(run_raw, dict) else {}
                failed = run.get("status") == "error"
                events.append(
                    self._event(
                        raw, CanonicalType.TOOL_ERROR if failed else CanonicalType.TOOL_RESULT,
                        slot=f"tool_result:{index}", native_join_id=block["toolUseID"],
                        payload={"call_id": block["toolUseID"], "run": run, "is_error": failed},
                    )
                )
        texts = "\n".join(
            block["text"]
            for block in _blocks(message.get("content"))
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        )
        if role == "user" and texts:
            events.append(
                self._event(
                    raw, CanonicalType.USER_INPUT, slot="user_message",
                    native_type="user",
                    payload={"native_type": "user", "text": texts},
                )
            )
        if role == "assistant" and (message.get("state") or {}).get("stopReason") == "end_turn" and texts:
            events.append(
                self._event(
                    raw, CanonicalType.FINAL_RESPONSE, slot="end_turn",
                    native_join_id=str(message.get("protocolMessageID") or message.get("messageId")),
                    payload={"text": texts, "message_id": message.get("protocolMessageID")},
                )
            )
        if role == "info" and any(block.get("type") == "summary" for block in _blocks(message.get("content"))):
            events.append(
                self._event(
                    raw, CanonicalType.COMPACTION, slot="summary",
                    payload={"message_id": message.get("protocolMessageID")},
                )
            )
        return tuple(events) or (self._context(raw, subtype=str(role or "amp_message")),)


def make_normalizer(context: NormalizationContext) -> TranscriptNormalizer:
    # Fail closed on a client with no JSONL normalizer: silently defaulting an
    # unknown harness to Codex normalization would fabricate evidence. Hermes
    # evidence has no JSONL form -- it arrives through the hermes-db lane
    # plug, which emits canonical events directly.
    if context.client is Client.CLAUDE:
        return ClaudeNormalizer(context)
    if context.client is Client.CODEX:
        return CodexNormalizer(context)
    if context.client is Client.DEVIN:
        return DevinNormalizer(context)
    if context.client is Client.AMP:
        return AmpNormalizer(context)
    raise ValueError(f"no JSONL transcript normalizer for client {context.client!r}")


def native_session_identity(transcript_path: Path | str) -> str | None:
    """Derive one transcript's adapter-native session identity.

    The transcript is replayed through the same normalizers the durable
    journal uses, so the returned value is exactly the ``session_id``
    canonical events for that transcript carry. Any transcript that does not
    identify a Claude/Codex session -- unreadable, foreign schema, no client
    version, or inconsistent native identity -- yields ``None`` so callers
    can fail closed instead of binding a guess.
    """
    path = Path(transcript_path)
    try:
        with JsonlTailReader(path) as reader:
            records = reader.poll().records
    except OSError:
        return None
    client: Client | None = None
    client_version: str | None = None
    for raw in records:
        record = raw.record or {}
        if client is None:
            if isinstance(record.get("sessionId"), str):
                client = Client.CLAUDE
            elif record.get("type") == "session_meta":
                client = Client.CODEX
            elif record.get("type") == "amp_thread":
                client = Client.AMP
        payload = record.get("payload")
        candidate = record.get("version") if client in (Client.CLAUDE, Client.AMP) else (
            payload.get("cli_version") if isinstance(payload, dict) else None
        )
        if isinstance(candidate, str):
            client_version = candidate
        if client is not None and client_version is not None:
            break
    if client is None or client_version is None:
        return None
    context = NormalizationContext(
        instance="native-session-identity",
        lane="native-session-identity",
        client=client,
        client_version=client_version,
    )
    normalizer = make_normalizer(context)
    try:
        for raw in records:
            normalizer.normalize(raw)
    except ValueError:
        return None
    finally:
        normalizer.close()
    return normalizer.session_id

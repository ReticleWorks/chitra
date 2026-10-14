"""Read-only evidence plugs for database-backed agent transcripts.

Devin keeps its session history in ``~/.local/share/devin/cli/sessions.db``
(``message_nodes`` rows carrying one JSON chat message each) and Hermes keeps
its history in ``~/.hermes/state.db`` (``messages`` rows). Neither source is a
JSONL transcript, so these lanes bind URI locators — ``devin-db:<session>``
and ``hermes-db:<session>`` for root or ``hermes-db:<profile>/<session>`` for
a Hermes profile — and read canonical events through this plug instead of the
journal ingestor. ``CHITRA_HERMES_DB`` continues to select the root database;
profile databases are resolved beside it under ``profiles/<profile>/``.

Both plugs are evidence-only: they can ``events()``, ``status()``, and
``handle_for_binding()``. Delivery to such lanes belongs to the harness's own
plug (lane-run for Devin); these plugs never put text on a wire. Every read
is incremental: the cursor is the last row primary key served, so a monitor
pass sees only new rows, and journal append dedupe keeps a stale cursor safe.
Reads open the database ``mode=ro``; a missing or foreign database raises
StreamUnavailable so the lane renders UNKNOWN, never fabricated evidence.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, ClassVar

from chitra.adapter.contract import (
    Capabilities,
    EventBatch,
    LaneHandle,
    LaneStart,
    LaneStatus,
    LaunchRefused,
    ReadProof,
    SendReceipt,
    StopResult,
    StreamUnavailable,
    Unsupported,
    WireOrder,
    utc_now_iso,
)
from chitra.journal.models import CanonicalEvent, CanonicalType, TranscriptIdentity
from chitra.journal.normalizers import NORMALIZER_VERSION, _canonical_digest
from chitra.journal.reader import DEFAULT_MAX_BATCH_BYTES
from chitra.transcript_bindings import BoundTranscript

_DEVIN_URI_PREFIX = "devin-db:"
_HERMES_URI_PREFIX = "hermes-db:"
_HERMES_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

# One canonical event template a subclass's ``_row_events`` yields per slot:
# (slot, normalized type, native type, join id, payload, native time, raw record).
RowEvent = tuple[str, CanonicalType, str, str | None, dict[str, Any], str | None, dict[str, Any]]


def _devin_db_path() -> Path:
    override = os.environ.get("CHITRA_DEVIN_DB")
    if override:
        return Path(override)
    return Path.home() / ".local/share/devin/cli/sessions.db"


def _hermes_db_path(profile: str | None = None) -> Path:
    override = os.environ.get("CHITRA_HERMES_DB")
    if profile is None:
        return Path(override) if override else Path.home() / ".hermes/state.db"
    if not isinstance(profile, str) or not _HERMES_PROFILE.fullmatch(profile):
        raise StreamUnavailable("UNKNOWN: Hermes profile locator is invalid")
    root = Path(override).expanduser().parent if override else Path.home() / ".hermes"
    return root / "profiles" / profile / "state.db"


def _epoch_iso(value: object) -> str | None:
    if not isinstance(value, int | float):
        return None
    try:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


class _DbEvidencePlug:
    """Shared read-only plug for session/message SQLite evidence stores.

    Subclasses declare the URI prefix, the table names, and ``_row_events``
    which maps one message row (already JSON-decoded where the store keeps
    JSON) to canonical events. The cursor contract is uniform: ``None``
    replays every row for the bound session; otherwise rows with a primary
    key greater than the cursor are served in primary-key order.
    """

    name: ClassVar[str]
    client: ClassVar[str]
    capabilities: ClassVar[Capabilities] = Capabilities(
        delivery="steer", read_level="accepted", graceful_stop=False
    )
    uri_prefix: ClassVar[str]
    client_version: ClassVar[str]

    # -- subclass contract -------------------------------------------------
    def _db_path(self, handle: LaneHandle) -> Path:
        raise NotImplementedError

    def _session_locator(self, value: str) -> tuple[str, dict[str, str]]:
        return value, {}

    def _select_rows(self, conn: sqlite3.Connection, session_id: str, after: int | None) -> sqlite3.Cursor:
        raise NotImplementedError

    def _row_events(self, row: tuple[Any, ...]) -> tuple[RowEvent, ...]:
        raise NotImplementedError

    def _session_exists(self, conn: sqlite3.Connection, session_id: str) -> bool:
        raise NotImplementedError

    # -- plug machinery -----------------------------------------------------
    def handle_for_binding(self, bound: BoundTranscript, *, session_ref: str) -> LaneHandle:
        if not bound.is_uri or not str(bound.path).startswith(self.uri_prefix):
            raise StreamUnavailable(f"{self.name} needs a {self.uri_prefix} locator")
        session_id, extra = self._session_locator(
            str(bound.path)[len(self.uri_prefix):])
        if not session_id:
            raise StreamUnavailable(f"{self.uri_prefix} binding carries no session id")
        return LaneHandle(
            plug=self.name,
            lane_id=str(bound.binding.lane),
            session_ref=session_ref,
            started_at=utc_now_iso(),
            locator={
                "instance": str(bound.binding.instance),
                "client": self.client,
                "client_version": self.client_version,
                "session_id": session_id,
                **extra,
            },
            binding_ref=str(bound.path),
            native_session_id=session_id,
        )

    def _connect(self, handle: LaneHandle) -> sqlite3.Connection:
        db_path = self._db_path(handle)
        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=5.0
            )
        except sqlite3.Error as exc:
            raise StreamUnavailable(
                f"UNKNOWN: {self.client} evidence database unavailable: {type(exc).__name__}"
            ) from exc
        return conn

    def events(self, handle: LaneHandle, cursor: str | None = None) -> EventBatch:
        session_id = handle.locator.get("session_id")
        if not session_id:
            raise StreamUnavailable(f"{self.name} handle carries no session id")
        try:
            after = int(cursor) if cursor is not None else None
        except ValueError as exc:
            raise StreamUnavailable(f"{self.name} cursor is not a row id") from exc
        conn = self._connect(handle)
        rows: sqlite3.Cursor | None = None
        events: list[CanonicalEvent] = []
        last_row_id = after
        try:
            if not self._session_exists(conn, session_id):
                raise StreamUnavailable(
                    f"UNKNOWN: {self.client} session {session_id!r} absent from evidence database"
                )
            rows = self._select_rows(conn, session_id, after)
            batch_bytes = 0
            while row := rows.fetchone():
                row_id = int(row[0])
                # Move the cursor only after this row has been fully converted.
                # A row that emits no events still advances and cannot stall.
                events.extend(self._emit(row, row_id=row_id, handle=handle))
                last_row_id = row_id
                batch_bytes += sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row)
                # Match the JSONL reader's per-poll budget. A single oversized
                # row is processed whole so the cursor always makes progress.
                if batch_bytes >= DEFAULT_MAX_BATCH_BYTES:
                    break
        except sqlite3.Error as exc:
            raise StreamUnavailable(
                f"UNKNOWN: {self.client} evidence query failed: {type(exc).__name__}"
            ) from exc
        finally:
            try:
                if rows is not None:
                    rows.close()
            finally:
                conn.close()
        return EventBatch(
            events=tuple(events),
            cursor=str(last_row_id) if last_row_id is not None else cursor,
        )

    def _emit(self, row: tuple[Any, ...], *, row_id: int, handle: LaneHandle) -> tuple[CanonicalEvent, ...]:
        session_id = str(handle.locator["session_id"])
        profile = handle.locator.get("profile")
        identity = TranscriptIdentity(path=str(handle.binding_ref or session_id), device=0, inode=0, generation=0)
        out: list[CanonicalEvent] = []
        for slot, normalized, native_type, join_id, payload, native_time, raw_record in self._row_events(row):
            key = (f"{self.client}:{profile}:{session_id}:{row_id}:{slot}"
                   if profile is not None else f"{self.client}:{session_id}:{row_id}:{slot}")
            payload_digest = _canonical_digest(payload)
            event_identity = {
                "client": self.client,
                "session_id": session_id,
                "native_key": key,
                "normalized_type": normalized.value,
                "payload_digest": payload_digest,
            }
            if profile is not None:
                event_identity["profile"] = profile
            out.append(
                CanonicalEvent(
                    event_id=_canonical_digest(event_identity),
                    instance=handle.locator["instance"],
                    lane=handle.lane_id,
                    client=self.client,
                    client_version=handle.locator["client_version"],
                    process_id=None,
                    transcript=identity,
                    session_id=session_id,
                    resume_id=None,
                    observed_at=utc_now_iso(),
                    native_time=native_time,
                    native_type=native_type,
                    native_join_id=join_id,
                    raw_byte_range=None,
                    raw_sha256=None,
                    normalized_type=normalized,
                    goal_ref=handle.session_ref,
                    item_ref=None,
                    payload_digest=payload_digest,
                    normalizer_version=NORMALIZER_VERSION,
                    payload=payload,
                    raw_record=raw_record,
                )
            )
        return tuple(out)

    # -- operations an evidence plug does not perform -----------------------
    def start(self, spec: LaneStart) -> LaneHandle:
        raise LaunchRefused(f"{self.name} reads existing evidence; it does not launch lanes")

    def send(self, handle: LaneHandle, order: WireOrder) -> SendReceipt:
        raise Unsupported(f"{self.name} is an evidence source; delivery uses the lane's own plug")

    def prove_read(
        self,
        handle: LaneHandle,
        order: WireOrder,
        receipt: SendReceipt | None = None,
        *,
        deadline_s: float = 60.0,
    ) -> ReadProof:
        raise Unsupported(f"{self.name} is an evidence source; read proof uses the delivery plug")

    def status(self, handle: LaneHandle) -> LaneStatus:
        return LaneStatus(state="unknown", detail="db evidence plug does not track lane liveness")

    def stop(self, handle: LaneHandle) -> StopResult:
        raise Unsupported(f"{self.name} is an evidence source; stop uses the lane's own plug")


class DevinDbPlug(_DbEvidencePlug):
    """Canonical events from Devin's ``sessions.db`` ``message_nodes`` rows."""

    name: ClassVar[str] = "devin-db"
    client: ClassVar[str] = "devin"
    uri_prefix: ClassVar[str] = _DEVIN_URI_PREFIX
    client_version: ClassVar[str] = "devin-sessions-db-v1"

    def _db_path(self, handle: LaneHandle) -> Path:
        return _devin_db_path()

    def _session_exists(self, conn: sqlite3.Connection, session_id: str) -> bool:
        return (
            conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is not None
        )

    def _select_rows(self, conn: sqlite3.Connection, session_id: str, after: int | None) -> sqlite3.Cursor:
        if after is None:
            return conn.execute(
                "SELECT row_id, node_id, created_at, chat_message FROM message_nodes"
                " WHERE session_id=? ORDER BY row_id",
                (session_id,),
            )
        return conn.execute(
            "SELECT row_id, node_id, created_at, chat_message FROM message_nodes"
            " WHERE session_id=? AND row_id>? ORDER BY row_id",
            (session_id, after),
        )

    def _row_events(self, row: tuple[Any, ...]) -> tuple[RowEvent, ...]:
        row_id, node_id, created_at, chat_json = row
        try:
            message = json.loads(chat_json)
        except (TypeError, json.JSONDecodeError):
            return (
                ("record", CanonicalType.UNKNOWN, "devin_message", None,
                 {"native_type": "devin_message", "decode_error": "chat_message not JSON"},
                 _epoch_iso(created_at), {"row_id": row_id, "node_id": node_id}),
            )
        if not isinstance(message, dict):
            return (
                ("record", CanonicalType.UNKNOWN, "devin_message", None,
                 {"native_type": "devin_message", "decode_error": "chat_message not an object"},
                 _epoch_iso(created_at), message),
            )
        native_time = _epoch_iso(created_at)
        message_id = message.get("message_id")
        role = message.get("role")
        metadata_raw = message.get("metadata")
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
        events: list[RowEvent] = []
        if role == "user":
            if metadata.get("is_user_input"):
                events.append(
                    (
                        "input", CanonicalType.USER_INPUT, "user",
                        str(message_id) if isinstance(message_id, str) else None,
                        {"native_type": "user", "text": message.get("content") if isinstance(message.get("content"), str) else ""},
                        native_time, message,
                    )
                )
            else:
                events.append(("context", CanonicalType.CONTEXT, "user_context", None,
                               {"native_type": "user", "native_subtype": "user_context"}, native_time, message))
        elif role == "assistant":
            thinking = message.get("thinking") or message.get("reasoning_content")
            if isinstance(thinking, str) and thinking.strip():
                events.append(("reasoning", CanonicalType.CONTEXT, "reasoning", None,
                               {"native_type": "assistant", "native_subtype": "reasoning"}, native_time, message))
            calls = [c for c in (message.get("tool_calls") or []) if isinstance(c, dict)]
            for index, call in enumerate(calls):
                call_id = call.get("id") or call.get("call_id")
                if not isinstance(call_id, str):
                    continue
                events.append(
                    (
                        f"tool_call:{index}", CanonicalType.TOOL_CALL, "tool_call", call_id,
                        {"call_id": call_id, "tool_name": call.get("name"), "input": call.get("arguments")},
                        native_time, message,
                    )
                )
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                if calls:
                    events.append(("message", CanonicalType.CONTEXT, "assistant_message", None,
                                   {"native_type": "assistant", "native_subtype": "assistant_message"}, native_time, message))
                else:
                    events.append(
                        (
                            "response", CanonicalType.FINAL_RESPONSE, "assistant",
                            str(message_id) if isinstance(message_id, str) else None,
                            {"text": content, "message_id": message_id},
                            native_time, message,
                        )
                    )
        elif role == "tool":
            call_id = message.get("tool_call_id")
            events.append(
                (
                    "tool_result", CanonicalType.TOOL_RESULT, "tool",
                    str(call_id) if isinstance(call_id, str) else None,
                    {"call_id": call_id, "output": message.get("content"), "tool_name": message.get("name")},
                    native_time, message,
                )
            )
        if not events:
            events.append(("context", CanonicalType.CONTEXT, str(role or "message"), None,
                           {"native_type": str(role or "message"), "native_subtype": "context"}, native_time, message))
        return tuple(events)


class HermesDbPlug(_DbEvidencePlug):
    """Canonical events from Hermes's ``state.db`` ``messages`` rows."""

    name: ClassVar[str] = "hermes-db"
    client: ClassVar[str] = "hermes"
    uri_prefix: ClassVar[str] = _HERMES_URI_PREFIX
    client_version: ClassVar[str] = "hermes-state-db-v1"

    def _db_path(self, handle: LaneHandle) -> Path:
        return _hermes_db_path(handle.locator.get("profile"))

    def _session_locator(self, value: str) -> tuple[str, dict[str, str]]:
        if "/" not in value:
            return value, {}
        profile, separator, session_id = value.partition("/")
        if (not separator or not _HERMES_PROFILE.fullmatch(profile)
                or not session_id or "/" in session_id):
            raise StreamUnavailable("hermes-db profile locator is invalid")
        return session_id, {"profile": profile}

    def _session_exists(self, conn: sqlite3.Connection, session_id: str) -> bool:
        return (
            conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is not None
        )

    def _select_rows(self, conn: sqlite3.Connection, session_id: str, after: int | None) -> sqlite3.Cursor:
        columns = "id, role, content, tool_call_id, tool_calls, tool_name, timestamp, finish_reason, reasoning_content"
        if after is None:
            return conn.execute(
                f"SELECT {columns} FROM messages WHERE session_id=? ORDER BY id",
                (session_id,),
            )
        return conn.execute(
            f"SELECT {columns} FROM messages WHERE session_id=? AND id>? ORDER BY id",
            (session_id, after),
        )

    def _row_events(self, row: tuple[Any, ...]) -> tuple[RowEvent, ...]:
        (row_id, role, content, tool_call_id, tool_calls_json, tool_name,
         timestamp, finish_reason, reasoning_content) = row
        native_time = _epoch_iso(timestamp)
        raw_record = {
            "id": row_id, "role": role, "content": content, "tool_call_id": tool_call_id,
            "tool_name": tool_name, "finish_reason": finish_reason,
        }
        events: list[RowEvent] = []
        if role == "user":
            events.append(
                (
                    "input", CanonicalType.USER_INPUT, "user", str(row_id),
                    {"native_type": "user", "text": content if isinstance(content, str) else ""},
                    native_time, raw_record,
                )
            )
        elif role == "assistant":
            if isinstance(reasoning_content, str) and reasoning_content.strip():
                events.append(("reasoning", CanonicalType.CONTEXT, "reasoning", None,
                               {"native_type": "assistant", "native_subtype": "reasoning"}, native_time, raw_record))
            calls: list[dict[str, Any]] = []
            if isinstance(tool_calls_json, str) and tool_calls_json.strip():
                try:
                    parsed = json.loads(tool_calls_json)
                    calls = [c for c in parsed if isinstance(c, dict)] if isinstance(parsed, list) else []
                except json.JSONDecodeError:
                    calls = []
            for index, call in enumerate(calls):
                function_raw = call.get("function")
                function = function_raw if isinstance(function_raw, dict) else {}
                call_id = call.get("call_id") or call.get("id")
                name = function.get("name") or call.get("name")
                arguments = function.get("arguments") or call.get("arguments")
                if not isinstance(call_id, str):
                    call_id = str(row_id)
                events.append(
                    (
                        f"tool_call:{index}", CanonicalType.TOOL_CALL, "tool_call", call_id,
                        {"call_id": call_id, "tool_name": name, "input": arguments},
                        native_time, raw_record,
                    )
                )
            if isinstance(content, str) and content.strip():
                if calls:
                    events.append(("message", CanonicalType.CONTEXT, "assistant_message", None,
                                   {"native_type": "assistant", "native_subtype": "assistant_message"}, native_time, raw_record))
                else:
                    events.append(
                        (
                            "response", CanonicalType.FINAL_RESPONSE, "assistant", str(row_id),
                            {"text": content, "finish_reason": finish_reason},
                            native_time, raw_record,
                        )
                    )
        elif role == "tool":
            join = tool_call_id if isinstance(tool_call_id, str) and tool_call_id else str(row_id)
            events.append(
                (
                    "tool_result", CanonicalType.TOOL_RESULT, "tool", join,
                    {"call_id": tool_call_id, "output": content, "tool_name": tool_name},
                    native_time, raw_record,
                )
            )
        if not events:
            events.append(("context", CanonicalType.CONTEXT, str(role or "message"), None,
                           {"native_type": str(role or "message"), "native_subtype": "context"}, native_time, raw_record))
        return tuple(events)


__all__ = ["DevinDbPlug", "HermesDbPlug"]

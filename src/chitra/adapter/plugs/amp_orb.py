"""The Amp orb lane plug.

An Amp orb lane is a governed ``chitra-orb-lane`` record: the wrapper owns the
exact ``amp`` invocation shape, orbctl campaign governance, busy-executor stop
protection, and the per-lane JSON record under the orb-lanes state root
(``${CHITRA_STATE_DIR:-/var/lib/polyphony-chitra}/orb-lanes``). This plug never
drives ``amp`` lifecycle commands directly; the only bare ``amp`` call it makes
is the read-only ``amp threads export`` that feeds canonical events and read
proof.

Proof rule: ``chitra-orb-lane steer`` exiting 0 means the order text was found
in the thread markdown — the wire acknowledged it. That alone is ``accepted``.
``consumed`` additionally requires the structured export to show the order
text as a user message followed by later agent/tool activity. A ``swallowed``
steer (exit 3) means the order never landed — FAILED, not unconfirmed.

Campaign uncertainty is never guessed away: ``status`` surfaces the record's
``launching``/``launch_failed`` states, and ``stop`` on a launch-uncertain
campaign raises so the operator runs ``chitra-orb-lane adopt`` first.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from chitra.adapter.contract import (
    AdapterError,
    Capabilities,
    EventBatch,
    HarnessUnavailable,
    LaneDead,
    LaneHandle,
    LaneStart,
    LaneStatus,
    LaunchFailed,
    LaunchRefused,
    ReadProof,
    SendReceipt,
    SendRejected,
    StopResult,
    StreamUnavailable,
    WireOrder,
    utc_now_iso,
)
from chitra.adapter.proof import prove_read_from_events
from chitra.journal.models import CanonicalEvent, CanonicalType, TranscriptIdentity
from chitra.journal.normalizers import NORMALIZER_VERSION, _canonical_digest
from chitra.orders import DispatchStatus
from chitra.transcript_bindings import BoundTranscript

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

ORB_URI_PREFIX = "amp-orb:"
"""Transcript-binding URI scheme for orb lanes: ``amp-orb:<lane-slug>``."""

_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")
_AMP_CLIENT = "amp"
_DEAD_RECORD_STATES = frozenset({"launch_failed", "stop_requested"})


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def orb_lane_state_root() -> Path:
    """The governed orb-lane record root the ``chitra-orb-lane`` wrapper uses."""
    return Path(os.environ.get("CHITRA_STATE_DIR", "/var/lib/polyphony-chitra")) / "orb-lanes"


def _native_time(message: dict[str, Any]) -> str | None:
    created = message.get("createdAt")
    if isinstance(created, str) and created:
        return created
    meta = message.get("meta")
    sent_at = meta.get("sentAt") if isinstance(meta, dict) else None
    if isinstance(sent_at, int | float):
        return datetime.fromtimestamp(sent_at / 1000, tz=UTC).isoformat()
    return None


def amp_events_from_export(
    export: dict[str, Any],
    *,
    instance: str,
    lane: str,
    client_version: str,
    goal_ref: str | None,
    binding_ref: str,
    item_ref: str | None = None,
    observed_at: str | None = None,
) -> tuple[CanonicalEvent, ...]:
    """Convert one ``amp threads export`` document into canonical events.

    The export is structured (messages[] with typed content blocks), so this
    never parses the fragile markdown rendering. Per the canonical contract:
    tool calls carry ``payload["input"]``; an arriving order is a
    ``native_type="user"`` event whose ``payload["text"]`` holds the exact
    order text; the text is never rewritten.
    """
    messages = export.get("messages")
    if not isinstance(messages, list):
        raise StreamUnavailable("amp export carries no messages list")
    thread_id = export.get("id")
    session_id = thread_id if isinstance(thread_id, str) and thread_id else "unknown"
    transcript = TranscriptIdentity(path=binding_ref, device=0, inode=0, generation=0)
    observed = observed_at or utc_now_iso()

    def emit(
        message: dict[str, Any],
        key: str,
        normalized: CanonicalType,
        native_type: str,
        payload: dict[str, Any],
        native_join_id: str | None,
    ) -> CanonicalEvent:
        payload_digest = _canonical_digest(payload)
        return CanonicalEvent(
            event_id=_canonical_digest(
                {
                    "client": _AMP_CLIENT,
                    "session_id": session_id,
                    "native_key": key,
                    "normalized_type": normalized.value,
                    "payload_digest": payload_digest,
                }
            ),
            instance=instance,
            lane=lane,
            client=_AMP_CLIENT,
            client_version=client_version,
            process_id=None,
            transcript=transcript,
            session_id=session_id,
            resume_id=None,
            observed_at=observed,
            native_time=_native_time(message),
            native_type=native_type,
            native_join_id=native_join_id,
            raw_byte_range=None,
            raw_sha256=None,
            normalized_type=normalized,
            goal_ref=goal_ref,
            item_ref=item_ref,
            payload_digest=payload_digest,
            normalizer_version=NORMALIZER_VERSION,
            payload=payload,
            raw_record=message,
        )

    events: list[CanonicalEvent] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        message_id = message.get("messageId")
        message_key = message_id if isinstance(message_id, str) and message_id else f"msg:{message_index}"
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            key = f"{message_key}:{block_index}"
            block_type = block.get("type")
            if block_type == "text" and role == "user":
                events.append(
                    emit(
                        message,
                        key,
                        CanonicalType.UNKNOWN,
                        "user",
                        {"native_type": "user", "text": block.get("text") if isinstance(block.get("text"), str) else ""},
                        message_id if isinstance(message_id, str) else None,
                    )
                )
            elif block_type == "tool_use":
                call_id = block.get("id")
                events.append(
                    emit(
                        message,
                        key,
                        CanonicalType.TOOL_CALL,
                        "tool_use",
                        {"call_id": call_id, "tool_name": block.get("name"), "input": block.get("input")},
                        call_id if isinstance(call_id, str) else None,
                    )
                )
            elif block_type == "tool_result":
                join = block.get("toolUseID")
                events.append(
                    emit(
                        message,
                        key,
                        CanonicalType.TOOL_RESULT,
                        "tool_result",
                        {"call_id": join, "run": block.get("run")},
                        join if isinstance(join, str) else None,
                    )
                )
            elif block_type == "text" and role == "assistant":
                events.append(
                    emit(
                        message,
                        key,
                        CanonicalType.FINAL_RESPONSE,
                        "assistant",
                        {"text": block.get("text"), "message_id": message_id},
                        message_id if isinstance(message_id, str) else None,
                    )
                )
            else:
                events.append(
                    emit(
                        message,
                        key,
                        CanonicalType.UNKNOWN,
                        str(block_type or role or "unknown"),
                        {"native_type": role, "block_type": block_type},
                        message_id if isinstance(message_id, str) else None,
                    )
                )
    return tuple(events)


class AmpOrbPlug:
    """One governed Amp orb lane behind the shared interface."""

    name: ClassVar[str] = "amp-orb"
    client: ClassVar[str] = _AMP_CLIENT
    capabilities: ClassVar[Capabilities] = Capabilities(
        delivery="steer",
        read_level="consumed",
        graceful_stop=True,
    )

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        orb_lane_bin: str | None = None,
        amp_bin: str = "amp",
        state_root: Path | None = None,
    ) -> None:
        self._runner = runner or _run
        self._orb_lane_bin = orb_lane_bin or os.environ.get("CHITRA_ORB_LANE_BIN", "chitra-orb-lane")
        self._amp_bin = amp_bin
        self._state_root_override = state_root

    # -- internals -----------------------------------------------------------

    def _state_root(self) -> Path:
        return self._state_root_override if self._state_root_override is not None else orb_lane_state_root()

    def _record_path(self, slug: str) -> Path:
        return self._state_root() / f"{slug}.json"

    def _run_orb(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner([self._orb_lane_bin, *args])
        except FileNotFoundError as exc:
            raise HarnessUnavailable(f"{self._orb_lane_bin} not found") from exc
        except OSError as exc:
            raise HarnessUnavailable(f"{self._orb_lane_bin} could not run: {exc}") from exc

    def _run_amp(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner([self._amp_bin, *args])
        except FileNotFoundError as exc:
            raise HarnessUnavailable(f"{self._amp_bin} not found") from exc
        except OSError as exc:
            raise HarnessUnavailable(f"{self._amp_bin} could not run: {exc}") from exc

    @staticmethod
    def _detail(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stderr or "").strip() or (result.stdout or "").strip() or f"exit {result.returncode}"

    def _slug_of(self, handle: LaneHandle) -> str:
        slug = handle.locator.get("slug") or _slug_from_ref(handle.binding_ref or "")
        if _SLUG_RE.fullmatch(slug) is None:
            raise AdapterError(f"orb lane handle carries no valid slug: {slug!r}")
        return slug

    def _record(self, slug: str) -> dict[str, Any]:
        path = self._record_path(slug)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LaneDead(f"orb lane {slug!r} has no record at {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise AdapterError(f"orb lane record {path} is unreadable: {exc}") from exc
        if not isinstance(record, dict):
            raise AdapterError(f"orb lane record {path} is not an object")
        return record

    def _live_record(self, slug: str) -> dict[str, Any]:
        record = self._record(slug)
        state = record.get("state")
        if isinstance(state, str) and state in _DEAD_RECORD_STATES:
            raise LaneDead(f"orb lane {slug!r} is {state}")
        return record

    def _thread_id(self, handle: LaneHandle) -> str:
        if handle.native_session_id:
            return handle.native_session_id
        record = self._live_record(self._slug_of(handle))
        thread = record.get("thread_id")
        if not isinstance(thread, str) or not thread:
            raise LaneDead("orb lane has no thread id")
        return thread

    def _export(self, thread: str) -> dict[str, Any]:
        result = self._run_amp("threads", "export", thread)
        if result.returncode != 0:
            raise StreamUnavailable(f"amp threads export {thread} failed: {self._detail(result)}")
        try:
            export = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise StreamUnavailable(f"amp threads export {thread} returned non-JSON: {exc}") from exc
        if not isinstance(export, dict):
            raise StreamUnavailable(f"amp threads export {thread} returned a non-object document")
        return export

    def _context(self, handle: LaneHandle, export: dict[str, Any]) -> dict[str, Any]:
        locator = handle.locator
        return {
            "instance": locator.get("instance") or handle.lane_id,
            "lane": handle.lane_id,
            "client_version": locator.get("client_version") or _export_client_version(export),
            "goal_ref": handle.session_ref,
            "binding_ref": handle.binding_ref or f"amp-thread:{handle.native_session_id or 'unknown'}",
        }

    def _events(self, handle: LaneHandle) -> tuple[CanonicalEvent, ...]:
        export = self._export(self._thread_id(handle))
        return amp_events_from_export(export, **self._context(handle, export))

    # -- bindings ------------------------------------------------------------

    def handle_for_binding(self, bound: BoundTranscript, *, session_ref: str) -> LaneHandle:
        """Rebuild a lane handle from a persisted ``amp-orb:<slug>`` binding."""
        slug = _slug_from_ref(str(bound.path))
        record = self._live_record(slug)
        thread = record.get("thread_id")
        if not isinstance(thread, str) or not thread:
            raise LaneDead(f"orb lane {slug!r} has no thread id")
        return LaneHandle(
            plug=self.name,
            lane_id=bound.lane,
            session_ref=session_ref,
            started_at=str(record.get("created") or ""),
            locator={
                "slug": slug,
                "record": str(self._record_path(slug)),
                "instance": bound.binding.instance,
                "client_version": bound.binding.client_version,
            },
            binding_ref=str(bound.path),
            native_session_id=thread,
        )

    # -- lifecycle -----------------------------------------------------------

    def start(self, spec: LaneStart) -> LaneHandle:
        slug = spec.orb_slug or spec.lane_id
        if _SLUG_RE.fullmatch(slug) is None:
            raise LaunchRefused(f"orb lane slug must match {_SLUG_RE.pattern}: {slug!r}")
        if not spec.project:
            raise LaunchRefused("orb lane start requires spec.project (ns/repo)")
        if not spec.orb_size:
            raise LaunchRefused("orb lane start requires spec.orb_size")
        if spec.brief_path is None:
            raise LaunchRefused("orb lane start requires spec.brief_path")
        result = self._run_orb(
            "new", slug, "--project", spec.project, "--orb-size", spec.orb_size, "--brief", str(spec.brief_path)
        )
        if result.returncode != 0:
            detail = self._detail(result)
            if "already exists" in detail:
                raise LaunchRefused(f"orb lane {slug!r} already exists")
            raise LaunchFailed(f"orb lane {slug!r} did not launch: {detail}")
        try:
            record = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LaunchFailed(f"orb lane {slug!r} returned an unreadable record: {exc}") from exc
        if not isinstance(record, dict):
            raise LaunchFailed(f"orb lane {slug!r} returned a non-object record")
        thread = record.get("thread_id")
        return LaneHandle(
            plug=self.name,
            lane_id=slug,
            session_ref=spec.session_ref or f"orb:{slug}:0.0",
            started_at=str(record.get("created") or utc_now_iso()),
            locator={"slug": slug, "record": str(self._record_path(slug))},
            binding_ref=f"{ORB_URI_PREFIX}{slug}",
            native_session_id=thread if isinstance(thread, str) else None,
        )

    def status(self, handle: LaneHandle) -> LaneStatus:
        slug = self._slug_of(handle)
        result = self._run_orb("status", slug)
        if result.returncode != 0:
            detail = self._detail(result)
            if "has no record" in detail:
                raise LaneDead(detail)
            return LaneStatus(state="unknown", detail=detail)
        try:
            record = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise StreamUnavailable(f"orb lane {slug!r} status returned non-JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise StreamUnavailable(f"orb lane {slug!r} status returned a non-object record")
        state = record.get("state")
        observed = record.get("observed")
        busy = observed.get("amp_launcher_busy") is True if isinstance(observed, dict) else False
        mapped: Literal["starting", "running", "exited", "unknown"]
        if state == "launching":
            mapped = "starting"
        elif state == "running":
            mapped = "running"
        elif state in ("launch_failed", "stop_requested"):
            mapped = "exited"
        else:
            mapped = "unknown"
        pid = record.get("amp_pid")
        return LaneStatus(
            state=mapped,
            detail=f"state={state} amp_launcher_busy={busy}",
            pid=pid if isinstance(pid, int) else None,
        )

    def stop(self, handle: LaneHandle, *, force: bool = False) -> StopResult:
        slug = self._slug_of(handle)
        try:
            was_running = self.status(handle).state in {"starting", "running"}
        except LaneDead:
            was_running = False
        result = self._run_orb("stop", slug, *(["--force"] if force else []))
        if result.returncode == 0:
            return StopResult(
                was_running=was_running,
                stopped=True,
                method=f"chitra-orb-lane stop{' --force' if force else ''}",
            )
        detail = self._detail(result)
        if "has no record" in detail:
            raise LaneDead(detail)
        if "launch is uncertain" in detail:
            raise AdapterError(f"orb lane {slug!r} stop refused: {detail} — run chitra-orb-lane adopt first")
        if "stop refused" in detail or "executor is busy" in detail:
            return StopResult(was_running=was_running, stopped=False, method=detail)
        raise AdapterError(f"orb lane {slug!r} stop failed: {detail}")

    # -- delivery ------------------------------------------------------------

    def send(self, handle: LaneHandle, order: WireOrder) -> SendReceipt:
        slug = self._slug_of(handle)
        if not order.text:
            raise SendRejected("orb steer refuses an empty order", status=DispatchStatus.BLOCKED)
        result = self._run_orb("steer", slug, order.text)
        combined = f"{result.stderr or ''}\n{result.stdout or ''}"
        if "has no record" in combined or "has no thread id" in combined:
            raise LaneDead(combined.strip())
        if result.returncode == 0 and "delivered" in result.stdout:
            return SendReceipt(
                order_id=order.order_id,
                sent_at=utc_now_iso(),
                delivery="steered",
                ack="delivered",
                native={"steer": "delivered"},
            )
        if result.returncode == 3 or "swallowed" in result.stdout:
            raise SendRejected(f"orb steer swallowed the order (never reached the thread): {slug}")
        raise SendRejected(f"orb steer failed for {slug}: {self._detail(result)}")

    def prove_read(
        self,
        handle: LaneHandle,
        order: WireOrder,
        receipt: SendReceipt | None = None,
        *,
        deadline_s: float = 60.0,
    ) -> ReadProof:
        """Prove consumption from the thread's structured export.

        One export is one observation; ``deadline_s``-level retry belongs to
        the caller's verify loop (dispatchd re-verifies DELIVERY_UNCONFIRMED
        orders every pass). A ``steer``-acked write with no thread evidence is
        ``accepted``, never ``consumed``.
        """
        del deadline_s
        thread = self._thread_id(handle)
        events = self._events(handle)
        return prove_read_from_events(
            order_id=order.order_id,
            order_text=order.text,
            events=events,
            native_session_id=thread,
            binding_ref=handle.binding_ref,
            acked=receipt is not None,
        )

    def events(self, handle: LaneHandle, cursor: str | None = None) -> EventBatch:
        """Return canonical events for the thread's structured export.

        ``cursor`` is the count of messages already emitted; the export is a
        full snapshot, so the plug slices off already-seen messages before
        converting. Journal dedupe makes a stale cursor safe.
        """
        thread = self._thread_id(handle)
        export = self._export(thread)
        messages = export.get("messages")
        if not isinstance(messages, list):
            raise StreamUnavailable("amp export carries no messages list")
        start = int(cursor) if cursor else 0
        events = amp_events_from_export({**export, "messages": messages[start:]}, **self._context(handle, export))
        return EventBatch(events=events, cursor=str(len(messages)))


def _export_client_version(export: dict[str, Any]) -> str:
    platform = ((export.get("env") or {}).get("initial") or {}).get("platform")
    version = platform.get("clientVersion") if isinstance(platform, dict) else None
    return version if isinstance(version, str) and version else "unknown"


def _slug_from_ref(ref: str) -> str:
    """Extract the lane slug from an ``amp-orb:<slug>`` binding URI."""
    slug = ref[len(ORB_URI_PREFIX) :] if ref.startswith(ORB_URI_PREFIX) else ""
    if _SLUG_RE.fullmatch(slug) is None:
        raise AdapterError(f"unsupported amp orb binding ref: {ref!r} (expected {ORB_URI_PREFIX}<slug>)")
    return slug

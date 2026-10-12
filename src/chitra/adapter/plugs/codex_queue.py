"""Queue input to an existing native Codex session; never type into exec stdout."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from uuid import UUID

from chitra.adapter.contract import (
    Capabilities,
    EventBatch,
    LaneHandle,
    LaneStart,
    LaneStatus,
    ReadProof,
    SendReceipt,
    SendRejected,
    StopResult,
    StreamUnavailable,
    Unsupported,
    WireOrder,
    utc_now_iso,
)
from chitra.adapter.plugs.codex_tmux import CodexTmuxPlug
from chitra.adapter.proof import prove_read_from_events
from chitra.dispatch import directive_voice_violation
from chitra.journal import native_session_identity
from chitra.journal.reader import JsonlTailReader
from chitra.orders import DispatchStatus
from chitra.transcript_bindings import BoundTranscript


class CodexQueuePlug:
    name: ClassVar[str] = "codex-queue"
    client: ClassVar[str] = "codex-queue"
    capabilities: ClassVar[Capabilities] = Capabilities(delivery="steer", read_level="consumed", graceful_stop=False)

    def __init__(self) -> None:
        self._transcripts = CodexTmuxPlug()

    def handle_for_binding(self, bound: BoundTranscript, *, session_ref: str) -> LaneHandle:
        prefix = "codex-queue:"
        if not bound.is_uri or not str(bound.path).startswith(prefix):
            raise StreamUnavailable("codex-queue needs an explicit native transcript locator")
        path = Path(str(bound.path)[len(prefix):])
        if not path.is_absolute():
            raise StreamUnavailable("codex-queue transcript must be an absolute local path")
        native_id = native_session_identity(path)
        if native_id is None:
            raise StreamUnavailable("codex-queue native transcript identity unavailable")
        try:
            UUID(native_id)
        except ValueError as exc:
            raise StreamUnavailable("codex-queue transcript has no native session UUID") from exc
        handle = LaneHandle(
            plug=self.name, lane_id=bound.lane, session_ref=session_ref, started_at="",
            binding_ref=str(bound.path), native_session_id=native_id,
            locator={"transcript_path": str(path), "instance": bound.instance, "client_version": bound.binding.client_version},
        )
        if not any(event.session_id == native_id for event in self._read_events(handle).events):
            raise StreamUnavailable("codex-queue binding is not a native Codex transcript")
        return handle

    def send(self, handle: LaneHandle, order: WireOrder) -> SendReceipt:
        if order.order is None or order.order.message_kind != "operator_relay":
            violation = directive_voice_violation(order.text)
            if violation:
                raise SendRejected(violation, status=DispatchStatus.BLOCKED)
        # Recheck the native binding before egress; never select by pane, recency or session name.
        if not handle.native_session_id or native_session_identity(handle.locator["transcript_path"]) != handle.native_session_id:
            raise StreamUnavailable("codex-queue native transcript identity changed or unavailable")
        if not any(event.session_id == handle.native_session_id for event in self._read_events(handle).events):
            raise StreamUnavailable("codex-queue binding is not a native Codex transcript")
        try:
            result = subprocess.run(
                ["codex", "queue", "--thread", handle.native_session_id, "--message", order.text],
                capture_output=True, text=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            return SendReceipt(
                order_id=order.order_id, sent_at=utc_now_iso(), delivery="queued", ack="UNKNOWN",
                native={"reason": "native queue timed out; acceptance UNKNOWN"},
            )
        except OSError as exc:
            raise SendRejected(f"native Codex queue unavailable: {type(exc).__name__}") from exc
        if result.returncode != 0:
            return SendReceipt(
                order_id=order.order_id, sent_at=utc_now_iso(), delivery="queued", ack="UNKNOWN",
                native={"reason": f"native queue exit {result.returncode}; acceptance UNKNOWN"},
            )
        return SendReceipt(order_id=order.order_id, sent_at=utc_now_iso(), delivery="queued", ack="native queue accepted")

    def events(self, handle: LaneHandle, cursor: str | None = None) -> EventBatch:
        local = replace(handle, binding_ref=handle.locator["transcript_path"])
        return self._transcripts.events(local, cursor)

    def _read_events(self, handle: LaneHandle) -> EventBatch:
        # shortcut: replay the bound rollout for proof; use a durable order cursor if measured history makes dispatch expensive.
        local = replace(handle, binding_ref=handle.locator["transcript_path"])
        try:
            with JsonlTailReader(Path(handle.locator["transcript_path"])) as reader:
                return self._transcripts.events(local, reader=reader)
        except (OSError, ValueError) as exc:
            raise StreamUnavailable(f"native Codex proof unavailable: {type(exc).__name__}") from exc

    def prove_read(
        self, handle: LaneHandle, order: WireOrder, receipt: SendReceipt | None = None, *, deadline_s: float = 60.0,
    ) -> ReadProof:
        # A later dispatch pass reconciles the existing nonce; this plug never retries a queue write.
        del deadline_s
        acked = receipt is not None and receipt.ack == "native queue accepted"
        try:
            created = datetime.fromisoformat(order.order.created_at) if order.order else None
            if created is None or created.tzinfo is None:
                raise ValueError("missing order boundary")
            events = tuple(
                event for event in self._read_events(handle).events
                if event.native_time and datetime.fromisoformat(event.native_time).tzinfo is not None
                and datetime.fromisoformat(event.native_time) >= created
            )
        except ValueError:
            return ReadProof(order_id=order.order_id, level="accepted" if acked else "none", detail="order time boundary unavailable")
        proof = prove_read_from_events(
            order_id=order.order_id, order_text=order.text, events=events,
            native_session_id=handle.native_session_id, binding_ref=handle.binding_ref,
            acked=acked,
        )
        if proof.level == "none" and receipt and receipt.native.get("reason"):
            return replace(proof, detail=str(receipt.native["reason"]))
        return proof

    def start(self, spec: LaneStart) -> LaneHandle:
        raise Unsupported("codex-queue only targets existing native sessions; lane-run owns launch")

    def stop(self, handle: LaneHandle) -> StopResult:
        raise Unsupported("codex-queue does not stop or restart native sessions")

    def status(self, handle: LaneHandle) -> LaneStatus:
        return LaneStatus(state="unknown", detail="a transcript does not establish native process health")

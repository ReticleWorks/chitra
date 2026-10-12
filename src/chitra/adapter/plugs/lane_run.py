"""Safely continue an existing lane-run Codex exec or Claude print lane."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from chitra.adapter.contract import (
    Capabilities,
    EventBatch,
    LaneHandle,
    LaneStart,
    LaneStatus,
    ReadProof,
    SendReceipt,
    StopResult,
    StreamUnavailable,
    Unsupported,
    WireOrder,
    utc_now_iso,
)
from chitra.adapter.plugs.claude_tmux import ClaudeTmuxPlug
from chitra.adapter.plugs.codex_tmux import CodexTmuxPlug
from chitra.adapter.proof import prove_read_from_events
from chitra.journal import native_session_identity
from chitra.orders import DispatchOrder
from chitra.transcript_bindings import BoundTranscript

_CLIENTS = {"lane-run-codex": "codex", "lane-run-claude": "claude"}


def _lane_root() -> Path:
    override = os.environ.get("AGENT_TOOLS_LANE_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local/share/agent-tools/lanes" if sys.platform == "darwin" else Path("/var/lib/agent-tools/lanes")


def _continuation_text(nonce: str, order: DispatchOrder) -> str:
    if order.goal_digest is None:
        raise ValueError("lane-run needs the frozen scope digest")
    return (
        "NATIVE_ORDER_NONCE=" + json.dumps(nonce) + "\n"
        + "NATIVE_GOAL_ID=" + json.dumps(order.session_ref) + "\n"
        + "FROZEN_SCOPE_SHA256=" + order.goal_digest + "\n\n"
        + order.nudge + "\n\n"
    )


def _native_id(path: str) -> str | None:
    try:
        return native_session_identity(path)
    except (OSError, ValueError, RuntimeError):
        return None


class LaneRunPlug:
    name: ClassVar[str] = "lane-run"
    client: ClassVar[str] = "lane-run"
    capabilities: ClassVar[Capabilities] = Capabilities(delivery="steer", read_level="consumed", graceful_stop=False)

    def __init__(self) -> None:
        self._native = {"codex": CodexTmuxPlug(), "claude": ClaudeTmuxPlug()}

    def handle_for_binding(self, bound: BoundTranscript, *, session_ref: str) -> LaneHandle:
        selector = str(bound.binding.client)
        cli = _CLIENTS.get(selector)
        prefix = selector + ":"
        if cli is None or not bound.is_uri or not str(bound.path).startswith(prefix):
            raise StreamUnavailable("lane-run needs an explicit native transcript locator")
        transcript = Path(str(bound.path)[len(prefix):])
        if not transcript.is_absolute():
            raise StreamUnavailable("lane-run transcript must be an absolute local path")
        native_id = _native_id(str(transcript))
        if native_id is None:
            raise StreamUnavailable("UNKNOWN: lane-run native transcript identity unavailable")
        try:
            lane_dir = _lane_root().resolve(strict=True) / bound.lane
            record = json.loads((lane_dir / "lane.json").read_text(encoding="utf-8"))
            cwd = Path(record["cwd"]).resolve(strict=True)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            raise StreamUnavailable(f"UNKNOWN: lane-run lane record unavailable: {type(exc).__name__}") from exc
        if (
            not isinstance(record, dict)
            or record.get("id") != bound.lane
            or record.get("record") != str(lane_dir / "lane.json")
            or record.get("cli") != cli
            or record.get("native_session_id") != native_id
            or record.get("owner_uid") != os.getuid()
            or type(record.get("owner_session_id")) is not int
            or not isinstance(record.get("ownership_token"), str)
            or not record["ownership_token"]
        ):
            raise StreamUnavailable("UNKNOWN: lane-run binding does not match the exact owned native lane")
        if not isinstance(record.get("cwd"), str) or not Path(record["cwd"]).is_absolute():
            raise StreamUnavailable("UNKNOWN: lane-run lane cwd is unavailable")
        return LaneHandle(
            plug=self.name,
            lane_id=bound.lane,
            session_ref=session_ref,
            started_at="",
            binding_ref=str(bound.path),
            native_session_id=native_id,
            locator={
                "client": cli,
                "client_version": bound.binding.client_version,
                "instance": bound.binding.instance,
                "transcript_path": str(transcript),
                "cwd": str(cwd),
            },
        )

    def _packet(self, handle: LaneHandle, order: WireOrder) -> dict[str, Any]:
        dispatch = order.order
        if dispatch is None or dispatch.goal_version is None or dispatch.goal_digest is None:
            raise StreamUnavailable("lane-run needs the existing governed goal version and frozen scope digest")
        if dispatch.order_id != order.order_id or dispatch.nudge != order.text or dispatch.session_ref != handle.session_ref:
            raise StreamUnavailable("lane-run wire order does not match its governed dispatch order")
        try:
            root = _lane_root().resolve(strict=True)
            lane_dir = root / handle.lane_id
            record_path = lane_dir / "lane.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            cwd = Path(record["cwd"]).resolve(strict=True)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            raise StreamUnavailable(f"UNKNOWN: lane-run lane record unavailable: {type(exc).__name__}") from exc
        if (
            not isinstance(record, dict)
            or record.get("id") != handle.lane_id
            or record.get("record") != str(record_path)
            or record.get("cli") != handle.locator["client"]
            or record.get("native_session_id") != handle.native_session_id
            or record.get("owner_uid") != os.getuid()
            or type(record.get("owner_session_id")) is not int
            or not isinstance(record.get("ownership_token"), str)
            or not record["ownership_token"]
            or str(cwd) != handle.locator["cwd"]
            or _native_id(handle.locator["transcript_path"]) != handle.native_session_id
        ):
            raise StreamUnavailable("UNKNOWN: lane-run native ownership or transcript binding changed")
        return {
            "schema": "agent-tools.continuation.v1",
            "nonce": order.order_id,
            "lane_id": handle.lane_id,
            "cli": handle.locator["client"],
            "native_session_id": handle.native_session_id,
            "owner_uid": record["owner_uid"],
            "owner_session_id": record["owner_session_id"],
            "ownership_token": record["ownership_token"],
            "cwd": str(cwd),
            "transcript_path": handle.locator["transcript_path"],
            "authority": {
                "goal_id": dispatch.session_ref,
                "frozen_scope_sha256": dispatch.goal_digest,
            },
            "message": order.text,
        }

    @staticmethod
    def _call(action: str, value: Path) -> dict[str, Any] | None:
        executable = shutil.which("lane-run")
        if executable is None:
            return None
        try:
            result = subprocess.run(
                [executable, action, str(value)], capture_output=True, text=True, timeout=120, check=False,
            )
            raw = json.loads(result.stdout) if result.stdout else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if (
            result.returncode != 0
            or not isinstance(raw, dict)
            or raw.get("schema") != "agent-tools.continuation-result.v1"
            or not isinstance(raw.get("nonce"), str)
            or not isinstance(raw.get("lane_id"), str)
            or not isinstance(raw.get("native_session_id"), str)
            or raw.get("state") not in {"accepted", "starting", "started", "consumed"}
        ):
            return None
        return {key: raw.get(key) for key in ("nonce", "lane_id", "native_session_id", "state", "action", "receipt")}

    def _enqueue(self, packet: dict[str, Any]) -> dict[str, Any] | None:
        fd, raw_path = tempfile.mkstemp(prefix="chitra-lane-run-", suffix=".json")
        path = Path(raw_path)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(packet, stream, ensure_ascii=False)
                stream.write("\n")
            return self._call("enqueue", path)
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _matches(result: dict[str, Any] | None, packet: dict[str, Any]) -> bool:
        return bool(result and result.get("nonce") == packet["nonce"]
                    and result.get("lane_id") == packet["lane_id"]
                    and result.get("native_session_id") == packet["native_session_id"])

    def send(self, handle: LaneHandle, order: WireOrder) -> SendReceipt:
        packet = self._packet(handle, order)
        result = self._enqueue(packet)
        if not self._matches(result, packet):
            return SendReceipt(
                order_id=order.order_id, sent_at=utc_now_iso(), delivery="queued", ack="UNKNOWN",
                native={"reason": "lane-run enqueue result UNKNOWN"},
            )
        assert result is not None
        receipt = result.get("receipt")
        return SendReceipt(
            order_id=order.order_id,
            sent_at=utc_now_iso(),
            delivery="queued",
            ack="lane-run durably queued",
            native={"receipt": receipt} if isinstance(receipt, str) and Path(receipt).is_absolute() else {},
        )

    def events(self, handle: LaneHandle, cursor: str | None = None) -> EventBatch:
        local = replace(handle, binding_ref=handle.locator["transcript_path"])
        return self._native[handle.locator["client"]].events(local, cursor)

    def prove_read(
        self, handle: LaneHandle, order: WireOrder, receipt: SendReceipt | None = None, *, deadline_s: float = 60.0,
    ) -> ReadProof:
        del deadline_s
        try:
            packet = self._packet(handle, order)
        except StreamUnavailable as exc:
            return ReadProof(order_id=order.order_id, level="none", detail=f"UNKNOWN: {exc}", binding_ref=handle.binding_ref)
        accepted = receipt is not None and receipt.ack == "lane-run durably queued"
        receipt_path = receipt.native.get("receipt") if receipt is not None else None
        if isinstance(receipt_path, str) and Path(receipt_path).is_absolute():
            queued: dict[str, Any] | None = {
                "receipt": receipt_path,
                "nonce": packet["nonce"],
                "lane_id": packet["lane_id"],
                "native_session_id": packet["native_session_id"],
            }
        else:
            queued = self._enqueue(packet)
            accepted = self._matches(queued, packet)
            receipt_path = queued.get("receipt") if queued else None
        if isinstance(receipt_path, str) and Path(receipt_path).is_absolute():
            drained = self._call("drain", Path(receipt_path))
            accepted = accepted or self._matches(drained, packet)

        try:
            dispatch = order.order
            if dispatch is None:
                raise ValueError("missing dispatch order")
            created = datetime.fromisoformat(dispatch.created_at)
            if created is None or created.tzinfo is None:
                raise ValueError("missing order time boundary")
            events = tuple(
                event for event in self.events(handle).events
                if event.session_id == handle.native_session_id and event.native_time
                and datetime.fromisoformat(event.native_time).tzinfo is not None
                and datetime.fromisoformat(event.native_time) >= created
            )
        except (OSError, ValueError, StreamUnavailable):
            return ReadProof(
                order_id=order.order_id, level="accepted" if accepted else "none",
                detail="UNKNOWN: bound native transcript evidence unavailable", binding_ref=handle.binding_ref,
            )
        proof = prove_read_from_events(
            order_id=order.order_id,
            order_text=_continuation_text(order.order_id, dispatch),
            events=events,
            native_session_id=handle.native_session_id,
            binding_ref=handle.binding_ref,
            acked=accepted,
        )
        return proof if proof.level == "consumed" else replace(proof, detail=f"UNKNOWN: {proof.detail}")

    def start(self, spec: LaneStart) -> LaneHandle:
        raise Unsupported("lane-run only continues an existing native lane")

    def stop(self, handle: LaneHandle) -> StopResult:
        raise Unsupported("lane-run continuation does not stop or restart native lanes")

    def status(self, handle: LaneHandle) -> LaneStatus:
        return LaneStatus(state="unknown", detail="native continuation receipts do not prove lane process health")

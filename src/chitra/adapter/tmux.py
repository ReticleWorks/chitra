"""The shared lane plug for tmux-hosted harnesses (Claude, Codex, OpenCode).

``send`` delegates to ``chitra.dispatch.dispatch_to_tmux`` — the governed,
verified paste path — so a lane behind the interface behaves exactly like the
historical dispatch path: directive-voice guard, copy-mode cancel, bracketed
paste, verified submit, transcript proof, pane fallback. Read proof stays
fused inside that call on the send path; ``prove_read`` decodes the receipt's
embedded dispatch result, or — with no receipt, the nonce-reconcile shape —
re-verifies against the bound transcript alone.

``events`` reads the lane's bound JSONL transcript through the same
``JsonlTailReader`` + normalizer the journal ingestor uses. The reader is
injectable so a remote lane's transcript can be supplied by the caller instead
of read from a local path.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar

from chitra import dispatch as dispatch_mod
from chitra import lane_anchor
from chitra.adapter.contract import (
    AdapterError,
    Capabilities,
    EventBatch,
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
    Unsupported,
    WireOrder,
    utc_now_iso,
)
from chitra.dispatch import DispatchTuning, transcript_confirms_nudge
from chitra.journal.normalizers import NormalizationContext, TranscriptNormalizer, make_normalizer
from chitra.journal.reader import JsonlTailReader
from chitra.orders import DispatchOrder, DispatchResult, DispatchStatus
from chitra.policy_config import PolicyConfig
from chitra.transcript_bindings import BoundTranscript

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _send_rejected(result: DispatchResult) -> SendRejected:
    """Map a rejected/failed fused dispatch result to the contract error."""
    status = DispatchStatus.BLOCKED if result.status is DispatchStatus.BLOCKED else DispatchStatus.FAILED
    detail = result.reason or f"tmux dispatch {result.status.value}"
    return SendRejected(detail, status=status)


class TmuxLanePlug:
    """One tmux-hosted harness behind the shared lane interface."""

    name: ClassVar[str] = "tmux"
    backend: ClassVar[str] = "claude"
    client: ClassVar[str] = "claude"
    capabilities: ClassVar[Capabilities] = Capabilities(
        delivery="composer",
        read_level="consumed",
        graceful_stop=False,
    )

    def __init__(self) -> None:
        # Pooled (reader, normalizer) per bound transcript path — the same
        # incremental-stream state monitord's ingestor pool keeps.
        self._streams: dict[str, tuple[JsonlTailReader, TranscriptNormalizer]] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        spec: LaneStart,
        *,
        runner: CommandRunner | None = None,
        self_test: bool = True,
        host: str = lane_anchor.SANCTIONED_HOST,
        socket_path: Path | None = None,
    ) -> LaneHandle:
        lane = spec.lane
        if lane is None:
            raise LaunchRefused(f"{self.name} starts declared lanes only (spec.lane is required)")
        try:
            lane_anchor.start_lane(
                lane,
                backend=self.backend,
                model=spec.model,
                effort=spec.effort,
                host=host,
                socket_path=socket_path,
                runner=runner or _run,
                self_test=self_test,
            )
        except lane_anchor.LaneLaunchRefused as exc:
            raise LaunchRefused(str(exc)) from exc
        except lane_anchor.LaneStartupFailed as exc:
            raise LaunchFailed(str(exc)) from exc
        return LaneHandle(
            plug=self.name,
            lane_id=lane.identifier,
            session_ref=lane_anchor.session_ref(lane, host),
            started_at=utc_now_iso(),
            locator={
                "host": host,
                "tmux_session": lane.tmux_session,
                "tmux_socket": str(lane.tmux_socket),
                "pane": f"{lane.tmux_session}:0.0",
                "backend": self.backend,
            },
            lane=lane,
        )

    def status(
        self,
        handle: LaneHandle,
        *,
        runner: CommandRunner | None = None,
    ) -> LaneStatus:
        """tmux ``has-session`` for the lane's session on its socket."""
        run = runner or _run
        session = handle.locator.get("tmux_session")
        socket = handle.locator.get("tmux_socket")
        if not session:
            raise Unsupported(f"{self.name} status needs the handle's tmux session locator")
        command = ["tmux", "-S", socket, "has-session", "-t", session] if socket else ["tmux", "has-session", "-t", session]
        if handle.lane is not None:
            command = lane_anchor._run_as_lane(handle.lane, command)
        result = run(command)
        if result.returncode == 0:
            return LaneStatus(state="running", detail=f"tmux session {session} is live")
        if lane_anchor._session_absent(result):
            return LaneStatus(state="exited", detail=f"tmux session {session} is absent")
        detail = (result.stderr or result.stdout or "").strip()
        return LaneStatus(state="unknown", detail=detail or f"tmux exited {result.returncode}")

    def stop(
        self,
        handle: LaneHandle,
        *,
        runner: CommandRunner | None = None,
    ) -> StopResult:
        """Kill the lane's tmux session (the historical hard stop)."""
        run = runner or _run
        session = handle.locator.get("tmux_session")
        if handle.lane is not None:
            was_running = self.status(handle, runner=run).state == "running"
            try:
                stopped = lane_anchor.stop_lane(handle.lane, runner=run)
            except RuntimeError as exc:
                raise AdapterError(str(exc)) from exc
            return StopResult(was_running=was_running, stopped=stopped, method="tmux kill-session")
        socket = handle.locator.get("tmux_socket")
        if not session:
            raise Unsupported(f"{self.name} stop needs the handle's tmux session locator")
        command = ["tmux", "-S", socket, "kill-session", "-t", session] if socket else ["tmux", "kill-session", "-t", session]
        result = run(command)
        if result.returncode == 0:
            return StopResult(was_running=True, stopped=True, method="tmux kill-session")
        if lane_anchor._session_absent(result):
            raise LaneDead(f"tmux session {session} is absent")
        detail = (result.stderr or result.stdout or "").strip()
        raise AdapterError(detail or f"tmux exited {result.returncode}")

    # -- bindings ------------------------------------------------------------

    def handle_for_binding(self, bound: BoundTranscript, *, session_ref: str) -> LaneHandle:
        """Rebuild a lane handle from a persisted transcript binding."""
        parts = session_ref.split(":")
        locator = {
            "host": parts[0] if len(parts) == 3 else "",
            "instance": bound.binding.instance,
            "client_version": bound.binding.client_version,
        }
        if len(parts) == 3:
            locator["tmux_session"] = parts[1]
            locator["pane"] = f"{parts[1]}:{parts[2]}"
        return LaneHandle(
            plug=self.name,
            lane_id=bound.lane,
            session_ref=session_ref,
            started_at="",
            locator=locator,
            binding_ref=str(bound.path),
        )

    # -- events --------------------------------------------------------------

    def events(
        self,
        handle: LaneHandle,
        cursor: str | None = None,
        *,
        reader: JsonlTailReader | None = None,
    ) -> EventBatch:
        """Poll the bound JSONL transcript and return normalized events.

        The reader is injectable: pass ``reader`` to stream a transcript that
        is not a local path (a remote lane's file pulled over ssh, an in-test
        reader). ``cursor`` is informational — pooled reader state carries the
        resume position; it reports the byte offset after the poll.
        """
        del cursor
        binding_ref = handle.binding_ref
        if binding_ref is None:
            raise StreamUnavailable(f"{self.name} lane {handle.lane_id} has no bound transcript")
        if reader is not None:
            batch = reader.poll()
            normalizer = make_normalizer(self._context(handle))
            events = [event for record in batch.records for event in normalizer.normalize(record)]
            return EventBatch(events=tuple(events), cursor=str(reader.offset))
        stream = self._streams.get(binding_ref)
        if stream is None:
            stream = (JsonlTailReader(Path(binding_ref)), make_normalizer(self._context(handle)))
            self._streams[binding_ref] = stream
        tail_reader, normalizer = stream
        batch = tail_reader.poll()
        events = [event for record in batch.records for event in normalizer.normalize(record)]
        return EventBatch(events=tuple(events), cursor=str(tail_reader.offset))

    def _context(self, handle: LaneHandle) -> NormalizationContext:
        return NormalizationContext(
            instance=handle.locator.get("instance") or handle.lane_id,
            lane=handle.lane_id,
            client=self.client,
            client_version=handle.locator.get("client_version") or "unknown",
            goal_ref=handle.session_ref,
        )

    # -- delivery ------------------------------------------------------------

    def send(
        self,
        handle: LaneHandle,
        order: WireOrder,
        *,
        runner: dispatch_mod.TmuxRunner | None = None,
        input_runner: dispatch_mod.TmuxInputRunner | None = None,
        local_extra: set[str] | None = None,
        allowed_hosts: set[str] | None = None,
        projects_root: Path | None = None,
        exclude_transcripts: set[Path] | None = None,
        tuning: DispatchTuning | None = None,
        policy: PolicyConfig | None = None,
        tmux_socket: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> SendReceipt:
        """Paste the order through the governed tmux dispatch path.

        The dispatch is fused: it returns only after the post-paste verify
        window, so the receipt carries the whole dispatch result for
        ``prove_read`` to decode.
        """
        socket = tmux_socket or (Path(handle.locator["tmux_socket"]) if handle.locator.get("tmux_socket") else None)
        dispatch_order = order.order or DispatchOrder(
            order_id=order.order_id, session_ref=handle.session_ref, nudge=order.text
        )
        result = dispatch_mod.dispatch_to_tmux(
            dispatch_order,
            runner=runner,
            input_runner=input_runner,
            local_extra=local_extra,
            allowed_hosts=allowed_hosts,
            projects_root=projects_root,
            expected_transcript_path=handle.binding_ref,
            exclude_transcripts=exclude_transcripts,
            tuning=tuning,
            policy=policy,
            tmux_socket=socket,
            sleep=sleep,
        )
        if result.status in (DispatchStatus.BLOCKED, DispatchStatus.FAILED):
            raise _send_rejected(result)
        return SendReceipt(
            order_id=order.order_id,
            sent_at=result.at,
            delivery="pasted",
            ack=result.reason,
            native={"dispatch_result": result.model_dump(mode="json")},
        )

    def prove_read(
        self,
        handle: LaneHandle,
        order: WireOrder,
        receipt: SendReceipt | None = None,
        *,
        deadline_s: float = 60.0,
        runner: dispatch_mod.TmuxRunner | None = None,
        projects_root: Path | None = None,
        local_extra: set[str] | None = None,
    ) -> ReadProof:
        """Decode the send's fused proof, or re-verify the bound transcript.

        With no receipt (the nonce-reconcile shape) only the transcript grep
        runs — pane evidence alone never upgrades a reconcile, matching the
        dispatchd crash path.
        """
        del deadline_s
        stored = receipt.native.get("dispatch_result") if receipt is not None else None
        if isinstance(stored, dict):
            result = DispatchResult.model_validate(stored)
            if result.status is DispatchStatus.SENT:
                return ReadProof(
                    order_id=order.order_id,
                    level="consumed",
                    detail=result.reason,
                    native_session_id=result.native_session_id,
                    binding_ref=result.transcript_path or handle.binding_ref,
                )
            if result.status is DispatchStatus.DELIVERY_UNCONFIRMED:
                return ReadProof(
                    order_id=order.order_id,
                    level="accepted",
                    detail=result.reason,
                    binding_ref=handle.binding_ref,
                )
            return ReadProof(order_id=order.order_id, level="none", detail=result.reason, binding_ref=handle.binding_ref)

        host = handle.locator.get("host") or _session_ref_host(handle.session_ref)
        confirmed, transcript_path = transcript_confirms_nudge(
            order.text,
            host=host,
            projects_root=projects_root,
            expected_transcript_path=handle.binding_ref,
            runner=runner,
            local_extra=local_extra,
        )
        if confirmed:
            return ReadProof(
                order_id=order.order_id,
                level="consumed",
                detail="order recorded as user input and agent/tool activity followed",
                binding_ref=str(transcript_path) if transcript_path is not None else handle.binding_ref,
            )
        return ReadProof(
            order_id=order.order_id,
            level="none",
            detail="order text not confirmed in the bound transcript",
            binding_ref=handle.binding_ref,
        )


def _session_ref_host(session_ref: str) -> str:
    parts = session_ref.split(":")
    return parts[0] if len(parts) == 3 else ""

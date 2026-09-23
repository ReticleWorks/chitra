"""The harness-neutral lane contract: start, events, send, prove_read, status, stop.

Every lane Chitra drives — tmux-hosted Claude/Codex/OpenCode today, Amp orb
lanes through ``chitra-orb-lane`` — is one :class:`LanePlug`. Chitra code talks
to the six operations; harness differences live inside the plug. Nothing here
assumes the lane's evidence is a local file: ``LaneHandle.binding_ref`` is an
opaque locator (a JSONL path for tmux lanes, a URI such as ``amp-orb:<slug>``
for orb lanes) and event readers are injectable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from chitra.journal.models import CanonicalEvent
from chitra.lane_config import LaneSpec
from chitra.orders import DispatchOrder, DispatchStatus
from chitra.transcript_bindings import BoundTranscript

ReadLevel = Literal["consumed", "accepted", "none"]
"""How far a plug can prove an order traveled.

``consumed`` — the order appears as a user-input record and agent/tool
activity follows it. ``accepted`` — the harness acknowledged the write but the
lane's log cannot (yet) be shown to contain it. ``none`` — the plug cannot
prove either. A plug must never report above its declared
``Capabilities.read_level``.
"""

Cursor = str


class AdapterError(RuntimeError):
    """Base for every adapter-raised failure."""


class LaunchRefused(AdapterError):
    """The plug will not start this lane (policy, missing inputs, unsafe spec)."""


class HarnessUnavailable(AdapterError):
    """The harness binary, daemon, or registered plug itself is absent."""


class LaunchFailed(AdapterError):
    """start() ran but the lane did not come up."""


class LaneDead(AdapterError):
    """The lane the handle names no longer exists (session, record, or thread gone)."""


class SendRejected(AdapterError):
    """send() was refused or failed before/instead of landing on the wire.

    ``status`` is the DispatchStatus the caller should record — BLOCKED when
    the order was rejected without delivery, FAILED when a delivery attempt
    did not reach the lane.
    """

    def __init__(self, reason: str, *, status: DispatchStatus = DispatchStatus.FAILED) -> None:
        if status not in (DispatchStatus.BLOCKED, DispatchStatus.FAILED):
            raise ValueError("SendRejected maps to BLOCKED or FAILED only")
        super().__init__(reason)
        self.reason = reason
        self.status = status


class ReadUnproven(AdapterError):
    """prove_read() could not evaluate the lane's evidence stream."""


class StreamUnavailable(AdapterError):
    """The lane's event/proof stream cannot be read (missing, foreign, unreadable)."""


class Unsupported(AdapterError):
    """The plug does not implement this operation for the given handle."""


@dataclass(frozen=True, slots=True)
class LaneStart:
    """Launch parameters for one lane.

    Plugs read the fields their harness needs: tmux plugs require ``lane``
    (the declared LaneSpec); the amp-orb plug requires ``orb_slug``,
    ``project``, and ``brief_path``. ``model``/``effort`` are launch-time
    choices, never lane-manifest state.
    """

    lane_id: str
    session_ref: str
    workdir: Path | None = None
    model: str | None = None
    effort: str | None = None
    lane: LaneSpec | None = None
    orb_slug: str | None = None
    project: str | None = None
    orb_size: str | None = None
    brief_path: Path | None = None


@dataclass(frozen=True, slots=True)
class LaneHandle:
    """A durable reference to one running (or stopped) lane.

    ``locator`` carries the plug-private coordinates needed to reach the lane
    again (tmux session/socket, orb slug, thread id, binding identity). It
    round-trips through JSON via ``registry.save_handle``/``load_handle``.
    ``lane`` holds the live LaneSpec for tmux plugs; it is not serialized —
    a reloaded handle that needs it raises :class:`Unsupported`.
    """

    plug: str
    lane_id: str
    session_ref: str
    started_at: str
    locator: Mapping[str, str] = field(default_factory=dict)
    binding_ref: str | None = None
    native_session_id: str | None = None
    lane: LaneSpec | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "chitra.lane-handle.v1",
            "plug": self.plug,
            "lane_id": self.lane_id,
            "session_ref": self.session_ref,
            "started_at": self.started_at,
            "locator": dict(self.locator),
            "binding_ref": self.binding_ref,
            "native_session_id": self.native_session_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LaneHandle:
        if payload.get("schema") != "chitra.lane-handle.v1":
            raise ValueError(f"not a chitra.lane-handle.v1 record: {payload.get('schema')!r}")
        locator = payload.get("locator")
        if locator is not None and not isinstance(locator, dict):
            raise ValueError("lane handle locator must be an object")
        return cls(
            plug=str(payload["plug"]),
            lane_id=str(payload["lane_id"]),
            session_ref=str(payload["session_ref"]),
            started_at=str(payload["started_at"]),
            locator={str(k): str(v) for k, v in (locator or {}).items()},
            binding_ref=payload.get("binding_ref"),
            native_session_id=payload.get("native_session_id"),
        )


@dataclass(frozen=True, slots=True)
class WireOrder:
    """The exact text Chitra puts on the lane's wire. Never rewritten.

    ``order`` carries the full DispatchOrder when the caller has one (tag,
    routing_hint, task_type, attestations, input hashes) so plugs that drive
    the governed tmux path can hand it through byte-identical. A bare
    ``order_id``/``text`` pair is enough for plugs that only steer text.
    """

    order_id: str
    text: str
    order: DispatchOrder | None = None

    @classmethod
    def from_order(cls, order: DispatchOrder) -> WireOrder:
        return cls(order_id=order.order_id, text=order.nudge, order=order)


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """send() outcome: the order reached the lane's wire.

    ``native`` is a plug-private evidence bag (the tmux plug stores its fused
    dispatch result; the amp plug stores the steer verdict). It never crosses
    plug boundaries.
    """

    order_id: str
    sent_at: str
    delivery: Literal["pasted", "steered", "queued", "tool_boundary"]
    ack: str = ""
    native: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReadProof:
    """prove_read() outcome.

    ``level`` is ``consumed`` only when the order text was found as a
    user-input record and subsequent agent/tool activity exists. ``accepted``
    means the harness took the write (or the input record exists) but
    consumption is not proven — Chitra maps it to DELIVERY_UNCONFIRMED.
    ``binding_ref``/``native_session_id`` bind the proof to the lane's
    evidence source (bound transcript path or URI; native session identity
    such as the Amp thread id) for the delivery ledger.
    """

    order_id: str
    level: ReadLevel
    detail: str = ""
    input_event_id: str | None = None
    consuming_event_id: str | None = None
    native_session_id: str | None = None
    binding_ref: str | None = None


@dataclass(frozen=True, slots=True)
class LaneStatus:
    state: Literal["starting", "running", "exited", "unknown"]
    detail: str = ""
    pid: int | None = None
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class StopResult:
    was_running: bool
    stopped: bool
    method: str


@dataclass(frozen=True, slots=True)
class EventBatch:
    """One events() pull. ``cursor`` is an opaque resume token the caller
    hands back on the next call; ``None`` means full replay."""

    events: tuple[CanonicalEvent, ...]
    cursor: str | None = None
    decode_errors: int = 0


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a plug can prove and how it delivers. Static per plug."""

    delivery: Literal["composer", "steer"]
    read_level: ReadLevel
    graceful_stop: bool


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@runtime_checkable
class LanePlug(Protocol):
    """One harness behind the shared lane interface."""

    name: ClassVar[str]
    client: ClassVar[str]
    capabilities: ClassVar[Capabilities]

    def start(self, spec: LaneStart) -> LaneHandle:
        """Launch the lane and return its handle.

        Raises LaunchRefused (spec rejected), HarnessUnavailable (binary or
        daemon missing), LaunchFailed (launch attempted, did not come up).
        """
        ...

    def events(self, handle: LaneHandle, cursor: str | None = None) -> EventBatch:
        """Pull canonical events from the lane's evidence stream."""
        ...

    def send(self, handle: LaneHandle, order: WireOrder) -> SendReceipt:
        """Put ``order.text`` on the wire unchanged.

        Raises SendRejected (BLOCKED/FAILED), LaneDead, HarnessUnavailable.
        """
        ...

    def prove_read(
        self,
        handle: LaneHandle,
        order: WireOrder,
        receipt: SendReceipt | None = None,
        *,
        deadline_s: float = 60.0,
    ) -> ReadProof:
        """Prove the order was consumed: user-input record + later activity.

        A bare harness ack is ``accepted``, never ``consumed``. Raises
        StreamUnavailable when the evidence stream cannot be read.
        """
        ...

    def status(self, handle: LaneHandle) -> LaneStatus:
        """Current lane liveness."""
        ...

    def stop(self, handle: LaneHandle) -> StopResult:
        """Stop the lane. Raises LaneDead if it is already gone."""
        ...

    def handle_for_binding(self, bound: BoundTranscript, *, session_ref: str) -> LaneHandle:
        """Rebuild a lane handle from a persisted transcript binding.

        The binding is the durable handoff between dispatch and monitor
        passes: a file path for JSONL-transcript lanes, a locator URI for
        plug-owned evidence streams. Raises LaneDead when the lane record is
        gone or terminal, AdapterError on a malformed locator.
        """
        ...

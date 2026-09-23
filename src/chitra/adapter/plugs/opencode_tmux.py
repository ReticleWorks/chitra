"""The OpenCode tmux lane plug.

OpenCode keeps its session state in SQLite, not a JSONL transcript: the plug
can steer the lane's pane but has no structured event stream to prove reads
from. ``capabilities.read_level`` is therefore ``accepted`` — a cleared
composer proves the order reached the lane, never that it was consumed — and
``prove_read`` never reports ``consumed``.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from chitra.adapter.contract import Capabilities, EventBatch, LaneHandle, ReadProof, SendReceipt, StreamUnavailable, WireOrder
from chitra.adapter.tmux import TmuxLanePlug
from chitra.dispatch import TmuxRunner


class OpenCodeTmuxPlug(TmuxLanePlug):
    name: ClassVar[str] = "opencode-tmux"
    backend: ClassVar[str] = "opencode"
    client: ClassVar[str] = "opencode"
    capabilities: ClassVar[Capabilities] = Capabilities(
        delivery="composer",
        read_level="accepted",
        graceful_stop=False,
    )

    def events(self, handle: LaneHandle, cursor: str | None = None, **_kwargs: object) -> EventBatch:
        raise StreamUnavailable(
            "opencode lanes keep session state in SQLite — no JSONL transcript stream exists to normalize"
        )

    def prove_read(
        self,
        handle: LaneHandle,
        order: WireOrder,
        receipt: SendReceipt | None = None,
        *,
        deadline_s: float = 60.0,
        runner: TmuxRunner | None = None,
        projects_root: Path | None = None,
        local_extra: set[str] | None = None,
    ) -> ReadProof:
        """Pane evidence can show acceptance, never transcript-level consumption."""
        proof = super().prove_read(
            handle,
            order,
            receipt,
            deadline_s=deadline_s,
            runner=runner,
            projects_root=projects_root,
            local_extra=local_extra,
        )
        if proof.level == "consumed":
            return ReadProof(
                order_id=proof.order_id,
                level="accepted",
                detail=f"{proof.detail} (opencode has no transcript; capped at accepted)",
                input_event_id=proof.input_event_id,
                consuming_event_id=proof.consuming_event_id,
                native_session_id=proof.native_session_id,
                binding_ref=proof.binding_ref,
            )
        return proof

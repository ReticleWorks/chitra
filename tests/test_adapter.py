"""Tests for the lane adapter package and its dispatchd/monitord routing.

Each test pins one behavior the phase-1 adapter adds: registry resolution,
URI transcript bindings, tmux plug delegation to ``dispatch_to_tmux``, the
amp-orb plug's steer/export proof path, and the daemon's URI-bound order
routing. Nothing here shells out to a real harness — every runner is fake.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import chitra.dispatchd as dispatchd_mod
import chitra.monitord as monitord_mod
from chitra.adapter.contract import (
    Capabilities,
    EventBatch,
    HarnessUnavailable,
    LaneDead,
    LaneHandle,
    LaneStatus,
    ReadProof,
    SendReceipt,
    StopResult,
    StreamUnavailable,
    WireOrder,
)
from chitra.adapter.plugs.amp_orb import AmpOrbPlug, amp_events_from_export
from chitra.adapter.plugs.claude_tmux import ClaudeTmuxPlug
from chitra.adapter.plugs.opencode_tmux import OpenCodeTmuxPlug
from chitra.adapter.proof import prove_read_from_events
from chitra.adapter.registry import plug, plug_for_client, plug_names
from chitra.dispatch import DispatchResult, DispatchStatus
from chitra.journal.models import CanonicalType, Client
from chitra.journal.normalizers import NormalizationContext, make_normalizer
from chitra.journal.store import EventJournal
from chitra.orders import DispatchOrder
from chitra.transcript_bindings import BoundTranscript, TranscriptBinding, load_transcript_bindings


def _binding(**overrides: Any) -> TranscriptBinding:
    base = {
        "session_ref": "host:lane-a:0.0",
        "lane": "lane-a",
        "path": "amp-orb:ops-1",
        "client": "amp",
        "client_version": "0.0.1",
        "instance": "pytest",
    }
    base.update(overrides)
    return TranscriptBinding(**base)


def _amp_record(slug: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": "chitra.orb-lane.v1",
        "lane_slug": slug,
        "state": "running",
        "thread_id": "T-9abc",
        "created": "2026-09-01T00:00:00Z",
        "steers": [],
    }
    record.update(overrides)
    return record


def _amp_export(*, order_text: str | None = "run the checklist") -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if order_text is not None:
        messages.append(
            {
                "role": "user",
                "messageId": "m-1",
                "createdAt": "2026-09-01T00:00:01Z",
                "content": [{"type": "text", "text": order_text}],
            }
        )
    messages.append(
        {
            "role": "assistant",
            "messageId": "m-2",
            "createdAt": "2026-09-01T00:00:02Z",
            "content": [
                {"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"cmd": "ls"}},
                {"type": "text", "text": "Done."},
            ],
        }
    )
    messages.append(
        {
            "role": "user",
            "messageId": "m-3",
            "createdAt": "2026-09-01T00:00:03Z",
            "content": [{"type": "tool_result", "toolUseID": "tu-1", "run": {"status": "done"}}],
        }
    )
    return {
        "id": "T-9abc",
        "env": {"initial": {"platform": {"clientVersion": "0.0.1"}}},
        "messages": messages,
    }


def _completed(args: list[str], rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)


def _write_order(orders_dir: Path, order: DispatchOrder) -> Path:
    orders_dir.mkdir(parents=True, exist_ok=True)
    path = orders_dir / f"{order.order_id}.json"
    path.write_text(order.model_dump_json(), encoding="utf-8")
    return path


def _write_uri_binding(path: Path, *, session_ref: str, lane: str, ref: str, client: str = "amp") -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "chitra.transcript-bindings.v1",
                "bindings": [
                    {
                        "session_ref": session_ref,
                        "lane": lane,
                        "path": ref,
                        "client": client,
                        "client_version": "0.0.1",
                        "instance": "pytest",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


# -- registry ---------------------------------------------------------------


def test_registry_resolves_builtins_and_fails_closed() -> None:
    assert plug("claude-tmux") is plug("claude-tmux")
    assert set(plug_names()) == {"claude-tmux", "codex-tmux", "opencode-tmux", "amp-orb"}
    assert isinstance(plug_for_client(Client.CLAUDE), ClaudeTmuxPlug)
    assert isinstance(plug_for_client("amp"), AmpOrbPlug)
    with pytest.raises(HarnessUnavailable, match="not built"):
        plug("dsh")
    with pytest.raises(HarnessUnavailable, match="no lane plug"):
        plug("prime-x")
    with pytest.raises(HarnessUnavailable, match="no lane plug serves"):
        plug_for_client("unreal")


def test_make_normalizer_fails_closed_on_unknown_client() -> None:
    """An unrecognized harness must not silently normalize as Codex."""
    context = NormalizationContext(instance="i", lane="lane-a", client="amp", client_version="1.0")
    with pytest.raises(ValueError, match="no JSONL transcript normalizer"):
        make_normalizer(context)
    codex_context = NormalizationContext(instance="i", lane="l", client=Client.CODEX, client_version="1.0")
    assert type(make_normalizer(codex_context)).__name__ == "CodexNormalizer"


# -- transcript bindings ----------------------------------------------------


def test_uri_binding_keeps_locator_and_widened_client(tmp_path: Path) -> None:
    binding = _binding()
    assert binding.client == "amp"
    assert not isinstance(binding.client, Client)
    assert binding.is_uri_locator
    ref = binding.resolved_ref(manifest_path=tmp_path / "manifest.json", transcript_root=tmp_path)
    assert ref == "amp-orb:ops-1"

    file_binding = _binding(path="session.jsonl", client="claude")
    assert file_binding.client is Client.CLAUDE
    assert not file_binding.is_uri_locator
    resolved = file_binding.resolved_ref(manifest_path=tmp_path / "manifest.json", transcript_root=tmp_path)
    assert isinstance(resolved, Path) and resolved.is_absolute()

    manifest = _write_uri_binding(tmp_path / "bindings.json", session_ref="host:lane-a:0.0", lane="lane-a", ref="amp-orb:ops-1")
    (loaded,) = load_transcript_bindings(manifest, transcript_root=tmp_path)
    assert loaded.is_uri_locator


# -- tmux plug ---------------------------------------------------------------


def _tmux_handle() -> LaneHandle:
    return LaneHandle(
        plug="claude-tmux",
        lane_id="lane-a",
        session_ref="localhost:lane-a:0.0",
        started_at="2026-09-01T00:00:00Z",
        locator={"tmux_session": "lane-a", "tmux_socket": "/tmp/sock", "host": "localhost"},
    )


def test_tmux_plug_send_delegates_and_receipt_proves(monkeypatch: pytest.MonkeyPatch) -> None:
    plug = ClaudeTmuxPlug()
    handle = _tmux_handle()
    order = DispatchOrder(order_id="ord-1", session_ref="localhost:lane-a:0.0", nudge="continue the work", task_type="code-review")
    captured: dict[str, DispatchOrder] = {}

    def fake_dispatch(dispatch_order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        captured["order"] = dispatch_order
        return DispatchResult(
            order_id=dispatch_order.order_id,
            session_ref=dispatch_order.session_ref,
            status=DispatchStatus.SENT,
            reason="sent: transcript confirmed",
            transcript_path="/tmp/t.jsonl",
            native_session_id="sess-1",
        )

    monkeypatch.setattr("chitra.dispatch.dispatch_to_tmux", fake_dispatch)
    wire = WireOrder.from_order(order)
    receipt = plug.send(handle, wire)
    assert receipt.delivery == "pasted"
    # The full order rides through byte-identical — no field attrition.
    assert captured["order"].task_type == "code-review"
    proof = plug.prove_read(handle, wire, receipt)
    assert proof.level == "consumed"
    assert proof.native_session_id == "sess-1"


def test_tmux_plug_send_rejection_maps_to_contract_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from chitra.adapter.contract import SendRejected

    plug = ClaudeTmuxPlug()
    monkeypatch.setattr(
        "chitra.dispatch.dispatch_to_tmux",
        lambda order, **kwargs: DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            status=DispatchStatus.BLOCKED,
            reason="directive-voice",
        ),
    )
    with pytest.raises(SendRejected) as exc:
        plug.send(_tmux_handle(), WireOrder(order_id="ord-2", text="the operator wants this"))
    assert exc.value.status is DispatchStatus.BLOCKED


def test_opencode_plug_caps_read_proof_at_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    plug = OpenCodeTmuxPlug()
    assert plug.capabilities.read_level == "accepted"
    handle = _tmux_handle()
    order = WireOrder(order_id="ord-3", text="do it")
    monkeypatch.setattr(
        "chitra.dispatch.dispatch_to_tmux",
        lambda o, **k: DispatchResult(
            order_id=o.order_id,
            session_ref=o.session_ref,
            status=DispatchStatus.SENT,
            reason="sent",
            transcript_path="/tmp/t.jsonl",
        ),
    )
    receipt = plug.send(handle, order)
    proof = plug.prove_read(handle, order, receipt)
    assert proof.level == "accepted"
    with pytest.raises(StreamUnavailable):
        plug.events(handle)


def test_tmux_plug_events_normalize_claude_jsonl(tmp_path: Path) -> None:
    # The plug's client ClassVar must reach make_normalizer as a Client enum —
    # a plain string makes every events() call raise, and the failure must
    # surface as StreamUnavailable, not the raw ValueError.
    transcript = tmp_path / "lane-a.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s-1",
                "cwd": str(tmp_path),
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )
    handle = LaneHandle(
        plug="claude-tmux",
        lane_id="lane-a",
        session_ref="localhost:lane-a:0.0",
        started_at="2026-09-01T00:00:00Z",
        locator={"tmux_session": "lane-a", "tmux_socket": "/tmp/sock", "host": "localhost"},
        binding_ref=str(transcript),
    )
    batch = ClaudeTmuxPlug().events(handle)
    assert any(event.payload.get("text") == "done" for event in batch.events)

    class NoNormalizerPlug(ClaudeTmuxPlug):
        client = "unknown-harness"  # type: ignore[assignment]

    with pytest.raises(StreamUnavailable):
        NoNormalizerPlug().events(handle)


# -- amp orb plug ------------------------------------------------------------


def test_amp_export_events_cover_user_tools_and_join() -> None:
    events = amp_events_from_export(
        _amp_export(),
        instance="inst",
        lane="lane-a",
        client_version="0.0.1",
        goal_ref="host:lane-a:0.0",
        binding_ref="amp-orb:ops-1",
    )
    kinds = [(event.native_type, event.normalized_type) for event in events]
    assert ("user", CanonicalType.UNKNOWN) in kinds
    assert ("tool_use", CanonicalType.TOOL_CALL) in kinds
    assert ("tool_result", CanonicalType.TOOL_RESULT) in kinds
    assert ("assistant", CanonicalType.FINAL_RESPONSE) in kinds
    tool_call = next(e for e in events if e.normalized_type is CanonicalType.TOOL_CALL)
    tool_result = next(e for e in events if e.normalized_type is CanonicalType.TOOL_RESULT)
    assert tool_call.payload["input"] == {"cmd": "ls"}
    assert tool_call.native_join_id == "tu-1" == tool_result.native_join_id
    assert tool_result.payload["call_id"] == "tu-1"
    assert all(event.client == "amp" and event.session_id == "T-9abc" for event in events)
    # Replay stability: identical export → identical event ids (journal dedupe).
    again = amp_events_from_export(
        _amp_export(), instance="inst", lane="lane-a", client_version="0.0.1",
        goal_ref="host:lane-a:0.0", binding_ref="amp-orb:ops-1",
    )
    assert [e.event_id for e in again] == [e.event_id for e in events]


def _orb_plug(tmp_path: Path, runner: Any) -> AmpOrbPlug:
    return AmpOrbPlug(runner=runner, orb_lane_bin="orb-lane", amp_bin="amp", state_root=tmp_path)


def _orb_handle(plug: AmpOrbPlug, tmp_path: Path) -> LaneHandle:
    binding = _binding()
    bound = BoundTranscript(binding=binding, path="amp-orb:ops-1")
    return plug.handle_for_binding(bound, session_ref=binding.session_ref)


def test_amp_orb_send_prove_and_cursor(tmp_path: Path) -> None:
    state_root = tmp_path
    (state_root / "ops-1.json").write_text(json.dumps(_amp_record("ops-1")), encoding="utf-8")
    export = _amp_export()
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[1] == "steer":
            return _completed(argv, stdout="delivered\n")
        if argv[1] == "status":
            return _completed(argv, stdout=json.dumps(_amp_record("ops-1")))
        if argv[1:3] == ["threads", "export"]:
            return _completed(argv, stdout=json.dumps(export))
        return _completed(argv, stderr="unexpected argv")

    plug = _orb_plug(state_root, runner)
    handle = _orb_handle(plug, state_root)
    assert handle.native_session_id == "T-9abc"
    assert handle.binding_ref == "amp-orb:ops-1"

    wire = WireOrder(order_id="ord-4", text="run the checklist")
    receipt = plug.send(handle, wire)
    assert receipt.ack == "delivered"
    assert calls[-1][:3] == ["orb-lane", "steer", "ops-1"]

    proof = plug.prove_read(handle, wire, receipt)
    assert proof.level == "consumed"
    assert proof.native_session_id == "T-9abc"

    batch = plug.events(handle)
    assert batch.cursor == "3"
    second = plug.events(handle, cursor=batch.cursor)
    assert second.events == ()

    status = plug.status(handle)
    assert status.state == "running"


def test_amp_orb_swallowed_steer_and_dead_record(tmp_path: Path) -> None:
    from chitra.adapter.contract import SendRejected

    (tmp_path / "ops-1.json").write_text(json.dumps(_amp_record("ops-1")), encoding="utf-8")
    (tmp_path / "gone.json").write_text(json.dumps(_amp_record("gone", state="stop_requested")), encoding="utf-8")

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1] == "steer":
            return _completed(argv, rc=3, stdout="swallowed\n")
        return _completed(argv)

    plug = _orb_plug(tmp_path, runner)
    handle = _orb_handle(plug, tmp_path)
    wire = WireOrder(order_id="ord-5", text="run the checklist")
    with pytest.raises(SendRejected, match="swallowed"):
        plug.send(handle, wire)

    dead = BoundTranscript(binding=_binding(path="amp-orb:gone"), path="amp-orb:gone")
    with pytest.raises(LaneDead):
        plug.handle_for_binding(dead, session_ref="host:gone:0.0")


# -- canonical read proof ----------------------------------------------------


def test_prove_read_from_events_levels() -> None:
    events = amp_events_from_export(
        _amp_export(order_text="run the checklist"),
        instance="i", lane="lane-a", client_version="0.0.1",
        goal_ref="s", binding_ref="amp-orb:ops-1",
    )
    consumed = prove_read_from_events(order_id="o", order_text="run the checklist", events=events, native_session_id="T-9abc")
    assert consumed.level == "consumed"
    assert consumed.input_event_id is not None and consumed.consuming_event_id is not None

    # Same text in another session's stream must not prove this read.
    foreign = prove_read_from_events(order_id="o", order_text="run the checklist", events=events, native_session_id="T-other")
    assert foreign.level == "none"

    # Order text present but no later activity: accepted, never consumed.
    quiet = amp_events_from_export(
        {**_amp_export(), "messages": _amp_export()["messages"][:1]},
        instance="i", lane="lane-a", client_version="0.0.1", goal_ref="s", binding_ref="amp-orb:ops-1",
    )
    accepted = prove_read_from_events(order_id="o", order_text="run the checklist", events=quiet)
    assert accepted.level == "accepted"

    # Harness ack with nothing on the stream: accepted, not none.
    acked = prove_read_from_events(order_id="o", order_text="missing", events=(), acked=True)
    assert acked.level == "accepted"


# -- dispatchd URI routing ---------------------------------------------------


class _FakeUriPlug:
    """A plug standing in for amp-orb in dispatchd routing tests."""

    name = "fake-uri"
    client = "amp"
    capabilities = Capabilities(delivery="steer", read_level="consumed", graceful_stop=True)

    def __init__(self, *, proof_level: str = "consumed") -> None:
        self.proof_level = proof_level
        self.sent: list[str] = []
        self.proved: list[str] = []

    def handle_for_binding(self, bound: BoundTranscript, *, session_ref: str) -> LaneHandle:
        return LaneHandle(
            plug=self.name,
            lane_id=bound.lane,
            session_ref=session_ref,
            started_at="2026-09-01T00:00:00Z",
            binding_ref=str(bound.path),
            native_session_id="T-9abc",
        )

    def send(self, handle: LaneHandle, order: WireOrder) -> SendReceipt:
        self.sent.append(order.text)
        return SendReceipt(order_id=order.order_id, sent_at="2026-09-01T00:00:01Z", delivery="steered", ack="delivered")

    def prove_read(
        self, handle: LaneHandle, order: WireOrder, receipt: SendReceipt | None = None, *, deadline_s: float = 60.0
    ) -> ReadProof:
        self.proved.append(order.order_id)
        return ReadProof(
            order_id=order.order_id,
            level=self.proof_level,  # type: ignore[arg-type]
            detail=f"fake proof {self.proof_level}",
            native_session_id="T-9abc",
            binding_ref=handle.binding_ref,
        )

    def events(self, handle: LaneHandle, cursor: str | None = None) -> EventBatch:
        return EventBatch(events=())

    def status(self, handle: LaneHandle) -> LaneStatus:
        return LaneStatus(state="running")

    def stop(self, handle: LaneHandle) -> StopResult:
        return StopResult(was_running=True, stopped=True, method="fake")


def test_dispatchd_routes_uri_bound_order_through_plug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_ref = "orb:ops-1:0.0"
    bindings = _write_uri_binding(tmp_path / "bindings.json", session_ref=session_ref, lane="lane-a", ref="amp-orb:ops-1")
    fake = _FakeUriPlug(proof_level="consumed")
    monkeypatch.setattr(dispatchd_mod, "plug_for_client", lambda client: fake)
    monkeypatch.setattr(
        dispatchd_mod,
        "dispatch_to_tmux",
        lambda order, **kwargs: (_ for _ in ()).throw(AssertionError("URI-bound orders never touch tmux")),
    )
    queue_dir = tmp_path / "queue"
    _write_order(queue_dir / "orders", DispatchOrder(order_id="ord-uri", session_ref=session_ref, nudge="continue the tracked work"))

    (result,) = dispatchd_mod.run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        transcript_bindings_path=bindings,
    )

    assert result.status is DispatchStatus.SENT
    assert result.transcript_path == "amp-orb:ops-1"
    assert result.native_session_id == "T-9abc"
    assert fake.sent == ["continue the tracked work"]
    assert (tmp_path / "ledger.jsonl").exists()


def test_dispatchd_uri_bound_order_unconfirmed_when_ack_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plug that proves only acceptance lands DELIVERY_UNCONFIRMED — never SENT."""
    session_ref = "orb:ops-1:0.0"
    bindings = _write_uri_binding(tmp_path / "bindings.json", session_ref=session_ref, lane="lane-a", ref="amp-orb:ops-1")
    fake = _FakeUriPlug(proof_level="accepted")
    monkeypatch.setattr(dispatchd_mod, "plug_for_client", lambda client: fake)
    queue_dir = tmp_path / "queue"
    _write_order(queue_dir / "orders", DispatchOrder(order_id="ord-uri-2", session_ref=session_ref, nudge="continue the tracked work"))

    results = dispatchd_mod.run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        transcript_bindings_path=bindings,
    )

    assert results[0].status is DispatchStatus.DELIVERY_UNCONFIRMED
    assert not (tmp_path / "ledger.jsonl").exists()


# -- monitord URI ingest -----------------------------------------------------


def test_monitord_ingests_uri_binding_through_plug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = monitord_mod.MonitordConfig(
        state_dir=tmp_path / "state",
        transcript_root=tmp_path,
        findings_path=tmp_path / "findings.jsonl",
        poll_seconds=1.0,
        shadow_mode=True,
        transcript_bindings_path=tmp_path / "bindings.json",
    )
    binding = _binding()
    export_events = amp_events_from_export(
        _amp_export(),
        instance=binding.instance,
        lane=binding.lane,
        client_version=binding.client_version,
        goal_ref=binding.session_ref,
        binding_ref=str(binding.path),
    )

    class _EventsPlug(_FakeUriPlug):
        def events(self, handle: LaneHandle, cursor: str | None = None) -> EventBatch:
            return EventBatch(events=export_events, cursor="3")

    monkeypatch.setattr(monitord_mod, "plug_for_client", lambda client: _EventsPlug())
    observed = monitord_mod.ingest_transcript_bindings(config, (binding,))

    assert len(observed) == len(export_events)
    journaled = EventJournal(config.state_dir, binding.lane).load()
    assert [e.event_id for e in journaled] == [e.event_id for e in export_events]


def test_monitord_uri_binding_failure_is_per_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = monitord_mod.MonitordConfig(
        state_dir=tmp_path / "state",
        transcript_root=tmp_path,
        findings_path=tmp_path / "findings.jsonl",
        poll_seconds=1.0,
        shadow_mode=True,
        transcript_bindings_path=tmp_path / "bindings.json",
    )
    binding = _binding()

    def broken(client: object) -> Any:
        raise HarnessUnavailable("no amp on this host")

    monkeypatch.setattr(monitord_mod, "plug_for_client", broken)
    assert monitord_mod.ingest_transcript_bindings(config, (binding,)) == ()

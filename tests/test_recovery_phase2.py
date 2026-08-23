"""Canonical recovery sequence, restart, and governed-checkpoint tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitra.detect.ladder import ConsumptionProof, IncidentRecord, IncidentStore
from chitra.detect.rescue import (
    RecoveryCheckpointBinding,
    RescueBundle,
    write_checkpoint_receipt,
    write_rescue_bundle,
)
from chitra.goals import GoalRecord
from chitra.joined_lane import journal_provider_probe
from chitra.journal.models import CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.journal.store import EventJournal
from chitra.ledger import LedgerEntry
from chitra.provider_protocol import (
    CheckpointRequest,
    CreateOrResumeRequest,
    ProviderName,
    ProviderState,
    ProviderStatus,
    ProviderUpdate,
    ReadUpdatesResult,
    SendRequest,
    UpdateKind,
)
from chitra.recovery import RecoveryEngine, RecoveryStateStore
from chitra.session_contract import (
    JoinedLaneRecord,
    LaneUpdate,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    RoadmapStep,
)

NOW = datetime(2026, 8, 23, 14, tzinfo=UTC)


def _goal() -> GoalRecord:
    return GoalRecord(
        session_ref="tophand:lane-a:1",
        lane_id="lane-a",
        goal_id="goal-a",
        goal="Ship the enrolled change",
        done_when="The verified acceptance check passes",
        source="test",
        status="working",
    )


def _record() -> JoinedLaneRecord:
    update = LaneUpdate(
        lane_id="lane-a",
        goal_id="goal-a",
        session_ref="tophand:lane-a:1",
        goal_version=1,
        sequence=1,
        observed_at=NOW.isoformat(),
        plan_version=1,
        steps=(RoadmapStep(id="implement", status="active", owner="lane-manager"),),
        current_action="Implement the enrolled change",
        next_action="Run the focused acceptance check",
    )
    return JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-a",
        goal_version=1,
        session_ref="tophand:lane-a:1",
        physical_session_generation=1,
        provider=ProviderIdentity(
            kind="tophand",
            handle="tophand-lane-a",
            instance_id="instance-a",
            generation=1,
            capabilities=ProviderCapabilities.from_supported(
                ("send", "checkpoint", "create_or_resume", "read_updates", "status")
            ),
        ),
        current_update=update,
    )


def _result(request: SendRequest | CheckpointRequest | CreateOrResumeRequest, status: str = "consumed") -> ProviderOperationResult:
    consumed = status == "consumed"
    return ProviderOperationResult(
        operation_id=request.operation_id,
        kind=request.operation.kind,
        lane_id=request.lane_id,
        provider_handle=request.provider_handle,
        idempotency_key=request.idempotency_key,
        payload_digest=request.payload_digest,
        provider_instance_id=request.provider_instance_id,
        provider_generation=request.provider_generation,
        status=status,  # type: ignore[arg-type]
        accepted=True,
        consumed=consumed,
        observed_at=request.operation.created_at,
        evidence=f"typed-{status}",
    )


class SequenceProvider:
    provider_name = ProviderName.TOPHAND
    capabilities = ProviderCapabilities.from_supported(
        ("send", "checkpoint", "create_or_resume", "read_updates", "status")
    )

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.calls: list[str] = []
        self.session_ref = "tophand:lane-a:1"

    def send(self, request: SendRequest) -> ProviderOperationResult:
        self.calls.append("diagnostic" if "bounded diagnostic" in request.text else "send")
        return _result(request)

    def checkpoint(self, request: CheckpointRequest) -> ProviderOperationResult:
        self.calls.append("checkpoint")
        record = RecoveryStateStore(self.root, request.lane_id).load()
        assert record is not None
        cycle_id, sequence = record.recovery.cycle_id, record.recovery.event_sequence
        assert cycle_id is not None and sequence is not None
        binding = RecoveryCheckpointBinding(
            lane_id=record.lane_id,
            goal_id=record.goal_id,
            session_ref=record.session_ref,
            cycle_id=cycle_id,
            operation_id=request.operation_id,
            provider_handle=request.provider_handle,
            provider_session_id=request.operation.provider_session_id,
            provider_instance_id=request.provider_instance_id,
            provider_generation=request.provider_generation,
            idempotency_key=request.idempotency_key,
            payload_digest=request.payload_digest,
            event_sequence=sequence,
        )
        self._govern_checkpoint(record, binding)
        return _result(request)

    def _govern_checkpoint(self, lane: JoinedLaneRecord, binding: RecoveryCheckpointBinding) -> None:
        process = {
            "target_pid": 321,
            "target_uid": 501,
            "target_gid": 20,
            "target_start_time": "123",
            "target_comm": "agent",
            "target_exe": "/agent",
        }
        self.monkeypatch.setattr("chitra.detect.rescue._observe_process_identity", lambda _pid: process)
        transcript = self.root / "lane-a.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        consumption = ConsumptionProof(
            ledger_entry=LedgerEntry(
                order_id="rescue-order",
                session_ref=lane.session_ref,
                tag="[C]",
                sig_v=4,
                message_hash="a" * 64,
                sent_at=NOW.isoformat(),
                signature="signed-rescue-order",
            ),
            session_ref=lane.session_ref,
            native_session_id=lane.session_ref,
            user_event_id="rescue-user",
            turn_event_id="rescue-turn",
        )
        incident = IncidentRecord(
            lane=lane.lane_id,
            fingerprint="b" * 64,
            detector="recovery",
            stage="rescue",
            order_marker="rescue-marker",
            opened_at=NOW.isoformat(),
            event_refs=("stall-event",),
            unmet_item="acceptance",
            expected_next_progress="checkpoint",
            detail="stalled",
            consumption=consumption,
        )
        store = IncidentStore(self.root, lane.lane_id)
        store._append(incident)
        bundle = RescueBundle(
            lane=lane.lane_id,
            session_ref=lane.session_ref,
            captured_at=NOW.isoformat(),
            transcript_ref=str(transcript),
            transcript_sha256=hashlib.sha256(transcript.read_bytes()).hexdigest(),
            process_identity={
                **process,
                "capture_pid": 111,
                "capture_ppid": 110,
                "session_ref": lane.session_ref,
            },
            pane_capture="",
            git_state={"branch": "test", "head": "c" * 40},
            untracked_files=(),
            receipt_paths=(),
            contract="preserve goal-a",
            incident_history=(json.dumps({"fingerprint": incident.fingerprint}),),
            open_asks=(),
            checkpoint_requested=True,
            recovery_binding=binding,
        )
        bundle = bundle.model_copy(update={"bundle_sha256": bundle.compute_digest()})
        write_rescue_bundle(bundle, self.root)
        write_checkpoint_receipt(bundle=bundle, record=incident, state_root=self.root, checkpoint_ref="checkpoint-a")
        store.seal_rescue_checkpoint(
            fingerprint=incident.fingerprint,
            order_marker=incident.order_marker,
            bundle_sha256=bundle.bundle_sha256,
            checkpoint_ref="checkpoint-a",
        )

    def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
        self.calls.append("relaunch")
        self.session_ref = "tophand:lane-a:2"
        return _result(request)

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            provider=self.provider_name,
            state=ProviderState.IDLE,
            provider_session_id=self.session_ref,
            generation=2 if self.session_ref.endswith(":2") else 1,
            fresh=True,
            provider_available=True,
        )

    def read_updates(self, cursor: str | None = None) -> ReadUpdatesResult:
        return ReadUpdatesResult(cursor, cursor or "0")


def test_recovery_follows_exact_bounded_sequence_and_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SequenceProvider(tmp_path, monkeypatch)
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0), wait_interval=timedelta(0))
    record = _record()
    for offset in range(5):
        decision = engine.run_once(
            record,
            now=NOW + timedelta(seconds=offset),
            failure_signature="stall-1",
            goal=_goal(),
        )
        assert not decision.asks_user
        record = decision.record

    assert provider.calls == ["send", "send", "checkpoint", "relaunch", "diagnostic"]
    assert record.recovery.stage == "waiting"
    assert record.next_check is not None and record.next_check.wake_condition
    assert record.goal_id == "goal-a"
    assert record.current_update is not None
    assert record.current_update.current_action == "Implement the enrolled change"
    assert record.session_ref == "tophand:lane-a:2"
    assert len(record.operation_history) == 5


class AcceptedThenObservedProvider:
    provider_name = ProviderName.TOPHAND
    capabilities = ProviderCapabilities.from_supported(("send", "read_updates", "status"))

    def __init__(self, pending: ProviderOperationResult | None = None) -> None:
        self.sent = 0
        self.pending = pending

    def send(self, request: SendRequest) -> ProviderOperationResult:
        self.sent += 1
        return _result(request, "accepted")

    def read_updates(self, cursor: str | None = None) -> ReadUpdatesResult:
        if self.pending is None:
            return ReadUpdatesResult(cursor, cursor or "0")
        result = self.pending
        update = ProviderUpdate(
            event_id="consumed-after-restart",
            cursor="1",
            kind=UpdateKind.STEER_CONSUMED,
            provider_session_id="tophand:lane-a:1",
            observed_at=(NOW + timedelta(minutes=1)).isoformat(),
            operation_id=result.operation_id,
            lane_id=result.lane_id,
            idempotency_key=result.idempotency_key,
            payload_digest=result.payload_digest,
            provider_instance_id=result.provider_instance_id or "",
            provider_generation=result.provider_generation or 1,
            provider_handle=result.provider_handle,
        )
        return ReadUpdatesResult(cursor, "1", (update,))


def test_restart_reuses_pending_operation_and_reconciles_normal_provider_contract(tmp_path: Path) -> None:
    first_provider = AcceptedThenObservedProvider()
    first = RecoveryEngine(provider=first_provider, state_root=tmp_path, check_interval=timedelta(0)).run_once(
        _record(), now=NOW, failure_signature="restart-stall", goal=_goal()
    )
    assert first.record.pending_operation is not None
    operation_id = first.record.pending_operation.operation_id
    assert first.record.last_operation_result is not None
    pending = first.record.pending_operation
    EventJournal(tmp_path, "lane-a").append(
        (
            CanonicalEvent(
                event_id="consumed-after-restart",
                instance="test",
                lane="lane-a",
                client=Client.CODEX,
                client_version="test",
                process_id="1",
                transcript=TranscriptIdentity(path="/tmp/lane-a", device=1, inode=1),
                session_id="tophand:lane-a:1",
                resume_id=None,
                observed_at=(NOW + timedelta(minutes=1)).isoformat(),
                native_time=(NOW + timedelta(minutes=1)).isoformat(),
                native_type="provider_update",
                native_join_id=None,
                raw_byte_range=None,
                raw_sha256=None,
                normalized_type=CanonicalType.UNKNOWN,
                goal_ref="goal-a",
                item_ref=None,
                payload_digest=pending.payload_digest,
                normalizer_version="test",
                payload={
                    "operation_id": pending.operation_id,
                    "lane_id": pending.lane_id,
                    "goal_id": "goal-a",
                    "session_ref": "tophand:lane-a:1",
                    "provider_handle": pending.provider_handle,
                    "provider_instance_id": pending.provider_instance_id,
                    "provider_generation": pending.provider_generation,
                    "idempotency_key": pending.idempotency_key,
                    "payload_digest": pending.payload_digest,
                    "event_sequence": first.record.recovery.event_sequence,
                    "result_evidence": {"consumed": True},
                },
                raw_record=None,
            ),
        )
    )
    assert journal_provider_probe(tmp_path)(first.record) is not None
    second_provider = AcceptedThenObservedProvider(first.record.last_operation_result)
    ownership = {
        "authoritative": True,
        "status": "authoritative",
        "provider_instance_id": "instance-a",
        "session_ref": "tophand:lane-a:1",
        "lane_id": "lane-a",
        "lane_generation": 1,
        "ownership_generation": 1,
    }
    second = RecoveryEngine(
        provider=second_provider,
        state_root=tmp_path,
        check_interval=timedelta(0),
        ownership_probe=lambda _record: ownership,
    ).run_once(
        first.record, now=NOW + timedelta(minutes=1), goal=_goal()
    )
    assert second.operation is not None and second.operation.operation_id == operation_id
    assert second_provider.sent == 0
    assert second.record.pending_operation is None
    assert second.record.recovery.stage == "correct"


def test_corrupt_newest_joined_record_loads_previous_valid_snapshot(tmp_path: Path) -> None:
    store = RecoveryStateStore(tmp_path, "lane-a")
    first = store.save(_record())
    second = store.save(first.model_copy(update={"repository_commit": "abc123"}))
    assert second.revision > first.revision
    store.path.write_text("{not-json", encoding="utf-8")
    assert store.load() == first


def test_exact_bound_material_progress_clears_recovery(tmp_path: Path) -> None:
    transcript = TranscriptIdentity(path="/tmp/lane-a", device=1, inode=1)
    event = CanonicalEvent(
        event_id="progress-event-1",
        instance="test",
        lane="lane-a",
        client=Client.CODEX,
        client_version="test",
        process_id="1",
        transcript=transcript,
        session_id="tophand:lane-a:1",
        resume_id=None,
        observed_at=NOW.isoformat(),
        native_time=NOW.isoformat(),
        native_type="tool_result",
        native_join_id="tool-1",
        raw_byte_range=None,
        raw_sha256=None,
        normalized_type=CanonicalType.TOOL_RESULT,
        goal_ref="goal-a",
        item_ref="implement",
        payload_digest="d" * 64,
        normalizer_version="test",
        payload={"progress_evidence": {"artifact_changed": True}, "summary": "artifact changed"},
        raw_record=None,
    )
    decision = RecoveryEngine(state_root=tmp_path).run_once(
        _record(), now=NOW, failure_signature="progress-stall", goal=_goal(), events=(event,)
    )
    assert decision.action == "progress_confirmed"
    assert decision.record.recovery.stage == "complete"
    assert decision.record.last_useful_progress is not None

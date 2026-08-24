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
    find_recovery_checkpoint_receipt,
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
    NextCheck,
    PlanAssessment,
    Problem,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    ProgressEvidence,
    RecoveryState,
    RoadmapStep,
)
from test_recovery_contract_regressions import append_named_wake

NOW = datetime(2026, 8, 23, 14, tzinfo=UTC)


def _document_digest(document: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
            provider_session_id="tophand:lane-a:1",
            process_start_token="boot-a:77",
            instance_id="instance-a",
            generation=1,
            capabilities=ProviderCapabilities.from_supported(
                ("send", "checkpoint", "create_or_resume", "read_updates", "status")
            ),
        ),
        current_update=update,
    )


def _record_with_resume_state() -> JoinedLaneRecord:
    base = _record()
    assert base.current_update is not None
    return base.model_copy(
        update={
            "update_cursor": "transcript-cursor-17",
            "worktree_path": "/worktrees/lane-a",
            "repository_commit": "a" * 40,
            "preserved_work_manifest": ("tracked.txt", "scratch.txt"),
            "current_update": base.current_update.model_copy(
                update={
                    "problems": (
                        Problem(
                            id="blocked-access",
                            summary="Access is unavailable",
                            owner="lane-manager",
                            state="open",
                        ),
                    )
                }
            ),
        }
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
        provider_session_id=request.operation.provider_session_id,
        process_start_token=request.operation.process_start_token,
        provider_instance_id=request.provider_instance_id,
        provider_generation=request.provider_generation,
        provider_pid=4242,
        owner_pid=4242,
        observed_process={
            "pid": 4242,
            "boot_id": "boot-a",
            "start_ticks": 77,
            "process_start_token": request.operation.process_start_token,
        },
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
        self.relaunch_count = 0

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
            goal_version=record.goal_version,
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
        checkpoint_ref = "checkpoint-compressed" if "compress" in binding.operation_id else "checkpoint-a"
        write_checkpoint_receipt(bundle=bundle, record=incident, state_root=self.root, checkpoint_ref=checkpoint_ref)
        store.seal_rescue_checkpoint(
            fingerprint=incident.fingerprint,
            order_marker=incident.order_marker,
            bundle_sha256=bundle.bundle_sha256,
            checkpoint_ref=checkpoint_ref,
        )

    def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
        self.calls.append("relaunch")
        self.relaunch_count += 1
        self.session_ref = f"tophand:lane-a:{self.relaunch_count + 1}"
        return _result(request)

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            provider=self.provider_name,
            state=ProviderState.IDLE,
            provider_session_id=self.session_ref,
            generation=self.relaunch_count + 1,
            fresh=True,
            provider_available=True,
            provider_instance_id="instance-a",
            process_start_token=f"boot-a:{77 + self.relaunch_count}",
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


def test_repeated_stall_reframes_without_changing_enrolled_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = SequenceProvider(tmp_path, monkeypatch)
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0), wait_interval=timedelta(0))
    goal = _goal()
    record = _record_with_resume_state()
    immutable_before = (goal.goal_id, goal.lane_id, goal.goal_version, goal.goal, goal.done_when, goal.scope)
    for offset in range(5):
        record = engine.run_once(
            record, now=NOW + timedelta(seconds=offset), failure_signature="repeated-stall", goal=goal
        ).record

    decision = engine.run_once(record, now=NOW + timedelta(seconds=5), goal=goal)
    assert decision.action == "reframe"
    assert not decision.asks_user
    assert decision.record.recovery.stage == "relaunch"
    assert decision.record.recovery.execution_objective.startswith("Unblock the enrolled lane")
    assert len(decision.record.recovery.execution_plan) == 3
    assert (goal.goal_id, goal.lane_id, goal.goal_version, goal.goal, goal.done_when, goal.scope) == immutable_before


def test_context_handoff_survives_restart_and_binds_all_continuity_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = SequenceProvider(tmp_path, monkeypatch)
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0), wait_interval=timedelta(0))
    goal = _goal()
    record = _record_with_resume_state()
    for offset in range(6):
        record = engine.run_once(
            record, now=NOW + timedelta(seconds=offset), failure_signature="compress-stall", goal=goal
        ).record
    before = RecoveryStateStore(tmp_path, "lane-a").load()
    assert before is not None and before.recovery.execution_plan

    compressed = RecoveryEngine(
        provider=provider, state_root=tmp_path, check_interval=timedelta(0), wait_interval=timedelta(0)
    ).run_once(before, now=NOW + timedelta(seconds=6), goal=goal)
    assert compressed.action == "compress"
    assert not compressed.asks_user
    record = compressed.record
    assert record.recovery.handoff_id is not None
    path = tmp_path / record.recovery.handoff_reference
    handoff = json.loads(path.read_text(encoding="utf-8"))
    assert handoff["handoff_id"] == record.recovery.handoff_id
    assert handoff["session_ref"] == before.session_ref
    assert handoff["immutable_goal"]["source"] == goal.source
    assert handoff["immutable_goal"]["enrolled_at"] == goal.enrolled_at
    assert handoff["physical_session_generation"] == before.physical_session_generation
    assert handoff["roadmap_snapshot"] == before.current_update.to_dict()
    assert handoff["roadmap_digest"] == _document_digest(handoff["roadmap_snapshot"])
    assert handoff["provider_identity"] == before.provider.to_dict()
    assert handoff["provider_digest"] == _document_digest(handoff["provider_identity"])
    assert handoff["checkpoint_reference"] == before.checkpoint_reference
    assert handoff["pending_operation"] is None
    assert handoff["next_check"] == before.next_check.to_dict()
    assert handoff["wake_receipts"] == [item.to_dict() for item in before.wake_receipts]
    assert handoff["plan_assessment"] == before.plan_assessment.to_dict()
    assert handoff["last_useful_progress"] is None
    assert handoff["recovery_state"]["cycle_id"] == before.recovery.cycle_id

    restarted = RecoveryStateStore(tmp_path, "lane-a").load()
    assert restarted is not None
    assert RecoveryEngine(state_root=tmp_path)._handoff_valid(restarted, goal)


def test_compressed_handoff_relaunches_from_the_same_restartable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = SequenceProvider(tmp_path, monkeypatch)
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0), wait_interval=timedelta(0))
    goal = _goal()
    record = _record_with_resume_state()
    for offset in range(6):
        record = engine.run_once(
            record, now=NOW + timedelta(seconds=offset), failure_signature="restartable-compress", goal=goal
        ).record

    compressed = engine.run_once(record, now=NOW + timedelta(seconds=6), goal=goal)
    assert compressed.action == "compress"
    persisted = RecoveryStateStore(tmp_path, "lane-a").load()
    assert persisted is not None
    handoff_reference = persisted.recovery.handoff_reference
    restarted = RecoveryEngine(
        provider=provider, state_root=tmp_path, check_interval=timedelta(0), wait_interval=timedelta(0)
    ).run_once(persisted, now=NOW + timedelta(seconds=7), goal=goal)
    assert restarted.action == "relaunch"
    assert restarted.record.provider.provider_session_id == "tophand:lane-a:3"
    assert restarted.record.recovery.handoff_reference == handoff_reference


def test_recomputed_provider_roadmap_and_handoff_id_forgery_fails_closed(tmp_path: Path) -> None:
    record = _record_with_resume_state().model_copy(
        update={
            "recovery": RecoveryState(
                stage="relaunch",
                cycle_id="cycle-forged",
                failure_signature="forged-handoff",
                attempted_remedy="reframe",
                attempt_count=5,
                next_allowed_attempt=NOW.isoformat(),
                execution_objective="preserve the tactical objective",
                execution_plan=("preserve the tactical plan",),
            )
        }
    )
    store = RecoveryStateStore(tmp_path, "lane-a")
    stored = store.save(record)
    engine = RecoveryEngine(state_root=tmp_path)
    anchored = engine._write_context_handoff(stored, _goal(), NOW, True)
    assert engine._handoff_valid(anchored, _goal())

    path = tmp_path / anchored.recovery.handoff_reference
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["provider_identity"]["handle"] = "forged-provider"
    forged["provider_digest"] = _document_digest(forged["provider_identity"])
    forged["roadmap_snapshot"]["current_action"] = "forged roadmap"
    forged["roadmap_digest"] = _document_digest(forged["roadmap_snapshot"])
    forged["resume"]["provider"]["handle"] = "forged-provider"
    forged["resume"]["current_update"]["current_action"] = "forged roadmap"
    body = dict(forged)
    body.pop("handoff_sha256", None)
    forged["handoff_sha256"] = _document_digest(body)
    path.write_text(json.dumps(forged), encoding="utf-8")

    assert not engine._handoff_valid(anchored, _goal())
    blocked = engine.run_once(anchored, now=NOW + timedelta(seconds=1), goal=_goal())
    assert blocked.action == "waiting"
    assert blocked.wake_condition == "the durable context handoff is valid and untampered"

    wrong_anchor = anchored.model_copy(
        update={
            "recovery": anchored.recovery.model_copy(
                update={
                    "handoff_id": "other-cycle-context",
                    "handoff_reference": "recovery-handoffs/other-cycle-context.json",
                }
            )
        }
    )
    assert not engine._handoff_valid(wrong_anchor, _goal())


def test_schedule_preserves_tactical_state_and_handoff_across_restart(tmp_path: Path) -> None:
    record = _record_with_resume_state().model_copy(
        update={
            "recovery": RecoveryState(
                stage="relaunch",
                cycle_id="cycle-preserved",
                failure_signature="same-stall",
                attempted_remedy="reframe",
                attempt_count=5,
                event_sequence=7,
                next_allowed_attempt=NOW.isoformat(),
                pending_payload="the exact pending recovery payload",
                execution_objective="keep the enrolled objective",
                execution_plan=("keep the first tactic", "keep the second tactic"),
                handoff_id="lane-a-cycle-preserved-context",
                handoff_reference="recovery-handoffs/lane-a-cycle-preserved-context.json",
                handoff_digest="handoff-digest",
            ),
            "plan_assessment": PlanAssessment(
                state="valid", assessed_at=NOW.isoformat(), reason="the lane plan remains usable"
            ),
            "last_useful_progress": ProgressEvidence(
                update_sequence=6,
                summary="the prior proof remains the last material progress",
                observed_at=NOW.isoformat(),
                evidence_ref="progress-6",
            ),
            "next_check": NextCheck(
                at=(NOW + timedelta(minutes=5)).isoformat(),
                reason="preserve the next recovery check",
                wake_condition="the same logical lane resumes",
            ),
        }
    )
    store = RecoveryStateStore(tmp_path, "lane-a")
    stored = store.save(record)
    scheduled = RecoveryEngine(state_root=tmp_path).schedule(stored, "same-stall", now=NOW + timedelta(minutes=1))
    restarted = store.load()
    assert restarted is not None
    assert scheduled.recovery == restarted.recovery
    assert restarted.recovery.cycle_id == "cycle-preserved"
    assert restarted.recovery.event_sequence == 7
    assert restarted.recovery.pending_payload == "the exact pending recovery payload"
    assert restarted.recovery.execution_plan == ("keep the first tactic", "keep the second tactic")
    assert restarted.recovery.handoff_id == "lane-a-cycle-preserved-context"
    assert restarted.next_check is not None
    assert restarted.next_check.wake_condition == "the same logical lane resumes"
    assert restarted.plan_assessment.reason == "the lane plan remains usable"
    assert restarted.last_useful_progress is not None
    assert restarted.last_useful_progress.evidence_ref == "progress-6"


def test_named_wake_preserves_tactical_state_and_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    condition = "the same logical lane resumes"
    append_named_wake(tmp_path, "wake-preserve-1", condition)
    record = _record_with_resume_state().model_copy(
        update={
            "recovery": RecoveryState(
                stage="waiting",
                cycle_id="cycle-preserved",
                failure_signature="same-stall",
                attempted_remedy="reframe",
                attempt_count=5,
                event_sequence=7,
                next_allowed_attempt=NOW.isoformat(),
                pending_payload="the exact pending recovery payload",
                execution_objective="keep the enrolled objective",
                execution_plan=("keep the first tactic", "keep the second tactic"),
                handoff_id="lane-a-cycle-preserved-context",
                handoff_reference="recovery-handoffs/lane-a-cycle-preserved-context.json",
                handoff_digest="handoff-digest",
            ),
            "plan_assessment": PlanAssessment(
                state="valid", assessed_at=NOW.isoformat(), reason="the lane plan remains usable"
            ),
            "last_useful_progress": ProgressEvidence(
                update_sequence=6,
                summary="the prior proof remains the last material progress",
                observed_at=NOW.isoformat(),
                evidence_ref="progress-6",
            ),
            "next_check": NextCheck(
                at=(NOW + timedelta(hours=1)).isoformat(),
                reason="wait for the named wake",
                wake_condition=condition,
            ),
        }
    )
    store = RecoveryStateStore(tmp_path, "lane-a")
    stored = store.save(record)
    engine = RecoveryEngine(state_root=tmp_path, check_interval=timedelta(0), wait_interval=timedelta(0))
    monkeypatch.setattr(
        engine,
        "_send",
        lambda record, resolved_goal, current, action, facts, persist: engine._decision("waiting", record, "test seam"),
    )
    decision = engine.run_once(
        stored,
        now=NOW,
        wake_id="wake-preserve-1",
        observed_wake_condition=condition,
        wake_event_sequence=1,
        goal=_goal(),
    )
    restarted = store.load()
    assert restarted is not None
    assert decision.action == "waiting"
    assert restarted.recovery.cycle_id == "cycle-preserved"
    assert restarted.recovery.event_sequence == 7
    assert restarted.recovery.pending_payload == "the exact pending recovery payload"
    assert restarted.recovery.execution_objective == "keep the enrolled objective"
    assert restarted.recovery.handoff_id == "lane-a-cycle-preserved-context"
    assert [item.wake_id for item in restarted.wake_receipts] == ["wake-preserve-1"]
    assert restarted.next_check is not None and restarted.next_check.wake_condition == condition
    assert restarted.plan_assessment.reason == "the lane plan remains usable"
    assert restarted.last_useful_progress is not None


def test_checkpoint_proof_requires_the_current_goal_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SequenceProvider(tmp_path, monkeypatch)
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0), wait_interval=timedelta(0))
    record = _record()
    for offset in range(3):
        record = engine.run_once(
            record,
            now=NOW + timedelta(seconds=offset),
            failure_signature="checkpoint-version-stall",
            goal=_goal(),
        ).record

    checkpoint_ref = record.checkpoint_reference
    assert checkpoint_ref == "checkpoint-a"
    payload = json.loads((tmp_path / "checkpoints" / f"{checkpoint_ref}.json").read_text(encoding="utf-8"))
    assert payload["goal_version"] == 1
    binding = RecoveryCheckpointBinding.model_validate(payload["recovery_binding"])
    assert find_recovery_checkpoint_receipt(tmp_path, binding) == checkpoint_ref
    assert find_recovery_checkpoint_receipt(tmp_path, binding.model_copy(update={"goal_version": 2})) is None
    assert find_recovery_checkpoint_receipt(tmp_path, binding.model_copy(update={"goal_version": None})) is None


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
                goal_version=1,
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
        goal_version=1,
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

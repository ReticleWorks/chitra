"""Adversarial checks for recovery restart and supervision fencing."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_recovery_contract_regressions import NOW, ConsumedSendProvider, append_named_wake, goal, lane_record

from chitra.detect.ladder import IncidentRecord, IncidentStore, ResponseLadder
from chitra.joined_lane import JoinedLaneConflictError, JoinedLaneStore
from chitra.journal import EventJournal
from chitra.provider_protocol import ProviderName, ProviderState, ProviderStatus, ProviderUpdate, ReadUpdatesResult, UpdateKind
from chitra.recovery import RecoveryEngine, RecoveryStateError, RecoveryStateStore, RecoverySupervisor
from chitra.session_contract import (
    JoinedLaneRecord,
    NextCheck,
    ProviderCapabilities,
    ProviderOperationResult,
    RecoveryState,
    WakeReceipt,
)


class _RetryProvider:
    provider_name = ProviderName.TOPHAND
    capabilities = ProviderCapabilities.from_supported(("send", "read_updates"))

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send(self, request: object) -> ProviderOperationResult:
        operation = request.operation
        self.calls.append((operation.operation_id, request.text))
        if len(self.calls) == 1:
            raise RuntimeError("simulated lost reply")
        return ProviderOperationResult(
            operation_id=operation.operation_id,
            kind=operation.kind,
            lane_id=operation.lane_id,
            provider_handle=operation.provider_handle,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            provider_instance_id=operation.provider_instance_id,
            provider_generation=operation.provider_generation,
            status="consumed",
            accepted=True,
            consumed=True,
            observed_at=operation.created_at,
            evidence="same operation retried after the lane update changed",
        )

    def read_updates(self, cursor: str | None = None) -> ReadUpdatesResult:
        return ReadUpdatesResult(requested_cursor=cursor, next_cursor=cursor or "0", updates=())


class _OpaqueSessionProbeProvider:
    provider_name = ProviderName.TOPHAND
    capabilities = ProviderCapabilities.from_supported(("send", "read_updates"))

    def __init__(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        self.pending: object | None = None
        self.send_calls = 0

    def send(self, request: object) -> ProviderOperationResult:
        self.pending = request
        self.send_calls += 1
        raise RuntimeError("simulated lost reply")

    def read_updates(self, cursor: str | None = None) -> ReadUpdatesResult:
        if self.pending is None:
            return ReadUpdatesResult(requested_cursor=cursor, next_cursor=cursor or "0", updates=())
        operation = self.pending.operation
        update = ProviderUpdate(
            event_id="opaque-session-consumed",
            cursor="1",
            kind=UpdateKind.STEER_CONSUMED,
            provider_session_id=self.provider_session_id,
            observed_at=(NOW + timedelta(seconds=1)).isoformat(),
            operation_id=operation.operation_id,
            lane_id=operation.lane_id,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            provider_instance_id=operation.provider_instance_id or "instance-a",
            provider_generation=operation.provider_generation or 1,
            provider_handle=operation.provider_handle,
        )
        return ReadUpdatesResult(requested_cursor=cursor, next_cursor="1", updates=(update,))


def _waiting_record(*, lane: str = "lane-a", goal_id: str = "goal-a", session: str = "tophand:lane-a:1") -> JoinedLaneRecord:
    record = lane_record(
        recovery=RecoveryState(stage="waiting", cycle_id="cycle-wait", failure_signature="stall"),
        next_check=NextCheck(
            at=(NOW + timedelta(hours=1)).isoformat(),
            reason="wait for the named condition",
            wake_condition="the same logical lane resumes",
        ),
    )
    if lane == "lane-a":
        return record
    update = record.current_update.model_copy(update={"lane_id": lane, "goal_id": goal_id, "session_ref": session})
    return record.model_copy(
        update={
            "lane_id": lane,
            "goal_id": goal_id,
            "session_ref": session,
            "provider": record.provider.model_copy(update={"handle": f"tophand-{lane}", "instance_id": f"instance-{lane}"}),
            "current_update": update,
        }
    )


def test_pending_retry_uses_allocated_payload_after_next_action_changes(tmp_path: Path) -> None:
    provider = _RetryProvider()
    record = lane_record(
        recovery=RecoveryState(stage="none", cycle_id="cycle-a", event_sequence=1, failure_signature="stall")
    )
    first = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0)).run_once(
        record,
        now=NOW,
        goal=goal(),
    )
    assert first.record.pending_operation is not None
    changed_update = first.record.current_update.model_copy(
        update={
            "sequence": 2,
            "observed_at": (NOW + timedelta(seconds=1)).isoformat(),
            "next_action": "a new action after the lost reply",
        }
    )
    changed = first.record.model_copy(update={"current_update": changed_update})

    second = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0)).run_once(
        changed, now=NOW + timedelta(seconds=2), goal=goal()
    )

    assert len(provider.calls) == 2
    assert provider.calls[0][0] == provider.calls[1][0]
    assert provider.calls[0][1] == provider.calls[1][1]
    assert second.record.last_operation_result is not None
    assert second.record.last_operation_result.status == "consumed"


def test_new_failure_signature_does_not_clear_an_inflight_operation(tmp_path: Path) -> None:
    provider = _RetryProvider()
    record = lane_record(
        recovery=RecoveryState(stage="none", cycle_id="cycle-signature", failure_signature="old-signature")
    )
    first = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0)).run_once(
        record, now=NOW, goal=goal()
    )
    assert first.record.pending_operation is not None
    scheduled = RecoveryEngine(provider=provider, state_root=tmp_path).schedule(
        first.record, "new-signature", now=NOW + timedelta(seconds=1)
    )
    assert scheduled.pending_operation == first.record.pending_operation
    assert scheduled.recovery.attempted_remedy == first.record.recovery.attempted_remedy
    assert scheduled.recovery.pending_payload == first.record.recovery.pending_payload


def test_interrupted_wake_transaction_replays_the_missing_journal_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JoinedLaneStore(tmp_path)
    record = store.create(lane_record())
    engine = RecoveryEngine(state_root=tmp_path)
    original = EventJournal.append_wakes

    def fail_after_state_write(_journal: EventJournal, _rows: object) -> tuple[object, ...]:
        raise OSError("simulated crash after joined-lane write")

    monkeypatch.setattr(EventJournal, "append_wakes", fail_after_state_write)
    with pytest.raises(OSError, match="simulated crash"):
        engine._record_wake(record, "wake-crash", "the same logical lane resumes", 1, NOW, True)
    monkeypatch.setattr(EventJournal, "append_wakes", original)

    recovered = RecoveryStateStore(tmp_path, "lane-a").load()
    assert recovered is not None
    assert [item.wake_id for item in recovered.wake_receipts] == ["wake-crash"]
    assert [item.wake_id for item in EventJournal(tmp_path, "lane-a").load_wakes()] == ["wake-crash"]
    assert not (tmp_path / "wake-transactions" / "lane-a.json").exists()


def test_close_evidence_symlink_is_rejected(tmp_path: Path) -> None:
    record = lane_record(recovery=RecoveryState(cycle_id="cycle-evidence"))
    operation = RecoveryEngine()._operation(record, "send", "payload", NOW)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    evidence_dir = tmp_path / "close-evidence"
    evidence_dir.mkdir()
    evidence_path = evidence_dir / f"{operation.operation_id}.json"
    evidence_path.symlink_to(target)

    with pytest.raises(RecoveryStateError, match="symlink"):
        RecoveryStateStore(tmp_path, "lane-a").close_evidence_path(operation)


def test_lane_control_lock_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside-lock"
    outside.mkdir()
    (tmp_path / "lane-control").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RecoveryStateError, match="symlink"), RecoveryStateStore(tmp_path, "lane-a").lane_control_lock():
        pass
    assert not (outside / "lane-a.lock").exists()


def test_wake_transaction_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside-wake"
    outside.mkdir()
    (tmp_path / "wake-transactions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RecoveryStateError, match="symlink"):
        RecoveryStateStore(tmp_path, "lane-a").load()
    assert not list(outside.iterdir())


def test_context_handoff_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside-handoff"
    outside.mkdir()
    (tmp_path / "recovery-handoffs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RecoveryStateError, match="symlink"):
        RecoveryEngine(state_root=tmp_path)._handoff_path(
            "recovery-handoffs/lane-a-cycle-context.json"
        )
    assert not list(outside.iterdir())


def test_pending_retry_never_calls_provider_without_durable_storage(tmp_path: Path) -> None:
    provider = _RetryProvider()
    record = lane_record(
        recovery=RecoveryState(stage="none", cycle_id="cycle-durable", event_sequence=1, failure_signature="stall")
    )
    first = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0)).run_once(
        record, now=NOW, goal=goal()
    )
    assert first.record.pending_operation is not None
    assert len(provider.calls) == 1

    second = RecoveryEngine(provider=provider, state_root=None, check_interval=timedelta(0)).run_once(
        first.record, now=NOW + timedelta(seconds=2), goal=goal(), persist=False
    )

    assert len(provider.calls) == 1
    assert second.record.pending_operation is not None
    assert "durable joined-lane storage" in second.reason


@pytest.mark.parametrize(
    ("observed_session_id", "consumed"),
    (("tophand-session-1", True), ("tophand-session-foreign", False)),
)
def test_provider_probe_binds_opaque_session_separately_from_provider_handle(
    tmp_path: Path, observed_session_id: str, consumed: bool
) -> None:
    provider = _OpaqueSessionProbeProvider("tophand-session-1")
    record = lane_record(
        recovery=RecoveryState(stage="none", cycle_id="cycle-probe", event_sequence=1, failure_signature="stall")
    )
    record = record.model_copy(
        update={
            "provider": record.provider.model_copy(update={"provider_session_id": "tophand-session-1"}),
        }
    )
    first = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0)).run_once(
        record, now=NOW, goal=goal()
    )
    assert first.record.pending_operation is not None
    assert first.record.pending_operation.provider_handle == "tophand-lane-a"
    assert first.record.pending_operation.provider_session_id == "tophand-session-1"

    if not consumed:
        provider.provider_session_id = observed_session_id
    second = RecoveryEngine(
        provider=provider,
        state_root=tmp_path,
        check_interval=timedelta(0),
        ownership_probe=lambda _record: {
            "authoritative": True,
            "status": "authoritative",
            "provider_instance_id": "instance-a",
            "session_ref": "tophand:lane-a:1",
            "lane_id": "lane-a",
            "lane_generation": 1,
            "ownership_generation": 1,
        },
    ).run_once(
        first.record,
        now=NOW + timedelta(seconds=2),
        goal=goal(),
    )

    if consumed:
        assert second.record.pending_operation is None
        assert second.record.recovery.stage == "correct"
    else:
        assert second.record.pending_operation is not None
        assert second.record.last_operation_result is not None
        assert second.record.last_operation_result.status != "consumed"


def test_supervisor_isolates_one_lane_failure_and_continues(tmp_path: Path) -> None:
    first = _waiting_record()
    second = _waiting_record(lane="lane-b", goal_id="goal-b", session="tophand:lane-b:1")
    store = JoinedLaneStore(tmp_path)
    store.create(first)
    store.create(second)

    def resolve(record: JoinedLaneRecord) -> None:
        if record.lane_id == "lane-a":
            raise RuntimeError("provider resolver unavailable")
        return None

    decisions = RecoverySupervisor(tmp_path, resolve).run_once(now=NOW)

    assert [decision.record.lane_id for decision in decisions] == ["lane-a", "lane-b"]
    assert "failed closed" in decisions[0].reason
    assert decisions[1].record.lane_id == "lane-b"


def test_supervisor_supplies_exact_named_wake_evidence(tmp_path: Path) -> None:
    record = _waiting_record()
    store = JoinedLaneStore(tmp_path)
    store.create(record)
    append_named_wake(tmp_path, "wake-1", "the same logical lane resumes")

    decisions = RecoverySupervisor(tmp_path, lambda _record: None).run_once(now=NOW)

    assert len(decisions) == 1
    assert decisions[0].record.wake_receipts[0].wake_id == "wake-1"
    assert store.require("lane-a").wake_receipts[0].wake_id == "wake-1"


def test_relaunch_accepts_opaque_provider_status_identity_without_format_guessing(tmp_path: Path) -> None:
    class ForeignProvider:
        provider_name = ProviderName.TOPHAND
        capabilities = ProviderCapabilities.from_supported(("create_or_resume", "status"))

        def create_or_resume(self, request: object) -> ProviderOperationResult:
            operation = request.operation
            return ProviderOperationResult(
                operation_id=operation.operation_id,
                kind=operation.kind,
                lane_id=operation.lane_id,
                provider_handle=operation.provider_handle,
                idempotency_key=operation.idempotency_key,
                payload_digest=operation.payload_digest,
                provider_instance_id=operation.provider_instance_id,
                provider_generation=operation.provider_generation,
                status="consumed",
                accepted=True,
                consumed=True,
                observed_at=operation.created_at,
                evidence="foreign status test",
            )

        def status(self) -> ProviderStatus:
            return ProviderStatus(
                provider=ProviderName.TOPHAND,
                state=ProviderState.IDLE,
                    provider_session_id="tophand-session-opaque-2",
                    provider_instance_id="instance-a",
                    generation=1,
                fresh=True,
                provider_available=True,
                provider_instance_id="instance-a",
                context_available=True,
            )

    record = lane_record(
        recovery=RecoveryState(stage="relaunch", cycle_id="cycle-r", event_sequence=1, failure_signature="stall"),
        checkpoint_reference="checkpoint-a",
    )
    decision = RecoveryEngine(provider=ForeignProvider(), state_root=tmp_path).run_once(record, now=NOW, goal=goal())

    assert decision.record.session_ref == "tophand-session-opaque-2"
    assert "rotated physical session" not in decision.reason


def test_supervisor_path_cannot_skip_unproved_ladder_entry(tmp_path: Path) -> None:
    record = lane_record(
        recovery=RecoveryState(stage="correct", cycle_id="cycle-c", failure_signature="stall")
    )
    IncidentStore(tmp_path, "lane-a")._append(
        IncidentRecord(
            lane="lane-a",
            fingerprint="stall",
            detector="test",
            stage="redirect",
            order_marker="redirect-1",
            opened_at=NOW.isoformat(),
            event_refs=(),
            unmet_item="acceptance",
            expected_next_progress="the next in-scope action",
            detail="unproved redirect",
        )
    )
    provider = ConsumedSendProvider()
    decision = RecoveryEngine(
        provider=provider,
        state_root=tmp_path,
        response_ladder=ResponseLadder(IncidentStore(tmp_path, "lane-a")),
        check_interval=timedelta(0),
    ).run_once(record, now=NOW, goal=goal())

    assert decision.action == "waiting"
    assert provider.operation_ids == []


def test_existing_duplicate_active_owner_is_rejected_on_reload(tmp_path: Path) -> None:
    first = lane_record()
    update = first.current_update.model_copy(update={"lane_id": "lane-b", "goal_id": "goal-b", "session_ref": "tophand:lane-b:1"})
    second = first.model_copy(
        update={"lane_id": "lane-b", "goal_id": "goal-b", "session_ref": "tophand:lane-b:1", "current_update": update}
    )
    directory = tmp_path / "joined-lanes"
    directory.mkdir()
    (directory / "lane-a.json").write_text(json.dumps(first.to_dict()), encoding="utf-8")
    (directory / "lane-b.json").write_text(json.dumps(second.to_dict()), encoding="utf-8")

    with pytest.raises(JoinedLaneConflictError):
        JoinedLaneStore(tmp_path).list()


def test_wake_receipt_session_identity_is_checked(tmp_path: Path) -> None:
    del tmp_path
    record = lane_record().model_copy(
        update={
            "wake_receipts": (
                WakeReceipt(
                    wake_id="wake-1",
                    lane_id="lane-a",
                    goal_id="goal-a",
                    session_ref="tophand:other-lane:1",
                    wake_condition="condition",
                    event_sequence=1,
                    observed_at=NOW.isoformat(),
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="wake receipt session identity"):
        JoinedLaneRecord.from_dict(record.to_dict())

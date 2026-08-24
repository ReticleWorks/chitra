"""Adversarial regressions for the shared recovery contract."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitra.detect.detectors import Finding
from chitra.detect.ladder import ConsumptionProof, IncidentStore, ResponseLadder
from chitra.goals import GoalRecord
from chitra.joined_lane import JoinedLaneIdentityError, JoinedLaneReconciler, JoinedLaneStore
from chitra.journal.models import CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.journal.store import EventJournal
from chitra.ledger import LedgerEntry, message_hash, sign
from chitra.provider_protocol import (
    CreateOrResumeRequest,
    MutationRequest,
    ProviderName,
    ProviderState,
    ProviderStatus,
    SendRequest,
)
from chitra.recovery import RecoveryEngine, confirm_useful_progress
from chitra.session_contract import (
    MAX_INLINE_WAKE_RECEIPTS,
    JoinedLaneRecord,
    LaneUpdate,
    NextCheck,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    RecoveryState,
    RoadmapStep,
    WakeReceipt,
)

NOW = datetime(2026, 8, 23, 14, tzinfo=UTC)


def goal() -> GoalRecord:
    return GoalRecord(
        session_ref="tophand:lane-a:1",
        lane_id="lane-a",
        goal_id="goal-a",
        goal="Ship the enrolled change",
        done_when="The verified acceptance check passes",
        source="recovery-contract-regression",
        status="working",
    )


def lane_record(
    *,
    recovery: RecoveryState | None = None,
    next_check: NextCheck | None = None,
    checkpoint_reference: str | None = None,
    physical_session_generation: int | None = 1,
) -> JoinedLaneRecord:
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
        physical_session_generation=physical_session_generation,
        provider=ProviderIdentity(
            kind="tophand",
            handle="tophand-lane-a",
            instance_id="instance-a",
            generation=1,
            capabilities=ProviderCapabilities.from_supported(("send", "checkpoint", "create_or_resume", "read_updates")),
        ),
        current_update=update,
        recovery=recovery or RecoveryState(),
        next_check=next_check,
        checkpoint_reference=checkpoint_reference,
    )


def append_named_wake(
    tmp_path: Path,
    wake_id: str,
    condition: str,
    *,
    sequence: int = 1,
    goal_version: int | None = 1,
) -> None:
    EventJournal(tmp_path, "lane-a").append(
        (
            CanonicalEvent(
                event_id=wake_id,
                instance="test",
                lane="lane-a",
                client=Client.CODEX,
                client_version="test",
                process_id="1",
                transcript=TranscriptIdentity(path="/tmp/lane-a", device=1, inode=1),
                session_id="tophand:lane-a:1",
                resume_id=None,
                observed_at=(NOW + timedelta(seconds=sequence)).isoformat(),
                native_time=None,
                native_type="wake_condition_changed",
                native_join_id=None,
                raw_byte_range=None,
                raw_sha256=None,
                normalized_type=CanonicalType.UNKNOWN,
                goal_ref="goal-a",
                goal_version=goal_version,
                item_ref=None,
                payload_digest="a" * 64,
                normalizer_version="test",
                payload={"wake_condition": condition, "wake_condition_changed": True},
                raw_record=None,
            ),
        )
    )
class ConsumedSendProvider:
    def __init__(self) -> None:
        self.operation_ids: list[str] = []

    provider_name = ProviderName.TOPHAND
    capabilities = ProviderCapabilities.from_supported(("send", "checkpoint", "create_or_resume", "read_updates", "status"))

    @staticmethod
    def result(request: MutationRequest, *, status: str = "consumed") -> ProviderOperationResult:
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
            evidence="observed lane consumption" if consumed else "transport only",
        )

    def send(self, request: SendRequest) -> ProviderOperationResult:
        self.operation_ids.append(request.operation_id)
        return self.result(request)


def test_progress_evidence_from_another_lane_cannot_complete_recovery(tmp_path: Path) -> None:
    class OtherLaneProgress:
        event_id = "other-lane-progress"
        lane = "lane-b"
        session_id = "tophand:lane-b:1"
        observed_at = NOW.isoformat()
        payload = {
            "progress_evidence": {"artifact_changed": True},
            "summary": "lane B changed an artifact",
        }

    decision = RecoveryEngine(state_root=tmp_path).run_once(
        lane_record(),
        now=NOW,
        failure_signature="cross-lane-progress",
        goal=goal(),
        events=(OtherLaneProgress(),),
    )

    assert decision.action == "nudge"
    assert decision.record.recovery.stage != "complete"
    assert decision.record.last_useful_progress is None


def test_progress_evidence_requires_the_current_goal_version(tmp_path: Path) -> None:
    del tmp_path
    base = lane_record()
    record = base.model_copy(
        update={"goal_version": 2, "current_update": base.current_update.model_copy(update={"goal_version": 2})}
    )

    def event(event_id: str, goal_version: int | None) -> CanonicalEvent:
        return CanonicalEvent(
            event_id=event_id,
            instance="test",
            lane="lane-a",
            client=Client.CODEX,
            client_version="test",
            process_id="1",
            transcript=TranscriptIdentity(path="/tmp/lane-a", device=1, inode=1),
            session_id="tophand:lane-a:1",
            resume_id=None,
            observed_at=NOW.isoformat(),
            native_time=None,
            native_type="tool_result",
            native_join_id=None,
            raw_byte_range=None,
            raw_sha256=None,
            normalized_type=CanonicalType.TOOL_RESULT,
            goal_ref="goal-a",
            goal_version=goal_version,
            item_ref=None,
            payload_digest="a" * 64,
            normalizer_version="test",
            payload={"progress_evidence": {"artifact_changed": True}},
            raw_record=None,
        )

    assert confirm_useful_progress(record, events=(event("stale-progress", 1),)) is None
    assert confirm_useful_progress(record, events=(event("unbound-progress", None),)) is None
    current = confirm_useful_progress(record, events=(event("current-progress", 2),))
    assert current is not None
    assert current.evidence_ref == "current-progress"


def test_consumed_result_with_wrong_operation_identity_cannot_advance(tmp_path: Path) -> None:
    class WrongIdentityProvider:
        def __init__(self) -> None:
            self.sent_operation_id = ""

        def send(self, request: SendRequest) -> ProviderOperationResult:
            self.sent_operation_id = request.operation_id
            return ProviderOperationResult(
                operation_id="wrong-operation",
                kind="send",
                lane_id="other-lane",
                provider_handle="other-handle",
                idempotency_key="wrong-idempotency-key",
                payload_digest="wrong-payload-digest",
                provider_instance_id="other-instance",
                provider_generation=99,
                status="consumed",
                accepted=True,
                consumed=True,
                observed_at=NOW.isoformat(),
                evidence="unrelated result",
            )

    provider = WrongIdentityProvider()
    decision = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0)).run_once(
        lane_record(), now=NOW, failure_signature="wrong-result", goal=goal()
    )

    assert decision.record.recovery.stage != "correct"
    assert decision.operation is not None
    assert decision.operation.operation_id == provider.sent_operation_id


def test_boolean_checkpoint_without_governed_receipt_cannot_enable_relaunch(tmp_path: Path) -> None:
    class ForgedCheckpointProvider(ConsumedSendProvider):
        def __init__(self) -> None:
            super().__init__()
            self.checkpoint_calls = 0
            self.relaunch_calls = 0

        def checkpoint(self, request: MutationRequest) -> object:
            del request
            self.checkpoint_calls += 1
            return {"status": "consumed", "valid": True}

        def create_or_resume(self, request: MutationRequest) -> object:
            del request
            self.relaunch_calls += 1
            return {"status": "consumed", "session_ref": "tophand:lane-a:1"}

    provider = ForgedCheckpointProvider()
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0))
    record = lane_record()
    checkpoint_decision = None
    for offset in range(4):
        decision = engine.run_once(
            record,
            now=NOW + timedelta(seconds=offset),
            failure_signature="forged-checkpoint",
            goal=goal(),
        )
        record = decision.record
        if decision.action == "checkpoint":
            checkpoint_decision = decision
            break

    assert checkpoint_decision is not None
    assert checkpoint_decision.record.checkpoint_reference is None
    assert checkpoint_decision.record.pending_operation is not None
    assert checkpoint_decision.record.pending_operation.kind == "checkpoint"
    assert not (tmp_path / "checkpoints").exists()
    assert not (tmp_path / "rescue").exists()
    assert provider.relaunch_calls == 0


def test_sparse_rotated_session_response_persists_same_logical_lane(tmp_path: Path) -> None:
    class RotatingProvider(ConsumedSendProvider):
        def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
            return self.result(request)

        def status(self) -> ProviderStatus:
            return ProviderStatus(
                provider=ProviderName.TOPHAND,
                state=ProviderState.IDLE,
                provider_session_id="tophand:lane-a:2",
                generation=2,
                fresh=True,
                provider_available=True,
                provider_instance_id="instance-a",
            )

    initial = lane_record(
        recovery=RecoveryState(
            stage="relaunch",
            failure_signature="rotated-session",
            attempted_remedy="checkpoint",
            next_allowed_attempt=NOW.isoformat(),
        ),
        next_check=NextCheck(
            at=NOW.isoformat(),
            reason="Relaunch from the governed checkpoint",
            wake_condition="the same logical lane resumes",
        ),
        checkpoint_reference="checkpoint-a",
    )
    store = JoinedLaneStore(tmp_path)
    stored = store.save(initial)

    decision = RecoveryEngine(provider=RotatingProvider(), state_root=tmp_path, check_interval=timedelta(0)).run_once(
        stored, now=NOW, goal=goal()
    )
    reloaded = store.require("lane-a")

    assert decision.record.session_ref == "tophand:lane-a:2"
    assert reloaded.session_ref == "tophand:lane-a:2"
    assert reloaded.lane_id == initial.lane_id
    assert reloaded.goal_id == initial.goal_id
    assert reloaded.current_update is not None
    assert initial.current_update is not None
    assert reloaded.current_update.session_ref == "tophand:lane-a:2"
    assert reloaded.current_update.steps == initial.current_update.steps
    assert reloaded.current_update.current_action == initial.current_update.current_action
    assert reloaded.current_update.next_action == initial.current_update.next_action
    assert reloaded.physical_session_generation == 2


def test_transport_acceptance_does_not_complete_diagnostic_sibling(tmp_path: Path) -> None:
    class AcceptedDiagnosticProvider(ConsumedSendProvider):
        def send(self, request: SendRequest) -> ProviderOperationResult:
            return self.result(request, status="accepted")

    record = lane_record(
        recovery=RecoveryState(
            stage="diagnostic",
            failure_signature="accepted-diagnostic",
            attempted_remedy="relaunch",
            next_allowed_attempt=NOW.isoformat(),
        ),
        next_check=NextCheck(
            at=NOW.isoformat(),
            reason="Run one bounded diagnostic sibling",
            wake_condition="a material diagnostic result",
        ),
    )
    decision = RecoveryEngine(
        provider=AcceptedDiagnosticProvider(),
        state_root=tmp_path,
        check_interval=timedelta(0),
    ).run_once(record, now=NOW, goal=goal())

    assert decision.record.recovery.stage == "diagnostic"
    assert decision.stage == "diagnostic"
    assert "completed" not in decision.reason


def test_fresh_recovery_cycle_gets_a_new_operation_identity(tmp_path: Path) -> None:
    provider = ConsumedSendProvider()
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0))
    first = engine.run_once(lane_record(), now=NOW, failure_signature="recurring-stall", goal=goal())
    assert first.operation is not None
    completed = first.record.model_copy(
        update={
            "recovery": first.record.recovery.model_copy(update={"stage": "complete", "next_allowed_attempt": None}),
            "next_check": None,
        }
    )
    fresh = engine.schedule(completed, "recurring-stall", now=NOW + timedelta(hours=1))
    second = engine.run_once(fresh, now=NOW + timedelta(hours=1), goal=goal())

    assert second.operation is not None
    assert second.operation.operation_id != first.operation.operation_id
    assert second.operation.idempotency_key != first.operation.idempotency_key
    assert len(provider.operation_ids) == 2


def test_consumed_nudge_does_not_deadlock_against_real_response_ladder(tmp_path: Path) -> None:
    provider = ConsumedSendProvider()
    finding = Finding(
        detector="recovery-contract",
        fingerprint_seed={"signature": "same-stall"},
        event_refs=("finding-event",),
        unmet_item="acceptance",
        expected_next_progress="complete the next in-scope action",
        detail="the same useful-progress stall recurred",
    )
    scheduled = RecoveryEngine(state_root=tmp_path).schedule(
        lane_record(), "ladder-stall", now=NOW
    )
    cycle_id = scheduled.recovery.cycle_id
    assert cycle_id is not None
    marker = f"recovery-lane-a-{cycle_id}-nudge"
    user_text = f"[C] {marker}"
    key = b"k" * 32
    transcript = TranscriptIdentity(path="/tmp/lane-a.jsonl", device=1, inode=1)
    common = {
        "instance": "test",
        "lane": "lane-a",
        "client": Client.CODEX,
        "client_version": "test",
        "process_id": "1",
        "transcript": transcript,
        "session_id": "tophand:lane-a:1",
        "resume_id": None,
        "observed_at": NOW.isoformat(),
        "native_time": NOW.isoformat(),
        "native_join_id": None,
        "raw_byte_range": None,
        "raw_sha256": None,
        "payload_digest": "a" * 64,
        "normalizer_version": "test",
        "raw_record": None,
    }
    journal = (
        CanonicalEvent(
            event_id="user-nudge",
            native_type="user",
            normalized_type=CanonicalType.UNKNOWN,
            goal_ref="goal-a",
            item_ref=None,
            payload={"text": user_text},
            **common,  # type: ignore[arg-type]
        ),
        CanonicalEvent(
            event_id="turn-nudge",
            native_type="assistant",
            normalized_type=CanonicalType.FINAL_RESPONSE,
            goal_ref="goal-a",
            item_ref=None,
            payload={"text": "still stalled"},
            **common,  # type: ignore[arg-type]
        ),
    )
    ladder_store = IncidentStore(tmp_path, "lane-a")
    ladder = ResponseLadder(ladder_store, journal_events=journal, ledger_key=key)
    engine = RecoveryEngine(
        provider=provider,
        state_root=tmp_path,
        response_ladder=ladder,
        check_interval=timedelta(0),
        wait_interval=timedelta(0),
    )
    first = engine.run_once(
        scheduled,
        now=NOW,
        goal=goal(),
        finding=finding,
    )
    digest = message_hash(user_text)
    sent_at = NOW.isoformat()
    ladder_store.attach_consumption(
        fingerprint=finding.fingerprint,
        order_marker=marker,
        proof=ConsumptionProof(
            ledger_entry=LedgerEntry(
                order_id="nudge-order",
                session_ref="tophand:lane-a:1",
                tag="[C]",
                sig_v=4,
                message_hash=digest,
                sent_at=sent_at,
                signature=sign(
                    key,
                    session_ref="tophand:lane-a:1",
                    tag="[C]",
                    digest=digest,
                    sent_at=sent_at,
                ),
            ),
            session_ref="tophand:lane-a:1",
            native_session_id="tophand:lane-a:1",
            user_event_id="user-nudge",
            turn_event_id="turn-nudge",
        ),
    )
    second = engine.run_once(
        first.record,
        now=NOW + timedelta(seconds=1),
        goal=goal(),
        finding=finding,
    )

    assert first.record.last_intervention is not None
    assert first.record.last_intervention.consumed is True
    assert second.record.recovery.stage == "relaunch"
    assert second.action == "correct"


def test_unproved_wake_flag_cannot_bypass_named_wake_condition(tmp_path: Path) -> None:
    future = NOW + timedelta(hours=1)
    record = lane_record(
        recovery=RecoveryState(
            stage="waiting",
            failure_signature="durable-wait",
            next_allowed_attempt=future.isoformat(),
        ),
        next_check=NextCheck(
            at=future.isoformat(),
            reason="Wait for the provider fact revision to change",
            wake_condition="provider fact revision changes",
        ),
    )
    decision = RecoveryEngine(state_root=tmp_path).run_once(record, now=NOW, goal=goal(), wake_event=True)

    assert decision.action == "noop"
    assert decision.record.recovery.stage == "waiting"
    assert decision.record.wake_condition == "provider fact revision changes"


def test_wake_receipt_survives_durable_reload(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    future = NOW + timedelta(hours=1)
    store.save(
        lane_record(
            recovery=RecoveryState(
                stage="waiting",
                failure_signature="wake-receipt",
                next_allowed_attempt=future.isoformat(),
            ),
            next_check=NextCheck(
                at=future.isoformat(),
                reason="Wait for a material update",
                wake_condition="a material update is observed",
            ),
        )
    )
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: None,
        journal_probe=lambda _record: None,
        ownership_probe=lambda _record: None,
        now=lambda: NOW,
    )
    append_named_wake(tmp_path, "wake-receipt-1", "a material update is observed")

    reconciler.wake(
        "lane-a",
        wake_id="wake-receipt-1",
        wake_condition="a material update is observed",
        event_sequence=1,
    )
    reloaded = JoinedLaneStore(tmp_path).require("lane-a")

    assert reloaded.last_intervention is not None
    assert reloaded.last_intervention.operation_id == "wake-receipt-1"
    assert reloaded.last_intervention.consumed is True
    assert reloaded.next_check is not None
    assert reloaded.next_check.wake_condition == "a material update is observed"
    scheduled = RecoveryEngine(state_root=tmp_path).schedule(
        reloaded,
        "later-recovery-action",
        now=NOW + timedelta(minutes=1),
    )
    assert [receipt.wake_id for receipt in scheduled.wake_receipts] == ["wake-receipt-1"]
    replay = reconciler.wake(
        "lane-a",
        wake_id="wake-receipt-1",
        wake_condition="a material update is observed",
        event_sequence=1,
    )
    assert replay.status == "wake_reused"


@pytest.mark.parametrize("event_goal_version", (1, None), ids=("stale", "unbound"))
def test_wake_event_requires_the_current_goal_version(tmp_path: Path, event_goal_version: int | None) -> None:
    base = lane_record()
    current_update = base.current_update.model_copy(update={"goal_version": 2})
    current = base.model_copy(update={"goal_version": 2, "current_update": current_update})
    store = JoinedLaneStore(tmp_path)
    store.create(current)
    condition = "a material update is observed"
    append_named_wake(tmp_path, "versioned-wake", condition, goal_version=event_goal_version)
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: None,
        journal_probe=lambda _record: None,
        ownership_probe=lambda _record: None,
        now=lambda: NOW,
    )

    with pytest.raises(JoinedLaneIdentityError, match="exact canonical evidence"):
        reconciler.wake("lane-a", wake_id="versioned-wake", wake_condition=condition, event_sequence=1)


def test_wake_receipt_version_is_checked_but_unbound_legacy_receipt_is_readable(tmp_path: Path) -> None:
    base = lane_record()
    legacy = WakeReceipt(
        wake_id="legacy-wake",
        lane_id="lane-a",
        goal_id="goal-a",
        session_ref="tophand:lane-a:1",
        wake_condition="a material update is observed",
        event_sequence=1,
        observed_at=NOW.isoformat(),
    )
    readable = JoinedLaneRecord.from_dict(base.model_copy(update={"wake_receipts": (legacy,)}).to_dict())
    assert readable.wake_receipts[0].goal_version is None

    current_update = base.current_update.model_copy(update={"goal_version": 2})
    stale = base.model_copy(
        update={
            "goal_version": 2,
            "current_update": current_update,
            "wake_receipts": (legacy.model_copy(update={"goal_version": 1}),),
        }
    )
    with pytest.raises(ValueError, match="wake receipt goal_version"):
        JoinedLaneRecord.from_dict(stale.to_dict())

    condition = "a material update is observed"
    current_update = base.current_update.model_copy(update={"goal_version": 2})
    current = base.model_copy(
        update={
            "goal_version": 2,
            "current_update": current_update,
            "recovery": RecoveryState(stage="waiting", failure_signature="legacy-wake"),
            "next_check": NextCheck(at=(NOW + timedelta(hours=1)).isoformat(), reason="wait", wake_condition=condition),
            "wake_receipts": (legacy,),
        }
    )
    EventJournal(tmp_path, "lane-a").append_wakes((legacy,))
    engine = RecoveryEngine(state_root=tmp_path)
    assert not engine._named_wake(current, "legacy-wake", condition, 1)

    append_named_wake(tmp_path, "legacy-wake", condition, goal_version=2)
    assert engine._named_wake(current, "legacy-wake", condition, 1)
    current_receipt = legacy.model_copy(update={"goal_version": 2})
    journal = EventJournal(tmp_path, "lane-a")
    assert journal.append_wakes((current_receipt,)) == (current_receipt,)
    assert [receipt.goal_version for receipt in journal.load_wakes()] == [None, 2]
    assert journal.append_wakes((current_receipt,)) == ()


def test_wake_receipts_compact_inline_and_remain_deduplicated_in_journal(tmp_path: Path) -> None:
    condition = "a material update is observed"
    store = JoinedLaneStore(tmp_path)
    store.create(
        lane_record(
            recovery=RecoveryState(stage="waiting", failure_signature="long-wait"),
            next_check=NextCheck(at=NOW.isoformat(), reason="wait", wake_condition=condition),
        )
    )
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: None,
        journal_probe=lambda _record: None,
        ownership_probe=lambda _record: None,
        now=lambda: NOW,
    )
    total = MAX_INLINE_WAKE_RECEIPTS + 1
    for sequence in range(1, total + 1):
        wake_id = f"wake-{sequence}"
        append_named_wake(tmp_path, wake_id, condition, sequence=sequence)
        reconciler.wake(wake_id="wake-" + str(sequence), lane_id="lane-a", wake_condition=condition, event_sequence=sequence)

    reloaded = store.require("lane-a")
    assert len(reloaded.wake_receipts) == MAX_INLINE_WAKE_RECEIPTS
    assert reloaded.wake_archive_count == 1
    assert reloaded.wake_archive_digest
    assert len(EventJournal(tmp_path, "lane-a").load_wakes()) == total
    replay = reconciler.wake(lane_id="lane-a", wake_id="wake-1", wake_condition=condition, event_sequence=1)
    assert replay.status == "wake_reused"


def test_a_production_module_invokes_recovery_supervision() -> None:
    source_root = Path(__file__).parents[1] / "src" / "chitra"
    recovery_call_names = {"RecoveryEngine", "run_recovery_check", "run_recovery_supervision", "schedule_recovery_check"}
    callers: list[tuple[Path, int]] = []
    for path in sorted(source_root.glob("*.py")):
        if path.name == "recovery.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name in recovery_call_names:
                callers.append((path, node.lineno))

    assert callers, "RecoveryEngine is test-only until a production supervisor invokes it"

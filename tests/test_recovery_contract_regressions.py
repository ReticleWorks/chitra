"""Adversarial regressions for the shared recovery contract."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chitra.detect.detectors import Finding
from chitra.detect.ladder import IncidentStore, ResponseLadder
from chitra.goals import GoalRecord
from chitra.joined_lane import JoinedLaneReconciler, JoinedLaneStore
from chitra.recovery import RecoveryEngine
from chitra.session_contract import (
    JoinedLaneRecord,
    LaneUpdate,
    NextCheck,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    RecoveryState,
    RoadmapStep,
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


class ConsumedSendProvider:
    def __init__(self) -> None:
        self.operation_ids: list[str] = []

    def send(self, record: JoinedLaneRecord, text: str, operation: object) -> object:
        del record, text
        self.operation_ids.append(operation.operation_id)  # type: ignore[attr-defined]
        return {"status": "consumed", "evidence": "observed lane consumption"}


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


def test_consumed_result_with_wrong_operation_identity_cannot_advance(tmp_path: Path) -> None:
    class WrongIdentityProvider:
        def __init__(self) -> None:
            self.sent_operation_id = ""

        def send(self, record: JoinedLaneRecord, text: str, operation: object) -> ProviderOperationResult:
            del record, text
            self.sent_operation_id = operation.operation_id  # type: ignore[attr-defined]
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

        def checkpoint(self, record: JoinedLaneRecord, operation: object) -> object:
            del record, operation
            self.checkpoint_calls += 1
            return {"status": "consumed", "valid": True}

        def create_or_resume(self, record: JoinedLaneRecord, operation: object) -> object:
            del record, operation
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
    assert checkpoint_decision.record.recovery.stage != "relaunch"
    assert not (tmp_path / "checkpoints").exists()
    assert not (tmp_path / "rescue").exists()
    assert provider.relaunch_calls == 0


def test_sparse_rotated_session_response_persists_same_logical_lane(tmp_path: Path) -> None:
    class RotatingProvider:
        def create_or_resume(self, record: JoinedLaneRecord, operation: object) -> object:
            del record, operation
            return {"status": "consumed", "session_ref": "tophand:lane-a:2"}

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
    class AcceptedDiagnosticProvider:
        def diagnostic(self, record: JoinedLaneRecord, operation: object, max_children: int = 1) -> object:
            del record, operation, max_children
            return {"status": "accepted", "evidence": "transport only"}

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
    ladder = ResponseLadder(IncidentStore(tmp_path, "lane-a"))
    engine = RecoveryEngine(
        provider=provider,
        state_root=tmp_path,
        response_ladder=ladder,
        check_interval=timedelta(0),
        wait_interval=timedelta(0),
    )
    first = engine.run_once(
        lane_record(),
        now=NOW,
        failure_signature="ladder-stall",
        goal=goal(),
        finding=finding,
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

    reconciler.wake(
        "lane-a",
        wake_id="wake-receipt-1",
        wake_condition="a material update is observed",
    )
    reloaded = JoinedLaneStore(tmp_path).require("lane-a")

    assert reloaded.last_intervention is not None
    assert reloaded.last_intervention.operation_id == "wake-receipt-1"
    assert reloaded.last_intervention.consumed is True
    assert reloaded.next_check is not None
    assert reloaded.next_check.wake_condition == "a material update is observed"


def test_a_production_module_invokes_recovery_supervision() -> None:
    source_root = Path(__file__).parents[1] / "src" / "chitra"
    recovery_call_names = {"RecoveryEngine", "run_recovery_check", "schedule_recovery_check"}
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

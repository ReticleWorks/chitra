"""Deterministic Phase 2 recovery and restart regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from chitra.goals import GoalRecord
from chitra.recovery import RecoveryEngine, RecoveryStateStore
from chitra.session_contract import (
    JoinedLaneRecord,
    LaneUpdate,
    ProviderCapabilities,
    ProviderIdentity,
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
        provider=ProviderIdentity(
            kind="tophand",
            handle="tophand-lane-a",
            instance_id="instance-a",
            generation=1,
            capabilities=ProviderCapabilities.from_supported(("send", "checkpoint", "create_or_resume", "read_updates")),
        ),
        current_update=update,
    )


class SequenceProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | None]] = []

    def send(self, record: JoinedLaneRecord, text: str, operation: object) -> object:
        self.calls.append(("send", text, None))
        return {"status": "consumed", "evidence": "fake transcript receipt"}

    def checkpoint(self, record: JoinedLaneRecord, operation: object) -> object:
        self.calls.append(("checkpoint", "", None))
        return {"status": "consumed", "valid": True, "checkpoint_ref": "checkpoint-a"}

    def create_or_resume(self, record: JoinedLaneRecord, operation: object) -> object:
        self.calls.append(("relaunch", "", None))
        return {"status": "consumed", "session_ref": record.session_ref}

    def diagnostic(self, record: JoinedLaneRecord, operation: object, max_children: int = 1) -> object:
        self.calls.append(("diagnostic", "", max_children))
        return {"status": "consumed", "wake_condition": "a new provider fact"}


def _advance(engine: RecoveryEngine, record: JoinedLaneRecord, provider_goal: GoalRecord) -> JoinedLaneRecord:
    decision = engine.run_once(record, now=NOW, failure_signature="stall-1", goal=provider_goal)
    assert not decision.asks_user
    return decision.record


def test_recovery_follows_exact_bounded_sequence_and_waits(tmp_path: Path) -> None:
    provider = SequenceProvider()
    engine = RecoveryEngine(
        provider=provider,
        state_root=tmp_path,
        check_interval=timedelta(0),
        wait_interval=timedelta(0),
    )
    record = _record()
    for _ in range(5):
        record = _advance(engine, record, _goal())

    assert [call[0] for call in provider.calls] == [
        "send",
        "send",
        "checkpoint",
        "relaunch",
        "diagnostic",
    ]
    assert provider.calls[-1][2] == 1
    assert record.recovery.stage == "waiting"
    assert record.wake_condition == "a new provider fact"
    assert record.next_check is not None
    assert record.next_check.wake_condition == "a new provider fact"
    assert record.goal_id == "goal-a"
    assert record.current_update is not None
    assert record.current_update.all_steps[0].status == "active"
    assert len(record.operation_history) == 5
    assert not engine.run_once(record, now=NOW, failure_signature="stall-1", goal=_goal()).asks_user
    assert [call[0] for call in provider.calls].count("diagnostic") == 1


class LostReplyProvider:
    def __init__(self) -> None:
        self.sent = 0
        self.reconciled = 0

    def send(self, record: JoinedLaneRecord, text: str, operation: object) -> object:
        self.sent += 1
        return None

    def reconcile(self, record: JoinedLaneRecord, operation: object) -> object:
        self.reconciled += 1
        return {"status": "consumed", "evidence": "same operation observed after restart"}


class AcceptedOnlyProvider:
    def __init__(self) -> None:
        self.sent = 0
        self.reconciled = 0

    def send(self, record: JoinedLaneRecord, text: str, operation: object) -> object:
        self.sent += 1
        return {"status": "accepted", "evidence": "transport receipt only"}

    def reconcile(self, record: JoinedLaneRecord, operation: object) -> object:
        self.reconciled += 1
        return {"status": "accepted", "evidence": "still no lane consumption"}


def test_lost_response_reconciles_same_operation_after_restart(tmp_path: Path) -> None:
    first_provider = LostReplyProvider()
    first = RecoveryEngine(
        provider=first_provider,
        state_root=tmp_path,
        check_interval=timedelta(0),
        wait_interval=timedelta(0),
    )
    first_decision = first.run_once(_record(), now=NOW, failure_signature="stall-2", goal=_goal())
    assert first_decision.action == "nudge"
    assert first_decision.record.recovery.stage == "nudge"
    assert first_provider.sent == 1

    second_provider = LostReplyProvider()
    restarted = RecoveryEngine(
        provider=second_provider,
        state_root=tmp_path,
        check_interval=timedelta(0),
        wait_interval=timedelta(0),
    )
    second_decision = restarted.run_once(
        first_decision.record,
        now=NOW + timedelta(minutes=1),
        failure_signature="stall-2",
        goal=_goal(),
    )
    assert second_decision.action == "nudge"
    assert second_decision.record.recovery.stage == "correct"
    assert second_provider.sent == 0
    assert second_provider.reconciled == 1


def test_transport_acceptance_does_not_count_as_useful_consumption(tmp_path: Path) -> None:
    provider = AcceptedOnlyProvider()
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0))
    first = engine.run_once(_record(), now=NOW, failure_signature="stall-accepted", goal=_goal())
    second = engine.run_once(first.record, now=NOW + timedelta(minutes=1), failure_signature="stall-accepted", goal=_goal())

    assert first.action == "nudge"
    assert first.record.recovery.stage == "nudge"
    assert second.record.recovery.stage == "waiting"
    assert provider.sent == 1
    assert provider.reconciled == 1
    assert not second.asks_user


def test_corrupt_newest_joined_record_loads_previous_valid_snapshot(tmp_path: Path) -> None:
    store = RecoveryStateStore(tmp_path, "lane-a")
    first = store.save(_record())
    second = store.save(first.model_copy(update={"repository_commit": "abc123"}))
    assert second.revision > first.revision
    store.path.write_text("{not-json", encoding="utf-8")

    restored = store.load()
    assert restored == first
    assert restored is not None
    assert restored.repository_commit is None


class ProgressEvent:
    event_id = "progress-event-1"
    observed_at = NOW.isoformat()
    payload = {"progress_evidence": {"artifact_changed": True}, "summary": "focused check changed the artifact"}


def test_material_progress_clears_recovery_without_a_user_ask(tmp_path: Path) -> None:
    provider = SequenceProvider()
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, check_interval=timedelta(0))
    decision = engine.run_once(
        _record(),
        now=NOW,
        failure_signature="stall-3",
        goal=_goal(),
        events=(ProgressEvent(),),
    )
    assert decision.action == "progress_confirmed"
    assert decision.stage == "complete"
    assert decision.record.recovery.stage == "complete"
    assert decision.record.last_useful_progress is not None
    assert provider.calls == []
    assert not decision.asks_user

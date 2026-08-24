"""Focused tests for the small Chitra-owned governed close seam."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chitra.goals import GoalRecord
from chitra.joined_lane import JoinedLaneStore
from chitra.provider_protocol import ProviderState, ProviderStatus
from chitra.recovery import RecoveryEngine, RecoverySupervisor
from chitra.session_contract import (
    CloseArchiveResult,
    JoinedLaneRecord,
    ProviderCapabilities,
    ProviderIdentity,
)

NOW = datetime(2026, 8, 23, 14, tzinfo=UTC)


def _record() -> JoinedLaneRecord:
    return JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-a",
        goal_version=1,
        session_ref="logical-session-a",
        provider=ProviderIdentity(
            kind="tophand",
            handle="remote-handle-a",
            provider_session_id="physical-session-a",
            instance_id="tophand-instance-a",
            generation=3,
            capabilities=ProviderCapabilities(status=True, checkpoint=True, close=True),
        ),
    )


class FakeProvider:
    provider_name = "tophand"
    capabilities = ProviderCapabilities(status=True, checkpoint=True, close=True)

    def __init__(self, *, lose_first_reply: bool = False) -> None:
        self.lose_first_reply = lose_first_reply
        self.calls = 0
        self.archive_calls = 0
        self.archived = False

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            provider="tophand",
            state=ProviderState.ARCHIVED if self.archived else ProviderState.IDLE,
            provider_session_id="physical-session-a",
            generation=3,
            fresh=True,
            provider_available=True,
            current_turn_id=None,
        )

    def close(self, request: Any) -> CloseArchiveResult:
        self.calls += 1
        payload = json.loads(request.operation.payload)
        if not self.archived:
            self.archive_calls += 1
            self.archived = True
        result = CloseArchiveResult(
            operation_id=request.operation_id,
            lane_id=request.lane_id,
            provider_handle="remote-handle-a",
            provider_instance_id="tophand-instance-a",
            provider_generation=3,
            idempotency_key=request.idempotency_key,
            payload_digest=request.payload_digest,
            state="closed",
            provider_thread_ref="remote-handle-a",
            provider_session_id="physical-session-a",
            same_provider_thread=True,
            later_resume_supported=False,
            checkpoint_ref=payload["checkpoint_ref"],
            quiescent=True,
            observed_at=NOW.isoformat(),
            evidence="same physical session; archive receipt",
        )
        if self.lose_first_reply and self.calls == 1:
            raise OSError("response lost after provider committed close")
        return result


def test_close_persists_chitra_checkpoint_and_archives_once(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    provider = FakeProvider()

    first = RecoveryEngine(provider=provider, state_root=tmp_path).governed_close(record, now=NOW)
    second = RecoveryEngine(provider=provider, state_root=tmp_path).governed_close(first.record, now=NOW)

    assert first.action == "closed"
    assert second.action == "closed"
    assert first.record.checkpoint_reference is not None
    checkpoint = tmp_path / "checkpoints" / f"{first.record.checkpoint_reference}.json"
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["provenance"] == {"kind": "governed-completion-checkpoint", "owner": "chitra"}
    assert payload["provider_binding"]["provider_session_id"] == "physical-session-a"
    assert provider.archive_calls == 1
    assert provider.calls == 1


def test_lost_close_reply_reuses_pending_operation_and_reconciles_after_restart(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    provider = FakeProvider(lose_first_reply=True)

    first = RecoveryEngine(provider=provider, state_root=tmp_path).governed_close(record, now=NOW)
    persisted = RecoveryEngine(provider=provider, state_root=tmp_path).load("lane-a")
    assert first.action == "waiting"
    assert persisted is not None and persisted.pending_operation is not None
    operation_id = persisted.pending_operation.operation_id

    second = RecoveryEngine(provider=provider, state_root=tmp_path).governed_close(
        persisted, now=NOW.replace(minute=1)
    )

    assert second.action == "closed"
    assert second.operation is not None
    assert second.operation.operation_id == operation_id
    assert provider.calls == 2
    assert provider.archive_calls == 1


def test_supervisor_routes_done_goal_to_close_and_reconciles_after_restart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    provider = FakeProvider(lose_first_reply=True)
    goal = GoalRecord(
        session_ref=record.session_ref,
        lane_id=record.lane_id,
        goal_id=record.goal_id,
        goal="finish the lane",
        done_when="the provider receipt is durable",
        source="test",
        status="done-pending-close",
    )
    monkeypatch.setattr("chitra.recovery.get_goal", lambda _root, _session: goal)
    monkeypatch.setattr("chitra.governed_close.get_goal", lambda _root, _session: goal)
    supervisor = RecoverySupervisor(tmp_path, lambda _record: provider)

    first = supervisor.run_once(now=NOW)
    second = supervisor.run_once(now=NOW.replace(minute=1))

    assert len(first) == len(second) == 1
    assert first[0].action == "waiting"
    assert second[0].action == "closed"
    assert provider.archive_calls == 1


def test_physical_session_mismatch_blocks_close(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    provider = FakeProvider()
    provider.status = lambda: ProviderStatus(  # type: ignore[method-assign]
        provider="tophand",
        state=ProviderState.IDLE,
        provider_session_id="different-physical-session",
        generation=3,
        fresh=True,
        provider_available=True,
        current_turn_id=None,
    )

    decision = RecoveryEngine(provider=provider, state_root=tmp_path).governed_close(record, now=NOW)

    assert decision.action == "waiting"
    assert "physical session" in decision.reason
    assert provider.calls == 0


def test_final_chitra_write_failure_reconciles_without_second_archive(tmp_path: Path, monkeypatch: Any) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    provider = FakeProvider()
    original_save = __import__("chitra.recovery", fromlist=["RecoveryStateStore"]).RecoveryStateStore.save
    saves = 0

    def fail_final_save(store: Any, candidate: JoinedLaneRecord, **kwargs: Any) -> JoinedLaneRecord:
        nonlocal saves
        saves += 1
        if saves == 2:
            raise OSError("simulated final joined-lane write failure")
        return original_save(store, candidate, **kwargs)

    monkeypatch.setattr("chitra.recovery.RecoveryStateStore.save", fail_final_save)
    first = RecoveryEngine(provider=provider, state_root=tmp_path).governed_close(record, now=NOW)
    persisted = RecoveryEngine(provider=provider, state_root=tmp_path).load("lane-a")
    assert first.action == "waiting"
    assert persisted is not None and persisted.pending_operation is not None

    second = RecoveryEngine(provider=provider, state_root=tmp_path).governed_close(
        persisted, now=NOW.replace(minute=2)
    )

    assert second.action == "closed"
    assert provider.archive_calls == 1


def test_close_rejects_provider_result_without_physical_binding(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    provider = FakeProvider()
    original = provider.close

    def missing_physical_session(request: Any) -> CloseArchiveResult:
        return original(request).model_copy(update={"provider_session_id": None})

    provider.close = missing_physical_session  # type: ignore[method-assign]
    decision = RecoveryEngine(provider=provider, state_root=tmp_path).governed_close(record, now=NOW)

    assert decision.action == "waiting"
    assert decision.record.pending_operation is not None
    assert "identity validation" in decision.reason

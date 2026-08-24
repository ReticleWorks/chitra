"""Focused tests for the small Chitra-owned governed close seam."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chitra.goals import GoalRecord
from chitra.joined_lane import JoinedLaneStore
from chitra.provider_protocol import ProviderState, ProviderStatus
from chitra.recovery import (
    RecoveryEngine,
    RecoverySupervisor,
    _close_receipt_hmac,
    _resume_auth_token,
    _resume_receipt_hmac,
)
from chitra.session_contract import (
    CloseArchiveResult,
    JoinedLaneRecord,
    OwnerProcessIdentity,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    ReopenReceipt,
)
from chitra.tophand_wire import request_digest

NOW = datetime(2026, 8, 23, 14, tzinfo=UTC)
OLD_OWNER = OwnerProcessIdentity(
    pid=101, uid=1000, gid=1000, start_token="old", comm="claude", exe="/bin/claude"
)
NEW_OWNER = OwnerProcessIdentity(
    pid=202, uid=1000, gid=1000, start_token="new", comm="claude", exe="/bin/claude"
)


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
            process_start_token=OLD_OWNER.start_token,
            observed_process={
                **OLD_OWNER.model_dump(mode="json"),
                "process_start_token": OLD_OWNER.start_token,
            },
            instance_id="tophand-instance-a",
            generation=3,
            capabilities=ProviderCapabilities(
                create_or_resume=True,
                status=True,
                checkpoint=True,
                close=True,
                resume_after_close=True,
            ),
        ),
    )


def _completion_gate(root: Path, record: JoinedLaneRecord) -> None:
    goal = GoalRecord(
        session_ref=record.session_ref,
        lane_id=record.lane_id,
        goal_id=record.goal_id,
        goal_version=record.goal_version,
        goal="finish the lane",
        done_when="the provider receipt is durable",
        source="test",
        status="done-pending-close",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )
    (root / "goals.json").write_text(
        json.dumps({"schema": "chitra.goals.v4", "goals": [goal.to_dict()]}),
        encoding="utf-8",
    )


class FakeProvider:
    provider_name = "tophand"
    capabilities = ProviderCapabilities(
        create_or_resume=True,
        status=True,
        checkpoint=True,
        close=True,
        resume_after_close=True,
    )

    def __init__(self, *, lose_first_reply: bool = False, lose_first_resume_reply: bool = False) -> None:
        self.lose_first_reply = lose_first_reply
        self.lose_first_resume_reply = lose_first_resume_reply
        self.calls = 0
        self.archive_calls = 0
        self.resume_calls = 0
        self.close_tokens: list[str] = []
        self.archived = False
        self.process_start_token = OLD_OWNER.start_token

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            provider="tophand",
            state=ProviderState.ARCHIVED if self.archived else ProviderState.IDLE,
            provider_session_id="physical-session-a",
            generation=3,
            fresh=True,
            provider_available=True,
            provider_instance_id="tophand-instance-a",
            process_start_token=self.process_start_token,
            current_turn_id=None,
        )

    def close(self, request: Any) -> CloseArchiveResult:
        self.calls += 1
        payload = json.loads(request.operation.payload)
        if not self.archived:
            self.archive_calls += 1
            self.archived = True
        close_token = payload["close_token"]
        self.close_tokens.append(close_token)
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
            later_resume_supported=True,
            checkpoint_ref=payload["checkpoint_ref"],
            quiescent=True,
            checkpoint_receipt=request.checkpoint_receipt,
            checkpoint_receipt_sha256=request.checkpoint_receipt_sha256,
            checkpoint_verifier=request.checkpoint_verifier,
            target_checkpoint_ref="1" * 64,
            target_transcript_sha256="1" * 64,
            close_token=close_token,
            owner_process=OLD_OWNER,
            observed_at=NOW.isoformat(),
            evidence="same physical session; archive receipt",
        )
        result = result.model_copy(
            update={
                "close_receipt_hmac": _close_receipt_hmac(
                    result.to_dict(), close_token
                )
            }
        )
        if self.lose_first_reply and self.calls == 1:
            raise OSError("response lost after provider committed close")
        return result

    def create_or_resume(self, request: Any) -> Any:
        self.resume_calls += 1
        self.archived = False
        self.process_start_token = NEW_OWNER.start_token
        receipt = {
            "schema": "chitra.lane-reopen.v1",
            "operation_id": request.operation_id,
            "close_operation_id": request.close_operation_id,
            "lane_id": request.lane_id,
            "goal_id": request.goal_id,
            "goal_version": request.goal_version,
            "session_ref": request.session_ref,
            "provider_session_id": "physical-session-a",
            "provider_handle": "remote-handle-a",
            "provider_instance_id": request.provider_instance_id,
            "provider_generation": request.provider_generation,
            "checkpoint_ref": request.context_ref,
            "prior_owner_process": OLD_OWNER.model_dump(mode="json"),
            "owner_process": NEW_OWNER.model_dump(mode="json"),
            "created_new_lane": False,
            "created_new_session": False,
            "auth_token": request.resume_token,
            "observed_at": request.operation.created_at,
            "evidence": "same physical session resumed",
        }
        receipt["receipt_hmac"] = _resume_receipt_hmac(receipt, request.resume_token or "")
        result = ProviderOperationResult(
            operation_id=request.operation_id,
            kind="create_or_resume",
            lane_id=request.lane_id,
            provider_handle="remote-handle-a",
            idempotency_key=request.idempotency_key,
            payload_digest=request.payload_digest,
            provider_instance_id=request.provider_instance_id,
            provider_generation=request.provider_generation,
            process_start_token=NEW_OWNER.start_token,
            observed_process={
                **NEW_OWNER.model_dump(mode="json"),
                "process_start_token": NEW_OWNER.start_token,
            },
            status="consumed",
            accepted=True,
            consumed=True,
            provider_session_id="physical-session-a",
            observed_at=request.operation.created_at,
            evidence="same physical session resumed",
            reopen_receipt=ReopenReceipt.from_dict(receipt),
        )
        if self.lose_first_resume_reply and self.resume_calls == 1:
            raise OSError("response lost after provider resumed session")
        return result


def test_close_persists_chitra_checkpoint_and_archives_once(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider()

    first = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(record, now=NOW)
    second = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(first.record, now=NOW)

    assert first.action == "closed"
    assert second.action == "closed"
    assert first.record.checkpoint_reference is not None
    checkpoint = tmp_path / "checkpoints" / f"{first.record.checkpoint_reference}.json"
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["provenance"] == {"kind": "governed-completion-checkpoint", "owner": "chitra"}
    assert payload["provider_binding"]["provider_session_id"] == "physical-session-a"
    assert first.operation is not None
    close_payload = json.loads(first.operation.payload)
    assert close_payload["checkpoint_ref"] == first.record.checkpoint_reference
    assert close_payload["checkpoint_receipt_sha256"]
    assert len(close_payload["close_token"]) == 64
    assert provider.archive_calls == 1
    assert provider.calls == 1


def test_lost_close_reply_reuses_pending_operation_and_reconciles_after_restart(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider(lose_first_reply=True)

    first = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(record, now=NOW)
    persisted = RecoveryEngine(provider=provider, state_root=tmp_path).load("lane-a")
    assert first.action == "waiting"
    assert persisted is not None and persisted.pending_operation is not None
    operation_id = persisted.pending_operation.operation_id

    fresh_provider = FakeProvider()
    fresh_provider.archived = True
    second = RecoveryEngine(provider=fresh_provider, state_root=tmp_path, goal_root=tmp_path).governed_close(
        persisted, now=NOW.replace(minute=1)
    )

    assert second.action == "closed"
    assert second.operation is not None
    assert second.operation.operation_id == operation_id
    assert provider.archive_calls == 1
    assert fresh_provider.calls == 1
    assert fresh_provider.archive_calls == 0
    assert provider.close_tokens == fresh_provider.close_tokens


def test_forged_fleet_close_hmac_keeps_the_lane_active(tmp_path: Path) -> None:
    class ForgedCloseProvider(FakeProvider):
        def close(self, request: Any) -> CloseArchiveResult:
            return super().close(request).model_copy(
                update={"close_receipt_hmac": "0" * 64}
            )

    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)

    decision = RecoveryEngine(
        provider=ForgedCloseProvider(),
        state_root=tmp_path,
        goal_root=tmp_path,
    ).governed_close(record, now=NOW)

    assert decision.action == "waiting"
    assert decision.record.lifecycle == "active"
    assert decision.record.pending_operation is not None
    assert decision.record.last_close_result is None


def test_supervisor_routes_done_goal_to_close_and_reconciles_after_restart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
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
    closed_goals: list[str] = []
    monkeypatch.setattr(
        "chitra.recovery.close_goal",
        lambda _root, session_ref: closed_goals.append(session_ref) or goal,
    )
    supervisor = RecoverySupervisor(tmp_path, lambda _record: provider)

    first = supervisor.run_once(now=NOW)
    second = supervisor.run_once(now=NOW.replace(minute=1))

    assert len(first) == len(second) == 1
    assert first[0].action == "waiting"
    assert second[0].action == "closed"
    assert first[0].asks_user is False
    assert second[0].asks_user is False
    assert provider.archive_calls == 1
    assert closed_goals == [record.session_ref]


def test_physical_session_mismatch_blocks_close(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider()
    provider.status = lambda: ProviderStatus(  # type: ignore[method-assign]
        provider="tophand",
        state=ProviderState.IDLE,
        provider_session_id="different-physical-session",
        generation=3,
        fresh=True,
        provider_available=True,
        provider_instance_id="tophand-instance-a",
        current_turn_id=None,
    )

    decision = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(record, now=NOW)

    assert decision.action == "waiting"
    assert "physical session" in decision.reason
    assert provider.calls == 0


def test_supervisor_keeps_goal_pending_until_completion_close_succeeds(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider()
    supervisor = RecoverySupervisor(tmp_path, lambda _record: provider)

    first = supervisor.run_once(now=NOW)
    second = supervisor.run_once(now=NOW.replace(minute=1))

    assert first[0].action == "waiting"
    assert "completion goal remains open" in first[0].reason
    assert second[0].action == "waiting"
    assert provider.archive_calls == 1


def test_final_chitra_write_failure_reconciles_without_second_archive(tmp_path: Path, monkeypatch: Any) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
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
    first = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(record, now=NOW)
    persisted = RecoveryEngine(provider=provider, state_root=tmp_path).load("lane-a")
    assert first.action == "waiting"
    assert persisted is not None and persisted.pending_operation is not None

    second = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(
        persisted, now=NOW.replace(minute=2)
    )

    assert second.action == "closed"
    assert provider.archive_calls == 1


def test_close_rejects_provider_result_without_physical_binding(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider()
    original = provider.close

    def missing_physical_session(request: Any) -> CloseArchiveResult:
        return original(request).model_copy(update={"provider_session_id": None})

    provider.close = missing_physical_session  # type: ignore[method-assign]
    decision = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(record, now=NOW)

    assert decision.action == "waiting"
    assert decision.record.pending_operation is not None
    assert "identity validation" in decision.reason


def test_unknown_close_receipt_never_marks_the_lane_inactive(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider()
    original = provider.close

    def unknown_receipt(request: Any) -> CloseArchiveResult:
        return original(request).model_copy(
            update={
                "state": "unknown",
                "same_provider_thread": None,
                "checkpoint_ref": None,
                "quiescent": None,
                "later_resume_supported": None,
            }
        )

    provider.close = unknown_receipt  # type: ignore[method-assign]
    decision = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(record, now=NOW)

    assert decision.action == "waiting"
    assert decision.record.lifecycle == "active"


def test_persisted_close_state_without_durable_evidence_is_not_terminal(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider()
    engine = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path)

    closed = engine.governed_close(record, now=NOW)
    assert closed.action == "closed"
    evidence = tmp_path / "close-evidence" / f"{closed.close_result.operation_id}.json"
    evidence.unlink()

    replay = RecoveryEngine(
        provider=provider, state_root=tmp_path, goal_root=tmp_path
    ).governed_close(closed.record, now=NOW.replace(minute=1))

    assert replay.action == "waiting"
    assert "durable Chitra evidence" in replay.reason
    assert provider.archive_calls == 1


def test_direct_close_requires_an_explicit_completion_goal_root(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider()

    decision = RecoveryEngine(provider=provider, state_root=tmp_path).governed_close(record, now=NOW)

    assert decision.action == "waiting"
    assert "explicit completion goal root" in decision.reason
    assert provider.archive_calls == 0


def test_resume_after_close_restores_the_same_provider_session(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider()

    closed = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(record, now=NOW)
    resumed = RecoveryEngine(provider=provider, state_root=tmp_path).resume_after_close(
        closed.record, now=NOW.replace(minute=1)
    )

    assert closed.action == "closed"
    assert resumed.action == "resumed"
    assert resumed.record.lifecycle == "active"
    assert resumed.record.last_close_result is None
    assert resumed.record.provider.provider_session_id == "physical-session-a"
    assert resumed.operation is not None and resumed.operation.kind == "create_or_resume"
    assert json.loads(resumed.operation.payload) == {
        "session_ref": "logical-session-a",
        "provider_session_id": "physical-session-a",
        "context_ref": closed.record.checkpoint_reference,
        "goal_id": "goal-a",
        "goal_version": 1,
        "resume_after_close": True,
        "close_operation_id": closed.record.last_close_result.operation_id,
        "owner_process": OLD_OWNER.model_dump(mode="json"),
        "resume_token": _resume_auth_token(
            closed.record, closed.record.last_close_result, resumed.operation, state_root=tmp_path
        ),
    }
    assert resumed.record.provider.process_start_token == NEW_OWNER.start_token
    assert resumed.record.provider.observed_process == {
        **NEW_OWNER.model_dump(mode="json"),
        "process_start_token": NEW_OWNER.start_token,
    }
    assert closed.record.last_close_result is not None
    assert resumed.operation.payload_digest == request_digest(
        "create_or_resume",
        {
            "session_ref": "logical-session-a",
            "provider_session_id": "physical-session-a",
            "context_ref": closed.record.checkpoint_reference,
            "goal_id": "goal-a",
            "goal_version": 1,
            "resume_after_close": True,
            "close_operation_id": closed.record.last_close_result.operation_id,
            "owner_process": OLD_OWNER.model_dump(mode="json"),
            "resume_token": _resume_auth_token(
                closed.record, closed.record.last_close_result, resumed.operation, state_root=tmp_path
            ),
        },
    )
    assert provider.resume_calls == 1


def test_resume_lost_reply_reuses_the_durable_operation_after_restart(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider(lose_first_resume_reply=True)

    closed = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(record, now=NOW)
    first = RecoveryEngine(provider=provider, state_root=tmp_path).resume_after_close(
        closed.record, now=NOW.replace(minute=1)
    )
    persisted = RecoveryEngine(provider=provider, state_root=tmp_path).load("lane-a")
    assert first.action == "waiting"
    assert persisted is not None and persisted.pending_operation is not None
    operation_id = persisted.pending_operation.operation_id

    second = RecoveryEngine(provider=provider, state_root=tmp_path).resume_after_close(
        persisted, now=NOW.replace(minute=2)
    )

    assert second.action == "resumed"
    assert second.operation is not None and second.operation.operation_id == operation_id
    assert provider.resume_calls == 2


def test_forged_resume_receipt_hmac_keeps_the_lane_inactive(tmp_path: Path) -> None:
    class ForgedResume(FakeProvider):
        def create_or_resume(self, request: Any) -> Any:
            result = super().create_or_resume(request)
            if result.status == "consumed" and result.reopen_receipt is not None:
                receipt = result.reopen_receipt.model_copy(update={"receipt_hmac": "0" * 64})
                return result.model_copy(update={"reopen_receipt": receipt})
            return result

    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = ForgedResume()
    closed = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(
        record, now=NOW
    )
    resumed = RecoveryEngine(provider=provider, state_root=tmp_path).resume_after_close(
        closed.record, now=NOW.replace(minute=1)
    )

    assert resumed.action == "waiting"
    assert resumed.record.lifecycle == "inactive"
    assert "Fleet receipt HMAC" in resumed.reason

def test_concurrent_supervisors_issue_one_physical_close(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    provider = FakeProvider()

    def close_once() -> str:
        return RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(
            lane_id="lane-a", now=NOW
        ).action

    with ThreadPoolExecutor(max_workers=2) as workers:
        actions = tuple(workers.map(lambda _index: close_once(), (1, 2)))

    assert actions == ("closed", "closed")
    assert provider.archive_calls == 1


def test_completion_close_does_not_ask_the_user_for_permission(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)
    decision = RecoveryEngine(provider=FakeProvider(), state_root=tmp_path, goal_root=tmp_path).governed_close(
        record, now=NOW
    )

    assert decision.asks_user is False


def test_handoff_snapshot_contains_the_full_authoritative_goal_record(tmp_path: Path) -> None:
    record = _record()
    goal = GoalRecord(
        session_ref=record.session_ref,
        lane_id=record.lane_id,
        goal_id=record.goal_id,
        goal_version=record.goal_version,
        goal="finish the lane",
        done_when="the provider receipt is durable",
        source="task-file:close-contract",
        status="done-pending-close",
        enrolled_done_when="the provider receipt is durable",
        enrolled_at=NOW.isoformat(),
        intent="Keep the exact provider session moving through close and resume.",
        scope="One lane and its one physical provider session.",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        needs="none",
    )
    payload = RecoveryEngine._immutable_goal_payload(goal)

    assert payload == goal.to_dict()
    assert payload["goal_id"] == goal.goal_id
    assert payload["goal_version"] == goal.goal_version
    assert payload["intent"] == goal.intent
    assert payload["scope"] == goal.scope
    assert payload["needs"] == goal.needs

def test_consumed_resume_does_not_activate_an_archived_provider(tmp_path: Path) -> None:
    record = _record()
    JoinedLaneStore(tmp_path).create(record)
    _completion_gate(tmp_path, record)

    class StaleResumeProvider(FakeProvider):
        def create_or_resume(self, request: Any) -> ProviderOperationResult:
            result = super().create_or_resume(request)
            self.archived = True
            return result

    provider = StaleResumeProvider()
    closed = RecoveryEngine(provider=provider, state_root=tmp_path, goal_root=tmp_path).governed_close(record, now=NOW)
    resumed = RecoveryEngine(provider=provider, state_root=tmp_path).resume_after_close(
        closed.record, now=NOW.replace(minute=1)
    )

    assert resumed.action == "waiting"
    assert resumed.record.lifecycle == "inactive"
    assert "fresh live provider status" in resumed.reason

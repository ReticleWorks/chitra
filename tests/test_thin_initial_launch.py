"""Focused proof for the thin, durable Tophand first-launch path."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra.goals import GoalRecord, upsert_goal
from chitra.initial_launch import (
    InitialLaunchError,
    top_hand_bootstrap_record,
    top_hand_create_operation,
    top_hand_identity_from_facts,
)
from chitra.joined_lane import JoinedLaneStore
from chitra.provider_protocol import CreateOrResumeRequest, ProviderName
from chitra.recovery import RecoverySupervisor
from chitra.session_contract import (
    OperatingFact,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
)

NOW = datetime(2026, 8, 24, 2, tzinfo=UTC)


def _goal(root: Path, lane: str = "lane-a") -> GoalRecord:
    return upsert_goal(
        root,
        GoalRecord(
            session_ref=f"tophand:{lane}:1",
            lane_id="",
            intent="Run the enrolled lane goal.",
            goal="Run the enrolled lane goal and verify its receipt.",
            done_when="The lane records an exact launch receipt.",
            scope="The enrolled lane only.",
            source="test-thin-initial-launch",
            status="working",
            **enrollment_fields("The lane records an exact launch receipt."),
        ),
    )


def _fact(goal: GoalRecord, **changes: object) -> OperatingFact:
    value: dict[str, object] = {
        "provider_session_id": goal.session_ref,
        "provider_handle": f"tophand-{goal.lane_id}",
        "provider_instance_id": f"instance-{goal.lane_id}",
        "provider_generation": 1,
        "process_start_token": f"boot-a:{goal.lane_id}",
        "capabilities": ["create_or_resume"],
    }
    value.update(changes)
    return OperatingFact(
        name=f"fleet.provider-capabilities.tophand.{goal.lane_id}",
        value=value,
        state="known",
        source="fleet-authority",
        revision=1,
        observed_at=NOW.isoformat(),
        freshness="current",
        fresh_until=(NOW + timedelta(days=1)).isoformat(),
        within_authority=True,
    )


def _ownership(operation: object, *, provider_pid: int = 4321, **changes: object) -> dict[str, object]:
    assert hasattr(operation, "operation_id")
    payload: dict[str, object] = {
        "operation_id": operation.operation_id,
        "lane_id": operation.lane_id,
        "session_ref": operation.provider_session_id,
        "provider_session_id": operation.provider_session_id,
        "provider_handle": operation.provider_handle,
        "provider_instance_id": operation.provider_instance_id,
        "provider_generation": operation.provider_generation,
        "process_start_token": operation.process_start_token,
        "provider_pid": provider_pid,
        "owner_pid": provider_pid,
        "observed_process": {"pid": provider_pid, "process_start_token": operation.process_start_token},
        "authoritative": True,
        "status": "owned",
    }
    payload.update(changes)
    return payload


class _LaunchProvider:
    provider_name = ProviderName.TOPHAND
    capabilities = ProviderCapabilities.from_supported(("create_or_resume",))

    def __init__(self, root: Path, *, fail_first: bool = False, ownership_changes: dict[str, object] | None = None) -> None:
        self.root = root
        self.fail_first = fail_first
        self.ownership_changes = ownership_changes or {}
        self.calls: list[str] = []
        self.root_creates: set[str] = set()
        self.pending_seen_before_call = False

    def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
        operation = request.operation
        self.calls.append(operation.operation_id)
        pending = JoinedLaneStore(self.root).require(operation.lane_id).pending_operation
        self.pending_seen_before_call = pending is not None and pending.operation_id == operation.operation_id
        if operation.operation_id not in self.root_creates:
            self.root_creates.add(operation.operation_id)
            if self.fail_first:
                self.fail_first = False
                raise RuntimeError("lost Tophand response")
        return ProviderOperationResult(
            operation_id=operation.operation_id,
            kind=operation.kind,
            lane_id=operation.lane_id,
            provider_handle=operation.provider_handle,
            provider_session_id=operation.provider_session_id,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            provider_instance_id=operation.provider_instance_id,
            provider_generation=operation.provider_generation,
            process_start_token=operation.process_start_token,
            provider_pid=4321,
            owner_pid=4321,
            observed_process={"pid": 4321, "process_start_token": operation.process_start_token},
            ownership=_ownership(operation, **self.ownership_changes),
            status="consumed",
            accepted=True,
            consumed=True,
            observed_at=NOW.isoformat(),
            evidence="exact Tophand ownership receipt",
        )


class _CrossProcessLaunchProvider:
    """Provider surface whose call count is shared by separate supervisors."""

    provider_name = ProviderName.TOPHAND
    capabilities = ProviderCapabilities.from_supported(("create_or_resume",))

    def __init__(self, root: Path) -> None:
        self.root = root

    def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
        operation = request.operation
        calls_path = self.root / "provider-calls.jsonl"
        calls_path.parent.mkdir(parents=True, exist_ok=True)
        with calls_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps({"operation_id": operation.operation_id}) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        ownership = _ownership(
            operation,
            provider_pid=4321,
            observed_process={"pid": 4321, "process_start_token": operation.process_start_token},
        )
        return ProviderOperationResult(
            operation_id=operation.operation_id,
            kind=operation.kind,
            lane_id=operation.lane_id,
            provider_handle=operation.provider_handle,
            provider_session_id=operation.provider_session_id,
            process_start_token=operation.process_start_token,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            provider_instance_id=operation.provider_instance_id,
            provider_generation=operation.provider_generation,
            provider_pid=4321,
            owner_pid=4321,
            observed_process=ownership["observed_process"],
            ownership=ownership,
            status="consumed",
            accepted=True,
            consumed=True,
            observed_at=NOW.isoformat(),
            evidence="cross-process exact ownership receipt",
        )


def _run_cross_process_supervisor(root_text: str, facts: tuple[OperatingFact, ...]) -> None:
    root = Path(root_text)
    provider = _CrossProcessLaunchProvider(root)
    supervisor = RecoverySupervisor(
        root,
        lambda _record: provider,
        goal_root=root,
        lane_id="lane-a",
        operating_facts_reader=lambda: facts,
    )
    supervisor.run_once(now=NOW)


def _supervisor(root: Path, provider: _LaunchProvider, facts: tuple[OperatingFact, ...]) -> RecoverySupervisor:
    return RecoverySupervisor(
        root,
        lambda _record: provider,
        goal_root=root,
        lane_id="lane-a",
        operating_facts_reader=lambda: facts,
    )


def test_identity_resolution_requires_current_authoritative_exact_fact(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    identity = top_hand_identity_from_facts(goal, (_fact(goal),), now=NOW)
    assert identity.handle == "tophand-lane-a"
    assert identity.provider_session_id == goal.session_ref
    with pytest.raises(InitialLaunchError, match="session"):
        top_hand_identity_from_facts(goal, (_fact(goal, provider_session_id="tophand:other:1"),), now=NOW)
    with pytest.raises(InitialLaunchError, match="current authoritative"):
        top_hand_identity_from_facts(
            goal,
            (_fact(goal).model_copy(update={"freshness": "unknown", "fresh_until": None}),),
            now=NOW,
        )


def test_pending_create_is_persisted_before_restricted_provider_call_and_materialized(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    provider = _LaunchProvider(tmp_path)
    facts = (_fact(goal),)
    decisions = _supervisor(tmp_path, provider, facts).run_once(now=NOW)
    persisted = JoinedLaneStore(tmp_path).require(goal.lane_id)
    assert provider.pending_seen_before_call
    assert len(provider.calls) == 1
    assert len(provider.root_creates) == 1
    assert persisted.pending_operation is None
    assert persisted.provider.provider_session_id == goal.session_ref
    assert decisions[0].action == "relaunch"


def test_restart_after_pending_write_resumes_the_same_create(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    identity = top_hand_identity_from_facts(goal, (_fact(goal),), now=NOW)
    operation = top_hand_create_operation(goal, identity, now=NOW)
    pending = top_hand_bootstrap_record(goal, identity, operation, now=NOW)
    JoinedLaneStore(tmp_path).create(pending)
    provider = _LaunchProvider(tmp_path)
    decisions = _supervisor(tmp_path, provider, (_fact(goal),)).run_once(now=NOW)
    assert provider.calls == [operation.operation_id]
    assert decisions[0].action == "relaunch"
    assert JoinedLaneStore(tmp_path).require(goal.lane_id).pending_operation is None


def test_restart_after_result_persisted_finishes_without_a_second_provider_call(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    identity = top_hand_identity_from_facts(goal, (_fact(goal),), now=NOW)
    operation = top_hand_create_operation(goal, identity, now=NOW)
    pending = top_hand_bootstrap_record(goal, identity, operation, now=NOW)
    store = JoinedLaneStore(tmp_path)
    store.create(pending)
    provider = _LaunchProvider(tmp_path)
    result = provider.create_or_resume(
        CreateOrResumeRequest(
            operation=operation,
            session_ref=goal.session_ref,
            provider_session_id=goal.session_ref,
        )
    )
    store.save(
        pending.model_copy(update={"last_operation_result": result, "revision": pending.revision + 1}),
        expected_revision=pending.revision,
    )
    _supervisor(tmp_path, provider, (_fact(goal),)).run_once(now=NOW)
    assert provider.calls == [operation.operation_id]
    assert store.require(goal.lane_id).pending_operation is None


def test_lost_response_restart_reuses_one_operation_and_one_root_create(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    facts = (_fact(goal),)
    first_provider = _LaunchProvider(tmp_path, fail_first=True)
    first = _supervisor(tmp_path, first_provider, facts).run_once(now=NOW)
    pending = JoinedLaneStore(tmp_path).require(goal.lane_id).pending_operation
    assert first and pending is not None
    assert first_provider.calls == [pending.operation_id]
    assert len(first_provider.root_creates) == 1

    second_provider = _LaunchProvider(tmp_path)
    second = _supervisor(tmp_path, second_provider, facts).run_once(now=NOW + timedelta(minutes=6))
    persisted = JoinedLaneStore(tmp_path).require(goal.lane_id)
    assert second and second[0].action == "relaunch"
    assert second_provider.calls == [pending.operation_id]
    assert len(second_provider.root_creates) == 1
    assert persisted.pending_operation is None


def test_pid_reuse_or_mismatched_ownership_stays_pending(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    provider = _LaunchProvider(tmp_path, ownership_changes={"provider_instance_id": "new-instance"})
    _supervisor(tmp_path, provider, (_fact(goal),)).run_once(now=NOW)
    persisted = JoinedLaneStore(tmp_path).require(goal.lane_id)
    assert persisted.pending_operation is not None
    assert persisted.last_operation_result is not None
    assert persisted.last_operation_result.status == "unknown"


def test_two_supervisors_serialize_one_initial_create(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    facts = (_fact(goal),)
    provider = _LaunchProvider(tmp_path)
    barrier = threading.Barrier(2)
    decisions: list[tuple[object, ...]] = []

    def run() -> None:
        barrier.wait()
        decisions.append(_supervisor(tmp_path, provider, facts).run_once(now=NOW))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(decisions) == 2
    assert len(provider.root_creates) == 1
    assert provider.calls.count(next(iter(provider.root_creates))) == 1
    assert JoinedLaneStore(tmp_path).require(goal.lane_id).pending_operation is None


def test_separate_supervisor_processes_create_one_initial_operation(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    facts = (_fact(goal),)
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_run_cross_process_supervisor, args=(str(tmp_path), facts))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    calls = (tmp_path / "provider-calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert JoinedLaneStore(tmp_path).require(goal.lane_id).pending_operation is None


def test_provider_exception_does_not_abort_other_lane(tmp_path: Path) -> None:
    first_goal = _goal(tmp_path, "lane-a")
    second_goal = _goal(tmp_path, "lane-b")
    first_provider = _LaunchProvider(tmp_path, fail_first=True)
    second_provider = _LaunchProvider(tmp_path)
    providers = {"lane-a": first_provider, "lane-b": second_provider}
    facts = (_fact(first_goal), _fact(second_goal))
    supervisor = RecoverySupervisor(
        tmp_path,
        lambda record: providers[record.lane_id],
        goal_root=tmp_path,
        operating_facts_reader=lambda: facts,
    )
    supervisor.run_once(now=NOW)
    assert JoinedLaneStore(tmp_path).require("lane-a").pending_operation is not None
    assert JoinedLaneStore(tmp_path).require("lane-b").pending_operation is None


def test_bootstrap_record_and_operation_are_stable_across_restart(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    identity = top_hand_identity_from_facts(goal, (_fact(goal),), now=NOW)
    first_operation = top_hand_create_operation(goal, identity, now=NOW)
    second_operation = top_hand_create_operation(goal, identity, now=NOW + timedelta(hours=1))
    assert first_operation.operation_id == second_operation.operation_id
    assert first_operation.idempotency_key == second_operation.idempotency_key
    assert first_operation.payload_digest == second_operation.payload_digest
    record = top_hand_bootstrap_record(goal, identity, first_operation, now=NOW)
    assert record.pending_operation == first_operation
    assert record.current_update is None

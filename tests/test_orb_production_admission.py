"""Acceptance contract for first ORB admission through production supervision.

These tests deliberately enter through :func:`run_recovery_supervision`.  They
do not call an ORB bootstrap helper or write a joined-lane row on the test's
behalf.  The production supervisor must own admission, the durable intent, and
restart reconciliation before the provider is allowed to create anything.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from _goal_fixtures import enrollment_fields

from chitra.goals import GoalRecord, upsert_goal
from chitra.joined_lane import JoinedLaneStore
from chitra.provider_protocol import CreateOrResumeRequest, ProviderName
from chitra.recovery import RecoveryStateStore, RecoverySupervisor, run_recovery_supervision
from chitra.session_contract import (
    CapabilityName,
    FactFreshness,
    JoinedLaneRecord,
    OperatingFact,
    PendingProviderOperation,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    RecordTransitionKind,
)

PROFILE_DIGEST = "sha256:" + "a" * 64
AMP_VERSION = "0.0.1787241916-g56aafe"
CREATED_HANDLE = "amp-created-thread-a"
CAPABILITIES: tuple[CapabilityName, ...] = (
    "create_or_resume",
    "status",
    "send",
    "read_updates",
    "checkpoint",
    "usage",
    "cancel_current_turn",
    "close",
    "resume_after_close",
    "subagents",
    "parent_child_usage",
)


def _goal(root: Path, *, lane_id: str = "orb-lane") -> GoalRecord:
    return upsert_goal(
        root,
        GoalRecord(
            session_ref=f"amp:{lane_id}:1",
            lane_id="",
            intent="Run one admitted ORB lane.",
            goal="Run one admitted ORB lane and retain its exact evidence.",
            done_when="The ORB lane records its exact accepted launch.",
            scope="The enrolled ORB lane only.",
            source="test-orb-production-admission",
            status="working",
            **enrollment_fields("The ORB lane records its exact accepted launch."),
        ),
    )


def _orb_fact(
    goal: GoalRecord,
    *,
    enabled: bool = False,
    freshness: FactFreshness = "current",
    provider_session_id: str | None = None,
    provider_handle: str | None = None,
    revision: str = "orb-admission-1",
) -> OperatingFact:
    now = datetime.now(UTC)
    return OperatingFact(
        name="fleet.provider-capabilities",
        value={
            "amp": {"binary": "/usr/local/bin/amp", "version": AMP_VERSION},
            "orb_lane_surface": {
                "name": "orb",
                "target_machine": "twinridge",
                "provider": "amp",
                "transport": "direct-amp-cli",
                "enabled": enabled,
                "visibility": "private",
                "orb_size": "a1.tiny",
                "no_archive_after_execute": True,
                "amp_binary_path": "/usr/local/bin/amp",
                "amp_version": AMP_VERSION,
                "lane_id": goal.lane_id,
                "provider_session_id": provider_session_id or goal.session_ref,
                "provider_instance_id": f"amp-instance-{goal.lane_id}",
                "provider_generation": 1,
                "process_start_token": f"amp-process-{goal.lane_id}",
                "parent_thread_ref": "amp-parent-thread",
                "capabilities": list(CAPABILITIES),
                "project_ref": "amp-project-a",
                "profile_digest": PROFILE_DIGEST,
                "cost_ceiling_usd": 10,
                "turn_reserve_usd": 1,
                "usage_poll_interval_seconds": 30,
                "usage_max_age_seconds": 120,
                **({"provider_handle": provider_handle} if provider_handle is not None else {}),
            },
        },
        state="known",
        source="fleet-authority:test",
        revision=revision,
        observed_at=(now - timedelta(seconds=5)).isoformat(),
        freshness=freshness,
        fresh_until=(now + timedelta(minutes=5)).isoformat() if freshness == "current" else None,
        within_authority=True,
    )


class _OrbLaunchProvider:
    provider_name = ProviderName.AMP
    capabilities = ProviderCapabilities.from_supported(CAPABILITIES)

    def __init__(
        self,
        root: Path,
        *,
        lose_first_response: bool = False,
        result_provider_session_id: str | None = None,
        result_instance_id: str | None = None,
    ) -> None:
        self.root = root
        self.lose_first_response = lose_first_response
        self.calls: list[PendingProviderOperation] = []
        self.physical_creates: set[str] = set()
        self.pre_io_rows: list[JoinedLaneRecord] = []
        self.create_request_handles: list[str | None] = []
        self.result_provider_session_id = result_provider_session_id
        self.result_instance_id = result_instance_id

    def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
        operation = request.operation
        # This read happens at the provider boundary.  It proves the canonical
        # row, policy, and attempted pending envelope existed before I/O.
        self.pre_io_rows.append(cast(JoinedLaneRecord, JoinedLaneStore(self.root).require(operation.lane_id)))
        self.create_request_handles.append(operation.provider_handle)
        self.calls.append(operation)
        first_physical_create = operation.idempotency_key not in self.physical_creates
        self.physical_creates.add(operation.idempotency_key)
        if first_physical_create and self.lose_first_response:
            self.lose_first_response = False
            raise RuntimeError("simulated lost ORB create response")
        return ProviderOperationResult(
            operation_id=operation.operation_id,
            kind=operation.kind,
            lane_id=operation.lane_id,
            # Amp alone supplies the created/adopted physical thread.  A
            # caller-provided handle would turn this create into a resume.
            provider_handle=CREATED_HANDLE,
            provider_session_id=self.result_provider_session_id or operation.provider_session_id,
            process_start_token=operation.process_start_token,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            provider_instance_id=self.result_instance_id or operation.provider_instance_id,
            provider_generation=operation.provider_generation,
            status="consumed",
            accepted=True,
            consumed=True,
            observed_at=datetime.now(UTC).isoformat(),
            evidence="exact mocked ORB create receipt",
        )


def _supervisor(
    root: Path,
    goal: GoalRecord,
    provider: _OrbLaunchProvider,
    facts: tuple[OperatingFact, ...],
    *,
    tophand_calls: list[str] | None = None,
) -> RecoverySupervisor:
    calls = tophand_calls if tophand_calls is not None else []

    def forbidden_tophand_identity(*_args: object, **_kwargs: object) -> ProviderIdentity:
        calls.append(goal.lane_id)
        raise AssertionError("an Amp enrollment was coerced through Tophand identity resolution")

    return RecoverySupervisor(
        root,
        lambda record: provider if record.provider.kind == "amp" else None,
        goal_root=root,
        lane_id=goal.lane_id,
        identity_resolver=forbidden_tophand_identity,
        operating_facts_reader=lambda: facts,
    )


def test_public_supervisor_persists_orb_policy_and_pending_create_before_provider_io(
    tmp_path: Path,
) -> None:
    goal = _goal(tmp_path)
    provider = _OrbLaunchProvider(tmp_path)
    tophand_calls: list[str] = []

    decisions = run_recovery_supervision(
        _supervisor(
            tmp_path,
            goal,
            provider,
            (_orb_fact(goal),),
            tophand_calls=tophand_calls,
        )
    )

    assert tophand_calls == []
    assert len(provider.calls) == 1
    assert len(provider.pre_io_rows) == 1
    pre_io = provider.pre_io_rows[0]
    assert pre_io.provider.kind == "amp"
    assert pre_io.provider.provider_session_id == goal.session_ref
    assert pre_io.provider.handle is None
    assert pre_io.pending_operation == provider.calls[0]
    assert pre_io.pending_operation.provider_handle is None
    assert provider.create_request_handles == [None]
    assert pre_io.pending_operation.attempted is True
    assert pre_io.current_update is None
    assert pre_io.launch_policy is not None
    assert pre_io.launch_policy.model_dump(exclude={"created_at"}) == {
        "schema": "chitra.lane-launch-policy.v1",
        "lane_id": goal.lane_id,
        "goal_id": goal.goal_id,
        "goal_version": goal.goal_version,
        "provider_kind": "amp",
        "project_ref": "amp-project-a",
        "profile_digest": PROFILE_DIGEST,
        "provider_version": AMP_VERSION,
        "cost_ceiling_usd": 10,
        "turn_reserve_usd": 1,
        "usage_poll_interval_seconds": 30,
        "usage_max_age_seconds": 120,
    }
    assert decisions and decisions[0].record.lane_id == goal.lane_id
    stored = JoinedLaneStore(tmp_path).require(goal.lane_id)
    assert stored.provider.handle == CREATED_HANDLE
    assert stored.last_operation_result is not None
    assert stored.last_operation_result.provider_handle == CREATED_HANDLE


@pytest.mark.parametrize(
    "inadmissible",
    ("missing", "stale", "duplicate", "enabled", "wrong-session", "prebound-handle"),
)
def test_public_supervisor_does_not_create_before_exact_orb_admission(
    tmp_path: Path,
    inadmissible: str,
) -> None:
    goal = _goal(tmp_path)
    provider = _OrbLaunchProvider(tmp_path)
    tophand_calls: list[str] = []
    fact = _orb_fact(goal)
    facts: tuple[OperatingFact, ...]
    if inadmissible == "missing":
        facts = ()
    elif inadmissible == "stale":
        facts = (_orb_fact(goal, freshness="stale"),)
    elif inadmissible == "duplicate":
        facts = (fact, _orb_fact(goal, revision="orb-admission-2"))
    elif inadmissible == "enabled":
        facts = (_orb_fact(goal, enabled=True),)
    elif inadmissible == "wrong-session":
        facts = (_orb_fact(goal, provider_session_id="amp:some-other-lane:1"),)
    else:
        facts = (_orb_fact(goal, provider_handle="invented-before-create"),)

    run_recovery_supervision(
        _supervisor(tmp_path, goal, provider, facts, tophand_calls=tophand_calls)
    )

    assert provider.calls == []
    assert provider.pre_io_rows == []
    assert provider.create_request_handles == []
    assert JoinedLaneStore(tmp_path).load(goal.lane_id) is None
    assert tophand_calls == []


def test_restarted_public_supervisor_reuses_pending_orb_create_after_lost_response(
    tmp_path: Path,
) -> None:
    goal = _goal(tmp_path)
    facts = (_orb_fact(goal),)
    provider = _OrbLaunchProvider(tmp_path, lose_first_response=True)

    first = run_recovery_supervision(_supervisor(tmp_path, goal, provider, facts))
    pending = JoinedLaneStore(tmp_path).require(goal.lane_id).pending_operation

    assert first
    assert pending is not None
    assert len(provider.calls) == 1
    assert provider.calls[0].operation_id == pending.operation_id
    assert provider.calls[0].idempotency_key == pending.idempotency_key
    assert provider.calls[0].payload_digest == pending.payload_digest
    assert pending.provider_handle is None
    assert provider.create_request_handles == [None]
    assert len(provider.physical_creates) == 1

    second = run_recovery_supervision(_supervisor(tmp_path, goal, provider, facts))
    stored = JoinedLaneStore(tmp_path).require(goal.lane_id)

    assert second
    assert len(provider.calls) == 2
    assert provider.calls[1].model_dump(exclude={"attempt", "created_at"}) == provider.calls[0].model_dump(
        exclude={"attempt", "created_at"}
    )
    assert len(provider.physical_creates) == 1
    assert provider.create_request_handles == [None, None]
    assert stored.pending_operation is None
    assert stored.last_operation_result is not None
    assert stored.last_operation_result.operation_id == pending.operation_id
    assert stored.last_operation_result.provider_handle == CREATED_HANDLE
    assert stored.provider.handle == CREATED_HANDLE


@pytest.mark.parametrize("mismatch", ("session", "instance"))
def test_initial_amp_result_must_preserve_every_preallocated_identity_field(
    tmp_path: Path,
    mismatch: str,
) -> None:
    goal = _goal(tmp_path)
    provider = _OrbLaunchProvider(
        tmp_path,
        result_provider_session_id=("amp:wrong-session:1" if mismatch == "session" else None),
        result_instance_id=("amp-wrong-instance" if mismatch == "instance" else None),
    )

    run_recovery_supervision(_supervisor(tmp_path, goal, provider, (_orb_fact(goal),)))

    stored = JoinedLaneStore(tmp_path).require(goal.lane_id)
    assert stored.provider.handle is None
    assert stored.pending_operation is not None
    assert stored.pending_operation.provider_handle is None
    assert stored.last_operation_result is not None
    assert stored.last_operation_result.status == "unknown"


def test_restart_binds_stored_amp_result_without_another_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    goal = _goal(tmp_path)
    provider = _OrbLaunchProvider(tmp_path)
    original_save = RecoveryStateStore.save

    def crash_before_bind(
        store: RecoveryStateStore,
        record: JoinedLaneRecord,
        *,
        transition: RecordTransitionKind = "steady",
    ) -> JoinedLaneRecord:
        if transition == "initial-bind":
            raise RuntimeError("simulated crash after result persistence")
        return original_save(store, record, transition=transition)

    with monkeypatch.context() as context:
        context.setattr(RecoveryStateStore, "save", crash_before_bind)
        run_recovery_supervision(_supervisor(tmp_path, goal, provider, (_orb_fact(goal),)))

    interrupted = JoinedLaneStore(tmp_path).require(goal.lane_id)
    assert interrupted.provider.handle is None
    assert interrupted.pending_operation is not None
    assert interrupted.last_operation_result is not None
    assert interrupted.last_operation_result.provider_handle == CREATED_HANDLE
    assert len(provider.calls) == 1

    run_recovery_supervision(_supervisor(tmp_path, goal, provider, (_orb_fact(goal),)))

    resumed = JoinedLaneStore(tmp_path).require(goal.lane_id)
    assert len(provider.calls) == 1
    assert resumed.pending_operation is None
    assert resumed.provider.handle == CREATED_HANDLE


def test_concurrent_public_supervisors_issue_one_amp_create(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    facts = (_orb_fact(goal),)
    provider = _OrbLaunchProvider(tmp_path)
    barrier = threading.Barrier(2)
    decisions: list[object] = []

    def run() -> None:
        barrier.wait()
        decisions.append(run_recovery_supervision(_supervisor(tmp_path, goal, provider, facts)))

    threads = (threading.Thread(target=run), threading.Thread(target=run))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(decisions) == 2
    assert len(provider.calls) == 1
    assert JoinedLaneStore(tmp_path).require(goal.lane_id).provider.handle == CREATED_HANDLE

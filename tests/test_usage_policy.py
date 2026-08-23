"""Focused Amp launch, usage, and restart policy tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra.joined_lane import JoinedLaneReconciler, JoinedLaneStore
from chitra.session_contract import (
    ChildRosterEntry,
    ContractValidationError,
    JoinedLaneRecord,
    LaneLaunchPolicy,
    LaneUpdate,
    OperationReference,
    OperationStatus,
    PendingProviderOperation,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    UsageComponent,
    UsageReport,
    validate_record_transition,
)
from chitra.usage_policy import (
    AmpCreateSearchEvidence,
    evaluate_amp_create_policy,
    evaluate_usage_policy,
    expected_amp_create_tag,
)

NOW = datetime(2026, 8, 23, 15, 0, tzinfo=UTC)
PROFILE_DIGEST = f"sha256:{'a' * 64}"
OTHER_PROFILE_DIGEST = f"sha256:{'b' * 64}"


def child_roster() -> tuple[ChildRosterEntry, ...]:
    return (
        ChildRosterEntry(
            child_id="thread-child-a",
            parent_id="lane-a",
            ancestry=("lane-a", "thread-child-a"),
            retained_state="retained",
            material_result=True,
            material_result_ref="message:child-result-a",
        ),
    )


def provider() -> ProviderIdentity:
    return ProviderIdentity(
        kind="amp",
        handle="thread-parent-a",
        instance_id="amp-instance-a",
        generation=1,
        parent_thread_ref="thread-anchor-a",
        project_ref="amp-project:project-a",
        profile_digest=PROFILE_DIGEST,
        provider_version="0.0.1787241916-g56aafe",
        capabilities=ProviderCapabilities.from_supported(
            (
                "create_or_resume",
                "status",
                "send",
                "usage",
                "cancel_current_turn",
                "subagents",
                "parent_child_usage",
            )
        ),
    )


def launch_policy() -> LaneLaunchPolicy:
    return LaneLaunchPolicy(
        lane_id="lane-a",
        goal_id="goal-a",
        goal_version=1,
        project_ref="amp-project:project-a",
        profile_digest=PROFILE_DIGEST,
        provider_version="0.0.1787241916-g56aafe",
        cost_ceiling_usd=10.0,
        turn_reserve_usd=2.0,
        usage_poll_interval_seconds=30,
        usage_max_age_seconds=120,
        created_at="2026-08-23T14:00:00+00:00",
    )


def lane_record(*, policy: LaneLaunchPolicy | None = None) -> JoinedLaneRecord:
    update = LaneUpdate(
        lane_id="lane-a",
        goal_id="goal-a",
        session_ref="amp:lane-a:1",
        goal_version=1,
        sequence=1,
        observed_at="2026-08-23T14:59:00+00:00",
        plan_version=1,
        next_action="Continue the bounded lane turn",
        child_roster=child_roster(),
    )
    return JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-a",
        goal_version=1,
        session_ref="amp:lane-a:1",
        provider=provider(),
        launch_policy=policy,
        current_update=update,
    )


def usage_report(
    total: float,
    *,
    complete: bool = True,
    child_roster_complete: bool = True,
) -> UsageReport:
    roster = child_roster()
    return UsageReport(
        parent=UsageComponent(name="lane-a", amount=total - 1.0, unit="USD"),
        children=(UsageComponent(name="thread-child-a", amount=1.0, unit="USD"),),
        child_roster=roster,
        child_roster_complete=child_roster_complete,
        child_roster_evidence="evidence:children" if child_roster_complete else None,
        total=UsageComponent(name="lane-total", amount=total, unit="USD"),
        evidence_source="evidence:usage-rollup",
        observed_at="2026-08-23T14:59:30+00:00",
        complete=complete,
    )


def pre_create_record() -> JoinedLaneRecord:
    pending = PendingProviderOperation(
        operation_id="create-1",
        kind="create_or_resume",
        lane_id="lane-a",
        provider_handle="thread-parent-a",
        idempotency_key="idem-create-1",
        payload_digest="digest-create-1",
        provider_instance_id="amp-instance-a",
        provider_generation=1,
        created_at="2026-08-23T14:59:00+00:00",
    )
    reference = OperationReference(
        operation_id=pending.operation_id,
        idempotency_key=pending.idempotency_key,
        payload_digest=pending.payload_digest,
        kind=pending.kind,
        created_at=pending.created_at,
    )
    return JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-a",
        goal_version=1,
        session_ref="amp:lane-a:1",
        provider=provider(),
        launch_policy=launch_policy(),
        pending_operation=pending,
        operation_history=(reference,),
    )


def create_search(record: JoinedLaneRecord, matches: int) -> AmpCreateSearchEvidence:
    pending = record.pending_operation
    assert pending is not None
    return AmpCreateSearchEvidence(
        operation_id=pending.operation_id,
        create_tag=expected_amp_create_tag(record),
        match_count=matches,
        observed_at="2026-08-23T14:59:30+00:00",
        evidence=f"evidence:create-search:{matches}",
    )


def create_result(record: JoinedLaneRecord, *, status: OperationStatus) -> ProviderOperationResult:
    pending = record.pending_operation
    assert pending is not None
    accepted = None if status in {"unknown", "lost-response"} else True
    consumed = None if status in {"unknown", "lost-response"} else False
    return ProviderOperationResult(
        operation_id=pending.operation_id,
        kind=pending.kind,
        lane_id=pending.lane_id,
        provider_handle=pending.provider_handle,
        idempotency_key=pending.idempotency_key,
        payload_digest=pending.payload_digest,
        provider_instance_id=pending.provider_instance_id,
        provider_generation=pending.provider_generation,
        status=status,
        accepted=accepted,
        consumed=consumed,
        observed_at="2026-08-23T14:59:45+00:00",
        evidence=f"evidence:create-result:{status}",
    )


def authoritative_ownership(record: JoinedLaneRecord) -> dict[str, object]:
    return {
        "authoritative": True,
        "status": "authoritative",
        "provider_instance_id": record.provider.instance_id,
        "session_ref": record.session_ref,
        "lane_id": record.lane_id,
        "lane_generation": record.goal_version,
        "ownership_generation": record.chitra_ownership_epoch,
    }


def test_launch_policy_round_trips_inside_joined_lane() -> None:
    record = lane_record(policy=launch_policy())

    restored = JoinedLaneRecord.from_dict(record.to_dict())

    assert restored == record
    assert restored.launch_policy is not None
    assert restored.launch_policy.schema == "chitra.lane-launch-policy.v1"
    assert restored.launch_policy.cost_ceiling_usd == 10.0


def test_no_launch_policy_holds_amp_lane() -> None:
    decision = evaluate_usage_policy(lane_record(), None, now=NOW)

    assert decision.action == "unknown-and-hold"
    assert not decision.mutation_allowed
    assert not decision.cancel_required
    assert decision.reason == "Amp lane has no Chitra launch policy"


def test_profile_digest_mismatch_holds_amp_lane() -> None:
    mismatched = launch_policy().model_copy(update={"profile_digest": OTHER_PROFILE_DIGEST})
    record = lane_record(policy=launch_policy()).model_copy(update={"launch_policy": mismatched})

    decision = evaluate_usage_policy(record, usage_report(5.0), now=NOW)

    assert decision.action == "unknown-and-hold"
    assert not decision.mutation_allowed
    assert "profile digest" in decision.reason


def test_launch_policy_is_immutable_within_one_goal_version() -> None:
    record = lane_record(policy=launch_policy())
    changed = record.model_copy(
        update={
            "revision": 2,
            "launch_policy": launch_policy().model_copy(update={"cost_ceiling_usd": 20.0}),
        }
    )

    with pytest.raises(ContractValidationError, match="launch policy is immutable"):
        validate_record_transition(record, changed)


def test_incomplete_child_roster_holds_amp_lane() -> None:
    decision = evaluate_usage_policy(
        lane_record(policy=launch_policy()),
        usage_report(5.0, complete=False, child_roster_complete=False),
        now=NOW,
    )

    assert decision.action == "unknown-and-hold"
    assert not decision.mutation_allowed
    assert "complete child roster" in decision.reason


def test_reaching_ceiling_requires_cancel_and_hold() -> None:
    decision = evaluate_usage_policy(lane_record(policy=launch_policy()), usage_report(10.0), now=NOW)

    assert decision.action == "cancel-and-hold"
    assert not decision.mutation_allowed
    assert decision.cancel_required
    assert decision.report is not None
    assert decision.report.ceiling == 10.0


def test_reserve_blocks_overshoot_but_allows_exact_reserved_headroom() -> None:
    record = lane_record(policy=launch_policy())

    over_reserved = evaluate_usage_policy(record, usage_report(9.0), now=NOW)
    exactly_reserved = evaluate_usage_policy(record, usage_report(8.0), now=NOW)

    assert over_reserved.action == "hold"
    assert not over_reserved.mutation_allowed
    assert not over_reserved.cancel_required
    assert exactly_reserved.action == "allow"
    assert exactly_reserved.mutation_allowed
    assert exactly_reserved.report is not None
    assert exactly_reserved.report.ceiling == 10.0


def test_pre_create_policy_uses_exact_tag_cardinality_without_invented_usage() -> None:
    record = pre_create_record()

    assert expected_amp_create_tag(record) == "chitra-a4a35d0bcf13446027b54db6292c3deb"
    zero = evaluate_amp_create_policy(record, create_search(record, 0), now=NOW)
    one = evaluate_amp_create_policy(record, create_search(record, 1), now=NOW)
    many = evaluate_amp_create_policy(record, create_search(record, 2), now=NOW)

    assert (zero.action, zero.create_allowed) == ("create-once", True)
    assert (one.action, one.create_allowed) == ("adopt", False)
    assert one.provider_reconciliation_allowed
    assert (many.action, many.provider_reconciliation_allowed) == ("ambiguous-and-hold", False)


def test_zero_match_pre_create_search_allows_one_exact_retry_before_usage_exists(tmp_path: Path) -> None:
    record = pre_create_record()
    store = JoinedLaneStore(tmp_path)
    store.create(record)
    calls: list[str] = []

    def search_probe(current: JoinedLaneRecord) -> AmpCreateSearchEvidence:
        calls.append("search")
        return create_search(current, 0)

    def provider_probe(current: JoinedLaneRecord) -> ProviderOperationResult:
        calls.append("provider")
        return create_result(current, status="lost-response")

    def ownership_probe(current: JoinedLaneRecord) -> dict[str, object]:
        calls.append("ownership")
        return authoritative_ownership(current)

    def journal_probe(_record: JoinedLaneRecord) -> None:
        calls.append("journal")

    def retry_create(_pending: PendingProviderOperation) -> ProviderOperationResult:
        calls.append("create")
        return create_result(record, status="accepted")

    def usage_probe(_record: JoinedLaneRecord) -> None:
        calls.append("usage")

    reconciler = JoinedLaneReconciler(
        store,
        amp_create_search_probe=search_probe,
        provider_probe=provider_probe,
        ownership_probe=ownership_probe,
        journal_probe=journal_probe,
        retry_pending_operation=retry_create,
        usage_probe=usage_probe,
        now=lambda: NOW,
    )

    outcome = reconciler.reconcile_all().outcomes[0]

    assert outcome.status == "awaiting_ack"
    assert calls == ["search", "provider", "ownership", "journal", "create"]
    assert store.require("lane-a").pending_operation == record.pending_operation


def test_one_match_pre_create_search_requires_adoption_and_denies_retry(tmp_path: Path) -> None:
    record = pre_create_record()
    store = JoinedLaneStore(tmp_path)
    store.create(record)
    calls: list[str] = []

    def search_probe(current: JoinedLaneRecord) -> AmpCreateSearchEvidence:
        calls.append("search")
        return create_search(current, 1)

    def provider_probe(current: JoinedLaneRecord) -> ProviderOperationResult:
        calls.append("provider")
        return create_result(current, status="lost-response")

    def ownership_probe(current: JoinedLaneRecord) -> dict[str, object]:
        calls.append("ownership")
        return authoritative_ownership(current)

    def journal_probe(_record: JoinedLaneRecord) -> None:
        calls.append("journal")

    def retry_create(_pending: PendingProviderOperation) -> ProviderOperationResult:
        calls.append("create")
        return create_result(record, status="accepted")

    reconciler = JoinedLaneReconciler(
        store,
        amp_create_search_probe=search_probe,
        provider_probe=provider_probe,
        ownership_probe=ownership_probe,
        journal_probe=journal_probe,
        retry_pending_operation=retry_create,
        now=lambda: NOW,
    )

    outcome = reconciler.reconcile_all().outcomes[0]

    assert outcome.status == "blocked"
    assert outcome.reason == "one matching Amp thread must be adopted; create retry is denied"
    assert calls == ["search", "provider", "ownership", "journal"]


def test_ceiling_restart_marks_canonical_cancel_and_quiescence_as_pending(tmp_path: Path) -> None:
    record = lane_record(policy=launch_policy())
    store = JoinedLaneStore(tmp_path)
    store.create(record)
    calls: list[str] = []

    def called(name: str) -> None:
        calls.append(name)

    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: called("provider"),
        journal_probe=lambda _record: called("journal"),
        ownership_probe=lambda _record: called("ownership"),
        usage_probe=lambda _record: usage_report(10.0),
        now=lambda: NOW,
    )

    outcome = reconciler.reconcile_all().outcomes[0]

    assert outcome.status == "cancel_required"
    assert "supervisor must schedule a canonical cancel operation" in outcome.reason
    assert "verify provider quiescence" in outcome.reason
    assert calls == []


def test_restart_blocks_before_adapter_probe_or_retry_when_policy_is_missing(tmp_path: Path) -> None:
    pending = PendingProviderOperation(
        operation_id="send-1",
        kind="send",
        lane_id="lane-a",
        provider_handle="thread-parent-a",
        idempotency_key="idem-send-1",
        payload_digest="digest-send-1",
        provider_instance_id="amp-instance-a",
        provider_generation=1,
        created_at="2026-08-23T14:59:00+00:00",
    )
    reference = OperationReference(
        operation_id=pending.operation_id,
        idempotency_key=pending.idempotency_key,
        payload_digest=pending.payload_digest,
        kind=pending.kind,
        created_at=pending.created_at,
    )
    record = lane_record().model_copy(update={"pending_operation": pending, "operation_history": (reference,)})
    store = JoinedLaneStore(tmp_path)
    store.create(record)
    calls: list[str] = []

    def called(name: str) -> None:
        calls.append(name)

    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: called("provider"),
        journal_probe=lambda _record: called("journal"),
        ownership_probe=lambda _record: called("ownership"),
        retry_pending_operation=lambda _operation: called("retry"),
        usage_probe=lambda _record: called("usage"),
        now=lambda: NOW,
    )

    report = reconciler.reconcile_all()

    assert not report.ready
    assert len(report.blocked) == 1
    assert report.blocked[0].reason == "Amp lane has no Chitra launch policy"
    assert calls == []
    persisted = store.require("lane-a")
    assert persisted.pending_operation == pending
    assert persisted.operation_history == (reference,)

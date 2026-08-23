"""Deterministic tests for joined-lane durability and restart fencing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra.dispatchd import run_once
from chitra.joined_lane import (
    JoinedLaneConflictError,
    JoinedLaneCorruptError,
    JoinedLaneIdentityError,
    JoinedLaneReconciler,
    JoinedLaneRevisionError,
    JoinedLaneStore,
    ReconcileOutcome,
    ReconcileReport,
)
from chitra.orders import DispatchOrder, DispatchStatus
from chitra.session_contract import (
    JoinedLaneRecord,
    LaneUpdate,
    OperationReference,
    PendingProviderOperation,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
)


def provider(*, instance_id: str = "instance-a", generation: int = 1) -> ProviderIdentity:
    return ProviderIdentity(
        kind="tophand",
        handle="thread-a",
        instance_id=instance_id,
        generation=generation,
        capabilities=ProviderCapabilities.from_supported(("send", "read_updates")),
    )


def lane_update(*, sequence: int) -> LaneUpdate:
    return LaneUpdate(
        lane_id="lane-a",
        goal_id="goal-a",
        session_ref="tophand:lane-a",
        goal_version=1,
        sequence=sequence,
        observed_at="2026-08-23T14:00:00+00:00",
        plan_version=1,
        next_action="wait",
    )


def pending_operation(*, operation_id: str = "op-1", provider_instance_id: str = "instance-a") -> PendingProviderOperation:
    return PendingProviderOperation(
        operation_id=operation_id,
        kind="send",
        lane_id="lane-a",
        provider_handle="thread-a",
        idempotency_key=f"idem-{operation_id}",
        payload_digest=f"digest-{operation_id}",
        provider_instance_id=provider_instance_id,
        provider_generation=1,
        created_at="2026-08-23T14:00:00+00:00",
    )


def operation_result(
    pending: PendingProviderOperation,
    *,
    status: str = "accepted",
    accepted: bool | None = True,
    consumed: bool | None = False,
) -> ProviderOperationResult:
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
        observed_at="2026-08-23T14:00:01+00:00",
    )


def record(
    *,
    lane_id: str = "lane-a",
    goal_id: str = "goal-a",
    session_ref: str = "tophand:lane-a",
    ownership_epoch: int = 1,
    revision: int = 1,
    current_update: LaneUpdate | None = None,
    pending: PendingProviderOperation | None = None,
    result: ProviderOperationResult | None = None,
    provider_identity: ProviderIdentity | None = None,
    wake_condition: str | None = None,
) -> JoinedLaneRecord:
    operation = pending
    if operation is None and result is not None:
        operation = pending_operation(operation_id=result.operation_id)
    history = (
        OperationReference(
            operation_id=operation.operation_id,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            kind=operation.kind,
            created_at=operation.created_at,
        ),
    ) if operation is not None else ()
    next_check = (
        {
            "at": "2026-08-23T14:00:00+00:00",
            "reason": "Wait for wake condition",
            "wake_condition": wake_condition,
        }
        if wake_condition is not None
        else None
    )
    return JoinedLaneRecord(
        lane_id=lane_id,
        goal_id=goal_id,
        goal_version=1,
        session_ref=session_ref,
        chitra_ownership_epoch=ownership_epoch,
        provider=provider_identity or provider(),
        current_update=current_update,
        pending_operation=pending,
        last_operation_result=result,
        operation_history=history,
        next_check=next_check,
    ).model_copy(update={"revision": revision})


def accepted_observation(operation_id: str, *, status: str = "accepted", **updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "status": status,
        "operation_id": operation_id,
        "lane_id": "lane-a",
        "provider_handle": "thread-a",
        "provider_instance_id": "instance-a",
        "provider_generation": 1,
    }
    values.update(updates)
    return values


def ownership() -> dict[str, object]:
    return {
        "authoritative": True,
        "status": "authoritative",
        "provider_instance_id": "instance-a",
        "session_ref": "tophand:lane-a",
        "lane_id": "lane-a",
        "lane_generation": 1,
        "ownership_generation": 1,
    }


def fixed_now() -> datetime:
    return datetime(2026, 8, 23, 14, 0, tzinfo=UTC)


def test_atomic_write_keeps_previous_valid_document_for_corrupt_newest(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    store.create(record(revision=1, current_update=lane_update(sequence=1)))
    store.save(record(revision=2, current_update=lane_update(sequence=2)))

    store.path("lane-a").write_text("{not-json", encoding="utf-8")
    loaded = store.load_with_source("lane-a")
    assert loaded is not None and loaded.source == "previous"
    assert loaded.record.revision == 1
    assert store.previous_path("lane-a").exists()

    store.save(record(revision=3, current_update=lane_update(sequence=3)))
    assert store.load("lane-a").revision == 3
    assert store.load_with_source("lane-a").source == "current"

    store.previous_path("lane-a").write_text("broken", encoding="utf-8")
    store.path("lane-a").write_text("broken", encoding="utf-8")
    with pytest.raises(JoinedLaneCorruptError):
        store.load("lane-a")


def test_corrupt_newest_without_predecessor_fails_closed(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    store.path("lane-a").parent.mkdir(parents=True)
    store.path("lane-a").write_text("{not-json", encoding="utf-8")
    with pytest.raises(JoinedLaneCorruptError):
        store.load("lane-a")


def test_legacy_wire_schema_is_rejected_without_migration(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    store.path("lane-a").parent.mkdir(parents=True)
    store.path("lane-a").write_text('{"schema":"chitra.joined_lane.v1"}', encoding="utf-8")
    with pytest.raises(JoinedLaneCorruptError):
        store.load("lane-a")


def test_store_rejects_stale_revision_and_sequence(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    store.create(record(revision=1, current_update=lane_update(sequence=1)))
    with pytest.raises(JoinedLaneRevisionError):
        store.save(record(revision=1, current_update=lane_update(sequence=2)))
    with pytest.raises(JoinedLaneRevisionError):
        store.save(record(revision=2, current_update=lane_update(sequence=0)))
    with pytest.raises(JoinedLaneConflictError):
        store.save(record(revision=2, current_update=lane_update(sequence=2)), expected_revision=9)


def test_store_rejects_goal_changes_and_ownership_epoch_rollback(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    store.create(record(ownership_epoch=2))
    with pytest.raises(JoinedLaneIdentityError, match="goal_id"):
        store.save(record(goal_id="goal-b", revision=2))
    with pytest.raises(JoinedLaneRevisionError, match="ownership epoch"):
        store.save(record(ownership_epoch=1, revision=2))


def test_store_rejects_duplicate_active_provider_owners_atomically(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    store.create(record())
    with pytest.raises(JoinedLaneConflictError, match="active provider owner"):
        store.create(record(lane_id="lane-b", session_ref="tophand:lane-b"))


def test_provider_acceptance_without_durable_ack_is_held(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    pending = pending_operation()
    store.create(record(pending=pending))
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: accepted_observation("op-1"),
        ownership_probe=lambda _record: ownership(),
        now=fixed_now,
        next_check_delay_seconds=10,
    )
    outcome = reconciler.reconcile_all().outcomes[0]
    assert (outcome.status, outcome.send_allowed) == ("awaiting_ack", False)
    saved = store.require("lane-a")
    assert saved.pending_operation.operation_id == "op-1"
    assert saved.next_check.at == "2026-08-23T14:00:10+00:00"


def test_sent_direction_without_journal_observation_is_not_replayed(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    pending = pending_operation()
    store.create(record(pending=pending, result=operation_result(pending)))
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: accepted_observation("op-1"),
        journal_probe=lambda _record: accepted_observation("op-1", status="sent"),
        ledger_probe=lambda _record: accepted_observation("op-1", status="acknowledged"),
        ownership_probe=lambda _record: ownership(),
    )
    outcome = reconciler.reconcile_all().outcomes[0]
    assert (outcome.status, outcome.send_allowed) == ("sent_unobserved", False)


def test_exact_journal_observation_allows_progress_but_ledger_alone_does_not(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    pending = pending_operation()
    store.create(record(pending=pending))
    probes = {"journal": lambda _record: None}
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: accepted_observation("op-1"),
        journal_probe=lambda current: probes["journal"](current),
        ledger_probe=lambda _record: accepted_observation("op-1", status="observed"),
        ownership_probe=lambda _record: ownership(),
        now=fixed_now,
    )
    held = reconciler.reconcile_all().outcomes[0]
    assert not held.send_allowed

    probes["journal"] = lambda _record: accepted_observation("op-1", status="observed", event_id="evt-1")
    observed = reconciler.reconcile_all().outcomes[0]
    assert (observed.status, observed.send_allowed) == ("observed", True)
    assert store.require("lane-a").last_operation_result.status == "consumed"


def test_identity_mismatch_is_durable_and_fail_closed(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    pending = pending_operation()
    store.create(record(pending=pending))
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: accepted_observation("op-1", provider_instance_id="new-instance"),
        ownership_probe=lambda _record: ownership(),
        now=fixed_now,
    )
    outcome = reconciler.reconcile_all().outcomes[0]
    assert (outcome.status, outcome.send_allowed) == ("identity_mismatch", False)
    saved = store.require("lane-a")
    assert saved.next_check.at == "2026-08-23T14:00:30+00:00"
    assert "mismatch" in saved.recovery.failure_signature


def test_missing_provider_or_ownership_evidence_fails_closed(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    store.create(record(pending=pending_operation()))
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: None,
        ownership_probe=lambda _record: None,
        now=fixed_now,
    )
    outcome = reconciler.reconcile_all().outcomes[0]
    assert (outcome.status, outcome.send_allowed) == ("blocked", False)
    assert store.require("lane-a").next_check is not None


def test_non_authoritative_ownership_evidence_fails_closed(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    store.create(record(pending=pending_operation()))
    bad_ownership = ownership()
    bad_ownership.pop("authoritative")
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: accepted_observation("op-1"),
        ownership_probe=lambda _record: bad_ownership,
        now=fixed_now,
    )
    outcome = reconciler.reconcile_all().outcomes[0]
    assert (outcome.status, outcome.send_allowed) == ("identity_mismatch", False)


def test_active_lane_with_consumed_history_still_fences_provider_generation(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    pending = pending_operation()
    store.create(record(pending=pending, result=operation_result(pending, status="consumed", consumed=True)))
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: accepted_observation("op-1", provider_generation=2),
        ownership_probe=lambda _record: ownership(),
        now=fixed_now,
    )
    outcome = reconciler.reconcile_all().outcomes[0]
    assert (outcome.status, outcome.send_allowed) == ("identity_mismatch", False)
    assert store.require("lane-a").next_check is not None


def test_wake_is_idempotent_and_preserves_operation_identity(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    pending = pending_operation()
    store.create(record(pending=pending, result=operation_result(pending)))
    reconciler = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: accepted_observation("op-1"),
        ledger_probe=lambda _record: accepted_observation("op-1", status="acknowledged"),
        ownership_probe=lambda _record: ownership(),
        now=fixed_now,
    )
    first = reconciler.wake("lane-a", wake_id="wake-1")
    second = reconciler.wake("lane-a", wake_id="wake-1")
    assert first.status == "sent_unobserved"
    assert second.status == "wake_reused"
    saved = store.require("lane-a")
    assert saved.pending_operation.operation_id == "op-1"
    assert saved.last_intervention is not None
    assert saved.last_intervention.operation_id == "wake-1"


def test_reconcile_report_blocks_dispatch_barrier_for_matching_session() -> None:
    report = ReconcileReport((ReconcileOutcome("lane-a", "tophand:lane-a", "blocked", False, "identity mismatch"),))
    assert not report.allows("tophand:lane-a")
    assert not report.allows("tophand:other")


def test_dispatchd_runs_restart_gate_before_claim_and_defers_blocked_order(tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    orders = queue / "orders"
    orders.mkdir(parents=True)
    order = DispatchOrder(order_id="op-1", session_ref="tophand:lane-a", nudge="continue")
    order_path = orders / "op-1.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")
    report = ReconcileReport((ReconcileOutcome("lane-a", "tophand:lane-a", "blocked", False, "identity mismatch"),))
    called = False

    def gate() -> ReconcileReport:
        nonlocal called
        called = True
        assert order_path.exists()
        return report

    results = run_once(queue, reconciliation_gate=gate)
    assert called
    assert results[0].status == DispatchStatus.DEFERRED
    assert not order_path.exists()
    assert (queue / "deferred" / "op-1.json").exists()


def test_dispatchd_without_reconciler_fails_closed_when_unfinished_lane_exists(tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    orders = queue / "orders"
    orders.mkdir(parents=True)
    JoinedLaneStore(queue).create(record(pending=pending_operation()))
    order = DispatchOrder(order_id="op-1", session_ref="tophand:lane-a", nudge="continue")
    order_path = orders / "op-1.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")

    results = run_once(queue)
    assert results[0].status == DispatchStatus.DEFERRED
    assert (queue / "deferred" / "op-1.json").exists()

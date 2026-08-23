"""Deterministic tests for joined-lane durability and restart fencing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import chitra.dispatchd as dispatchd
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
    filesystem_provider_probe,
    ledger_provider_probe,
    ownership_provider_probe,
)
from chitra.journal.models import CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.journal.store import EventJournal
from chitra.lane_config import LaneCredentials, LaneSpec
from chitra.ledger import LedgerEntry, append_entry
from chitra.orders import DispatchOrder, DispatchStatus
from chitra.provider_protocol import ProviderUpdate, UpdateKind
from chitra.session_contract import (
    CloseResult,
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


def append_wake_event(root: Path, wake_id: str, condition: str) -> None:
    EventJournal(root, "lane-a").append(
        (
            CanonicalEvent(
                event_id=wake_id,
                instance="test",
                lane="lane-a",
                client=Client.CODEX,
                client_version="test",
                process_id="1",
                transcript=TranscriptIdentity(path="/tmp/lane-a", device=1, inode=1),
                session_id="tophand:lane-a",
                resume_id=None,
                observed_at="2026-08-23T14:00:01+00:00",
                native_time=None,
                native_type="wake_condition_changed",
                native_join_id=None,
                raw_byte_range=None,
                raw_sha256=None,
                normalized_type=CanonicalType.UNKNOWN,
                goal_ref="goal-a",
                goal_version=1,
                item_ref=None,
                payload_digest="a" * 64,
                normalizer_version="test",
                payload={"wake_condition": condition, "wake_condition_changed": True},
                raw_record=None,
            ),
        )
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


def accepted_observation(
    operation_id: str,
    *,
    status: str = "accepted",
    provider_instance_id: str = "instance-a",
    provider_generation: int = 1,
) -> ProviderOperationResult:
    if status == "unknown":
        accepted: bool | None = None
        consumed: bool | None = None
    elif status == "lost-response":
        accepted = None
        consumed = None
    elif status == "rejected":
        accepted = False
        consumed = False
    else:
        accepted = True
        consumed = status == "consumed"
        status = "consumed" if consumed else "accepted"
    return ProviderOperationResult(
        operation_id=operation_id,
        kind="send",
        lane_id="lane-a",
        provider_handle="thread-a",
        idempotency_key=f"idem-{operation_id}",
        payload_digest=f"digest-{operation_id}",
        provider_instance_id=provider_instance_id,
        provider_generation=provider_generation,
        status=status,
        accepted=accepted,
        consumed=consumed,
        observed_at="2026-08-23T14:00:01+00:00",
    )


def journal_observation(operation_id: str, *, consumed: bool | None, event_id: str = "evt-1") -> ProviderUpdate:
    return ProviderUpdate(
        event_id=event_id,
        cursor="1",
        kind=UpdateKind.STEER_CONSUMED if consumed is True else UpdateKind.STEER_ACCEPTED,
        provider_session_id="tophand:lane-a",
        observed_at="2026-08-23T14:00:02+00:00",
        operation_id=operation_id,
        lane_id="lane-a",
        idempotency_key=f"idem-{operation_id}",
        payload_digest=f"digest-{operation_id}",
        provider_instance_id="instance-a",
        provider_generation=1,
        provider_handle="thread-a",
        payload={"result_evidence": {"accepted": True, "consumed": consumed}},
    )


def ledger_observation(operation_id: str) -> LedgerEntry:
    return LedgerEntry(
        order_id=operation_id,
        session_ref="tophand:lane-a",
        tag="[C]",
        message_hash="digest",
        sent_at="2026-08-23T14:00:02+00:00",
        signature="signature",
    )


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


def test_previous_only_document_is_discovered_and_reconciled(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    pending = pending_operation()
    store.create(record(pending=pending))
    store.previous_path("lane-a").write_text(store.path("lane-a").read_text(encoding="utf-8"), encoding="utf-8")
    store.path("lane-a").unlink()
    assert [item.lane_id for item in store.unfinished()] == ["lane-a"]
    report = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: accepted_observation("op-1"),
        journal_probe=lambda _record: None,
        ownership_probe=lambda _record: ownership(),
    ).reconcile_all()
    assert report.outcomes[0].lane_id == "lane-a"


def test_lost_reply_retries_same_pending_operation_without_duplicate(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    pending = pending_operation()
    store.create(record(pending=pending))
    seen: list[PendingProviderOperation] = []

    def retry(operation: PendingProviderOperation) -> ProviderOperationResult:
        seen.append(operation)
        return accepted_observation(operation.operation_id)

    outcome = JoinedLaneReconciler(
        store,
        provider_probe=lambda _record: accepted_observation("op-1", status="lost-response"),
        journal_probe=lambda _record: None,
        ownership_probe=lambda _record: ownership(),
        retry_pending_operation=retry,
    ).reconcile_all().outcomes[0]
    assert outcome.status == "awaiting_ack"
    assert seen == [pending]
    assert store.require("lane-a").pending_operation.operation_id == "op-1"


def test_filesystem_provider_and_verified_ledger_adapters_fence_exact_operation(tmp_path: Path) -> None:
    pending = pending_operation()
    current = record(pending=pending)
    result = accepted_observation("op-1")
    provider_path = tmp_path / "provider-results" / "lane-a.jsonl"
    provider_path.parent.mkdir(parents=True)
    provider_path.write_text(result.model_dump_json() + "\n", encoding="utf-8")
    provider_probe = filesystem_provider_probe(tmp_path)
    assert provider_probe(current) == result

    key = b"adapter-test-key"
    ledger_path = tmp_path / "ledger.jsonl"
    key_path = tmp_path / "ledger.key"
    key_path.write_bytes(key)
    append_entry(
        ledger_path,
        order_id="op-1",
        session_ref=current.session_ref,
        tag="[C]",
        nudge="continue",
        key=key,
        sent_at="2026-08-23T14:00:02+00:00",
    )
    entry = ledger_provider_probe(ledger_path, key_path)(current)
    assert entry is not None and entry.order_id == pending.operation_id


def test_ownership_adapter_uses_local_socket_for_twinridge_lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def local_request(socket_path: Path, query: object) -> object:
        seen["socket"] = socket_path
        seen["query"] = query
        return {"status": "owned", "authoritative": True, "host_id": "twinridge", "boot_id": "boot-local"}

    monkeypatch.setattr("chitra.joined_lane.request_json_line", local_request)
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("boot-local\n", encoding="utf-8")
    probe = ownership_provider_probe(local_extra={"twinridge"}, boot_id_path=boot_id_path)
    response = probe(record(session_ref="twinridge:lane-a"))
    assert response is not None
    assert seen["query"]["host_id"] == "twinridge"
    assert seen["query"]["session_ref"] == "twinridge:lane-a"


def test_ownership_adapter_uses_batchmode_ssh_for_remote_tophand_lane() -> None:
    seen: list[list[str]] = []

    def remote_runner(command: list[str]) -> object:
        seen.append(command)
        return type("Completed", (), {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "host_id": "tophand",
                    "boot_id": "boot-remote",
                    "result": {"session_ref": "tophand:lane-a", "status": "owned"},
                }
            ),
        })()

    probe = ownership_provider_probe(local_extra={"twinridge"}, remote_runner=remote_runner)
    assert probe(record(session_ref="tophand:lane-a")) is not None
    assert seen and seen[0][0] == "ssh"
    assert "BatchMode=yes" in seen[0]
    assert seen[0][-1] == "chitra-ownership-query --session-ref tophand:lane-a"


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
        journal_probe=lambda _record: None,
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
        journal_probe=lambda _record: journal_observation("op-1", consumed=None),
        ledger_probe=lambda _record: ledger_observation("op-1"),
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
        ledger_probe=lambda _record: ledger_observation("op-1"),
        ownership_probe=lambda _record: ownership(),
        now=fixed_now,
    )
    held = reconciler.reconcile_all().outcomes[0]
    assert not held.send_allowed

    probes["journal"] = lambda _record: journal_observation("op-1", consumed=True, event_id="evt-1")
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
        journal_probe=lambda _record: None,
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
        journal_probe=lambda _record: None,
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
        journal_probe=lambda _record: None,
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
        journal_probe=lambda _record: None,
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
        journal_probe=lambda _record: None,
        ledger_probe=lambda _record: ledger_observation("op-1"),
        ownership_probe=lambda _record: ownership(),
        now=fixed_now,
    )
    append_wake_event(tmp_path, "wake-1", "a new safe provider fact or material lane update")
    first = reconciler.wake("lane-a", wake_id="wake-1", event_sequence=1)
    second = reconciler.wake("lane-a", wake_id="wake-1", event_sequence=1)
    assert first.status == "sent_unobserved"
    assert second.status == "wake_reused"
    saved = store.require("lane-a")
    assert saved.pending_operation.operation_id == "op-1"
    assert saved.last_intervention is not None
    assert saved.last_intervention.operation_id == "wake-1"
    assert saved.next_check is not None
    assert saved.next_check.wake_condition != "wake-1"


def test_chitra_state_update_preserves_identical_lane_authored_snapshot(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    authored = lane_update(sequence=1)
    first = record(current_update=authored)
    store.create(first)
    updated = first.model_copy(
        update={"revision": 2, "recovery": first.recovery.model_copy(update={"stage": "waiting"})}
    )
    saved = store.save(updated)
    assert saved.current_update == authored
    assert saved.problems == authored.problems


def test_store_requires_explicit_transfer_and_resume_transition_kinds(tmp_path: Path) -> None:
    store = JoinedLaneStore(tmp_path)
    first_provider = provider(instance_id="instance-a", generation=1)
    first = record(provider_identity=first_provider, session_ref="tophand:lane-a:1").model_copy(
        update={"physical_session_generation": 1}
    )
    store.create(first)
    transferred_provider = provider(instance_id="instance-b", generation=2)
    transferred = first.model_copy(
        update={
            "revision": 2,
            "session_ref": "tophand:lane-a:2",
            "provider": transferred_provider,
            "chitra_ownership_epoch": 2,
            "physical_session_generation": 2,
        }
    )
    with pytest.raises(JoinedLaneIdentityError, match="session_ref"):
        store.save(transferred)
    assert store.save(transferred, transition="provider-transfer").session_ref == "tophand:lane-a:2"


def test_steady_inactive_to_active_is_forbidden_but_store_resume_is_typed(tmp_path: Path) -> None:
    close = CloseResult.model_validate(
        {
            "operation_id": "close-1",
            "lane_id": "lane-a",
            "provider_handle": "thread-a",
            "provider_instance_id": "instance-a",
            "provider_generation": 1,
            "idempotency_key": "idem-close-1",
            "payload_digest": "digest-close-1",
            "state": "archived",
            "provider_thread_ref": "thread-a",
            "same_provider_thread": True,
            "later_resume_supported": True,
            "checkpoint_ref": "checkpoint-1",
            "quiescent": True,
            "observed_at": "2026-08-23T14:00:01+00:00",
            "evidence": "archive",
        },
        strict=True,
    )
    resume_provider = ProviderIdentity(
        kind="amp",
        handle="thread-a",
        instance_id="instance-a",
        generation=1,
        capabilities=ProviderCapabilities.from_supported(("create_or_resume", "close", "checkpoint", "resume_after_close")),
    )
    history = (
        OperationReference(
            operation_id="close-1",
            idempotency_key="idem-close-1",
            payload_digest="digest-close-1",
            kind="close",
            created_at="2026-08-23T14:00:00+00:00",
        ),
    )
    inactive = JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-a",
        goal_version=1,
        session_ref="amp:lane-a:1",
        lifecycle="inactive",
        provider=resume_provider,
        operation_history=history,
        last_close_result=close,
    )
    store = JoinedLaneStore(tmp_path)
    store.create(inactive)
    active = inactive.model_copy(update={"revision": 2, "lifecycle": "active", "last_close_result": None})
    with pytest.raises(JoinedLaneRevisionError, match="resume"):
        store.save(active)
    assert store.save(active, transition="resume").lifecycle == "active"


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


def test_dispatchd_requeues_matching_joined_lane_defer_when_barrier_clears(tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    orders = queue / "orders"
    orders.mkdir(parents=True)
    order = DispatchOrder(order_id="op-1", session_ref="tophand:lane-a", nudge="continue")
    orders.joinpath("op-1.json").write_text(order.model_dump_json(), encoding="utf-8")
    blocked = ReconcileReport((ReconcileOutcome("lane-a", order.session_ref, "blocked", False, "identity mismatch"),))
    assert run_once(queue, reconciliation_gate=lambda: blocked)[0].status == DispatchStatus.DEFERRED
    allowed = ReconcileReport((ReconcileOutcome("lane-a", order.session_ref, "observed", True),))
    results = run_once(queue, reconciliation_gate=lambda: allowed)
    assert results[0].order_id == "op-1"
    assert not (queue / "deferred" / ".op-1.joined-lane.json").exists()


def test_main_wires_startup_reconciler_before_run_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()
    monkeypatch.setattr(dispatchd, "build_filesystem_reconciler", lambda root, **kwargs: sentinel)

    def fake_run_once(queue_dir: Path, **kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(dispatchd, "run_once", fake_run_once)
    assert dispatchd.main(["--once", "--queue-dir", str(tmp_path / "queue")]) == 0
    assert captured["joined_lane_reconciler"] is sentinel


def test_lanes_file_entrypoint_reconciles_before_claim_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped multi-lane mode cannot bypass the restart send barrier."""
    state_root = tmp_path / "lane-state"
    lane = LaneSpec(
        identifier="alpha",
        account="alpha",
        uid=1000,
        home=tmp_path / "home",
        workdir=tmp_path / "workdir",
        config_dir=tmp_path / "config",
        state_dir=state_root,
        tmux_socket=tmp_path / "alpha.sock",
        tmux_session="alpha",
        credentials=LaneCredentials(
            claude_credentials=tmp_path / "credentials.json",
            ssh_dispatch_key=tmp_path / "dispatch.key",
        ),
    )
    queue = lane.queue_dir
    queue_orders = queue / "orders"
    queue_orders.mkdir(parents=True)
    order = DispatchOrder(order_id="op-lane-entrypoint", session_ref="tophand:alpha:0.0", nudge="continue")
    order_path = queue_orders / f"{order.order_id}.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")
    barrier = ReconcileReport((ReconcileOutcome("alpha", order.session_ref, "blocked", False, "unreconciled"),))
    reconciler_calls = 0
    built_roots: list[Path] = []

    class FakeReconciler:
        def reconcile_all(self) -> ReconcileReport:
            nonlocal reconciler_calls
            reconciler_calls += 1
            assert order_path.exists()
            return barrier

    monkeypatch.setattr("chitra.lane_config.enabled_lanes", lambda _path: (lane,))

    def build(root: Path, **_kwargs: object) -> FakeReconciler:
        built_roots.append(root)
        return FakeReconciler()

    monkeypatch.setattr(dispatchd, "build_filesystem_reconciler", build)
    monkeypatch.setattr(
        dispatchd,
        "dispatch_to_tmux",
        lambda *_args, **_kwargs: pytest.fail("blocked lane reached provider dispatch"),
    )

    assert dispatchd.main(["--lanes-file", str(tmp_path / "lanes.yaml"), "--once"]) == 0
    assert built_roots == [state_root]
    assert reconciler_calls == 2
    assert not order_path.exists()
    assert (queue / "deferred" / order_path.name).exists()
    assert not (queue / "results" / order_path.name).exists()


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

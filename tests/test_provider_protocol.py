"""Focused contract tests for the provider lifecycle seam and its fakes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import cast

import pytest
from _provider_fakes import (
    AMP_FIXTURES,
    FAKE_SCENARIOS,
    TOPHAND_FIXTURES,
    FakeAmpProvider,
    FakeTophandProvider,
    fake_provider_scenario,
)

from chitra.provider_protocol import (
    CancelCurrentTurnRequest as _CancelCurrentTurnRequest,
)
from chitra.provider_protocol import (
    CheckpointRequest as _CheckpointRequest,
)
from chitra.provider_protocol import (
    ChildRosterEntry,
    OperationKind,
    PendingProviderOperation,
    Provider,
    ProviderCapabilities,
    ProviderState,
    ProviderUpdate,
    UpdateKind,
)
from chitra.provider_protocol import (
    CloseRequest as _CloseRequest,
)
from chitra.provider_protocol import (
    CreateOrResumeRequest as _CreateOrResumeRequest,
)
from chitra.provider_protocol import (
    SendRequest as _SendRequest,
)


def _operation(
    kind: str, operation: object, idempotency_key: str | None, payload: tuple[object, ...], **kwargs: object
) -> PendingProviderOperation:
    if isinstance(operation, PendingProviderOperation):
        if idempotency_key is not None:
            raise ValueError("nested operation envelope cannot be combined with operation identity fields")
        return operation
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation_id must be supplied")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key must be supplied")
    lane_id = str(kwargs.get("lane_id", "host:lane:instance"))
    provider_instance_id = str(kwargs.get("provider_instance_id", "default"))
    provider_generation = int(str(kwargs.get("provider_generation", 1)))
    provider_handle = str(kwargs.get("provider_handle", "provider"))
    created_at = str(kwargs.get("created_at", "2026-01-01T00:00:00Z"))
    attempt = int(str(kwargs.get("attempt", 1)))
    digest = (
        str(kwargs.get("payload_digest", ""))
        or hashlib.sha256("|".join((operation, kind, idempotency_key, lane_id, *(str(value) for value in payload))).encode()).hexdigest()
    )
    return PendingProviderOperation(
        operation_id=operation,
        kind=cast(OperationKind, kind),
        lane_id=lane_id,
        provider_handle=provider_handle,
        idempotency_key=idempotency_key,
        payload_digest=digest,
        provider_instance_id=provider_instance_id,
        provider_generation=provider_generation,
        created_at=created_at,
        attempt=attempt,
    )


def CreateOrResumeRequest(
    operation: object = None,
    idempotency_key: str | None = None,
    session_ref: str = "",
    provider_session_id: str | None = None,
    context_ref: str | None = None,
    **kwargs: object,
) -> _CreateOrResumeRequest:
    return _CreateOrResumeRequest(
        operation=_operation("create_or_resume", operation, idempotency_key, (session_ref, provider_session_id, context_ref), **kwargs),
        session_ref=session_ref,
        provider_session_id=provider_session_id,
        context_ref=context_ref,
    )


def SendRequest(operation: object = None, idempotency_key: str | None = None, text: str = "", **kwargs: object) -> _SendRequest:
    return _SendRequest(operation=_operation("send", operation, idempotency_key, (text,), **kwargs), text=text)


def CheckpointRequest(
    operation: object = None, idempotency_key: str | None = None, label: str = "checkpoint", **kwargs: object
) -> _CheckpointRequest:
    return _CheckpointRequest(operation=_operation("checkpoint", operation, idempotency_key, (label,), **kwargs), label=label)


def CancelCurrentTurnRequest(
    operation: object = None, idempotency_key: str | None = None, reason: str = "cancelled by monitor", **kwargs: object
) -> _CancelCurrentTurnRequest:
    return _CancelCurrentTurnRequest(
        operation=_operation("cancel_current_turn", operation, idempotency_key, (reason,), **kwargs), reason=reason
    )


def CloseRequest(operation: object = None, idempotency_key: str | None = None, archive: bool = True, **kwargs: object) -> _CloseRequest:
    return _CloseRequest(operation=_operation("close", operation, idempotency_key, (archive,), **kwargs), archive=archive)


def _create(provider: Provider) -> None:
    result = provider.create_or_resume(CreateOrResumeRequest("create-1", "idem-create-1", "host:lane:instance"))
    assert result.accepted is True
    assert result.consumed is True


@pytest.mark.parametrize("provider_type", [FakeTophandProvider, FakeAmpProvider])
def test_both_provider_doubles_implement_the_same_lifecycle_protocol(provider_type: type[Provider]) -> None:
    provider = provider_type()
    assert isinstance(provider, Provider)
    _create(provider)
    assert provider.status().state is ProviderState.IDLE
    assert provider.usage().complete is True


def test_mutating_requests_require_operation_and_idempotency_identity() -> None:
    with pytest.raises(ValueError, match="operation_id"):
        CreateOrResumeRequest("", "idem")
    with pytest.raises(ValueError, match="idempotency_key"):
        SendRequest("send-1", "", "hello")
    with pytest.raises(ValueError, match="text"):
        SendRequest("send-1", "idem-send-1", "")

    first = SendRequest("send-1", "idem-send-1", "hello")
    second = SendRequest("send-1", "idem-send-1", "goodbye")
    assert first.payload_digest != second.payload_digest

    provider = FakeTophandProvider()
    _create(provider)
    provider.send(first)
    with pytest.raises(ValueError, match="operation_id"):
        provider.send(SendRequest("send-1", "idem-send-2", "hello"))


def test_lost_response_is_unknown_until_the_same_idempotent_request_reconciles() -> None:
    provider = FakeAmpProvider("lost_response")
    request = CreateOrResumeRequest("create-1", "idem-create-1")

    lost = provider.create_or_resume(request)
    assert lost.status == "lost-response"
    assert lost.accepted is None
    assert lost.consumed is None
    assert lost.evidence == "response_lost_after_provider_acceptance"

    replay = provider.create_or_resume(request)
    assert replay.status == "consumed"
    assert replay.accepted is True
    assert replay.consumed is True
    assert len(provider.events) == 1


def test_send_acceptance_is_not_consumption_and_idempotency_prevents_duplicate_events() -> None:
    provider = FakeTophandProvider("steer_not_consumed")
    _create(provider)
    request = SendRequest("send-1", "idem-send-1", "please continue")

    first = provider.send(request)
    second = provider.send(request)
    assert first.accepted is True
    assert first.consumed is None
    assert second.status == "accepted"
    assert len(provider.events) == 2  # session creation + one accepted steer
    assert [event.kind for event in provider.read_updates().updates] == [
        UpdateKind.SESSION_CREATED,
        UpdateKind.STEER_ACCEPTED,
    ]


def test_send_consumption_is_only_observed_by_a_later_read() -> None:
    provider = FakeTophandProvider()
    _create(provider)
    request = SendRequest("send-1", "idem-send-1", "please continue")

    accepted = provider.send(request)
    assert accepted.status == "accepted"
    assert accepted.consumed is None
    assert [event.kind for event in provider.events] == [UpdateKind.SESSION_CREATED, UpdateKind.STEER_ACCEPTED]

    observed = provider.read_updates("1")
    assert [event.kind for event in observed.updates] == [UpdateKind.STEER_ACCEPTED, UpdateKind.STEER_CONSUMED]
    assert observed.updates[-1].operation_id == request.operation_id
    result_evidence = cast(Mapping[str, object], observed.updates[-1].payload["result_evidence"])
    assert result_evidence["consumed"] is True


def test_operation_envelope_and_update_identity_are_retained() -> None:
    provider = FakeTophandProvider(provider_instance_id="tophand-a")
    create = CreateOrResumeRequest(
        "create-1",
        "idem-create-1",
        provider_instance_id="tophand-a",
        provider_generation=1,
    )
    result = provider.create_or_resume(create)
    assert result.operation_id == create.operation_id
    assert result.provider_handle == provider.session_id
    assert result.idempotency_key == create.idempotency_key
    assert result.payload_digest == create.payload_digest
    assert result.provider_instance_id == "tophand-a"
    assert result.provider_generation == 1

    update = provider.read_updates().updates[0]
    assert update.operation_id == create.operation_id
    assert update.lane_id == create.lane_id
    assert update.idempotency_key == create.idempotency_key
    assert update.payload_digest == create.payload_digest
    assert update.provider_instance_id == "tophand-a"
    assert update.child_roster == ()
    result_evidence = cast(Mapping[str, object], update.payload["result_evidence"])
    assert result_evidence["consumed"] is True


def test_requests_can_carry_the_canonical_pending_operation_envelope() -> None:
    envelope = PendingProviderOperation(
        operation_id="send-1",
        kind="send",
        lane_id="host:lane:instance",
        provider_handle="tophand",
        idempotency_key="idem-send-1",
        payload_digest="digest-send-1",
        provider_instance_id="tophand-a",
        provider_generation=1,
        created_at="2026-01-01T00:00:00Z",
    )
    request = SendRequest(operation=envelope, text="continue")
    assert request.operation is envelope
    assert request.operation_id == envelope.operation_id
    assert request.payload_digest == envelope.payload_digest


def test_updates_carry_canonical_child_roster_evidence() -> None:
    child = ChildRosterEntry(
        child_id="child-1",
        parent_id="parent-1",
        ancestry=("parent-1", "child-1"),
        retained_state="retained",
        material_result=True,
        material_result_ref="result-1",
    )
    update = ProviderUpdate(
        event_id="event-1",
        cursor="1",
        kind=UpdateKind.PROGRESS_CLAIM,
        provider_session_id="tophand-session-1",
        observed_at="2026-01-01T00:00:00Z",
        operation_id="send-1",
        lane_id="host:lane:instance",
        idempotency_key="idem-send-1",
        payload_digest="digest-send-1",
        provider_instance_id="tophand-a",
        provider_generation=1,
        child_roster=(child,),
    )
    assert update.child_roster == (child,)
    assert update.child_roster[0].ancestry[-1] == update.child_roster[0].child_id


def test_read_updates_advances_cursor_and_surfaces_duplicate_event_ids() -> None:
    provider = FakeTophandProvider("duplicate_event")
    _create(provider)
    first = provider.read_updates()
    assert first.next_cursor == "2"
    assert len(first.updates) == 2
    assert len(provider.events) == 2
    assert provider.events[0].event_id == provider.events[1].event_id

    replay = provider.read_updates(first.next_cursor)
    assert replay.updates == ()
    assert replay.next_cursor == "2"


def test_restart_preserves_session_identity_and_increments_generation() -> None:
    provider = FakeTophandProvider("restart")
    _create(provider)
    session_id = provider.session_id
    before = provider.status().generation
    provider.restart()
    assert provider.status().generation == before + 1

    resumed = provider.create_or_resume(CreateOrResumeRequest("resume-1", "idem-resume-1"))
    assert resumed.accepted is True
    assert provider.session_id == session_id
    assert provider.status().state is ProviderState.IDLE


def test_create_or_resume_rejects_a_supplied_session_id_that_does_not_match() -> None:
    provider = FakeTophandProvider()
    mismatch = provider.create_or_resume(CreateOrResumeRequest("create-1", "idem-create-1", provider_session_id="old"))
    assert mismatch.accepted is False
    assert mismatch.consumed is False
    assert mismatch.status == "rejected"
    assert mismatch.evidence == "provider_session_id_mismatch"
    assert provider.session_id is None


def test_stale_state_and_context_loss_are_not_reported_as_healthy_idle() -> None:
    stale = FakeTophandProvider("stale_state")
    _create(stale)
    assert stale.status().state is ProviderState.STALE
    assert stale.status().unknown is True

    context_lost = FakeAmpProvider("context_loss")
    _create(context_lost)
    assert context_lost.status().state is ProviderState.CONTEXT_LOST
    assert context_lost.status().context_available is False


def test_provider_outage_returns_unknown_status_updates_and_usage() -> None:
    provider = FakeAmpProvider("provider_outage")
    status = provider.status()
    updates = provider.read_updates()
    usage = provider.usage()
    mutation = provider.create_or_resume(CreateOrResumeRequest("create-1", "idem-create-1"))
    assert status.state is ProviderState.OUTAGE
    assert status.unknown is True
    assert updates.unknown is True
    assert updates.provider_available is False
    assert mutation.status == "unknown"
    assert mutation.accepted is None
    assert mutation.consumed is None
    assert usage.complete is False
    assert usage.parent.name == "amp-parent"
    assert usage.children[0].name == "amp-child"
    assert usage.total.amount == 150


def test_false_progress_is_marked_as_a_claim_without_evidence() -> None:
    provider = FakeTophandProvider("false_progress")
    _create(provider)
    result = provider.send(SendRequest("send-1", "idem-send-1", "report progress"))
    assert result.accepted is True
    assert result.consumed is None
    claim = next(event for event in provider.read_updates().updates if event.kind is UpdateKind.PROGRESS_CLAIM)
    assert claim.kind is UpdateKind.PROGRESS_CLAIM
    assert claim.payload["progress"] is True
    assert claim.payload["progress_evidence"] == {}


def test_cancel_observes_running_turn_and_close_archive_can_resume_later() -> None:
    provider = FakeTophandProvider("close_archive_later_resume")
    _create(provider)
    provider.send(SendRequest("send-1", "idem-send-1", "start turn"))

    cancelled = provider.cancel_current_turn(CancelCurrentTurnRequest("cancel-1", "idem-cancel-1"))
    assert cancelled.accepted is True
    assert "cancelled=true" in cancelled.evidence
    assert provider.status().state is ProviderState.CANCELLED

    checkpoint = provider.checkpoint(CheckpointRequest("checkpoint-1", "idem-checkpoint-1"))
    assert checkpoint.accepted is True
    assert "checkpoint_id=" in checkpoint.evidence

    closed = provider.close(CloseRequest("close-1", "idem-close-1", archive=True))
    assert closed.state == "archived"
    assert closed.archived is True
    assert closed.provider_handle == closed.provider_thread_ref
    assert provider.status().state is ProviderState.ARCHIVED

    resumed = provider.create_or_resume(CreateOrResumeRequest("resume-1", "idem-resume-1"))
    assert resumed.accepted is True
    assert provider.status().state is ProviderState.IDLE


def test_close_requires_checkpoint_and_quiescent_turn() -> None:
    provider = FakeAmpProvider()
    _create(provider)
    no_checkpoint = provider.close(CloseRequest("close-1", "idem-close-1"))
    assert no_checkpoint.state == "failed"
    assert no_checkpoint.evidence == "checkpoint_required"

    provider.send(SendRequest("send-1", "idem-send-1", "start turn"))
    provider.checkpoint(CheckpointRequest("checkpoint-1", "idem-checkpoint-1"))
    while_running = provider.close(CloseRequest("close-2", "idem-close-2"))
    assert while_running.state == "failed"
    assert while_running.evidence == "current_turn_not_quiescent"


def test_amp_close_is_archived_even_when_caller_requests_plain_close_and_resume_keeps_id() -> None:
    provider = FakeAmpProvider()
    _create(provider)
    provider.checkpoint(CheckpointRequest("checkpoint-1", "idem-checkpoint-1"))
    closed = provider.close(CloseRequest("close-1", "idem-close-1", archive=False))
    assert closed.archived is True
    assert closed.provider_handle == closed.provider_thread_ref
    assert provider.status().state is ProviderState.ARCHIVED
    session_id = provider.session_id
    resumed = provider.create_or_resume(CreateOrResumeRequest("resume-1", "idem-resume-1", provider_session_id=session_id))
    assert resumed.accepted is True
    assert provider.session_id == session_id


def test_tophand_plain_close_does_not_advertise_later_resume() -> None:
    provider = FakeTophandProvider()
    _create(provider)
    provider.checkpoint(CheckpointRequest("checkpoint-1", "idem-checkpoint-1"))
    closed = provider.close(CloseRequest("close-1", "idem-close-1", archive=False))
    assert closed.state == "closed"
    assert provider.capabilities.resume_after_close is False
    resumed = provider.create_or_resume(CreateOrResumeRequest("resume-1", "idem-resume-1"))
    assert resumed.status == "rejected"
    assert resumed.evidence == "closed_session_not_resumable"


def test_pending_operations_are_rejected_when_capability_is_false() -> None:
    capabilities = ProviderCapabilities.from_supported(("create_or_resume", "status", "read_updates", "usage", "close"))
    provider = FakeTophandProvider(capabilities=capabilities)
    _create(provider)
    send = provider.send(SendRequest("send-1", "idem-send-1", "continue"))
    checkpoint = provider.checkpoint(CheckpointRequest("checkpoint-1", "idem-checkpoint-1"))
    close = provider.close(CloseRequest("close-1", "idem-close-1"))
    assert send.status == "rejected"
    assert send.evidence == "capability_unsupported"
    assert checkpoint.status == "rejected"
    assert checkpoint.evidence == "capability_unsupported"
    assert close.state == "failed"
    assert close.evidence == "checkpoint_capability_required"


def test_usage_exposes_complete_and_incomplete_snapshots() -> None:
    complete = FakeTophandProvider("complete_usage").usage()
    incomplete = FakeAmpProvider("incomplete_usage").usage()
    assert complete.complete is True
    assert complete.parent.amount == 100
    assert complete.children[0].amount == 50
    assert complete.total.amount == 150
    assert incomplete.complete is False
    assert incomplete.parent.amount == 100
    assert incomplete.children[0].amount == 50
    assert incomplete.total.amount == 150


def test_fixture_catalogs_cover_the_required_scenarios() -> None:
    required = {
        "restart",
        "lost_response",
        "duplicate_event",
        "stale_state",
        "context_loss",
        "provider_outage",
        "false_progress",
        "steer_consumed",
        "cancel",
        "close_archive_later_resume",
        "complete_usage",
        "incomplete_usage",
    }
    assert required <= set(FAKE_SCENARIOS)
    assert set(TOPHAND_FIXTURES) == set(AMP_FIXTURES)
    assert all(fake_provider_scenario(name).name == name for name in required)


def test_idempotency_key_cannot_be_reused_for_another_operation() -> None:
    provider = FakeTophandProvider()
    _create(provider)
    provider.send(SendRequest("send-1", "idem-send-1", "first"))
    with pytest.raises(ValueError, match="reused"):
        provider.send(SendRequest("send-2", "idem-send-1", "different"))

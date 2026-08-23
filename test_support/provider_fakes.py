"""Deterministic provider doubles and lifecycle fixtures for seam tests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Final, cast

from chitra.provider_protocol import (
    AbstractProvider,
    CancelCurrentTurnRequest,
    CancelResult,
    CheckpointRequest,
    CheckpointResult,
    CloseRequest,
    CloseResult,
    CreateOrResumeRequest,
    CreateOrResumeResult,
    Cursor,
    MutationRequest,
    MutationResult,
    ProviderName,
    ProviderState,
    ProviderStatus,
    ProviderUpdate,
    ReadUpdatesResult,
    SendRequest,
    SendResult,
    UpdateKind,
    UsageResult,
)
from chitra.session_contract import (
    ChildRosterEntry,
    CloseArchiveResult,
    CloseState,
    OperationKind,
    OperationStatus,
    ProviderCapabilities,
    ProviderOperationResult,
    UsageComponent,
    UsageReport,
)


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _usage_component(*, name: str, amount: int | float, unit: str) -> UsageComponent:
    return UsageComponent(name=name, amount=amount, unit=unit)


def _usage_report(
    *,
    parent: UsageComponent,
    children: tuple[UsageComponent, ...],
    total: UsageComponent,
    evidence_source: str,
    observed_at: str,
    complete: bool,
) -> UsageReport:
    child_roster = tuple(
        ChildRosterEntry(
            child_id=child.name,
            parent_id=parent.name,
            ancestry=(parent.name, child.name),
            retained_state="retained",
            material_result=True,
            material_result_ref=f"{evidence_source}:{child.name}",
        )
        for child in children
    )
    return UsageReport(
        parent=parent,
        children=children,
        child_roster=child_roster,
        child_roster_complete=complete,
        child_roster_evidence=evidence_source if complete else None,
        total=total,
        evidence_source=evidence_source,
        observed_at=observed_at,
        complete=complete,
    )


@dataclass(frozen=True, slots=True)
class FakeProviderScenario:
    """Named fault fixture for the deterministic provider doubles."""

    name: str = "normal"
    lost_response_operations: frozenset[str] = frozenset()
    steer_consumed: bool = True
    usage_complete: bool = True

    def __post_init__(self) -> None:
        _required_text(self.name, "name")

    @classmethod
    def named(cls, name: str) -> FakeProviderScenario:
        _required_text(name, "name")
        if name == "lost_response":
            return cls(name=name, lost_response_operations=frozenset({"create_or_resume", "send", "checkpoint", "cancel", "close"}))
        if name == "incomplete_usage":
            return cls(name=name, usage_complete=False)
        if name == "steer_not_consumed":
            return cls(name=name, steer_consumed=False)
        return cls(name=name)

    def has(self, feature: str) -> bool:
        return self.name == feature

    def loses_response(self, operation: str) -> bool:
        return self.has("lost_response") or operation in self.lost_response_operations

    @property
    def provider_outage(self) -> bool:
        return self.has("provider_outage")

    @property
    def context_loss(self) -> bool:
        return self.has("context_loss")

    @property
    def stale_state(self) -> bool:
        return self.has("stale_state")

    @property
    def duplicate_event(self) -> bool:
        return self.has("duplicate_event")

    @property
    def false_progress(self) -> bool:
        return self.has("false_progress")

    @property
    def restart(self) -> bool:
        return self.has("restart")

    @property
    def archive_on_close(self) -> bool:
        return self.has("close_archive") or self.has("close_archive_later_resume")


def _operation_status(*, accepted: bool | None, consumed: bool | None, lost_response: bool = False) -> OperationStatus:
    if lost_response:
        return "lost-response"
    if accepted is None:
        return "unknown"
    if accepted is False:
        return "rejected"
    return "consumed" if consumed is True else "accepted"


def _make_operation_result(
    *,
    request: MutationRequest,
    operation: OperationKind,
    lane_id: str,
    provider_handle: str,
    observed_at: str,
    accepted: bool | None,
    consumed: bool | None,
    evidence: str = "",
    lost_response: bool = False,
    provider_instance_id: str | None = None,
    provider_generation: int | None = None,
) -> MutationResult:
    return ProviderOperationResult(
        operation_id=request.operation_id,
        kind=operation,
        lane_id=lane_id,
        provider_handle=provider_handle,
        idempotency_key=request.idempotency_key,
        payload_digest=request.payload_digest,
        status=_operation_status(accepted=accepted, consumed=consumed, lost_response=lost_response),
        accepted=accepted,
        consumed=consumed,
        observed_at=observed_at,
        evidence=evidence,
        provider_instance_id=provider_instance_id or request.provider_instance_id,
        provider_generation=provider_generation or request.provider_generation,
    )


def _make_close_result(
    *,
    request: CloseRequest,
    provider_handle: str,
    provider_thread_ref: str,
    state: CloseState,
    observed_at: str,
    evidence: str,
    later_resume_supported: bool | None,
    checkpoint_ref: str | None = None,
    quiescent: bool | None = None,
) -> CloseResult:
    return CloseArchiveResult(
        operation_id=request.operation_id,
        lane_id=request.lane_id,
        provider_handle=provider_handle,
        provider_instance_id=request.provider_instance_id,
        provider_generation=request.provider_generation,
        idempotency_key=request.idempotency_key,
        payload_digest=request.payload_digest,
        state=state,
        provider_thread_ref=provider_thread_ref,
        same_provider_thread=True if state in {"closed", "archived"} else None,
        later_resume_supported=later_resume_supported,
        checkpoint_ref=checkpoint_ref,
        quiescent=quiescent,
        observed_at=observed_at,
        evidence=evidence,
    )


FAKE_SCENARIOS: Final[tuple[str, ...]] = (
    "normal",
    "restart",
    "lost_response",
    "duplicate_event",
    "stale_state",
    "context_loss",
    "provider_outage",
    "false_progress",
    "steer_consumed",
    "steer_not_consumed",
    "cancel",
    "close_archive_later_resume",
    "close",
    "complete_usage",
    "incomplete_usage",
)


def fake_provider_scenario(name: str) -> FakeProviderScenario:
    """Return one of the named deterministic lifecycle fixtures."""
    if name not in FAKE_SCENARIOS:
        raise ValueError(f"unknown fake provider scenario: {name!r}")
    return FakeProviderScenario.named(name)


@dataclass(frozen=True, slots=True)
class _StoredOperation:
    fingerprint: tuple[object, ...]
    result: MutationResult | CloseResult


class DeterministicFakeProvider(AbstractProvider):
    """Small in-memory provider double shared by Tophand and Amp tests."""

    provider_name: ProviderName | str
    _always_archive_on_close: bool = False
    provider_instance_id: str = "default"

    def __init__(
        self,
        scenario: str | FakeProviderScenario = "normal",
        *,
        session_ref: str = "host:lane:instance",
        provider_instance_id: str = "default",
        capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self.scenario = fake_provider_scenario(scenario) if isinstance(scenario, str) else scenario
        self.session_ref = _required_text(session_ref, "session_ref")
        self.provider_instance_id = _required_text(provider_instance_id, "provider_instance_id")
        self._capabilities_override = capabilities
        self._available = not self.scenario.provider_outage
        self._session_id: str | None = None
        self._state = ProviderState.STARTING
        self._generation = 1
        self._turn_id: str | None = None
        self._events: list[ProviderUpdate] = []
        self._operations: dict[str, _StoredOperation] = {}
        self._pending_consumptions: list[SendRequest] = []
        self._event_number = 0
        self._checkpoint_number = 0
        self._last_checkpoint_id: str | None = None
        self._duplicate_injected = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def capabilities(self) -> ProviderCapabilities:
        if self._capabilities_override is not None:
            return self._capabilities_override
        return ProviderCapabilities(
            create_or_resume=True,
            status=True,
            send=True,
            read_updates=True,
            checkpoint=True,
            usage=True,
            cancel_current_turn=True,
            close=True,
            resume_after_close=self._always_archive_on_close or self.scenario.archive_on_close,
            parent_child_usage=True,
        )

    @property
    def _resume_after_close_supported(self) -> bool:
        return self.capabilities.resume_after_close

    @property
    def events(self) -> tuple[ProviderUpdate, ...]:
        """Expose the raw stream for assertions, including duplicate events."""
        return tuple(self._events)

    def set_available(self, available: bool) -> None:
        """Toggle the fake outage without changing the provider identity."""
        self._available = available

    def restart(self) -> None:
        """Simulate a provider process restart while retaining its session."""
        self._generation += 1
        self._state = ProviderState.STARTING
        self._append_event(UpdateKind.PROVIDER_RESTARTED, {"generation": self._generation})
        if self._session_id is not None:
            self._state = ProviderState.IDLE

    def _timestamp(self) -> str:
        minute, second = divmod(self._event_number, 60)
        return f"2026-01-01T00:{minute:02d}:{second:02d}Z"

    def _append_event(
        self,
        kind: UpdateKind | str,
        payload: Mapping[str, object] | None = None,
        *,
        event_id: str | None = None,
        operation_id: str | None = None,
        lane_id: str | None = None,
        idempotency_key: str | None = None,
        payload_digest: str | None = None,
        provider_instance_id: str | None = None,
        provider_generation: int | None = None,
        child_roster: tuple[ChildRosterEntry, ...] = (),
        result_evidence: Mapping[str, object] | None = None,
    ) -> ProviderUpdate:
        self._event_number += 1
        resolved_operation_id = operation_id or f"system:{self._event_number:04d}"
        resolved_lane_id = lane_id or self.session_ref
        resolved_idempotency_key = idempotency_key or f"system:{self._event_number:04d}"
        resolved_payload_digest = (
            payload_digest or hashlib.sha256(f"{resolved_operation_id}|{resolved_lane_id}|{resolved_idempotency_key}".encode()).hexdigest()
        )
        resolved_provider_instance_id = provider_instance_id or self.provider_instance_id
        resolved_provider_generation = provider_generation or self._generation
        resolved_result_evidence = dict(result_evidence or {})
        resolved_payload = dict(payload or {})
        resolved_payload.update(
            {
                "operation_id": resolved_operation_id,
                "lane_id": resolved_lane_id,
                "idempotency_key": resolved_idempotency_key,
                "payload_digest": resolved_payload_digest,
                "provider_instance_id": resolved_provider_instance_id,
                "provider_generation": resolved_provider_generation,
                "child_roster": [entry.to_dict() for entry in child_roster],
                "result_evidence": resolved_result_evidence,
            }
        )
        event = ProviderUpdate(
            event_id=event_id or f"{self.provider_name}-{self._event_number:04d}",
            cursor=str(self._event_number),
            kind=kind,
            provider_session_id=self._session_id,
            observed_at=self._timestamp(),
            operation_id=resolved_operation_id,
            lane_id=resolved_lane_id,
            idempotency_key=resolved_idempotency_key,
            payload_digest=resolved_payload_digest,
            provider_instance_id=resolved_provider_instance_id,
            provider_generation=resolved_provider_generation,
            payload=resolved_payload,
            child_roster=child_roster,
        )
        self._events.append(event)
        if self.scenario.duplicate_event and not self._duplicate_injected:
            self._duplicate_injected = True
            self._event_number += 1
            self._events.append(replace(event, cursor=str(self._event_number), observed_at=self._timestamp()))
        return event

    def _fingerprint(self, request: MutationRequest, *parts: object) -> tuple[object, ...]:
        return (
            request.operation_id,
            request.idempotency_key,
            request.payload_digest,
            request.lane_id,
            request.provider_instance_id,
            request.provider_generation,
            type(request).__name__,
            *parts,
        )

    def _unknown_result(
        self,
        request: MutationRequest,
        *,
        operation: OperationKind,
        reason: str,
        lost_response: bool = False,
    ) -> MutationResult:
        return _make_operation_result(
            request=request,
            operation=operation,
            lane_id=request.lane_id,
            provider_handle=self._session_id or "unknown",
            observed_at=self._timestamp(),
            accepted=None,
            consumed=None,
            evidence=reason,
            lost_response=lost_response,
        )

    def _unsupported_result(self, request: MutationRequest, operation: OperationKind) -> MutationResult:
        return _make_operation_result(
            request=request,
            operation=operation,
            lane_id=request.lane_id,
            provider_handle=self._session_id or "unknown",
            observed_at=self._timestamp(),
            accepted=False,
            consumed=False,
            evidence="capability_unsupported",
        )

    def _run_mutation(
        self,
        request: MutationRequest,
        operation: OperationKind,
        fingerprint: tuple[object, ...],
        effect: Callable[[], MutationResult],
        *,
        capability_supported: bool = True,
    ) -> MutationResult:
        stored = self._operations.get(request.idempotency_key)
        if stored is not None:
            if stored.fingerprint != fingerprint or stored.result.operation_id != request.operation_id:
                raise ValueError("idempotency_key was reused for a different operation")
            return cast(MutationResult, stored.result)
        if any(
            stored_result.result.operation_id == request.operation_id and stored_result.result.idempotency_key != request.idempotency_key
            for stored_result in self._operations.values()
        ):
            raise ValueError("operation_id was reused with a different idempotency_key")
        if not capability_supported:
            result = effect()
            self._operations[request.idempotency_key] = _StoredOperation(fingerprint, result)
            return result
        if not self._available:
            return self._unknown_result(request, operation=operation, reason="provider_outage")

        result = effect()
        self._operations[request.idempotency_key] = _StoredOperation(fingerprint, result)
        if self.scenario.loses_response(operation):
            return self._unknown_result(
                request,
                operation=operation,
                reason="response_lost_after_provider_acceptance",
                lost_response=True,
            )
        return result

    def _run_close(
        self,
        request: CloseRequest,
        fingerprint: tuple[object, ...],
        effect: Callable[[], CloseResult],
        *,
        capability_supported: bool = True,
    ) -> CloseResult:
        stored = self._operations.get(request.idempotency_key)
        if stored is not None:
            if stored.fingerprint != fingerprint or stored.result.operation_id != request.operation_id:
                raise ValueError("idempotency_key was reused for a different operation")
            return cast(CloseResult, stored.result)
        if any(
            stored_result.result.operation_id == request.operation_id and stored_result.result.idempotency_key != request.idempotency_key
            for stored_result in self._operations.values()
        ):
            raise ValueError("operation_id was reused with a different idempotency_key")
        if not capability_supported:
            result = effect()
            self._operations[request.idempotency_key] = _StoredOperation(fingerprint, result)
            return result
        if not self._available:
            return _make_close_result(
                request=request,
                provider_handle=self._session_id or "unknown",
                provider_thread_ref=self._session_id or "unknown",
                state="unknown",
                observed_at=self._timestamp(),
                evidence="provider_outage",
                later_resume_supported=None,
            )
        result = effect()
        self._operations[request.idempotency_key] = _StoredOperation(fingerprint, result)
        if self.scenario.loses_response("close"):
            return _make_close_result(
                request=request,
                provider_handle=self._session_id or "unknown",
                provider_thread_ref=self._session_id or "unknown",
                state="unknown",
                observed_at=self._timestamp(),
                evidence="response_lost_after_provider_acceptance",
                later_resume_supported=None,
            )
        return result

    def create_or_resume(self, request: CreateOrResumeRequest) -> CreateOrResumeResult:
        def effect() -> CreateOrResumeResult:
            if not self.capabilities.create_or_resume:
                return self._unsupported_result(request, "create_or_resume")
            if request.provider_session_id is not None and request.provider_session_id != self._session_id:
                return _make_operation_result(
                    request=request,
                    operation="create_or_resume",
                    lane_id=request.lane_id,
                    provider_handle=self._session_id or "unknown",
                    observed_at=self._timestamp(),
                    accepted=False,
                    consumed=False,
                    evidence="provider_session_id_mismatch",
                )
            if self._state is ProviderState.CLOSED and not self._resume_after_close_supported:
                return _make_operation_result(
                    request=request,
                    operation="create_or_resume",
                    lane_id=request.lane_id,
                    provider_handle=self._session_id or "unknown",
                    observed_at=self._timestamp(),
                    accepted=False,
                    consumed=False,
                    evidence="closed_session_not_resumable",
                )
            resumed = self._session_id is not None
            if self._session_id is None:
                self._session_id = f"{self.provider_name}-session-1"
                self._generation = max(self._generation, 1)
                self._state = ProviderState.IDLE
                event = self._append_event(
                    UpdateKind.SESSION_CREATED,
                    {"session_ref": request.session_ref or self.session_ref},
                    operation_id=request.operation_id,
                    lane_id=request.lane_id,
                    idempotency_key=request.idempotency_key,
                    payload_digest=request.payload_digest,
                    provider_instance_id=request.provider_instance_id,
                    provider_generation=request.provider_generation,
                    result_evidence={"accepted": True, "consumed": True},
                )
            else:
                self._generation += 1 if self.scenario.restart else 0
                self._state = ProviderState.CONTEXT_LOST if self.scenario.context_loss else ProviderState.IDLE
                event = self._append_event(
                    UpdateKind.SESSION_RESUMED,
                    {"generation": self._generation},
                    operation_id=request.operation_id,
                    lane_id=request.lane_id,
                    idempotency_key=request.idempotency_key,
                    payload_digest=request.payload_digest,
                    provider_instance_id=request.provider_instance_id,
                    provider_generation=request.provider_generation,
                    result_evidence={"accepted": True, "consumed": True},
                )
            return _make_operation_result(
                request=request,
                operation="create_or_resume",
                lane_id=request.lane_id,
                provider_handle=self._session_id or "unknown",
                observed_at=self._timestamp(),
                accepted=True,
                consumed=True,
                evidence=(
                    f"event_id={event.event_id};resumed={str(resumed).lower()};"
                    f"context_available={str(not self.scenario.context_loss).lower()}"
                ),
            )

        fingerprint = self._fingerprint(request, request.session_ref, request.provider_session_id, request.context_ref)
        return self._run_mutation(
            request,
            "create_or_resume",
            fingerprint,
            effect,
            capability_supported=self.capabilities.create_or_resume,
        )

    def status(self) -> ProviderStatus:
        if not self.capabilities.status:
            return ProviderStatus(
                provider=self.provider_name,
                state=ProviderState.UNKNOWN,
                provider_session_id=self._session_id,
                generation=self._generation,
                fresh=False,
                provider_available=True,
                context_available=None,
                current_turn_id=self._turn_id,
                last_event_id=self._events[-1].event_id if self._events else None,
                reason="capability_unsupported",
            )
        self._materialize_pending_consumptions()
        if not self._available:
            return ProviderStatus(
                provider=self.provider_name,
                state=ProviderState.OUTAGE,
                provider_session_id=self._session_id,
                generation=self._generation,
                fresh=False,
                provider_available=False,
                context_available=None,
                current_turn_id=self._turn_id,
                last_event_id=self._events[-1].event_id if self._events else None,
                reason="provider_outage",
            )
        if self.scenario.stale_state:
            state = ProviderState.STALE
            fresh = False
            reason = "stale_provider_state"
        elif self.scenario.context_loss:
            state = ProviderState.CONTEXT_LOST
            fresh = True
            reason = "context_lost"
        else:
            state = self._state
            fresh = True
            reason = ""
        return ProviderStatus(
            provider=self.provider_name,
            state=state,
            provider_session_id=self._session_id,
            generation=self._generation,
            fresh=fresh,
            provider_available=True,
            context_available=not self.scenario.context_loss if self._session_id else None,
            current_turn_id=self._turn_id,
            last_event_id=self._events[-1].event_id if self._events else None,
            reason=reason,
        )

    def send(self, request: SendRequest) -> SendResult:
        def effect() -> SendResult:
            if not self.capabilities.send:
                return self._unsupported_result(request, "send")
            if self._session_id is None:
                return _make_operation_result(
                    request=request,
                    operation="send",
                    lane_id=request.lane_id,
                    provider_handle=self._session_id or "unknown",
                    observed_at=self._timestamp(),
                    accepted=False,
                    consumed=False,
                    evidence="no_active_session",
                )
            self._state = ProviderState.RUNNING
            self._turn_id = f"{self.provider_name}-turn-{self._event_number + 1:04d}"
            accepted_event = self._append_event(
                UpdateKind.STEER_ACCEPTED,
                {"text": request.text},
                operation_id=request.operation_id,
                lane_id=request.lane_id,
                idempotency_key=request.idempotency_key,
                payload_digest=request.payload_digest,
                provider_instance_id=request.provider_instance_id,
                provider_generation=request.provider_generation,
                result_evidence={"accepted": True, "consumed": None},
            )
            event_ids = [accepted_event.event_id]
            if self.scenario.steer_consumed:
                # Transport acceptance is returned now.  The fake proves
                # consumption only when a later observation reads the event.
                self._pending_consumptions.append(request)
            if self.scenario.false_progress:
                progress_event = self._append_event(
                    UpdateKind.PROGRESS_CLAIM,
                    {"progress": True, "progress_evidence": {}, "text": request.text},
                    operation_id=request.operation_id,
                    lane_id=request.lane_id,
                    idempotency_key=request.idempotency_key,
                    payload_digest=request.payload_digest,
                    provider_instance_id=request.provider_instance_id,
                    provider_generation=request.provider_generation,
                    result_evidence={"accepted": True, "consumed": None, "progress_evidence": {}},
                )
                event_ids.append(progress_event.event_id)
            return _make_operation_result(
                request=request,
                operation="send",
                lane_id=request.lane_id,
                provider_handle=self._session_id or "unknown",
                observed_at=self._timestamp(),
                accepted=True,
                # The acknowledgement proves transport acceptance only.  The
                # STEER_CONSUMED event is deliberately exposed by a later
                # read_updates call; it is not folded into this response.
                consumed=None,
                evidence=(
                    f"event_ids={','.join(event_ids)};"
                    f"consumption_pending={str(self.scenario.steer_consumed).lower()};"
                    f"false_progress={str(self.scenario.false_progress).lower()}"
                ),
            )

        fingerprint = self._fingerprint(request, request.text)
        return self._run_mutation(
            request,
            "send",
            fingerprint,
            effect,
            capability_supported=self.capabilities.send,
        )

    def read_updates(self, cursor: Cursor | None = None) -> ReadUpdatesResult:
        if not self.capabilities.read_updates:
            return ReadUpdatesResult(
                cursor, str(self._event_number), provider_available=True, complete=False, reason="capability_unsupported"
            )
        if not self._available:
            return ReadUpdatesResult(cursor, str(self._event_number), provider_available=False, complete=False, reason="provider_outage")
        self._materialize_pending_consumptions()
        start = 0 if cursor is None else self._parse_cursor(cursor)
        candidates = [event for event in self._events if int(event.cursor) > start]
        # Duplicate event IDs are part of the provider failure surface.  The
        # provider must expose them so the journal/consumer can apply its own
        # durable event-id policy instead of silently losing the evidence.
        return ReadUpdatesResult(cursor, str(self._event_number), tuple(candidates))

    def _materialize_pending_consumptions(self) -> None:
        pending = tuple(self._pending_consumptions)
        self._pending_consumptions.clear()
        for request in pending:
            self._append_event(
                UpdateKind.STEER_CONSUMED,
                {"text": request.text},
                operation_id=request.operation_id,
                lane_id=request.lane_id,
                idempotency_key=request.idempotency_key,
                payload_digest=request.payload_digest,
                provider_instance_id=request.provider_instance_id,
                provider_generation=request.provider_generation,
                result_evidence={"accepted": True, "consumed": True},
            )

    @staticmethod
    def _parse_cursor(cursor: Cursor) -> int:
        try:
            parsed = int(cursor)
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor must be a non-negative decimal string") from exc
        if parsed < 0:
            raise ValueError("cursor must be a non-negative decimal string")
        return parsed

    def checkpoint(self, request: CheckpointRequest) -> CheckpointResult:
        def effect() -> CheckpointResult:
            if not self.capabilities.checkpoint:
                return self._unsupported_result(request, "checkpoint")
            if self._session_id is None:
                return _make_operation_result(
                    request=request,
                    operation="checkpoint",
                    lane_id=request.lane_id,
                    provider_handle=self._session_id or "unknown",
                    observed_at=self._timestamp(),
                    accepted=False,
                    consumed=False,
                    evidence="no_active_session",
                )
            self._checkpoint_number += 1
            checkpoint_id = f"{self.provider_name}-checkpoint-{self._checkpoint_number:04d}"
            event = self._append_event(
                UpdateKind.CHECKPOINT_CREATED,
                {"checkpoint_id": checkpoint_id, "label": request.label},
                operation_id=request.operation_id,
                lane_id=request.lane_id,
                idempotency_key=request.idempotency_key,
                payload_digest=request.payload_digest,
                provider_instance_id=request.provider_instance_id,
                provider_generation=request.provider_generation,
                result_evidence={"accepted": True, "consumed": True, "checkpoint_id": checkpoint_id},
            )
            self._last_checkpoint_id = checkpoint_id
            return _make_operation_result(
                request=request,
                operation="checkpoint",
                lane_id=request.lane_id,
                provider_handle=self._session_id or "unknown",
                observed_at=self._timestamp(),
                accepted=True,
                consumed=True,
                evidence=f"event_id={event.event_id};checkpoint_id={checkpoint_id}",
            )

        fingerprint = self._fingerprint(request, request.label)
        return self._run_mutation(
            request,
            "checkpoint",
            fingerprint,
            effect,
            capability_supported=self.capabilities.checkpoint,
        )

    def usage(self) -> UsageResult:
        evidence_source = f"{self.provider_name}:{self._session_id or self.session_ref}"
        parent = _usage_component(name=f"{self.provider_name}-parent", amount=100, unit="tokens")
        child = _usage_component(name=f"{self.provider_name}-child", amount=50, unit="tokens")
        if not self.capabilities.usage:
            return _usage_report(
                parent=parent,
                children=(child,),
                total=_usage_component(name=f"{self.provider_name}-total", amount=150, unit="tokens"),
                evidence_source="capability_unsupported",
                observed_at="2026-01-01T00:00:00Z",
                complete=False,
            )
        if not self._available:
            return _usage_report(
                parent=parent,
                children=(child,),
                total=_usage_component(name=f"{self.provider_name}-total", amount=150, unit="tokens"),
                evidence_source="provider_outage",
                observed_at="2026-01-01T00:00:00Z",
                complete=False,
            )
        if not self.scenario.usage_complete or self.scenario.has("incomplete_usage"):
            return _usage_report(
                parent=parent,
                children=(child,),
                total=_usage_component(name=f"{self.provider_name}-total", amount=150, unit="tokens"),
                evidence_source=evidence_source,
                observed_at="2026-01-01T00:00:00Z",
                complete=False,
            )
        return _usage_report(
            parent=parent,
            children=(child,),
            total=_usage_component(name=f"{self.provider_name}-total", amount=150, unit="tokens"),
            evidence_source=evidence_source,
            observed_at="2026-01-01T00:00:00Z",
            complete=True,
        )

    def cancel_current_turn(self, request: CancelCurrentTurnRequest) -> CancelResult:
        def effect() -> CancelResult:
            if not self.capabilities.cancel_current_turn:
                return self._unsupported_result(request, "cancel_current_turn")
            if self._session_id is None:
                return _make_operation_result(
                    request=request,
                    operation="cancel_current_turn",
                    lane_id=request.lane_id,
                    provider_handle=self._session_id or "unknown",
                    observed_at=self._timestamp(),
                    accepted=False,
                    consumed=False,
                    evidence="no_active_session",
                )
            requested_event = self._append_event(
                UpdateKind.TURN_CANCEL_REQUESTED,
                {"reason": request.reason},
                operation_id=request.operation_id,
                lane_id=request.lane_id,
                idempotency_key=request.idempotency_key,
                payload_digest=request.payload_digest,
                provider_instance_id=request.provider_instance_id,
                provider_generation=request.provider_generation,
                result_evidence={"accepted": True, "consumed": None},
            )
            running = self._state is ProviderState.RUNNING
            event_ids = [requested_event.event_id]
            if running:
                cancelled_event = self._append_event(
                    UpdateKind.TURN_CANCELLED,
                    {"reason": request.reason},
                    operation_id=request.operation_id,
                    lane_id=request.lane_id,
                    idempotency_key=request.idempotency_key,
                    payload_digest=request.payload_digest,
                    provider_instance_id=request.provider_instance_id,
                    provider_generation=request.provider_generation,
                    result_evidence={"accepted": True, "consumed": True, "cancelled": True},
                )
                event_ids.append(cancelled_event.event_id)
                self._state = ProviderState.CANCELLED
                self._turn_id = None
            return _make_operation_result(
                request=request,
                operation="cancel_current_turn",
                lane_id=request.lane_id,
                provider_handle=self._session_id or "unknown",
                observed_at=self._timestamp(),
                accepted=True,
                consumed=running,
                evidence=(f"event_ids={','.join(event_ids)};cancelled={str(running).lower()}"),
            )

        fingerprint = self._fingerprint(request, request.reason)
        return self._run_mutation(
            request,
            "cancel_current_turn",
            fingerprint,
            effect,
            capability_supported=self.capabilities.cancel_current_turn,
        )

    def close(self, request: CloseRequest) -> CloseResult:
        def effect() -> CloseResult:
            if not self.capabilities.close:
                return _make_close_result(
                    request=request,
                    provider_handle=self._session_id or "unknown",
                    provider_thread_ref=self._session_id or "unknown",
                    state="failed",
                    observed_at=self._timestamp(),
                    evidence="capability_unsupported",
                    later_resume_supported=False,
                )
            if not self.capabilities.checkpoint:
                return _make_close_result(
                    request=request,
                    provider_handle=self._session_id or "unknown",
                    provider_thread_ref=self._session_id or "unknown",
                    state="failed",
                    observed_at=self._timestamp(),
                    evidence="checkpoint_capability_required",
                    later_resume_supported=False,
                )
            if self._session_id is None:
                return _make_close_result(
                    request=request,
                    provider_handle="unknown",
                    provider_thread_ref="unknown",
                    state="failed",
                    observed_at=self._timestamp(),
                    evidence="no_active_session",
                    later_resume_supported=False,
                )
            if self._last_checkpoint_id is None:
                return _make_close_result(
                    request=request,
                    provider_handle=self._session_id,
                    provider_thread_ref=self._session_id,
                    state="failed",
                    observed_at=self._timestamp(),
                    evidence="checkpoint_required",
                    later_resume_supported=None,
                )
            if self._state is ProviderState.RUNNING or self._turn_id is not None:
                return _make_close_result(
                    request=request,
                    provider_handle=self._session_id,
                    provider_thread_ref=self._session_id,
                    state="failed",
                    observed_at=self._timestamp(),
                    evidence="current_turn_not_quiescent",
                    later_resume_supported=None,
                )
            archive = request.archive or self._always_archive_on_close or self.scenario.archive_on_close
            kind = UpdateKind.SESSION_ARCHIVED if archive else UpdateKind.SESSION_CLOSED
            event = self._append_event(
                kind,
                {"archive": archive},
                operation_id=request.operation_id,
                lane_id=request.lane_id,
                idempotency_key=request.idempotency_key,
                payload_digest=request.payload_digest,
                provider_instance_id=request.provider_instance_id,
                provider_generation=request.provider_generation,
                result_evidence={
                    "accepted": True,
                    "consumed": True,
                    "state": "archived" if archive else "closed",
                    "checkpoint_id": self._last_checkpoint_id,
                },
            )
            self._state = ProviderState.ARCHIVED if archive else ProviderState.CLOSED
            self._turn_id = None
            return _make_close_result(
                request=request,
                provider_handle=self._session_id,
                provider_thread_ref=self._session_id,
                state="archived" if archive else "closed",
                observed_at=self._timestamp(),
                evidence=f"event_id={event.event_id};checkpoint_id={self._last_checkpoint_id}",
                later_resume_supported=archive,
                checkpoint_ref=self._last_checkpoint_id,
                quiescent=True,
            )

        fingerprint = self._fingerprint(request, request.archive, self._last_checkpoint_id)
        return self._run_close(
            request,
            fingerprint,
            effect,
            capability_supported=self.capabilities.close and self.capabilities.checkpoint,
        )


class FakeTophandProvider(DeterministicFakeProvider):
    """Deterministic Tophand double; no Tophand transport is implemented."""

    provider_name = ProviderName.TOPHAND
    _always_archive_on_close = False


class FakeAmpProvider(DeterministicFakeProvider):
    """Deterministic Amp double; no Amp transport is implemented."""

    provider_name = ProviderName.AMP
    _always_archive_on_close = True


# The fixture names are intentionally explicit so tests can parameterize both
# providers without relying on hidden product behavior.
TOPHAND_FIXTURES: Final[dict[str, FakeProviderScenario]] = {name: fake_provider_scenario(name) for name in FAKE_SCENARIOS}
AMP_FIXTURES: Final[dict[str, FakeProviderScenario]] = {name: fake_provider_scenario(name) for name in FAKE_SCENARIOS}


__all__ = [
    "AMP_FIXTURES",
    "FAKE_SCENARIOS",
    "DeterministicFakeProvider",
    "FakeAmpProvider",
    "FakeProviderScenario",
    "FakeTophandProvider",
    "TOPHAND_FIXTURES",
    "fake_provider_scenario",
]

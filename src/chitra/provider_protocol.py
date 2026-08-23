"""Typed provider lifecycle seam and deterministic provider doubles.

The monitor talks to a provider through this deliberately small boundary.  A
provider may be Tophand, Amp, or a future adapter.  This module does not make
network calls and does not know how either product starts a process.

Two facts are intentionally kept separate on every mutating result:

``accepted``
    What the provider's request endpoint acknowledged.  It is ``None`` when
    the response was lost or the provider was unavailable.

``observed_consumption``
    What a later provider observation proved about the requested effect.  It
    is also ``None`` when the response or observation is unknown.  A successful
    request is therefore not treated as proof that a steer was consumed.

The fake providers at the bottom of this module are test doubles only.  They
use a deterministic event stream and idempotency records so tests can exercise
restart, duplicate event, stale state, context loss, outage, and lost-response
paths without a live Tophand or Amp installation.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final, Protocol, cast, runtime_checkable

from .session_contract import (
    ChildRosterEntry,
    CloseArchiveResult,
    CloseState,
    OperationKind,
    OperationStatus,
    PendingProviderOperation,
    ProviderCapabilities,
    ProviderOperationResult,
    UsageComponent,
    UsageReport,
)


class ProviderName(StrEnum):
    """Names used by the first provider adapters."""

    TOPHAND = "tophand"
    AMP = "amp"


class ProviderState(StrEnum):
    """A provider session state as observed by the monitor."""

    UNKNOWN = "unknown"
    STARTING = "starting"
    IDLE = "idle"
    RUNNING = "running"
    CANCELLED = "cancelled"
    CLOSED = "closed"
    ARCHIVED = "archived"
    STALE = "stale"
    CONTEXT_LOST = "context_lost"
    OUTAGE = "outage"
    COMPLETE = "complete"


class UpdateKind(StrEnum):
    """Small, provider-neutral vocabulary for lifecycle updates."""

    PROVIDER_RESTARTED = "provider_restarted"
    SESSION_CREATED = "session_created"
    SESSION_RESUMED = "session_resumed"
    STEER_ACCEPTED = "steer_accepted"
    STEER_CONSUMED = "steer_consumed"
    PROGRESS_CLAIM = "progress_claim"
    CHECKPOINT_CREATED = "checkpoint_created"
    TURN_STARTED = "turn_started"
    TURN_CANCEL_REQUESTED = "turn_cancel_requested"
    TURN_CANCELLED = "turn_cancelled"
    SESSION_CLOSED = "session_closed"
    SESSION_ARCHIVED = "session_archived"


type Cursor = str

DEFAULT_CREATED_AT: Final[str] = "2026-01-01T00:00:00Z"
DEFAULT_PROVIDER_HANDLE: Final[str] = "provider"


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


@dataclass(frozen=True, slots=True, init=False)
class MutationRequest:
    """Provider mutation carrying one canonical pending-operation envelope."""

    operation: PendingProviderOperation

    @classmethod
    def _build_operation(
        cls,
        operation: str | PendingProviderOperation | None,
        idempotency_key: str | None,
        *,
        operation_id: str | None,
        kind: OperationKind,
        lane_id: str,
        provider_instance_id: str,
        provider_generation: int,
        payload_digest: str,
        payload_material: tuple[object, ...],
        provider_handle: str,
        created_at: str,
        attempt: int,
    ) -> PendingProviderOperation:
        if isinstance(operation, PendingProviderOperation):
            if idempotency_key is not None or operation_id is not None:
                raise ValueError("nested operation envelope cannot be combined with operation identity fields")
            if operation.kind != kind:
                raise ValueError(f"operation envelope kind must be {kind}")
            return operation
        if operation is not None and operation_id is not None and operation != operation_id:
            raise ValueError("operation and operation_id must identify the same operation")
        resolved_operation_id = operation if operation is not None else operation_id
        if resolved_operation_id is None:
            raise ValueError("operation_id must be supplied")
        _required_text(resolved_operation_id, "operation_id")
        if idempotency_key is None:
            raise ValueError("idempotency_key must be supplied")
        _required_text(idempotency_key, "idempotency_key")
        _required_text(lane_id, "lane_id")
        _required_text(provider_instance_id, "provider_instance_id")
        _required_text(provider_handle, "provider_handle")
        if isinstance(provider_generation, bool) or provider_generation < 1:
            raise ValueError("provider_generation must be a positive integer")
        _required_text(created_at, "created_at")
        if isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        digest = (
            payload_digest
            or hashlib.sha256(
                "|".join(
                    (
                        resolved_operation_id,
                        idempotency_key,
                        lane_id,
                        provider_instance_id,
                        str(provider_generation),
                        kind,
                        *(str(value) for value in payload_material),
                    )
                ).encode()
            ).hexdigest()
        )
        _required_text(digest, "payload_digest")
        return PendingProviderOperation(
            operation_id=resolved_operation_id,
            kind=kind,
            lane_id=lane_id,
            provider_handle=provider_handle,
            idempotency_key=idempotency_key,
            payload_digest=digest,
            provider_instance_id=provider_instance_id,
            provider_generation=provider_generation,
            created_at=created_at,
            attempt=attempt,
        )

    @property
    def operation_id(self) -> str:
        return self.operation.operation_id

    @property
    def idempotency_key(self) -> str:
        return self.operation.idempotency_key

    @property
    def lane_id(self) -> str:
        return self.operation.lane_id

    @property
    def provider_instance_id(self) -> str:
        return self.operation.provider_instance_id

    @property
    def provider_generation(self) -> int:
        return self.operation.provider_generation

    @property
    def payload_digest(self) -> str:
        return self.operation.payload_digest

    @property
    def provider_handle(self) -> str:
        return self.operation.provider_handle


@dataclass(frozen=True, slots=True, init=False)
class CreateOrResumeRequest(MutationRequest):
    """Create a provider session or resume the known session."""

    session_ref: str
    provider_session_id: str | None = None
    context_ref: str | None = None

    def __init__(
        self,
        operation: str | PendingProviderOperation | None = None,
        idempotency_key: str | None = None,
        session_ref: str = "",
        provider_session_id: str | None = None,
        context_ref: str | None = None,
        *,
        operation_id: str | None = None,
        lane_id: str = "host:lane:instance",
        provider_instance_id: str = "default",
        provider_generation: int = 1,
        payload_digest: str = "",
        provider_handle: str = DEFAULT_PROVIDER_HANDLE,
        created_at: str = DEFAULT_CREATED_AT,
        attempt: int = 1,
    ) -> None:
        if session_ref:
            _required_text(session_ref, "session_ref")
        _optional_text(provider_session_id, "provider_session_id")
        _optional_text(context_ref, "context_ref")
        envelope = self._build_operation(
            operation,
            idempotency_key,
            operation_id=operation_id,
            kind="create_or_resume",
            lane_id=lane_id,
            provider_instance_id=provider_instance_id,
            provider_generation=provider_generation,
            payload_digest=payload_digest,
            payload_material=(session_ref, provider_session_id, context_ref),
            provider_handle=provider_handle,
            created_at=created_at,
            attempt=attempt,
        )
        object.__setattr__(self, "operation", envelope)
        object.__setattr__(self, "session_ref", session_ref)
        object.__setattr__(self, "provider_session_id", provider_session_id)
        object.__setattr__(self, "context_ref", context_ref)


@dataclass(frozen=True, slots=True, init=False)
class SendRequest(MutationRequest):
    """Send one steer/message to the active provider turn."""

    text: str

    def __init__(
        self,
        operation: str | PendingProviderOperation | None = None,
        idempotency_key: str | None = None,
        text: str = "",
        *,
        operation_id: str | None = None,
        lane_id: str = "host:lane:instance",
        provider_instance_id: str = "default",
        provider_generation: int = 1,
        payload_digest: str = "",
        provider_handle: str = DEFAULT_PROVIDER_HANDLE,
        created_at: str = DEFAULT_CREATED_AT,
        attempt: int = 1,
    ) -> None:
        _required_text(text, "text")
        envelope = self._build_operation(
            operation,
            idempotency_key,
            operation_id=operation_id,
            kind="send",
            lane_id=lane_id,
            provider_instance_id=provider_instance_id,
            provider_generation=provider_generation,
            payload_digest=payload_digest,
            payload_material=(text,),
            provider_handle=provider_handle,
            created_at=created_at,
            attempt=attempt,
        )
        object.__setattr__(self, "operation", envelope)
        object.__setattr__(self, "text", text)


@dataclass(frozen=True, slots=True, init=False)
class CheckpointRequest(MutationRequest):
    """Ask the provider to persist a recoverable checkpoint."""

    label: str

    def __init__(
        self,
        operation: str | PendingProviderOperation | None = None,
        idempotency_key: str | None = None,
        label: str = "checkpoint",
        *,
        operation_id: str | None = None,
        lane_id: str = "host:lane:instance",
        provider_instance_id: str = "default",
        provider_generation: int = 1,
        payload_digest: str = "",
        provider_handle: str = DEFAULT_PROVIDER_HANDLE,
        created_at: str = DEFAULT_CREATED_AT,
        attempt: int = 1,
    ) -> None:
        _required_text(label, "label")
        envelope = self._build_operation(
            operation,
            idempotency_key,
            operation_id=operation_id,
            kind="checkpoint",
            lane_id=lane_id,
            provider_instance_id=provider_instance_id,
            provider_generation=provider_generation,
            payload_digest=payload_digest,
            payload_material=(label,),
            provider_handle=provider_handle,
            created_at=created_at,
            attempt=attempt,
        )
        object.__setattr__(self, "operation", envelope)
        object.__setattr__(self, "label", label)


@dataclass(frozen=True, slots=True, init=False)
class CancelCurrentTurnRequest(MutationRequest):
    """Cancel the currently running turn, if one exists."""

    reason: str

    def __init__(
        self,
        operation: str | PendingProviderOperation | None = None,
        idempotency_key: str | None = None,
        reason: str = "cancelled by monitor",
        *,
        operation_id: str | None = None,
        lane_id: str = "host:lane:instance",
        provider_instance_id: str = "default",
        provider_generation: int = 1,
        payload_digest: str = "",
        provider_handle: str = DEFAULT_PROVIDER_HANDLE,
        created_at: str = DEFAULT_CREATED_AT,
        attempt: int = 1,
    ) -> None:
        _required_text(reason, "reason")
        envelope = self._build_operation(
            operation,
            idempotency_key,
            operation_id=operation_id,
            kind="cancel_current_turn",
            lane_id=lane_id,
            provider_instance_id=provider_instance_id,
            provider_generation=provider_generation,
            payload_digest=payload_digest,
            payload_material=(reason,),
            provider_handle=provider_handle,
            created_at=created_at,
            attempt=attempt,
        )
        object.__setattr__(self, "operation", envelope)
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True, slots=True, init=False)
class CloseRequest(MutationRequest):
    """Close a session, optionally retaining it for a later resume."""

    archive: bool

    def __init__(
        self,
        operation: str | PendingProviderOperation | None = None,
        idempotency_key: str | None = None,
        archive: bool = True,
        *,
        operation_id: str | None = None,
        lane_id: str = "host:lane:instance",
        provider_instance_id: str = "default",
        provider_generation: int = 1,
        payload_digest: str = "",
        provider_handle: str = DEFAULT_PROVIDER_HANDLE,
        created_at: str = DEFAULT_CREATED_AT,
        attempt: int = 1,
    ) -> None:
        if not isinstance(archive, bool):
            raise ValueError("archive must be a boolean")
        envelope = self._build_operation(
            operation,
            idempotency_key,
            operation_id=operation_id,
            kind="close",
            lane_id=lane_id,
            provider_instance_id=provider_instance_id,
            provider_generation=provider_generation,
            payload_digest=payload_digest,
            payload_material=(archive,),
            provider_handle=provider_handle,
            created_at=created_at,
            attempt=attempt,
        )
        object.__setattr__(self, "operation", envelope)
        object.__setattr__(self, "archive", archive)


# Short aliases keep call sites readable while retaining the explicit method
# name in the protocol.
CancelRequest = CancelCurrentTurnRequest


@dataclass(frozen=True, slots=True)
class ProviderUpdate:
    """One immutable, cursor-addressable provider event."""

    event_id: str
    cursor: Cursor
    kind: UpdateKind | str
    provider_session_id: str | None
    observed_at: str
    operation_id: str
    lane_id: str
    idempotency_key: str | None = None
    payload_digest: str | None = None
    provider_instance_id: str = "default"
    provider_generation: int = 1
    payload: Mapping[str, object] = field(default_factory=dict)
    child_roster: tuple[ChildRosterEntry, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.event_id, "event_id")
        _required_text(self.cursor, "cursor")
        _required_text(str(self.kind), "kind")
        _optional_text(self.provider_session_id, "provider_session_id")
        _required_text(self.observed_at, "observed_at")
        _required_text(self.operation_id, "operation_id")
        _required_text(self.lane_id, "lane_id")
        _optional_text(self.idempotency_key, "idempotency_key")
        _optional_text(self.payload_digest, "payload_digest")
        _required_text(self.provider_instance_id, "provider_instance_id")
        if isinstance(self.provider_generation, bool) or self.provider_generation < 1:
            raise ValueError("provider_generation must be a positive integer")
        if not isinstance(self.child_roster, tuple) or not all(isinstance(entry, ChildRosterEntry) for entry in self.child_roster):
            raise ValueError("child_roster must contain canonical ChildRosterEntry values")


@dataclass(frozen=True, slots=True)
class ReadUpdatesResult:
    """A bounded update read that can safely report an unavailable provider."""

    requested_cursor: Cursor | None
    next_cursor: Cursor
    updates: tuple[ProviderUpdate, ...] = ()
    provider_available: bool = True
    complete: bool = True
    reason: str = ""

    @property
    def unknown(self) -> bool:
        return not self.provider_available or not self.complete

    @property
    def events(self) -> tuple[ProviderUpdate, ...]:
        """Compatibility spelling for callers that call updates events."""
        return self.updates


UpdateBatch = ReadUpdatesResult


MutationResult = ProviderOperationResult
CloseResult = CloseArchiveResult
CreateOrResumeResult = ProviderOperationResult
SendResult = ProviderOperationResult
CheckpointResult = ProviderOperationResult
CancelResult = ProviderOperationResult


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Current provider observation, including freshness and context."""

    provider: ProviderName | str
    state: ProviderState
    provider_session_id: str | None
    generation: int
    fresh: bool
    provider_available: bool
    context_available: bool | None = None
    current_turn_id: str | None = None
    last_event_id: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        _required_text(str(self.provider), "provider")
        if isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        _optional_text(self.provider_session_id, "provider_session_id")
        _optional_text(self.current_turn_id, "current_turn_id")
        _optional_text(self.last_event_id, "last_event_id")

    @property
    def unknown(self) -> bool:
        return not self.provider_available or self.state is ProviderState.UNKNOWN or not self.fresh

    @property
    def stale(self) -> bool:
        return not self.fresh or self.state is ProviderState.STALE


UsageResult = UsageReport
UsageSnapshot = UsageReport


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


@runtime_checkable
class Provider(Protocol):
    """The minimal lifecycle protocol implemented by provider adapters."""

    @property
    def provider_name(self) -> ProviderName | str: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def create_or_resume(self, request: CreateOrResumeRequest) -> CreateOrResumeResult: ...

    def status(self) -> ProviderStatus: ...

    def send(self, request: SendRequest) -> SendResult: ...

    def read_updates(self, cursor: Cursor | None = None) -> ReadUpdatesResult: ...

    def checkpoint(self, request: CheckpointRequest) -> CheckpointResult: ...

    def usage(self) -> UsageResult: ...

    def cancel_current_turn(self, request: CancelCurrentTurnRequest) -> CancelResult: ...

    def close(self, request: CloseRequest) -> CloseResult: ...


ProviderProtocol = Provider


class AbstractProvider(ABC):
    """Optional ABC for adapters that prefer inheritance over duck typing."""

    @property
    @abstractmethod
    def provider_name(self) -> ProviderName | str:
        raise NotImplementedError

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    def create_or_resume(self, request: CreateOrResumeRequest) -> CreateOrResumeResult:
        raise NotImplementedError

    @abstractmethod
    def status(self) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def send(self, request: SendRequest) -> SendResult:
        raise NotImplementedError

    @abstractmethod
    def read_updates(self, cursor: Cursor | None = None) -> ReadUpdatesResult:
        raise NotImplementedError

    @abstractmethod
    def checkpoint(self, request: CheckpointRequest) -> CheckpointResult:
        raise NotImplementedError

    @abstractmethod
    def usage(self) -> UsageResult:
        raise NotImplementedError

    @abstractmethod
    def cancel_current_turn(self, request: CancelCurrentTurnRequest) -> CancelResult:
        raise NotImplementedError

    @abstractmethod
    def close(self, request: CloseRequest) -> CloseResult:
        raise NotImplementedError


ProviderABC = AbstractProvider


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
        resolved_provider_instance_id = provider_instance_id or self.provider_instance_id
        resolved_provider_generation = provider_generation or self._generation
        resolved_result_evidence = dict(result_evidence or {})
        resolved_payload = dict(payload or {})
        resolved_payload.update(
            {
                "operation_id": resolved_operation_id,
                "lane_id": resolved_lane_id,
                "idempotency_key": idempotency_key,
                "payload_digest": payload_digest,
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
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
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
    "TOPHAND_FIXTURES",
    "AbstractProvider",
    "CancelCurrentTurnRequest",
    "CancelRequest",
    "CancelResult",
    "ChildRosterEntry",
    "CheckpointRequest",
    "CheckpointResult",
    "CloseRequest",
    "CloseResult",
    "CloseArchiveResult",
    "CloseState",
    "CreateOrResumeRequest",
    "CreateOrResumeResult",
    "Cursor",
    "DeterministicFakeProvider",
    "FakeAmpProvider",
    "FakeProviderScenario",
    "FakeTophandProvider",
    "MutationRequest",
    "MutationResult",
    "OperationKind",
    "OperationStatus",
    "Provider",
    "ProviderABC",
    "ProviderName",
    "ProviderCapabilities",
    "PendingProviderOperation",
    "ProviderOperationResult",
    "ProviderProtocol",
    "ProviderState",
    "ProviderStatus",
    "ProviderUpdate",
    "ReadUpdatesResult",
    "SendRequest",
    "SendResult",
    "TOPHAND_FIXTURES",
    "UpdateBatch",
    "UpdateKind",
    "UsageReport",
    "UsageComponent",
    "UsageResult",
    "UsageSnapshot",
    "fake_provider_scenario",
]

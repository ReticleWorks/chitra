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

Deterministic provider doubles live in test support.  They use a deterministic
event stream and idempotency records so tests can exercise restart, duplicate
event, stale state, context loss, outage, and lost-response paths without a
live Tophand or Amp installation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

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


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


@dataclass(frozen=True, slots=True)
class MutationRequest:
    """Provider mutation carrying one canonical pending-operation envelope."""

    operation: PendingProviderOperation

    def __post_init__(self) -> None:
        if not isinstance(self.operation, PendingProviderOperation):
            raise TypeError("operation must be a PendingProviderOperation")
        if self.operation.provider_instance_id is None:
            raise ValueError("operation envelope provider_instance_id is required")
        if self.operation.provider_generation is None:
            raise ValueError("operation envelope provider_generation is required")

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
        provider_instance_id = self.operation.provider_instance_id
        assert provider_instance_id is not None
        return provider_instance_id

    @property
    def provider_generation(self) -> int:
        provider_generation = self.operation.provider_generation
        assert provider_generation is not None
        return provider_generation

    @property
    def payload_digest(self) -> str:
        return self.operation.payload_digest

    @property
    def provider_handle(self) -> str:
        return self.operation.provider_handle


@dataclass(frozen=True, slots=True)
class CreateOrResumeRequest(MutationRequest):
    """Create a provider session or resume the known session."""

    session_ref: str
    provider_session_id: str | None = None
    context_ref: str | None = None

    def __post_init__(self) -> None:
        MutationRequest.__post_init__(self)
        if self.operation.kind != "create_or_resume":
            raise ValueError("operation envelope kind must be create_or_resume")
        if self.session_ref:
            _required_text(self.session_ref, "session_ref")
        _optional_text(self.provider_session_id, "provider_session_id")
        _optional_text(self.context_ref, "context_ref")


@dataclass(frozen=True, slots=True)
class SendRequest(MutationRequest):
    """Send one steer/message to the active provider turn."""

    text: str

    def __post_init__(self) -> None:
        MutationRequest.__post_init__(self)
        if self.operation.kind != "send":
            raise ValueError("operation envelope kind must be send")
        _required_text(self.text, "text")


@dataclass(frozen=True, slots=True)
class CheckpointRequest(MutationRequest):
    """Ask the provider to persist a recoverable checkpoint."""

    label: str

    def __post_init__(self) -> None:
        MutationRequest.__post_init__(self)
        if self.operation.kind != "checkpoint":
            raise ValueError("operation envelope kind must be checkpoint")
        _required_text(self.label, "label")


@dataclass(frozen=True, slots=True)
class CancelCurrentTurnRequest(MutationRequest):
    """Cancel the currently running turn, if one exists."""

    reason: str

    def __post_init__(self) -> None:
        MutationRequest.__post_init__(self)
        if self.operation.kind != "cancel_current_turn":
            raise ValueError("operation envelope kind must be cancel_current_turn")
        _required_text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class CloseRequest(MutationRequest):
    """Close a session, optionally retaining it for a later resume."""

    archive: bool
    # Chitra may run on a different host from the provider.  The checkpoint
    # therefore crosses the provider boundary as an authenticated document;
    # providers must never discover it by reading Chitra's filesystem.
    checkpoint_receipt: Mapping[str, object] | None = None
    checkpoint_receipt_sha256: str | None = None
    checkpoint_verifier: str | None = None

    def __post_init__(self) -> None:
        MutationRequest.__post_init__(self)
        if self.operation.kind != "close":
            raise ValueError("operation envelope kind must be close")
        if not isinstance(self.archive, bool):
            raise ValueError("archive must be a boolean")
        if self.checkpoint_receipt is not None and not isinstance(self.checkpoint_receipt, Mapping):
            raise ValueError("checkpoint_receipt must be a mapping when supplied")


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
    idempotency_key: str
    payload_digest: str
    provider_instance_id: str
    provider_generation: int
    # The provider operation/thread handle is distinct from the physical
    # session ID above.  Adapters that can supply both should do so; legacy
    # events may carry the handle in their payload for migration.
    provider_handle: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    child_roster: tuple[ChildRosterEntry, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.event_id, "event_id")
        _required_text(self.cursor, "cursor")
        _required_text(str(self.kind), "kind")
        _optional_text(self.provider_session_id, "provider_session_id")
        _optional_text(self.provider_handle, "provider_handle")
        _required_text(self.observed_at, "observed_at")
        _required_text(self.operation_id, "operation_id")
        _required_text(self.lane_id, "lane_id")
        _required_text(self.idempotency_key, "idempotency_key")
        _required_text(self.payload_digest, "payload_digest")
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


__all__ = [
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
    "MutationRequest",
    "MutationResult",
    "OperationKind",
    "OperationStatus",
    "PendingProviderOperation",
    "Provider",
    "ProviderABC",
    "ProviderCapabilities",
    "ProviderName",
    "ProviderOperationResult",
    "ProviderProtocol",
    "ProviderState",
    "ProviderStatus",
    "ProviderUpdate",
    "ReadUpdatesResult",
    "SendRequest",
    "SendResult",
    "UpdateBatch",
    "UpdateKind",
    "UsageComponent",
    "UsageReport",
    "UsageResult",
    "UsageSnapshot",
]

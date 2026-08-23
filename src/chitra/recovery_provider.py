"""The narrow production seam between recovery and provider adapters.

Chitra owns the joined-lane record, pending operation, cursor, result, event,
checkpoint, and cancellation evidence.  Provider adapters receive those
boundaries; they do not create a second state store or discover an adapter by
importing arbitrary code.

This module only assembles an injected resolver.  It does not start a
provider, read credentials, or contact a live system.  A missing factory,
unknown provider kind, missing Chitra-owned boundary, unavailable
operating-facts reader, or factory failure returns ``None``.  ``None`` is the
canonical unknown provider result consumed by recovery, which keeps recovery
waiting instead of guessing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import structlog

from ._fsio import locked_json_store
from .detect.rescue import RecoveryCheckpointBinding, find_recovery_checkpoint_receipt
from .joined_lane import JoinedLaneStore
from .journal import EventJournal
from .journal.models import CanonicalEvent
from .lane_config import LaneSpec
from .operating_facts import OperatingFactsSources, read_operating_facts
from .provider_protocol import (
    CancelCurrentTurnRequest,
    CheckpointRequest,
    CloseRequest,
    CreateOrResumeRequest,
    Provider,
    ProviderCapabilities,
    ProviderName,
    ProviderOperationResult,
    ProviderState,
    ProviderStatus,
    ProviderUpdate,
    ReadUpdatesResult,
    SendRequest,
    UsageReport,
)
from .recovery import RecoveryProviderResolver
from .session_contract import (
    JoinedLaneRecord,
    LaneUpdate,
    OperatingFact,
    PendingProviderOperation,
    ProviderIdentity,
    validate_update,
)

try:
    # Fleet packages this exact module under /opt/polyphony/deploy-main.  The
    # import is deliberately static and allowlisted; a missing package keeps
    # recovery unavailable instead of selecting an arbitrary adapter.
    from tools.support.chitra_adapter.tophand_adapter import (  # type: ignore[import-untyped]
        build_tophand_provider as _imported_tophand_builder,
    )
except ImportError:  # pragma: no cover - exercised by source-only installs
    _packaged_tophand_builder: Callable[..., object] | None = None
else:
    _packaged_tophand_builder = cast(Callable[..., object], _imported_tophand_builder)

logger = structlog.get_logger(__name__)

RecoverySink = Callable[[object], object | None]
RecoveryVerifier = Callable[[object], bool | None]
RecoveryFactsReader = Callable[[JoinedLaneRecord], Sequence[OperatingFact]]


class RecoveryProviderFactory(Protocol):
    """Factory contract for one explicitly allowlisted provider adapter.

    The factory receives the canonical provider identity plus all Chitra-owned
    evidence boundaries.  It may return ``None`` when the adapter is not
    currently available.  A factory must not replace any of these boundaries
    with provider-local persistence.
    """

    def __call__(
        self,
        *,
        identity: ProviderIdentity,
        lane: LaneSpec,
        record: JoinedLaneRecord,
        state_root: Path,
        pending_sink: RecoverySink,
        cursor_sink: RecoverySink,
        result_sink: RecoverySink,
        event_sink: RecoverySink,
        checkpoint_verifier: RecoveryVerifier,
        cancel_verifier: RecoveryVerifier,
        facts_reader: RecoveryFactsReader,
        operating_facts: tuple[OperatingFact, ...],
    ) -> Provider | None: ...


@dataclass(frozen=True, slots=True)
class RecoveryProviderBindings:
    """Chitra-owned dependencies passed to one provider factory call."""

    lane: LaneSpec
    state_root: Path
    pending_sink: RecoverySink
    cursor_sink: RecoverySink
    result_sink: RecoverySink
    event_sink: RecoverySink
    checkpoint_verifier: RecoveryVerifier
    cancel_verifier: RecoveryVerifier
    facts_reader: RecoveryFactsReader


def _unavailable_sink(_value: object) -> None:
    """Default sink that cannot claim to have persisted provider evidence."""


def _unknown_verifier(_value: object) -> bool:
    """Default verifier that never authorizes an unproved mutation."""

    return False


def _default_facts_reader(
    sources: OperatingFactsSources | None,
) -> RecoveryFactsReader:
    """Read only the explicit versioned operating-facts projection."""

    def read(_record: JoinedLaneRecord) -> tuple[OperatingFact, ...]:
        return read_operating_facts(sources).facts

    return read


def build_recovery_facts_reader(
    sources: OperatingFactsSources | None = None,
) -> RecoveryFactsReader:
    """Build the read-only production operating-facts snapshot reader."""

    return _default_facts_reader(sources)


# Keep the public name used by the production dispatch seam.  The longer name
# above remains for callers that adopted the initial integration spelling.
default_operating_facts_reader = build_recovery_facts_reader


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _operation_dict(operation: object) -> dict[str, object]:
    if isinstance(operation, PendingProviderOperation):
        return cast(dict[str, object], operation.model_dump(mode="json"))
    if isinstance(operation, Mapping):
        return {str(key): value for key, value in operation.items()}
    raise TypeError("provider operation must be a canonical mapping")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return cast(Mapping[str, object], dumped)
    raise TypeError(f"{name} must be a mapping")


def _provider_result(
    value: object,
    operation: PendingProviderOperation,
) -> ProviderOperationResult:
    """Translate the packaged adapter result into Chitra's typed result."""

    raw = _mapping(value, "Tophand provider result")
    for field in (
        "operation_id",
        "kind",
        "lane_id",
        "provider_handle",
        "idempotency_key",
        "payload_digest",
        "provider_instance_id",
        "provider_generation",
    ):
        observed = raw.get(field)
        expected = getattr(operation, field)
        if observed is not None and observed != expected:
            raise ValueError(f"Tophand provider result {field} changed")
    status = raw.get("status")
    if status not in {"accepted", "consumed", "rejected", "unknown", "lost-response"}:
        status = "unknown"
    accepted: bool | None
    consumed: bool | None
    if status == "consumed":
        accepted, consumed = True, True
    elif status == "accepted":
        accepted, consumed = True, None
    elif status == "rejected":
        accepted, consumed = False, None
    else:
        accepted, consumed = None, None
    observed_at = raw.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at.strip():
        observed_at = _now()
    evidence = raw.get("evidence")
    return ProviderOperationResult(
        operation_id=operation.operation_id,
        kind=operation.kind,
        lane_id=operation.lane_id,
        provider_handle=operation.provider_handle,
        idempotency_key=operation.idempotency_key,
        payload_digest=operation.payload_digest,
        provider_instance_id=operation.provider_instance_id,
        provider_generation=operation.provider_generation,
        status=cast(Any, status),
        accepted=accepted,
        consumed=consumed,
        observed_at=observed_at,
        evidence=evidence if isinstance(evidence, str) else "",
    )


class _PackagedTophandProvider:
    """Typed Chitra facade over the allowlisted Fleet Tophand adapter.

    The Fleet adapter deliberately exposes a mapping boundary because it is
    also used by its command wrapper.  Recovery uses Chitra's stricter typed
    request/result models.  This facade performs only that translation and
    keeps every durable evidence callback pointed at Chitra-owned sinks.
    """

    def __init__(
        self,
        adapter: object,
        *,
        result_sink: RecoverySink,
    ) -> None:
        self._adapter = adapter
        self._result_sink = result_sink

    @property
    def provider_name(self) -> ProviderName:
        return ProviderName.TOPHAND

    @property
    def capabilities(self) -> ProviderCapabilities:
        raw = getattr(self._adapter, "capabilities", {})
        if not isinstance(raw, Mapping):
            return ProviderCapabilities()
        supported = tuple(
            name
            for name in (
                "create_or_resume",
                "status",
                "send",
                "read_updates",
                "checkpoint",
                "usage",
                "cancel_current_turn",
                "close",
            )
            if raw.get(name) is True
        )
        return ProviderCapabilities.from_supported(cast(Any, supported))

    def _call(
        self,
        method: str,
        request: object,
        operation: PendingProviderOperation,
    ) -> ProviderOperationResult:
        payload: dict[str, object] = {"operation": _operation_dict(operation)}
        if isinstance(request, SendRequest):
            payload["text"] = request.text
        elif isinstance(request, CheckpointRequest):
            payload["label"] = request.label
        elif isinstance(request, CreateOrResumeRequest):
            payload.update(
                {
                    "session_ref": request.session_ref,
                    "provider_session_id": request.provider_session_id,
                    "context_ref": request.context_ref,
                }
            )
        elif isinstance(request, CancelCurrentTurnRequest):
            payload["reason"] = request.reason
        elif isinstance(request, CloseRequest):
            payload["archive"] = request.archive
        raw = getattr(self._adapter, method)(payload)
        result = _provider_result(raw, operation)
        self._result_sink(raw)
        return result

    def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
        return self._call("create_or_resume", request, request.operation)

    def status(self) -> ProviderStatus:
        adapter = cast(Any, self._adapter)
        raw = _mapping(adapter.status(), "Tophand status")
        state = raw.get("state", "unknown")
        if state not in {item.value for item in ProviderState}:
            state = "unknown"
        provider_session_id = raw.get("provider_session_id")
        generation = raw.get("generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int):
            generation = 0
        context_available = raw.get("context_available")
        current_turn_id = raw.get("current_turn_id")
        last_event_id = raw.get("last_event_id")
        reason = raw.get("reason")
        return ProviderStatus(
            provider=ProviderName.TOPHAND,
            state=cast(Any, state),
            provider_session_id=provider_session_id if isinstance(provider_session_id, str) else None,
            generation=generation,
            fresh=raw.get("fresh") is True,
            provider_available=raw.get("provider_available") is True,
            context_available=context_available if isinstance(context_available, bool) else None,
            current_turn_id=current_turn_id if isinstance(current_turn_id, str) else None,
            last_event_id=last_event_id if isinstance(last_event_id, str) else None,
            reason=reason if isinstance(reason, str) else "",
        )

    def send(self, request: SendRequest) -> ProviderOperationResult:
        return self._call("send", request, request.operation)

    def read_updates(self, cursor: str | None = None) -> ReadUpdatesResult:
        adapter = cast(Any, self._adapter)
        raw = _mapping(adapter.read_updates(cursor), "Tophand update batch")
        values = raw.get("updates", ())
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise TypeError("Tophand updates must be a sequence")
        updates: list[ProviderUpdate] = []
        for value in values:
            item = _mapping(value, "Tophand update")
            operation_id = item.get("operation_id")
            event_id = item.get("event_id")
            event_cursor = item.get("cursor")
            if not all(isinstance(field, str) and field for field in (operation_id, event_id, event_cursor)):
                continue
            kind = item.get("kind", "unknown")
            payload = item.get("payload", {})
            if not isinstance(payload, Mapping):
                payload = {}
            observed_at = item.get("observed_at")
            if not isinstance(observed_at, str) or not observed_at:
                observed_at = _now()
            generation = item.get("provider_generation", 0)
            if isinstance(generation, bool) or not isinstance(generation, int):
                continue
            instance_id = item.get("provider_instance_id")
            lane_id = item.get("lane_id")
            provider_handle = item.get("provider_handle")
            idempotency_key = item.get("idempotency_key")
            payload_digest = item.get("payload_digest")
            if not all(
                isinstance(field, str) and field
                for field in (instance_id, lane_id, provider_handle, idempotency_key, payload_digest)
            ):
                continue
            event_id_text = cast(str, event_id)
            event_cursor_text = cast(str, event_cursor)
            operation_id_text = cast(str, operation_id)
            lane_id_text = cast(str, lane_id)
            idempotency_key_text = cast(str, idempotency_key)
            payload_digest_text = cast(str, payload_digest)
            instance_id_text = cast(str, instance_id)
            provider_handle_text = cast(str, provider_handle)
            provider_session_id = item.get("provider_session_id")
            provider_session_id_text = provider_session_id if isinstance(provider_session_id, str) else None
            updates.append(
                ProviderUpdate(
                    event_id=event_id_text,
                    cursor=event_cursor_text,
                    kind=cast(Any, kind),
                    provider_session_id=provider_session_id_text,
                    observed_at=observed_at,
                    operation_id=operation_id_text,
                    lane_id=lane_id_text,
                    idempotency_key=idempotency_key_text,
                    payload_digest=payload_digest_text,
                    provider_instance_id=instance_id_text,
                    provider_generation=generation,
                    provider_handle=provider_handle_text,
                    payload=cast(Mapping[str, object], payload),
                )
            )
        next_cursor = raw.get("next_cursor", raw.get("cursor"))
        reason = raw.get("reason")
        return ReadUpdatesResult(
            requested_cursor=cursor,
            next_cursor=next_cursor if isinstance(next_cursor, str) else cursor or "",
            updates=tuple(updates),
            provider_available=raw.get("provider_available") is not False,
            complete=raw.get("complete") is not False,
            reason=reason if isinstance(reason, str) else "",
        )

    def checkpoint(self, request: CheckpointRequest) -> ProviderOperationResult:
        return self._call("checkpoint", request, request.operation)

    def usage(self) -> UsageReport:
        raise RuntimeError("Tophand usage is not part of recovery supervision")

    def cancel_current_turn(self, request: CancelCurrentTurnRequest) -> ProviderOperationResult:
        return self._call("cancel_current_turn", request, request.operation)

    def close(self, request: CloseRequest) -> object:
        raise RuntimeError("Tophand close is not part of recovery supervision")


def _canonical_tophand_factory(
    *,
    identity: ProviderIdentity,
    lane: LaneSpec,
    record: JoinedLaneRecord,
    state_root: Path,
    pending_sink: RecoverySink,
    cursor_sink: RecoverySink,
    result_sink: RecoverySink,
    event_sink: RecoverySink,
    checkpoint_verifier: RecoveryVerifier,
    cancel_verifier: RecoveryVerifier,
    facts_reader: RecoveryFactsReader,
    operating_facts: tuple[OperatingFact, ...],
) -> Provider | None:
    """Construct the only packaged production provider route.

    Identity and restart fences must be present in Chitra's record.  The
    adapter receives snapshots and callbacks but no authority to create a
    second state store.  The facts argument is intentionally consumed only as
    an explicit availability gate; the adapter cannot infer a route from it.
    """

    del facts_reader
    if identity.kind != "tophand" or _packaged_tophand_builder is None:
        return None
    if identity.instance_id is None or identity.generation is None:
        return None
    del operating_facts
    pending = () if record.pending_operation is None else (record.pending_operation.model_dump(mode="json"),)
    completed = () if record.last_operation_result is None else (record.last_operation_result.model_dump(mode="json"),)
    journal = EventJournal(state_root, lane.identifier)
    seen_event_ids = tuple(event.event_id for event in journal.load())
    seen_keys = tuple(item.idempotency_key for item in record.operation_history)
    try:
        adapter = _packaged_tophand_builder(
            lane_id=record.lane_id,
            goal_id=record.goal_id,
            session_ref=record.session_ref,
            provider_session_id=identity.provider_session_id or record.session_ref,
            provider_handle=identity.handle,
            provider_instance_id=identity.instance_id,
            provider_generation=identity.generation,
            state_dir=state_root,
            cursor=record.update_cursor or None,
            pending_operations=pending,
            completed_results=completed,
            seen_event_ids=seen_event_ids,
            seen_idempotency_keys=seen_keys,
            update_sink=_canonical_update_sink(lane),
            result_sink=result_sink,
            cursor_sink=cursor_sink,
            event_sink=event_sink,
            checkpoint_verifier=checkpoint_verifier,
            cancel_verifier=cancel_verifier,
        )
    except Exception as exc:  # noqa: BLE001 - unavailable provider is canonical unknown
        logger.warning("packaged_tophand_unavailable", lane_id=record.lane_id, error=str(exc))
        return None
    if adapter is None:
        return None
    provider = _PackagedTophandProvider(adapter, result_sink=result_sink)
    return provider if isinstance(provider, Provider) else None


def _canonical_recovery_bindings(
    lane: LaneSpec,
) -> tuple[RecoverySink, RecoverySink, RecoverySink, RecoverySink, RecoveryVerifier, RecoveryVerifier]:
    """Return Chitra-owned evidence callbacks for the packaged adapter."""

    results_path = lane.state_dir / "provider-results" / f"{lane.identifier}.jsonl"
    journal = EventJournal(lane.state_dir, lane.identifier)

    def pending_sink(value: object) -> None:
        # RecoveryEngine persists the pending envelope before invoking the
        # provider.  Validate the callback payload but never let an adapter
        # perform a second joined-lane state transition.
        _operation_dict(value)

    def cursor_sink(value: object) -> None:
        if value is not None and not isinstance(value, str):
            raise TypeError("provider cursor must be text or null")

    def result_sink(value: object) -> None:
        raw = dict(_mapping(value, "provider result"))
        current = JoinedLaneStore(lane.state_dir).load(lane.identifier)
        operation_id = raw.get("operation_id")
        if current is None or not isinstance(operation_id, str):
            raise ValueError("provider result has no matching Chitra pending operation")
        operation = current.pending_operation
        if operation is None:
            prior = current.last_operation_result
            reference = next(
                (item for item in current.operation_history if item.operation_id == operation_id),
                None,
            )
            if prior is None or prior.operation_id != operation_id or reference is None:
                raise ValueError("provider result has no matching Chitra operation history")
            operation = PendingProviderOperation(
                operation_id=prior.operation_id,
                kind=prior.kind,
                lane_id=prior.lane_id,
                provider_handle=prior.provider_handle,
                provider_session_id=current.provider.provider_session_id or current.session_ref,
                idempotency_key=prior.idempotency_key,
                payload_digest=prior.payload_digest,
                provider_instance_id=prior.provider_instance_id,
                provider_generation=prior.provider_generation,
                created_at=reference.created_at,
            )
        elif operation.operation_id != operation_id:
            raise ValueError("provider result has no matching Chitra pending operation")
        canonical = _provider_result(raw, operation)
        raw = canonical.model_dump(mode="json")
        operation_id = canonical.operation_id
        path = results_path
        with locked_json_store(path):
            existing: set[str] = set()
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        parsed = _mapping(json.loads(line), "provider result row")
                        prior = parsed.get("operation_id")
                        if isinstance(prior, str):
                            existing.add(prior)
            if operation_id in existing:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n")

    def event_sink(value: object) -> None:
        event = CanonicalEvent.model_validate(value)
        journal.append((event,))

    def checkpoint_verifier(value: object) -> bool:
        current = JoinedLaneStore(lane.state_dir).load(lane.identifier)
        if current is None or current.pending_operation is None:
            return False
        operation = current.pending_operation
        cycle_id = current.recovery.cycle_id
        sequence = current.recovery.event_sequence
        if cycle_id is None or sequence is None or operation.provider_instance_id is None or operation.provider_generation is None:
            return False
        binding = RecoveryCheckpointBinding(
            lane_id=current.lane_id,
            goal_id=current.goal_id,
            session_ref=current.session_ref,
            cycle_id=cycle_id,
            operation_id=operation.operation_id,
            provider_handle=operation.provider_handle,
            provider_session_id=operation.provider_session_id,
            provider_instance_id=operation.provider_instance_id,
            provider_generation=operation.provider_generation,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            event_sequence=sequence,
        )
        del value
        return find_recovery_checkpoint_receipt(lane.state_dir, binding) is not None

    def cancel_verifier(_value: object) -> bool:
        # Cancellation needs an exact transcript-boundary receipt.  Until
        # Chitra observes one, the adapter cannot claim that a turn stopped.
        return False

    return pending_sink, cursor_sink, result_sink, event_sink, checkpoint_verifier, cancel_verifier


def _canonical_update_sink(lane: LaneSpec) -> RecoverySink:
    """Persist provider session updates through the joined-lane store."""

    store = JoinedLaneStore(lane.state_dir)

    def update_sink(value: object) -> None:
        update = LaneUpdate.from_dict(value)

        def apply(current: JoinedLaneRecord) -> JoinedLaneRecord:
            if update.lane_id != current.lane_id or update.goal_id != current.goal_id:
                raise ValueError("provider session update identity changed")
            if current.current_update is not None:
                validate_update(current.current_update, update)
            return current.model_copy(update={"current_update": update})

        store.update(lane.identifier, apply)

    return update_sink


def _factory_map(
    *,
    tophand_factory: RecoveryProviderFactory | None,
    amp_factory: RecoveryProviderFactory | None,
    provider_factories: Mapping[str, RecoveryProviderFactory] | None,
) -> dict[str, RecoveryProviderFactory | None]:
    """Build the closed provider allowlist from injected callables only."""

    supplied = provider_factories or {}
    return {
        "tophand": tophand_factory if tophand_factory is not None else supplied.get("tophand"),
        "amp": amp_factory if amp_factory is not None else supplied.get("amp"),
    }


def build_recovery_provider_resolver(
    lane: LaneSpec,
    *,
    tophand_factory: RecoveryProviderFactory | None = None,
    amp_factory: RecoveryProviderFactory | None = None,
    provider_factories: Mapping[str, RecoveryProviderFactory] | None = None,
    pending_sink: RecoverySink | None = None,
    cursor_sink: RecoverySink | None = None,
    result_sink: RecoverySink | None = None,
    event_sink: RecoverySink | None = None,
    checkpoint_verifier: RecoveryVerifier | None = None,
    cancel_verifier: RecoveryVerifier | None = None,
    facts_reader: RecoveryFactsReader | None = None,
    operating_facts_reader: RecoveryFactsReader | None = None,
    operating_facts_sources: OperatingFactsSources | None = None,
) -> RecoveryProviderResolver:
    """Build a fail-closed resolver for one rendered lane.

    ``ProviderIdentity.kind`` is the only route selector.  The two accepted
    keys are ``tophand`` and ``amp``; arbitrary strings are ignored, and no
    module or executable is discovered from a record or a lane manifest.
    Dependencies are captured once and passed through unchanged on each
    resolution.  The resolver reads operating facts only when a matching
    injected factory is selected, so an unavailable factory does not touch
    the filesystem or a provider.
    """

    # The no-argument form is the shipped production path.  Keep the generic
    # injected form fail-closed, but bind that path to the one explicit
    # packaged Tophand adapter and Chitra-owned evidence callbacks when the
    # caller has not supplied an alternate test or integration seam.
    production_defaults = all(
        dependency is None
        for dependency in (
            tophand_factory,
            amp_factory,
            provider_factories,
            pending_sink,
            cursor_sink,
            result_sink,
            event_sink,
            checkpoint_verifier,
            cancel_verifier,
        )
    )
    if production_defaults:
        (
            pending_sink,
            cursor_sink,
            result_sink,
            event_sink,
            checkpoint_verifier,
            cancel_verifier,
        ) = _canonical_recovery_bindings(lane)
        tophand_factory = _canonical_tophand_factory

    factories = _factory_map(
        tophand_factory=tophand_factory,
        amp_factory=amp_factory,
        provider_factories=provider_factories,
    )
    resolved_facts_reader = (
        operating_facts_reader
        if operating_facts_reader is not None
        else facts_reader
        if facts_reader is not None
        else _default_facts_reader(operating_facts_sources)
    )
    bindings = RecoveryProviderBindings(
        lane=lane,
        state_root=lane.state_dir,
        pending_sink=pending_sink or _unavailable_sink,
        cursor_sink=cursor_sink or _unavailable_sink,
        result_sink=result_sink or _unavailable_sink,
        event_sink=event_sink or _unavailable_sink,
        checkpoint_verifier=checkpoint_verifier or _unknown_verifier,
        cancel_verifier=cancel_verifier or _unknown_verifier,
        facts_reader=resolved_facts_reader,
    )
    boundaries_complete = all(
        dependency is not None
        for dependency in (
            pending_sink,
            cursor_sink,
            result_sink,
            event_sink,
            checkpoint_verifier,
            cancel_verifier,
        )
    )

    def resolve(record: JoinedLaneRecord) -> Provider | None:
        if record.lane_id != lane.identifier:
            return None
        kind = record.provider.kind
        if not isinstance(kind, str):
            return None
        factory = factories.get(kind)
        if factory is None:
            return None
        if not boundaries_complete:
            return None
        try:
            operating_facts = tuple(bindings.facts_reader(record))
            provider = factory(
                identity=record.provider,
                lane=lane,
                record=record,
                state_root=bindings.state_root,
                pending_sink=bindings.pending_sink,
                cursor_sink=bindings.cursor_sink,
                result_sink=bindings.result_sink,
                event_sink=bindings.event_sink,
                checkpoint_verifier=bindings.checkpoint_verifier,
                cancel_verifier=bindings.cancel_verifier,
                facts_reader=bindings.facts_reader,
                operating_facts=operating_facts,
            )
            if provider is None or not isinstance(provider, Provider):
                return None
            provider_name = getattr(provider, "provider_name", None)
            if provider_name != kind:
                return None
            return provider
        except Exception as exc:  # noqa: BLE001 -- adapter availability is an unknown, never a dispatch failure
            logger.warning(
                "recovery_provider_unavailable",
                lane_id=record.lane_id,
                provider_kind=kind,
                reason=str(exc),
            )
            return None

    return resolve


__all__ = [
    "RecoveryFactsReader",
    "RecoveryProviderBindings",
    "RecoveryProviderFactory",
    "build_recovery_facts_reader",
    "default_operating_facts_reader",
    "RecoverySink",
    "RecoveryVerifier",
    "build_recovery_provider_resolver",
]

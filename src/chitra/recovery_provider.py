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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

from .lane_config import LaneSpec
from .operating_facts import OperatingFactsSources, read_operating_facts
from .provider_protocol import Provider
from .recovery import RecoveryProviderResolver
from .session_contract import JoinedLaneRecord, OperatingFact, ProviderIdentity

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
    "RecoverySink",
    "RecoveryVerifier",
    "build_recovery_provider_resolver",
]

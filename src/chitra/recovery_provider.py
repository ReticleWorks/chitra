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

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import structlog

from ._fsio import locked_json_store
from .detect.rescue import (
    RecoveryCheckpointBinding,
    find_recovery_checkpoint_receipt,
    verify_checkpoint_receipt_signature,
)
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
    ChildRosterEntry,
    CloseArchiveResult,
    JoinedLaneRecord,
    LaneUpdate,
    OperatingFact,
    PendingProviderOperation,
    ProviderIdentity,
    validate_update,
)
from .usage_policy import launch_policy_problem

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

try:
    # Amp is an optional, disabled-by-policy production capability.  Keep the
    # import path closed and explicit so a lane cannot select arbitrary code.
    from tools.support.chitra_adapter.amp_adapter import (  # type: ignore[import-untyped]
        AmpAdapter as _imported_amp_adapter,
    )
    from tools.support.chitra_adapter.amp_cli_transport import (  # type: ignore[import-untyped]
        AmpCliProfile as _imported_amp_profile,
    )
    from tools.support.chitra_adapter.amp_cli_transport import (
        AmpCliTransport as _imported_amp_transport,
    )
except ImportError:  # pragma: no cover - exercised by source-only installs
    _packaged_amp_adapter: Callable[..., object] | None = None
    _packaged_amp_profile: Callable[..., object] | None = None
    _packaged_amp_transport: Callable[..., object] | None = None
else:
    _packaged_amp_adapter = cast(Callable[..., object], _imported_amp_adapter)
    _packaged_amp_profile = cast(Callable[..., object], _imported_amp_profile)
    _packaged_amp_transport = cast(Callable[..., object], _imported_amp_transport)

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


def _provider_state(value: object) -> ProviderState:
    """Normalize adapter text into the shared status enum."""

    if isinstance(value, ProviderState):
        return value
    try:
        return ProviderState(value) if isinstance(value, str) else ProviderState.UNKNOWN
    except ValueError:
        return ProviderState.UNKNOWN


def _operation_dict(operation: object) -> dict[str, object]:
    if isinstance(operation, PendingProviderOperation):
        return cast(dict[str, object], operation.model_dump(mode="json"))
    if isinstance(operation, Mapping):
        return {str(key): value for key, value in operation.items()}
    raise TypeError("provider operation must be a canonical mapping")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    for method_name in ("model_dump", "to_dict"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            dumped = method(mode="json") if method_name == "model_dump" else method()
        except TypeError:
            dumped = method()
        if isinstance(dumped, Mapping):
            return cast(Mapping[str, object], dumped)
    raise TypeError(f"{name} must be a mapping")


def _provider_result(
    value: object,
    operation: PendingProviderOperation,
    *,
    provider_label: str = "provider",
) -> ProviderOperationResult:
    """Translate the packaged adapter result into Chitra's typed result."""

    raw = _mapping(value, f"{provider_label} provider result")
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
            raise ValueError(f"{provider_label} provider result {field} changed")
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


def _unknown_provider_result(
    operation: PendingProviderOperation,
    evidence: str,
) -> ProviderOperationResult:
    """Return an explicit unknown result without claiming an Amp mutation."""

    return ProviderOperationResult(
        operation_id=operation.operation_id,
        kind=operation.kind,
        lane_id=operation.lane_id,
        provider_handle=operation.provider_handle,
        idempotency_key=operation.idempotency_key,
        payload_digest=operation.payload_digest,
        provider_instance_id=operation.provider_instance_id,
        provider_generation=operation.provider_generation,
        status="unknown",
        accepted=None,
        consumed=None,
        observed_at=_now(),
        evidence=evidence,
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
        state_root: Path,
        result_sink: RecoverySink,
    ) -> None:
        self._adapter = adapter
        self._state_root = state_root
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
        state = _provider_state(raw.get("state", "unknown"))
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
            state=state,
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
        operation = request.operation
        try:
            payload = json.loads(operation.payload)
            if not isinstance(payload, Mapping):
                raise ValueError("Chitra close payload must be an object")
            reference = payload.get("checkpoint_ref")
            if not isinstance(reference, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", reference) is None:
                raise ValueError("Chitra close checkpoint reference is unsafe")
            checkpoint_path = (self._state_root / "checkpoints" / f"{reference}.json").resolve()
            checkpoint_dir = (self._state_root / "checkpoints").resolve()
            if checkpoint_path.parent != checkpoint_dir:
                raise ValueError("Chitra close checkpoint path escaped state root")
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(checkpoint, Mapping):
                raise ValueError("Chitra close checkpoint is not an object")
            receipt = dict(checkpoint)
            if not verify_checkpoint_receipt_signature(receipt, state_root=self._state_root):
                raise ValueError("Chitra close checkpoint signature is invalid")
            if (
                receipt.get("schema_name") != "chitra.governed-close-checkpoint.v1"
                or receipt.get("checkpoint_ref") != reference
                or receipt.get("lane") != operation.lane_id
                or receipt.get("goal_id") != payload.get("goal_id")
                or receipt.get("goal_version") != payload.get("goal_version")
                or receipt.get("session_ref") != payload.get("session_ref")
                or receipt.get("provenance") != {"kind": "governed-completion-checkpoint", "owner": "chitra"}
            ):
                raise ValueError("Chitra close checkpoint logical binding changed")
            binding = receipt.get("provider_binding")
            expected_binding = {
                "kind": "tophand",
                "handle": operation.provider_handle,
                "provider_session_id": operation.provider_session_id,
                "instance_id": operation.provider_instance_id,
                "generation": operation.provider_generation,
            }
            if not isinstance(binding, Mapping) or dict(binding) != expected_binding:
                raise ValueError("Chitra close checkpoint provider binding changed")
            receipt_digest = hashlib.sha256(
                json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            wire_request = {
                "operation": operation.model_dump(mode="json"),
                "archive": True,
                "payload": dict(payload),
                "goal_id": payload.get("goal_id"),
                "lane_id": operation.lane_id,
                "session_ref": payload.get("session_ref"),
                "provider_session_id": operation.provider_session_id,
                "checkpoint_ref": reference,
                "checkpoint_receipt": receipt,
                "checkpoint_receipt_sha256": receipt_digest,
                "checkpoint_verifier": "chitra.detect.rescue.verify_checkpoint_receipt_signature",
            }
            raw = cast(Any, self._adapter).close(wire_request)
            self._result_sink(raw)
            values = dict(_mapping(raw, "Tophand close result"))
            return {
                field: values[field]
                for field in CloseArchiveResult.model_fields
                if field in values
            }
        except Exception as exc:  # noqa: BLE001 - an unproved close remains pending
            return _unknown_close_result(operation, f"Tophand close evidence unavailable: {exc}")


_AMP_CAPABILITY_NAMES = (
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

# Fleet publishes the reviewed Amp executable under this exact category.  The
# ORB lane surface remains the policy/adapter declaration; it is not a second
# runtime source.
_AMP_RUNTIME_FACT_NAMES = frozenset(("fleet.provider-capabilities",))
_TWINRIDGE_AMP_BINARY = "/usr/local/bin/amp"
_TWINRIDGE_AMP_ORB_SIZE = "a1.tiny"


@dataclass(frozen=True, slots=True)
class _AmpRuntimeConfig:
    """One complete Amp declaration projected by Fleet for Chitra."""

    binary: str
    version: str
    orb_size: str
    visibility: str
    fleet_enabled: bool


def _amp_runtime_config(
    operating_facts: Sequence[OperatingFact],
    *,
    expected_project_ref: str,
    expected_profile_digest: str,
    expected_version: str,
) -> _AmpRuntimeConfig | None:
    """Return the reviewed Amp binary and version from current Fleet facts.

    The packaged ``AmpCliTransport`` has a macOS development default.  A
    production lane must never inherit it.  Fleet therefore publishes the
    exact Linux wrapper and its reviewed version under the explicit
    ``provider-capabilities.amp`` record.  Missing,
    stale, conflicting, malformed, or policy-mismatched data leaves the lane
    unavailable instead of selecting a path or version by convention.
    """

    candidates: set[_AmpRuntimeConfig] = set()
    for fact in operating_facts:
        if fact.name not in _AMP_RUNTIME_FACT_NAMES:
            continue
        if fact.state in {"stale", "conflicting", "inaccessible"}:
            return None
        if fact.state != "known":
            continue
        value = fact.value
        if not isinstance(value, Mapping):
            continue
        # Fleet's authoritative shape has the runtime pin in the top-level
        # ``provider-capabilities.amp`` record and the fixed ORB launch inputs
        # in the nested surface.  Both are required for a production lane.
        # They are one fact, not two fallbacks: a disagreement is unsafe and
        # must not be resolved by choosing one source silently.
        surface_value = value.get("orb_lane_surface")
        runtime_value = value.get("amp")
        if surface_value is None and runtime_value is None:
            continue
        if runtime_value is None or surface_value is None:
            return None
        if not isinstance(surface_value, Mapping):
            return None
        if not isinstance(runtime_value, Mapping):
            return None
        surface = cast(Mapping[str, object], surface_value)
        runtime = cast(Mapping[str, object], runtime_value)
        if surface.get("provider") != "amp":
            return None
        if not fact.is_current():
            return None
        binary = runtime.get("binary")
        version = runtime.get("version")
        nested_binary = surface.get("amp_binary_path")
        nested_version = surface.get("amp_version")
        if (
            not isinstance(binary, str)
            or binary != _TWINRIDGE_AMP_BINARY
            or not isinstance(version, str)
            or not version.strip()
            or not isinstance(nested_binary, str)
            or not isinstance(nested_version, str)
            or not nested_version.strip()
            or (nested_binary, nested_version) != (binary, version)
        ):
            return None
        project_ref = surface.get("project_ref")
        if project_ref is not None and project_ref != expected_project_ref:
            return None
        profile_digest = surface.get("profile_digest")
        if profile_digest is not None and profile_digest != expected_profile_digest:
            return None
        if version != expected_version:
            return None
        orb_size = surface.get("orb_size")
        visibility = surface.get("visibility")
        fleet_enabled = surface.get("enabled")
        if (
            orb_size != _TWINRIDGE_AMP_ORB_SIZE
            or visibility != "private"
            or type(fleet_enabled) is not bool
            or fleet_enabled is not False
            or surface.get("no_archive_after_execute") is not True
        ):
            return None
        candidates.add(
            _AmpRuntimeConfig(
                binary=binary,
                version=version,
                orb_size=orb_size,
                visibility=visibility,
                fleet_enabled=fleet_enabled,
            )
        )
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def _amp_capabilities(value: object) -> ProviderCapabilities:
    raw = value if isinstance(value, Mapping) else {}
    supported = tuple(name for name in _AMP_CAPABILITY_NAMES if raw.get(name) is True)
    return ProviderCapabilities.from_supported(cast(Any, supported))


def _amp_child_roster(lane_reader: Callable[[], object]) -> tuple[ChildRosterEntry, ...]:
    context = _mapping(lane_reader(), "Amp lane context")
    values = context.get("child_roster", ())
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise TypeError("Amp lane child_roster must be a sequence")
    entries: list[ChildRosterEntry] = []
    for value in values:
        entries.append(ChildRosterEntry.model_validate(value, strict=True))
    return tuple(entries)


def _amp_usage_report(value: object, lane_reader: Callable[[], object]) -> UsageReport:
    """Translate Amp evidence without allowing it to replace Chitra's roster."""

    raw = dict(_mapping(value, "Amp usage report"))
    # ``usage_scope`` is provider evidence, not part of Chitra's typed report.
    # The launch policy remains the only owner of any ceiling.
    raw.pop("usage_scope", None)
    raw.pop("ceiling", None)
    expected = _amp_child_roster(lane_reader)
    observed = raw.get("child_roster", ())
    if not isinstance(observed, Sequence) or isinstance(observed, (str, bytes)):
        raise TypeError("Amp usage child_roster must be a sequence")
    if observed and not all(
        isinstance(value, Mapping) and "retained_state" in value and "material_result" in value
        for value in observed
    ):
        by_id = {entry.child_id: entry for entry in expected}
        observed_ids = {
            str(value.get("child_id", value.get("thread_id", value.get("id", ""))))
            for value in observed
            if isinstance(value, Mapping)
        }
        if observed_ids != set(by_id):
            raise ValueError("Amp usage roster does not match Chitra's observed child roster")
        raw["child_roster"] = [by_id[child_id].model_dump(mode="json") for child_id in sorted(by_id)]
    elif observed:
        canonical = tuple(ChildRosterEntry.model_validate(value, strict=True) for value in observed)
        if canonical != expected:
            raise ValueError("Amp usage changed Chitra's child roster evidence")
        raw["child_roster"] = [entry.model_dump(mode="json") for entry in canonical]
    elif expected and raw.get("complete") is True:
        raise ValueError("complete Amp usage omitted Chitra's observed child roster")
    return UsageReport.from_dict(raw)


def _unknown_close_result(operation: PendingProviderOperation, evidence: str) -> CloseArchiveResult:
    provider_generation = operation.provider_generation
    if provider_generation is None:  # MutationRequest normally prevents this.
        raise ValueError("close operation lacks provider generation")
    return CloseArchiveResult(
        operation_id=operation.operation_id,
        lane_id=operation.lane_id,
        provider_handle=operation.provider_handle,
        provider_instance_id=operation.provider_instance_id or "unknown-instance",
        provider_generation=provider_generation,
        idempotency_key=operation.idempotency_key,
        payload_digest=operation.payload_digest,
        state="unknown",
        provider_thread_ref=operation.provider_handle,
        provider_session_id=operation.provider_session_id,
        same_provider_thread=None,
        later_resume_supported=None,
        checkpoint_ref=None,
        quiescent=None,
        observed_at=_now(),
        evidence=evidence or "Amp close evidence is unavailable",
    )


def _amp_close_result(
    value: object,
    operation: PendingProviderOperation,
    *,
    expected_checkpoint_ref: str | None,
) -> CloseArchiveResult:
    raw = dict(_mapping(value, "Amp close result"))
    provider_generation = operation.provider_generation
    provider_instance_id = operation.provider_instance_id
    if provider_generation is None or provider_instance_id is None:
        return _unknown_close_result(operation, "close operation lacks a complete provider identity")
    for field in (
        "operation_id",
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
            return _unknown_close_result(operation, f"Amp close result {field} changed")
    state = raw.get("state")
    if state not in {"closed", "archived", "unknown", "failed"}:
        return _unknown_close_result(operation, "Amp close result state is unknown")
    provider_session_id = raw.get("provider_session_id")
    if state in {"closed", "archived"} and provider_session_id != operation.provider_session_id:
        return _unknown_close_result(operation, "Amp close result physical session changed")
    provider_thread_ref = raw.get("provider_thread_ref")
    if not isinstance(provider_thread_ref, str) or not provider_thread_ref:
        return _unknown_close_result(operation, "Amp close result has no provider thread evidence")
    checkpoint_ref = raw.get("checkpoint_ref")
    if checkpoint_ref is not None and not isinstance(checkpoint_ref, str):
        return _unknown_close_result(operation, "Amp close checkpoint evidence is malformed")
    same_provider_thread = raw.get("same_provider_thread")
    later_resume_supported = raw.get("later_resume_supported")
    quiescent = raw.get("quiescent")
    evidence = raw.get("evidence")
    observed_at = raw.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at.strip():
        observed_at = _now()
    if not isinstance(evidence, str) or not evidence.strip():
        evidence = "Amp close evidence is unavailable"
    if state in {"closed", "archived"} and (
        state != "archived"
        or expected_checkpoint_ref is None
        or checkpoint_ref != expected_checkpoint_ref
        or provider_thread_ref != operation.provider_handle
        or same_provider_thread is not True
        or quiescent is not True
    ):
        return _unknown_close_result(operation, "Amp archive did not prove Chitra's governed close conditions")
    return CloseArchiveResult(
        operation_id=operation.operation_id,
        lane_id=operation.lane_id,
        provider_handle=operation.provider_handle,
        provider_instance_id=provider_instance_id,
        provider_generation=provider_generation,
        idempotency_key=operation.idempotency_key,
        payload_digest=operation.payload_digest,
        state=cast(Any, state),
        provider_thread_ref=provider_thread_ref,
        provider_session_id=cast(str | None, provider_session_id),
        same_provider_thread=same_provider_thread if isinstance(same_provider_thread, bool) else None,
        later_resume_supported=later_resume_supported if isinstance(later_resume_supported, bool) else None,
        checkpoint_ref=checkpoint_ref,
        quiescent=quiescent if isinstance(quiescent, bool) else None,
        observed_at=observed_at,
        evidence=evidence,
    )


def _session_update_payload(item: Mapping[str, object]) -> object | None:
    """Return one lane snapshot nested in a normalized Amp event, if any."""

    for key in ("session_update", "update", "lane_update"):
        value = item.get(key)
        if value is not None:
            return value
    payload = item.get("payload")
    if isinstance(payload, Mapping):
        for key in ("session_update", "lane_update"):
            value = payload.get(key)
            if value is not None:
                return value
    return None


class _PackagedAmpProvider:
    """Typed Chitra facade over the single allowlisted Amp adapter.

    Amp supplies transport observations.  Chitra supplies durable operation,
    checkpoint, cursor, result, event, facts, and close authority.  In
    particular, ``checkpoint`` is exposed to satisfy the shared Provider
    protocol but never calls an Amp checkpoint primitive.
    """

    def __init__(
        self,
        adapter: object,
        *,
        result_sink: RecoverySink,
        cursor_sink: RecoverySink,
        lane_reader: Callable[[], object],
        update_batch_sink: Callable[[Sequence[object], str], object | None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._result_sink = result_sink
        self._cursor_sink = cursor_sink
        self._lane_reader = lane_reader
        self._update_batch_sink = update_batch_sink

    @property
    def provider_name(self) -> ProviderName:
        return ProviderName.AMP

    @property
    def capabilities(self) -> ProviderCapabilities:
        return _amp_capabilities(getattr(self._adapter, "capabilities", {}))

    def _payload(self, request: object, operation: PendingProviderOperation) -> dict[str, object]:
        payload: dict[str, object] = {"operation": _operation_dict(operation)}
        if isinstance(request, SendRequest):
            payload["text"] = request.text
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
            context = _mapping(self._lane_reader(), "Amp lane context")
            current_turn_id = context.get("current_turn_id")
            if isinstance(current_turn_id, str) and current_turn_id:
                payload["current_turn_id"] = current_turn_id
        elif isinstance(request, CloseRequest):
            payload["archive"] = request.archive
        return payload

    def _call(
        self,
        method: str,
        request: object,
        operation: PendingProviderOperation,
    ) -> ProviderOperationResult:
        raw = getattr(self._adapter, method)(self._payload(request, operation))
        result = _provider_result(raw, operation, provider_label="Amp")
        self._result_sink(raw)
        return result

    def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
        # The exact pending envelope crosses unchanged.  AmpAdapter performs
        # the exact-tag search and holds on zero-result lost creates.
        return self._call("create_or_resume", request, request.operation)

    def status(self) -> ProviderStatus:
        adapter = cast(Any, self._adapter)
        raw = _mapping(adapter.status(None), "Amp status")
        state = _provider_state(raw.get("state", "unknown"))
        generation = raw.get("generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int):
            generation = 0
        provider_session_id = raw.get("provider_session_id")
        current_turn_id = raw.get("current_turn_id")
        last_event_id = raw.get("last_event_id")
        reason = raw.get("reason")
        context_available = raw.get("context_available")
        return ProviderStatus(
            provider=ProviderName.AMP,
            state=state,
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
        raw = _mapping(adapter.read_updates(cursor), "Amp update batch")
        values = raw.get("updates", ())
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise TypeError("Amp updates must be a sequence")
        updates: list[ProviderUpdate] = []
        session_updates: list[object] = []
        for value in values:
            item = _mapping(value, "Amp update")
            session_update = _session_update_payload(item)
            required = tuple(item.get(name) for name in ("operation_id", "event_id", "cursor"))
            if not all(isinstance(field, str) and field for field in required):
                if session_update is not None:
                    raise ValueError("Amp session update envelope is malformed")
                continue
            generation = item.get("provider_generation", 0)
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
                if session_update is not None:
                    raise ValueError("Amp session update envelope is malformed")
                continue
            identity = tuple(
                item.get(name)
                for name in (
                    "provider_instance_id",
                    "lane_id",
                    "provider_handle",
                    "idempotency_key",
                    "payload_digest",
                )
            )
            if not all(isinstance(field, str) and field for field in identity):
                if session_update is not None:
                    raise ValueError("Amp session update envelope is malformed")
                continue
            payload = item.get("payload", {})
            if not isinstance(payload, Mapping):
                payload = {}
            if session_update is not None:
                session_updates.append(session_update)
            observed_at = item.get("observed_at")
            if not isinstance(observed_at, str) or not observed_at:
                observed_at = _now()
            provider_session_id = item.get("provider_session_id")
            updates.append(
                ProviderUpdate(
                    event_id=cast(str, item["event_id"]),
                    cursor=cast(str, item["cursor"]),
                    kind=cast(Any, item.get("kind", "unknown")),
                    provider_session_id=provider_session_id if isinstance(provider_session_id, str) else None,
                    observed_at=observed_at,
                    operation_id=cast(str, item["operation_id"]),
                    lane_id=cast(str, item["lane_id"]),
                    idempotency_key=cast(str, item["idempotency_key"]),
                    payload_digest=cast(str, item["payload_digest"]),
                    provider_instance_id=cast(str, item["provider_instance_id"]),
                    provider_generation=generation,
                    provider_handle=cast(str, item["provider_handle"]),
                    payload=cast(Mapping[str, object], payload),
                )
            )
        next_cursor = raw.get("next_cursor", raw.get("cursor"))
        next_cursor_text = next_cursor if isinstance(next_cursor, str) else cursor or ""
        if self._update_batch_sink is not None:
            # Chitra commits every lane snapshot and the cursor as one state
            # transition.  The adapter callback is intentionally not used on
            # this production route: a malformed later snapshot must not
            # leave an earlier snapshot durable with the old cursor.
            self._update_batch_sink(tuple(session_updates), next_cursor_text)
        else:
            self._cursor_sink(next_cursor_text)
        reason = raw.get("reason")
        return ReadUpdatesResult(
            requested_cursor=cursor,
            next_cursor=next_cursor_text,
            updates=tuple(updates),
            provider_available=raw.get("provider_available") is not False,
            complete=raw.get("complete") is not False,
            reason=reason if isinstance(reason, str) else "",
        )

    def checkpoint(self, request: CheckpointRequest) -> ProviderOperationResult:
        # CheckpointRequest is recorded and verified by Chitra.  Amp CLI has
        # no provider checkpoint primitive, so this method cannot claim one.
        return _unknown_provider_result(
            request.operation,
            "Chitra owns the checkpoint receipt; Amp has no checkpoint primitive",
        )

    def usage(self) -> UsageReport:
        adapter = cast(Any, self._adapter)
        raw = adapter.usage(None)
        return _amp_usage_report(raw, self._lane_reader)

    def cancel_current_turn(self, request: CancelCurrentTurnRequest) -> ProviderOperationResult:
        return self._call("cancel_current_turn", request, request.operation)

    def close(self, request: CloseRequest) -> CloseArchiveResult:
        if not request.archive:
            return _unknown_close_result(request.operation, "Amp close is archive-only")
        try:
            context = _mapping(self._lane_reader(), "Amp lane context")
            expected_checkpoint_ref = context.get("checkpoint_ref")
            if not isinstance(expected_checkpoint_ref, str) or not expected_checkpoint_ref:
                return _unknown_close_result(request.operation, "Chitra has no durable checkpoint reference")
            if context.get("quiescent") is not True:
                return _unknown_close_result(request.operation, "Chitra has not observed lane quiescence")
            # AmpAdapter.close is the canonical composite operation.  It
            # validates Amp quiescence/usage and returns Chitra's checkpoint
            # reference plus archive evidence from the transport.
            adapter = cast(Any, self._adapter)
            raw = adapter.close(self._payload(request, request.operation))
            return _amp_close_result(
                raw,
                request.operation,
                expected_checkpoint_ref=expected_checkpoint_ref,
            )
        except Exception as exc:  # noqa: BLE001 - provider outage is an unknown result
            return _unknown_close_result(request.operation, f"Amp close evidence unavailable: {exc}")


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
    provider = _PackagedTophandProvider(adapter, state_root=state_root, result_sink=result_sink)
    return provider if isinstance(provider, Provider) else None


def _canonical_amp_factory(
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
    """Build the one static Amp route after Chitra's launch-policy gate."""

    del pending_sink, event_sink, checkpoint_verifier, cancel_verifier
    if identity.kind != "amp":
        return None
    if (
        _packaged_amp_adapter is None
        or _packaged_amp_profile is None
        or _packaged_amp_transport is None
        or identity.instance_id is None
        or identity.generation is None
    ):
        return None
    if launch_policy_problem(record) is not None:
        return None
    policy = record.launch_policy
    if policy is None:  # The policy check above is intentionally repeated for type narrowing.
        return None
    runtime = _amp_runtime_config(
        operating_facts,
        expected_project_ref=policy.project_ref,
        expected_profile_digest=policy.profile_digest,
        expected_version=identity.provider_version,
    )
    if runtime is None:
        return None
    amp_binary = runtime.binary
    amp_version = runtime.version

    store = JoinedLaneStore(state_root)
    initial_facts = tuple(operating_facts)

    def lane_reader() -> dict[str, object]:
        current = store.load(lane.identifier) or record
        current_facts = tuple(facts_reader(current))
        update = current.current_update
        roster = () if update is None else update.child_roster
        known_handles = [current.provider.handle]
        if current.last_close_result is not None:
            known_handles.append(current.last_close_result.provider_thread_ref)
        context: dict[str, object] = {
            "lane_id": current.lane_id,
            "goal_id": current.goal_id,
            "goal_version": current.goal_version,
            "session_ref": current.session_ref,
            "provider_handle": current.provider.handle,
            "provider_instance_id": current.provider.instance_id,
            "provider_generation": current.provider.generation,
            "parent_thread_ref": current.provider.parent_thread_ref,
            "project_ref": current.provider.project_ref,
            "profile_digest": current.provider.profile_digest,
            "provider_version": current.provider.provider_version,
            "known_provider_handles": tuple(dict.fromkeys(known_handles)),
            "child_roster": tuple(entry.model_dump(mode="json") for entry in roster),
            "child_ids": tuple(entry.child_id for entry in roster),
            "checkpoint_ref": current.checkpoint_reference,
            # A governed close is itself the pending operation.  It does not
            # represent an active provider turn, so retain the Chitra
            # checkpoint/quiescence proof while that exact close envelope is
            # in flight.  Other pending mutations remain non-quiescent.
            "quiescent": current.checkpoint_reference is not None
            and (current.pending_operation is None or current.pending_operation.kind == "close"),
            "observed_at": update.observed_at if update is not None else _now(),
            "operating_facts": tuple(fact.model_dump(mode="json") for fact in current_facts),
        }
        if not current_facts and initial_facts:
            context["operating_facts"] = tuple(fact.model_dump(mode="json") for fact in initial_facts)
        return context

    try:
        # Fleet supplies the fixed ORB size and private visibility.  Fleet's
        # declaration remains disabled; the explicit Chitra launch-policy gate
        # is the only point that enables this already-reviewed surface.
        profile = _packaged_amp_profile(
            project_ref=policy.project_ref,
            orb_size=runtime.orb_size,
            profile_digest=policy.profile_digest,
            visibility=runtime.visibility,
        )
        transport = _packaged_amp_transport(
            profile,
            amp_binary=amp_binary,
            amp_version=amp_version,
        )
        adapter = _packaged_amp_adapter(
            transport=transport,
            project_ref=policy.project_ref,
            visibility=runtime.visibility,
            enabled=not runtime.fleet_enabled,
            profile_digest=policy.profile_digest,
            anchor_thread_id=identity.parent_thread_ref,
            lane_reader=lane_reader,
            amp_version=amp_version,
        )
    except Exception as exc:  # noqa: BLE001 - unavailable provider is canonical unknown
        logger.warning("packaged_amp_unavailable", lane_id=record.lane_id, error=str(exc))
        return None
    if adapter is None:
        return None
    provider = _PackagedAmpProvider(
        adapter,
        result_sink=result_sink,
        cursor_sink=cursor_sink,
        lane_reader=lane_reader,
        update_batch_sink=_canonical_update_batch_sink(lane),
    )
    return provider if isinstance(provider, Provider) else None


def _canonical_recovery_bindings(
    lane: LaneSpec,
) -> tuple[RecoverySink, RecoverySink, RecoverySink, RecoverySink, RecoveryVerifier, RecoveryVerifier]:
    """Return Chitra-owned evidence callbacks for the packaged adapter."""

    results_path = lane.state_dir / "provider-results" / f"{lane.identifier}.jsonl"
    journal = EventJournal(lane.state_dir, lane.identifier)
    store = JoinedLaneStore(lane.state_dir)

    def pending_sink(value: object) -> None:
        # RecoveryEngine persists the pending envelope before invoking the
        # provider.  Validate the callback payload but never let an adapter
        # perform a second joined-lane state transition.
        _operation_dict(value)

    def cursor_sink(value: object) -> None:
        if value is not None and not isinstance(value, str):
            raise TypeError("provider cursor must be text or null")
        cursor = "" if value is None else value

        def apply(current: JoinedLaneRecord) -> JoinedLaneRecord:
            if current.update_cursor == cursor:
                return current
            return current.model_copy(update={"update_cursor": cursor})

        store.update(lane.identifier, apply)

    def result_sink(value: object) -> None:
        raw = dict(_mapping(value, "provider result"))
        current = store.load(lane.identifier)
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
        current = store.load(lane.identifier)
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
            goal_version=current.goal_version,
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
            _validate_lane_update_identity(current, update)
            if current.current_update is not None:
                validate_update(current.current_update, update)
            return current.model_copy(update={"current_update": update})

        store.update(lane.identifier, apply)

    return update_sink


def _validate_lane_update_identity(current: JoinedLaneRecord, update: LaneUpdate) -> None:
    expected = (current.lane_id, current.goal_id, current.session_ref, current.goal_version)
    observed = (update.lane_id, update.goal_id, update.session_ref, update.goal_version)
    if observed != expected:
        raise ValueError("provider session update identity changed")


def _canonical_update_batch_sink(lane: LaneSpec) -> Callable[[Sequence[object], str], None]:
    """Atomically persist a complete Amp snapshot batch and its cursor."""

    store = JoinedLaneStore(lane.state_dir)

    def update_batch_sink(values: Sequence[object], cursor: str) -> None:
        if not isinstance(cursor, str):
            raise TypeError("provider cursor must be text")
        # Parse every snapshot before opening the state transition.  A later
        # malformed snapshot therefore cannot leave an earlier one durable.
        updates = tuple(LaneUpdate.from_dict(value) for value in values)

        def apply(current: JoinedLaneRecord) -> JoinedLaneRecord:
            previous = current.current_update
            for update in updates:
                _validate_lane_update_identity(current, update)
                if previous is not None:
                    validate_update(previous, update)
                previous = update
            changes: dict[str, object] = {"update_cursor": cursor}
            if updates:
                changes["current_update"] = updates[-1]
            return current.model_copy(update=changes)

        store.update(lane.identifier, apply)

    return update_batch_sink


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
        amp_factory = _canonical_amp_factory

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

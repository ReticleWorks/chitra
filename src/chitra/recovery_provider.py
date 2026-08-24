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

import hmac
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import structlog

from ._fsio import locked_json_store
from .amp_capability import CapabilitySignatureVerifier, verify_amp_capability_receipt
from .detect.rescue import (
    RecoveryCheckpointBinding,
    find_recovery_checkpoint_receipt,
    verify_checkpoint_receipt_signature,
)
from .joined_lane import JoinedLaneStore
from .journal import EventJournal
from .journal.models import CanonicalEvent
from .lane_config import LaneSpec
from .operating_facts import (
    OperatingFactsBinding,
    OperatingFactsSources,
    bind_current_operating_facts,
    read_operating_facts,
)
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
    ReopenReceipt,
    canonical_digest,
    validate_update,
)
from .tophand_wire import request_digest
from .usage_policy import launch_policy_problem

try:
    # Fleet packages this exact module under /opt/polyphony/deploy-main.  The
    # import is deliberately static and allowlisted; a missing package keeps
    # recovery unavailable instead of selecting an arbitrary adapter.
    from tools.support.chitra_adapter.tophand_adapter import (
        TophandCommandTransport as _imported_tophand_transport,
    )
    from tools.support.chitra_adapter.tophand_adapter import (  # type: ignore[import-untyped]
        build_tophand_provider as _imported_tophand_builder,
    )
except ImportError:  # pragma: no cover - exercised by source-only installs
    _packaged_tophand_builder: Callable[..., object] | None = None
    _packaged_tophand_transport: type[Any] | None = None
else:
    _packaged_tophand_builder = cast(Callable[..., object], _imported_tophand_builder)
    _packaged_tophand_transport = cast(type[Any], _imported_tophand_transport)

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
        operating_facts_binding: OperatingFactsBinding | None,
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


LANE_REGISTRATION_SCHEMA = "chitra.lane-registration.v1"
LANE_CONTROL_SCHEMA = "chitra.lane-control.v1"


def _registration_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"registration {field} is missing")
    return value.strip()


def _registration_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"registration {field} is invalid")
    return value


def _registration_timestamp(value: object, field: str) -> str:
    text = _registration_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"registration {field} is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"registration {field} has no timezone")
    return parsed.astimezone(UTC).isoformat()


def _current_fact(facts: Sequence[OperatingFact], name: str, *, now: datetime) -> OperatingFact:
    candidates = [fact for fact in facts if fact.name == name and fact.is_current(now=now)]
    if len(candidates) != 1:
        raise ValueError(f"current {name} fact is unavailable or ambiguous")
    return candidates[0]


def _tophand_registration_facts(
    lane: LaneSpec, facts: Sequence[OperatingFact], *, now: datetime
) -> tuple[ProviderCapabilities, str, str, str | None, str]:
    """Resolve only current routing and capability facts.

    Physical provider identity comes from the target-owned registration below.
    This helper must never provide a handle, instance, generation, or PID.
    """

    capability_candidates = [
        fact
        for fact in facts
        if fact.name in {"fleet.provider-capabilities", "fleet.provider-capabilities.tophand"}
        and fact.is_current(now=now)
    ]
    if len(capability_candidates) != 1 or not isinstance(capability_candidates[0].value, Mapping):
        raise ValueError("current provider-capabilities fact is unavailable")
    capability_value = capability_candidates[0].value
    authority = capability_value.get("lane_registration_authority")
    if not isinstance(authority, Mapping) or {
        authority.get("schema"), authority.get("source"), authority.get("mode")
    } != {LANE_REGISTRATION_SCHEMA, "target-owned-launcher", 0o600}:
        raise ValueError("target-owned lane registration authority is not declared")
    top = capability_value.get("tophand")
    if isinstance(top, Mapping):
        top_value = top
    elif capability_candidates[0].name == "fleet.provider-capabilities.tophand":
        top_value = capability_value
    else:
        raise ValueError("Tophand capability fact is unavailable")
    raw_capabilities = top_value.get("capabilities")
    try:
        if isinstance(raw_capabilities, Mapping):
            capabilities = ProviderCapabilities.model_validate(dict(raw_capabilities), strict=True)
        elif isinstance(raw_capabilities, Sequence) and not isinstance(raw_capabilities, (str, bytes)):
            capabilities = ProviderCapabilities.from_supported(raw_capabilities)
        else:
            raise ValueError("capabilities must be an object or list")
    except (TypeError, ValueError) as exc:
        raise ValueError("Tophand capability fact is malformed") from exc
    if not capabilities.create_or_resume:
        raise ValueError("Tophand create_or_resume capability is not current")
    placement = _current_fact(facts, "fleet.placement", now=now)
    routing = _current_fact(facts, "fleet.routing", now=now)
    placement_value = placement.value if isinstance(placement.value, Mapping) else {}
    routing_value = routing.value if isinstance(routing.value, Mapping) else {}
    dispatch_target = routing_value.get("dispatch_target")
    if not isinstance(dispatch_target, Mapping):
        raise ValueError("current dispatch target fact is unavailable")
    host = dispatch_target.get("host")
    account = dispatch_target.get("user")
    if not isinstance(host, str) or not host.strip() or not isinstance(account, str) or not account.strip():
        raise ValueError("current dispatch target fact is incomplete")
    placement_host = placement_value.get("host")
    placement_account = placement_value.get("account")
    if placement_host != host or placement_account != account:
        raise ValueError("placement and routing target facts disagree")
    if lane.target_host is not None and lane.target_host != host:
        raise ValueError("lane target host does not match current facts")
    if lane.target_account is not None and lane.target_account != account:
        raise ValueError("lane target account does not match current facts")
    fact_revision = capability_candidates[0].revision
    revision = str(fact_revision) if isinstance(fact_revision, (str, int)) else None
    provider_version = top_value.get("provider_version")
    return (
        capabilities,
        host,
        account,
        revision,
        provider_version.strip() if isinstance(provider_version, str) else "",
    )


def _verified_tophand_registration(
    raw: object,
    *,
    lane: LaneSpec,
    goal: Any,
    facts: Sequence[OperatingFact],
    now: datetime,
) -> ProviderIdentity:
    """Turn one Fleet readback into identity, with no receipt fallback."""

    if (
        not isinstance(raw, Mapping)
        or raw.get("schema") != LANE_CONTROL_SCHEMA
        or raw.get("action") != "registration"
        or raw.get("status") != "verified"
    ):
        raise ValueError("Fleet did not return a verified lane registration")
    registration = raw.get("registration")
    if not isinstance(registration, Mapping):
        raise ValueError("Fleet registration response has no full registration")
    capabilities, target_host, target_account, fact_revision, provider_version = _tophand_registration_facts(
        lane, facts, now=now
    )
    required = (
        "provider",
        "lane_id",
        "provider_handle",
        "provider_session_id",
        "provider_instance_id",
        "logical_session",
        "session_ref",
        "facts_revision",
        "operation_id",
        "idempotency_key",
        "process_start_token",
        "registration_sha256",
    )
    values = {field: _registration_text(registration.get(field), field) for field in required}
    if (
        values["provider"] != "tophand"
        or values["lane_id"] != lane.identifier
        or values["logical_session"] != lane.identifier
        or values["session_ref"] != goal.session_ref
        or values["provider_session_id"] != goal.session_ref
        or values["process_start_token"].startswith("pid:")
        or registration.get("lifecycle") != "running"
        or fact_revision is not None and values["facts_revision"] != fact_revision
    ):
        raise ValueError("lane registration identity does not match current facts or enrolled goal")
    generation = _registration_positive_int(
        registration.get("provider_generation"), "provider_generation"
    )
    if not re.fullmatch(r"[0-9a-f]{64}", values["registration_sha256"]):
        raise ValueError("lane registration digest is invalid")
    target = registration.get("target")
    if (
        not isinstance(target, Mapping)
        or target.get("host") != target_host
        or target.get("account") != target_account
    ):
        raise ValueError("lane registration target does not match current facts")
    process = registration.get("process")
    if not isinstance(process, Mapping):
        raise ValueError("lane registration has no process observation")
    pid = _registration_positive_int(process.get("tmux_pane_pid"), "process.tmux_pane_pid")
    boot_id = _registration_text(process.get("boot_id"), "process.boot_id")
    start_ticks = _registration_positive_int(process.get("start_ticks"), "process.start_ticks")
    process_token = values["process_start_token"]
    if process_token != f"{boot_id}:{start_ticks}":
        raise ValueError("lane registration process-start token is not bound to raw process fields")
    observation = registration.get("observation")
    if not isinstance(observation, Mapping):
        raise ValueError("lane registration has no full process observation")
    if (
        observation.get("provider_pid") != pid
        or observation.get("owner_pid") != pid
        or observation.get("provider_handle") != values["provider_handle"]
        or observation.get("provider_session_id") != values["provider_session_id"]
        or observation.get("provider_instance_id") != values["provider_instance_id"]
        or observation.get("provider_generation") != generation
        or observation.get("process_start_token") != process_token
        or observation.get("boot_id") != boot_id
        or observation.get("start_ticks") != start_ticks
    ):
        raise ValueError("lane registration process observation changed")
    observed_at = _registration_timestamp(observation.get("observed_at"), "observation.observed_at")
    if datetime.fromisoformat(observed_at) > now:
        raise ValueError("lane registration observation is from the future")
    goal_id = registration.get("goal_id")
    if goal_id is not None and goal_id != goal.goal_id:
        raise ValueError("lane registration goal identity changed")
    goal_version = registration.get("goal_version")
    if goal_version is not None and goal_version != goal.goal_version:
        raise ValueError("lane registration goal version changed")
    return ProviderIdentity(
        kind="tophand",
        handle=values["provider_handle"],
        provider_session_id=values["provider_session_id"],
        instance_id=values["provider_instance_id"],
        generation=generation,
        process_start_token=process_token,
        observed_process={
            "provider_pid": pid,
            "owner_pid": pid,
            "boot_id": boot_id,
            "start_ticks": start_ticks,
            "process_start_token": process_token,
            "observed_at": observed_at,
        },
        registration_digest=f"sha256:{values['registration_sha256']}",
        registration_observed_at=observed_at,
        target_host=target_host,
        target_account=target_account,
        capabilities=capabilities,
        provider_version=provider_version,
    )


def build_tophand_registration_identity_resolver(
    lane: LaneSpec,
    *,
    transport_factory: type[Any] | None = None,
) -> Callable[[Any, Sequence[OperatingFact]], ProviderIdentity | None]:
    """Build the production bootstrap resolver from target-owned registration."""

    transport_type = transport_factory or _packaged_tophand_transport

    def resolve(goal: Any, facts: Sequence[OperatingFact]) -> ProviderIdentity | None:
        if transport_type is None:
            return None
        try:
            transport = transport_type.from_environment(
                lane.identifier,
                session_ref=goal.session_ref,
                goal_id=goal.goal_id,
            )
            reader = getattr(transport, "registration", None)
            if not callable(reader):
                return None
            return _verified_tophand_registration(
                reader(), lane=lane, goal=goal, facts=facts, now=datetime.now(UTC)
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.info(
                "tophand_registration_unavailable", lane_id=lane.identifier, reason=str(exc)
            )
            return None

    return resolve


def _facts_binding_current(binding: OperatingFactsBinding | None) -> bool:
    """Keep provider I/O behind the immutable facts deadline."""

    if binding is None:
        return True
    try:
        deadline = datetime.fromisoformat(binding.deadline.replace("Z", "+00:00"))
    except ValueError:
        return False
    return deadline >= datetime.now(UTC)


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
    """Serialize the generic Chitra operation used by non-Tophand adapters."""

    if isinstance(operation, PendingProviderOperation):
        return cast(dict[str, object], operation.model_dump(mode="json"))
    if isinstance(operation, Mapping):
        return {str(key): value for key, value in operation.items()}
    raise TypeError("provider operation must be a canonical mapping")


def _tophand_operation_dict(operation: object) -> dict[str, object]:
    if isinstance(operation, PendingProviderOperation):
        # Keep Chitra's durable payload on this boundary. The Adapter owns
        # projection to the exact Fleet wire and the attempted-state fence.
        # It needs the payload to recover an attempted resume after a fresh
        # Chitra process starts.
        return {
            "operation_id": operation.operation_id,
            "kind": operation.kind,
            "lane_id": operation.lane_id,
            "provider_handle": operation.provider_handle,
            "provider_session_id": operation.provider_session_id,
            "process_start_token": operation.process_start_token,
            "idempotency_key": operation.idempotency_key,
            "payload_digest": operation.payload_digest,
            "payload": operation.payload,
            "provider_instance_id": operation.provider_instance_id,
            "provider_generation": operation.provider_generation,
            "created_at": operation.created_at,
            "attempt": operation.attempt,
        }
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


def _authenticated_reopen_process_token(
    operation: PendingProviderOperation,
    raw: Mapping[str, object],
    receipt: ReopenReceipt | None,
) -> str | None:
    """Authorize the one process-token change carried by a resume receipt."""

    if (
        raw.get("status") != "consumed"
        or operation.kind != "create_or_resume"
        or operation.process_start_token is not None
        or receipt is None
    ):
        return None
    try:
        payload = json.loads(operation.payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ) != operation.payload:
        return None
    if set(payload) != {
        "session_ref",
        "provider_session_id",
        "context_ref",
        "goal_id",
        "goal_version",
        "resume_after_close",
        "close_operation_id",
        "owner_process",
        "resume_token",
    } or payload.get("resume_after_close") is not True:
        return None
    token = payload.get("resume_token")
    raw_token = raw.get("process_start_token")
    if not isinstance(token, str) or not token or not isinstance(raw_token, str) or not raw_token:
        return None
    unsigned = {
        key: value
        for key, value in receipt.to_dict().items()
        if key not in {"receipt_hmac", "signature"}
    }
    expected_hmac = hmac.new(
        token.encode(),
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
        "sha256",
    ).hexdigest()
    if (
        request_digest("create_or_resume", payload) != operation.payload_digest
        or receipt.receipt_hmac is None
        or not hmac.compare_digest(receipt.receipt_hmac, expected_hmac)
        or receipt.operation_id != operation.operation_id
        or receipt.lane_id != operation.lane_id
        or receipt.provider_handle != operation.provider_handle
        or receipt.provider_session_id != operation.provider_session_id
        or receipt.provider_instance_id != operation.provider_instance_id
        or receipt.provider_generation != operation.provider_generation
        or receipt.session_ref != payload.get("session_ref")
        or receipt.goal_id != payload.get("goal_id")
        or receipt.goal_version != payload.get("goal_version")
        or receipt.checkpoint_ref != payload.get("context_ref")
        or receipt.close_operation_id != payload.get("close_operation_id")
        or receipt.prior_owner_process.model_dump(mode="json") != payload.get("owner_process")
        or receipt.auth_token != token
        or receipt.created_new_lane is not False
        or receipt.created_new_session is not False
        or receipt.owner_process == receipt.prior_owner_process
        or receipt.owner_process.start_token != raw_token
    ):
        return None
    return raw_token


def _provider_result(
    value: object,
    operation: PendingProviderOperation,
    *,
    provider_label: str = "provider",
) -> ProviderOperationResult:
    """Translate the packaged adapter result into Chitra's typed result."""

    raw = _mapping(value, f"{provider_label} provider result")
    status = raw.get("status")
    if status not in {"accepted", "consumed", "rejected", "unknown", "lost-response"}:
        status = "unknown"
    reopen_receipt = (
        ReopenReceipt.from_dict(raw["reopen_receipt"])
        if raw.get("reopen_receipt") is not None
        else None
    )
    reopened_process_token = _authenticated_reopen_process_token(
        operation, raw, reopen_receipt
    )

    # A provider result that claims a disposition must carry the physical
    # identity and observation time that the provider actually returned.
    # Copying these fields from Chitra's pending envelope would turn an
    # unknown or replaced provider into a false success receipt.
    observed_at = raw.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at.strip():
        raise ValueError(f"{provider_label} provider result observed_at is missing")
    raw_instance_id = raw.get("provider_instance_id")
    raw_generation = raw.get("provider_generation")
    if status not in {"unknown", "lost-response"}:
        if not isinstance(raw_instance_id, str) or not raw_instance_id.strip():
            raise ValueError(f"{provider_label} provider result provider_instance_id is missing")
        if operation.provider_instance_id is None or raw_instance_id != operation.provider_instance_id:
            raise ValueError(f"{provider_label} provider result provider_instance_id changed or is missing")
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int) or raw_generation < 1:
            raise ValueError(f"{provider_label} provider result provider_generation is missing or invalid")
        if operation.provider_generation is None or raw_generation != operation.provider_generation:
            raise ValueError(f"{provider_label} provider result provider_generation changed or is missing")
        if operation.process_start_token is not None:
            raw_process_token = raw.get("process_start_token")
            if not isinstance(raw_process_token, str) or raw_process_token != operation.process_start_token:
                raise ValueError(f"{provider_label} provider result process_start_token changed or is missing")

    for field in (
        "operation_id",
        "kind",
        "lane_id",
        "provider_handle",
        "idempotency_key",
        "payload_digest",
        "provider_instance_id",
        "provider_generation",
        "process_start_token",
    ):
        observed = raw.get(field)
        expected = getattr(operation, field)
        if field == "process_start_token" and observed == reopened_process_token:
            continue
        if observed is not None and observed != expected:
            raise ValueError(f"{provider_label} provider result {field} changed")
    raw_provider_session_id = raw.get("provider_session_id")
    if raw_provider_session_id is not None and (
        not isinstance(raw_provider_session_id, str) or not raw_provider_session_id.strip()
    ):
        raise ValueError(f"{provider_label} provider result provider_session_id is malformed")
    if operation.provider_session_id is not None:
        if raw_provider_session_id is not None and raw_provider_session_id != operation.provider_session_id:
            raise ValueError(f"{provider_label} provider result provider_session_id changed")
        if raw_provider_session_id is None and status not in {"unknown", "lost-response"}:
            raise ValueError(f"{provider_label} provider result provider_session_id is missing")
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
    evidence = raw.get("evidence")
    ownership = dict(raw["ownership"]) if isinstance(raw.get("ownership"), Mapping) else None
    observed_process = raw.get("observed_process")
    if observed_process is not None and not isinstance(observed_process, Mapping):
        raise ValueError(f"{provider_label} provider result observed_process is invalid")
    if ownership is not None:
        for field, expected in (
            ("lane_id", operation.lane_id),
            ("provider_session_id", operation.provider_session_id),
            ("provider_handle", operation.provider_handle),
            ("provider_instance_id", operation.provider_instance_id),
            ("provider_generation", operation.provider_generation),
            ("process_start_token", operation.process_start_token),
        ):
            if field == "process_start_token" and ownership.get(field) == reopened_process_token:
                continue
            if field in ownership and ownership[field] != expected:
                raise ValueError(f"{provider_label} provider ownership {field} changed")
        nested_process = ownership.get("observed_process")
        if not isinstance(nested_process, Mapping):
            raise ValueError(f"{provider_label} provider ownership lacks observed_process")
        if observed_process is not None and dict(nested_process) != dict(observed_process):
            raise ValueError(f"{provider_label} provider result observed_process changed")
        observed_process = nested_process
    provider_session_id = raw.get("provider_session_id")
    if provider_session_id is not None and provider_session_id != operation.provider_session_id:
        raise ValueError(f"{provider_label} provider result provider_session_id changed")
    raw_provider_pid = raw.get("provider_pid")
    raw_owner_pid = raw.get("owner_pid")
    return ProviderOperationResult(
        operation_id=operation.operation_id,
        kind=operation.kind,
        lane_id=operation.lane_id,
        provider_handle=operation.provider_handle,
        idempotency_key=operation.idempotency_key,
        payload_digest=operation.payload_digest,
        provider_session_id=raw_provider_session_id if isinstance(raw_provider_session_id, str) else None,
        provider_instance_id=raw_instance_id if isinstance(raw_instance_id, str) else None,
        provider_generation=raw_generation if isinstance(raw_generation, int) and not isinstance(raw_generation, bool) else None,
        process_start_token=(
            raw.get("process_start_token") if isinstance(raw.get("process_start_token"), str) else None
        ),
        provider_pid=(
            raw_provider_pid
            if isinstance(raw_provider_pid, int) and not isinstance(raw_provider_pid, bool)
            else None
        ),
        owner_pid=(
            raw_owner_pid
            if isinstance(raw_owner_pid, int) and not isinstance(raw_owner_pid, bool)
            else None
        ),
        observed_process=dict(observed_process) if isinstance(observed_process, Mapping) else None,
        ownership=ownership,
        status=cast(Any, status),
        accepted=accepted,
        consumed=consumed,
        observed_at=observed_at,
        evidence=evidence if isinstance(evidence, str) else "",
        reopen_receipt=reopen_receipt,
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
        provider_session_id=operation.provider_session_id,
        idempotency_key=operation.idempotency_key,
        payload_digest=operation.payload_digest,
        provider_instance_id=operation.provider_instance_id,
        provider_generation=operation.provider_generation,
        process_start_token=operation.process_start_token,
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
        state_root: Path | None = None,
        result_sink: RecoverySink,
        process_start_token: str | None = None,
        reconcile_before_call: bool = False,
        operating_facts_binding: OperatingFactsBinding | None = None,
    ) -> None:
        # Chitra verifies the signed receipt before it crosses the provider
        # boundary. Tophand still never reads this filesystem or its key.
        self._state_root = state_root
        self._adapter = adapter
        self._result_sink = result_sink
        self._process_start_token = process_start_token
        self._reconcile_before_call = reconcile_before_call
        self._operating_facts_binding = operating_facts_binding

    def _require_current_facts(self) -> None:
        if not _facts_binding_current(self._operating_facts_binding):
            raise RuntimeError("Fleet operating-facts binding expired; recovery will retry")

    def _verify_close_checkpoint(self, request: CloseRequest) -> None:
        receipt = request.checkpoint_receipt
        if self._state_root is None or not isinstance(receipt, Mapping):
            raise ValueError("Chitra close requires a local signed checkpoint verifier")
        if request.checkpoint_verifier != "chitra.detect.rescue.verify_checkpoint_receipt_signature":
            raise ValueError("close checkpoint verifier is not canonical")
        if request.checkpoint_receipt_sha256 != canonical_digest(receipt):
            raise ValueError("close checkpoint digest does not match the supplied receipt")
        if not verify_checkpoint_receipt_signature(dict(receipt), state_root=self._state_root):
            raise ValueError("close checkpoint HMAC is invalid")
        payload = json.loads(request.operation.payload)
        if not isinstance(payload, Mapping):
            raise ValueError("close payload must be an object")
        if any(
            receipt.get(receipt_field) != payload.get(payload_field)
            for receipt_field, payload_field in (
                ("checkpoint_ref", "checkpoint_ref"),
                ("lane", "lane_id"),
                ("goal_id", "goal_id"),
                ("goal_version", "goal_version"),
                ("session_ref", "session_ref"),
            )
        ):
            raise ValueError("close checkpoint identity changed")
        binding = receipt.get("provider_binding")
        if not isinstance(binding, Mapping) or any(
            binding.get(field) != expected
            for field, expected in (
                ("kind", "tophand"),
                ("handle", request.operation.provider_handle),
                ("provider_session_id", request.operation.provider_session_id),
                ("instance_id", request.operation.provider_instance_id),
                ("generation", request.operation.provider_generation),
            )
        ):
            raise ValueError("close checkpoint provider binding changed")

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
        if raw.get("resume_after_close") is True and all(
            raw.get(name) is True for name in ("create_or_resume", "close")
        ):
            supported = (*supported, "resume_after_close")
        return ProviderCapabilities.from_supported(cast(Any, supported))

    def _call(
        self,
        method: str,
        request: object,
        operation: PendingProviderOperation,
    ) -> ProviderOperationResult:
        self._require_current_facts()
        if self._reconcile_before_call and operation.attempted:
            reconcile = getattr(self._adapter, "reconcile", None)
            if callable(reconcile):
                recovered = reconcile(_tophand_operation_dict(operation))
                if isinstance(recovered, Mapping):
                    candidate = recovered.get(operation.operation_id)
                    if candidate is not None:
                        result = _provider_result(candidate, operation)
                        self._result_sink(candidate)
                        return result
        payload: dict[str, object] = {"operation": _tophand_operation_dict(operation)}
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
                    "goal_id": request.goal_id,
                    "goal_version": request.goal_version,
                    "resume_after_close": request.resume_after_close,
                    "close_operation_id": request.close_operation_id,
                    "resume_token": request.resume_token,
                    "owner_process": (
                        request.owner_process.model_dump(mode="json")
                        if request.owner_process is not None
                        else None
                    ),
                }
            )
        elif isinstance(request, CancelCurrentTurnRequest):
            payload["reason"] = request.reason
        elif isinstance(request, CloseRequest):
            self._verify_close_checkpoint(request)
            payload["archive"] = request.archive
        raw = getattr(self._adapter, method)(payload)
        self._require_current_facts()
        result = _provider_result(raw, operation)
        self._result_sink(raw)
        return result

    def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
        return self._call("create_or_resume", request, request.operation)

    def status(self) -> ProviderStatus:
        self._require_current_facts()
        adapter = cast(Any, self._adapter)
        raw = _mapping(adapter.status(), "Tophand status")
        self._require_current_facts()
        state = _provider_state(raw.get("state", "unknown"))
        provider_session_id = raw.get("provider_session_id")
        provider_instance_id = raw.get("provider_instance_id")
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
            provider_instance_id=provider_instance_id if isinstance(provider_instance_id, str) else None,
            context_available=context_available if isinstance(context_available, bool) else None,
            current_turn_id=current_turn_id if isinstance(current_turn_id, str) else None,
            last_event_id=last_event_id if isinstance(last_event_id, str) else None,
            reason=reason if isinstance(reason, str) else "",
            process_start_token=(
                raw.get("process_start_token")
                if isinstance(raw.get("process_start_token"), str)
                else self._process_start_token
            ),
        )

    def send(self, request: SendRequest) -> ProviderOperationResult:
        return self._call("send", request, request.operation)

    def read_updates(self, cursor: str | None = None) -> ReadUpdatesResult:
        self._require_current_facts()
        adapter = cast(Any, self._adapter)
        raw = _mapping(adapter.read_updates(cursor), "Tophand update batch")
        self._require_current_facts()
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
        # Validate before entering the provider-outage fallback. A forged
        # checkpoint must be rejected as an invalid boundary, not converted
        # into an apparently ordinary provider-unknown response.
        self._verify_close_checkpoint(request)
        try:
            payload = json.loads(operation.payload)
            if not isinstance(payload, Mapping):
                raise ValueError("Chitra close payload must be an object")
            wire_request = {
                "operation": operation.model_dump(mode="json"),
                "archive": request.archive,
                "payload": dict(payload),
                "goal_id": payload.get("goal_id"),
                "lane_id": operation.lane_id,
                "session_ref": payload.get("session_ref"),
                "provider_session_id": operation.provider_session_id,
                "checkpoint_ref": payload.get("checkpoint_ref"),
                "checkpoint_receipt": request.checkpoint_receipt,
                "checkpoint_receipt_sha256": request.checkpoint_receipt_sha256,
                "checkpoint_verifier": request.checkpoint_verifier,
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
    capability_receipt_digest: str
    capability_receipt_expires_at: str
    capability_receipt_json: str


def _amp_runtime_config(
    operating_facts: Sequence[OperatingFact],
    *,
    expected_project_ref: str,
    expected_profile_digest: str,
    expected_version: str,
    capability_verifier: CapabilitySignatureVerifier | None = None,
    now: datetime | None = None,
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
        capability_receipt = surface.get("capability_probe")
        verified_receipt = verify_amp_capability_receipt(
            capability_receipt,
            expected_binary=binary,
            expected_version=version,
            expected_project_ref=expected_project_ref,
            expected_profile_digest=expected_profile_digest,
            expected_orb_size=orb_size,
            now=now,
            signature_verifier=capability_verifier,
        )
        if verified_receipt is None:
            # Fleet has not published a current, signed probe result.  The
            # ordinary lane surface must remain unavailable.
            return None
        candidates.add(
            _AmpRuntimeConfig(
                binary=binary,
                version=version,
                orb_size=orb_size,
                visibility=visibility,
                fleet_enabled=fleet_enabled,
                capability_receipt_digest=verified_receipt.digest,
                capability_receipt_expires_at=verified_receipt.expires_at,
                capability_receipt_json=json.dumps(
                    verified_receipt.value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
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
    # The pinned Amp version is checked by the adapter's runtime capability
    # probe and retained in Chitra's typed usage evidence.
    amp_version = raw.get("amp_version")
    if not isinstance(amp_version, str) or not amp_version.strip():
        raise ValueError("Amp usage report lacks its reviewed amp_version")
    expected = _amp_child_roster(lane_reader)
    observed = raw.get("child_roster", ())
    if not isinstance(observed, Sequence) or isinstance(observed, (str, bytes)):
        raise TypeError("Amp usage child_roster must be a sequence")
    if observed and not all(
        isinstance(value, Mapping) and "retained_state" in value and "material_result" in value
        for value in observed
    ):
        raise ValueError("Amp usage roster lacks retained-state or material-result evidence")
    elif observed:
        canonical = tuple(ChildRosterEntry.from_dict(value) for value in observed)
        # A root ORB can retain a material built-in Task result without a
        # durable provider child.  The adapter has already bound this
        # synthetic roster to the lane and aggregate usage evidence.
        inline_discovery = raw.get("child_evidence_mode") == "inline" and not expected
        if not inline_discovery and canonical != expected:
            raise ValueError("Amp usage changed Chitra's child roster evidence")
        raw["child_roster"] = [entry.model_dump(mode="json") for entry in canonical]
    elif raw.get("child_evidence_mode") == "inline" and raw.get("complete") is True:
        raise ValueError("complete inline Amp usage omitted its material child result")
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
        if observed is None or observed != expected:
            return _unknown_close_result(operation, f"Amp close result {field} is missing or changed")
    raw_provider_session_id = raw.get("provider_session_id")
    if raw_provider_session_id is not None and (
        not isinstance(raw_provider_session_id, str) or not raw_provider_session_id.strip()
    ):
        return _unknown_close_result(operation, "Amp close result provider_session_id is malformed")
    if operation.provider_session_id is not None and raw_provider_session_id != operation.provider_session_id:
        return _unknown_close_result(operation, "Amp close result provider_session_id changed or is missing")
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
    # Amp can archive a thread, but this facade has no process-start identity
    # and no authenticated reopen receipt. Preserve the archive evidence while
    # refusing to advertise later resume as a Chitra capability.
    later_resume_supported = False
    quiescent = raw.get("quiescent")
    evidence = raw.get("evidence")
    observed_at = raw.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at.strip():
        return _unknown_close_result(operation, "Amp close result observed_at is missing")
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
        return _unknown_close_result(
            operation,
            "Amp archive did not prove Chitra's governed close conditions and later resume",
        )
    return CloseArchiveResult(
        operation_id=operation.operation_id,
        lane_id=operation.lane_id,
        provider_handle=operation.provider_handle,
        provider_instance_id=cast(str, raw["provider_instance_id"]),
        provider_generation=cast(int, raw["provider_generation"]),
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
                return cast(object, value)
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
        process_start_token: str | None = None,
        operating_facts_binding: OperatingFactsBinding | None = None,
        update_batch_sink: Callable[[Sequence[object], str], object | None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._result_sink = result_sink
        self._cursor_sink = cursor_sink
        self._lane_reader = lane_reader
        self._process_start_token = process_start_token
        self._operating_facts_binding = operating_facts_binding
        self._update_batch_sink = update_batch_sink

    def _require_current_facts(self) -> None:
        if not _facts_binding_current(self._operating_facts_binding):
            raise RuntimeError("Fleet operating-facts binding expired; recovery will retry")

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
                    "goal_id": request.goal_id,
                    "goal_version": request.goal_version,
                    "resume_after_close": request.resume_after_close,
                    "close_operation_id": request.close_operation_id,
                    "owner_process": (
                        request.owner_process.model_dump(mode="json")
                        if request.owner_process is not None
                        else None
                    ),
                    "resume_token": request.resume_token,
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
        self._require_current_facts()
        raw = getattr(self._adapter, method)(self._payload(request, operation))
        self._require_current_facts()
        result = _provider_result(raw, operation, provider_label="Amp")
        self._result_sink(raw)
        return result

    def create_or_resume(self, request: CreateOrResumeRequest) -> ProviderOperationResult:
        # The exact pending envelope crosses unchanged.  AmpAdapter performs
        # the exact-tag search and holds on zero-result lost creates.
        return self._call("create_or_resume", request, request.operation)

    def status(self) -> ProviderStatus:
        self._require_current_facts()
        adapter = cast(Any, self._adapter)
        raw = _mapping(adapter.status(None), "Amp status")
        self._require_current_facts()
        state = _provider_state(raw.get("state", "unknown"))
        generation = raw.get("generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int):
            generation = 0
        provider_session_id = raw.get("provider_session_id")
        provider_instance_id = raw.get("provider_instance_id")
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
            provider_instance_id=provider_instance_id if isinstance(provider_instance_id, str) else None,
            context_available=context_available if isinstance(context_available, bool) else None,
            current_turn_id=current_turn_id if isinstance(current_turn_id, str) else None,
            last_event_id=last_event_id if isinstance(last_event_id, str) else None,
            reason=reason if isinstance(reason, str) else "",
            process_start_token=(
                raw.get("process_start_token")
                if isinstance(raw.get("process_start_token"), str)
                else self._process_start_token
            ),
        )

    def send(self, request: SendRequest) -> ProviderOperationResult:
        return self._call("send", request, request.operation)

    def read_updates(self, cursor: str | None = None) -> ReadUpdatesResult:
        self._require_current_facts()
        adapter = cast(Any, self._adapter)
        raw = _mapping(adapter.read_updates(cursor), "Amp update batch")
        self._require_current_facts()
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
        self._require_current_facts()
        adapter = cast(Any, self._adapter)
        raw = adapter.usage(None)
        self._require_current_facts()
        return _amp_usage_report(raw, self._lane_reader)

    def cancel_current_turn(self, request: CancelCurrentTurnRequest) -> ProviderOperationResult:
        return self._call("cancel_current_turn", request, request.operation)

    def close(self, request: CloseRequest) -> CloseArchiveResult:
        self._require_current_facts()
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
            self._require_current_facts()
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
    operating_facts_binding: OperatingFactsBinding | None,
) -> Provider | None:
    """Construct the only packaged production provider route.

    Identity and restart fences must be present in Chitra's record.  The
    adapter receives snapshots and callbacks but no authority to create a
    second state store.  The facts argument is intentionally consumed only as
    an explicit availability gate; the adapter cannot infer a route from it.
    """

    del facts_reader, operating_facts
    if identity.kind != "tophand" or _packaged_tophand_builder is None:
        return None
    if operating_facts_binding is not None and (
        identity.operating_facts_digest != operating_facts_binding.digest
        or identity.operating_facts_deadline != operating_facts_binding.deadline
        or identity.target_host != operating_facts_binding.target_host
        or identity.target_account != operating_facts_binding.target_account
    ):
        return None
    if identity.instance_id is None or identity.generation is None:
        return None
    pending = () if record.pending_operation is None else (_tophand_operation_dict(record.pending_operation),)
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
            # The Chitra facade is the sole production result sink.  The
            # adapter remains a translation layer and must not invoke the
            # same callback a second time.
            result_sink=None,
            process_start_token=identity.process_start_token,
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
    provider = _PackagedTophandProvider(
        adapter,
        state_root=state_root,
        result_sink=result_sink,
        process_start_token=identity.process_start_token,
        reconcile_before_call=(record.pending_operation is not None and record.pending_operation.attempted),
        operating_facts_binding=operating_facts_binding,
    )
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
    operating_facts_binding: OperatingFactsBinding | None,
    amp_capability_verifier: CapabilitySignatureVerifier | None = None,
) -> Provider | None:
    """Build the one static Amp route after Chitra's launch-policy gate."""

    del pending_sink, event_sink, checkpoint_verifier, cancel_verifier
    if operating_facts_binding is not None and (
        identity.operating_facts_digest != operating_facts_binding.digest
        or identity.operating_facts_deadline != operating_facts_binding.deadline
        or identity.target_host != operating_facts_binding.target_host
        or identity.target_account != operating_facts_binding.target_account
    ):
        return None
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
        capability_verifier=amp_capability_verifier,
    )
    if runtime is None:
        return None
    amp_binary = runtime.binary
    amp_version = runtime.version

    store = JoinedLaneStore(state_root)
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
            reviewed_subagents=True,
            capability_receipt_digest=runtime.capability_receipt_digest,
            capability_receipt_expires_at=runtime.capability_receipt_expires_at,
            capability_receipt=json.loads(runtime.capability_receipt_json),
            capability_receipt_verifier=amp_capability_verifier,
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
            process_start_token=identity.process_start_token,
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
        process_start_token=identity.process_start_token,
        operating_facts_binding=operating_facts_binding,
        update_batch_sink=_canonical_update_batch_sink(lane),
    )
    return provider if isinstance(provider, Provider) else None


def _cursor_is_not_older(previous: str, current: str) -> bool:
    if not previous:
        return True
    if not current:
        return False
    pattern = re.compile(
        r"^amp:(?P<thread>[^:]+):offset:(?P<offset>[0-9]+):"
        r"boundary:(?P<boundary>[^:]+):prefix:(?P<prefix>[0-9a-f]{64})$"
    )
    previous_match = pattern.match(previous)
    current_match = pattern.match(current)
    if current_match is None:
        # An opaque initial cursor may be accepted once.  A bound Amp cursor
        # may never be replaced by malformed or replay-shaped text.
        return previous_match is None
    if previous_match is None:
        return True
    if previous_match.group("thread") != current_match.group("thread"):
        return False
    previous_offset = int(previous_match.group("offset"))
    current_offset = int(current_match.group("offset"))
    if current_offset < previous_offset:
        return False
    if current_offset == previous_offset:
        return current == previous
    return True


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
            if not _cursor_is_not_older(current.update_cursor, cursor):
                raise ValueError("provider cursor regressed or changed its Amp thread")
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
                process_start_token=current.provider.process_start_token,
                idempotency_key=prior.idempotency_key,
                payload_digest=prior.payload_digest,
                provider_instance_id=prior.provider_instance_id,
                provider_generation=prior.provider_generation,
                created_at=reference.created_at,
                attempted=True,
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
        raw = _mapping(value, "provider session update")
        nested = raw.get("update")
        update = LaneUpdate.from_dict(nested if isinstance(nested, Mapping) else raw)

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
            if not _cursor_is_not_older(current.update_cursor, cursor):
                raise ValueError("provider cursor regressed or changed its Amp thread")
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
    amp_capability_verifier: CapabilitySignatureVerifier | None = None,
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

        def packaged_amp_factory(**kwargs: object) -> Provider | None:
            return _canonical_amp_factory(
                **cast(dict[str, Any], kwargs),
                amp_capability_verifier=amp_capability_verifier,
            )

        amp_factory = cast(RecoveryProviderFactory, packaged_amp_factory)

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
    strict_production_facts = production_defaults and facts_reader is None and operating_facts_reader is None
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
            operating_facts_binding: OperatingFactsBinding | None = None
            if strict_production_facts:
                snapshot = read_operating_facts(operating_facts_sources)
                operating_facts_binding = bind_current_operating_facts(snapshot, provider_kind=kind)
                if operating_facts_binding is None:
                    logger.info(
                        "recovery_waiting_for_current_operating_facts",
                        lane_id=record.lane_id,
                        provider_kind=kind,
                    )
                    return None
                operating_facts = tuple(snapshot.facts)
                if lane.target_host is not None and lane.target_host != operating_facts_binding.target_host:
                    return None
                if lane.target_account is not None and lane.target_account != operating_facts_binding.target_account:
                    return None
                if record.provider.target_host is not None and record.provider.target_host != operating_facts_binding.target_host:
                    return None
                if record.provider.target_account is not None and record.provider.target_account != operating_facts_binding.target_account:
                    return None
                identity = record.provider.model_copy(
                    update={
                        "target_host": operating_facts_binding.target_host,
                        "target_account": operating_facts_binding.target_account,
                        "operating_facts_digest": operating_facts_binding.digest,
                        "operating_facts_deadline": operating_facts_binding.deadline,
                    }
                )
            else:
                operating_facts = tuple(bindings.facts_reader(record))
                identity = record.provider
            provider = factory(
                identity=identity,
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
                operating_facts_binding=operating_facts_binding,
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
    "build_tophand_registration_identity_resolver",
    "default_operating_facts_reader",
    "RecoverySink",
    "RecoveryVerifier",
    "build_recovery_provider_resolver",
]

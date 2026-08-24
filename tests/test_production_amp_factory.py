"""Adversarial tests for the static production Amp provider route."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from _goal_fixtures import enrollment_fields

import chitra.recovery as recovery
from chitra import dispatchd
from chitra.detect.detectors import Finding
from chitra.detect.ladder import IncidentStore
from chitra.goals import GoalRecord, upsert_goal
from chitra.joined_lane import JoinedLaneIdentityError, JoinedLaneStore, ReconcileReport
from chitra.lane_config import LaneCredentials, LaneSpec
from chitra.orders import DispatchOrder
from chitra.provider_protocol import (
    CheckpointRequest,
    CloseRequest,
    CreateOrResumeRequest,
    ProviderName,
    ProviderOperationResult,
    SendRequest,
)
from chitra.recovery_provider import (
    _canonical_recovery_bindings,
    _PackagedAmpProvider,
    _PackagedTophandProvider,
    build_recovery_provider_resolver,
)
from chitra.operating_facts import OperatingFactsBinding
from chitra.session_contract import (
    CapabilityName,
    JoinedLaneRecord,
    LaneLaunchPolicy,
    LaneUpdate,
    OperatingFact,
    PendingProviderOperation,
    ProviderCapabilities,
    ProviderIdentity,
    RoadmapStep,
)

NOW = datetime(2026, 8, 23, 15, tzinfo=UTC)
PROFILE_DIGEST = "sha256:" + "a" * 64
AMP_VERSION = "0.0.1787241916-g56aafe"
CAPABILITIES: tuple[CapabilityName, ...] = (
    "create_or_resume",
    "status",
    "send",
    "read_updates",
    "checkpoint",
    "usage",
    "cancel_current_turn",
    "close",
    "resume_after_close",
    "parent_child_usage",
)


def _lane(root: Path) -> LaneSpec:
    return LaneSpec(
        identifier="amp-lane",
        account="amp-lane",
        uid=1000,
        home=root / "home",
        workdir=root / "workdir",
        config_dir=root / "config",
        state_dir=root / "state",
        tmux_socket=root / "tmux.sock",
        tmux_session="amp-lane",
        credentials=LaneCredentials(
            claude_credentials=root / "claude.json",
            ssh_dispatch_key=root / "dispatch.key",
        ),
    )


def _record(lane: LaneSpec, *, policy: bool = True, parent_thread_ref: str | None = "amp-parent-a") -> JoinedLaneRecord:
    update = LaneUpdate(
        lane_id=lane.identifier,
        goal_id="goal-amp",
        session_ref="amp:amp-lane:1",
        goal_version=1,
        observed_at=NOW.isoformat(),
        plan_version=1,
        sequence=1,
        steps=(RoadmapStep(id="implement", status="active", owner="lane-manager"),),
        current_action="Implement the enrolled change",
        next_action="Run the focused acceptance check",
    )
    identity = ProviderIdentity(
        kind="amp",
        handle="amp-thread-a",
        instance_id="amp-instance-a",
        generation=1,
        parent_thread_ref=parent_thread_ref,
        project_ref="amp-project-a",
        profile_digest=PROFILE_DIGEST,
        provider_version=AMP_VERSION,
        capabilities=ProviderCapabilities.from_supported(CAPABILITIES),
    )
    launch_policy = (
        LaneLaunchPolicy(
            lane_id=lane.identifier,
            goal_id=update.goal_id,
            goal_version=1,
            project_ref="amp-project-a",
            profile_digest=PROFILE_DIGEST,
            provider_version=AMP_VERSION,
            cost_ceiling_usd=10,
            turn_reserve_usd=1,
            usage_poll_interval_seconds=30,
            usage_max_age_seconds=120,
            created_at=NOW.isoformat(),
        )
        if policy
        else None
    )
    return JoinedLaneRecord(
        lane_id=lane.identifier,
        goal_id=update.goal_id,
        goal_version=1,
        session_ref=update.session_ref,
        provider=identity,
        launch_policy=launch_policy,
        current_update=update,
    )


def _operation(kind: str, *, operation_id: str = "operation-1") -> PendingProviderOperation:
    return PendingProviderOperation(
        operation_id=operation_id,
        kind=kind,  # type: ignore[arg-type]
        lane_id="amp-lane",
        provider_handle="amp-thread-a",
        idempotency_key=f"idem-{operation_id}",
        payload_digest=f"digest-{operation_id}",
        provider_instance_id="amp-instance-a",
        provider_generation=1,
        created_at=NOW.isoformat(),
    )


def _amp_facts(record: JoinedLaneRecord) -> tuple[OperatingFact, ...]:
    return (
        OperatingFact(
            name="fleet.provider-capabilities",
            value={
                "amp": {
                    "binary": "/usr/local/bin/amp",
                    "version": record.provider.provider_version,
                },
                "orb_lane_surface": {
                    "provider": "amp",
                    "amp_binary_path": "/usr/local/bin/amp",
                    "amp_version": record.provider.provider_version,
                    "project_ref": record.provider.project_ref,
                    "profile_digest": record.provider.profile_digest,
                    "enabled": False,
                    "visibility": "private",
                    "orb_size": "a1.tiny",
                    "no_archive_after_execute": True,
                },
            },
            state="known",
            source="fleet-authority",
            revision="amp-runtime-1",
            observed_at=NOW.isoformat(),
            freshness="current",
            fresh_until=(NOW + timedelta(days=1)).isoformat(),
            within_authority=True,
        ),
    )


def _production_facts(record: JoinedLaneRecord) -> tuple[OperatingFact, ...]:
    """Return all seven Fleet categories for a positive production fixture."""
    common = {
        "state": "known",
        "source": "fleet-authority:test",
        "revision": "amp-production-fixture-1",
        "observed_at": NOW.isoformat(),
        "freshness": "current",
        "fresh_until": (NOW + timedelta(days=3)).isoformat(),
        "within_authority": True,
    }
    return (
        OperatingFact(name="fleet.placement", value={"host": "twinridge", "account": "ubuntu"}, **common),
        OperatingFact(
            name="fleet.routing",
            value={"dispatch_target": {"host": "tophand", "user": "ubuntu"}},
            **common,
        ),
        OperatingFact(name="fleet.credential-readiness", value={"dispatch": {"ready": True}}, **common),
        OperatingFact(name="fleet.access", value={"dispatch": {"ready": True}}, **common),
        OperatingFact(name="fleet.capacity", value={"slots": 2}, **common),
        OperatingFact(name="fleet.versions", value={"chitra": "0.16.0"}, **common),
        _amp_facts(record)[0],
    )


def _write_production_facts(path: Path, facts: tuple[OperatingFact, ...]) -> None:
    """Write a byte- and mode-verifiable Fleet publisher receipt."""
    source_path = path.with_name("approved-operating-facts-inputs.json")
    source_bytes = json.dumps(
        {"fixture": "amp-production", "facts": [fact.to_dict() for fact in facts]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    source_path.write_bytes(source_bytes)
    core = {
        "schema": "chitra.operating-facts.v1",
        "observed_at": NOW.isoformat(),
        "facts": [fact.to_dict() for fact in facts],
    }
    payload = {
        **core,
        "provenance": {
            "schema": "chitra.operating-facts-provenance.v1",
            "source_path": str(source_path),
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "source_mode": 0o644,
            "snapshot_sha256": hashlib.sha256(
                json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "snapshot_mode": 0o644,
            "readback_verified": True,
            "readback_at": NOW.isoformat(),
        },
    }
    path.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    path.chmod(0o644)


def _resolver(lane: LaneSpec, record: JoinedLaneRecord) -> recovery.RecoveryProviderResolver:
    return build_recovery_provider_resolver(
        lane,
        facts_reader=lambda _record: _amp_facts(record),
    )


class _AmpProfile:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        self.project_ref = kwargs["project_ref"]
        self.visibility = kwargs["visibility"]


class _AmpTransport:
    calls: list[tuple[object, dict[str, object]]] = []
    capabilities = {name: True for name in CAPABILITIES} | {"subagents": False}

    def __init__(self, profile: object, **kwargs: object) -> None:
        self.calls.append((profile, kwargs))


class _AmpAdapter:
    calls: list[tuple[str, dict[str, object]]] = []
    instances: list[dict[str, object]] = []
    event_log: list[str] | None = None
    capabilities = _AmpTransport.capabilities

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.instances.append(kwargs)

    @staticmethod
    def _result(request: dict[str, object], *, status: str = "accepted", consumed: bool | None = None) -> dict[str, object]:
        operation = request["operation"]
        assert isinstance(operation, dict)
        return {
            **operation,
            "status": status,
            "accepted": True if status in {"accepted", "consumed"} else None,
            "consumed": consumed,
            "observed_at": NOW.isoformat(),
            "evidence": "Amp fixture evidence",
        }

    def create_or_resume(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append(("create_or_resume", request))
        return self._result(request, status="consumed", consumed=True)

    def status(self, _request: object = None) -> dict[str, object]:
        self.calls.append(("status", {}))
        return {
            "provider": "amp",
            "provider_session_id": "amp-thread-a",
            "state": "idle",
            "generation": 1,
            "fresh": True,
            "provider_available": True,
        }

    def send(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append(("send", request))
        if self.event_log is not None:
            self.event_log.append("recovery-send")
        return self._result(request)

    def read_updates(self, _request: object = None) -> dict[str, object]:
        self.calls.append(("read_updates", {}))
        return {"updates": (), "next_cursor": "0", "provider_available": True, "complete": True}

    def checkpoint(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append(("checkpoint", request))
        return self._result(request, status="consumed", consumed=True)

    def usage(self, _request: object = None) -> dict[str, object]:
        self.calls.append(("usage", {}))
        return {
            "parent": {"name": "amp-lane", "amount": 1, "unit": "usd"},
            "children": [],
            "child_roster": [],
            "child_roster_complete": True,
            "child_roster_evidence": "amp-roster",
            "total": {"name": "total", "amount": 1, "unit": "usd"},
            "evidence_source": "amp-fixture",
            "observed_at": NOW.isoformat(),
            "complete": True,
        }

    def cancel_current_turn(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append(("cancel_current_turn", request))
        return self._result(request, status="consumed", consumed=True)

    def close(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append(("close", request))
        operation = request["operation"]
        assert isinstance(operation, dict)
        return {
            **operation,
            "provider_thread_ref": "amp-thread-a",
            "state": "archived",
            "same_provider_thread": True,
            "later_resume_supported": True,
            "checkpoint_ref": "chitra-checkpoint-1",
            "quiescent": True,
            "observed_at": NOW.isoformat(),
            "evidence": "post-archive export proves same thread and quiescence",
        }


class _ArchiveTransport:
    def state(self, _thread_id: str) -> SimpleNamespace:
        return SimpleNamespace(state="idle", current_turn_id=None)

    def archive(self, _thread_id: str) -> dict[str, object]:
        return {
            "state": "archived",
            "same_provider_thread": True,
            "quiescent": True,
            "later_resume_supported": True,
            "checkpoint_ref": "amp-post-export-digest",
            "observed_at": NOW.isoformat(),
            "evidence": "post-archive export is archived and idle",
        }


class _TransportBackedAmp(_AmpAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.transport = _ArchiveTransport()


def _install_amp_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    import chitra.recovery_provider as module

    _AmpProfile.calls.clear()
    _AmpTransport.calls.clear()
    _AmpAdapter.calls.clear()
    _AmpAdapter.instances.clear()
    _AmpAdapter.event_log = None
    monkeypatch.setattr(module, "_packaged_amp_profile", _AmpProfile, raising=True)
    monkeypatch.setattr(module, "_packaged_amp_transport", _AmpTransport, raising=True)
    monkeypatch.setattr(module, "_packaged_amp_adapter", _AmpAdapter, raising=True)


def test_amp_factory_is_disabled_without_explicit_launch_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Amp identity alone cannot activate a provider or transport."""
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)
    resolver = _resolver(lane, record)

    assert resolver(record.model_copy(update={"launch_policy": None})) is None
    assert _AmpProfile.calls == []
    assert _AmpTransport.calls == []
    assert _AmpAdapter.calls == []


def test_amp_factory_rejects_missing_or_mismatched_twinridge_runtime_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production route never falls back to the macOS Amp default."""
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)
    bad_fact = _amp_facts(record)[0].model_copy(
        update={
            "value": {
                "orb_lane_surface": {
                    "provider": "amp",
                    "amp_binary_path": "/opt/homebrew/bin/amp",
                    "amp_version": record.provider.provider_version,
                }
            }
        }
    )
    resolver = build_recovery_provider_resolver(
        lane,
        facts_reader=lambda _record: (bad_fact,),
    )

    assert resolver(record) is None
    assert _AmpProfile.calls == []
    assert _AmpTransport.calls == []
    assert _AmpAdapter.calls == []


def test_amp_factory_rejects_conflicting_amp_runtime_fact_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested ORB and top-level Amp runtime pins cannot disagree."""
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)
    source = _amp_facts(record)[0]
    value = cast(dict[str, object], source.value)
    surface = cast(dict[str, object], value["orb_lane_surface"])
    value["orb_lane_surface"] = {**surface, "amp_version": "different-reviewed-pin"}
    conflicting_fact = source.model_copy(update={"value": value})

    resolver = build_recovery_provider_resolver(
        lane,
        facts_reader=lambda _record: (conflicting_fact,),
    )

    assert resolver(record) is None
    assert _AmpProfile.calls == []
    assert _AmpTransport.calls == []
    assert _AmpAdapter.calls == []


def test_amp_factory_rejects_authoritative_version_drift_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production route rejects a Fleet version that differs from Chitra's identity."""
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)
    source = _amp_facts(record)[0]
    value = cast(dict[str, object], source.value)
    surface = cast(dict[str, object], value["orb_lane_surface"])
    runtime = cast(dict[str, object], value["amp"])
    drift = f"{record.provider.provider_version}-drift"
    value["orb_lane_surface"] = {**surface, "amp_version": drift}
    value["amp"] = {**runtime, "version": drift}
    drifted_fact = source.model_copy(update={"value": value})
    resolver = build_recovery_provider_resolver(
        lane,
        facts_reader=lambda _record: (drifted_fact,),
    )

    assert resolver(record) is None
    assert _AmpProfile.calls == []
    assert _AmpTransport.calls == []
    assert _AmpAdapter.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    (("orb_size", "a1.large"), ("visibility", "public"), ("enabled", True)),
)
def test_amp_factory_requires_authoritative_fleet_orb_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)
    source = _amp_facts(record)[0]
    fact_value = cast(dict[str, object], source.value)
    surface = cast(dict[str, object], fact_value["orb_lane_surface"])
    fact_value["orb_lane_surface"] = {**surface, field: value}

    resolver = build_recovery_provider_resolver(
        lane,
        facts_reader=lambda _record: (source.model_copy(update={"value": fact_value}),),
    )

    assert resolver(record) is None
    assert _AmpProfile.calls == []


def test_amp_goal_bootstrap_requires_and_persists_authoritative_policy_inputs(tmp_path: Path) -> None:
    """Amp enrollment stores measured capabilities and the exact Chitra policy."""
    lane = _lane(tmp_path)
    source = _record(lane)
    goal = SimpleNamespace(
        lane_id=lane.identifier,
        session_ref=source.session_ref,
        goal_id=source.goal_id,
        goal_version=source.goal_version,
    )
    provider_result = ProviderOperationResult(
        operation_id="bootstrap-op",
        kind="send",
        lane_id=lane.identifier,
        provider_handle=source.provider.handle,
        idempotency_key="bootstrap-idem",
        payload_digest="bootstrap-digest",
        provider_instance_id=source.provider.instance_id,
        provider_generation=source.provider.generation,
        status="accepted",
        accepted=True,
        consumed=None,
        observed_at=NOW.isoformat(),
    )
    store = JoinedLaneStore(lane.state_dir)
    created = store.ensure_from_goal(
        goal,
        provider_result,
        provider_kind="amp",
        provider_capabilities=source.provider.capabilities,
        provider_project_ref=source.provider.project_ref,
        provider_profile_digest=source.provider.profile_digest,
        provider_version=source.provider.provider_version,
        launch_policy=source.launch_policy,
    )
    assert created.provider.capabilities == source.provider.capabilities
    assert created.provider.provider_session_id == created.session_ref
    assert created.launch_policy == source.launch_policy
    assert created.provider.project_ref == source.provider.project_ref
    assert created.provider.profile_digest == source.provider.profile_digest
    assert created.provider.provider_version == source.provider.provider_version

    with pytest.raises(JoinedLaneIdentityError, match="measured provider capabilities"):
        JoinedLaneStore(tmp_path / "missing").ensure_from_goal(
            goal,
            provider_result,
            provider_kind="amp",
            launch_policy=source.launch_policy,
            provider_project_ref=source.provider.project_ref,
            provider_profile_digest=source.provider.profile_digest,
            provider_version=source.provider.provider_version,
        )


def test_amp_facade_exposes_eight_operations_and_keeps_checkpoint_in_chitra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The facade maps the shared protocol without invoking an Amp checkpoint."""
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)
    resolver = _resolver(lane, record)
    provider = resolver(record)
    assert provider is not None
    assert provider.provider_name == ProviderName.AMP
    assert all(hasattr(provider, name) for name in (
        "create_or_resume",
        "status",
        "send",
        "read_updates",
        "checkpoint",
        "usage",
        "cancel_current_turn",
        "close",
    ))
    assert provider.capabilities.checkpoint is True

    checkpoint = _operation("checkpoint")
    result = provider.checkpoint(CheckpointRequest(operation=checkpoint, label="governed-checkpoint"))
    assert result.status == "unknown"
    assert not any(name == "checkpoint" for name, _request in _AmpAdapter.calls)


@pytest.mark.parametrize("provider_kind", ("tophand", "amp"))
def test_expired_facts_binding_blocks_every_provider_facade_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_kind: str
) -> None:
    """A stale route cannot reach either provider adapter after restart or wake."""
    _install_amp_fakes(monkeypatch)
    expired = OperatingFactsBinding(
        digest="sha256:" + "b" * 64,
        deadline="2020-01-01T00:00:00Z",
        source_path=str(tmp_path / "approved-inputs.json"),
        source_sha256="c" * 64,
        source_mode=0o644,
        snapshot_mode=0o644,
        target_host="twinridge",
        target_account="ubuntu",
    )
    adapter = _AmpAdapter()
    if provider_kind == "tophand":
        provider = _PackagedTophandProvider(
            adapter,
            result_sink=lambda _value: None,
            operating_facts_binding=expired,
        )
    else:
        provider = _PackagedAmpProvider(
            adapter,
            result_sink=lambda _value: None,
            cursor_sink=lambda _value: None,
            lane_reader=lambda: {},
            operating_facts_binding=expired,
        )

    with pytest.raises(RuntimeError, match="operating-facts binding expired"):
        provider.status()
    assert _AmpAdapter.calls == []


@pytest.mark.parametrize("provider_kind", ("tophand", "amp"))
def test_provider_rechecks_facts_deadline_after_adapter_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_kind: str
) -> None:
    """A route that expires during provider I/O cannot accept the response."""
    import chitra.recovery_provider as recovery_provider

    _AmpAdapter.calls.clear()
    checks = iter((True, False))
    monkeypatch.setattr(
        recovery_provider,
        "_facts_binding_current",
        lambda _binding: next(checks),
        raising=True,
    )
    operation = _operation("send", operation_id=f"post-io-{provider_kind}")
    request = SendRequest(operation=operation, text="continue")
    if provider_kind == "tophand":
        provider = _PackagedTophandProvider(_AmpAdapter(), result_sink=lambda _value: None)
    else:
        provider = _PackagedAmpProvider(
            _AmpAdapter(),
            result_sink=lambda _value: None,
            cursor_sink=lambda _value: None,
            lane_reader=lambda: {},
        )
    with pytest.raises(RuntimeError, match="operating-facts binding expired"):
        provider.send(request)
    assert any(name == "send" for name, _request in _AmpAdapter.calls)


def test_amp_facade_preserves_exact_pending_create_envelope_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create receives the stored envelope; it never derives a replacement payload."""
    _install_amp_fakes(monkeypatch)
    provider = _PackagedAmpProvider(
        _AmpAdapter(),
        result_sink=lambda _value: None,
        cursor_sink=lambda _value: None,
        lane_reader=lambda: {"provider_handle": "amp-thread-a"},
    )
    operation = _operation("create_or_resume", operation_id="lost-create")
    request = CreateOrResumeRequest(
        operation=operation,
        session_ref="amp:amp-lane:1",
        provider_session_id="amp-thread-a",
        context_ref="checkpoint-1",
    )
    result = provider.create_or_resume(request)
    assert result.status == "consumed"
    calls = [request for name, request in _AmpAdapter.calls if name == "create_or_resume"]
    assert len(calls) == 1
    assert calls[0]["operation"] == operation.model_dump(mode="json")


def test_amp_facade_preserves_provider_handle_on_updates() -> None:
    """Update reconciliation retains the exact provider operation handle."""

    class UpdatesAmp(_AmpAdapter):
        def read_updates(self, _cursor: str | None = None) -> dict[str, object]:
            return {
                "updates": [
                    {
                        "operation_id": "operation-1",
                        "event_id": "event-1",
                        "cursor": "cursor-1",
                        "kind": "steer_consumed",
                        "provider_session_id": "amp-session-a",
                        "lane_id": "amp-lane",
                        "provider_handle": "amp-handle-a",
                        "idempotency_key": "idem-operation-1",
                        "payload_digest": "digest-operation-1",
                        "provider_instance_id": "amp-instance-a",
                        "provider_generation": 1,
                        "payload": {},
                    }
                ],
                "next_cursor": "cursor-1",
            }

    provider = _PackagedAmpProvider(
        UpdatesAmp(),
        result_sink=lambda _value: None,
        cursor_sink=lambda _value: None,
        lane_reader=lambda: {},
    )

    result = provider.read_updates()

    assert len(result.updates) == 1
    assert result.updates[0].provider_handle == "amp-handle-a"
    assert result.updates[0].provider_session_id == "amp-session-a"


def test_amp_factory_binds_chitra_lane_update_sink_for_roadmap_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production ORB route persists the same lane snapshot as Tophand."""

    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)
    store = JoinedLaneStore(lane.state_dir)
    store.create(record)

    provider = _resolver(lane, record)(record)

    assert provider is not None
    sink = _AmpAdapter.instances[-1].get("update_sink")
    assert callable(sink)
    assert record.current_update is not None
    next_update = record.current_update.model_copy(
        update={
            "sequence": 2,
            "current_action": "Review the ORB roadmap snapshot",
            "next_action": "Run the focused ORB acceptance check",
        }
    )
    sink(next_update.to_dict())

    saved = store.require(lane.identifier)
    assert saved.current_update == next_update
    assert saved.current_update.steps[0].owner == "lane-manager"
    assert saved.provider.kind == "amp"


def test_recovery_cursor_sink_persists_into_joined_lane_store(tmp_path: Path) -> None:
    lane = _lane(tmp_path)
    record = _record(lane)
    store = JoinedLaneStore(lane.state_dir)
    store.create(record)
    _pending_sink, cursor_sink, *_rest = _canonical_recovery_bindings(lane)

    cursor_sink("amp-cursor-7")

    assert store.require(lane.identifier).update_cursor == "amp-cursor-7"


def test_amp_close_requires_chitra_checkpoint_and_maps_same_thread_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close combines Chitra checkpoint evidence with Amp archive evidence."""
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)

    def result_sink(_value: object) -> None:
        return None

    def cursor_sink(_value: object) -> None:
        return None

    provider = _resolver(lane, record)(record)
    assert provider is not None

    # The production lane reader gets this value from the joined record.  A
    # direct facade fixture uses the same Chitra-owned context explicitly.
    facade = _PackagedAmpProvider(
        _AmpAdapter(),
        result_sink=result_sink,
        cursor_sink=cursor_sink,
        lane_reader=lambda: {"checkpoint_ref": "chitra-checkpoint-1", "quiescent": True},
    )
    operation = _operation("close", operation_id="close-1")
    closed = facade.close(CloseRequest(operation=operation, archive=True))
    assert closed.state == "archived"
    assert closed.provider_handle == "amp-thread-a"
    assert closed.provider_thread_ref == "amp-thread-a"
    assert closed.checkpoint_ref == "chitra-checkpoint-1"
    assert closed.same_provider_thread is True
    assert closed.quiescent is True

    transport_backed = _PackagedAmpProvider(
        _TransportBackedAmp(),
        result_sink=result_sink,
        cursor_sink=cursor_sink,
        lane_reader=lambda: {"checkpoint_ref": "chitra-checkpoint-1", "quiescent": True},
    )
    transport_closed = transport_backed.close(CloseRequest(operation=operation, archive=True))
    assert transport_closed.state == "archived"
    assert transport_closed.checkpoint_ref == "chitra-checkpoint-1"
    assert "post-archive export" in transport_closed.evidence
    assert any(name == "close" for name, _request in _AmpAdapter.calls)

    missing = _PackagedAmpProvider(
        _AmpAdapter(),
        result_sink=result_sink,
        cursor_sink=cursor_sink,
        lane_reader=lambda: {"checkpoint_ref": None, "quiescent": True},
    )
    unknown = missing.close(CloseRequest(operation=operation, archive=True))
    assert unknown.state == "unknown"


def test_amp_root_lane_does_not_require_parent_thread_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane, parent_thread_ref=None)

    provider = _resolver(lane, record)(record)

    assert provider is not None
    assert _AmpAdapter.instances[-1]["anchor_thread_id"] is None


def test_amp_checkpoint_false_keeps_chitra_checkpoint_and_allows_governed_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)
    provider = _resolver(lane, record)(record)
    assert provider is not None

    class NoAmpCheckpoint(_AmpAdapter):
        capabilities = {**_AmpAdapter.capabilities, "checkpoint": False}

    facade = _PackagedAmpProvider(
        NoAmpCheckpoint(),
        result_sink=lambda _value: None,
        cursor_sink=lambda _value: None,
        lane_reader=lambda: {"checkpoint_ref": "chitra-checkpoint-1", "quiescent": True},
    )
    assert facade.capabilities.checkpoint is False
    checkpoint = facade.checkpoint(
        CheckpointRequest(operation=_operation("checkpoint", operation_id="checkpoint-no-amp"), label="lane")
    )
    assert checkpoint.status == "unknown"
    closed = facade.close(CloseRequest(operation=_operation("close", operation_id="close-no-amp"), archive=True))
    assert closed.state == "archived"
    assert closed.checkpoint_ref == "chitra-checkpoint-1"


def test_lanes_file_uses_static_amp_factory_before_queue_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped entrypoint activates Amp without replacing its resolver."""
    _install_amp_fakes(monkeypatch)
    lane = _lane(tmp_path)
    record = _record(lane)
    goal = upsert_goal(
        lane.state_dir,
        GoalRecord(
            session_ref=record.session_ref,
            lane_id=lane.identifier,
            intent="Ship the enrolled change through the Amp lane safely.",
            goal="Ship and verify the enrolled Amp change for this lane.",
            done_when="The focused acceptance check passes with durable evidence.",
            scope="The enrolled lane only.",
            source="test://amp",
            status="working",
            **cast(Any, enrollment_fields("The focused acceptance check passes with durable evidence.")),
        ),
    )
    record = record.model_copy(
        update={
            "goal_id": goal.goal_id,
            "current_update": (
                record.current_update.model_copy(update={"goal_id": goal.goal_id})
                if record.current_update is not None
                else None
            ),
            "launch_policy": (
                record.launch_policy.model_copy(update={"goal_id": goal.goal_id})
                if record.launch_policy is not None
                else None
            ),
        }
    )
    JoinedLaneStore(lane.state_dir).create(record)
    finding = Finding(
        detector="isolated-review",
        fingerprint_seed={"signature": "amp-entrypoint"},
        event_refs=(),
        unmet_item="isolated reviewer availability",
        expected_next_progress="a material lane update",
        detail="the isolated reviewer was unavailable",
    )
    recovery.RecoveryEngine(state_root=lane.state_dir).schedule(
        record,
        finding.fingerprint,
        now=NOW,
        wake_condition="isolated reviewer availability or a material lane update",
    )
    IncidentStore(lane.state_dir, lane.identifier).open_incident(
        lane=lane.identifier,
        finding=finding,
        order_marker="amp-entrypoint",
    )
    orders = lane.queue_dir / "orders"
    orders.mkdir(parents=True)
    order = DispatchOrder(order_id="amp-order", session_ref=record.session_ref, nudge="continue")
    (orders / "amp-order.json").write_text(order.model_dump_json(), encoding="utf-8")

    facts_path = tmp_path / "operating-facts.json"
    _write_production_facts(facts_path, _production_facts(record))
    monkeypatch.setattr(
        "chitra.operating_facts.PRODUCTION_OPERATING_FACTS_PATH",
        facts_path,
        raising=True,
    )
    monkeypatch.setattr(
        "chitra.operating_facts.PRODUCTION_OPERATING_FACTS_INPUTS_PATH",
        facts_path.with_name("approved-operating-facts-inputs.json"),
        raising=True,
    )

    manifest = tmp_path / "lanes.yaml"
    manifest.write_text(
        "\n".join(
            (
                "lanes:",
                f"  - id: {lane.identifier}",
                f"    account: {lane.account}",
                f"    uid: {lane.uid}",
                f"    home: {lane.home}",
                f"    workdir: {lane.workdir}",
                f"    config_dir: {lane.config_dir}",
                f"    state_dir: {lane.state_dir}",
                f"    tmux_socket: {lane.tmux_socket}",
                f"    tmux_session: {lane.tmux_session}",
                "    credentials:",
                f"      claude_credentials: {lane.credentials.claude_credentials}",
                f"      ssh_dispatch_key: {lane.credentials.ssh_dispatch_key}",
                "    enabled: true",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    events: list[str] = []
    _AmpAdapter.event_log = events

    class FakeReconciler:
        def reconcile_all(self) -> ReconcileReport:
            events.append("reconcile")
            return ReconcileReport(())

    monkeypatch.setattr(dispatchd, "build_filesystem_reconciler", lambda *_args, **_kwargs: FakeReconciler())
    def process_stub(*_args: object, **_kwargs: object) -> None:
        events.append("queue-dispatch")

    monkeypatch.setattr(dispatchd, "process_one_order", process_stub)
    assert dispatchd.main(["--lanes-file", str(manifest), "--once"]) == 0

    persisted = JoinedLaneStore(lane.state_dir).require(lane.identifier)
    assert persisted.pending_operation is not None
    sent = [request for name, request in _AmpAdapter.calls if name == "send"]
    assert len(sent) == 1
    assert sent[0]["operation"] == persisted.pending_operation.model_dump(mode="json")
    assert events == ["recovery-send", "queue-dispatch"]
    assert _AmpProfile.calls == [
        {
            "project_ref": "amp-project-a",
            "orb_size": "a1.tiny",
            "profile_digest": PROFILE_DIGEST,
            "visibility": "private",
        }
    ]
    assert _AmpTransport.calls[0][1]["amp_binary"] == "/usr/local/bin/amp"
    assert _AmpTransport.calls[0][1]["amp_version"] == AMP_VERSION
    assert _AmpAdapter.instances
    assert "state_dir" not in _AmpAdapter.instances[0]
    assert _AmpAdapter.instances[0]["enabled"] is True

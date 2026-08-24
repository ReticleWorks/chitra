"""Adversarial successor contract for production ORB admission.

The tests in this module are deliberately test-only.  They compose Chitra
with the current real Fleet AmpAdapter through an in-memory transport and
exercise process restarts without contacting Amp, Fleet, or any live host.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from test_orb_production_admission import (
    AMP_VERSION,
    CAPABILITIES,
    CREATED_HANDLE,
    PROFILE_DIGEST,
    _goal,
    _orb_fact,
    _OrbLaunchProvider,
    _supervisor,
)
from test_production_amp_factory import (
    CAPABILITY_KEY,
    _amp_facts,
    _AmpAdapter,
    _AmpTransport,
    _install_amp_fakes,
    _lane,
    _production_facts,
    _record,
    _write_production_facts,
    hmac_capability_verifier,
)

from chitra import dispatchd
from chitra.initial_launch import amp_bootstrap_from_facts, amp_bootstrap_record, amp_create_operation
from chitra.joined_lane import JoinedLaneStore
from chitra.recovery import run_recovery_supervision
from chitra.recovery_provider import _PackagedAmpProvider
from chitra.session_contract import (
    ContractValidationError,
    PendingProviderOperation,
    ProviderOperationResult,
    validate_record_transition,
)

ADAPTER_SOURCE_ROOT = Path(os.environ.get("ADAPTER_SOURCE_ROOT", "/private/tmp/adapter-orb-repair-20260824"))
ADAPTER_PACKAGE_ROOT = ADAPTER_SOURCE_ROOT / "tools" / "support" / "chitra_adapter"
PROJECT_REF = "amp-project-a"
REAL_HANDLE = "T-11111111-1111-4111-8111-111111111111"


@pytest.fixture(scope="module")
def real_adapter_modules() -> tuple[types.ModuleType, types.ModuleType]:
    """Load the current peer Adapter without shadowing Chitra's tools package."""

    if not ADAPTER_PACKAGE_ROOT.is_dir():
        pytest.skip(f"current Fleet AmpAdapter source is absent: {ADAPTER_PACKAGE_ROOT}")
    package_name = "_orb_admission_peer_amp_adapter"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ADAPTER_PACKAGE_ROOT)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    return (
        importlib.import_module(f"{package_name}.amp_adapter"),
        importlib.import_module(f"{package_name}.amp_transport"),
    )


class _MemoryAmpTransport:
    """Exact-tag transport double used only under the real AmpAdapter."""

    capabilities = {name: True for name in CAPABILITIES}
    requires_create_marker = False
    runtime_subagents = False
    reviewed_subagents = False

    def __init__(self, transport_module: types.ModuleType, *, matches: int = 0) -> None:
        self._transport_module = transport_module
        self.matches = matches
        self.searches: list[str] = []
        self.posts: list[tuple[dict[str, object], dict[str, object]]] = []

    def search(self, query: str, *, include_archived: bool = True) -> tuple[dict[str, object], ...]:
        assert include_archived is True
        self.searches.append(query)
        tag = query.removeprefix('"').removesuffix('"')
        return tuple(
            {
                "thread_id": f"{REAL_HANDLE[:-1]}{index}",
                "parent_thread_id": None,
                "project_ref": PROJECT_REF,
                "visibility": "private",
                "archived": False,
                "agent_tag": tag,
                "profile_digest": PROFILE_DIGEST,
                "observed_at": datetime.now(UTC).isoformat(),
            }
            for index in range(1, self.matches + 1)
        )

    def post(self, operation: object, payload: dict[str, object]) -> object:
        operation_map = cast(dict[str, object], operation)
        self.posts.append((dict(operation_map), dict(payload)))
        return self._transport_module.TransportAcceptance(
            accepted=True,
            consumed=True,
            provider_handle=REAL_HANDLE,
            observed_at=datetime.now(UTC).isoformat(),
            evidence="in-memory exact create receipt",
        )

    def export(self, _thread_id: str) -> dict[str, object]:
        raise AssertionError("exact-tag adoption must use the search evidence already returned")

    def state(self, _thread_id: str) -> object:
        raise AssertionError("a handleless create must not enter resume state inspection")

    def messages(self, _thread_id: str, *, full: bool, cursor: str | None) -> object:
        raise AssertionError((full, cursor))

    def usage(self, _thread_id: str) -> object:
        raise AssertionError("usage is outside this create contract")

    def archive(self, _thread_id: str, **_kwargs: object) -> object:
        raise AssertionError("archive is outside this create contract")

    def unarchive(self, _thread_id: str, **_kwargs: object) -> object:
        raise AssertionError("unarchive is outside this create contract")

    def cancel(self, _thread_id: str, _turn_id: str, **_kwargs: object) -> object:
        raise AssertionError("cancel is outside this create contract")


class _RecordingAdapter:
    """Record Chitra's wire, then delegate to the unmodified real Adapter."""

    def __init__(self, adapter: object) -> None:
        self._adapter = adapter
        self.operations: list[dict[str, object]] = []

    @property
    def capabilities(self) -> object:
        return self._adapter.capabilities  # type: ignore[attr-defined]

    def create_or_resume(self, request: dict[str, object]) -> dict[str, object]:
        operation = cast(dict[str, object], request["operation"])
        self.operations.append(dict(operation))
        return cast(dict[str, object], self._adapter.create_or_resume(request))  # type: ignore[attr-defined]


def _real_provider(
    modules: tuple[types.ModuleType, types.ModuleType],
    *,
    matches: int,
) -> tuple[_PackagedAmpProvider, _RecordingAdapter, _MemoryAmpTransport]:
    amp_adapter, amp_transport = modules
    transport = _MemoryAmpTransport(amp_transport, matches=matches)
    real = amp_adapter.AmpAdapter(
        transport=transport,
        project_ref=PROJECT_REF,
        profile_digest=PROFILE_DIGEST,
        lane_reader=lambda: {"lane_id": "orb-lane", "known_provider_handles": []},
        amp_version=AMP_VERSION,
        enabled=True,
    )
    recording = _RecordingAdapter(real)
    provider = _PackagedAmpProvider(
        recording,
        result_sink=lambda _value: None,
        cursor_sink=lambda _value: None,
        lane_reader=lambda: {},
    )
    return provider, recording, transport


def _seed_attempted_create(root: Path) -> tuple[object, PendingProviderOperation]:
    goal = _goal(root)
    fact = _orb_fact(goal)
    identity, policy = amp_bootstrap_from_facts(goal, (fact,))
    operation = amp_create_operation(goal, identity)
    record = amp_bootstrap_record(goal, identity, policy, operation)
    attempted = operation.model_copy(update={"attempted": True})
    JoinedLaneStore(root).create(record.model_copy(update={"pending_operation": attempted}))
    return goal, attempted


def test_real_amp_adapter_receives_false_attempt_evidence_for_first_physical_post(
    tmp_path: Path,
    real_adapter_modules: tuple[types.ModuleType, types.ModuleType],
) -> None:
    goal = _goal(tmp_path)
    provider, recording, transport = _real_provider(real_adapter_modules, matches=0)

    run_recovery_supervision(_supervisor(tmp_path, goal, provider, (_orb_fact(goal),)))

    assert len(recording.operations) == 1
    assert recording.operations[0].get("create_attempted") is False
    assert len(transport.posts) == 1
    assert transport.posts[0][0]["create_attempted"] is False
    assert JoinedLaneStore(tmp_path).require(goal.lane_id).provider.handle == REAL_HANDLE


@pytest.mark.parametrize(
    ("matches", "expected_handle"),
    ((0, None), (1, f"{REAL_HANDLE[:-1]}1"), (2, None)),
    ids=("zero-holds", "one-adopts", "multiple-fail-closed"),
)
def test_real_amp_adapter_restart_uses_true_attempt_evidence_and_never_reposts(
    tmp_path: Path,
    real_adapter_modules: tuple[types.ModuleType, types.ModuleType],
    matches: int,
    expected_handle: str | None,
) -> None:
    goal, pending = _seed_attempted_create(tmp_path)
    provider, recording, transport = _real_provider(real_adapter_modules, matches=matches)

    run_recovery_supervision(_supervisor(tmp_path, goal, provider, (_orb_fact(goal),)))

    assert len(recording.operations) == 1
    assert recording.operations[0].get("create_attempted") is True
    assert recording.operations[0]["operation_id"] == pending.operation_id
    assert transport.posts == []
    stored = JoinedLaneStore(tmp_path).require(goal.lane_id)
    assert stored.provider.handle == expected_handle
    assert (stored.pending_operation is None) is (expected_handle is not None)


def _initial_result(
    pending: PendingProviderOperation,
    *,
    status: str,
    accepted: bool | None,
    consumed: bool | None,
    omit_process_token: bool = False,
) -> ProviderOperationResult:
    return ProviderOperationResult(
        operation_id=pending.operation_id,
        kind=pending.kind,
        lane_id=pending.lane_id,
        provider_handle=CREATED_HANDLE,
        provider_session_id=pending.provider_session_id,
        process_start_token=None if omit_process_token else pending.process_start_token,
        idempotency_key=pending.idempotency_key,
        payload_digest=pending.payload_digest,
        provider_instance_id=pending.provider_instance_id,
        provider_generation=pending.provider_generation,
        status=cast(Any, status),
        accepted=accepted,
        consumed=consumed,
        observed_at=datetime.now(UTC).isoformat(),
        evidence="raw initial-bind test result",
    )


@pytest.mark.parametrize(
    ("case", "status", "accepted", "consumed", "omit_process_token", "change_identity"),
    (
        ("unknown-raw-handle", "unknown", None, None, False, False),
        ("accepted-only", "accepted", True, None, False, False),
        ("rejected", "rejected", False, None, False, False),
        ("malformed-consumed", "consumed", True, True, True, False),
        ("provider-identity-change", "consumed", True, True, False, True),
    ),
)
def test_initial_bind_rejects_every_nonexact_raw_handle_disposition(
    tmp_path: Path,
    case: str,
    status: str,
    accepted: bool | None,
    consumed: bool | None,
    omit_process_token: bool,
    change_identity: bool,
) -> None:
    _goal_value, pending = _seed_attempted_create(tmp_path)
    previous = JoinedLaneStore(tmp_path).require(pending.lane_id)
    result = _initial_result(
        pending,
        status=status,
        accepted=accepted,
        consumed=consumed,
        omit_process_token=omit_process_token,
    )
    provider = previous.provider.model_copy(update={"handle": CREATED_HANDLE})
    if change_identity:
        provider = provider.model_copy(update={"instance_id": "changed-provider-instance"})
    current = previous.model_copy(
        update={
            "revision": previous.revision + 1,
            "provider": provider,
            "pending_operation": None,
            "last_operation_result": result,
        }
    )

    with pytest.raises(ContractValidationError):
        validate_record_transition(previous, current, transition="initial-bind")


def test_dispatch_waits_for_consumed_initial_bind_before_claiming_queue_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    goal = _goal(tmp_path)
    provider = _OrbLaunchProvider(tmp_path, lose_first_response=True)
    queue = tmp_path / "queue"
    orders = queue / "orders"
    orders.mkdir(parents=True)
    (orders / "queued.json").write_text("{}", encoding="utf-8")
    dispatched_with: list[str | None] = []

    def process_stub(*_args: object, **_kwargs: object) -> None:
        record = JoinedLaneStore(tmp_path).require(goal.lane_id)
        dispatched_with.append(record.provider.handle)

    monkeypatch.setattr(dispatchd, "process_one_order", process_stub)
    monkeypatch.setattr(dispatchd, "read_operating_facts", lambda _sources=None: ())
    monkeypatch.setattr(dispatchd, "bind_current_operating_facts", lambda _snapshot: None)

    supervisor = _supervisor(tmp_path, goal, provider, (_orb_fact(goal),))
    dispatchd.run_once(queue, joined_lane_root=tmp_path, recovery_supervisor=supervisor)
    interrupted = JoinedLaneStore(tmp_path).require(goal.lane_id)
    assert interrupted.provider.handle is None
    assert interrupted.pending_operation is not None
    dispatchd.run_once(queue, joined_lane_root=tmp_path, recovery_supervisor=supervisor)

    assert dispatched_with == [CREATED_HANDLE]


_PROCESS_WORKER = r"""
import fcntl, json, sys
from datetime import UTC, datetime
from pathlib import Path

project_root = Path(sys.argv[6])
sys.path[:0] = [str(project_root / "src"), str(project_root / "tests")]

from chitra.goals import load_goals
from chitra.joined_lane import JoinedLaneStore
from chitra.provider_protocol import ProviderName
from chitra.recovery import RecoverySupervisor, run_recovery_supervision
from chitra.session_contract import OperatingFact, ProviderCapabilities, ProviderOperationResult

root, facts_path, calls_path, marker_path = map(Path, sys.argv[1:5])
mode = sys.argv[5]
goal = load_goals(root)[0]
facts = tuple(OperatingFact.model_validate(value) for value in json.loads(facts_path.read_text()))

class Provider:
    provider_name = ProviderName.AMP
    capabilities = ProviderCapabilities.from_supported((
        "create_or_resume", "status", "send", "read_updates", "checkpoint", "usage",
        "cancel_current_turn", "close", "resume_after_close", "subagents", "parent_child_usage",
    ))

    def create_or_resume(self, request):
        operation = request.operation
        guard_path = marker_path.with_suffix(".lock")
        guard_path.parent.mkdir(parents=True, exist_ok=True)
        with guard_path.open("a+") as guard:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
            first = not marker_path.exists()
            material = operation.model_dump(mode="json")
            if first:
                marker_path.write_text(json.dumps(material, sort_keys=True))
            else:
                original = json.loads(marker_path.read_text())
                for field in ("operation_id", "idempotency_key", "payload_digest", "lane_id"):
                    if original[field] != material[field]:
                        raise AssertionError(f"restart changed {field}")
            with calls_path.open("a") as calls:
                calls.write(operation.operation_id + "\n")
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
        if first and mode == "lose-first-response":
            raise RuntimeError("simulated process exit after physical create")
        return ProviderOperationResult(
            operation_id=operation.operation_id,
            kind=operation.kind,
            lane_id=operation.lane_id,
            provider_handle="amp-created-thread-a",
            provider_session_id=operation.provider_session_id,
            process_start_token=operation.process_start_token,
            idempotency_key=operation.idempotency_key,
            payload_digest=operation.payload_digest,
            provider_instance_id=operation.provider_instance_id,
            provider_generation=operation.provider_generation,
            status="consumed",
            accepted=True,
            consumed=True,
            observed_at=datetime.now(UTC).isoformat(),
            evidence="separate-process exact adoption",
        )

provider = Provider()
supervisor = RecoverySupervisor(
    root,
    lambda record: provider if record.provider.kind == "amp" else None,
    goal_root=root,
    lane_id=goal.lane_id,
    identity_resolver=lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Amp process test used Tophand identity")
    ),
    operating_facts_reader=lambda: facts,
)
run_recovery_supervision(supervisor)
record = JoinedLaneStore(root).require(goal.lane_id)
print(json.dumps({"handle": record.provider.handle, "pending": record.pending_operation is not None}))
"""


def _process_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "state"
    root.mkdir()
    goal = _goal(root)
    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps([_orb_fact(goal).to_dict()]), encoding="utf-8")
    return root, facts_path, tmp_path / "provider-calls", tmp_path / "physical-create.json"


def _worker_command(
    root: Path,
    facts_path: Path,
    calls_path: Path,
    marker_path: Path,
    mode: str,
) -> list[str]:
    return [
        sys.executable,
        "-c",
        _PROCESS_WORKER,
        str(root),
        str(facts_path),
        str(calls_path),
        str(marker_path),
        mode,
        str(Path(__file__).parents[1]),
    ]


def test_separate_process_restart_adopts_the_same_create_without_repost(tmp_path: Path) -> None:
    root, facts_path, calls_path, marker_path = _process_inputs(tmp_path)
    first = subprocess.run(
        _worker_command(root, facts_path, calls_path, marker_path, "lose-first-response"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr + first.stdout
    interrupted = JoinedLaneStore(root).require(_goal(root).lane_id)
    assert interrupted.provider.handle is None
    assert interrupted.pending_operation is not None

    second = subprocess.run(
        _worker_command(root, facts_path, calls_path, marker_path, "adopt"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr + second.stdout
    assert len(calls_path.read_text(encoding="utf-8").splitlines()) == 2
    assert JoinedLaneStore(root).require(interrupted.lane_id).provider.handle == CREATED_HANDLE


def test_concurrent_separate_process_supervisors_issue_one_create(tmp_path: Path) -> None:
    root, facts_path, calls_path, marker_path = _process_inputs(tmp_path)
    command = _worker_command(root, facts_path, calls_path, marker_path, "consume")
    workers = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _index in range(2)]
    outputs = [worker.communicate(timeout=30) for worker in workers]
    for worker, (stdout, stderr) in zip(workers, outputs, strict=True):
        assert worker.returncode == 0, stderr + stdout
    assert len(calls_path.read_text(encoding="utf-8").splitlines()) == 1
    goal = _goal(root)
    assert JoinedLaneStore(root).require(goal.lane_id).provider.handle == CREATED_HANDLE


class _InitialAmpAdapter(_AmpAdapter):
    def create_or_resume(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append(("create_or_resume", request))
        operation = cast(dict[str, object], request["operation"])
        return {
            **operation,
            "provider_handle": CREATED_HANDLE,
            "provider_session_id": operation["provider_session_id"],
            "status": "consumed",
            "accepted": True,
            "consumed": True,
            "observed_at": datetime.now(UTC).isoformat(),
            "evidence": "signed production-facts create receipt",
        }


def test_handleless_production_entrypoint_requires_current_facts_and_signed_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_amp_fakes(monkeypatch)
    monkeypatch.setattr("chitra.recovery_provider._packaged_amp_adapter", _InitialAmpAdapter, raising=True)
    lane = _lane(tmp_path)
    goal = _goal(lane.state_dir, lane_id=lane.identifier)
    receipt_record = _record(lane)
    receipt_fact = _amp_facts(receipt_record)[0]
    receipt_surface = cast(dict[str, object], cast(dict[str, object], receipt_fact.value)["orb_lane_surface"])
    admission = _orb_fact(goal)
    admission_value = cast(dict[str, object], admission.value)
    admission_surface = cast(dict[str, object], admission_value["orb_lane_surface"])
    admission = admission.model_copy(
        update={
            "value": {
                **admission_value,
                "orb_lane_surface": {
                    **admission_surface,
                    "capability_probe": receipt_surface["capability_probe"],
                },
            }
        }
    )
    facts = (*_production_facts(receipt_record)[:-1], admission)
    facts_path = tmp_path / "operating-facts.json"
    _write_production_facts(facts_path, facts)
    monkeypatch.setattr("chitra.operating_facts.PRODUCTION_OPERATING_FACTS_PATH", facts_path, raising=True)
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

    dispatchd.run_lanes_once(
        manifest,
        amp_capability_verifier=hmac_capability_verifier(CAPABILITY_KEY),
    )

    stored = JoinedLaneStore(lane.state_dir).require(goal.lane_id)
    assert stored.provider.handle == CREATED_HANDLE
    assert _AmpTransport.calls
    transport_options = _AmpTransport.calls[0][1]
    assert isinstance(transport_options["capability_receipt_digest"], str)
    assert isinstance(transport_options["capability_receipt_expires_at"], str)

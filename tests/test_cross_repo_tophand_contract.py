"""Red cross-repository contract for the Chitra/Adapter/Fleet Tophand seam.

These tests deliberately load the source-side Fleet adapter from a separate
worktree.  They use an in-memory transport only.  No provider, host, service,
credential, or network action is allowed.

Set ``FLEET_ADAPTER_ROOT`` to a Fleet worktree when running the contract.  The
default is the current local Fleet candidate layout.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import sys
import types
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from _amp_capability_fixtures import hmac_capability_verifier, sign_amp_capability_receipt
from test_amp_capability import KEY as AMP_KEY
from test_amp_capability import _payload as capability_payload

from chitra.amp_capability import verify_amp_capability_receipt
from chitra.provider_protocol import CreateOrResumeRequest, SendRequest
from chitra.recovery_provider import _PackagedTophandProvider, _provider_result, _tophand_operation_dict
from chitra.session_contract import (
    ContractValidationError,
    OwnerProcessIdentity,
    PendingProviderOperation,
    ProviderOperationResult,
    validate_operation_result,
)
from chitra.tophand_wire import request_digest as chitra_request_digest
from chitra.tophand_wire import request_payload as chitra_request_payload

NOW = "2026-08-23T15:01:00+00:00"
FLEET_ROOT = Path(
    os.environ.get(
        "FLEET_ADAPTER_ROOT",
        "/Users/roundtop/chitra-autonomy/worktrees/adapter-facts-skills-source-20260824",
    )
)


def _adapter_root() -> Path:
    direct = FLEET_ROOT / "tools" / "support" / "chitra_adapter"
    if direct.is_dir():
        return direct
    packaged = (
        FLEET_ROOT
        / "packages"
        / "chitra-launcher"
        / "files"
        / "opt"
        / "polyphony"
        / "deploy-main"
        / "tools"
        / "support"
        / "chitra_adapter"
    )
    if packaged.is_dir():
        return packaged
    raise AssertionError(f"Fleet Adapter source is absent: {direct} or {packaged}")


@pytest.fixture(scope="module")
def fleet_modules() -> tuple[types.ModuleType, types.ModuleType]:
    """Load the real Fleet wire and adapter modules without package shadowing."""

    root = _adapter_root()
    package_name = "_cross_repo_fleet_chitra_adapter"
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    def load(name: str) -> types.ModuleType:
        module_name = f"{package_name}.{name}"
        spec = importlib.util.spec_from_file_location(module_name, root / f"{name}.py")
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load Fleet module {name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    return load("tophand_wire"), load("tophand_adapter")


CANONICAL_CREATE_REQUEST: dict[str, object] = {
    "session_ref": "tophand:lane-a:4",
    "provider_session_id": "physical-session-a",
    "context_ref": "completion-checkpoint",
    "goal_id": "goal-a",
    "goal_version": 3,
    "resume_after_close": True,
    "close_operation_id": "close-1",
    "owner_process": {
        "pid": 10,
        "uid": 1000,
        "gid": 1000,
        "start_token": "start-a",
        "comm": "chitra",
        "exe": "/usr/local/bin/chitra",
    },
    "resume_token": "resume-token",
}


def _send_operation() -> PendingProviderOperation:
    return PendingProviderOperation(
        operation_id="send-1",
        kind="send",
        lane_id="lane-a",
        provider_handle="thread-a",
        provider_session_id="physical-session-a",
        idempotency_key="idem-send-1",
        payload_digest=chitra_request_digest("send", {"text": "continue"}),
        provider_instance_id="instance-a",
        provider_generation=3,
        process_start_token="start-a",
        created_at=NOW,
        attempted=True,
    )


def _resume_operation() -> PendingProviderOperation:
    return PendingProviderOperation(
        operation_id="resume-1",
        kind="create_or_resume",
        lane_id="lane-a",
        provider_handle="thread-a",
        provider_session_id="physical-session-a",
        idempotency_key="idem-resume-1",
        payload_digest=chitra_request_digest(
            "create_or_resume", CANONICAL_CREATE_REQUEST
        ),
        payload=json.dumps(
            CANONICAL_CREATE_REQUEST,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        provider_instance_id="instance-a",
        provider_generation=3,
        process_start_token=None,
        created_at=NOW,
        attempted=True,
    )


class FakeTransport:
    """Transport fixture; it never reaches a provider."""

    def __init__(self, response: Mapping[str, object]) -> None:
        self.response = dict(response)
        self.calls: list[dict[str, object]] = []

    def _return(self, request: object) -> dict[str, object]:
        self.calls.append(cast(dict[str, object], request))
        return dict(self.response)

    def create_or_resume(self, request: object) -> dict[str, object]:
        return self._return(request)

    def send(self, request: object) -> dict[str, object]:
        return self._return(request)

    def checkpoint(self, request: object) -> dict[str, object]:
        return self._return(request)

    def cancel_current_turn(self, request: object) -> dict[str, object]:
        return self._return(request)

    def close(self, request: object) -> dict[str, object]:
        return self._return(request)

    def status(self) -> dict[str, object]:
        return {"AGENT_STATUS": {"state": "running"}}

    def read_updates(self, cursor: str | None = None) -> dict[str, object]:
        return {"updates": [], "next_cursor": cursor or ""}

    def usage(self) -> dict[str, object]:
        return {}


def _complete_result() -> dict[str, object]:
    operation = _send_operation()
    return {
        **_tophand_operation_dict(operation),
        "provider_session_id": operation.provider_session_id,
        "provider_instance_id": operation.provider_instance_id,
        "provider_generation": operation.provider_generation,
        "process_start_token": operation.process_start_token,
        "status": "consumed",
        "accepted": True,
        "consumed": True,
        "observed_at": NOW,
        "evidence": "synthetic transport observation",
    }


def _build_real_fleet_provider(
    fleet_adapter: types.ModuleType,
    transport: FakeTransport,
) -> object:
    """Call the real Fleet builder with the complete Chitra envelope."""

    try:
        return fleet_adapter.build_tophand_provider(
            lane_id="lane-a",
            goal_id="goal-a",
            session_ref=cast(str, CANONICAL_CREATE_REQUEST["session_ref"]),
            provider_session_id=cast(str, CANONICAL_CREATE_REQUEST["provider_session_id"]),
            provider_handle="thread-a",
            provider_instance_id="instance-a",
            provider_generation=3,
            process_start_token="start-a",
            transport=transport,
            result_sink=None,
        )
    except TypeError as exc:
        pytest.fail(f"real Fleet builder rejects the complete Chitra envelope: {exc}")


def test_nine_field_projection_and_digest_match_real_fleet_wire(
    fleet_modules: tuple[types.ModuleType, types.ModuleType],
) -> None:
    fleet_wire, _fleet_adapter = fleet_modules

    assert chitra_request_payload("create_or_resume", CANONICAL_CREATE_REQUEST) == CANONICAL_CREATE_REQUEST
    assert fleet_wire.request_payload("create_or_resume", CANONICAL_CREATE_REQUEST) == CANONICAL_CREATE_REQUEST
    assert fleet_wire.request_digest("create_or_resume", CANONICAL_CREATE_REQUEST) == chitra_request_digest(
        "create_or_resume", CANONICAL_CREATE_REQUEST
    )


@pytest.mark.parametrize(
    "field",
    (
        "provider_session_id",
        "goal_id",
        "goal_version",
        "resume_after_close",
        "close_operation_id",
        "owner_process",
        "resume_token",
    ),
)
def test_each_resume_identity_field_changes_the_canonical_digest(field: str) -> None:
    changed = dict(CANONICAL_CREATE_REQUEST)
    if field == "owner_process":
        changed[field] = {**cast(dict[str, object], CANONICAL_CREATE_REQUEST[field]), "start_token": "start-b"}
    elif field == "goal_version":
        changed[field] = 4
    elif field == "resume_after_close":
        changed[field] = False
    else:
        changed[field] = f"changed-{field}"
    assert chitra_request_digest("create_or_resume", changed) != chitra_request_digest(
        "create_or_resume", CANONICAL_CREATE_REQUEST
    )


def test_real_fleet_builder_accepts_the_complete_envelope(
    fleet_modules: tuple[types.ModuleType, types.ModuleType],
) -> None:
    _fleet_wire, fleet_adapter = fleet_modules
    provider = _build_real_fleet_provider(fleet_adapter, FakeTransport(_complete_result()))
    assert provider is not None


def test_fleet_rejects_a_result_that_omits_raw_physical_identity(
    fleet_modules: tuple[types.ModuleType, types.ModuleType],
) -> None:
    _fleet_wire, fleet_adapter = fleet_modules
    transport = FakeTransport({"status": "consumed", "accepted": True, "consumed": True})
    adapter = fleet_adapter.TophandAdapter(
        transport,
        lane_id="lane-a",
        goal_id="goal-a",
        session_ref="physical-session-a",
        provider_handle="thread-a",
        provider_instance_id="instance-a",
        provider_generation=3,
        result_sink=None,
    )

    with pytest.raises(fleet_adapter.TophandAdapterError):
        adapter.send({"operation": _tophand_operation_dict(_send_operation()), "text": "continue"})


def test_chitra_does_not_fill_unknown_result_identity_from_pending_operation() -> None:
    operation = _send_operation()
    raw = {
        **_tophand_operation_dict(operation),
        "status": "lost-response",
        "observed_at": NOW,
        "evidence": "synthetic response omitted physical identity",
    }
    raw.pop("provider_instance_id")
    raw.pop("provider_generation")
    raw.pop("process_start_token")

    result = _provider_result(raw, operation, provider_label="Tophand")

    assert result.provider_instance_id is None
    assert result.provider_generation is None
    assert result.process_start_token is None


def test_orb_result_digest_is_bound_to_material_result() -> None:
    material = b'{"child_id":"inline:child-test","status":"consumed"}'
    expected = "sha256:" + hashlib.sha256(material).hexdigest()
    receipt = sign_amp_capability_receipt(
        capability_payload(result_digest="sha256:" + "0" * 64),
        signature_key_id="fleet-key-1",
        key=AMP_KEY,
    )
    assert receipt["result_digest"] != expected

    verified = verify_amp_capability_receipt(
        receipt,
        expected_binary="/usr/local/bin/amp",
        expected_version="0.0.1787505256-gdf42f4",
        expected_project_ref="amp-project",
        expected_profile_digest="sha256:" + "a" * 64,
        expected_orb_size="a1.tiny",
        now=datetime.now(UTC),
        signature_verifier=hmac_capability_verifier(AMP_KEY),
    )
    assert verified is None


def test_orb_result_digest_rejects_an_arbitrary_nonzero_signed_digest() -> None:
    material = b'{"child_id":"inline:child-test","status":"consumed"}'
    expected = "sha256:" + hashlib.sha256(material).hexdigest()
    forged = "sha256:" + "1" * 64
    receipt = sign_amp_capability_receipt(
        capability_payload(result_digest=forged),
        signature_key_id="fleet-key-1",
        key=AMP_KEY,
    )
    assert forged != expected

    verified = verify_amp_capability_receipt(
        receipt,
        expected_binary="/usr/local/bin/amp",
        expected_version="0.0.1787505256-gdf42f4",
        expected_project_ref="amp-project",
        expected_profile_digest="sha256:" + "a" * 64,
        expected_orb_size="a1.tiny",
        now=datetime.now(UTC),
        signature_verifier=hmac_capability_verifier(AMP_KEY),
    )

    assert verified is None


def test_typed_consumed_result_requires_raw_physical_identity() -> None:
    operation = _send_operation()
    result = ProviderOperationResult(
        operation_id=operation.operation_id,
        kind=operation.kind,
        lane_id=operation.lane_id,
        provider_handle=operation.provider_handle,
        provider_session_id=None,
        process_start_token=None,
        idempotency_key=operation.idempotency_key,
        payload_digest=operation.payload_digest,
        provider_instance_id=None,
        provider_generation=None,
        status="consumed",
        accepted=True,
        consumed=True,
        observed_at=NOW,
        evidence="typed provider omitted raw physical identity",
    )

    with pytest.raises(ContractValidationError):
        validate_operation_result(operation, result)


def test_real_fleet_adapter_through_chitra_emits_one_result_sink_callback(
    fleet_modules: tuple[types.ModuleType, types.ModuleType],
) -> None:
    _fleet_wire, fleet_adapter = fleet_modules
    received: list[object] = []
    provider = _PackagedTophandProvider(
        _build_real_fleet_provider(fleet_adapter, FakeTransport(_complete_result())),
        result_sink=received.append,
    )
    result = provider.send(SendRequest(operation=_send_operation(), text="continue"))

    assert result.status == "consumed"
    assert len(received) == 1


def test_authenticated_resume_token_rotation_crosses_packaged_chitra_result_sink(
    fleet_modules: tuple[types.ModuleType, types.ModuleType],
) -> None:
    _fleet_wire, fleet_adapter = fleet_modules
    operation = _resume_operation()
    prior_owner = cast(dict[str, object], CANONICAL_CREATE_REQUEST["owner_process"])
    new_owner = {**prior_owner, "pid": 20, "start_token": "start-b"}
    receipt: dict[str, object] = {
        "schema": "chitra.lane-reopen.v1",
        "operation_id": operation.operation_id,
        "close_operation_id": CANONICAL_CREATE_REQUEST["close_operation_id"],
        "lane_id": operation.lane_id,
        "goal_id": CANONICAL_CREATE_REQUEST["goal_id"],
        "goal_version": CANONICAL_CREATE_REQUEST["goal_version"],
        "session_ref": CANONICAL_CREATE_REQUEST["session_ref"],
        "provider_session_id": operation.provider_session_id,
        "provider_handle": operation.provider_handle,
        "provider_instance_id": operation.provider_instance_id,
        "provider_generation": operation.provider_generation,
        "checkpoint_ref": CANONICAL_CREATE_REQUEST["context_ref"],
        "prior_owner_process": prior_owner,
        "owner_process": new_owner,
        "created_new_lane": False,
        "created_new_session": False,
        "auth_token": CANONICAL_CREATE_REQUEST["resume_token"],
        "observed_at": NOW,
        "evidence": "authenticated same-session reopen",
    }
    token = cast(str, CANONICAL_CREATE_REQUEST["resume_token"])
    receipt["receipt_hmac"] = hmac.new(
        token.encode(),
        json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    response = {
        **_tophand_operation_dict(operation),
        "provider_session_id": operation.provider_session_id,
        "provider_instance_id": operation.provider_instance_id,
        "provider_generation": operation.provider_generation,
        "process_start_token": new_owner["start_token"],
        "status": "consumed",
        "accepted": True,
        "consumed": True,
        "observed_at": NOW,
        "evidence": "authenticated same-session reopen",
        "reopen_receipt": receipt,
    }
    received: list[object] = []
    provider = _PackagedTophandProvider(
        _build_real_fleet_provider(fleet_adapter, FakeTransport(response)),
        result_sink=received.append,
    )
    result = provider.create_or_resume(
        CreateOrResumeRequest(
            operation=operation,
            session_ref=cast(str, CANONICAL_CREATE_REQUEST["session_ref"]),
            provider_session_id=cast(
                str, CANONICAL_CREATE_REQUEST["provider_session_id"]
            ),
            context_ref=cast(str, CANONICAL_CREATE_REQUEST["context_ref"]),
            goal_id=cast(str, CANONICAL_CREATE_REQUEST["goal_id"]),
            goal_version=cast(int, CANONICAL_CREATE_REQUEST["goal_version"]),
            resume_after_close=True,
            close_operation_id=cast(
                str, CANONICAL_CREATE_REQUEST["close_operation_id"]
            ),
            owner_process=OwnerProcessIdentity.model_validate(prior_owner, strict=True),
            resume_token=token,
        )
    )

    assert result.status == "consumed"
    assert result.process_start_token == "start-b"
    assert result.reopen_receipt is not None
    assert result.reopen_receipt.owner_process.start_token == "start-b"
    assert len(received) == 1

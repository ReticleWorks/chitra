from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from chitra.governed_close import _close_payload, _write_checkpoint
from chitra.provider_protocol import CloseRequest
from chitra.recovery import RecoveryEngine
from chitra.recovery_provider import _PackagedTophandProvider
from chitra.session_contract import JoinedLaneRecord, ProviderCapabilities, ProviderIdentity


def _record() -> JoinedLaneRecord:
    return JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-a",
        goal_version=1,
        session_ref="logical-session-a",
        provider=ProviderIdentity(
            kind="tophand",
            handle="thread-a",
            provider_session_id="tophand:lane-a:0.0",
            instance_id="instance-a",
            generation=1,
            capabilities=ProviderCapabilities(status=True, checkpoint=True, close=True),
        ),
    )


class FakeAdapter:
    capabilities = {"close": True}

    def __init__(self, state: str = "closed") -> None:
        self.requests: list[dict[str, object]] = []
        self.state = state

    def close(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        operation = request["operation"]
        assert isinstance(operation, dict)
        return {
            **operation,
            "status": "consumed" if self.state == "closed" else "unknown",
            "accepted": True if self.state == "closed" else None,
            "consumed": True if self.state == "closed" else None,
            "state": self.state,
            "provider_thread_ref": "thread-a",
            "provider_session_id": "tophand:lane-a:0.0",
            "same_provider_thread": True if self.state == "closed" else None,
            "later_resume_supported": False,
            "checkpoint_ref": request.get("checkpoint_ref"),
            "quiescent": True if self.state == "closed" else None,
            "observed_at": "2026-08-23T14:00:01+00:00",
            "evidence": "fake Tophand close",
        }


def _request(root: Path, record: JoinedLaneRecord) -> CloseRequest:
    reference = _write_checkpoint(root, record)
    bound = record.model_copy(update={"checkpoint_reference": reference})
    operation = RecoveryEngine()._operation(
        bound, "close", _close_payload(bound), datetime(2026, 8, 23, 14, tzinfo=UTC)
    )
    receipt = json.loads((root / "checkpoints" / f"{reference}.json").read_text(encoding="utf-8"))
    digest = __import__("hashlib").sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return CloseRequest(
        operation=operation,
        archive=True,
        checkpoint_receipt=receipt,
        checkpoint_receipt_sha256=digest,
        checkpoint_verifier="chitra.detect.rescue.verify_checkpoint_receipt_signature",
    )


def test_facade_verifies_signed_checkpoint_and_projects_wire_request(tmp_path: Path) -> None:
    record = _record()
    request = _request(tmp_path, record)
    adapter = FakeAdapter()
    provider = _PackagedTophandProvider(adapter, state_root=tmp_path, result_sink=lambda _value: None)

    result = provider.close(request)

    assert isinstance(result, dict)
    assert result["state"] == "closed"
    assert len(adapter.requests) == 1
    wire = adapter.requests[0]
    assert wire["goal_id"] == record.goal_id
    assert wire["session_ref"] == record.session_ref
    assert wire["provider_session_id"] == record.provider.provider_session_id
    assert wire["checkpoint_ref"] == json.loads(request.operation.payload)["checkpoint_ref"]
    receipt = wire["checkpoint_receipt"]
    assert isinstance(receipt, dict)
    assert receipt["schema_name"] == "chitra.governed-close-checkpoint.v1"
    assert wire["checkpoint_verifier"] == "chitra.detect.rescue.verify_checkpoint_receipt_signature"


def test_facade_uses_the_explicit_checkpoint_even_if_tophand_path_changes(tmp_path: Path) -> None:
    record = _record()
    request = _request(tmp_path, record)
    reference = json.loads(request.operation.payload)["checkpoint_ref"]
    path = tmp_path / "checkpoints" / f"{reference}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["goal_id"] = "other-goal"
    path.write_text(json.dumps(payload), encoding="utf-8")

    adapter = FakeAdapter()
    provider = _PackagedTophandProvider(adapter, state_root=tmp_path, result_sink=lambda _value: None)
    result = provider.close(request)

    assert isinstance(result, dict)
    assert result["state"] == "closed"
    assert len(adapter.requests) == 1


def test_packaged_tophand_exposes_close_and_explicit_resume_capabilities(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    adapter.capabilities = {
        "create_or_resume": True,
        "close": True,
        "resume_after_close": True,
    }
    provider = _PackagedTophandProvider(adapter, state_root=tmp_path, result_sink=lambda _value: None)

    assert provider.capabilities.close is True
    assert provider.capabilities.create_or_resume is True
    assert provider.capabilities.resume_after_close is True

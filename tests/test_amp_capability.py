"""Tests for the fail-closed Fleet capability receipt gate."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from _amp_capability_fixtures import hmac_capability_verifier, sign_amp_capability_receipt

from chitra.amp_capability import verify_amp_capability_receipt

KEY = b"amp-capability-test-key"
NOW = datetime.now(UTC)
RESULT_MATERIAL = '{"child_id":"inline:child-test","status":"consumed"}'


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "chitra.amp-capability-probe.v1",
        "probe_id": "probe-test",
        "operation_id": "capability-probe:probe-test",
        "lane_id": "capability-probe:probe-test",
        "goal_id": "chitra-amp-capability-probe",
        "goal_version": 1,
        "session_ref": "chitra:amp-capability-probe:probe-test",
        "amp_binary": "/usr/local/bin/amp",
        "amp_version": "0.0.1787505256-gdf42f4",
        "project_ref": "amp-project",
        "profile_digest": "sha256:" + "a" * 64,
        "orb_size": "a1.tiny",
        "visibility": "private",
        "root_thread_id": "T-11111111-1111-4111-8111-111111111111",
        "child_id": "inline:child-test",
        "child_evidence_mode": "inline",
        "transcript_cursor": "amp:T-11111111-1111-4111-8111-111111111111:offset:1:boundary:M:prefix:" + "a" * 64,
        "usage_evidence_hash": "sha256:" + "b" * 64,
        "result_digest": "sha256:" + hashlib.sha256(RESULT_MATERIAL.encode("utf-8")).hexdigest(),
        "result_material": RESULT_MATERIAL,
        "containment_proof": {
            "schema": "chitra.amp-linux-containment.v1",
            "platform": "linux",
            "address_space_limit_bytes": 2 * 1024 * 1024 * 1024,
            "process_group_killed": True,
            "escaped_descendant_killed": True,
        },
        "created_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=59)).isoformat(),
    }
    payload.update(changes)
    return payload


def _verify(receipt: object, *, now: datetime = NOW):
    return verify_amp_capability_receipt(
        receipt,
        expected_binary="/usr/local/bin/amp",
        expected_version="0.0.1787505256-gdf42f4",
        expected_project_ref="amp-project",
        expected_profile_digest="sha256:" + "a" * 64,
        expected_orb_size="a1.tiny",
        now=now,
        signature_verifier=hmac_capability_verifier(KEY),
    )


def test_signed_receipt_verifies_and_projects_digest_deadline() -> None:
    receipt = sign_amp_capability_receipt(_payload(), signature_key_id="fleet-key-1", key=KEY)

    verified = _verify(receipt)

    assert verified is not None
    assert verified.digest == receipt["digest"]
    assert verified.expires_at == receipt["expires_at"]


def test_receipt_gate_rejects_stale_version_drift_tampering_and_missing_verifier() -> None:
    receipt = sign_amp_capability_receipt(_payload(), signature_key_id="fleet-key-1", key=KEY)
    stale = sign_amp_capability_receipt(
        _payload(
            created_at=(NOW - timedelta(hours=2)).isoformat(),
            expires_at=(NOW - timedelta(hours=1)).isoformat(),
        ),
        signature_key_id="fleet-key-1",
        key=KEY,
    )
    tampered = {**receipt, "result_digest": "sha256:" + "d" * 64}

    assert _verify(stale) is None
    assert _verify(receipt, now=NOW + timedelta(seconds=1)) is not None
    assert verify_amp_capability_receipt(
        receipt,
        expected_binary="/usr/local/bin/amp",
        expected_version="different",
        expected_project_ref="amp-project",
        expected_profile_digest="sha256:" + "a" * 64,
        expected_orb_size="a1.tiny",
        now=NOW,
        signature_verifier=hmac_capability_verifier(KEY),
    ) is None
    assert _verify(tampered) is None
    assert verify_amp_capability_receipt(
        receipt,
        expected_binary="/usr/local/bin/amp",
        expected_version="0.0.1787505256-gdf42f4",
        expected_project_ref="amp-project",
        expected_profile_digest="sha256:" + "a" * 64,
        expected_orb_size="a1.tiny",
        now=NOW,
        signature_verifier=None,
    ) is None


def test_receipt_gate_rejects_unproven_linux_containment() -> None:
    payload = _payload(
        containment_proof={
            "schema": "chitra.amp-linux-containment.v1",
            "platform": "linux",
            "address_space_limit_bytes": 0,
            "process_group_killed": True,
            "escaped_descendant_killed": True,
        }
    )
    receipt = sign_amp_capability_receipt(payload, signature_key_id="fleet-key-1", key=KEY)

    assert _verify(receipt) is None

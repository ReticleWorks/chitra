"""Fail-closed verification for Fleet's disposable Amp capability probe.

The probe is the only path that may establish that the pinned Amp build can
retain an inline child result.  Chitra only verifies a receipt published in
the authoritative operating-facts snapshot.  It does not run the probe, keep
mutable capability state, or choose a signing key.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

CAPABILITY_PROBE_SCHEMA = "chitra.amp-capability-probe.v1"
LINUX_CONTAINMENT_SCHEMA = "chitra.amp-linux-containment.v1"
CAPABILITY_PROBE_GOAL = "chitra-amp-capability-probe"
MAX_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PROBE_LIFETIME = timedelta(hours=1)

CapabilitySignatureVerifier = Callable[[bytes, str, str], bool]
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_THREAD_ID = re.compile(r"^T-[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
_BOUND_CURSOR = re.compile(
    r"^amp:(?P<thread>T-[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r":offset:[0-9]+:boundary:[^:]+:prefix:[0-9a-f]{64}$",
    re.I,
)
_TEXT_FIELDS = (
    "probe_id",
    "operation_id",
    "lane_id",
    "session_ref",
    "amp_binary",
    "amp_version",
    "project_ref",
    "profile_digest",
    "root_thread_id",
    "child_id",
    "transcript_cursor",
    "usage_evidence_hash",
    "result_digest",
    "created_at",
    "expires_at",
    "signature_key_id",
    "digest",
    "signature",
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        *_TEXT_FIELDS,
        "goal_id",
        "goal_version",
        "orb_size",
        "visibility",
        "child_evidence_mode",
        "containment_proof",
    }
)
_PROOF_FIELDS = frozenset(
    {
        "schema",
        "platform",
        "address_space_limit_bytes",
        "process_group_killed",
        "escaped_descendant_killed",
    }
)


class AmpCapabilityError(ValueError):
    """A capability receipt is absent, stale, malformed, or unverifiable."""


@dataclass(frozen=True, slots=True)
class AmpCapabilityReceipt:
    """The small verified projection used to construct one Amp transport."""

    value: dict[str, Any]
    digest: str
    expires_at: str


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def capability_receipt_digest(value: Mapping[str, object]) -> str:
    """Hash the signed receipt payload, excluding its digest and signature."""

    unsigned = {key: item for key, item in value.items() if key not in {"digest", "signature"}}
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AmpCapabilityError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AmpCapabilityError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise AmpCapabilityError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AmpCapabilityError(f"{field} must be non-empty text")
    return value


def _validate_containment(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROOF_FIELDS:
        raise AmpCapabilityError("Linux containment proof is incomplete")
    proof = dict(value)
    if proof["schema"] != LINUX_CONTAINMENT_SCHEMA or proof["platform"] != "linux":
        raise AmpCapabilityError("Linux containment proof schema is not pinned")
    limit = proof["address_space_limit_bytes"]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > MAX_ADDRESS_SPACE_BYTES:
        raise AmpCapabilityError("Linux address-space limit is outside the hard bound")
    if proof["process_group_killed"] is not True or proof["escaped_descendant_killed"] is not True:
        raise AmpCapabilityError("Linux descendant containment was not proven")
    return proof


def verify_amp_capability_receipt(
    value: object,
    *,
    expected_binary: str,
    expected_version: str,
    expected_project_ref: str,
    expected_profile_digest: str,
    expected_orb_size: str,
    now: datetime | None = None,
    signature_verifier: CapabilitySignatureVerifier | None,
) -> AmpCapabilityReceipt | None:
    """Verify one Fleet-published receipt and return only its safe projection.

    ``None`` is intentional.  Provider construction must remain unavailable
    when Fleet has not injected the verifier or any receipt field is wrong.
    """

    try:
        if not isinstance(value, Mapping) or set(value) != _RECEIPT_FIELDS:
            raise AmpCapabilityError("capability receipt has unexpected or missing fields")
        receipt = dict(value)
        if receipt.get("schema") != CAPABILITY_PROBE_SCHEMA:
            raise AmpCapabilityError("capability receipt schema is not pinned")
        for field in _TEXT_FIELDS:
            _require_text(receipt.get(field), field)
        if receipt.get("goal_id") != CAPABILITY_PROBE_GOAL or receipt.get("goal_version") != 1:
            raise AmpCapabilityError("capability probe goal changed")
        if receipt.get("orb_size") != expected_orb_size or receipt.get("visibility") != "private":
            raise AmpCapabilityError("capability probe ORB launch policy changed")
        if receipt.get("child_evidence_mode") != "inline":
            raise AmpCapabilityError("capability probe did not prove inline child evidence")
        if not _THREAD_ID.fullmatch(receipt["root_thread_id"]):
            raise AmpCapabilityError("capability probe root thread identity is malformed")
        cursor_match = _BOUND_CURSOR.fullmatch(receipt["transcript_cursor"])
        if cursor_match is None or cursor_match.group("thread") != receipt["root_thread_id"]:
            raise AmpCapabilityError("capability probe cursor is not bound to the root thread")
        if receipt.get("operation_id") != f"capability-probe:{receipt['probe_id']}":
            raise AmpCapabilityError("capability probe operation identity changed")
        if receipt.get("lane_id") != f"capability-probe:{receipt['probe_id']}":
            raise AmpCapabilityError("capability probe lane identity changed")
        if receipt.get("session_ref") != f"chitra:amp-capability-probe:{receipt['probe_id']}":
            raise AmpCapabilityError("capability probe session identity changed")
        expected_values = {
            "amp_binary": expected_binary,
            "amp_version": expected_version,
            "project_ref": expected_project_ref,
            "profile_digest": expected_profile_digest,
        }
        if any(receipt.get(field) != expected for field, expected in expected_values.items()):
            raise AmpCapabilityError("capability receipt is bound to a different Amp build or profile")
        if not _SHA256.fullmatch(receipt["usage_evidence_hash"]):
            raise AmpCapabilityError("usage evidence hash is not a digest")
        if not _SHA256.fullmatch(receipt["result_digest"]):
            raise AmpCapabilityError("result digest is not a digest")
        # A signer cannot turn an omitted ORB result into evidence by signing
        # a sentinel digest.  The provider must publish a non-sentinel digest
        # for the result bytes it claims to have observed.
        if receipt["result_digest"] == "sha256:" + "0" * 64:
            raise AmpCapabilityError("result digest is an unbound sentinel")
        _validate_containment(receipt["containment_proof"])
        created = _timestamp(receipt["created_at"], "created_at")
        expires = _timestamp(receipt["expires_at"], "expires_at")
        if expires <= created or expires - created > MAX_PROBE_LIFETIME:
            raise AmpCapabilityError("capability receipt lifetime is outside the probe bound")
        current = datetime.now(UTC) if now is None else now
        if current.tzinfo is None:
            raise AmpCapabilityError("now must be timezone-aware")
        current = current.astimezone(UTC)
        if created > current or expires < current:
            raise AmpCapabilityError("capability receipt is stale or from the future")
        expected_digest = capability_receipt_digest(receipt)
        if receipt["digest"] != expected_digest:
            raise AmpCapabilityError("capability receipt digest does not match its payload")
        signature_valid = False
        if signature_verifier is not None:
            try:
                signature_valid = signature_verifier(
                    _canonical({**{key: item for key, item in receipt.items() if key != "signature"}}),
                    receipt["signature_key_id"],
                    receipt["signature"],
                )
            except Exception:
                signature_valid = False
        if signature_valid is not True:
            raise AmpCapabilityError("capability receipt signature is not verified")
        return AmpCapabilityReceipt(value=receipt, digest=expected_digest, expires_at=receipt["expires_at"])
    except (AmpCapabilityError, TypeError, KeyError):
        return None


__all__ = [
    "AmpCapabilityError",
    "AmpCapabilityReceipt",
    "CapabilitySignatureVerifier",
    "CAPABILITY_PROBE_GOAL",
    "CAPABILITY_PROBE_SCHEMA",
    "LINUX_CONTAINMENT_SCHEMA",
    "MAX_ADDRESS_SPACE_BYTES",
    "capability_receipt_digest",
    "verify_amp_capability_receipt",
]

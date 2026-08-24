"""Test-only signing helpers for synthetic Fleet capability receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_amp_capability_receipt(
    payload: Mapping[str, object],
    *,
    signature_key_id: str,
    key: bytes,
) -> dict[str, object]:
    if not isinstance(signature_key_id, str) or not signature_key_id.strip():
        raise ValueError("signature key id is required")
    unsigned = {**dict(payload), "signature_key_id": signature_key_id}
    if "digest" in unsigned or "signature" in unsigned:
        raise ValueError("receipt payload must be unsigned")
    digest = "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()
    signed = {**unsigned, "digest": digest}
    signature = hmac.new(key, _canonical(signed), hashlib.sha256).hexdigest()
    return {**signed, "signature": signature}


def hmac_capability_verifier(key: bytes) -> Callable[[bytes, str, str], bool]:
    if not isinstance(key, bytes) or not key:
        raise ValueError("capability verification key must be non-empty bytes")

    def verify(payload: bytes, key_id: str, signature: str) -> bool:
        del key_id
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    return verify

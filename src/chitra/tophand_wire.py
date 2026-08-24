"""Small, explicit wire contract shared with the Fleet Tophand adapter.

The operation envelope is Chitra-owned, but the payload digest covers only the
request fields that the adapter sends to Fleet.  Control fields such as the
operation envelope and Chitra's durable payload copy are never part of that
digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

TOPHAND_OPERATION_SCHEMA = "chitra.tophand.operation.v1"

_REQUEST_FIELDS: dict[str, tuple[str, ...]] = {
    "create_or_resume": (
        "session_ref",
        "provider_session_id",
        "context_ref",
        "goal_id",
        "goal_version",
        "resume_after_close",
        "close_operation_id",
        "owner_process",
        "resume_token",
    ),
    "send": ("text",),
    "checkpoint": ("label",),
    "cancel_current_turn": ("reason",),
    "close": ("archive",),
}


def request_payload(kind: str, raw: Mapping[str, object]) -> dict[str, object]:
    """Project one adapter request to its exact transport payload."""

    try:
        fields = _REQUEST_FIELDS[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported Tophand request kind: {kind}") from exc
    return {field: raw[field] for field in fields if field in raw}


def request_digest(kind: str, raw: Mapping[str, object]) -> str:
    """Return the canonical SHA-256 digest for one projected request."""

    payload = request_payload(kind, raw)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["TOPHAND_OPERATION_SCHEMA", "request_digest", "request_payload"]

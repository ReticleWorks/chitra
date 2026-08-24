"""Small, restart-safe close seam owned by Chitra.

The provider sees a close request and an opaque checkpoint reference. Chitra
keeps the signed checkpoint and its signing key on its own host. The provider
never reads Chitra's checkpoint file or key; it only returns evidence bound to
the exact operation and physical session supplied in the request.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ._fsio import write_json_atomic
from .detect.rescue import load_or_create_checkpoint_key, sign_checkpoint_receipt, verify_checkpoint_receipt_signature
from .provider_protocol import Provider
from .recovery import (
    GovernedCloseDecision,
    RecoveryEngine,
    RecoveryStateError,
    RecoveryStateStore,
)
from .session_contract import (
    JoinedLaneRecord,
)

_CHECKPOINT_SCHEMA = "chitra.governed-close-checkpoint.v1"
_CHECKPOINT_PROVENANCE = "governed-completion-checkpoint"
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _checkpoint_path(state_root: Path, reference: str) -> Path | None:
    if _REFERENCE_RE.fullmatch(reference) is None:
        return None
    root = state_root.resolve()
    directory = (root / "checkpoints").resolve()
    path = (directory / f"{reference}.json").resolve()
    return path if directory.parent == root and path.parent == directory else None


def _checkpoint_binding(record: JoinedLaneRecord) -> dict[str, object]:
    provider = record.provider
    if provider.instance_id is None or provider.generation is None:
        raise RecoveryStateError("governed close requires a complete provider identity")
    if not provider.provider_session_id:
        raise RecoveryStateError("governed close requires an exact physical provider session ID")
    return {
        "kind": str(provider.kind),
        "handle": provider.handle,
        "provider_session_id": provider.provider_session_id,
        "instance_id": provider.instance_id,
        "generation": provider.generation,
    }


def _checkpoint_payload(record: JoinedLaneRecord, reference: str) -> dict[str, object]:
    return {
        "schema_name": _CHECKPOINT_SCHEMA,
        "schema_version": 1,
        "checkpoint_ref": reference,
        "lane": record.lane_id,
        "goal_id": record.goal_id,
        "goal_version": record.goal_version,
        "session_ref": record.session_ref,
        "provider_binding": _checkpoint_binding(record),
        "provenance": {"kind": _CHECKPOINT_PROVENANCE, "owner": "chitra"},
    }


def _checkpoint_matches(
    payload: Mapping[str, object], record: JoinedLaneRecord, reference: str, state_root: Path
) -> bool:
    try:
        if not verify_checkpoint_receipt_signature(dict(payload), state_root=state_root):
            return False
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    if payload.get("checkpoint_ref") != reference:
        return False
    if payload.get("lane") != record.lane_id or payload.get("goal_id") not in (None, record.goal_id):
        return False
    if payload.get("goal_version") != record.goal_version or payload.get("session_ref") != record.session_ref:
        return False
    binding = payload.get("provider_binding")
    if isinstance(binding, Mapping):
        return dict(binding) == _checkpoint_binding(record)
    # Accept an older Chitra rescue receipt, but never infer identity from a
    # provider handle. Its binding is checked field by field below.
    recovery = payload.get("recovery_binding")
    expected = _checkpoint_binding(record)
    return (
        isinstance(recovery, Mapping)
        and recovery.get("goal_id") == record.goal_id
        and recovery.get("goal_version") == record.goal_version
        and all(
            recovery.get(source) == expected[target]
            for source, target in (
                ("provider_handle", "handle"),
                ("provider_session_id", "provider_session_id"),
                ("provider_instance_id", "instance_id"),
                ("provider_generation", "generation"),
            )
        )
    )


def _read_checkpoint_payload(
    state_root: Path, record: JoinedLaneRecord, reference: str
) -> dict[str, object] | None:
    path = _checkpoint_path(state_root, reference)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    # Keep the state-root argument explicit. It prevents a provider or a
    # remote host from ever becoming the source of checkpoint verification.
    if not _checkpoint_matches(payload, record, reference, state_root):
        return None
    return dict(payload)


def _read_checkpoint(state_root: Path, record: JoinedLaneRecord, reference: str) -> bool:
    return _read_checkpoint_payload(state_root, record, reference) is not None


def _write_checkpoint(state_root: Path, record: JoinedLaneRecord) -> str:
    unsigned = _checkpoint_payload(record, "")
    reference = "completion-" + _digest(unsigned)[:32]
    payload = _checkpoint_payload(record, reference)
    path = _checkpoint_path(state_root, reference)
    if path is None:
        raise RecoveryStateError("governed close generated an unsafe checkpoint reference")
    if path.exists():
        if not _read_checkpoint(state_root, record, reference):
            raise RecoveryStateError("existing completion checkpoint binding changed")
        return reference
    signed = dict(payload)
    signed["signature"] = sign_checkpoint_receipt(signed, key=load_or_create_checkpoint_key(state_root))
    write_json_atomic(path, signed, fsync=True)
    if not _read_checkpoint(state_root, record, reference):
        raise RecoveryStateError("new completion checkpoint failed local verification")
    return reference


def _ensure_checkpoint(state_root: Path, record: JoinedLaneRecord) -> JoinedLaneRecord:
    reference = record.checkpoint_reference
    if reference:
        if not _read_checkpoint(state_root, record, reference):
            raise RecoveryStateError("existing Chitra checkpoint is missing or not bound to this lane")
        return record
    return record.model_copy(update={"checkpoint_reference": _write_checkpoint(state_root, record)})


def _close_payload(record: JoinedLaneRecord) -> str:
    return _json(
        {
            "archive": True,
            "checkpoint_ref": record.checkpoint_reference,
            "lane_id": record.lane_id,
            "goal_id": record.goal_id,
            "goal_version": record.goal_version,
            "session_ref": record.session_ref,
            "provider_handle": record.provider.handle,
            "provider_session_id": record.provider.provider_session_id,
            "provider_instance_id": record.provider.instance_id,
            "provider_generation": record.provider.generation,
        }
    )


def governed_close(
    *,
    provider: Provider | None,
    state_root: Path | None,
    goal_root: Path | None = None,
    record: JoinedLaneRecord | None = None,
    lane_id: str | None = None,
    now: datetime | None = None,
    persist: bool = True,
    state_store: RecoveryStateStore | None = None,
) -> GovernedCloseDecision:
    """Compatibility facade; RecoveryEngine owns close state transitions."""

    return RecoveryEngine(
        provider=provider,
        state_root=state_root,
        state_store=state_store,
        goal_root=goal_root,
    ).governed_close(record, lane_id=lane_id, now=now, persist=persist)


__all__ = ["governed_close"]

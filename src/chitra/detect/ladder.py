"""Incident store and the nudge → redirect → RESCUE → relaunch ladder.

The store is durable, append-only per lane, and keyed by
``(lane, finding fingerprint)``. The ladder advances only when the *same*
fingerprint recurs after proven consumption of the prior stage's order —
a signed delivery-ledger entry plus a bound user event and turn boundary in
the journal (the PR #93 receipt semantics). Elapsed time never establishes
or advances anything.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from chitra.journal.models import CanonicalEvent, CanonicalType
from chitra.ledger import LedgerEntry, message_hash, verify_entry

from .detectors import Finding

LADDER_STAGES: tuple[str, ...] = ("nudge", "redirect", "rescue", "relaunch")

CONSUMED_CHECKPOINT_SCHEMA = "chitra.detect.consumed-checkpoint.v1"

_LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_PROCESS_IDENTITY_KEYS = ("target_pid", "target_uid", "target_gid", "target_start_time", "target_comm", "target_exe")

IncidentStage = Literal["nudge", "redirect", "rescue", "relaunch"]


class ConsumptionProof(BaseModel):
    """Receipt that an issued ladder order was actually consumed.

    ``ledger_entry`` is the signed delivery-ledger row proving the text was
    pasted; ``user_event_id`` names the journal user-turn event whose payload
    hash binds the same marker text, and ``turn_event_id`` names the next
    FINAL_RESPONSE boundary after it.
    """

    model_config = ConfigDict(frozen=True)

    ledger_entry: LedgerEntry
    ledger_key_hex: str = ""
    session_ref: str = ""
    native_session_id: str = ""
    user_event_id: str
    turn_event_id: str


class IncidentRecord(BaseModel):
    """One durable incident keyed ``(lane, finding fingerprint)``."""

    model_config = ConfigDict(frozen=True)

    lane: str
    fingerprint: str
    detector: str
    stage: IncidentStage
    order_marker: str
    opened_at: str
    event_refs: tuple[str, ...]
    unmet_item: str
    expected_next_progress: str
    detail: str
    consumption: ConsumptionProof | None = None
    rescue_bundle_sha256: str = ""
    checkpoint_ref: str = ""


class LadderDecision(BaseModel):
    """Outcome of advancing one finding through the ladder."""

    model_config = ConfigDict(frozen=True)

    action: Literal["open", "hold", "advance"]
    stage: IncidentStage
    record: IncidentRecord
    reason: str


class IncidentStore:
    """Append-only per-lane incident log under ``<state_root>/incidents``."""

    def __init__(self, state_root: Path, lane: str) -> None:
        if _LANE_RE.fullmatch(lane) is None:
            raise ValueError(f"unsafe lane name: {lane!r}")
        self.lane = lane
        self.state_root = state_root
        self.directory = state_root / "incidents"
        self.path = self.directory / f"{lane}.jsonl"
        self.lock_path = self.directory / f"{lane}.lock"

    def load(self) -> list[IncidentRecord]:
        if not self.path.exists():
            return []
        records: list[IncidentRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    records.append(IncidentRecord.model_validate_json(line))
                except ValueError as exc:
                    raise ValueError(f"invalid incident row {self.path}:{line_number}: {exc}") from exc
        return records

    def latest(self, fingerprint: str) -> IncidentRecord | None:
        for record in reversed(self.load()):
            if record.fingerprint == fingerprint:
                return record
        return None

    def _append(self, record: IncidentRecord) -> IncidentRecord:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return self._append_locked(record)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _append_locked(self, record: IncidentRecord) -> IncidentRecord:
        """Durably append one record. The caller must hold ``lock_path``."""
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            encoded = (record.model_dump_json() + "\n").encode()
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return record

    def open_incident(self, *, lane: str, finding: Finding, order_marker: str) -> IncidentRecord:
        record = IncidentRecord(
            lane=lane,
            fingerprint=finding.fingerprint,
            detector=finding.detector,
            stage="nudge",
            order_marker=order_marker,
            opened_at=_utc_now(),
            event_refs=finding.event_refs,
            unmet_item=finding.unmet_item,
            expected_next_progress=finding.expected_next_progress,
            detail=finding.detail,
        )
        return self._append(record)

    def attach_consumption(
        self,
        *,
        fingerprint: str,
        order_marker: str,
        proof: ConsumptionProof,
    ) -> IncidentRecord:
        """Bind proven consumption to the newest open record for a fingerprint."""
        records = self.load()
        target = next(
            (record for record in reversed(records) if record.fingerprint == fingerprint and record.order_marker == order_marker),
            None,
        )
        if target is None:
            raise KeyError(f"no open incident for fingerprint {fingerprint!r} at marker {order_marker!r}")
        updated = target.model_copy(update={"consumption": proof})
        return self._append(updated)

    def advance(self, *, fingerprint: str) -> IncidentRecord:
        raise TypeError("advance requires next_order_marker")

    def advance_to_next_stage(self, *, fingerprint: str, next_order_marker: str) -> IncidentRecord:
        """Move the newest consumed record for a fingerprint to its next stage."""
        records = self.load()
        target = next((record for record in reversed(records) if record.fingerprint == fingerprint), None)
        if target is None:
            raise KeyError(f"no incident for fingerprint {fingerprint!r}")
        if target.consumption is None:
            raise ValueError("ladder cannot advance without proven consumption")
        index = LADDER_STAGES.index(target.stage)
        if index >= len(LADDER_STAGES) - 1:
            raise ValueError("incident already reached relaunch")
        advanced = target.model_copy(
            update={
                "stage": LADDER_STAGES[index + 1],
                "order_marker": next_order_marker,
                "opened_at": _utc_now(),
                "consumption": None,
                "rescue_bundle_sha256": "",
                "checkpoint_ref": "",
            }
        )
        return self._append(advanced)

    def seal_rescue_checkpoint(
        self, *, fingerprint: str, order_marker: str, bundle_sha256: str, checkpoint_ref: str
    ) -> IncidentRecord:
        if _HEX64_RE.fullmatch(bundle_sha256) is None or _SAFE_REF_RE.fullmatch(checkpoint_ref) is None:
            raise ValueError("rescue bundle hash and checkpoint reference are required")
        records = self.load()
        target = next(
            (record for record in reversed(records) if record.fingerprint == fingerprint and record.order_marker == order_marker),
            None,
        )
        if target is None:
            raise KeyError(f"no incident for fingerprint {fingerprint!r} at marker {order_marker!r}")
        if target.stage != "rescue":
            raise ValueError("only the rescue stage can be sealed for relaunch")
        if target.consumption is None:
            raise ValueError("rescue order consumption is required before sealing relaunch evidence")
        bundle = _rescue_bundle_verified(self.state_root, target, bundle_sha256)
        if bundle is None:
            raise ValueError("rescue bundle hash does not match a governed RESCUE bundle")
        nonce = _checkpoint_receipt_nonce(self.state_root, target, bundle, checkpoint_ref)
        if nonce is None:
            raise ValueError("checkpoint reference does not match a governed checkpoint receipt")
        advanced = target.model_copy(update={"rescue_bundle_sha256": bundle_sha256, "checkpoint_ref": checkpoint_ref})
        return self._consume_receipt_and_seal(advanced, checkpoint_ref=checkpoint_ref, nonce=nonce)

    def _consume_receipt_and_seal(
        self, record: IncidentRecord, *, checkpoint_ref: str, nonce: str
    ) -> IncidentRecord:
        """Spend a checkpoint receipt exactly once, then append its seal.

        The duplicate-receipt check, the durable consumption record, and the
        incident append all happen under one exclusive hold of the incident
        lock, so two racers (or a replay after restart, which re-reads the
        same durable consumption log) can never both proceed. The consumption
        row is fsync'd *before* the incident row: a crash between the two
        leaves the receipt spent with no sealed row -- a retry fails closed,
        never appending a second sealed row for one receipt.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                consumed_refs, consumed_nonces = _load_consumed_checkpoints(self.state_root)
                if checkpoint_ref in consumed_refs or nonce in consumed_nonces:
                    raise ValueError(
                        f"checkpoint receipt {checkpoint_ref!r} was already consumed; a receipt seals exactly once"
                    )
                latest = next(
                    (
                        candidate
                        for candidate in reversed(self.load())
                        if candidate.fingerprint == record.fingerprint
                    ),
                    None,
                )
                if latest is not None and (latest.checkpoint_ref or latest.rescue_bundle_sha256):
                    raise ValueError("incident rescue stage is already sealed")
                _append_consumed_checkpoint(
                    self.state_root,
                    checkpoint_ref=checkpoint_ref,
                    nonce=nonce,
                    lane=self.lane,
                    fingerprint=record.fingerprint,
                )
                return self._append_locked(record)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ResponseLadder:
    """Decide the response to one recurring finding.

    ``evaluate`` returns exactly one of:

    - ``open``: first sighting; a nudge order should be dispatched;
    - ``hold``: recurrence observed but the previous order's consumption was
      never proven — nothing advances;
    - ``advance``: the same fingerprint recurred after proven consumption of
      the prior stage's order, so the incident moves to the next stage.
    """

    def __init__(
        self,
        store: IncidentStore,
        *,
        journal_events: Sequence[CanonicalEvent] = (),
        ledger_key: bytes | None = None,
    ) -> None:
        self.store = store
        self._events = tuple(journal_events)
        self._ledger_key = ledger_key

    def evaluate(self, *, lane: str, finding: Finding, order_marker: str) -> LadderDecision:
        existing = self.store.latest(finding.fingerprint)
        if existing is None:
            record = self.store.open_incident(lane=lane, finding=finding, order_marker=order_marker)
            return LadderDecision(action="open", stage=record.stage, record=record, reason="first sighting of this finding fingerprint")
        if existing.stage == "relaunch":
            return LadderDecision(action="hold", stage=existing.stage, record=existing, reason="incident already reached relaunch")
        if existing.consumption is None or not self._consumption_proven(existing):
            return LadderDecision(
                action="hold",
                stage=existing.stage,
                record=existing,
                reason="prior order consumption is not proven; elapsed time never advances the ladder",
            )
        if existing.stage == "rescue" and (not existing.rescue_bundle_sha256 or not existing.checkpoint_ref):
            return LadderDecision(
                action="hold",
                stage=existing.stage,
                record=existing,
                reason="relaunch requires a sealed RESCUE bundle and checkpoint receipt",
            )
        advanced = self.store.advance_to_next_stage(fingerprint=finding.fingerprint, next_order_marker=order_marker)
        return LadderDecision(
            action="advance", stage=advanced.stage, record=advanced, reason="same fingerprint recurred after proven consumption"
        )

    def stage_action_proven(self, record: IncidentRecord) -> bool:
        """Verify that a persisted stage was reached through the ladder.

        Recovery supervision runs without a fresh detector ``Finding``. In
        that path the stage alone is not enough: a forged or truncated latest
        incident must not authorize the next provider action.
        """

        records = [candidate for candidate in self.store.load() if candidate.fingerprint == record.fingerprint]
        if not records or records[-1] != record:
            return False
        stage_index = LADDER_STAGES.index(record.stage)
        if stage_index == 0:
            return len(records) == 1
        if len(records) < 2:
            return False
        previous = records[-2]
        if previous.stage != LADDER_STAGES[stage_index - 1] or not self._consumption_proven(previous):
            return False
        return record.stage != "relaunch" or bool(previous.rescue_bundle_sha256 and previous.checkpoint_ref)

    def _consumption_proven(self, record: IncidentRecord) -> bool:
        proof = record.consumption
        if proof is None:
            return False
        if self._ledger_key is None:
            return False
        if proof.ledger_key_hex and proof.ledger_key_hex != hashlib.sha256(self._ledger_key).hexdigest():
            return False
        if not verify_entry(proof.ledger_entry, key=self._ledger_key):
            return False
        if proof.session_ref and proof.ledger_entry.session_ref != proof.session_ref:
            return False
        if not proof.session_ref:
            return False
        if proof.native_session_id != proof.session_ref:
            if proof.ledger_entry.native_session_id != proof.native_session_id:
                return False
        elif proof.ledger_entry.native_session_id and proof.ledger_entry.native_session_id != proof.native_session_id:
            return False
        if f":{record.lane}:" not in proof.ledger_entry.session_ref:
            return False
        events_by_id = {event.event_id: event for event in self._events}
        user_event = events_by_id.get(proof.user_event_id)
        turn_event = events_by_id.get(proof.turn_event_id)
        if user_event is None or turn_event is None:
            return False
        if user_event.lane != record.lane or turn_event.lane != record.lane:
            return False
        if not proof.native_session_id:
            return False
        if user_event.session_id != proof.native_session_id or turn_event.session_id != proof.native_session_id:
            return False
        if user_event.native_type != "user" or user_event.normalized_type in {
            CanonicalType.TOOL_CALL,
            CanonicalType.TOOL_RESULT,
            CanonicalType.TOOL_ERROR,
            CanonicalType.FINAL_RESPONSE,
        }:
            return False
        user_text = _payload_text(user_event)
        if record.order_marker not in user_text:
            return False
        if proof.ledger_entry.message_hash != message_hash(user_text):
            return False
        if turn_event.normalized_type is not CanonicalType.FINAL_RESPONSE:
            return False
        if not self._events:
            return False
        user_position = _position_of(self._events, user_event.event_id)
        turn_position = _position_of(self._events, turn_event.event_id)
        if turn_position <= user_position:
            return False
        return _next_final_boundary(self._events, user_position) == turn_event.event_id


def _rescue_bundle_verified(state_root: Path, record: IncidentRecord, bundle_sha256: str) -> Any | None:
    from .rescue import BUNDLE_SCHEMA, RescueBundle

    for path in sorted((state_root / "rescue").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        try:
            bundle = RescueBundle.model_validate(payload)
        except ValueError:
            continue
        if bundle.schema_name != BUNDLE_SCHEMA or bundle.bundle_sha256 != bundle_sha256:
            continue
        if bundle.compute_digest() != bundle_sha256:
            continue
        expected_session = record.consumption.session_ref if record.consumption else ""
        if bundle.lane != record.lane or bundle.session_ref != expected_session:
            continue
        if bundle.checkpoint_requested is not True:
            continue
        if not _valid_rescue_process_identity(bundle.process_identity, expected_session=expected_session):
            continue
        if not _rescue_transcript_hash_verified(bundle.transcript_ref, bundle.transcript_sha256):
            continue
        if len(bundle.pane_capture) > 20000:
            continue
        if not isinstance(bundle.git_state, dict) or not bundle.git_state.get("head") or not bundle.git_state.get("branch"):
            continue
        if not bundle.contract.strip():
            continue
        if not any(record.fingerprint in entry for entry in bundle.incident_history):
            continue
        return bundle
    return None


def _checkpoint_receipt_nonce(
    state_root: Path, record: IncidentRecord, bundle: Any, checkpoint_ref: str
) -> str | None:
    """Verify a governed checkpoint receipt and return its anti-replay nonce.

    Returns ``None`` when the receipt is missing, forged, unbound to this
    incident and bundle, or carries no usable nonce. The returned nonce is
    not yet proof of freshness -- the caller must durably consume it (see
    ``IncidentStore._consume_receipt_and_seal``) before honoring the seal.
    """
    from .rescue import (
        CHECKPOINT_CANONICALIZATION,
        CHECKPOINT_PROVENANCE_KIND,
        CHECKPOINT_SCHEMA,
        CHECKPOINT_SCHEMA_VERSION,
        CHECKPOINT_SIGNATURE_SCOPE,
        CHECKPOINT_WRITER,
        verify_checkpoint_receipt_signature,
    )

    path = state_root / "checkpoints" / f"{checkpoint_ref}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not verify_checkpoint_receipt_signature(payload, state_root=state_root):
        return None
    expected_session = record.consumption.session_ref if record.consumption else ""
    expected_fields = {
        "schema_name",
        "schema_version",
        "checkpoint_ref",
        "lane",
        "session_ref",
        "incident_fingerprint",
        "rescue_bundle_sha256",
        "target_process_identity",
        "created_at",
        "writer_identity",
        "ledger_binding",
        "recovery_binding",
        "provenance",
        "anti_replay_nonce",
        "signature",
    }
    bundle_binding = bundle.recovery_binding
    if bundle_binding is not None:
        if bundle_binding.goal_version is None:
            return None
        expected_fields.add("goal_version")
    if set(payload) != expected_fields:
        return None
    if payload.get("schema_name") != CHECKPOINT_SCHEMA or payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        return None
    if payload.get("checkpoint_ref") != checkpoint_ref:
        return None
    if payload.get("lane") != record.lane or payload.get("session_ref") != expected_session:
        return None
    if payload.get("incident_fingerprint") != record.fingerprint:
        return None
    if payload.get("rescue_bundle_sha256") != bundle.bundle_sha256:
        return None
    if not _checkpoint_target_identity_matches(payload.get("target_process_identity"), bundle.process_identity):
        return None
    if not _checkpoint_ledger_binding_matches(payload.get("ledger_binding"), record):
        return None
    if payload.get("recovery_binding") != (
        None if bundle_binding is None else bundle_binding.model_dump(mode="json")
    ):
        return None
    if bundle_binding is not None and payload.get("goal_version") != bundle_binding.goal_version:
        return None
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("kind") != CHECKPOINT_PROVENANCE_KIND:
        return None
    if provenance.get("writer") != CHECKPOINT_WRITER:
        return None
    if provenance.get("signature_scope") != CHECKPOINT_SIGNATURE_SCOPE:
        return None
    if provenance.get("canonicalization") != CHECKPOINT_CANONICALIZATION:
        return None
    nonce = payload.get("anti_replay_nonce")
    if not isinstance(nonce, str) or len(nonce) < 16:
        return None
    return nonce


def _consumed_checkpoints_path(state_root: Path) -> Path:
    return state_root / "checkpoints" / ".consumed-checkpoints.jsonl"


def _load_consumed_checkpoints(state_root: Path) -> tuple[set[str], set[str]]:
    """Return the checkpoint refs and nonces earlier seals durably consumed.

    Survives restarts by construction: the log is an append-only file under
    the state root, so a replayed receipt is rejected even by a fresh
    process that never saw the original seal.
    """
    refs: set[str] = set()
    nonces: set[str] = set()
    try:
        lines = _consumed_checkpoints_path(state_root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return refs, nonces
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        ref = payload.get("checkpoint_ref")
        if isinstance(ref, str) and ref:
            refs.add(ref)
        stored_nonce = payload.get("anti_replay_nonce")
        if isinstance(stored_nonce, str) and stored_nonce:
            nonces.add(stored_nonce)
    return refs, nonces


def consumed_checkpoint_refs(state_root: Path) -> frozenset[str]:
    """Return checkpoint references already sealed by the incident store."""

    refs, _nonces = _load_consumed_checkpoints(state_root)
    return frozenset(refs)


def _append_consumed_checkpoint(
    state_root: Path, *, checkpoint_ref: str, nonce: str, lane: str, fingerprint: str
) -> None:
    directory = state_root / "checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    row = {
        "schema_name": CONSUMED_CHECKPOINT_SCHEMA,
        "checkpoint_ref": checkpoint_ref,
        "anti_replay_nonce": nonce,
        "lane": lane,
        "incident_fingerprint": fingerprint,
        "consumed_at": _utc_now(),
    }
    encoded = (json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    fd = os.open(_consumed_checkpoints_path(state_root), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _valid_rescue_process_identity(identity: dict[str, Any], *, expected_session: str) -> bool:
    if identity.get("session_ref") != expected_session:
        return False
    for key in ("target_pid", "capture_pid", "capture_ppid", "target_uid", "target_gid"):
        value = identity.get(key)
        if type(value) is not int or (key.endswith("pid") and value <= 0):
            return False
    start_time = identity.get("target_start_time")
    if not isinstance(start_time, str) or not start_time.isdigit():
        return False
    comm = identity.get("target_comm")
    if not isinstance(comm, str) or not comm:
        return False
    exe = identity.get("target_exe")
    return isinstance(exe, str)


def _rescue_transcript_hash_verified(transcript_ref: str, transcript_sha256: str) -> bool:
    if _HEX64_RE.fullmatch(transcript_sha256) is None:
        return False
    try:
        payload = Path(transcript_ref).read_bytes()
    except OSError:
        return False
    return hashlib.sha256(payload).hexdigest() == transcript_sha256


def _checkpoint_target_identity_matches(receipt_identity: Any, bundle_identity: dict[str, Any]) -> bool:
    if not isinstance(receipt_identity, dict):
        return False
    return all(receipt_identity.get(key) == bundle_identity.get(key) for key in _PROCESS_IDENTITY_KEYS)


def _checkpoint_ledger_binding_matches(binding: Any, record: IncidentRecord) -> bool:
    if not isinstance(binding, dict) or record.consumption is None:
        return False
    entry = record.consumption.ledger_entry
    expected = {
        "order_id": entry.order_id,
        "session_ref": entry.session_ref,
        "native_session_id": entry.native_session_id,
        "message_hash": entry.message_hash,
        "sent_at": entry.sent_at,
        "signature": entry.signature,
    }
    return binding == expected


def _payload_text(event: CanonicalEvent) -> str:
    value = event.payload.get("text")
    if isinstance(value, str):
        return value
    raw = event.raw_record
    if not isinstance(raw, dict):
        return ""
    message = raw.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
            return "\n".join(texts)
    return ""


def _position_of(events: tuple[CanonicalEvent, ...], event_id: str) -> int:
    for position, event in enumerate(events):
        if event.event_id == event_id:
            return position
    return -1


def _next_final_boundary(events: tuple[CanonicalEvent, ...], start_position: int) -> str:
    for event in events[start_position + 1 :]:
        if event.normalized_type is CanonicalType.FINAL_RESPONSE:
            return event.event_id
    return ""


__all__ = [
    "ConsumptionProof",
    "consumed_checkpoint_refs",
    "IncidentRecord",
    "IncidentStore",
    "LADDER_STAGES",
    "LadderDecision",
    "ResponseLadder",
]

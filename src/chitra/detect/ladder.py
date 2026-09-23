"""Incident store and the nudge → redirect → RESCUE → relaunch ladder.

The store is durable, append-only per lane, and keyed by
``(lane, frozen goal digest, unmet done-item)`` — the pressure track. A lane
that rotates its excuse for the same unmet item climbs the existing track
instead of opening a fresh nudge; the finding fingerprint survives only as a
per-stage detail field. The ladder advances only when the track recurs after
proven consumption of the prior stage's order — a signed delivery-ledger
entry plus a bound user event and turn boundary in the journal (the PR #93
receipt semantics). Elapsed time never establishes or advances anything.

On-disk rows are versioned: v2 rows carry ``schema_name`` and the
``goal_digest`` that keys the track. Rows written before this version load
as :class:`LegacyIncidentRecord` — readable, but never advanced.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chitra._fsio import exclusive_lock
from chitra.journal.models import CanonicalEvent, CanonicalType
from chitra.journal.normalizers import queued_operator_prompt
from chitra.ledger import LedgerEntry, message_hash, verify_entry

from .detectors import Finding

LADDER_STAGES: tuple[str, ...] = ("nudge", "redirect", "rescue", "relaunch")

INCIDENT_SCHEMA: Literal["chitra.detect.incident.v2"] = "chitra.detect.incident.v2"
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


def track_key(goal_digest: str, unmet_item: str) -> str:
    """Hash the durable pressure-track identity ``(goal digest, unmet item)``."""
    encoded = json.dumps(
        {"goal_digest": goal_digest, "unmet_item": unmet_item},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class IncidentRecord(BaseModel):
    """One durable incident keyed ``(lane, goal digest, unmet done-item)``.

    ``fingerprint`` stays on the record as the detail of the finding that
    opened or last advanced the track — rotating the excuse no longer rotates
    the track. ``schema_name`` versions the on-disk row.
    """

    model_config = ConfigDict(frozen=True)

    schema_name: Literal["chitra.detect.incident.v2"] = INCIDENT_SCHEMA
    lane: str
    goal_digest: str = Field(min_length=1)
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

    @property
    def track_id(self) -> str:
        """The pressure-track identity this record belongs to."""
        return track_key(self.goal_digest, self.unmet_item)


class LegacyIncidentRecord(BaseModel):
    """The pre-v2 incident shape, keyed ``(lane, finding fingerprint)``.

    Rows without ``schema_name`` load here so old incident files stay
    readable. They are evidence only: the store's mutators take v2
    ``track_id`` keys, so a legacy row can never be consumed, sealed, or
    advanced — a recurrence of the same unmet item opens a fresh v2 track.
    """

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


def discover_consumption_proof(
    record: IncidentRecord,
    journal_events: Sequence[CanonicalEvent],
    ledger_entry: LedgerEntry,
    ledger_key: bytes,
) -> ConsumptionProof | None:
    """Find the exact journal turn that consumed a signed ladder order.

    Discovery is deliberately fail-closed. The signed ledger row must verify,
    identify this lane, and match one user payload in the canonical journal.
    That payload must contain this incident's marker and hash in full. The
    first following ``FINAL_RESPONSE`` must belong to the same native session
    and lane. A stored proof, when present, constrains discovery to its exact
    event IDs so a later or cross-session turn cannot silently replace it.
    """
    if not ledger_entry.session_ref:
        return None
    return discover_delivery_consumption_proof(
        lane=record.lane,
        session_ref=ledger_entry.session_ref,
        order_marker=record.order_marker,
        journal_events=journal_events,
        ledger_entry=ledger_entry,
        ledger_key=ledger_key,
        stored=record.consumption,
    )


def discover_delivery_consumption_proof(
    *,
    lane: str,
    session_ref: str,
    order_marker: str,
    journal_events: Sequence[CanonicalEvent],
    ledger_entry: LedgerEntry,
    ledger_key: bytes,
    stored: ConsumptionProof | None = None,
) -> ConsumptionProof | None:
    """Prove that one signed delivery was consumed by its bound session.

    This is the generic form used by both incident corrections and routine
    frozen-goal answers.  It requires the signed order, exact session and lane,
    the full delivered text in a canonical user event, and a following final
    response boundary.  ``stored`` pins recovery to the same event pair after
    the first proof; a later or cross-session response cannot replace it.
    """
    if (
        not ledger_key
        or not order_marker
        or not verify_entry(ledger_entry, key=ledger_key)
        or ledger_entry.session_ref != session_ref
    ):
        return None
    native_session_id = ledger_entry.native_session_id or ledger_entry.session_ref
    if not native_session_id:
        return None
    if stored is not None:
        if stored.ledger_entry != ledger_entry:
            return None
        if stored.ledger_key_hex and stored.ledger_key_hex != hashlib.sha256(ledger_key).hexdigest():
            return None
        if stored.session_ref != ledger_entry.session_ref or stored.native_session_id != native_session_id:
            return None

    events = tuple(journal_events)
    events_by_id = {event.event_id: event for event in events}
    for position, user_event in enumerate(events):
        if stored is not None and user_event.event_id != stored.user_event_id:
            continue
        if user_event.lane != lane or user_event.session_id != native_session_id:
            continue
        # Input arrives as a user record, or mid-turn as an origin-bearing
        # queued_command attachment. The boundary stays the next final response.
        is_input = user_event.native_type == "user" or queued_operator_prompt(user_event.raw_record) is not None
        if not is_input or user_event.normalized_type in {
            CanonicalType.TOOL_CALL,
            CanonicalType.TOOL_RESULT,
            CanonicalType.TOOL_ERROR,
            CanonicalType.FINAL_RESPONSE,
        }:
            continue
        if isinstance(user_event.raw_record, dict) and user_event.raw_record.get("isCompactSummary") is True:
            continue
        user_text = _payload_text(user_event)
        if not user_text or order_marker not in user_text:
            continue
        if ledger_entry.message_hash != message_hash(user_text):
            continue
        turn_event_id = _next_final_boundary(events, position)
        if not turn_event_id:
            continue
        turn_event = events_by_id.get(turn_event_id)
        if turn_event is None or turn_event.lane != lane or turn_event.session_id != native_session_id:
            continue
        if stored is not None and turn_event_id != stored.turn_event_id:
            continue
        return ConsumptionProof(
            ledger_entry=ledger_entry,
            ledger_key_hex=hashlib.sha256(ledger_key).hexdigest(),
            session_ref=ledger_entry.session_ref,
            native_session_id=native_session_id,
            user_event_id=user_event.event_id,
            turn_event_id=turn_event_id,
        )
    return None


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

    def load(self) -> list[IncidentRecord | LegacyIncidentRecord]:
        if not self.path.exists():
            return []
        records: list[IncidentRecord | LegacyIncidentRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"invalid incident row {self.path}:{line_number}: {exc}") from exc
                if not isinstance(payload, dict):
                    raise ValueError(f"invalid incident row {self.path}:{line_number}: not an object")
                schema = payload.get("schema_name")
                if schema == INCIDENT_SCHEMA:
                    model: type[IncidentRecord] | type[LegacyIncidentRecord] = IncidentRecord
                elif schema is None:
                    model = LegacyIncidentRecord
                else:
                    raise ValueError(f"invalid incident row {self.path}:{line_number}: unknown schema {schema!r}")
                try:
                    records.append(model.model_validate(payload))
                except ValueError as exc:
                    raise ValueError(f"invalid incident row {self.path}:{line_number}: {exc}") from exc
        return records

    def latest(self, track_id: str) -> IncidentRecord | None:
        """Return the newest v2 record on one pressure track, if any."""
        for record in reversed(self.load()):
            if isinstance(record, IncidentRecord) and record.track_id == track_id:
                return record
        return None

    def latest_by_fingerprint(self, fingerprint: str) -> IncidentRecord | LegacyIncidentRecord | None:
        """Return the newest record carrying ``fingerprint`` as its current detail.

        Diagnostic lookup only — production keys on ``latest(track_id)`` —
        but it keeps old fingerprint-bearing records reachable for review.
        """
        for record in reversed(self.load()):
            if record.fingerprint == fingerprint:
                return record
        return None

    def _append(self, record: IncidentRecord) -> IncidentRecord:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with exclusive_lock(self.lock_path, mode=0o600):
            return self._append_locked(record)

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

    def open_incident(self, *, lane: str, goal_digest: str, finding: Finding, order_marker: str) -> IncidentRecord:
        record = IncidentRecord(
            lane=lane,
            goal_digest=goal_digest,
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
        track_id: str,
        order_marker: str,
        proof: ConsumptionProof,
    ) -> IncidentRecord:
        """Bind proven consumption to the newest open record on a track."""
        records = self.load()
        target = next(
            (
                record
                for record in reversed(records)
                if isinstance(record, IncidentRecord) and record.track_id == track_id and record.order_marker == order_marker
            ),
            None,
        )
        if target is None:
            raise KeyError(f"no open incident for track {track_id!r} at marker {order_marker!r}")
        updated = target.model_copy(update={"consumption": proof})
        return self._append(updated)

    def advance(self, *, track_id: str) -> IncidentRecord:
        raise TypeError("advance requires next_order_marker")

    def advance_to_next_stage(self, *, track_id: str, next_order_marker: str, finding: Finding) -> IncidentRecord:
        """Move the newest consumed record on a track to its next stage.

        The track identity — goal digest and unmet item — never changes; the
        record instead takes the current finding's fingerprint, detector, and
        detail so the next stage's order speaks to the latest excuse.
        """
        records = self.load()
        target = next(
            (record for record in reversed(records) if isinstance(record, IncidentRecord) and record.track_id == track_id),
            None,
        )
        if target is None:
            raise KeyError(f"no incident for track {track_id!r}")
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
                "fingerprint": finding.fingerprint,
                "detector": finding.detector,
                "event_refs": finding.event_refs,
                "expected_next_progress": finding.expected_next_progress,
                "detail": finding.detail,
                "consumption": None,
                "rescue_bundle_sha256": "",
                "checkpoint_ref": "",
            }
        )
        return self._append(advanced)

    def seal_rescue_checkpoint(
        self, *, track_id: str, order_marker: str, bundle_sha256: str, checkpoint_ref: str
    ) -> IncidentRecord:
        if _HEX64_RE.fullmatch(bundle_sha256) is None or _SAFE_REF_RE.fullmatch(checkpoint_ref) is None:
            raise ValueError("rescue bundle hash and checkpoint reference are required")
        records = self.load()
        target = next(
            (
                record
                for record in reversed(records)
                if isinstance(record, IncidentRecord) and record.track_id == track_id and record.order_marker == order_marker
            ),
            None,
        )
        if target is None:
            raise KeyError(f"no incident for track {track_id!r} at marker {order_marker!r}")
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
        with exclusive_lock(self.lock_path, mode=0o600):
            consumed_refs, consumed_nonces = _load_consumed_checkpoints(self.state_root)
            if checkpoint_ref in consumed_refs or nonce in consumed_nonces:
                raise ValueError(
                    f"checkpoint receipt {checkpoint_ref!r} was already consumed; a receipt seals exactly once"
                )
            latest = next(
                (
                    candidate
                    for candidate in reversed(self.load())
                    if isinstance(candidate, IncidentRecord) and candidate.track_id == record.track_id
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ResponseLadder:
    """Decide the response to one recurring finding.

    ``evaluate`` returns exactly one of:

    - ``open``: first sighting of this ``(goal digest, unmet item)`` track; a
      nudge order should be dispatched;
    - ``hold``: recurrence observed but the previous order's consumption was
      never proven — nothing advances;
    - ``advance``: the track recurred after proven consumption of the prior
      stage's order, so the incident moves to the next stage. A rotated
      excuse keeps the same track — the finding fingerprint is detail, never
      the key.
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

    def evaluate(self, *, lane: str, finding: Finding, order_marker: str, goal_digest: str) -> LadderDecision:
        if not goal_digest:
            raise ValueError("evaluate requires the frozen goal digest to key the pressure track")
        track_id = track_key(goal_digest, finding.unmet_item)
        existing = self.store.latest(track_id)
        if existing is None:
            record = self.store.open_incident(lane=lane, goal_digest=goal_digest, finding=finding, order_marker=order_marker)
            return LadderDecision(
                action="open",
                stage=record.stage,
                record=record,
                reason="first finding on this unmet item under the frozen goal",
            )
        if existing.stage == "relaunch":
            return LadderDecision(action="hold", stage=existing.stage, record=existing, reason="incident already reached relaunch")
        if existing.consumption is None or not self._consumption_proven(existing, finding):
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
        advanced = self.store.advance_to_next_stage(track_id=track_id, next_order_marker=order_marker, finding=finding)
        return LadderDecision(
            action="advance",
            stage=advanced.stage,
            record=advanced,
            reason="a finding on the same unmet item recurred after proven consumption",
        )

    def _consumption_proven(self, record: IncidentRecord, finding: Finding) -> bool:
        proof = record.consumption
        if proof is None:
            return False
        if self._ledger_key is None:
            return False
        discovered = discover_consumption_proof(record, self._events, proof.ledger_entry, self._ledger_key)
        if discovered is None:
            return False
        turn_position = _position_of(self._events, discovered.turn_event_id)
        if turn_position < 0:
            return False
        # A historical finding must not advance the ladder. The detector must
        # report at least one event strictly after the consumed turn boundary.
        return any(_position_of(self._events, event_id) > turn_position for event_id in finding.event_refs)


def _iter_rescue_bundles(state_root: Path) -> Iterator[Any]:
    from .rescue import RescueBundle

    for path in sorted((state_root / "rescue").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        try:
            yield RescueBundle.model_validate(payload)
        except ValueError:
            continue


def _bundle_matches_record(bundle: Any, record: IncidentRecord, expected_session: str) -> bool:
    from .rescue import BUNDLE_SCHEMA

    return bool(
        bundle.schema_name == BUNDLE_SCHEMA
        and bundle.compute_digest() == bundle.bundle_sha256
        and bundle.lane == record.lane
        and bundle.session_ref == expected_session
        and bundle.checkpoint_requested is True
        and _valid_rescue_process_identity(bundle.process_identity, expected_session=expected_session)
        and _rescue_transcript_hash_verified(bundle.transcript_ref, bundle.transcript_sha256)
        and len(bundle.pane_capture) <= 20000
        and isinstance(bundle.git_state, dict)
        and bundle.git_state.get("head")
        and bundle.git_state.get("branch")
        and bundle.contract.strip()
        and any(record.fingerprint in entry for entry in bundle.incident_history)
    )


def find_rescue_bundle(state_root: Path, record: IncidentRecord, *, session_ref: str) -> Any | None:
    """Return a verified on-disk RESCUE bundle for ``record``, if one exists.

    ``session_ref`` is the frozen goal session the bundle must be bound to;
    the seal path additionally requires the incident's proven consumption
    session, which is identical once the rescue order has been consumed.
    """
    for bundle in _iter_rescue_bundles(state_root):
        if _bundle_matches_record(bundle, record, session_ref):
            return bundle
    return None


def _rescue_bundle_verified(state_root: Path, record: IncidentRecord, bundle_sha256: str) -> Any | None:
    expected_session = record.consumption.session_ref if record.consumption else ""
    for bundle in _iter_rescue_bundles(state_root):
        if bundle.bundle_sha256 == bundle_sha256 and _bundle_matches_record(bundle, record, expected_session):
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
        "provenance",
        "anti_replay_nonce",
        "signature",
    }
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
    queued = queued_operator_prompt(raw)
    if queued is not None:
        return queued
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
    "CONSUMED_CHECKPOINT_SCHEMA",
    "ConsumptionProof",
    "discover_consumption_proof",
    "discover_delivery_consumption_proof",
    "find_rescue_bundle",
    "INCIDENT_SCHEMA",
    "IncidentRecord",
    "IncidentStore",
    "LADDER_STAGES",
    "LadderDecision",
    "LegacyIncidentRecord",
    "ResponseLadder",
    "track_key",
]

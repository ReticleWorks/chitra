"""Durable blocker-claim records and the two cheapest checks on them.

A lane's "blocked on X" is prose until it is parsed, recorded, and compared
with the lane's own evidence. Every blocker-assertion line in a final
response becomes a :class:`BlockerClaimRecord` keyed by ``(goal digest,
unmet item, normalized obstacle)`` so a lane cannot rotate wording to escape
counting. Two deterministic checks then run on the claim history:

- the same claim re-asserted with no intervening tool call is a
  ``false_blocker`` finding;
- a claim that rotates while the same item stays unmet is a
  ``changed_excuse`` finding.

Both ride the pressure track for the unmet item the claim excuses rather than
trusting the prose or opening a fresh track per wording.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence, Set
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from chitra._fsio import exclusive_lock
from chitra.journal.models import CanonicalEvent, CanonicalType
from chitra.lexicon import (
    COMPLETION_EVIDENCE_FAILURE_RE,
    COMPLETION_EVIDENCE_LIVE_RESULT_RE,
    COMPLETION_EVIDENCE_PATH_RE,
    COMPLETION_EVIDENCE_PR_RE,
    COMPLETION_EVIDENCE_SHA_RE,
)

from .detectors import Finding, first_unmet_item_id

BLOCKER_CLAIM_SCHEMA: Literal["chitra.detect.blocker-claim.v1"] = "chitra.detect.blocker-claim.v1"

_LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

_BLOCKER_RE = re.compile(
    r"(?:\bblocked\b|\bblocker\b|\bcan't\b|\bcannot\b|\bunable to\b|\bno access\b"
    r"|\bpermission denied\b|\baccess denied\b|\bnot installed\b|\brate[- ]?limited\b"
    r"|\bwaiting (?:on|for)\b|\bheld up by\b|\bstuck (?:on|until|for|waiting)\b"
    r"|\bneed(?:s|ed)?\s+(?:an?\s+)?(?:(?:operator|human|your)\s+)?"
    r"(?:credentials?|access|approval|sign[- ]?off|permission|decision|input|review)\b)",
    re.IGNORECASE,
)
_LEADING_FILLER_RE = re.compile(r"^(?:on|for|by|to|until|because|due to|with|the|a|an)\s+", re.IGNORECASE)
_EVIDENCE_RES = (
    COMPLETION_EVIDENCE_SHA_RE,
    COMPLETION_EVIDENCE_PATH_RE,
    COMPLETION_EVIDENCE_PR_RE,
    COMPLETION_EVIDENCE_LIVE_RESULT_RE,
    COMPLETION_EVIDENCE_FAILURE_RE,
)


class BlockerClaimRecord(BaseModel):
    """One parsed blocker claim bound to the unmet item it excuses.

    ``claim_key`` is the normalized obstacle clause, so the same excuse in
    fresh wrapper text keeps one identity; ``evidence`` holds the checkable
    citation the claim offered, if any.
    """

    model_config = ConfigDict(frozen=True)

    schema_name: Literal["chitra.detect.blocker-claim.v1"] = BLOCKER_CLAIM_SCHEMA
    lane: str
    goal_digest: str = ""
    unmet_item: str = ""
    claim_key: str
    claim_text: str
    evidence: str = ""
    event_id: str
    recorded_at: str


def _normalize_claim_key(text: str) -> str:
    key = " ".join(text.lower().split()).strip(" -*`•_#>:,;.!'\"()[]{}")
    while True:
        stripped = _LEADING_FILLER_RE.sub("", key)
        if stripped == key:
            return key
        key = stripped


def extract_blocker_claims(text: str) -> list[dict[str, str]]:
    """Parse each blocker-assertion line into claim key, text, and evidence."""
    claims: list[dict[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _BLOCKER_RE.search(line)
        if match is None:
            continue
        obstacle = _normalize_claim_key(line[match.end() :])
        if not obstacle:
            obstacle = _normalize_claim_key(match.group(0))
        evidence = ""
        for pattern in _EVIDENCE_RES:
            found = pattern.search(line)
            if found is not None:
                evidence = found.group(0)[:160]
                break
        claims.append(
            {
                "claim_key": obstacle,
                "claim_text": line[:240],
                "evidence": evidence,
            }
        )
    return claims


class BlockerClaimStore:
    """Append-only per-lane blocker-claim log under ``<state_root>/blockers``."""

    def __init__(self, state_root: Path, lane: str) -> None:
        if _LANE_RE.fullmatch(lane) is None:
            raise ValueError(f"unsafe lane name: {lane!r}")
        self.lane = lane
        self.directory = state_root / "blockers"
        self.path = self.directory / f"{lane}.jsonl"
        self.lock_path = self.directory / f"{lane}.lock"

    def load(self) -> list[BlockerClaimRecord]:
        if not self.path.exists():
            return []
        records: list[BlockerClaimRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    records.append(BlockerClaimRecord.model_validate_json(line))
                except ValueError as exc:
                    raise ValueError(f"invalid blocker claim row {self.path}:{line_number}: {exc}") from exc
        return records

    def append(self, records: Sequence[BlockerClaimRecord]) -> int:
        """Durably append records, skipping duplicates already on disk."""
        if not records:
            return 0
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with exclusive_lock(self.lock_path, mode=0o600):
            seen = {(record.event_id, record.claim_key) for record in self.load()}
            appended = 0
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
                for record in records:
                    if (record.event_id, record.claim_key) in seen:
                        continue
                    encoded = (record.model_dump_json() + "\n").encode()
                    view = memoryview(encoded)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                    seen.add((record.event_id, record.claim_key))
                    appended += 1
                os.fsync(fd)
            finally:
                os.close(fd)
            return appended


def detect_blocker_claims(
    events: Sequence[CanonicalEvent],
    *,
    enrolled_items: Sequence[object] = (),
    met_items: Set[str] = frozenset(),
    history: Sequence[BlockerClaimRecord] = (),
    goal_digest: str = "",
) -> tuple[list[Finding], list[BlockerClaimRecord]]:
    """Record new blocker claims and emit findings for repeats or rotations.

    A first-time claim is recorded but produces no finding; the claim only
    becomes a finding when it re-appears with no intervening tool call, or
    when a different obstacle replaces it while the same item stays unmet.
    """
    findings: list[Finding] = []
    records: list[BlockerClaimRecord] = []
    unmet = first_unmet_item_id(enrolled_items, met_items)
    positions = {event.event_id: index for index, event in enumerate(events)}
    tool_call_positions = [
        index for index, event in enumerate(events) if event.normalized_type is CanonicalType.TOOL_CALL
    ]
    seen = {(record.event_id, record.claim_key) for record in history}
    track_history = [
        record for record in history if record.goal_digest == goal_digest and record.unmet_item == unmet
    ]
    for event in events:
        if event.normalized_type is not CanonicalType.FINAL_RESPONSE:
            continue
        text = event.payload.get("text")
        if not isinstance(text, str) or not text:
            continue
        for claim in extract_blocker_claims(text):
            if (event.event_id, claim["claim_key"]) in seen:
                continue
            record = BlockerClaimRecord(
                lane=event.lane,
                goal_digest=goal_digest,
                unmet_item=unmet,
                claim_key=claim["claim_key"],
                claim_text=claim["claim_text"],
                evidence=claim["evidence"],
                event_id=event.event_id,
                recorded_at=event.observed_at,
            )
            priors = [prior for prior in track_history if prior.event_id != event.event_id]
            same = [prior for prior in priors if prior.claim_key == record.claim_key]
            different = [prior for prior in priors if prior.claim_key != record.claim_key]
            if same:
                last_position = positions.get(same[-1].event_id)
                start = last_position if last_position is not None else -1
                attempts = sum(
                    1 for index in tool_call_positions if start < index < positions[event.event_id]
                )
                if attempts == 0:
                    findings.append(
                        Finding(
                            detector="false_blocker",
                            fingerprint_seed={
                                "claim_key": record.claim_key,
                                "event_id": event.event_id,
                                "unmet_item": unmet,
                            },
                            event_refs=(same[-1].event_id, event.event_id),
                            unmet_item=unmet,
                            expected_next_progress=(
                                "attempt the claimed obstacle or produce checkable evidence it still holds, "
                                "then continue toward the unmet item it excuses"
                            ),
                            detail=(
                                f"blocker {record.claim_text!r} re-claimed {len(same) + 1} times "
                                "with no intervening tool call"
                            ),
                        )
                    )
            elif different:
                previous = different[-1]
                findings.append(
                    Finding(
                        detector="changed_excuse",
                        fingerprint_seed={
                            "previous_claim_key": previous.claim_key,
                            "claim_key": record.claim_key,
                            "event_id": event.event_id,
                            "unmet_item": unmet,
                        },
                        event_refs=(previous.event_id, event.event_id),
                        unmet_item=unmet,
                        expected_next_progress=(
                            "stop rotating blocker claims: either attempt the obstacle or produce "
                            "evidence toward the unmet item it excuses"
                        ),
                        detail=(
                            f"blocker claim rotated from {previous.claim_text!r} to "
                            f"{record.claim_text!r} while item {unmet!r} stays unmet"
                        ),
                    )
                )
            records.append(record)
            track_history.append(record)
            seen.add((event.event_id, record.claim_key))
    return findings, records


__all__ = [
    "BLOCKER_CLAIM_SCHEMA",
    "BlockerClaimRecord",
    "BlockerClaimStore",
    "detect_blocker_claims",
    "extract_blocker_claims",
]

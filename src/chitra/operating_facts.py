"""Read and merge explicit Chitra operating-facts snapshots.

This module is deliberately a boundary reader, not a Fleet configuration
parser.  A Fleet-facing publisher will emit ``chitra.operating-facts.v1``
documents later.  Until then, Chitra accepts only those documents and keeps
their named records and uncertainty states intact.

The reader performs no network access, login, command execution, or write.  A
missing or malformed source is reported as a fact instead of being replaced
with a value inferred from another document.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from chitra.session_contract import FactState, OperatingFact

OPERATING_FACTS_SCHEMA: Literal["chitra.operating-facts.v1"] = "chitra.operating-facts.v1"
PRODUCTION_OPERATING_FACTS_PATH = Path("/var/lib/polyphony-chitra/operating-facts.json")

FactCategory = Literal["placement", "routing", "credential-readiness", "access", "capacity"]

_CATEGORY_PREFIX: dict[FactCategory, str] = {
    "placement": "fleet.placement",
    "routing": "fleet.routing",
    "credential-readiness": "fleet.credential-readiness",
    "access": "fleet.access",
    "capacity": "fleet.capacity",
}
_CATEGORIES: tuple[FactCategory, ...] = tuple(_CATEGORY_PREFIX)
_SNAPSHOT_KEYS = frozenset(("schema", "observed_at", "facts"))


def _as_paths(value: Path | Sequence[Path]) -> tuple[Path, ...]:
    if isinstance(value, Path):
        return (value,)
    return tuple(value)


@dataclass(frozen=True, slots=True)
class OperatingFactsSources:
    """Explicit paths for category snapshots published by an authority.

    Each path must contain one JSON ``chitra.operating-facts.v1`` snapshot.
    The category field is explicit in this object; the reader does not inspect
    arbitrary Fleet YAML or infer a category from unrelated configuration.
    Multiple paths in one category are merged and disagreements are retained
    as ``conflicting`` facts.
    """

    placement: Path | Sequence[Path] = ()
    routing: Path | Sequence[Path] = ()
    credential_readiness: Path | Sequence[Path] = ()
    access: Path | Sequence[Path] = ()
    capacity: Path | Sequence[Path] = ()

    def paths(self, category: FactCategory) -> tuple[Path, ...]:
        if category == "placement":
            return _as_paths(self.placement)
        if category == "routing":
            return _as_paths(self.routing)
        if category == "credential-readiness":
            return _as_paths(self.credential_readiness)
        if category == "access":
            return _as_paths(self.access)
        return _as_paths(self.capacity)


def production_operating_facts_sources() -> OperatingFactsSources:
    """Return the Fleet-published production snapshot location."""

    path = PRODUCTION_OPERATING_FACTS_PATH
    return OperatingFactsSources(
        placement=path,
        routing=path,
        credential_readiness=path,
        access=path,
        capacity=path,
    )


@dataclass(frozen=True, slots=True)
class OperatingFactsSnapshot:
    """Validated Chitra operating-facts snapshot."""

    observed_at: str
    facts: tuple[OperatingFact, ...]
    schema: Literal["chitra.operating-facts.v1"] = OPERATING_FACTS_SCHEMA

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "observed_at": self.observed_at,
            "facts": [fact.to_dict() for fact in self.facts],
        }

    @classmethod
    def from_dict(cls, payload: object) -> OperatingFactsSnapshot:
        if not isinstance(payload, Mapping):
            raise ValueError("operating facts document must be an object")
        if set(payload) != _SNAPSHOT_KEYS:
            raise ValueError("operating facts document has unexpected or missing fields")
        if payload.get("schema") != OPERATING_FACTS_SCHEMA:
            raise ValueError(f"document is not a {OPERATING_FACTS_SCHEMA} document")
        observed_at = payload.get("observed_at")
        raw_facts = payload.get("facts")
        if not isinstance(observed_at, str):
            raise ValueError("operating facts observed_at is required")
        _parse_timestamp(observed_at, "operating_facts.observed_at")
        if not isinstance(raw_facts, list):
            raise ValueError("operating facts facts must be a list")
        facts = tuple(OperatingFact.from_dict(item) for item in raw_facts)
        names = [fact.name for fact in facts]
        if len(names) != len(set(names)):
            raise ValueError("operating facts names must be unique")
        return cls(observed_at=observed_at, facts=facts)

    def get(self, name: str) -> OperatingFact | None:
        """Return one named fact, or ``None`` when it is absent."""

        return next((fact for fact in self.facts if fact.name == name), None)

    def by_category(self, category: FactCategory) -> tuple[OperatingFact, ...]:
        prefix = _CATEGORY_PREFIX[category]
        return tuple(fact for fact in self.facts if fact.name == prefix or fact.name.startswith(prefix + "."))


@dataclass(frozen=True, slots=True)
class _Candidate:
    fact: OperatingFact
    document: Path


@dataclass(frozen=True, slots=True)
class _SourceResult:
    facts: tuple[OperatingFact, ...] | None
    state: FactState
    reason: str = ""


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _now(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _revision(candidates: Sequence[_Candidate]) -> str:
    evidence = [
        {"document": str(candidate.document), "fact": candidate.fact.to_dict()}
        for candidate in sorted(candidates, key=lambda item: str(item.document))
    ]
    return hashlib.sha256(_canonical(evidence).encode("utf-8")).hexdigest()


def _source_text(candidates: Sequence[_Candidate]) -> str:
    return ";".join(sorted({candidate.fact.source for candidate in candidates}))


def _observed_at(fact: OperatingFact) -> datetime:
    return _parse_timestamp(fact.observed_at, "operating_fact.observed_at")


def _freshen(fact: OperatingFact, *, now: datetime) -> OperatingFact:
    """Honor an explicit deadline without deriving freshness from file mtime."""

    if fact.state == "known" and fact.fresh_until is not None and _parse_timestamp(fact.fresh_until, "fact.fresh_until") < now:
        return fact.model_copy(update={"state": "stale", "freshness": "stale"})
    return fact


def _failure_fact(
    *,
    name: str,
    source: str,
    state: FactState,
    now: datetime,
    path: Path | None = None,
    reason: str = "",
) -> OperatingFact:
    value: object | None = None
    if state == "inaccessible":
        value = {"path": str(path) if path is not None else None, "reason": reason or "source is inaccessible"}
    return OperatingFact(
        name=name,
        value=value,
        state=state,
        source=source,
        revision=state,
        observed_at=_iso(now),
        freshness="unknown",
        fresh_until=None,
        within_authority=False,
    )


def _read_source(path: Path, category: FactCategory) -> _SourceResult:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _SourceResult(None, "missing", "source file does not exist")
    except OSError as exc:
        return _SourceResult(None, "inaccessible", str(exc))

    try:
        payload = json.loads(raw.decode("utf-8"))
        snapshot = OperatingFactsSnapshot.from_dict(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return _SourceResult(None, "inaccessible", f"invalid operating-facts snapshot: {exc}")

    prefix = _CATEGORY_PREFIX[category]
    wrong_category = tuple(fact.name for fact in snapshot.facts if fact.name != prefix and not fact.name.startswith(prefix + "."))
    if wrong_category:
        return _SourceResult(None, "inaccessible", f"facts do not belong to {category}: {', '.join(wrong_category)}")
    return _SourceResult(snapshot.facts, "known")


def _merge(name: str, candidates: Sequence[_Candidate]) -> OperatingFact:
    if not candidates:
        raise ValueError("cannot merge an empty candidate set")
    unique_states = {(candidate.fact.state, _canonical(candidate.fact.value)) for candidate in candidates}
    if len(unique_states) > 1:
        ordered = sorted(candidates, key=lambda item: (str(item.document), item.fact.source, str(item.fact.revision)))
        return OperatingFact(
            name=name,
            value={
                "candidates": [
                    {"document": str(candidate.document), "fact": candidate.fact.to_dict()} for candidate in ordered
                ]
            },
            state="conflicting",
            source=_source_text(candidates),
            revision=_revision(candidates),
            observed_at=max(candidate.fact.observed_at for candidate in candidates),
            freshness="unknown",
            fresh_until=None,
            within_authority=False,
        )

    chosen = max(candidates, key=lambda item: (_observed_at(item.fact), item.fact.source, str(item.document)))
    if len(candidates) == 1:
        return chosen.fact
    return chosen.fact.model_copy(update={"source": _source_text(candidates), "revision": _revision(candidates)})


def read_operating_facts(
    sources: OperatingFactsSources | None = None,
    *,
    now: datetime | None = None,
) -> OperatingFactsSnapshot:
    """Read explicit category snapshots and merge named records.

    ``sources`` is a set of paths supplied by an authority.  An omitted path
    yields a missing category fact.  A malformed or wrongly categorized
    document yields an inaccessible source fact.  No source shape besides the
    versioned snapshot contract is accepted.
    """

    current = _now(now)
    configured = production_operating_facts_sources() if sources is None else sources
    candidates: dict[str, list[_Candidate]] = {}

    for category in _CATEGORIES:
        paths = configured.paths(category)
        prefix = _CATEGORY_PREFIX[category]
        valid_document = False
        health: list[OperatingFact] = []
        if not paths:
            health.append(
                _failure_fact(
                    name=prefix,
                    source=f"{prefix}:not-configured",
                    state="missing",
                    now=current,
                )
            )
        for index, path in enumerate(paths):
            result = _read_source(path, category)
            if result.facts is None:
                health.append(
                    _failure_fact(
                        name=f"{prefix}.source.{index}",
                        source=str(path),
                        state=result.state,
                        path=path,
                        reason=result.reason,
                        now=current,
                    )
                )
                continue
            if not result.facts:
                health.append(
                    _failure_fact(
                        name=f"{prefix}.source.{index}",
                        source=str(path),
                        state="missing",
                        path=path,
                        reason="snapshot has no named records",
                        now=current,
                    )
                )
                continue
            valid_document = True
            for fact in result.facts:
                current_fact = _freshen(fact, now=current)
                candidates.setdefault(current_fact.name, []).append(_Candidate(current_fact, path))

        if not valid_document and health:
            health_state: FactState = "inaccessible" if any(fact.state == "inaccessible" for fact in health) else "missing"
            health.append(
                _failure_fact(
                    name=prefix,
                    source=f"{prefix}:unavailable",
                    state=health_state,
                    now=current,
                    reason="no usable category snapshot",
                )
            )
        for fact in health:
            candidates.setdefault(fact.name, []).append(_Candidate(fact, Path(fact.source)))

    facts = tuple(_merge(name, candidates[name]) for name in sorted(candidates))
    return OperatingFactsSnapshot(observed_at=_iso(current), facts=facts)


__all__ = [
    "FactCategory",
    "OPERATING_FACTS_SCHEMA",
    "OperatingFactsSnapshot",
    "OperatingFactsSources",
    "PRODUCTION_OPERATING_FACTS_PATH",
    "production_operating_facts_sources",
    "read_operating_facts",
]

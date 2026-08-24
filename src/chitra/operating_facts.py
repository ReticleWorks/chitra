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
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from chitra.session_contract import FactState, OperatingFact

OPERATING_FACTS_SCHEMA: Literal["chitra.operating-facts.v1"] = "chitra.operating-facts.v1"
PRODUCTION_OPERATING_FACTS_PATH = Path("/var/lib/polyphony-chitra/operating-facts.json")
PRODUCTION_OPERATING_FACTS_INPUTS_PATH = Path(
    "/var/lib/polyphony-chitra/approved-operating-facts-inputs.json"
)

FactCategory = Literal["placement", "routing", "credential-readiness", "access", "capacity", "versions", "provider-capabilities"]

_CATEGORY_PREFIX: dict[FactCategory, str] = {
    "placement": "fleet.placement",
    "routing": "fleet.routing",
    "credential-readiness": "fleet.credential-readiness",
    "access": "fleet.access",
    "capacity": "fleet.capacity",
    "versions": "fleet.versions",
    "provider-capabilities": "fleet.provider-capabilities",
}
_CATEGORIES: tuple[FactCategory, ...] = tuple(_CATEGORY_PREFIX)
_SNAPSHOT_KEYS = frozenset(("schema", "observed_at", "facts", "provenance"))
_BASE_SNAPSHOT_KEYS = frozenset(("schema", "observed_at", "facts"))
PROVENANCE_SCHEMA = "chitra.operating-facts-provenance.v1"


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
    versions: Path | Sequence[Path] = ()
    provider_capabilities: Path | Sequence[Path] = ()
    # Production output includes a publisher receipt. Test and migration
    # sources may omit it until they are upgraded; production never does.
    require_provenance: bool = False
    # Production receipt paths are not self-authenticating. This independent
    # Fleet-owned path is the only source the production reader will accept.
    trusted_source: Path | None = None

    def paths(self, category: FactCategory) -> tuple[Path, ...]:
        if category == "placement":
            return _as_paths(self.placement)
        if category == "routing":
            return _as_paths(self.routing)
        if category == "credential-readiness":
            return _as_paths(self.credential_readiness)
        if category == "access":
            return _as_paths(self.access)
        if category == "capacity":
            return _as_paths(self.capacity)
        if category == "versions":
            return _as_paths(self.versions)
        return _as_paths(self.provider_capabilities)


def production_operating_facts_sources() -> OperatingFactsSources:
    """Return the Fleet-published production snapshot location."""

    path = PRODUCTION_OPERATING_FACTS_PATH
    return OperatingFactsSources(
        placement=path,
        routing=path,
        credential_readiness=path,
        access=path,
        capacity=path,
        versions=path,
        provider_capabilities=path,
        require_provenance=True,
        trusted_source=PRODUCTION_OPERATING_FACTS_INPUTS_PATH,
    )


@dataclass(frozen=True, slots=True)
class OperatingFactsProvenance:
    """Publisher evidence for one facts snapshot.

    ``snapshot_sha256`` covers only the schema, observation time, and facts.
    Excluding this receipt avoids a self-referential hash while still binding
    every selected route to the exact facts content.
    """

    source_path: str
    source_sha256: str
    source_mode: int
    snapshot_sha256: str
    snapshot_mode: int
    readback_verified: bool
    readback_at: str
    schema: Literal["chitra.operating-facts-provenance.v1"] = PROVENANCE_SCHEMA

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "source_mode": self.source_mode,
            "snapshot_sha256": self.snapshot_sha256,
            "snapshot_mode": self.snapshot_mode,
            "readback_verified": self.readback_verified,
            "readback_at": self.readback_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> OperatingFactsProvenance:
        if not isinstance(payload, Mapping) or payload.get("schema") != PROVENANCE_SCHEMA:
            raise ValueError("operating facts provenance has the wrong schema")
        text_fields = ("source_path", "source_sha256", "snapshot_sha256", "readback_at")
        if any(not isinstance(payload.get(field), str) or not str(payload[field]).strip() for field in text_fields):
            raise ValueError("operating facts provenance text fields are required")
        for field in ("source_sha256", "snapshot_sha256"):
            value = str(payload[field])
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"operating facts provenance {field} must be a SHA-256 hex digest")
        for field in ("source_mode", "snapshot_mode"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"operating facts provenance {field} must be a positive mode")
        if not isinstance(payload.get("readback_verified"), bool) or not payload["readback_verified"]:
            raise ValueError("operating facts provenance readback must be verified")
        _parse_timestamp(str(payload["readback_at"]), "operating_facts.provenance.readback_at")
        return cls(
            source_path=str(payload["source_path"]),
            source_sha256=str(payload["source_sha256"]),
            source_mode=int(payload["source_mode"]),
            snapshot_sha256=str(payload["snapshot_sha256"]),
            snapshot_mode=int(payload["snapshot_mode"]),
            readback_verified=True,
            readback_at=str(payload["readback_at"]),
        )


@dataclass(frozen=True, slots=True)
class OperatingFactsBinding:
    """Immutable receipt attached to a provider or route selection."""

    digest: str
    deadline: str
    source_path: str
    source_sha256: str
    source_mode: int
    snapshot_mode: int
    target_host: str | None = None
    target_account: str | None = None


def bind_current_operating_facts(
    snapshot: OperatingFactsSnapshot,
    *,
    now: datetime | None = None,
    provider_kind: str | None = None,
) -> OperatingFactsBinding | None:
    """Return a binding only when every Fleet category is actionable.

    Chitra does not fill gaps from host names, environment variables, or
    routing configuration. A category with a missing, stale, conflicting,
    inaccessible, unauthorized, or incomplete value fails closed.
    """

    current = _now(now)
    for category in _CATEGORIES:
        records = snapshot.by_category(category)
        if not records or any(not record.is_current(now=current) for record in records):
            return None
        if any(not isinstance(record.value, Mapping) or not record.value for record in records):
            return None
    binding = snapshot.binding(now=current, provider_kind=provider_kind)
    if binding is None or not binding.target_host or not binding.target_account:
        return None

    placement = snapshot.get("fleet.placement")
    routing = snapshot.get("fleet.routing")
    placement_value = placement.value if placement is not None and isinstance(placement.value, Mapping) else {}
    routing_value = routing.value if routing is not None and isinstance(routing.value, Mapping) else {}
    dispatch_target = routing_value.get("dispatch_target")
    dispatch_target = dispatch_target if isinstance(dispatch_target, Mapping) else {}
    placement_host = placement_value.get("host")
    routing_host = dispatch_target.get("host")
    if provider_kind == "amp":
        orb_surface = (
            snapshot.get("fleet.provider-capabilities").value
            if snapshot.get("fleet.provider-capabilities") is not None
            and isinstance(snapshot.get("fleet.provider-capabilities").value, Mapping)
            else {}
        )
        orb = orb_surface.get("orb_lane_surface")
        orb_target = orb.get("target_machine") if isinstance(orb, Mapping) else None
        if isinstance(placement_host, str) and isinstance(orb_target, str) and placement_host != orb_target:
            return None
    elif not isinstance(routing_host, str) or not routing_host:
        return None
    return binding


def _core_payload(schema: str, observed_at: str, facts: Sequence[OperatingFact]) -> dict[str, object]:
    return {"schema": schema, "observed_at": observed_at, "facts": [fact.to_dict() for fact in facts]}


@dataclass(frozen=True, slots=True)
class OperatingFactsSnapshot:
    """Validated Chitra operating-facts snapshot."""

    observed_at: str
    facts: tuple[OperatingFact, ...]
    schema: Literal["chitra.operating-facts.v1"] = OPERATING_FACTS_SCHEMA
    provenance: OperatingFactsProvenance | None = None

    def to_dict(self) -> dict[str, object]:
        payload = _core_payload(self.schema, self.observed_at, self.facts)
        if self.provenance is not None:
            payload["provenance"] = self.provenance.to_dict()
        return payload

    @property
    def content_digest(self) -> str:
        """Digest of the facts content, excluding publisher provenance."""

        return hashlib.sha256(
            _canonical(_core_payload(self.schema, self.observed_at, self.facts)).encode("utf-8")
        ).hexdigest()

    def binding(self, *, now: datetime | None = None, provider_kind: str | None = None) -> OperatingFactsBinding | None:
        """Return the receipt required to bind a route or provider action."""

        provenance = self.provenance
        if provenance is None:
            return None
        current = _now(now)
        deadlines = [
            _parse_timestamp(fact.fresh_until, "fact.fresh_until")
            for fact in self.facts
            if fact.fresh_until is not None and fact.is_current(now=current)
        ]
        if not deadlines:
            return None
        placement = self.get("fleet.placement")
        routing = self.get("fleet.routing")
        placement_value = placement.value if placement is not None and isinstance(placement.value, Mapping) else {}
        routing_value = routing.value if routing is not None and isinstance(routing.value, Mapping) else {}
        target = routing_value.get("dispatch_target")
        target_map = target if isinstance(target, Mapping) else {}
        if provider_kind == "amp":
            host = placement_value.get("host")
            account = placement_value.get("account")
            if not isinstance(account, str) or not account:
                account = target_map.get("user") if isinstance(target_map.get("user"), str) else None
        else:
            host = target_map.get("host")
            account = target_map.get("user")
            if not isinstance(host, str) or not host:
                host = placement_value.get("host") if isinstance(placement_value.get("host"), str) else None
            if not isinstance(account, str) or not account:
                account = placement_value.get("account") if isinstance(placement_value.get("account"), str) else None
        return OperatingFactsBinding(
            digest=f"sha256:{provenance.snapshot_sha256}",
            deadline=_iso(min(deadlines)),
            source_path=provenance.source_path,
            source_sha256=provenance.source_sha256,
            source_mode=provenance.source_mode,
            snapshot_mode=provenance.snapshot_mode,
            target_host=host if isinstance(host, str) else None,
            target_account=account if isinstance(account, str) else None,
        )

    @classmethod
    def from_dict(cls, payload: object) -> OperatingFactsSnapshot:
        if not isinstance(payload, Mapping):
            raise ValueError("operating facts document must be an object")
        if set(payload) not in (_BASE_SNAPSHOT_KEYS, _SNAPSHOT_KEYS):
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
        provenance_payload = payload.get("provenance")
        provenance = None if provenance_payload is None else OperatingFactsProvenance.from_dict(provenance_payload)
        snapshot = cls(observed_at=observed_at, facts=facts, provenance=provenance)
        if provenance is not None and provenance.snapshot_sha256 != snapshot.content_digest:
            raise ValueError("operating facts provenance does not match snapshot content")
        return snapshot

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
    provenance: OperatingFactsProvenance | None = None


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


def _read_nofollow_regular(path: Path) -> tuple[bytes, int]:
    """Read one regular file without following a symlink replacement."""

    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError("source must be a regular, non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            raise OSError("opened source is not a regular, non-symlink file")
        if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
            raise OSError("source changed before it was opened")
        opened_mode = stat.S_IMODE(opened.st_mode)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read()
            after = os.fstat(handle.fileno())
        if (
            opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or opened.st_size != after.st_size
            or stat.S_IMODE(after.st_mode) != opened_mode
        ):
            raise OSError("source changed while it was read")
        return data, opened_mode
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _read_source(
    path: Path,
    category: FactCategory,
    *,
    require_provenance: bool = False,
    trusted_source: Path | None = None,
) -> _SourceResult:
    try:
        raw, snapshot_mode = _read_nofollow_regular(path)
    except FileNotFoundError:
        return _SourceResult(None, "missing", "source file does not exist")
    except OSError as exc:
        return _SourceResult(None, "inaccessible", str(exc))

    try:
        payload = json.loads(raw.decode("utf-8"))
        snapshot = OperatingFactsSnapshot.from_dict(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return _SourceResult(None, "inaccessible", f"invalid operating-facts snapshot: {exc}")

    if require_provenance and snapshot.provenance is None:
        return _SourceResult(None, "inaccessible", "production facts lack publisher provenance")
    if snapshot.provenance is not None and snapshot.provenance.snapshot_mode != snapshot_mode:
        return _SourceResult(None, "inaccessible", "facts snapshot mode differs from its publisher receipt")
    if snapshot.provenance is not None:
        provenance = snapshot.provenance
        source_path = Path(provenance.source_path)
        if trusted_source is not None:
            if source_path.absolute() != trusted_source.absolute():
                return _SourceResult(None, "inaccessible", "facts provenance names an untrusted Fleet source")
        try:
            source_raw, source_mode = _read_nofollow_regular(source_path)
        except FileNotFoundError:
            return _SourceResult(None, "inaccessible", "facts provenance source file does not exist")
        except OSError as exc:
            return _SourceResult(None, "inaccessible", f"cannot read facts provenance source: {exc}")
        if source_mode != provenance.source_mode:
            return _SourceResult(None, "inaccessible", "facts source mode differs from its publisher receipt")
        if hashlib.sha256(source_raw).hexdigest() != provenance.source_sha256:
            return _SourceResult(None, "inaccessible", "facts source bytes differ from its publisher receipt")

    prefix = _CATEGORY_PREFIX[category]
    selected = tuple(
        fact for fact in snapshot.facts if fact.name == prefix or fact.name.startswith(prefix + ".")
    )
    # A Fleet production snapshot is intentionally multi-category.  Select the
    # requested namespace while still rejecting arbitrary unrelated documents.
    recognized = tuple(
        fact for fact in snapshot.facts
        if any(fact.name == known or fact.name.startswith(known + ".") for known in _CATEGORY_PREFIX.values())
    )
    if not selected and not recognized:
        return _SourceResult(None, "inaccessible", f"facts do not belong to {category}")
    return _SourceResult(selected, "known", provenance=snapshot.provenance)


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
    provenance: OperatingFactsProvenance | None = None

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
            result = _read_source(
                path,
                category,
                require_provenance=configured.require_provenance,
                trusted_source=configured.trusted_source,
            )
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
            if provenance is None:
                provenance = result.provenance
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
    return OperatingFactsSnapshot(observed_at=_iso(current), facts=facts, provenance=provenance)


__all__ = [
    "FactCategory",
    "OPERATING_FACTS_SCHEMA",
    "OperatingFactsBinding",
    "OperatingFactsProvenance",
    "OperatingFactsSnapshot",
    "OperatingFactsSources",
    "PRODUCTION_OPERATING_FACTS_PATH",
    "PRODUCTION_OPERATING_FACTS_INPUTS_PATH",
    "bind_current_operating_facts",
    "production_operating_facts_sources",
    "read_operating_facts",
]

"""Deterministic tests for the explicit Chitra operating-facts boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from chitra.operating_facts import (
    OperatingFactsSnapshot,
    OperatingFactsSources,
    bind_current_operating_facts,
    read_operating_facts,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
OBSERVED = "2026-08-23T11:55:00Z"


def _fact(
    name: str,
    value: object,
    *,
    source: str,
    state: str = "known",
    observed_at: str = OBSERVED,
    freshness: str = "current",
    fresh_until: str | None = "2026-08-23T12:30:00Z",
    within_authority: bool = True,
) -> dict[str, object]:
    return {
        "name": name,
        "value": value,
        "state": state,
        "source": source,
        "revision": "r1",
        "observed_at": observed_at,
        "freshness": freshness,
        "fresh_until": fresh_until,
        "within_authority": within_authority,
    }


def _write_snapshot(path: Path, facts: list[dict[str, object]], *, observed_at: str = OBSERVED) -> Path:
    path.write_text(
        json.dumps({"schema": "chitra.operating-facts.v1", "observed_at": observed_at, "facts": facts}),
        encoding="utf-8",
    )
    return path


def test_reads_named_records_for_all_operating_categories(tmp_path: Path) -> None:
    placement = _write_snapshot(
        tmp_path / "placement.json",
        [_fact("fleet.placement.tophand", {"host": "tophand"}, source="fleet-roster")],
    )
    routing = _write_snapshot(
        tmp_path / "routing.json",
        [_fact("fleet.routing.build", {"provider": "tophand"}, source="fleet-routing")],
    )
    credentials = _write_snapshot(
        tmp_path / "credentials.json",
        [_fact("fleet.credential-readiness.tophand", {"ready": True}, source="fleet-credential-readiness")],
    )
    access = _write_snapshot(
        tmp_path / "access.json",
        [_fact("fleet.access.tophand", {"read_only": True}, source="fleet-access-policy")],
    )
    capacity = _write_snapshot(
        tmp_path / "capacity.json",
        [_fact("fleet.capacity.tophand", {"slots": 2}, source="fleet-capacity")],
    )

    snapshot = read_operating_facts(
        OperatingFactsSources(
            placement=placement,
            routing=routing,
            credential_readiness=credentials,
            access=access,
            capacity=capacity,
        ),
        now=NOW,
    )

    assert snapshot.get("fleet.placement.tophand").value == {"host": "tophand"}  # type: ignore[union-attr]
    assert snapshot.get("fleet.routing.build").value == {"provider": "tophand"}  # type: ignore[union-attr]
    assert snapshot.get("fleet.credential-readiness.tophand").value == {"ready": True}  # type: ignore[union-attr]
    assert snapshot.get("fleet.access.tophand").value == {"read_only": True}  # type: ignore[union-attr]
    assert snapshot.get("fleet.capacity.tophand").value == {"slots": 2}  # type: ignore[union-attr]
    assert all(snapshot.get(name).state == "known" for name in (
        "fleet.placement.tophand", "fleet.routing.build", "fleet.credential-readiness.tophand",
        "fleet.access.tophand", "fleet.capacity.tophand",
    ))


def test_production_binding_requires_receipt_and_binds_target(tmp_path: Path) -> None:
    values = {
        "fleet.placement": {"host": "twinridge", "account": "chitra"},
        "fleet.routing": {"dispatch_target": {"host": "twinridge", "user": "chitra"}},
        "fleet.credential-readiness": {"dispatch": {"ready": True}},
        "fleet.access": {"dispatch": {"ready": True}},
        "fleet.capacity": {"slots": 2},
        "fleet.versions": {"chitra": "0.15.0"},
        "fleet.provider-capabilities": {"tophand": {"send": True}},
    }
    facts = [_fact(name, value, source="fleet-authority") for name, value in values.items()]
    source = tmp_path / "operating-facts.json"
    base = OperatingFactsSnapshot.from_dict(
        {"schema": "chitra.operating-facts.v1", "observed_at": OBSERVED, "facts": facts}
    )
    payload = base.to_dict()
    approved_inputs = tmp_path / "approved-inputs.json"
    approved_bytes = json.dumps(
        {"fixture": "operating-facts", "facts": facts}, sort_keys=True, separators=(",", ":")
    ).encode()
    approved_inputs.write_bytes(approved_bytes)
    payload["provenance"] = {
        "schema": "chitra.operating-facts-provenance.v1",
        "source_path": str(approved_inputs),
        "source_sha256": hashlib.sha256(approved_bytes).hexdigest(),
        "source_mode": 420,
        "snapshot_sha256": base.content_digest,
        "snapshot_mode": 420,
        "readback_verified": True,
        "readback_at": OBSERVED,
    }
    source.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = read_operating_facts(
        OperatingFactsSources(
            placement=source,
            routing=source,
            credential_readiness=source,
            access=source,
            capacity=source,
            versions=source,
            provider_capabilities=source,
            require_provenance=True,
        ),
        now=NOW,
    )
    binding = bind_current_operating_facts(snapshot, now=NOW)
    assert binding is not None
    assert binding.target_host == "twinridge"
    assert binding.target_account == "chitra"
    assert binding.digest == f"sha256:{base.content_digest}"


def test_required_provenance_fails_closed(tmp_path: Path) -> None:
    source = _write_snapshot(
        tmp_path / "legacy.json",
        [_fact("fleet.placement", {"host": "twinridge"}, source="legacy")],
    )
    snapshot = read_operating_facts(
        OperatingFactsSources(placement=source, require_provenance=True),
        now=NOW,
    )
    assert snapshot.get("fleet.placement").state == "inaccessible"  # type: ignore[union-attr]


def test_arbitrary_fleet_shape_is_not_parsed_or_inferred(tmp_path: Path) -> None:
    roster = tmp_path / "fleet.json"
    roster.write_text(
        json.dumps(
            {
                "schema": "fleet.machine-roster.v2",
                "machines": {"tophand": {"tailnet_address": "100.122.81.106"}},
            }
        ),
        encoding="utf-8",
    )

    snapshot = read_operating_facts(OperatingFactsSources(placement=roster), now=NOW)

    assert snapshot.get("fleet.placement.tophand") is None
    placement = snapshot.get("fleet.placement")
    assert placement is not None
    assert placement.state == "inaccessible"


def test_unconfigured_category_is_explicitly_missing(tmp_path: Path) -> None:
    snapshot = read_operating_facts(OperatingFactsSources(), now=NOW)

    for name in (
        "fleet.placement",
        "fleet.routing",
        "fleet.credential-readiness",
        "fleet.access",
        "fleet.capacity",
    ):
        fact = snapshot.get(name)
        assert fact is not None
        assert fact.state == "missing"
        assert fact.value is None


def test_explicit_deadline_turns_known_fact_stale(tmp_path: Path) -> None:
    placement = _write_snapshot(
        tmp_path / "placement.json",
        [
            _fact(
                "fleet.placement.tophand",
                {"host": "tophand"},
                source="fleet-roster",
                fresh_until="2026-08-23T11:00:00Z",
            )
        ],
    )

    snapshot = read_operating_facts(OperatingFactsSources(placement=placement), now=NOW)
    fact = snapshot.get("fleet.placement.tophand")

    assert fact is not None
    assert fact.state == "stale"
    assert fact.freshness == "stale"
    assert fact.is_current(now=NOW) is False


def test_disagreement_between_authoritative_records_is_conflicting(tmp_path: Path) -> None:
    first = _write_snapshot(
        tmp_path / "first.json",
        [_fact("fleet.placement.tophand", {"host": "tophand"}, source="fleet-roster-a")],
    )
    second = _write_snapshot(
        tmp_path / "second.json",
        [_fact("fleet.placement.tophand", {"host": "renegade"}, source="fleet-roster-b")],
    )

    snapshot = read_operating_facts(OperatingFactsSources(placement=(first, second)), now=NOW)
    fact = snapshot.get("fleet.placement.tophand")

    assert fact is not None
    assert fact.state == "conflicting"
    assert fact.within_authority is False
    assert len(fact.value["candidates"]) == 2  # type: ignore[index]


def test_malformed_and_explicit_unavailable_records_remain_visible(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    explicit = _write_snapshot(
        tmp_path / "explicit.json",
        [
            _fact(
                "fleet.routing.build",
                None,
                source="fleet-routing",
                state="inaccessible",
                freshness="unknown",
                fresh_until=None,
                within_authority=False,
            )
        ],
    )

    snapshot = read_operating_facts(OperatingFactsSources(routing=(malformed, explicit)), now=NOW)

    source_fact = snapshot.get("fleet.routing.source.0")
    assert source_fact is not None
    assert source_fact.state == "inaccessible"
    routing = snapshot.get("fleet.routing.build")
    assert routing is not None
    assert routing.state == "inaccessible"


def test_snapshot_round_trip_preserves_contract_records(tmp_path: Path) -> None:
    placement = _write_snapshot(
        tmp_path / "placement.json",
        [_fact("fleet.placement.tophand", {"host": "tophand"}, source="fleet-roster")],
    )
    original = read_operating_facts(OperatingFactsSources(placement=placement), now=NOW)
    restored = OperatingFactsSnapshot.from_dict(original.to_dict())

    assert restored == original


def _write_verified_production_snapshot(tmp_path: Path) -> tuple[Path, Path]:
    values = {
        "fleet.placement": {"host": "twinridge", "account": "chitra"},
        "fleet.routing": {"dispatch_target": {"host": "tophand", "user": "chitra"}},
        "fleet.credential-readiness": {"dispatch": {"ready": True}},
        "fleet.access": {"dispatch": {"ready": True}},
        "fleet.capacity": {"slots": 2},
        "fleet.versions": {"chitra": "0.16.0"},
        "fleet.provider-capabilities": {"tophand": {"send": True}},
    }
    facts = [_fact(name, value, source="fleet-authority") for name, value in values.items()]
    snapshot = tmp_path / "operating-facts.json"
    source = tmp_path / "approved-operating-facts-inputs.json"
    source_bytes = json.dumps(
        {"fixture": "security", "facts": facts}, sort_keys=True, separators=(",", ":")
    ).encode()
    source.write_bytes(source_bytes)
    core = {"schema": "chitra.operating-facts.v1", "observed_at": OBSERVED, "facts": facts}
    payload = {
        **core,
        "provenance": {
            "schema": "chitra.operating-facts-provenance.v1",
            "source_path": str(source),
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "source_mode": 0o644,
            "snapshot_sha256": hashlib.sha256(
                json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "snapshot_mode": 0o644,
            "readback_verified": True,
            "readback_at": OBSERVED,
        },
    }
    snapshot.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    snapshot.chmod(0o644)
    return snapshot, source


def test_provenance_is_verified_against_trusted_source_bytes_and_mode(tmp_path: Path) -> None:
    snapshot, source = _write_verified_production_snapshot(tmp_path)
    sources = OperatingFactsSources(
        placement=snapshot,
        routing=snapshot,
        credential_readiness=snapshot,
        access=snapshot,
        capacity=snapshot,
        versions=snapshot,
        provider_capabilities=snapshot,
        require_provenance=True,
        trusted_source=source,
    )
    assert bind_current_operating_facts(read_operating_facts(sources, now=NOW), now=NOW) is not None

    source.chmod(0o600)
    mode_mismatch = read_operating_facts(sources, now=NOW)
    assert mode_mismatch.get("fleet.placement").state == "inaccessible"  # type: ignore[union-attr]
    source.chmod(0o644)
    source.write_bytes(source.read_bytes() + b" forged")
    forged = read_operating_facts(sources, now=NOW)
    assert forged.get("fleet.placement").state == "inaccessible"  # type: ignore[union-attr]


def test_provenance_rejects_untrusted_source_and_symlink_snapshots(tmp_path: Path) -> None:
    snapshot, source = _write_verified_production_snapshot(tmp_path)
    decoy = tmp_path / "decoy-inputs.json"
    decoy.write_bytes(source.read_bytes())
    strict = OperatingFactsSources(
        placement=snapshot,
        require_provenance=True,
        trusted_source=decoy,
    )
    untrusted = read_operating_facts(strict, now=NOW)
    assert untrusted.get("fleet.placement").state == "inaccessible"  # type: ignore[union-attr]

    symlink = tmp_path / "operating-facts-link.json"
    symlink.symlink_to(snapshot)
    linked = read_operating_facts(
        OperatingFactsSources(placement=symlink, require_provenance=True, trusted_source=source),
        now=NOW,
    )
    assert linked.get("fleet.placement").state == "inaccessible"  # type: ignore[union-attr]

    source_link = tmp_path / "approved-inputs-link.json"
    source_link.symlink_to(source)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["provenance"]["source_path"] = str(source_link)
    snapshot.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    linked_source = read_operating_facts(
        OperatingFactsSources(placement=snapshot, require_provenance=True),
        now=NOW,
    )
    assert linked_source.get("fleet.placement").state == "inaccessible"  # type: ignore[union-attr]

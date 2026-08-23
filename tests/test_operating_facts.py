"""Deterministic tests for the explicit Chitra operating-facts boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from chitra.operating_facts import OperatingFactsSnapshot, OperatingFactsSources, read_operating_facts

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

import json
from datetime import UTC, datetime, timedelta

from chitra.operating_facts import OperatingFactsSources, read_operating_facts


def test_multi_category_snapshot_is_filtered_by_explicit_source_category(tmp_path):
    now = datetime(2026, 8, 23, 15, tzinfo=UTC)
    source = tmp_path / "operating-facts.json"
    fact = lambda name, value, rev: {"name": name, "value": value, "state": "known", "source": "fleet", "revision": rev, "observed_at": now.isoformat(), "freshness": "current", "fresh_until": (now + timedelta(minutes=5)).isoformat(), "within_authority": True}
    source.write_text(json.dumps({"schema": "chitra.operating-facts.v1", "observed_at": now.isoformat(), "facts": [fact("fleet.placement", {"host": "tophand"}, "p1"), fact("fleet.routing", {"route": "local"}, "r1")]}), encoding="utf-8")
    snapshot = read_operating_facts(OperatingFactsSources(placement=source, routing=source), now=now)
    assert snapshot.get("fleet.placement").state == "known"
    assert snapshot.get("fleet.routing").state == "known"

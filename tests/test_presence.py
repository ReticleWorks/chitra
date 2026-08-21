"""Parallel presence stays advisory and isolated by writer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chitra import presence_cli
from chitra.presence import announce_released, announce_using, list_presence, peers_using


def test_three_instances_have_disjoint_files_and_merged_state(tmp_path: Path) -> None:
    announce_using("monitor-a", "repo:a", lanes=["a-1"], root=tmp_path)
    announce_using("monitor-b", "host:b", lanes=["b-1"], root=tmp_path)
    announce_using("monitor-c", "pool:c", lanes=["c-1"], root=tmp_path)

    assert sorted(path.name for path in (tmp_path / "presence").iterdir()) == [
        "monitor-a.jsonl",
        "monitor-b.jsonl",
        "monitor-c.jsonl",
    ]
    assert [(record.instance, record.resource) for record in list_presence(root=tmp_path)] == [
        ("monitor-a", "repo:a"),
        ("monitor-b", "host:b"),
        ("monitor-c", "pool:c"),
    ]
    assert [record.lanes for record in list_presence(root=tmp_path)] == [("a-1",), ("b-1",), ("c-1",)]


def test_same_resource_is_visible_to_every_peer_without_a_claim(tmp_path: Path) -> None:
    resource = "ci-pool:arm64"
    assert announce_using("monitor-a", resource, lanes=["a-1"], root=tmp_path) == []
    assert [record.instance for record in announce_using("monitor-b", resource, lanes=["b-1"], root=tmp_path)] == ["monitor-a"]
    assert [record.instance for record in announce_using("monitor-c", resource, lanes=["c-1"], root=tmp_path)] == [
        "monitor-a",
        "monitor-b",
    ]

    for instance in ("monitor-a", "monitor-b", "monitor-c"):
        assert {record.instance for record in peers_using(instance, resource, root=tmp_path)} == {
            "monitor-a",
            "monitor-b",
            "monitor-c",
        } - {instance}
    assert not list((tmp_path / "presence").glob("*.lock"))


def test_release_is_explicit_and_never_expires_by_age(tmp_path: Path) -> None:
    old = datetime(2000, 1, 1, tzinfo=UTC)
    announce_using("monitor-a", "host:shared", since=old, root=tmp_path)
    assert [record.instance for record in list_presence(root=tmp_path)] == ["monitor-a"]

    announce_released("monitor-a", "host:shared", since=old + timedelta(seconds=1), root=tmp_path)
    assert list_presence(root=tmp_path) == []
    latest = list_presence(root=tmp_path, include_released=True)
    assert len(latest) == 1
    assert latest[0].state == "released"


def test_a_partial_tail_is_not_a_presence_record(tmp_path: Path) -> None:
    announce_using("monitor-a", "repo:a", root=tmp_path)
    with (tmp_path / "presence" / "monitor-a.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"instance":"monitor-a"')
    assert [record.resource for record in list_presence(root=tmp_path)] == ["repo:a"]


def test_presence_cli_uses_the_shared_dir_environment(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHITRA_SHARED_DIR", str(tmp_path))
    assert presence_cli.main(["using", "monitor-a", "repo:shared", "--lane", "a-1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"peers_using": []}

    assert presence_cli.main(["list"]) == 0
    records = json.loads(capsys.readouterr().out)
    assert [(record["instance"], record["resource"]) for record in records] == [("monitor-a", "repo:shared")]

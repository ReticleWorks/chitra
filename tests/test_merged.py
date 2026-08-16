"""Tests for the auto-merge daemon: discovery, one-at-a-time, notification."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from chitra import merged
from chitra.goals import GoalRecord
from chitra.merge import GitHubIdentity, MergeError, MergePolicy

APP = GitHubIdentity(login="polyphony-automation[bot]", kind="app", source="test")

POLICY = MergePolicy(
    allowed_repos=("ReticleWorks/chitra",),
    lane_authors=("lane-bot",),
    hold_label="chitra-hold",
    app_login="polyphony-automation[bot]",
)

PR_URL = "https://github.com/ReticleWorks/chitra/pull/7"


def pr_node(**overrides: object) -> dict[str, object]:
    node: dict[str, object] = {
        "number": 7,
        "title": "a change",
        "url": PR_URL,
        "isDraft": False,
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "author": {"login": "lane-bot"},
        "labels": {"nodes": []},
        "commits": {"nodes": [{"commit": {"oid": "a" * 40, "statusCheckRollup": {"state": "SUCCESS"}}}]},
    }
    node.update(overrides)
    return node


def scripted_runner(listing: list[dict[str, object]], node: dict[str, object] | None = None):
    """A gh runner that answers pr list, graphql, and pr merge from fixtures."""
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        parts = list(command)
        calls.append(parts)
        if parts[1:3] == ["pr", "list"]:
            body = json.dumps(listing)
        elif parts[1:3] == ["api", "graphql"]:
            body = json.dumps({"data": {"repository": {"pullRequest": node or pr_node()}}})
        elif parts[1:3] == ["api", "user"]:
            body = "polyphony-automation[bot]\tBot\n"
        else:
            body = ""
        return subprocess.CompletedProcess(parts, 0, body, "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_discovery_keeps_only_open_lane_authored_non_draft_pull_requests() -> None:
    runner = scripted_runner(
        [
            {"number": 7, "author": {"login": "lane-bot"}, "isDraft": False},
            {"number": 8, "author": {"login": "a-person"}, "isDraft": False},
            {"number": 9, "author": {"login": "lane-bot"}, "isDraft": True},
        ]
    )
    assert merged.discover_pull_requests("ReticleWorks/chitra", POLICY, runner=runner) == [7]


def test_a_failed_listing_raises_rather_than_reporting_an_empty_repository() -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(command), 1, "", "no such repo")

    with pytest.raises(MergeError):
        merged.discover_pull_requests("ReticleWorks/chitra", POLICY, runner=runner)


def test_only_one_pull_request_merges_per_repository_per_pass(tmp_path: Path) -> None:
    runner = scripted_runner(
        [
            {"number": 7, "author": {"login": "lane-bot"}, "isDraft": False},
            {"number": 11, "author": {"login": "lane-bot"}, "isDraft": False},
        ]
    )
    records = merged.process_repo(
        "ReticleWorks/chitra",
        POLICY,
        APP,
        root=tmp_path,
        queue_dir=tmp_path / "queue",
        goals_root=tmp_path / "goals",
        runner=runner,
    )
    assert [record.number for record in records] == [7]
    assert sum(1 for call in runner.calls if call[1:3] == ["pr", "merge"]) == 1  # type: ignore[attr-defined]


def test_a_refused_pull_request_does_not_stop_the_pass(tmp_path: Path) -> None:
    runner = scripted_runner(
        [{"number": 7, "author": {"login": "lane-bot"}, "isDraft": False}],
        node=pr_node(labels={"nodes": [{"name": "chitra-hold"}]}),
    )
    records = merged.process_repo(
        "ReticleWorks/chitra",
        POLICY,
        APP,
        root=tmp_path,
        queue_dir=tmp_path / "queue",
        goals_root=tmp_path / "goals",
        runner=runner,
    )
    assert [record.decision.reason for record in records] == ["hold_label_present"]
    assert not any(call[1:3] == ["pr", "merge"] for call in runner.calls)  # type: ignore[attr-defined]


def test_every_pass_writes_a_ledger_line_whether_or_not_it_merged(tmp_path: Path) -> None:
    runner = scripted_runner([{"number": 7, "author": {"login": "lane-bot"}, "isDraft": False}], node=pr_node(isDraft=True))
    merged.process_repo(
        "ReticleWorks/chitra",
        POLICY,
        APP,
        root=tmp_path,
        queue_dir=tmp_path / "queue",
        goals_root=tmp_path / "goals",
        runner=runner,
    )
    line = json.loads((tmp_path / "merge-ledger.jsonl").read_text(encoding="utf-8").strip())
    assert line["identity"]["login"] == "polyphony-automation[bot]"
    assert line["decision"]["reason"] == "draft"


def goal(session_ref: str, *, now: str = "", source: str = "") -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Land the merge daemon change on the main branch",
        done_when="the pull request is merged and the lane is told",
        source=source or "operator",
        status="working",
        now=now,
    )


def test_the_lane_that_named_the_pull_request_is_the_one_notified() -> None:
    records = [goal("tophand:lane-a:0", now=f"waiting on {PR_URL}"), goal("tophand:lane-b:0", now="something else")]
    assert merged.lane_for_pull_request(records, PR_URL) == "tophand:lane-a:0"


def test_no_lane_is_notified_when_two_lanes_name_the_same_pull_request() -> None:
    records = [goal("tophand:lane-a:0", now=PR_URL), goal("tophand:lane-b:0", now=PR_URL)]
    assert merged.lane_for_pull_request(records, PR_URL) == ""


def test_no_lane_is_notified_when_none_names_the_pull_request() -> None:
    assert merged.lane_for_pull_request([goal("tophand:lane-a:0")], PR_URL) == ""


def test_a_merge_queues_one_dispatch_order_for_the_authoring_lane(tmp_path: Path) -> None:
    from chitra.goals import upsert_goal

    goals_root = tmp_path / "goals"
    upsert_goal(goals_root, goal("tophand:lane-a:0", now=f"waiting on {PR_URL}"))
    runner = scripted_runner([{"number": 7, "author": {"login": "lane-bot"}, "isDraft": False}])
    merged.process_repo(
        "ReticleWorks/chitra",
        POLICY,
        APP,
        root=tmp_path,
        queue_dir=tmp_path / "queue",
        goals_root=goals_root,
        runner=runner,
    )
    orders = list((tmp_path / "queue" / "orders").glob("*.json"))
    assert len(orders) == 1
    order = json.loads(orders[0].read_text(encoding="utf-8"))
    assert order["session_ref"] == "tophand:lane-a:0"
    assert PR_URL in order["nudge"]


def test_an_unreachable_repository_does_not_stop_the_others(tmp_path: Path) -> None:
    policy = MergePolicy(
        allowed_repos=("ReticleWorks/gone", "ReticleWorks/chitra"),
        lane_authors=("lane-bot",),
        app_login=APP.login,
    )

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        parts = list(command)
        if parts[1:3] == ["api", "user"]:
            return subprocess.CompletedProcess(parts, 0, "polyphony-automation[bot]\tBot\n", "")
        if "ReticleWorks/gone" in parts:
            return subprocess.CompletedProcess(parts, 1, "", "not found")
        return scripted_runner([{"number": 7, "author": {"login": "lane-bot"}, "isDraft": False}])(parts)

    records = merged.run_once(policy, root=tmp_path, queue_dir=tmp_path / "queue", goals_root=tmp_path / "g", runner=runner)
    assert [record.repo for record in records] == ["ReticleWorks/chitra"]


def test_an_empty_allowlist_looks_at_nothing(tmp_path: Path) -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("the daemon must not call gh with an empty allowlist")

    assert merged.run_once(MergePolicy(), root=tmp_path, queue_dir=tmp_path / "q", runner=runner) == []

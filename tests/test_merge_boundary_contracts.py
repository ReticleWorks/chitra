"""E2E deny-path coverage for chitra-merge and polyphony-chitra-merged.

G3 replace-then-remove: these tests drive the real ``chitra.merge_cli`` and
``chitra.merged`` entry points as subprocesses. GitHub is stubbed only at the
outermost edge -- a ``gh`` executable on PATH answering scripted responses --
and token minting by a stub credential-helper command. Assertions read the
artifacts the daemons actually write: merge-ledger.jsonl, the repo merge
lock, the dispatch-order queue, stdout JSON, stderr, and exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import filelock
import pytest
from _g3_boundary import (
    fresh_timestamp,
    gh_calls,
    gh_rule,
    graphql_payload,
    green_gh_rules,
    install_gh_shim,
    install_mint_stub,
    json_block,
    merge_policy_yaml,
    read_gh_log,
    read_jsonl,
    run_module,
    write_gh_config,
)
from _goal_fixtures import enrollment_fields

from chitra.goals import GoalRecord, upsert_goal
from chitra.merge import repo_merge_lock

REPO = "ReticleWorks/chitra"
PR_URL = "https://github.com/ReticleWorks/chitra/pull/7"
MERGE_MODULE = "chitra.merge_cli"
MERGED_MODULE = "chitra.merged"


@pytest.fixture()
def rig(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_gh_shim(bin_dir)
    gh_config = tmp_path / "gh-shim.json"
    gh_log = tmp_path / "gh-log.jsonl"
    state_dir = tmp_path / "state"
    queue_dir = tmp_path / "queue"
    goals_root = tmp_path / "goals"

    class Rig:
        pass

    rig_ = Rig()
    rig_.bin_dir = bin_dir
    rig_.gh_config = gh_config
    rig_.gh_log = gh_log
    rig_.state_dir = state_dir
    rig_.queue_dir = queue_dir
    rig_.goals_root = goals_root
    rig_.ledger = state_dir / "merge-ledger.jsonl"

    def env(**extra: str) -> dict[str, str]:
        base = {"GH_SHIM_CONFIG": str(gh_config), "GH_SHIM_LOG": str(gh_log)}
        base.update(extra)
        return base

    rig_.env = env

    def merge_cli(*argv: str, env_extra: dict[str, str] | None = None):
        return run_module(
            MERGE_MODULE,
            list(argv),
            env_extra=env(**(env_extra or {})),
            bin_dir=bin_dir,
        )

    def merged(*argv: str, env_extra: dict[str, str] | None = None):
        return run_module(
            MERGED_MODULE,
            list(argv),
            env_extra=env(**(env_extra or {})),
            bin_dir=bin_dir,
        )

    def write_policy(path: Path, **overrides: object) -> Path:
        path.write_text(merge_policy_yaml(**overrides), encoding="utf-8")
        return path

    rig_.merge_cli = merge_cli
    rig_.merged = merged
    rig_.write_policy = write_policy
    rig_.policy = write_policy(tmp_path / "policy.yaml")
    return rig_


def test_merge_cli_green_path_merges_and_records(tmp_path: Path, rig) -> None:
    """Control case: a fully green lane PR merges, pinned to its head commit,
    and the ledger records the measured merger and resulting merge commit."""
    write_gh_config(rig.gh_config, green_gh_rules())

    result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert result.returncode == 0, result.stderr
    report = json_block(result.stdout)
    assert report["merged"] is True
    assert report["decision"]["reason"] == "ok"
    assert report["head_oid"] == "a" * 40
    # The merge commit is the one GitHub reports, not the merged head.
    assert report["merge_commit"] == "e4823048"
    assert report["merged_by"] == "polyphony-automation[bot]"
    assert report["identity"]["kind"] == "app"

    merge_calls = gh_calls(read_gh_log(rig.gh_log), "pr", "merge")
    assert len(merge_calls) == 1
    command = merge_calls[0]
    # The merge is pinned to the commit the decision was made against.
    assert "--match-head-commit" in command
    assert command[command.index("--match-head-commit") + 1] == "a" * 40

    ledger = read_jsonl(rig.ledger)
    assert len(ledger) == 1
    assert ledger[0]["schema"] == "chitra.merge-ledger.v1"
    assert ledger[0]["merged"] is True
    entries = read_gh_log(rig.gh_log)
    # An installation token cannot call /user; once /installation/repositories
    # answers, /user is never consulted.
    assert gh_calls(entries, "api", "user") == []
    # The decision read mergeStateStatus through GraphQL, not a weaker source.
    graphql_calls = gh_calls(entries, "api", "graphql")
    assert graphql_calls and any("mergeStateStatus" in part for part in graphql_calls[0])


@pytest.mark.parametrize(
    ("node", "reason", "detail_hint"),
    [
        # A draft in a repo that is not allowlisted reports the allowlist, not
        # the draft: configuration is checked before anything about the PR.
        ({"repo_outside": True, "isDraft": True}, "repo_not_allowlisted", "merge allowlist"),
        ({"author": {"login": "a-person"}}, "author_not_a_lane", "not a declared lane author"),
        ({"labels": {"nodes": [{"name": "chitra-hold"}]}}, "hold_label_present", "chitra-hold is set"),
        ({"labels": {"nodes": [{"name": "hold"}]}}, "hold_label_present", "hold is set"),
        ({"labels": {"nodes": [{"name": "enhancement"}, {"name": "hold"}]}}, "hold_label_present", "hold is set"),
        ({"state": "CLOSED"}, "already_closed", "state is CLOSED"),
        ({"isDraft": True}, "draft", "draft"),
        ({"mergeable": "CONFLICTING"}, "not_mergeable", "CONFLICTING"),
        ({"mergeStateStatus": "UNSTABLE"}, "merge_state_not_clean", "UNSTABLE"),
        ({"mergeStateStatus": "BEHIND"}, "merge_state_not_clean", "BEHIND"),
        (
            {"commits": {"nodes": [{"commit": {"oid": "b" * 40, "statusCheckRollup": {"state": "FAILURE"}}}]}},
            "checks_not_successful",
            "FAILURE",
        ),
        (
            {"commits": {"nodes": [{"commit": {"oid": "b" * 40, "statusCheckRollup": {"state": "PENDING"}}}]}},
            "checks_not_successful",
            "PENDING",
        ),
        (
            {"commits": {"nodes": [{"commit": {"oid": "b" * 40, "statusCheckRollup": None}}]}},
            "checks_not_successful",
            "MISSING",
        ),
    ],
    ids=lambda case: case if isinstance(case, str) else "",
)
def test_merge_cli_refuses_each_disqualifying_state(rig, node: dict, reason: str, detail_hint: str) -> None:
    """Every disqualifying state is refused with its own reason, the refusal is
    ledgered with the identity that was refused, and no merge is attempted."""
    repo = "someone/else" if node.pop("repo_outside", False) else REPO
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["api", "graphql"], stdout=graphql_payload(**node)),
        ],
    )

    result = rig.merge_cli(repo, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert result.returncode == 0, result.stderr
    report = json_block(result.stdout)
    assert report["merged"] is False
    assert report["decision"]["reason"] == reason
    assert detail_hint in report["decision"]["detail"]
    assert gh_calls(read_gh_log(rig.gh_log), "pr", "merge") == []
    ledger = read_jsonl(rig.ledger)
    assert len(ledger) == 1
    assert ledger[0]["decision"]["reason"] == reason
    assert ledger[0]["identity"]["login"] == "polyphony-automation[bot]"
    assert ledger[0]["merge_commit"] == ""


def test_merge_cli_an_empty_lane_allowlist_qualifies_nobody(rig, tmp_path: Path) -> None:
    """A misconfigured allowlist must merge nothing: an empty lane list means
    "no author qualifies", not "every author qualifies"."""
    policy = rig.write_policy(tmp_path / "empty-lanes.yaml", lane_authors=[])
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
        ],
    )

    result = rig.merge_cli(REPO, "7", "--policy-config", str(policy), "--state-dir", str(rig.state_dir))

    assert json_block(result.stdout)["decision"]["reason"] == "author_not_a_lane"
    assert gh_calls(read_gh_log(rig.gh_log), "pr", "merge") == []


def test_merge_cli_a_bot_named_user_token_is_still_not_an_app(rig) -> None:
    """A login ending in [bot] proves nothing; only the installation call
    does. A user/Bot token resolves to a non-app identity and is refused."""
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], exit=1, stderr="Resource not accessible by integration"),
            gh_rule(["api", "user"], stdout="something[bot]\tBot\n"),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
        ],
    )

    result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert json_block(result.stdout)["decision"]["reason"] == "identity_not_app"
    assert gh_calls(read_gh_log(rig.gh_log), "pr", "merge") == []


def test_merge_cli_refuses_a_stale_or_unreadable_pull_request(rig) -> None:
    """Freshness is a gate: a PR untouched for days, or one whose updated_at
    cannot be read, is refused as stale rather than assumed wanted."""
    for updated_at in (
        fresh_timestamp(days=5),
        "",
        "not a timestamp",
    ):
        write_gh_config(
            rig.gh_config,
            [
                gh_rule(["api", "/installation/repositories"], stdout="5\n"),
                gh_rule(["api", "graphql"], stdout=graphql_payload(updatedAt=updated_at)),
            ],
        )
        result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))
        assert result.returncode == 0, result.stderr
        assert json_block(result.stdout)["decision"]["reason"] == "stale_pull_request"


def test_merge_cli_refuses_a_dependency_bot_even_when_allowlisted(rig, tmp_path: Path) -> None:
    """The bot check sits before the lane allowlist: adding dependabot to
    lane_authors must not buy it a merge."""
    policy = rig.write_policy(tmp_path / "bot-policy.yaml", lane_authors=["dependabot[bot]"])
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["api", "graphql"], stdout=graphql_payload(author={"login": "dependabot[bot]"})),
        ],
    )

    result = rig.merge_cli(REPO, "7", "--policy-config", str(policy), "--state-dir", str(rig.state_dir))

    assert json_block(result.stdout)["decision"]["reason"] == "author_is_a_bot"
    assert gh_calls(read_gh_log(rig.gh_log), "pr", "merge") == []


def test_merge_cli_refuses_a_personal_token_identity(rig) -> None:
    """A PAT resolves to a user identity and may never merge; the refusal is
    ledgered with the identity that was refused."""
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], exit=1, stderr="Resource not accessible by integration"),
            gh_rule(["api", "user"], stdout="lean-wintermute\tUser\n"),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
        ],
    )

    result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert result.returncode == 0, result.stderr
    report = json_block(result.stdout)
    assert report["decision"]["reason"] == "identity_not_app"
    ledger = read_jsonl(rig.ledger)
    assert ledger[0]["identity"]["login"] == "lean-wintermute"
    assert ledger[0]["identity"]["kind"] == "user"
    assert gh_calls(read_gh_log(rig.gh_log), "pr", "merge") == []


def test_merge_cli_fails_closed_when_identity_is_unreadable(rig) -> None:
    """When neither identity endpoint answers, the CLI fails rather than
    merging as whoever gh happens to be."""
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], exit=1, stderr="denied"),
            gh_rule(["api", "user"], exit=1, stderr="denied"),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
        ],
    )

    result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert result.returncode == 1
    assert read_jsonl(rig.ledger) == []
    assert gh_calls(read_gh_log(rig.gh_log), "pr", "merge") == []


def test_merge_cli_fails_closed_when_the_graphql_read_fails(rig) -> None:
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["api", "graphql"], exit=1, stderr="gone", stdout=graphql_payload()),
        ],
    )

    result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert result.returncode == 1
    assert read_jsonl(rig.ledger) == []


def test_merge_cli_rejects_a_repo_argument_without_owner_and_name(rig) -> None:
    # The rules below answer the calls a missing owner/name split would reach:
    # observable proof that the bad argument is refused before it is used.
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
            gh_rule(["pr", "merge"]),
        ],
    )

    result = rig.merge_cli("chitra", "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert result.returncode == 1
    assert "owner/name" in result.stderr


def test_merge_cli_refuses_to_race_a_held_repo_lock(rig) -> None:
    """A second holder is refused rather than blocked: the CLI reports the
    skip, exits cleanly, and neither merges nor writes a ledger line."""
    lock_dir = rig.state_dir / "merge-locks"
    lock_dir.mkdir(parents=True)
    held = filelock.FileLock(str(lock_dir / "ReticleWorks_chitra.merge.lock"), timeout=0)
    held.acquire()
    try:
        write_gh_config(rig.gh_config, green_gh_rules())
        result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))
    finally:
        held.release()

    assert result.returncode == 0, result.stderr
    assert json_block(result.stdout)["skipped"] == "another merge holds this repo"
    assert read_jsonl(rig.ledger) == []
    assert gh_calls(read_gh_log(rig.gh_log), "pr", "merge") == []


def test_merge_cli_a_lock_on_another_repo_does_not_hold_this_one(rig, tmp_path: Path) -> None:
    """Locks are per repository: a merge in flight for repo X must not hold
    repo Y hostage."""
    write_gh_config(rig.gh_config, green_gh_rules())
    lock_dir = rig.state_dir / "merge-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    # Held through the real helper so a lock-name collision is observable.
    with repo_merge_lock(lock_dir, "other/repo") as held:
        assert held is True
        result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert json_block(result.stdout)["merged"] is True


def test_merge_cli_dry_run_decides_and_ledgers_but_never_merges(rig) -> None:
    write_gh_config(rig.gh_config, green_gh_rules())

    result = rig.merge_cli(REPO, "7", "--dry-run", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert result.returncode == 0, result.stderr
    report = json_block(result.stdout)
    assert report["dry_run"] is True
    assert report["merged"] is False
    assert report["decision"]["reason"] == "ok"
    assert gh_calls(read_gh_log(rig.gh_log), "pr", "merge") == []
    assert read_jsonl(rig.ledger)[0]["dry_run"] is True


def test_merge_cli_fails_when_github_rejects_the_merge(rig) -> None:
    """A refused merge is a hard failure, not a reported success."""
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
            gh_rule(["pr", "merge"], exit=1, stderr="head commit moved"),
        ],
    )

    result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert result.returncode == 1
    assert "head commit moved" in result.stderr
    ledger = read_jsonl(rig.ledger)
    assert ledger == [] or all(not line["merged"] for line in ledger)


def test_merge_cli_records_a_merge_even_when_the_outcome_read_fails(rig) -> None:
    """The merge already happened; an unreadable outcome leaves merged=true
    with empty outcome fields rather than undoing the record."""
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
            gh_rule(["pr", "merge"]),
            gh_rule(["/repos/ReticleWorks/chitra/pulls/7"], exit=1, stderr="rate limited"),
        ],
    )

    result = rig.merge_cli(REPO, "7", "--policy-config", str(rig.policy), "--state-dir", str(rig.state_dir))

    assert result.returncode == 0, result.stderr
    report = json_block(result.stdout)
    assert report["merged"] is True
    assert report["merged_by"] == ""
    assert report["merge_commit"] == ""


# ---------------------------------------------------------------------------
# polyphony-chitra-merged: the daemon pass through its real entry point.
# ---------------------------------------------------------------------------


def _daemon_args(rig, policy: Path, *extra: str) -> list[str]:
    return [
        "--once",
        "--policy-config",
        str(policy),
        "--state-dir",
        str(rig.state_dir),
        "--queue-dir",
        str(rig.queue_dir),
        "--goals-root",
        str(rig.goals_root),
        *extra,
    ]


def _minted_daemon_policy(rig, tmp_path: Path, mint: Path, **overrides: object) -> Path:
    policy = rig.write_policy(tmp_path / "daemon-policy.yaml", **overrides)
    # Append the token command as a real credential-helper command line.
    policy.write_text(policy.read_text(encoding="utf-8") + f"  token_command: [{mint}]\n", encoding="utf-8")
    return policy


def test_merged_merges_one_green_pr_and_notifies_only_the_naming_lane(rig, tmp_path: Path) -> None:
    """One merge per repo per pass; the dispatch order goes only to the one
    lane whose goal names the pull request."""
    mint = install_mint_stub(rig.bin_dir)
    policy = _minted_daemon_policy(rig, tmp_path, mint)
    upsert_goal(
        rig.goals_root,
        GoalRecord(
            session_ref="localhost:lane-a:0",
            goal="Land the merge daemon change on the main branch",
            done_when="the pull request is merged and the lane is told",
            source="operator",
            status="working",
            now=f"waiting on {PR_URL}",
            **enrollment_fields("the pull request is merged and the lane is told"),
        ),
    )
    upsert_goal(
        rig.goals_root,
        GoalRecord(
            session_ref="localhost:lane-b:0",
            goal="Land the other change on the main branch",
            done_when="the other pull request is merged and the lane is told",
            source="operator",
            status="working",
            now="working on something else entirely",
            **enrollment_fields("the other pull request is merged and the lane is told"),
        ),
    )
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(
                ["pr", "list"],
                stdout=json.dumps(
                    [
                        {"number": 7, "author": {"login": "lane-bot"}, "isDraft": False},
                        {"number": 11, "author": {"login": "lane-bot"}, "isDraft": False},
                    ]
                ),
            ),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
            gh_rule(["pr", "merge"]),
            gh_rule(["api", "/repos/ReticleWorks/chitra/pulls/7"], stdout="polyphony-automation[bot]\te4823048\n"),
        ],
    )

    result = rig.merged(*_daemon_args(rig, policy))

    assert result.returncode == 0, result.stderr
    # One merge per repository per pass: PR #11 is left for the next pass.
    assert len(gh_calls(read_gh_log(rig.gh_log), "pr", "merge")) == 1
    ledger = read_jsonl(rig.ledger)
    assert [line["number"] for line in ledger] == [7]
    # The minted token reached gh through the environment, never argv.
    entries = read_gh_log(rig.gh_log)
    assert entries and all(entry["gh_token"] == "ghs_fake_installation_token" for entry in entries)
    assert all("ghs_fake_installation_token" not in " ".join(entry["argv"]) for entry in entries)
    # Exactly one dispatch order, to the lane whose goal names the PR.
    orders = list((rig.queue_dir / "orders").glob("*.json"))
    assert len(orders) == 1
    order = json.loads(orders[0].read_text(encoding="utf-8"))
    assert order["session_ref"] == "localhost:lane-a:0"
    assert PR_URL in order["nudge"]


def test_merged_queues_no_order_when_two_lanes_name_the_pull_request(rig, tmp_path: Path) -> None:
    mint = install_mint_stub(rig.bin_dir)
    policy = _minted_daemon_policy(rig, tmp_path, mint)
    for lane in ("localhost:lane-a:0", "localhost:lane-b:0"):
        upsert_goal(
            rig.goals_root,
            GoalRecord(
                session_ref=lane,
                goal="Land the merge daemon change on the main branch",
                done_when="the pull request is merged and the lane is told",
                source="operator",
                status="working",
                now=f"waiting on {PR_URL}",
                **enrollment_fields("the pull request is merged and the lane is told"),
            ),
        )
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["pr", "list"], stdout=json.dumps([{"number": 7, "author": {"login": "lane-bot"}, "isDraft": False}])),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
            gh_rule(["pr", "merge"]),
            gh_rule(["api", "/repos/ReticleWorks/chitra/pulls/7"], stdout="polyphony-automation[bot]\te4823048\n"),
        ],
    )

    result = rig.merged(*_daemon_args(rig, policy))

    assert result.returncode == 0, result.stderr
    assert read_jsonl(rig.ledger)[0]["merged"] is True
    orders_dir = rig.queue_dir / "orders"
    assert not orders_dir.exists() or list(orders_dir.glob("*.json")) == []


def test_merged_queues_no_order_when_no_lane_names_the_pull_request(rig, tmp_path: Path) -> None:
    """Zero naming lanes is as ambiguous as two: the merge still happens and is
    ledgered, but no dispatch order is written for a guessed lane."""
    mint = install_mint_stub(rig.bin_dir)
    policy = _minted_daemon_policy(rig, tmp_path, mint)
    upsert_goal(
        rig.goals_root,
        GoalRecord(
            session_ref="localhost:lane-a:0",
            goal="Land the merge daemon change on the main branch",
            done_when="the pull request is merged and the lane is told",
            source="operator",
            status="working",
            now="working on something else entirely",
            **enrollment_fields("the pull request is merged and the lane is told"),
        ),
    )
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["pr", "list"], stdout=json.dumps([{"number": 7, "author": {"login": "lane-bot"}, "isDraft": False}])),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
            gh_rule(["pr", "merge"]),
            gh_rule(["api", "/repos/ReticleWorks/chitra/pulls/7"], stdout="polyphony-automation[bot]\te4823048\n"),
        ],
    )

    result = rig.merged(*_daemon_args(rig, policy))

    assert result.returncode == 0, result.stderr
    assert [line["merged"] for line in read_jsonl(rig.ledger)] == [True]
    assert list((rig.queue_dir / "orders").glob("*.json")) == []


def test_merged_prescreens_non_lane_draft_and_bot_pull_requests(rig, tmp_path: Path) -> None:
    """Discovery skips what cannot qualify: no full state read, no ledger line."""
    mint = install_mint_stub(rig.bin_dir)
    policy = _minted_daemon_policy(rig, tmp_path, mint)
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(
                ["pr", "list"],
                stdout=json.dumps(
                    [
                        {"number": 7, "author": {"login": "a-person"}, "isDraft": False},
                        {"number": 8, "author": {"login": "lane-bot"}, "isDraft": True},
                        {"number": 9, "author": {"login": "dependabot[bot]"}, "isDraft": False},
                    ]
                ),
            ),
        ],
    )

    result = rig.merged(*_daemon_args(rig, policy))

    assert result.returncode == 0, result.stderr
    entries = read_gh_log(rig.gh_log)
    assert gh_calls(entries, "api", "graphql") == []
    assert gh_calls(entries, "pr", "merge") == []
    assert read_jsonl(rig.ledger) == []


def test_merged_ledgers_a_refusal_and_continues_the_pass(rig, tmp_path: Path) -> None:
    """A refused pull request writes a ledger line with its reason and does
    not stop the pass or get merged."""
    mint = install_mint_stub(rig.bin_dir)
    policy = _minted_daemon_policy(rig, tmp_path, mint)
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(
                ["pr", "list"],
                stdout=json.dumps(
                    [
                        {"number": 7, "author": {"login": "lane-bot"}, "isDraft": False},
                        {"number": 8, "author": {"login": "lane-bot"}, "isDraft": False},
                    ]
                ),
            ),
            gh_rule(["api", "graphql", "number=7"], stdout=graphql_payload(labels={"nodes": [{"name": "chitra-hold"}]})),
            gh_rule(["api", "graphql", "number=8"], stdout=graphql_payload(number=8, isDraft=True)),
            gh_rule(["pr", "merge"]),
        ],
    )

    result = rig.merged(*_daemon_args(rig, policy))

    assert result.returncode == 0, result.stderr
    assert gh_calls(read_gh_log(rig.gh_log), "pr", "merge") == []
    ledger = read_jsonl(rig.ledger)
    assert [line["decision"]["reason"] for line in ledger] == ["hold_label_present", "draft"]


def test_merged_an_unreachable_repository_does_not_stop_the_others(rig, tmp_path: Path) -> None:
    mint = install_mint_stub(rig.bin_dir)
    policy = _minted_daemon_policy(rig, tmp_path, mint, allowed_repos=["ReticleWorks/gone", "ReticleWorks/chitra"])
    write_gh_config(
        rig.gh_config,
        [
            gh_rule(["api", "/installation/repositories"], stdout="5\n"),
            gh_rule(["pr", "list", "ReticleWorks/gone"], exit=1, stderr="not found"),
            gh_rule(["pr", "list"], stdout=json.dumps([{"number": 7, "author": {"login": "lane-bot"}, "isDraft": False}])),
            gh_rule(["api", "graphql"], stdout=graphql_payload()),
            gh_rule(["pr", "merge"]),
            gh_rule(["api", "/repos/ReticleWorks/chitra/pulls/7"], stdout="polyphony-automation[bot]\te4823048\n"),
        ],
    )

    result = rig.merged(*_daemon_args(rig, policy))

    assert result.returncode == 0, result.stderr
    ledger = read_jsonl(rig.ledger)
    assert [line["repo"] for line in ledger] == ["ReticleWorks/chitra"]
    assert ledger[0]["merged"] is True


def test_merged_an_unusable_credential_fails_the_unit(rig, tmp_path: Path) -> None:
    """A token command that cannot mint exits the daemon non-zero instead of
    looping forever looking alive."""
    policy = rig.write_policy(tmp_path / "bad-cred.yaml", token_command=["/usr/bin/false"])
    write_gh_config(rig.gh_config, green_gh_rules())

    result = rig.merged(*_daemon_args(rig, policy))

    assert result.returncode == 1
    assert read_jsonl(rig.ledger) == []


def test_merged_enabled_with_no_allowlisted_repository_fails_closed(rig, tmp_path: Path) -> None:
    """Enabled merge with an empty repo allowlist is a configuration fault:
    the daemon exits non-zero and GitHub is never queried."""
    policy = rig.write_policy(tmp_path / "empty.yaml", allowed_repos=[])
    write_gh_config(rig.gh_config, green_gh_rules())

    result = rig.merged(*_daemon_args(rig, policy))

    assert result.returncode != 0
    entries = read_gh_log(rig.gh_log)
    assert gh_calls(entries, "api", "graphql") == []
    assert gh_calls(entries, "pr", "merge") == []
    assert read_jsonl(rig.ledger) == []


def test_merged_disabled_exits_cleanly_without_touching_github(rig, tmp_path: Path) -> None:
    policy = rig.write_policy(tmp_path / "disabled.yaml", enabled=False)
    write_gh_config(rig.gh_config, green_gh_rules())

    result = rig.merged(*_daemon_args(rig, policy))

    assert result.returncode == 0, result.stderr
    assert rig.gh_log.exists() is False or read_gh_log(rig.gh_log) == []
    assert read_jsonl(rig.ledger) == []

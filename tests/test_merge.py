"""Tests for the merge decision, the GraphQL read, the lock, and the ledger."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra.merge import (
    GitHubIdentity,
    MergeError,
    MergePolicy,
    MergeRecord,
    append_merge_record,
    decide,
    fetch_state,
    merge,
    repo_merge_lock,
    resolve_identity,
)

NOW = datetime(2026, 8, 16, 22, 30, tzinfo=UTC)

APP = GitHubIdentity(login="polyphony-automation[bot]", kind="app", source="test")
PAT = GitHubIdentity(login="lean-wintermute", kind="user", source="test")

POLICY = MergePolicy(
    allowed_repos=("ReticleWorks/chitra",),
    lane_authors=("lane-bot",),
    hold_labels=("chitra-hold", "hold"),
    app_login="polyphony-automation[bot]",
)


def make_state(**overrides: object):
    from chitra.merge import PullRequestState

    base: dict[str, object] = {
        "repo": "ReticleWorks/chitra",
        "number": 7,
        "title": "a change",
        "url": "https://github.com/ReticleWorks/chitra/pull/7",
        "author": "lane-bot",
        "is_draft": False,
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "checks_rollup": "SUCCESS",
        "head_oid": "a" * 40,
        "labels": (),
        # Keep the default fixture fresh for tests that intentionally omit an
        # explicit `now`; the production freshness bound must stay narrow.
        "updated_at": datetime.now(UTC).isoformat(),
    }
    base.update(overrides)
    return PullRequestState(**base)  # type: ignore[arg-type]


def fake_runner(stdout: str = "", returncode: int = 0, stderr: str = ""):
    calls: list[Sequence[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), returncode, stdout, stderr)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_a_fully_green_lane_pull_request_is_allowed() -> None:
    decision = decide(make_state(), POLICY, APP)
    assert decision.allowed
    assert decision.reason == "ok"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"repo": "someone/else"}, "repo_not_allowlisted"),
        ({"author": "a-person"}, "author_not_a_lane"),
        ({"labels": ("chitra-hold",)}, "hold_label_present"),
        # The label ReticleWorks/chitra actually uses. A brake wired only to
        # chitra-hold would not have stopped anything in that repository.
        ({"labels": ("hold",)}, "hold_label_present"),
        ({"labels": ("enhancement", "hold")}, "hold_label_present"),
        ({"state": "CLOSED"}, "already_closed"),
        ({"is_draft": True}, "draft"),
        ({"mergeable": "CONFLICTING"}, "not_mergeable"),
        ({"merge_state_status": "UNSTABLE"}, "merge_state_not_clean"),
        ({"merge_state_status": "BEHIND"}, "merge_state_not_clean"),
        ({"checks_rollup": "FAILURE"}, "checks_not_successful"),
        ({"checks_rollup": "PENDING"}, "checks_not_successful"),
        ({"checks_rollup": "MISSING"}, "checks_not_successful"),
    ],
)
def test_each_disqualifying_state_is_refused_with_its_own_reason(overrides: dict[str, object], reason: str) -> None:
    assert decide(make_state(**overrides), POLICY, APP).reason == reason


def test_a_personal_access_token_may_not_merge_even_when_everything_else_is_green() -> None:
    decision = decide(make_state(), POLICY, PAT)
    assert not decision.allowed
    assert decision.reason == "identity_not_app"


def test_an_app_with_the_wrong_login_may_not_merge() -> None:
    other_app = GitHubIdentity(login="some-other-app[bot]", kind="app", source="test")
    assert decide(make_state(), POLICY, other_app).reason == "identity_not_app"


def test_the_shipped_hold_labels_cover_the_convention_repositories_actually_use() -> None:
    """Found the hard way: chitra labels a held pull request `hold`, and the
    original default was `chitra-hold`, a label that repository does not have.
    """
    from chitra.merge import DEFAULT_HOLD_LABELS

    assert "hold" in DEFAULT_HOLD_LABELS
    assert "chitra-hold" in DEFAULT_HOLD_LABELS


def test_the_reported_hold_reason_names_the_label_that_was_actually_set() -> None:
    decision = decide(make_state(labels=("hold",)), POLICY, APP)
    assert decision.reason == "hold_label_present"
    assert decision.detail.startswith("hold is set")


def test_an_unrelated_label_does_not_hold_a_pull_request() -> None:
    assert decide(make_state(labels=("enhancement", "python")), POLICY, APP).allowed


def test_a_minted_token_reaches_gh_through_the_environment_not_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token in argv shows up in any process list on the host."""
    import chitra.merge as merge_module

    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        if list(command) == ["mint-it"]:
            return subprocess.CompletedProcess(command, 0, "protocol=https\npassword=ghs_secret\n", "")
        seen["command"] = list(command)
        seen["env_token"] = (kwargs.get("env") or {}).get("GH_TOKEN")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(merge_module.subprocess, "run", fake_run)
    merge_module.minting_gh_runner(["mint-it"])(["gh", "api", "user"])

    assert seen["env_token"] == "ghs_secret"
    assert "ghs_secret" not in " ".join(seen["command"])  # type: ignore[arg-type]


def test_a_failed_mint_raises_rather_than_merging_as_whoever_gh_is(monkeypatch: pytest.MonkeyPatch) -> None:
    import chitra.merge as merge_module

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 1, "", "1Password is locked")

    monkeypatch.setattr(merge_module.subprocess, "run", fake_run)
    with pytest.raises(MergeError, match="could not mint"):
        merge_module.minting_gh_runner(["mint-it"])(["gh", "api", "user"])


def test_a_dependency_bot_is_refused_even_when_fully_green() -> None:
    """From a real overnight merge: five bot pull requests landed on green,
    one a major version bump from 1.28.1 to 2.0.0. Green CI is evidence the
    tests passed, never evidence a major bump is runtime-safe.
    """
    decision = decide(make_state(author="dependabot[bot]"), POLICY, APP)
    assert decision.reason == "author_is_a_bot"


def test_a_bot_is_refused_even_if_someone_allowlists_it() -> None:
    """The bot check sits before the allowlist on purpose.

    Merging without review rests on a lane having asked for it. A dependency
    bot never asked, so putting its login in lane_authors must not buy it a
    merge.
    """
    permissive = MergePolicy(
        allowed_repos=("ReticleWorks/chitra",),
        lane_authors=("dependabot[bot]",),
        app_login=APP.login,
    )
    assert decide(make_state(author="dependabot[bot]"), permissive, APP).reason == "author_is_a_bot"


def test_a_pull_request_nobody_has_touched_for_days_is_refused() -> None:
    """From a real overnight merge: one open about five days landed on green.
    Green and mergeable describe the branch, not whether anyone still wants it.
    """
    stale = make_state(updated_at="2026-08-11T00:00:00Z")
    decision = decide(stale, POLICY, APP, now=NOW)
    assert decision.reason == "stale_pull_request"
    assert "still wanted" in decision.detail


def test_a_pull_request_touched_today_passes_the_freshness_bound() -> None:
    assert decide(make_state(updated_at="2026-08-16T20:00:00Z"), POLICY, APP, now=NOW).allowed


def test_an_unreadable_last_updated_time_is_refused_rather_than_assumed_fresh() -> None:
    """Unknown is not zero. A freshness gate must not pass on missing data."""
    for value in ("", "not a timestamp", "2026-08-16T20:00:00"):
        assert decide(make_state(updated_at=value), POLICY, APP, now=NOW).reason == "stale_pull_request"


def test_the_freshness_bound_is_configurable_and_defaults_to_a_day() -> None:
    from chitra.merge import DEFAULT_MAX_AGE_HOURS

    assert DEFAULT_MAX_AGE_HOURS == 24.0
    wide = MergePolicy(
        allowed_repos=("ReticleWorks/chitra",),
        lane_authors=("lane-bot",),
        app_login=APP.login,
        max_age_hours=24 * 7,
    )
    assert decide(make_state(updated_at="2026-08-11T00:00:00Z"), wide, APP, now=NOW).allowed


def test_an_empty_lane_allowlist_qualifies_nobody() -> None:
    empty = MergePolicy(allowed_repos=("ReticleWorks/chitra",), app_login=APP.login)
    assert decide(make_state(), empty, APP).reason == "author_not_a_lane"


def test_configuration_is_reported_before_anything_about_the_pull_request() -> None:
    # A draft in a repo that is not allowlisted reports the allowlist, not the
    # draft: the daemon was never entitled to an opinion on that repository.
    assert decide(make_state(repo="someone/else", is_draft=True), POLICY, APP).reason == "repo_not_allowlisted"


def graphql_payload(**pr: object) -> str:
    node: dict[str, object] = {
        "number": 7,
        "title": "a change",
        "url": "https://github.com/ReticleWorks/chitra/pull/7",
        "isDraft": False,
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "updatedAt": "2026-08-16T22:00:00Z",
        "author": {"login": "lane-bot"},
        "labels": {"nodes": [{"name": "enhancement"}]},
        "commits": {"nodes": [{"commit": {"oid": "b" * 40, "statusCheckRollup": {"state": "SUCCESS"}}}]},
    }
    node.update(pr)
    return json.dumps({"data": {"repository": {"pullRequest": node}}})


def test_fetch_state_reads_the_graphql_fields_the_decision_depends_on() -> None:
    runner = fake_runner(stdout=graphql_payload())
    state = fetch_state("ReticleWorks/chitra", 7, runner=runner)
    assert state.merge_state_status == "CLEAN"
    assert state.checks_rollup == "SUCCESS"
    assert state.head_oid == "b" * 40
    assert state.labels == ("enhancement",)
    assert any("mergeStateStatus" in part for part in runner.calls[0])  # type: ignore[attr-defined]


def test_a_pull_request_with_no_checks_at_all_reads_as_missing_not_success() -> None:
    payload = graphql_payload(commits={"nodes": [{"commit": {"oid": "c" * 40, "statusCheckRollup": None}}]})
    state = fetch_state("ReticleWorks/chitra", 7, runner=fake_runner(stdout=payload))
    assert state.checks_rollup == "MISSING"
    assert decide(state, POLICY, APP, now=NOW).reason == "checks_not_successful"


def test_a_failed_graphql_query_raises_rather_than_returning_a_default_state() -> None:
    runner = fake_runner(returncode=1, stderr="gone")
    with pytest.raises(MergeError):
        fetch_state("ReticleWorks/chitra", 7, runner=runner)


def test_a_repo_without_a_slash_is_rejected() -> None:
    with pytest.raises(MergeError):
        fetch_state("chitra", 7, runner=fake_runner(stdout=graphql_payload()))


def api_runner(responses: dict[str, tuple[int, str, str]]):
    """A gh runner that answers per API path, defaulting to a 403."""
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        parts = list(command)
        calls.append(parts)
        path = parts[2] if len(parts) > 2 else ""
        code, out, err = responses.get(path, (1, "", "Resource not accessible by integration"))
        return subprocess.CompletedProcess(parts, code, out, err)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_an_installation_token_resolves_to_an_app_identity() -> None:
    """An installation token is recognised by an endpoint it can actually call.

    It cannot call /user at all: GitHub answers 403 there. Asking /user first
    would fail closed on the one identity the daemon requires.
    """
    runner = api_runner({"/installation/repositories": (0, "20\n", "")})
    identity = resolve_identity(expected_app_login="polyphony-automation[bot]", runner=runner)
    assert identity.is_app
    assert identity.login == "polyphony-automation[bot]"
    assert "installation token confirmed" in identity.source
    # /user is never consulted once the installation endpoint answers.
    assert not any(call[2] == "user" for call in runner.calls)  # type: ignore[attr-defined]


def test_an_app_identity_says_its_login_came_from_policy_not_a_reading() -> None:
    identity = resolve_identity(
        expected_app_login="polyphony-automation[bot]", runner=api_runner({"/installation/repositories": (0, "1\n", "")})
    )
    assert "login from policy" in identity.source


def test_a_human_token_resolves_to_a_user_identity() -> None:
    runner = api_runner({"user": (0, "lean-wintermute\tUser\n", "")})
    identity = resolve_identity(runner=runner)
    assert identity.kind == "user"
    assert not identity.is_app


def test_a_bot_looking_user_token_is_still_not_an_app() -> None:
    """A login ending in [bot] proves nothing; only the installation call does."""
    runner = api_runner({"user": (0, "something[bot]\tBot\n", "")})
    assert not resolve_identity(runner=runner).is_app


def test_an_unreadable_identity_raises_rather_than_guessing() -> None:
    with pytest.raises(MergeError):
        resolve_identity(runner=api_runner({}))


def test_the_repo_lock_refuses_a_second_holder_instead_of_blocking(tmp_path: Path) -> None:
    with repo_merge_lock(tmp_path, "ReticleWorks/chitra") as first:
        assert first
        with repo_merge_lock(tmp_path, "ReticleWorks/chitra") as second:
            assert not second
    with repo_merge_lock(tmp_path, "ReticleWorks/chitra") as third:
        assert third


def test_two_different_repositories_do_not_block_each_other(tmp_path: Path) -> None:
    with repo_merge_lock(tmp_path, "ReticleWorks/chitra") as first, repo_merge_lock(tmp_path, "ReticleWorks/other") as second:
        assert first and second


def test_a_refusal_is_recorded_with_the_identity_that_was_refused(tmp_path: Path) -> None:
    record = merge(make_state(is_draft=True), POLICY, PAT, runner=fake_runner())
    ledger = tmp_path / "merge-ledger.jsonl"
    append_merge_record(ledger, record)
    line = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert line["merged"] is False
    assert line["identity"]["login"] == "lean-wintermute"
    assert line["identity"]["kind"] == "user"
    assert line["schema"] == "chitra.merge-ledger.v1"


def test_a_dry_run_decides_but_never_calls_gh_pr_merge() -> None:
    runner = fake_runner()
    record = merge(make_state(), POLICY, APP, dry_run=True, runner=runner)
    assert record.decision.allowed
    assert record.merged is False
    assert record.dry_run is True
    assert runner.calls == []  # type: ignore[attr-defined]


def test_a_merge_is_pinned_to_the_commit_the_decision_was_made_against() -> None:
    runner = fake_runner()
    record = merge(make_state(), POLICY, APP, runner=runner)
    assert record.merged
    command = runner.calls[0]  # type: ignore[attr-defined]
    assert "--match-head-commit" in command
    assert command[command.index("--match-head-commit") + 1] == "a" * 40


def test_a_merge_records_who_github_says_actually_merged_it() -> None:
    """The one identity in the record that is measured, not configured."""
    runner = fake_runner(stdout="polyphony-automation[bot]\te4823048\n")
    record = merge(make_state(), POLICY, APP, runner=runner)
    assert record.merged_by == "polyphony-automation[bot]"
    assert record.to_dict()["merged_by"] == "polyphony-automation[bot]"


def test_the_recorded_merge_commit_is_the_resulting_one_not_the_merged_head() -> None:
    """A squash merge creates a new commit, so the head is not the merge commit.

    Recording the head under that name would put a sha in the ledger that does
    not exist on the base branch. Observed on the first real merge.
    """
    runner = fake_runner(stdout="polyphony-automation[bot]\te4823048\n")
    record = merge(make_state(), POLICY, APP, runner=runner)
    assert record.head_oid == "a" * 40
    assert record.merge_commit == "e4823048"


def test_an_unreadable_outcome_does_not_undo_a_merge_that_happened() -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        parts = list(command)
        # The merge succeeds; only the read-back afterwards fails.
        if parts[1:3] == ["api", "/repos/ReticleWorks/chitra/pulls/7"]:
            return subprocess.CompletedProcess(parts, 1, "", "rate limited")
        return subprocess.CompletedProcess(parts, 0, "", "")

    record = merge(make_state(), POLICY, APP, runner=runner)
    assert record.merged is True
    assert record.merged_by == ""
    assert record.merge_commit == ""


def test_a_refusal_records_no_outcome_because_nothing_happened() -> None:
    record = merge(make_state(is_draft=True), POLICY, APP, runner=fake_runner())
    assert record.merged is False
    assert record.merged_by == ""
    assert record.merge_commit == ""


def test_a_rejected_merge_raises_instead_of_reporting_success() -> None:
    runner = fake_runner(returncode=1, stderr="head commit moved")
    with pytest.raises(MergeError):
        merge(make_state(), POLICY, APP, runner=runner)


def test_the_ledger_appends_rather_than_replacing(tmp_path: Path) -> None:
    ledger = tmp_path / "merge-ledger.jsonl"
    for number in (1, 2):
        append_merge_record(
            ledger,
            MergeRecord(
                repo="ReticleWorks/chitra",
                number=number,
                url="",
                author="lane-bot",
                head_oid="",
                decision=decide(make_state(), POLICY, APP),
                identity=APP,
                merged=False,
                merge_commit="",
                dry_run=True,
            ),
        )
    assert len(ledger.read_text(encoding="utf-8").strip().splitlines()) == 2

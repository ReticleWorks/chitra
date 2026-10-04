"""Held unit guard: the one merge deny path unreachable through the real CLI.

``decide`` refuses an app identity whose login differs from ``policy.app_login``.
The real daemon can never produce that state: ``resolve_identity`` derives the
recorded login from the policy's configured expectation (its ``source`` field
says so plainly), so the ``identity.login != policy.app_login`` arm exists only
as defence in depth inside ``decide``. No process boundary can reach it, so it
keeps its unit test while every other deny path moved to
``test_merge_boundary_contracts.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from chitra.merge import GitHubIdentity, MergePolicy, decide

NOW = datetime.now(UTC)


def timestamp_ago(delta: timedelta) -> str:
    return (NOW - delta).isoformat().replace("+00:00", "Z")


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
        "updated_at": timestamp_ago(timedelta(minutes=30)),
    }
    base.update(overrides)
    return PullRequestState(**base)  # type: ignore[arg-type]


def test_an_app_with_the_wrong_login_may_not_merge() -> None:
    other_app = GitHubIdentity(login="some-other-app[bot]", kind="app", source="test")
    assert decide(make_state(), POLICY, other_app).reason == "identity_not_app"

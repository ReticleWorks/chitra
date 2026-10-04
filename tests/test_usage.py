"""Fail-closed account verdicts in chitra.usage (wave-1 burn-down keeps).

What stays is exactly the SOL finding #6 regression pair: ``evaluate_grouped``
must never merge two unknown (blank-account) sessions into one bucket, and
must keep sharing one verdict across the sessions of a real account. The rest
of this file was snapshot-shaping internals over synthetic snapshots; the
usage CLI itself remains covered by tests/test_usage_export.py.
"""

from __future__ import annotations

from chitra.policy_config import UsagePolicy
from chitra.usage import UsageSnapshot, UsageWindow, evaluate_grouped

_DEFAULT_FIVE_HOUR = UsageWindow(10, 1_700_000_100)
_DEFAULT_SEVEN_DAY = UsageWindow(10, 1_700_000_200)


def _snapshot(
    *,
    five_hour: UsageWindow | None = _DEFAULT_FIVE_HOUR,
    seven_day: UsageWindow | None = _DEFAULT_SEVEN_DAY,
    ts: str = "2026-07-10T12:00:00+00:00",
    session_id: str = "lane-1",
    account: str = "",
) -> UsageSnapshot:
    return UsageSnapshot(
        kind="claude",
        ts=ts,
        session_id=session_id,
        tmux_session="fleet-1",
        five_hour=five_hour,
        seven_day=seven_day,
        account=account,
    )


def test_evaluate_grouped_fails_closed_on_unknown_account_never_merging_unrelated_unknowns() -> None:
    """Regression for SOL finding #6: two unrelated sessions that both have
    an unknown (blank) account identity must never be merged into one
    account group. Before this fix, ``evaluate_grouped`` grouped by the raw
    (possibly empty) account string, so one hot fresh unknown-identity
    session could pause every unrelated unknown-identity sibling."""
    hot_unknown = _snapshot(session_id="hot-unknown", account="", five_hour=UsageWindow(99, 10))
    other_unknown = _snapshot(session_id="other-unknown", account="", five_hour=UsageWindow(5, 10))
    grouped = evaluate_grouped([(hot_unknown, True), (other_unknown, True)], policy=UsagePolicy())
    by_session = {item.session_id: item for item in grouped}

    assert by_session["hot-unknown"].level == "pause"
    assert by_session["other-unknown"].level == "ok"  # must never inherit the unrelated session's pause verdict
    assert by_session["hot-unknown"].account == ""
    assert by_session["other-unknown"].account == ""


def test_evaluate_grouped_still_shares_a_verdict_across_the_same_real_account() -> None:
    """The fail-closed isolation is specific to the unknown (blank) account
    -- two sessions sharing a REAL, known account identity still correctly
    share one account-level verdict, exactly as before."""
    hot = _snapshot(session_id="hot-real", account="real@example.com", five_hour=UsageWindow(99, 10))
    sibling = _snapshot(session_id="sibling-real", account="real@example.com", five_hour=UsageWindow(5, 10))
    grouped = evaluate_grouped([(hot, True), (sibling, True)], policy=UsagePolicy())
    assert {item.level for item in grouped} == {"pause"}

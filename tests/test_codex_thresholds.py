"""Tests for the Codex threshold ladder and reset-horizon window identity.

The shape under test was measured, not imagined. Read live on tophand
2026-08-16 with ``chitra-usage codex-snapshot``, a capped Codex account
returned its *weekly* cap in the ``primary`` slot -- 100% used, resetting
2026-08-20T03:37:33Z -- with ``secondary`` null. chitra maps ``primary`` to
``five_hour``, so a weekly threshold applied by slot name would have watched an
empty slot and never fired.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chitra.policy_config import UsagePolicy
from chitra.usage import UsageSnapshot, UsageWindow, effective_windows, evaluate

READING = datetime(2026, 8, 16, 15, 30, tzinfo=UTC)
READING_EPOCH = int(READING.timestamp())
# Two hours out: inside the six-hour horizon, so a short window.
SHORT_RESET = READING_EPOCH + 2 * 3600
# The real reset the capped account reported, three and a half days out.
WEEKLY_RESET = 1_787_197_053
POLICY = UsagePolicy()


def _codex(
    *,
    primary: UsageWindow | None,
    secondary: UsageWindow | None = None,
) -> UsageSnapshot:
    return UsageSnapshot(
        kind="codex",
        ts=READING.isoformat(),
        session_id="codex-account",
        tmux_session="",
        five_hour=primary,
        seven_day=secondary,
        account="infra@example.com",
    )


def _claude(*, five_hour: UsageWindow | None, seven_day: UsageWindow | None) -> UsageSnapshot:
    return UsageSnapshot(
        kind="claude",
        ts=READING.isoformat(),
        session_id="lane-1",
        tmux_session="fleet-1",
        five_hour=five_hour,
        seven_day=seven_day,
        account="agent@example.com",
    )


def test_a_weekly_cap_reported_in_the_primary_slot_is_a_weekly_window() -> None:
    """The measured tophand shape: weekly cap in primary, secondary null."""
    snapshot = _codex(primary=UsageWindow(pct=100.0, resets_at=WEEKLY_RESET))

    short, long_window = effective_windows(snapshot)

    assert short is None
    assert long_window is not None
    assert long_window.resets_at == WEEKLY_RESET


def test_that_weekly_cap_pauses_against_the_weekly_threshold() -> None:
    snapshot = _codex(primary=UsageWindow(pct=100.0, resets_at=WEEKLY_RESET))

    verdict = evaluate(snapshot, policy=POLICY)

    assert verdict.level == "pause"
    assert verdict.binding_window == "7d"
    assert verdict.resume_at_epoch == WEEKLY_RESET


def test_the_codex_weekly_pause_bites_before_the_claude_seven_day_pause() -> None:
    """91% is past Codex's weekly line and short of Claude's seven-day line.

    This is the margin the design bought: a Codex weekly cap is a hard wall
    with a multi-day reset, so it pauses at 90 rather than 95.
    """
    windows = {"five_hour": None, "seven_day": UsageWindow(pct=91.0, resets_at=WEEKLY_RESET)}

    codex = evaluate(_codex(primary=None, secondary=windows["seven_day"]), policy=POLICY)
    claude = evaluate(_claude(five_hour=None, seven_day=windows["seven_day"]), policy=POLICY)

    assert codex.level == "pause"
    assert claude.level == "approaching"


def test_codex_weekly_warns_at_its_own_line() -> None:
    snapshot = _codex(primary=None, secondary=UsageWindow(pct=86.0, resets_at=WEEKLY_RESET))

    verdict = evaluate(snapshot, policy=POLICY)

    assert verdict.level == "approaching"
    assert verdict.binding_window == "7d"
    # A warning names no resume time: nothing is paused yet.
    assert verdict.resume_at_epoch == 0


def test_a_genuine_codex_five_hour_window_still_uses_the_five_hour_ladder() -> None:
    snapshot = _codex(primary=UsageWindow(pct=93.0, resets_at=SHORT_RESET))

    verdict = evaluate(snapshot, policy=POLICY)

    assert verdict.level == "pause"
    assert verdict.binding_window == "5h"
    assert verdict.resume_at_epoch == SHORT_RESET


def test_the_more_severe_window_binds_when_both_are_present() -> None:
    snapshot = _codex(
        primary=UsageWindow(pct=81.0, resets_at=SHORT_RESET),
        secondary=UsageWindow(pct=95.0, resets_at=WEEKLY_RESET),
    )

    verdict = evaluate(snapshot, policy=POLICY)

    assert verdict.level == "pause"
    assert verdict.binding_window == "7d"


def test_claude_windows_are_never_reclassified() -> None:
    """Claude's slots mean what they say, whatever their reset horizons."""
    snapshot = _claude(
        five_hour=UsageWindow(pct=10.0, resets_at=WEEKLY_RESET),
        seven_day=UsageWindow(pct=20.0, resets_at=SHORT_RESET),
    )

    short, long_window = effective_windows(snapshot)

    assert short is snapshot.five_hour
    assert long_window is snapshot.seven_day


def test_the_claude_ladder_is_unchanged() -> None:
    for pct, expected in ((96.0, "pause"), (91.0, "approaching"), (50.0, "ok")):
        snapshot = _claude(five_hour=None, seven_day=UsageWindow(pct=pct, resets_at=WEEKLY_RESET))
        assert evaluate(snapshot, policy=POLICY).level == expected


def test_a_quiet_codex_account_is_ok() -> None:
    snapshot = _codex(
        primary=UsageWindow(pct=12.0, resets_at=SHORT_RESET),
        secondary=UsageWindow(pct=34.0, resets_at=WEEKLY_RESET),
    )

    assert evaluate(snapshot, policy=POLICY).level == "ok"


@pytest.mark.parametrize(
    "overrides",
    [
        {"codex_warn_weekly_pct": 95.0, "codex_pause_weekly_pct": 90.0},
        {"codex_warn_5h_pct": 95.0, "codex_pause_5h_pct": 92.0},
        {"codex_pause_weekly_pct": 0.0},
        {"codex_pause_5h_pct": 101.0},
    ],
)
def test_an_inverted_or_impossible_codex_ladder_is_refused(overrides: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        UsagePolicy(**overrides)


def test_auto_transfer_defaults_on_and_is_configurable() -> None:
    assert UsagePolicy().auto_transfer is True
    assert UsagePolicy(auto_transfer=False).auto_transfer is False


def test_threshold_accessors_split_by_provider() -> None:
    policy = UsagePolicy()

    assert policy.pause_thresholds("codex") == (92.0, 90.0)
    assert policy.pause_thresholds("claude") == (92.0, 95.0)
    assert policy.warn_thresholds("codex") == (80.0, 85.0)
    assert policy.warn_thresholds("claude") == (80.0, 90.0)

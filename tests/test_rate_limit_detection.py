"""Regression tests for provider rate-limit detection on live panes.

The two fixtures replay the screen of ``tophand:gct-secret-broker`` on
2026-08-14, the Codex lane that hit its weekly hard cap and then sat dead for
roughly two days because the monitor read it as an ordinary quiet pane. Every
signature in them is quoted from that lane's own transcript
(``governed-lanes/tophand/gct-secret-broker/tmux-transcript.log``; the banner is
at line 10234).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import (
    ManifestRepository,
    classify_snapshot,
    parse_resume_at,
)
from chitra.triaged import critical_hits, parse_event_line
from chitra.watchd import status_event_line

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HARD_CAP = (FIXTURES / "codex_weekly_hard_cap_20260814.txt").read_text(encoding="utf-8")
WARNING = (FIXTURES / "codex_rate_limit_warning_20260814.txt").read_text(encoding="utf-8")

# The lane ran on Tophand, whose local zone was EDT that week. The banner
# carries no zone of its own, so the test pins one rather than inheriting the
# runner's.
LANE_TIMEZONE = "America/New_York"


@pytest.fixture
def lane_timezone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", LANE_TIMEZONE)
    time.tzset()
    yield
    time.tzset()


@pytest.fixture
def repository(tmp_path: Path) -> ManifestRepository:
    """Force the bundled manifests, never a local override on the runner."""
    return ManifestRepository(tmp_path / "no-local-overrides")


def test_hard_cap_banner_is_not_read_as_idle(repository: ManifestRepository, lane_timezone: None) -> None:
    explain = classify_snapshot(HARD_CAP, agent="codex", repository=repository)

    assert explain.state == "rate_limited_hard"
    assert explain.matched_rule == "rate_limit_hard_cap"
    assert explain.authority == "manifest"
    assert explain.resume_at == "2026-08-20T03:37:00Z"


def test_the_capped_pane_would_otherwise_have_matched_idle(repository: ManifestRepository, lane_timezone: None) -> None:
    """This is the whole defect: the input row is still on screen."""
    explain = classify_snapshot(HARD_CAP, agent="codex", repository=repository)

    matched = {evaluation.rule_id for evaluation in explain.evaluated_rules if evaluation.matched}
    assert "input_row" in matched
    assert explain.state == "rate_limited_hard"


def test_warning_chooser_is_its_own_state(repository: ManifestRepository, lane_timezone: None) -> None:
    explain = classify_snapshot(WARNING, agent="codex", repository=repository)

    assert explain.state == "rate_limited_warn"
    assert explain.matched_rule == "rate_limit_warning"
    # Only the hard cap carries a resume time; the warning names no window.
    assert explain.resume_at is None


def test_an_ordinary_working_pane_is_untouched(repository: ManifestRepository, lane_timezone: None) -> None:
    snapshot = "• Working (12s · esc to interrupt)\n›\n"

    assert classify_snapshot(snapshot, agent="codex", repository=repository).state == "working"


def test_prose_about_a_cap_does_not_read_as_a_cap(repository: ManifestRepository, lane_timezone: None) -> None:
    """An agent writing about the incident must not classify as capped.

    The signatures are anchored at the line start, and the statusline form
    requires its own ``(/goal resume)`` hint, so ordinary sentences that
    mention the banner do not match.
    """
    snapshot = (
        "• A Codex lane hit your usage limit last week and nobody noticed.\n"
        "  The transcript said it would try again at some point.\n"
        "›\n"
    )

    assert classify_snapshot(snapshot, agent="codex", repository=repository).state == "idle"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("try again at Aug 19th, 2026 11:37 PM.", "2026-08-20T03:37:00Z"),
        ("try again at August 19, 2026 11:37 PM.", "2026-08-20T03:37:00Z"),
        ("try again at Aug 1st, 2026 12:05 AM.", "2026-08-01T04:05:00Z"),
        ("or try again at 2026-08-19 23:37.", "2026-08-20T03:37:00Z"),
    ],
)
def test_resume_time_is_parsed_from_the_banner(text: str, expected: str, lane_timezone: None) -> None:
    assert parse_resume_at(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your usage limit.",
        "try again at some point.",
        "try again at Smarch 41st, 2026 25:99 PM.",
        "",
    ],
)
def test_an_unreadable_resume_time_is_absent_rather_than_invented(text: str, lane_timezone: None) -> None:
    assert parse_resume_at(text) is None


def test_the_cap_reaches_triaged_as_a_critical_event(tmp_path: Path, lane_timezone: None) -> None:
    """End to end: capped pane in, CRIT with a resume time out.

    The banner scrolls away, so if the resume time does not survive onto the
    event line the response protocol has nothing to hold the lane against.
    """
    broker = AgentStatusBroker(tmp_path, ManifestRepository(tmp_path / "no-local-overrides"))
    event = broker.observe(
        pane_id="%12",
        target="gct-secret-broker:0.0",
        session_ref="tophand:gct-secret-broker:0.0",
        lane_id="%12",
        detected_agent="codex",
        snapshot=HARD_CAP,
        tmux_socket=None,
    )
    assert event is not None
    assert event.pane.state == "rate_limited_hard"

    parsed = parse_event_line(status_event_line(event.pane))
    assert parsed is not None
    _timestamp, lane_id, text = parsed

    assert lane_id == "%12"
    assert "state=rate_limited_hard" in text
    assert "resume_at=2026-08-20T03:37:00Z" in text
    assert [rule for rule, _ in critical_hits(text)] == ["rate_limited_hard"]


def test_triaged_classifies_a_hard_cap_as_critical() -> None:
    line = (
        "AGENT_STATUS state=rate_limited_hard resume_at=2026-08-20T03:37:00Z pane_id=%12 "
        "target=gct-secret-broker:0.0 agent=codex authority=manifest source=package:codex.toml "
        "rule=rate_limit_hard_cap fallback=none"
    )

    hits = critical_hits(line)

    assert [rule for rule, _ in hits] == ["rate_limited_hard"]


def test_triaged_leaves_a_warning_at_normal_priority() -> None:
    line = (
        "AGENT_STATUS state=rate_limited_warn pane_id=%12 target=gct-secret-broker:0.0 "
        "agent=codex authority=manifest source=package:codex.toml rule=rate_limit_warning fallback=none"
    )

    assert critical_hits(line) == []

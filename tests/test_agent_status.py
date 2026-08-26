from __future__ import annotations

from pathlib import Path

import pytest

from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import (
    DEFAULT_KNOWN_AGENT_IDLE_FALLBACK,
    LIFECYCLE_AUTHORITY_SKIP_REASON,
    MANIFEST_ERROR_IDLE_FALLBACK,
    ManifestError,
    ManifestRepository,
    classify_snapshot,
    parse_manifest,
)


def test_bundled_manifest_is_strict_about_blocked_and_ambiguous_defaults_idle() -> None:
    repository = ManifestRepository()

    blocked = classify_snapshot(
        "Allow command?\n  Yes\n  No\n",
        agent="codex",
        repository=repository,
    )
    ambiguous = classify_snapshot("Something unusual needs attention\n", agent="codex", repository=repository)

    assert blocked.state == "blocked"
    assert blocked.matched_rule == "permission_prompt"
    assert blocked.blocker_kind == "permission"
    assert ambiguous.state == "idle"
    assert ambiguous.fallback_reason == DEFAULT_KNOWN_AGENT_IDLE_FALLBACK


def test_blocked_rule_without_recognized_visible_reason_is_rejected() -> None:
    with pytest.raises(ManifestError, match="require blocker_kind"):
        parse_manifest(
            """
schema_version = 1
agent = "codex"
version = "test"

[[rules]]
id = "too_broad"
state = "blocked"
all = [{ kind = "contains", value = "error" }]
""",
            source="test",
            source_kind="local",
        )


def test_blocked_rule_cannot_search_the_whole_capture_scrollback() -> None:
    with pytest.raises(ManifestError, match="live bottom region"):
        parse_manifest(
            """
schema_version = 1
agent = "codex"
version = "test"

[[rules]]
id = "stale_prompt"
state = "blocked"
region = "whole"
blocker_kind = "approval"
all = [{ kind = "contains", value = "Do you trust this directory?" }]
""",
            source="test",
            source_kind="local",
        )


def test_stale_answered_codex_prompt_with_live_spinner_is_not_blocked() -> None:
    result = classify_snapshot(
        """Do you trust the contents of this directory?
  1. Yes
  2. No
Trust recorded; the task cannot be cancelled now.
• Working (12s • esc to interrupt)
""",
        agent="codex",
        repository=ManifestRepository(),
    )

    assert result.state == "working"
    assert result.matched_rule == "working_spinner"
    assert result.blocker_kind is None


def test_broker_classifies_a_frozen_spinner_below_a_real_prompt_as_blocked(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path, ManifestRepository())
    snapshot = """Do you trust the contents of this directory?
  1. Yes
  2. No
Trust recorded; the task cannot be cancelled now.
• Working (12s • esc to interrupt)
"""
    first = broker.observe(
        pane_id="%1",
        target="lane:0.0",
        session_ref="host:lane:0.0",
        lane_id="lane",
        detected_agent="codex",
        snapshot=snapshot,
        tmux_socket=None,
    )
    second = broker.observe(
        pane_id="%1",
        target="lane:0.0",
        session_ref="host:lane:0.0",
        lane_id="lane",
        detected_agent="codex",
        snapshot=snapshot,
        tmux_socket=None,
    )

    assert first is not None and first.pane.state == "working"
    assert second is not None and second.pane.state == "blocked"
    assert second.pane.explain.blocker_kind == "approval"


def test_broker_keeps_a_changing_spinner_working(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path, ManifestRepository())
    common = """Do you trust the contents of this directory?
  1. Yes
  2. No
Trust recorded; the task cannot be cancelled now.
"""
    snapshots = (common + "• Working (12s • esc to interrupt)\n", common + "• Working (13s • esc to interrupt)\n")

    results = [
        broker.observe(
            pane_id="%1",
            target="lane:0.0",
            session_ref="host:lane:0.0",
            lane_id="lane",
            detected_agent="codex",
            snapshot=snapshot,
            tmux_socket=None,
        )
        for snapshot in snapshots
    ]

    assert results[0] is not None and results[0].pane.state == "working"
    assert broker.statuses()[0].state == "working"


def test_broker_does_not_carry_stability_across_session_identity_change(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path, ManifestRepository())
    snapshot = """Do you trust the contents of this directory?
  1. Yes
  2. No
Trust recorded; the task cannot be cancelled now.
• Working (12s • esc to interrupt)
"""

    def observe(session_ref: str):
        return broker.observe(
            pane_id="%1",
            target=f"{session_ref}:0.0",
            session_ref=session_ref,
            lane_id="lane",
            detected_agent="codex",
            snapshot=snapshot,
            tmux_socket=None,
        )

    observe("host:old-lane")
    changed_session = observe("host:new-lane")
    stable_new_session = observe("host:new-lane")

    assert changed_session is not None and changed_session.pane.state == "working"
    assert stable_new_session is not None and stable_new_session.pane.state == "blocked"


def test_echoed_claude_permission_text_with_live_spinner_is_not_blocked() -> None:
    result = classify_snapshot(
        """I will ask: Do you want to proceed with this change?
Esc to cancel
✻ Working… esc to interrupt
""",
        agent="claude",
        repository=ManifestRepository(),
    )

    assert result.state == "working"
    assert result.matched_rule == "working_spinner"
    assert result.blocker_kind is None


def test_answer_tokens_require_case_sensitive_whole_words() -> None:
    repository = ManifestRepository()

    embedded = classify_snapshot(
        "Do you trust the contents of this directory?\nThe task cannot continue yet.\n",
        agent="codex",
        repository=repository,
    )
    lowercase = classify_snapshot(
        "Do you trust the contents of this directory?\n  1. yes\n  2. no\n",
        agent="codex",
        repository=repository,
    )

    assert embedded.state != "blocked"
    assert lowercase.state != "blocked"


def test_local_manifest_overrides_bundled_and_invalid_override_falls_back_idle(tmp_path: Path) -> None:
    local = tmp_path / "agent-detection"
    local.mkdir()
    path = local / "codex.toml"
    path.write_text(
        """
schema_version = 1
agent = "codex"
version = "local-1"

[[rules]]
id = "local_working"
state = "working"
all = [{ kind = "contains", value = "LOCAL SIGNAL" }]
""",
        encoding="utf-8",
    )
    repository = ManifestRepository(local)

    result = classify_snapshot("LOCAL SIGNAL", agent="codex", repository=repository)
    assert result.state == "working"
    assert result.source_kind == "local"
    assert result.manifest_version == "local-1"

    path.write_text("schema_version = 99\n", encoding="utf-8")
    invalid = classify_snapshot("Allow command?", agent="codex", repository=repository)
    assert invalid.state == "idle"
    assert invalid.fallback_reason == MANIFEST_ERROR_IDLE_FALLBACK
    assert invalid.warning is not None


def test_lifecycle_report_is_authoritative_and_skips_manifest(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path, ManifestRepository())
    broker.report_agent(
        pane_id="%1",
        session_ref="host:lane:0.0",
        source="integration:codex",
        agent="codex",
        state="working",
    )

    broker.observe(
        pane_id="%1",
        target="lane:0.0",
        session_ref="host:lane:0.0",
        lane_id="lane",
        detected_agent="codex",
        snapshot="Allow command?\nYes\nNo\n",
        tmux_socket=tmp_path / "tmux.sock",
    )

    status = broker.statuses()[0]
    assert status.state == "working"
    assert status.authority == "integration"
    assert status.explain.screen_detection_skipped is True
    assert status.explain.screen_detection_skip_reason == LIFECYCLE_AUTHORITY_SKIP_REASON


def test_lifecycle_authority_is_released_on_session_identity_change(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path, ManifestRepository())
    broker.report_agent(
        pane_id="%1",
        session_ref="host:old:0.0",
        source="integration:codex",
        agent="codex",
        state="working",
    )

    broker.observe(
        pane_id="%1",
        target="new:0.0",
        session_ref="host:new:0.0",
        lane_id="new",
        detected_agent="codex",
        snapshot="› Add a task\n",
        tmux_socket=None,
    )

    status = broker.statuses()[0]
    assert status.authority == "manifest"
    assert status.state == "idle"
    assert broker.lifecycle_reports() == ()


def test_done_is_completion_owned_and_plain_idle_does_not_erase_it(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path, ManifestRepository())
    broker.observe(
        pane_id="%1",
        target="lane:0.0",
        session_ref="host:lane:0.0",
        lane_id="lane",
        detected_agent="codex",
        snapshot="Working... esc to interrupt\n",
        tmux_socket=None,
    )
    broker.report_completion(pane_id="%1", session_ref="host:lane:0.0", agent="codex")
    broker.observe(
        pane_id="%1",
        target="lane:0.0",
        session_ref="host:lane:0.0",
        lane_id="lane",
        detected_agent="codex",
        snapshot="› Add a task\n",
        tmux_socket=None,
    )

    assert broker.statuses()[0].state == "done"
    assert broker.statuses()[0].authority == "completion"

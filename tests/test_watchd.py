"""Tests for chitra.watchd's pane normalization and triage handoff."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields, ingest_passing_receipt

from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import ManifestRepository
from chitra.dispatch import DispatchOrder
from chitra.goal_enforcement import ReviewerVerdict, ReviewFinding
from chitra.goals import GoalRecord, get_goal, upsert_goal
from chitra.lane_activity import load_lane_activity
from chitra.triaged import ReceivingOutputs, parse_event_line, run_once
from chitra.watchd import (
    Pane,
    Watchd,
    WatchdConfig,
    _pane_backend,
    append_event,
    build_arg_parser,
    list_panes,
    normalize,
    resolve_config,
    status_event_line,
)


def _completed(command: Sequence[str], stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=list(command), returncode=returncode, stdout=stdout, stderr="")


def test_normalize_removes_input_box_and_volatile_chrome() -> None:
    content = """useful state (12m 4s)
    ✻ thinking
tokens 12,345
Press up to edit
another useful state
❯ operator's unsent input
this is part of the live input box
"""

    assert normalize(content) == ["useful state", "another useful state"]


def test_watchd_emits_semantic_change_but_not_input_box_typing(tmp_path: Path) -> None:
    captures = iter(
        [
            "Working... esc to interrupt\n❯ first operator draft\n",
            "Working... esc to interrupt\n❯ a completely different operator draft\n",
            "Allow command?\nYes\nNo\n❯ operator draft remains unsent\n",
        ]
    )

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%7\tlane:0.0\t1\tcodex\n")
        if command[1] == "capture-pane":
            return _completed(command, next(captures))
        raise AssertionError(f"unexpected command: {command}")

    events_log = tmp_path / "events.log"
    watcher = Watchd(
        WatchdConfig(state_dir=tmp_path, events_log=events_log),
        runner=runner,
    )

    assert watcher.poll_once() == 0  # first capture establishes the baseline
    assert watcher.poll_once() == 0  # operator typing only is not a state change
    assert watcher.poll_once() == 1
    raw_captures = list((tmp_path / "watchd").glob("*.raw"))
    assert len(raw_captures) == 1
    assert "Allow command?" in raw_captures[0].read_text(encoding="utf-8")

    parsed = parse_event_line(events_log.read_text(encoding="utf-8"))
    assert parsed is not None
    _timestamp, lane_id, text = parsed
    assert lane_id == "lane:0.0"
    assert text.startswith("AGENT_STATUS state=blocked needs operator input pane_id=%7")
    assert "authority=manifest" in text
    assert "rule=permission_prompt" in text


def test_watchd_ambiguous_snapshot_defaults_idle_without_delayed_idle_event(tmp_path: Path) -> None:
    now = [100.0]

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tprobe:0.0\t1\tcodex\n")
        if command[1] == "capture-pane":
            return _completed(command, "unrecognized screen shape\n")
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(
        WatchdConfig(
            state_dir=tmp_path,
            events_log=tmp_path / "events.log",
            idle_threshold_seconds=10,
        ),
        runner=runner,
        clock=lambda: now[0],
    )
    assert watcher.poll_once() == 0
    now[0] = 109.0
    assert watcher.poll_once() == 0
    now[0] = 110.0
    assert watcher.poll_once() == 0
    now[0] = 120.0
    assert watcher.poll_once() == 0
    assert not (tmp_path / "events.log").exists()
    assert watcher.status_broker is not None
    status = watcher.status_broker.statuses()[0]
    assert status.state == "idle"
    assert status.explain.fallback_reason == "default_known_agent_idle_fallback"


def test_watchd_semantic_idle_periods_land_twice_in_triaged_queue(tmp_path: Path) -> None:
    now = [100.0]
    content = ["Working... esc to interrupt\n"]

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tprobe:0.0\t1\tcodex\n")
        if command[1] == "capture-pane":
            return _completed(command, content[0])
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(
        WatchdConfig(
            state_dir=tmp_path,
            events_log=tmp_path / "events.log",
            idle_threshold_seconds=10,
        ),
        runner=runner,
        clock=lambda: now[0],
    )
    assert watcher.poll_once() == 0
    content[0] = "status: waiting\n› Add a task\n"
    now[0] = 110.0
    assert watcher.poll_once() == 1

    content[0] = "Working (1m 2s • esc to interrupt)\n"
    now[0] = 120.0
    assert watcher.poll_once() == 1
    content[0] = "status: waiting\n› Add a task\n"
    now[0] = 130.0
    assert watcher.poll_once() == 1
    now[0] = 140.0
    assert watcher.poll_once() == 0
    now[0] = 150.0
    assert watcher.poll_once() == 0

    idle_events = [
        line
        for line in (tmp_path / "events.log").read_text(encoding="utf-8").splitlines()
        if "AGENT_STATUS state=idle" in line
    ]
    assert len(idle_events) == 2

    outputs = ReceivingOutputs(
        queue_file=tmp_path / "queue.tsv",
        flags_file=tmp_path / "flags.log",
        stats_file=tmp_path / "stats.json",
        alert_state_file=tmp_path / "alerts.json",
    )
    assert run_once(
        tmp_path / "events.log",
        state_file=tmp_path / "triaged-state.json",
        triage_log=tmp_path / "triaged.log",
        receiving_outputs=outputs,
    ) == 3
    assert [line.split("\t")[1] for line in outputs.queue_file.read_text(encoding="utf-8").splitlines()] == [
        "INFO",
        "INFO",
        "INFO",
    ]
    assert not outputs.flags_file.exists()


def test_watchd_does_not_emit_idle_without_input_row(tmp_path: Path) -> None:
    now = [100.0]

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tactive:0.0\t1\tcodex\n")
        if command[1] == "capture-pane":
            return _completed(command, "status: working\nRunning tests…\n")
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(
        WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log", idle_threshold_seconds=5),
        runner=runner,
        clock=lambda: now[0],
    )
    assert watcher.poll_once() == 0
    now[0] = 110.0
    assert watcher.poll_once() == 0
    assert not (tmp_path / "events.log").exists()


class _AcceptingReviewer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def review(self, goal, behavior, reviewer_id: str) -> ReviewerVerdict:
        self.calls.append(reviewer_id)
        return ReviewerVerdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,
            behavior_sha256=behavior.behavior_sha256,
            verdict="accept",
        )


class _RejectingReviewer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def review(self, goal, behavior, reviewer_id: str) -> ReviewerVerdict:
        self.calls.append(reviewer_id)
        return ReviewerVerdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,
            behavior_sha256=behavior.behavior_sha256,
            verdict="reject",
            findings=(
                ReviewFinding(
                    code="unsupported_completion",
                    detail="The completion claim lacks the required proof.",
                    citation="The forced completion review was completed and deployed.",
                ),
            ),
        )


def _tracked_goal(root: Path) -> GoalRecord:
    goal = upsert_goal(
        root,
        GoalRecord(
            session_ref="localhost:fleet:0.0",
            intent="Deliver the requested gate while preserving every explicit operator boundary.",
            goal="Build and verify the requested forced completion review.",
            done_when="The live completion probe passes with cited evidence.",
            scope="WS1 source tests and documentation only.",
            source="task-file:/tmp/ws1.md",
            status="working",
            **enrollment_fields("The live completion probe passes with cited evidence."),
        ),
    )
    ingest_passing_receipt(root, goal.session_ref)
    return goal


class _BlockingReviewer:
    """A reviewer whose review() blocks until released, to prove poll_once
    never runs the isolated review inline on the sensing thread."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls: list[str] = []

    def review(self, goal, behavior, reviewer_id: str) -> ReviewerVerdict:
        self.entered.set()
        # Block until the test releases us. If poll_once ran this inline it would
        # deadlock the sensing loop; the test proceeding past poll_once proves
        # the review is off-thread.
        self.release.wait(timeout=30)
        self.calls.append(reviewer_id)
        return ReviewerVerdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,
            behavior_sha256=behavior.behavior_sha256,
            verdict="accept",
        )


_DEPLOY_PROOF_LINE = "CHITRA-COMPLETION: " + json.dumps(
    {
        "kind": "deploy",
        "done_when_item_id": "done-1",
        "receipt_name": "tests-green",
        "validator": "pytest",
        "validator_result": "pass",
        "citation": "deployed SHA abc1234",
    },
    separators=(",", ":"),
)
_LIVE_PROOF_LINE = "CHITRA-COMPLETION: " + json.dumps(
    {
        "kind": "live_verify",
        "done_when_item_id": "done-1",
        "receipt_name": "tests-green",
        "validator": "pytest",
        "validator_result": "pass",
        "citation": "live health probe status=200 with 24 requests; /tmp/live-review.log",
    },
    separators=(",", ":"),
)
_CITED_CLAIM_CAPTURE = f"""The forced completion review was completed and deployed at SHA abc1234.
It reviews every finished lane turn before any done state is trusted.
{_DEPLOY_PROOF_LINE}
{_LIVE_PROOF_LINE}
\u276f
"""


def test_poll_once_does_not_block_on_a_slow_reviewer_and_later_drains_it(tmp_path: Path) -> None:
    goal = _tracked_goal(tmp_path)
    reviewer = _BlockingReviewer()
    state = {"polls": 0}

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\t1\tcodex\n")
        if command[1] == "capture-pane":
            # First capture is a mid-turn baseline; every capture after the
            # baseline is the finished completion-claim turn (stable content).
            content = "working on the implementation\nesc to interrupt\n\u276f\n" if state["polls"] == 0 else _CITED_CLAIM_CAPTURE
            return _completed(command, content)
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(
        WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"),
        runner=runner,
        reviewer=reviewer,
    )
    try:
        # First poll: no completion claim yet, establishes the baseline.
        watcher.poll_once()
        state["polls"] = 1
        # Second poll: completion-claim turn-end. This SUBMITS the review to the
        # executor and returns; if it ran the blocking reviewer inline the call
        # would hang for the reviewer's 30s wait and never return here.
        watcher.poll_once()
        # The review is genuinely running off-thread (it entered review()).
        assert reviewer.entered.wait(timeout=5)
        # ...but it has NOT finalized: the lane is in-flight, not done/blocked.
        stored = get_goal(tmp_path, goal.session_ref)
        assert stored is not None
        assert stored.status == "turn-finished-unverified"
        assert "in flight" in stored.now
        assert reviewer.calls == []  # reviewer still blocked, no verdict yet

        # Release the reviewer; a later poll drains the completed future.
        reviewer.release.set()
        for _ in range(50):
            watcher.poll_once()
            drained = get_goal(tmp_path, goal.session_ref)
            assert drained is not None
            if drained.status == "done-pending-close":
                break
            threading.Event().wait(0.05)
        drained = get_goal(tmp_path, goal.session_ref)
        assert drained is not None
        assert drained.status == "done-pending-close"
    finally:
        reviewer.release.set()
        watcher.shutdown()


def test_turn_end_automatically_runs_review_and_marks_cited_completion_pending_close(tmp_path: Path) -> None:
    goal = _tracked_goal(tmp_path)
    captures = iter(
        [
            "working on the implementation\nesc to interrupt\n❯\n",
            _CITED_CLAIM_CAPTURE,
        ]
    )

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\t1\tcodex\n")
        if command[1] == "capture-pane":
            return _completed(command, next(captures, _CITED_CLAIM_CAPTURE))
        raise AssertionError(f"unexpected command: {command}")

    reviewer = _AcceptingReviewer()
    watcher = Watchd(WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"), runner=runner, reviewer=reviewer)

    assert watcher.poll_once() == 0
    assert watcher.poll_once() == 1

    for _ in range(50):
        stored = get_goal(tmp_path, goal.session_ref)
        assert stored is not None
        if stored.status == "done-pending-close":
            break
        threading.Event().wait(0.01)
        watcher.poll_once()
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "done-pending-close"
    assert stored.last_verified
    assert reviewer.calls == ["reviewer-1-1", "reviewer-1-2"]
    review = json.loads((tmp_path / "completion_reviews.jsonl").read_text(encoding="utf-8"))
    assert review["condition"] == "completion_claim"
    assert review["completion_verdict"] == "CLEAN"


def test_legacy_goal_cannot_reach_done_through_watchd(tmp_path: Path) -> None:
    enrolled = _tracked_goal(tmp_path)
    payload = enrolled.to_dict()
    payload["interview_receipt"] = None
    payload["enrolled_done_when_items"] = []
    payload["completion_proofs"] = []
    (tmp_path / "goals.json").write_text(
        json.dumps({"schema": "chitra.goals.v2", "updated_at": enrolled.updated_at, "goals": [payload]}),
        encoding="utf-8",
    )
    captures = iter(["working\nesc to interrupt\n❯\n", _CITED_CLAIM_CAPTURE])

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\t1\tcodex\n")
        if command[1] == "capture-pane":
            return _completed(command, next(captures, _CITED_CLAIM_CAPTURE))
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"), runner=runner)
    try:
        watcher.poll_once()
        watcher.poll_once()
    finally:
        watcher.shutdown()

    stored = get_goal(tmp_path, enrolled.session_ref)
    assert stored is not None
    assert stored.status == "working"
    review = json.loads((tmp_path / "completion_reviews.jsonl").read_text(encoding="utf-8"))
    assert review["status"] == "unenrolled"
    assert review["completion_verdict"] == "COMPLETION_DISPUTE"


def test_rejected_turn_review_enqueues_reasoned_dispatch(tmp_path: Path) -> None:
    goal = _tracked_goal(tmp_path)
    reviewer = _RejectingReviewer()
    state = {"finished": False}

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\t1\tcodex\n")
        if command[1] == "capture-pane":
            content = _CITED_CLAIM_CAPTURE if state["finished"] else "working on the implementation\nesc to interrupt\n❯\n"
            return _completed(command, content)
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(
        WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"),
        runner=runner,
        reviewer=reviewer,
    )
    try:
        watcher.poll_once()
        state["finished"] = True
        watcher.poll_once()
        for _ in range(50):
            orders = list((tmp_path / "queue" / "orders").glob("*.json"))
            if orders:
                break
            threading.Event().wait(0.05)
            watcher.poll_once()

        assert len(orders) == 1
        order = DispatchOrder.model_validate_json(orders[0].read_text(encoding="utf-8"))
        assert order.session_ref == goal.session_ref
        assert order.message_kind == "reasoned_action"
        assert order.decision_attestation is not None
        assert order.decision_attestation.review_verdict == "reject"
        assert order.decision_attestation.operator_confirmed is True
        assert reviewer.calls == ["reviewer-1-1", "reviewer-1-2"]
    finally:
        watcher.shutdown()


def test_turn_end_without_claim_is_finished_unverified_not_idle_green(tmp_path: Path) -> None:
    goal = _tracked_goal(tmp_path)
    reviewer = _AcceptingReviewer()

    captures = iter(["Working... esc to interrupt\n", "I need the exact release target before continuing.\n❯\n"])

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\t1\tcodex\n")
        if command[1] == "capture-pane":
            return _completed(command, next(captures))
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(
        WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"),
        runner=runner,
        reviewer=reviewer,
    )
    watcher.poll_once()
    watcher.poll_once()

    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "turn-finished-unverified"
    assert "without a completion claim" in stored.now
    assert reviewer.calls == []
    review = json.loads((tmp_path / "completion_reviews.jsonl").read_text(encoding="utf-8"))
    assert review["review_verdict"] == "unavailable"
    assert "isolated review was not run" in review["summary"]


def test_list_panes_uses_live_tmux_enumeration_and_deduplicates_pane_id() -> None:
    seen_commands: list[Sequence[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        seen_commands.append(command)
        return _completed(command, "%1\tfleet:0.0\n%2\tfleet:0.1\n%1\tduplicate:9.9\n")

    assert list_panes(runner=runner) == [Pane(pane_id="%1", target="fleet:0.0"), Pane(pane_id="%2", target="fleet:0.1")]
    assert seen_commands == [
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{session_attached}\t#{pane_current_command}\t#{pane_pipe}",
        ]
    ]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("opencode", "opencode"),
        ("/usr/local/bin/opencode", "opencode"),
        ("opencode-helper", "unknown"),
        ("not-opencode", "unknown"),
        ("claude", "claude"),
        ("codex", "codex"),
    ],
)
def test_pane_backend_recognizes_only_allowlisted_executables(command: str, expected: str) -> None:
    assert _pane_backend(command) == expected


def test_watchd_persists_backend_neutral_change_recency_and_attachment(tmp_path: Path) -> None:
    _tracked_goal(tmp_path)

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\t0\tcodex\n")
        if command[1] == "capture-pane":
            return _completed(command, "working on the requested change\n")
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"), runner=runner)
    watcher.poll_once()

    activity = load_lane_activity(tmp_path)
    assert len(activity) == 1
    assert activity[0].session_ref == "localhost:fleet:0.0"
    assert activity[0].attached is False
    assert activity[0].backend == "codex"
    assert activity[0].last_change_at


def test_watchd_persists_opencode_backend_activity(tmp_path: Path) -> None:
    _tracked_goal(tmp_path)

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\t0\topencode\n")
        if command[1] == "capture-pane":
            return _completed(command, "working on the requested change\n")
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"), runner=runner)
    watcher.poll_once()

    activity = load_lane_activity(tmp_path)
    assert len(activity) == 1
    assert activity[0].backend == "opencode"


def test_list_panes_can_isolate_a_session_namespace() -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return _completed(
            command,
            "%1\tmonitor:0.0\n%2\tboomtown:0.0\n%3\tboomtown-design-a:0.0\n%4\tother:0.0\n",
        )

    assert list_panes(runner=runner, session_prefixes=("boomtown-",)) == [Pane(pane_id="%3", target="boomtown-design-a:0.0")]
    assert list_panes(runner=runner, excluded_session_prefixes=("boomtown",)) == [
        Pane(pane_id="%1", target="monitor:0.0"),
        Pane(pane_id="%4", target="other:0.0"),
    ]


def test_status_event_line_matches_triaged_reader_contract(tmp_path: Path) -> None:
    broker = AgentStatusBroker(tmp_path, ManifestRepository())
    event = broker.report_agent(pane_id="%9", source="test", agent="codex", state="blocked")
    assert event is not None
    line = status_event_line(event.pane)

    parsed = parse_event_line(line)
    assert parsed is not None
    timestamp, lane_id, text = parsed
    assert timestamp.endswith("Z")
    assert lane_id == "%9"
    assert text == (
        "AGENT_STATUS state=blocked needs operator input pane_id=%9 target=%9 "
        "agent=codex authority=integration source=test rule=none fallback=none"
    )


def test_append_event_rotates_at_max_size_under_lock(tmp_path: Path) -> None:
    events_log = tmp_path / "events.log"
    events_log.write_text("old\n", encoding="utf-8")

    append_event(events_log, "new\n", max_log_bytes=4)

    assert (tmp_path / "events.log.1").read_text(encoding="utf-8") == "old\n"
    assert events_log.read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "events.log.lock").exists()


def test_resolve_config_uses_chitra_state_and_watchd_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHITRA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHITRA_WATCHD_INTERVAL", "2.5")
    monkeypatch.setenv("CHITRA_WATCHD_PANES", "%1, %2")
    monkeypatch.setenv("CHITRA_WATCHD_SESSION_PREFIXES", "boomtown-, boomtown-review-")
    monkeypatch.setenv("CHITRA_WATCHD_SESSION_NAMES", "infra-health, atlas-v5")
    monkeypatch.setenv("CHITRA_WATCHD_TMUX_SOCKET", "/run/chitra-worker/tmux-1000/default")
    monkeypatch.setenv("CHITRA_WATCHD_IDLE_THRESHOLD_SECONDS", "30")
    monkeypatch.setenv("CHITRA_WATCHD_EXCLUDE_SESSION_PREFIXES", "boomtown-control")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_COUNT", "1")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_COMMAND", "/opt/chitra/bin/review-with-monitor-credentials")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_MODEL", "operator-cheap-model")
    monkeypatch.setenv("CHITRA_WATCHD_REASONED_DISPATCH_ENABLED", "false")

    config = resolve_config()

    assert config.events_log == tmp_path / "state" / "events.log"
    assert config.interval_seconds == 2.5
    assert config.panes_override == ("%1", "%2")
    assert config.session_prefixes == ("boomtown-", "boomtown-review-")
    assert config.session_names == ("infra-health", "atlas-v5")
    assert config.tmux_socket == Path("/run/chitra-worker/tmux-1000/default")
    assert config.idle_threshold_seconds == 30
    assert config.excluded_session_prefixes == ("boomtown-control",)
    assert config.reviewer_count == 1
    assert config.reviewer_command == "/opt/chitra/bin/review-with-monitor-credentials"
    assert config.reviewer_model == "operator-cheap-model"
    assert config.reasoned_dispatch_enabled is False


def test_reviewer_config_precedence_is_cli_then_env_then_pinned_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    defaults = resolve_config(state_dir=tmp_path / "defaults")
    assert defaults.reviewer_count == 2
    assert defaults.reviewer_command == "claude"
    assert defaults.reviewer_model is None
    assert defaults.reasoned_dispatch_enabled is True

    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_COUNT", "1")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_COMMAND", "env-claude")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_MODEL", "env-model")
    environment = resolve_config(state_dir=tmp_path / "environment")
    assert environment.reviewer_count == 1
    assert environment.reviewer_command == "env-claude"
    assert environment.reviewer_model == "env-model"

    args = build_arg_parser().parse_args(["--reviewer-count", "3", "--reviewer-command", "cli-claude", "--reviewer-model", "cli-model"])
    cli = resolve_config(
        state_dir=tmp_path / "cli",
        reviewer_count=args.reviewer_count,
        reviewer_command=args.reviewer_command,
        reviewer_model=args.reviewer_model,
    )
    assert cli.reviewer_count == 3
    assert cli.reviewer_command == "cli-claude"
    assert cli.reviewer_model == "cli-model"


@pytest.mark.parametrize("reviewer_count", [0, -1])
def test_resolve_config_rejects_non_positive_reviewer_count(tmp_path: Path, reviewer_count: int) -> None:
    with pytest.raises(ValueError, match="reviewer_count must be a positive integer"):
        resolve_config(state_dir=tmp_path, reviewer_count=reviewer_count)

"""G3 replace-then-remove: status, registry, roster, pane and ownership gates.

Every test drives the deny path through the real boundary: the installed
console CLIs over real files, the status broker fed by a real tmux server on
a unique socket, and the real unix-socket control API. The only fakes are at
external edges — a ``codex`` binary that gives a pane a recognized identity.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _g3_status_boundary import (
    install_fake_codex,
    install_real_tmux,
    kill_tmux,
    launch_codex_pane,
    respawn_codex_pane,
    run_cli,
    script,
    tmux,
    unique_socket,
    wait_pane_command,
)
from _goal_fixtures import enrollment_fields

from chitra.account_registry import load_registry, registry_path
from chitra.agent_runtime import AgentStatusBroker
from chitra.agent_status import ManifestRepository
from chitra.goals import GoalRecord, upsert_goal
from chitra.lane_config import LaneCredentials, LaneSpec
from chitra.load_shed import PressureSample
from chitra.pane_sensing import PaneSenseState, sense_lane_panes
from chitra.rate_limit_guard import sweep
from chitra.socket_api import ApiRuntime, ControlServer

SESSION_REF = "host-b:feeds:0.0"


@pytest.fixture
def codex_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``codex`` plus the unwrapped tmux binary, first on PATH.

    In-process chitra code (pane sensing, the control socket adapter) spawns
    ``tmux`` through PATH; on hosts whose PATH tmux is an env-scrubbing
    wrapper, the server would lose the test's PATH lead and stub env vars, so
    the real binary is linked ahead of it for the whole test.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    install_real_tmux(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return install_fake_codex(bin_dir)


def _lane(tmp_path: Path, *, socket: Path, session: str, identifier: str = "feeds") -> LaneSpec:
    state_dir = tmp_path / f"lane-{identifier}" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return LaneSpec(
        identifier=identifier,
        account="operator",
        uid=os.getuid(),
        home=tmp_path,
        workdir=tmp_path,
        config_dir=tmp_path / f"lane-{identifier}" / "config",
        state_dir=state_dir,
        tmux_socket=socket,
        tmux_session=session,
        credentials=LaneCredentials(
            claude_credentials=tmp_path / "no-claude.json",
            ssh_dispatch_key=tmp_path / "no-key",
        ),
        enabled=True,
    )


def _sense(lane: LaneSpec, broker: AgentStatusBroker, known: list[str] | None = None) -> int:
    return sense_lane_panes(
        lane,
        broker=broker,
        state=PaneSenseState(),
        known_session_refs=known or [],
        activity_root=lane.state_dir,
    )


def _status_for(broker: AgentStatusBroker, pane_id: str):
    return next((item for item in broker.statuses() if item.pane_id == pane_id), None)


def _pane_id(socket: Path, session: str) -> str:
    result = tmux(socket, "list-panes", "-s", "-t", session, "-F", "#{pane_id}")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# D1: the account registry fails closed on a malformed or wrong-schema store.
# Boundary: chitra-rate-limit-guard's sweep loads account_registry.json for
# real; a corrupt store must stop the sweep loudly, never proceed.
# Covers: test_load_registry_rejects_wrong_schema,
#         test_load_registry_rejects_malformed_payload,
#         test_registry_entry_from_dict_rejects_bad_shape.
# ---------------------------------------------------------------------------

PERMISSION_PROMPT = "Allow command?\n  Yes\n  No\n"
TRUST_PROMPT = "Do you trust the contents of this directory?\n  1. Yes\n  2. No\n"
STALE_ANSWERED_WITH_SPINNER = (
    "Do you trust the contents of this directory?\n"
    "  1. Yes\n"
    "  2. No\n"
    "Trust recorded; the task cannot be cancelled now.\n"
    "• Working (12s • esc to interrupt)\n"
)


def _usage_snapshot(directory: Path, name: str, *, tmux_session: str, account: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "schema": "chitra.usage.v1",
                "kind": "claude",
                "ts": datetime.now(UTC).isoformat(),
                "session_id": f"s-{name}",
                "tmux_session": tmux_session,
                "five_hour": None,
                "seven_day": None,
                "account": account,
            }
        ),
        encoding="utf-8",
    )
    return path


# The sweep is driven through its real daemon entry (``rate_limit_guard.sweep``,
# the function the CLI and the daemon both call) over real files; the only
# injected edge is the Linux PSI pressure probe, an OS service this VM does
# not expose (/proc/pressure is absent here, as on non-Linux hosts).
_QUIET_PRESSURE = PressureSample(mem_available_pct=90.0, memory_some_avg60=0.0, memory_full_avg60=0.0, cpu_some_avg60=0.0)


def _sweep(tmp_path: Path, usage_dir: Path, host: str = "host-b", now: datetime | None = None):
    return sweep(
        usage_dir=usage_dir,
        host=host,
        goals_root=tmp_path / "root",
        queue_dir=tmp_path / "queue",
        pressure_sample=_QUIET_PRESSURE,
        now=now,
    )


def test_d1_registry_sweep_fails_closed_on_wrong_schema(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    registry_path(root).write_text(json.dumps({"schema": "bogus", "entries": []}), encoding="utf-8")
    usage = _usage_snapshot(tmp_path / "usage", "s1", tmux_session="lane1", account="a@x.com")

    with pytest.raises(ValueError, match="account_registry"):
        _sweep(tmp_path, usage.parent)


def test_d1_registry_sweep_fails_closed_on_malformed_entry(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    registry_path(root).write_text(
        json.dumps(
            {
                "schema": "chitra.account_registry.v1",
                "entries": [{"tmux_session": 17, "session_id": "s", "kind": "claude", "account": "a", "updated_at": "u"}],
            }
        ),
        encoding="utf-8",
    )
    usage = _usage_snapshot(tmp_path / "usage", "s1", tmux_session="lane1", account="a@x.com")

    with pytest.raises(ValueError, match="account registry"):
        _sweep(tmp_path, usage.parent)


def test_d1_registry_sweep_fails_closed_on_non_list_entries(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    registry_path(root).write_text(
        json.dumps({"schema": "chitra.account_registry.v1", "entries": {"lane1": {}}}), encoding="utf-8"
    )
    usage = _usage_snapshot(tmp_path / "usage", "s1", tmux_session="lane1", account="a@x.com")

    with pytest.raises(ValueError, match="entries must be a list"):
        _sweep(tmp_path, usage.parent)


def test_registry_sweep_records_and_escalates_over_real_files(tmp_path: Path) -> None:
    """Parity for the non-deny registry behaviours: record, probe-skip,
    account-change escalation, no-change silence, disappeared-retained
    escalation, stale prune, serialized concurrent writers."""
    root = tmp_path / "root"
    usage = _usage_snapshot(tmp_path / "usage", "s1", tmux_session="lane1", account="a@x.com")
    _usage_snapshot(usage.parent, "probe", tmux_session="", account="probe@x.com")

    _sweep(tmp_path, usage.parent)
    entries = load_registry(root)
    # The synthetic account-wide probe carries no per-lane identity and is
    # never registered.
    assert [e.tmux_session for e in entries] == ["lane1"]
    assert entries[0].account == "a@x.com"

    # Same lane reports a different account: the sweep must escalate loudly.
    usage.write_text(
        json.dumps(
            {
                "schema": "chitra.usage.v1",
                "kind": "claude",
                "ts": datetime.now(UTC).isoformat(),
                "session_id": "s-1",
                "tmux_session": "lane1",
                "five_hour": None,
                "seven_day": None,
                "account": "b@x.com",
            }
        ),
        encoding="utf-8",
    )
    second = _sweep(tmp_path, usage.parent)
    assert any(
        "account identity changed" in line and "a@x.com" in line and "b@x.com" in line
        for line in second.escalations
    )

    # The same account reported again escalates nothing.
    unchanged = _sweep(tmp_path, usage.parent)
    assert not any("account identity changed" in line for line in unchanged.escalations)

    # A previously-fresh lane that vanishes is retained and escalated, not dropped.
    empty = tmp_path / "usage-empty"
    empty.mkdir()
    third = _sweep(tmp_path, empty)
    assert any("cannot safely pause or resume" in line for line in third.escalations)
    assert [e.tmux_session for e in load_registry(root)] == ["lane1"]

    # Once the entry is stale beyond the freshness window it is pruned
    # silently -- no escalation, no lingering record.
    fourth = _sweep(
        tmp_path, empty, now=datetime.now(UTC) + timedelta(minutes=61)
    )
    assert not any("cannot safely pause or resume" in line for line in fourth.escalations)
    assert load_registry(root) == []


def test_registry_sweep_serializes_concurrent_writers(tmp_path: Path) -> None:
    """N parallel sweep processes over distinct usage dirs share one registry
    under its file lock: no update is lost and the store stays valid.

    Each worker runs the real ``sweep`` entry through a small driver module so
    every writer executes identical production code."""
    processes = []
    for index in range(6):
        usage = _usage_snapshot(
            tmp_path / f"usage-{index}", "s1", tmux_session=f"lane-{index}", account=f"a{index}@x.com"
        )
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parent / "_g3_registry_worker.py"),
                    str(usage.parent),
                    str(tmp_path / "root"),
                    str(tmp_path / f"queue-{index}"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=dict(os.environ),
                cwd=str(Path(__file__).resolve().parent.parent),
            )
        )
    for process in processes:
        _, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr

    entries = load_registry(tmp_path / "root")
    assert sorted(e.tmux_session for e in entries) == [f"lane-{i}" for i in range(6)]


# ---------------------------------------------------------------------------
# D2..D10: offline status classification through `chitra-agent explain --file`.
# The CLI parses a real pane capture through the bundled/local manifests.
# ---------------------------------------------------------------------------


def _explain(tmp_path: Path, content: str, *, agent: str = "codex", manifest_dir: Path | None = None) -> dict:
    capture = tmp_path / "capture.txt"
    capture.write_text(content, encoding="utf-8")
    argv = [script("chitra-agent"), "explain", "--file", str(capture), "--agent", agent]
    if manifest_dir is not None:
        argv += ["--manifest-dir", str(manifest_dir)]
    result = run_cli(argv)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_d2_answered_permission_prompt_classifies_blocked(tmp_path: Path) -> None:
    result = _explain(tmp_path, PERMISSION_PROMPT)
    assert result["state"] == "blocked"
    assert result["matched_rule"] == "permission_prompt"
    assert result["blocker_kind"] == "permission"


def test_d3_ambiguous_capture_falls_back_to_idle(tmp_path: Path) -> None:
    result = _explain(tmp_path, "Something unusual needs attention\n")
    assert result["state"] == "idle"
    assert result["fallback_reason"] is not None


def test_d4_manifest_rejects_blocked_rule_without_blocker_kind(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "codex.toml").write_text(
        """
schema_version = 1
agent = "codex"
version = "test"

[[rules]]
id = "too_broad"
state = "blocked"
all = [{ kind = "contains", value = "error" }]
""",
        encoding="utf-8",
    )
    result = _explain(tmp_path, "error\n", manifest_dir=manifest_dir)
    assert result["state"] == "idle"
    assert result["fallback_reason"] is not None
    assert "require blocker_kind" in (result["warning"] or "")


def test_d4_manifest_rejects_whole_region_blocked_rule(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "codex.toml").write_text(
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
        encoding="utf-8",
    )
    result = _explain(tmp_path, TRUST_PROMPT, manifest_dir=manifest_dir)
    assert result["state"] == "idle"
    assert "live bottom region" in (result["warning"] or "")


def test_d5_stale_answered_prompt_below_live_spinner_is_working(tmp_path: Path) -> None:
    result = _explain(tmp_path, STALE_ANSWERED_WITH_SPINNER)
    assert result["state"] == "working"
    assert result["matched_rule"] == "working_spinner"
    assert result["blocker_kind"] is None
    assert result["suppressed_blocker_rule"] == "trust_directory"


def test_d6_answered_prompt_with_bare_input_row_is_idle(tmp_path: Path) -> None:
    cases = (
        (
            "codex",
            "Do you trust the contents of this directory?\n"
            "  1. Yes\n"
            "  2. No\n"
            "Trust recorded; the task cannot be cancelled now.\n"
            "›\n",
            "trust_directory",
        ),
        (
            "claude",
            "Do you want to proceed with this change?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "Permission granted; continuing.\n"
            "❯\n",
            "permission_prompt",
        ),
    )
    for index, (agent, snapshot, suppressed_rule) in enumerate(cases):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        result = _explain(case_dir, snapshot, agent=agent)
        assert result["state"] == "idle"
        assert result["matched_rule"] == "input_row"
        assert result["suppressed_blocker_rule"] == suppressed_rule


def test_d7_live_selector_or_draft_row_does_not_suppress_blocker(tmp_path: Path) -> None:
    cases = (
        ("codex", "Do you trust the contents of this directory?\n› 1. Yes\n  2. No\n"),
        ("claude", "Do you want to proceed with this change?\n❯ 1. Yes\n  2. No\n"),
        ("codex", "Allow command?\nYes\nNo\n❯ operator draft remains unsent\n"),
    )
    for index, (agent, snapshot) in enumerate(cases):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        result = _explain(case_dir, snapshot, agent=agent)
        assert result["state"] == "blocked"
        assert result["suppressed_blocker_rule"] is None


def test_d8_echoed_permission_text_below_live_spinner_is_working(tmp_path: Path) -> None:
    result = _explain(
        tmp_path,
        "I will ask: Do you want to proceed with this change?\nEsc to cancel\n✻ Working… esc to interrupt\n",
        agent="claude",
    )
    assert result["state"] == "working"
    assert result["blocker_kind"] is None


def test_d9_answer_tokens_are_whole_word_case_sensitive(tmp_path: Path) -> None:
    embedded = _explain(
        tmp_path,
        "Do you trust the contents of this directory?\nThe task cannot continue yet.\n",
    )
    assert embedded["state"] != "blocked"

    lower_dir = tmp_path / "lower"
    lower_dir.mkdir()
    lowercase = _explain(
        lower_dir,
        "Do you trust the contents of this directory?\n  1. yes\n  2. no\n",
    )
    assert lowercase["state"] != "blocked"


def test_d10_local_manifest_overrides_and_invalid_falls_back_idle(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "codex.toml").write_text(
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
    result = _explain(tmp_path, "LOCAL SIGNAL", manifest_dir=manifest_dir)
    assert result["state"] == "working"
    assert result["source_kind"] == "local"
    assert result["manifest_version"] == "local-1"

    (manifest_dir / "codex.toml").write_text("schema_version = 99\n", encoding="utf-8")
    invalid = _explain(tmp_path, PERMISSION_PROMPT, manifest_dir=manifest_dir)
    assert invalid["state"] == "idle"
    assert invalid["warning"]


# ---------------------------------------------------------------------------
# D11..D15: the broker over a real tmux server and the real socket API.
# ---------------------------------------------------------------------------


def test_d11_frozen_identical_snapshot_reclassifies_to_blocked(tmp_path: Path, codex_bin: Path) -> None:
    socket = unique_socket(tmp_path)
    session = "lane-frozen"
    launch_codex_pane(socket, session, codex_bin, STALE_ANSWERED_WITH_SPINNER)
    try:
        wait_pane_command(socket, session, "codex")
        broker = AgentStatusBroker(tmp_path / "broker", ManifestRepository())
        lane = _lane(tmp_path, socket=socket, session=session)
        known = [f"host-b:{session}:0.0"]

        _sense(lane, broker, known)
        first = _status_for(broker, _pane_id(socket, session))
        assert first is not None and first.state == "working"

        _sense(lane, broker, known)
        second = _status_for(broker, _pane_id(socket, session))
        assert second is not None and second.state == "blocked"
    finally:
        kill_tmux(socket, session)


def test_d12_changing_spinner_stays_working(tmp_path: Path, codex_bin: Path) -> None:
    socket = unique_socket(tmp_path)
    session = "lane-changing"
    common = (
        "Do you trust the contents of this directory?\n"
        "  1. Yes\n"
        "  2. No\n"
        "Trust recorded; the task cannot be cancelled now.\n"
    )
    launch_codex_pane(socket, session, codex_bin, common + "• Working (12s • esc to interrupt)\n")
    try:
        wait_pane_command(socket, session, "codex")
        broker = AgentStatusBroker(tmp_path / "broker", ManifestRepository())
        lane = _lane(tmp_path, socket=socket, session=session)
        known = [f"host-b:{session}:0.0"]

        _sense(lane, broker, known)
        respawn_codex_pane(socket, session, codex_bin, common + "• Working (13s • esc to interrupt)\n")
        wait_pane_command(socket, session, "codex")
        _sense(lane, broker, known)

        status = _status_for(broker, _pane_id(socket, session))
        assert status is not None and status.state == "working"
    finally:
        kill_tmux(socket, session)


def test_d13_stability_does_not_carry_across_session_identity(tmp_path: Path, codex_bin: Path) -> None:
    socket = unique_socket(tmp_path)
    old_session = "lane-old"
    new_session = "lane-new"
    launch_codex_pane(socket, old_session, codex_bin, STALE_ANSWERED_WITH_SPINNER)
    launch_codex_pane(socket, new_session, codex_bin, STALE_ANSWERED_WITH_SPINNER)
    try:
        wait_pane_command(socket, old_session, "codex")
        wait_pane_command(socket, new_session, "codex")
        broker = AgentStatusBroker(tmp_path / "broker", ManifestRepository())
        # One observed pane id; identity flips when the session changes.
        old_pane = _pane_id(socket, old_session)
        _pane_id(socket, new_session)
        # Re-key both sessions to the same pane so the broker sees one pane id
        # whose session identity changes -- the reset must apply per identity.
        # The two sessions use distinct panes, so instead drive the identity
        # reset on ONE session renamed between passes.
        lane_old = _lane(tmp_path, socket=socket, session=old_session, identifier="old")
        _sense(lane_old, broker, [f"host-b:{old_session}:0.0"])
        assert _status_for(broker, old_pane).state == "working"

        # Rename the live tmux session: same pane id, new session identity.
        assert tmux(socket, "rename-session", "-t", old_session, "lane-renamed").returncode == 0
        lane_new = _lane(tmp_path, socket=socket, session="lane-renamed", identifier="new")
        _sense(lane_new, broker, ["host-b:lane-renamed:0.0"])
        assert _status_for(broker, old_pane).state == "working"  # identity reset: identical capture is fresh
        _sense(lane_new, broker, ["host-b:lane-renamed:0.0"])
        assert _status_for(broker, old_pane).state == "blocked"
        kill_tmux(socket, "lane-renamed")
    finally:
        kill_tmux(socket, new_session)


def test_d14_lifecycle_report_is_authoritative_over_screen(tmp_path: Path, codex_bin: Path) -> None:
    socket = unique_socket(tmp_path)
    session = "lane-auth"
    launch_codex_pane(socket, session, codex_bin, PERMISSION_PROMPT)
    try:
        wait_pane_command(socket, session, "codex")
        broker = AgentStatusBroker(tmp_path / "broker", ManifestRepository())
        api = ApiRuntime(broker)
        ctl_sock = unique_socket(tmp_path, "ctl")
        server = ControlServer(ctl_sock, api)
        server.start()
        try:
            pane = _pane_id(socket, session)
            session_ref = f"host-b:{session}:0.0"
            report = run_cli(
                [
                    script("chitra-agent"),
                    "--socket-path",
                    str(ctl_sock),
                    "report",
                    "--pane-id",
                    pane,
                    "--session-ref",
                    session_ref,
                    "--source",
                    "integration:codex",
                    "--agent",
                    "codex",
                    "--state",
                    "working",
                ]
            )
            assert report.returncode == 0, report.stderr

            lane = _lane(tmp_path, socket=socket, session=session)
            _sense(lane, broker, [session_ref])
            status = _status_for(broker, pane)
            assert status is not None
            assert status.state == "working"
            assert status.authority == "integration"
            assert status.explain.screen_detection_skipped is True
        finally:
            server.shutdown()
    finally:
        kill_tmux(socket, session)


def test_d15_session_identity_change_releases_lifecycle_authority(tmp_path: Path, codex_bin: Path) -> None:
    socket = unique_socket(tmp_path)
    session = "lane-release"
    launch_codex_pane(socket, session, codex_bin, "› Add a task\n")
    try:
        wait_pane_command(socket, session, "codex")
        broker = AgentStatusBroker(tmp_path / "broker", ManifestRepository())
        api = ApiRuntime(broker)
        ctl_sock = unique_socket(tmp_path, "ctl")
        server = ControlServer(ctl_sock, api)
        server.start()
        try:
            pane = _pane_id(socket, session)
            report = run_cli(
                [
                    script("chitra-agent"),
                    "--socket-path",
                    str(ctl_sock),
                    "report",
                    "--pane-id",
                    pane,
                    "--session-ref",
                    "host-b:old:0.0",
                    "--source",
                    "integration:codex",
                    "--agent",
                    "codex",
                    "--state",
                    "working",
                ]
            )
            assert report.returncode == 0, report.stderr

            lane = _lane(tmp_path, socket=socket, session=session)
            _sense(lane, broker, [f"host-b:{session}:0.0"])
            status = _status_for(broker, pane)
            assert status is not None
            assert status.authority == "manifest"
            assert broker.lifecycle_reports() == ()
        finally:
            server.shutdown()
    finally:
        kill_tmux(socket, session)


def test_d16_pane_exec_refuses_unknown_pane_identity(tmp_path: Path) -> None:
    """pane_exec binds CHITRA_PANE_ID only to a real tmux pane id; anything
    else is refused before the child ever runs."""
    marker = tmp_path / "child-ran"
    argv = [sys.executable, "-m", "chitra.pane_exec", "--", "/bin/touch", str(marker)]
    for bogus in ("", "17", "%", "%abc", "%-1"):
        result = run_cli(argv, env_extra={"TMUX_PANE": bogus})
        assert result.returncode != 0
        assert "TMUX_PANE" in (result.stderr + result.stdout)
        assert not marker.exists()

    result = run_cli(argv, env_extra={"TMUX_PANE": "%77"})
    assert result.returncode == 0, result.stderr
    assert marker.exists()

    # The child sees the pane identity; other env is passed through unmutated.
    env_child = run_cli(
        [sys.executable, "-m", "chitra.pane_exec", "--", "/usr/bin/printenv", "CHITRA_PANE_ID"],
        env_extra={"TMUX_PANE": "%77"},
    )
    assert env_child.returncode == 0
    assert env_child.stdout.strip() == "%77"


def test_d17_ownership_partition_over_real_goals_store(tmp_path: Path) -> None:
    root = tmp_path / "goals-root"
    upsert_goal(
        root,
        GoalRecord(
            session_ref="host-b:feeds:0.0",
            goal="Keep the feeds digest lane running and verified",
            done_when="the digest ships to all readers",
            source="test",
            status="working",
            goal_version=1,
            **enrollment_fields("the digest ships to all readers"),
        ),
    )
    upsert_goal(
        root,
        GoalRecord(
            session_ref="host-b:held-lane:0.0",
            goal="A held lane is not owned by this host",
            done_when="the lane is unheld and running",
            source="test",
            status="held",
            goal_version=1,
            **enrollment_fields("the lane is unheld and running"),
        ),
    )
    upsert_goal(
        root,
        GoalRecord(
            session_ref="other-host:remote:0.0",
            goal="A remote lane is not owned by this host",
            done_when="the remote lane finishes its work",
            source="test",
            status="working",
            goal_version=1,
            **enrollment_fields("the remote lane finishes its work"),
        ),
    )

    result = run_cli(
        [
            script("chitra-ownership"),
            "--host",
            "host-b",
            "--session-ref",
            "host-b:feeds:0.0",
            "--session-ref",
            "host-b:held-lane:0.0",
            "--session-ref",
            "other-host:remote:0.0",
            "--session-ref",
            "host-b:unknown:0.0",
            "--state-dir",
            str(root),
        ]
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["host"] == "host-b"
    assert report["owned"] is True
    assert report["owned_session_refs"] == ["host-b:feeds:0.0"]
    assert sorted(report["unowned_session_refs"]) == [
        "host-b:held-lane:0.0",
        "host-b:unknown:0.0",
        "other-host:remote:0.0",
    ]

    # No working lane for this host: the query reports owned=false outright.
    result = run_cli(
        [
            script("chitra-ownership"),
            "--host",
            "host-b",
            "--session-ref",
            "host-b:held-lane:0.0",
            "--session-ref",
            "other-host:remote:0.0",
            "--state-dir",
            str(root),
        ]
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["owned"] is False
    assert report["owned_session_refs"] == []


# ---------------------------------------------------------------------------
# D18: the roster refuses a store carrying an unknown status (fail closed).
# ---------------------------------------------------------------------------


def _upsert_roster_goal(root: Path, session_ref: str, status: str, **fields: object) -> None:
    values: dict[str, object] = {
        "goal": "Keep this durable roster objective clear and verifiable.",
        "done_when": "Every required validation command passes cleanly.",
        "now": "running checks",
    }
    values.update(fields)
    upsert_goal(
        root,
        GoalRecord(
            session_ref=session_ref,
            status=status,  # type: ignore[arg-type]
            source="test",
            goal_version=1,
            **enrollment_fields(str(values["done_when"])),
            **values,
        ),
    )


def test_d18_roster_refuses_a_goal_with_an_unknown_status(tmp_path: Path) -> None:
    """An uncolorable status must never render: the store fails closed at load."""
    _upsert_roster_goal(tmp_path, "host-b:feeds:0.0", "working")
    goals_path = tmp_path / "goals.json"
    store = json.loads(goals_path.read_text(encoding="utf-8"))
    store["goals"][0]["status"] = "bogus-status"
    goals_path.write_text(json.dumps(store), encoding="utf-8")

    result = run_cli([script("chitra-goals"), "roster", "--root", str(tmp_path)])
    assert result.returncode != 0
    assert "status" in result.stderr


def test_roster_renders_markers_asks_and_artifacts(tmp_path: Path) -> None:
    """Render parity over real files: precedence markers, open asks, and the
    unreviewed-artifact block all reach the roster output."""
    _upsert_roster_goal(tmp_path, "host-b:zeta:0.0", "blocked", goal="the zeta lane needs the operator right now")
    _upsert_roster_goal(tmp_path, "host-b:alpha:0.0", "working")
    _upsert_roster_goal(tmp_path, "host-b:asks:0.0", "working", open_asks=("1. Decide the deployment window.",))
    result = run_cli(
        [script("chitra-goals"), "roster", "--root", str(tmp_path), "--format", "markdown"],
        env_extra={"COLUMNS": "100"},
    )
    assert result.returncode == 0, result.stderr
    rendered = result.stdout
    assert rendered.startswith("|  | Session | Goal | Now | Needs |")
    assert all(name in rendered for name in ("zeta", "alpha", "asks"))
    assert "🔴" in rendered and "🟢" in rendered
    assert "Decide the deployment window." in rendered

    box = run_cli([script("chitra-goals"), "roster", "--root", str(tmp_path), "--format", "box"], env_extra={"COLUMNS": "120"})
    assert box.returncode == 0, box.stderr
    assert "┌" in box.stdout and "│" in box.stdout


def test_roster_shows_unreviewed_artifact_block(tmp_path: Path) -> None:
    _upsert_roster_goal(tmp_path, "host-b:feeds:0.0", "working")
    (tmp_path / "artifacts.json").write_text(
        json.dumps(
            {
                "schema": "chitra.artifacts.v1",
                "updated_at": "2026-08-21T12:00:00+00:00",
                "artifacts": [
                    {
                        "url": "https://claude.ai/code/artifact/example-001",
                        "title": "Operator interview notes",
                        "kind": "interview",
                        "source": "host-b:/var/lib/chitra/artifact.html",
                        "brief": "",
                        "published_at": "2026-08-21T12:00:00+00:00",
                        "updated_at": "2026-08-21T12:00:00+00:00",
                        "review_status": "unreviewed",
                        "reviewed_at": "",
                        "response": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = run_cli([script("chitra-goals"), "roster", "--root", str(tmp_path), "--format", "markdown"])
    assert result.returncode == 0, result.stderr
    assert "UNREVIEWED ARTIFACTS" in result.stdout
    assert "Operator interview notes" in result.stdout


# Enrolled-goal fixture used by the redirect-immutability boundary test.


def test_enrolled_done_when_is_immutable_via_redirect_cli(tmp_path: Path) -> None:
    """An enrolled goal's frozen done condition cannot be redirected."""
    upsert_goal(
        tmp_path,
        GoalRecord(
            session_ref="host-b:feeds:0.0",
            goal="Ship the feeds digest to every subscribed reader",
            done_when="Every required validation command passes cleanly.",
            source="operator",
            status="working",
            goal_version=1,
            **enrollment_fields("Every required validation command passes cleanly."),
        ),
    )
    result = run_cli(
        [
            script("chitra-goals"),
            "redirect",
            "--root",
            str(tmp_path),
            "--session-ref",
            "host-b:feeds:0.0",
            "--reason",
            "operator asked to relax the done condition",
            "--done-when",
            "a weaker done condition",
        ]
    )
    assert result.returncode != 0
    assert "cannot be redirected" in result.stderr

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from chitra.goals import ForegroundTask, GoalRecord
from chitra.knowledge import KnowledgeBundle
from chitra.lane_anchor import (
    enqueue_native_controls,
    set_native_controls_lifecycle,
    write_native_controls,
    write_session_setup_note,
)
from chitra.lane_config import LaneCredentials, LaneSpec, load_lanes
from chitra.recovery import capture_worktree_binding, transition_lane_lifecycle


def _lane(tmp_path: Path, bundle: KnowledgeBundle) -> LaneSpec:
    return LaneSpec(
        identifier="alpha",
        account="alpha",
        uid=2109,
        home=tmp_path / "home",
        workdir=tmp_path / "worktree",
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        tmux_socket=tmp_path / "alpha.sock",
        tmux_session="alpha",
        credentials=LaneCredentials(
            claude_credentials=tmp_path / "credentials.json",
            ssh_dispatch_key=tmp_path / "id_ed25519",
        ),
        knowledge_bundle=bundle,
    )


def _goal() -> GoalRecord:
    return GoalRecord(
        session_ref="tophand:alpha:0.0",
        goal="Keep the lane moving until the tests independently verify completion.",
        done_when="The focused tests pass.",
        source="test",
        status="working",
    )


def _initialize_worktree(workdir: Path) -> None:
    workdir.mkdir()
    for args in (
        ("init",),
        ("config", "user.email", "chitra-test@example.test"),
        ("config", "user.name", "Chitra Test"),
    ):
        subprocess.run(["git", "-C", str(workdir), *args], check=True, capture_output=True, text=True)
    (workdir / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workdir), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(workdir), "commit", "-m", "initial"], check=True, capture_output=True, text=True)


def _transition(lane: LaneSpec, goal: GoalRecord, target: str, *, completed: bool = False) -> None:
    transition_lane_lifecycle(
        lane.state_dir,
        session_ref=goal.session_ref,
        target=target,  # type: ignore[arg-type]
        binding=capture_worktree_binding(lane.workdir),
        resume_note=f"Move the test lane to {target}.",
        independently_completed=completed,
        unfinished_work=not completed,
    )


def test_knowledge_bundle_hash_is_canonical_and_manifest_global_default_is_inherited(tmp_path: Path) -> None:
    import yaml

    bundle = KnowledgeBundle.from_mapping(
        {
            "system_facts": ["The service runs on the controlled host."],
            "architecture_principles": ["Use the existing durable ledger."],
            "code_patterns": ["Keep adapters small."],
            "decision_rules": ["Evidence changes plans."],
            "canonical_references": ["docs/architecture.md"],
        }
    )
    manifest = {
        "knowledge_bundle": {key: value for key, value in bundle.to_dict().items() if key != "schema"},
        "lanes": [
            {
                "id": "alpha",
                "account": "alpha",
                "uid": 2109,
                "home": str(tmp_path / "home"),
                "workdir": str(tmp_path / "worktree"),
                "config_dir": str(tmp_path / "config"),
                "state_dir": str(tmp_path / "state"),
                "tmux_socket": str(tmp_path / "alpha.sock"),
                "tmux_session": "alpha",
                "credentials": {
                    "claude_credentials": str(tmp_path / "credentials.json"),
                    "ssh_dispatch_key": str(tmp_path / "id_ed25519"),
                },
            }
        ],
    }
    manifest_path = tmp_path / "lanes.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    lane = load_lanes(manifest_path)[0]
    assert lane.knowledge_bundle == bundle
    assert lane.knowledge_bundle.sha256 == bundle.sha256
    assert len(bundle.sha256) == 64


def test_setup_note_and_native_controls_record_goal_bundle_and_four_lifecycle_states(tmp_path: Path) -> None:
    bundle = KnowledgeBundle(system_facts=("Use the supplied host facts.",))
    lane = _lane(tmp_path, bundle)
    goal = _goal()
    _initialize_worktree(lane.workdir)
    _transition(lane, goal, "active")
    setup_note = write_session_setup_note(lane, goal, backend="claude")
    controls = write_native_controls(lane, goal, backend="claude", setup_note=setup_note)

    note = setup_note.read_text(encoding="utf-8")
    assert bundle.sha256 in note
    assert "/goal Keep the lane moving" in note
    assert "/loop 5m" in note
    assert "## AgentTrail lane plan" in note
    assert f"Maintain this lane's plan at {lane.workdir / 'PLAN.md'}." in note
    assert "`## Card title {#card-id}`" in note
    assert "`needs: [card-id]`" in note
    assert "`tech:` evidence line" in note
    assert "the plan cannot change the goal or prove completion" in note
    declared = json.loads(controls.read_text(encoding="utf-8"))
    assert declared["lifecycle"] == "active"
    assert declared["controls"]["recurring_enforcement"]["state"] == "armed"
    queued = enqueue_native_controls(lane, goal)
    assert len(queued) == 2
    queued_payloads = [
        json.loads((lane.queue_dir / "orders" / f"{order_id}.json").read_text(encoding="utf-8"))
        for order_id in queued
    ]
    assert {payload["nudge"].split(" ", 1)[0] for payload in queued_payloads} == {"/goal", "/loop"}
    assert all(payload["task_type"] == "native-control" for payload in queued_payloads)

    _transition(lane, goal, "paused")
    # Recovery changes first. Even if a process dies before refreshing this
    # provider detail file, queueing must derive the paused state from recovery.
    prune = enqueue_native_controls(lane, goal)
    assert len(prune) == 1
    prune_payload = json.loads((lane.queue_dir / "orders" / f"{prune[0]}.json").read_text())
    assert prune_payload["task_type"] == "native-control-pause-prune"
    assert "CronDelete" in prune_payload["nudge"]
    paused = set_native_controls_lifecycle(lane, "paused")
    assert paused["provider_session_action"] == "retain"
    assert paused["controls"]["recurring_enforcement"]["state"] == "stopped-definition-retained"
    _transition(lane, goal, "shelved")
    shelved = set_native_controls_lifecycle(lane, "shelved")
    assert shelved["provider_session_action"] == "offline"
    _transition(lane, goal, "active")
    resumed = enqueue_native_controls(lane, goal)
    assert len(resumed) == 2
    assert set(resumed).isdisjoint(queued)
    _transition(lane, goal, "closed", completed=True)
    with pytest.raises(ValueError, match="independently verified"):
        set_native_controls_lifecycle(lane, "closed")
    closed = set_native_controls_lifecycle(lane, "closed", completion_verified=True)
    assert closed["controls"]["recurring_enforcement"]["state"] == "removed-after-verified-completion"


def test_setup_note_restores_durable_unfinished_work_after_shelving(tmp_path: Path) -> None:
    lane = _lane(tmp_path, KnowledgeBundle())
    goal = replace(
        _goal(),
        now="Investigate why the focused validation did not complete.",
        foreground_tasks=(
            ForegroundTask(
                task_id="foreground-replan-1",
                kind="replan",
                text="Select the next in-scope validation route.",
                source="supervisor",
                goal_version=1,
                created_at="2026-08-27T12:00:00Z",
            ),
            ForegroundTask(
                task_id="foreground-question-1",
                kind="question",
                text="Resolve the remaining implementation ambiguity.",
                source="monitord",
                goal_version=1,
                created_at="2026-08-27T12:01:00Z",
            ),
        ),
    )

    note = write_session_setup_note(lane, goal, backend="claude").read_text(encoding="utf-8")

    assert "## Current unfinished work" in note
    assert "- Current state: Investigate why the focused validation did not complete." in note
    assert "Foreground replan [foreground-replan-1] from supervisor: Select the next in-scope validation route." in note
    assert "Foreground question [foreground-question-1] from monitord: Resolve the remaining implementation ambiguity." in note

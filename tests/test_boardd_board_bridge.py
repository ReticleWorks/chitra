"""The bridge turns chitra GoalRecords into the two files the board reads.

PLAN.md is the canvas of session cards; roster.json's `escalations` is the
red stack on the right. Both are agenttrail's own formats, so what is
asserted here is the contract between chitra state and the approved board.
"""

import json

from boardd import board_bridge

BLOCKED = {
    "session_ref": "tophand:wiki-backfill",
    "lane_id": "wiki-backfill",
    "goal": "Backfill the Atlas wiki pages.",
    "status": "blocked",
    "now": "waiting on the operator",
    "last_verified": "2026-09-01T18:00:00-04:00",
    "updated_at": "2026-09-01T20:02:00-04:00",
    "open_asks": ("Rename the colliding page, or merge it into the existing one?",),
    "hold_reason": "",
    "done_when": "Every page resolves.",
    "foreground_tasks": ({"task_id": "t1", "kind": "question", "text": "Rename it; the merge loses history."},),
}
WORKING = {
    "session_ref": "roundtop:ramble-build",
    "lane_id": "ramble-build",
    "goal": "Build the ramble planner.",
    "status": "working",
    "now": "wiring the route solver",
    "updated_at": "2026-09-01T20:00:00-04:00",
    "open_asks": (),
}
SECTIONS = [("monitor", [WORKING, BLOCKED])]


def test_markers_map_chitra_status_onto_the_boards_vocabulary():
    assert board_bridge.mark_for(WORKING) == "~"
    assert board_bridge.mark_for(BLOCKED) == "!"
    assert board_bridge.mark_for({"status": "done-pending-close"}) == "x"
    assert board_bridge.mark_for({"status": "idle"}) == " "
    assert board_bridge.mark_for({"status": "held"}) == " "
    # Statuses that need review even with no literal ask.
    for status in ("turn-finished-unverified", "completion-disputed", "done-pending-verification"):
        assert board_bridge.mark_for({"status": status}) == "!", status
    # An open ask outranks the status: it is a live request to the operator.
    assert board_bridge.mark_for({"status": "working", "open_asks": ("?",)}) == "!"


def test_plan_renders_one_card_per_lane():
    plan = board_bridge.render_plan(SECTIONS)
    assert "## ramble-build — Build the ramble planner. {#ramble-build}" in plan
    assert "tech: chitra · roundtop:ramble-build · monitor monitor" in plan
    assert "- [~] Build the ramble planner. {#ramble-build-goal}" in plan
    assert "  tech: wiring the route solver" in plan


def test_a_needs_input_card_carries_the_ask_on_its_tech_line():
    plan = board_bridge.render_plan(SECTIONS)
    assert "- [!] Backfill the Atlas wiki pages. {#wiki-backfill-goal}" in plan
    assert "tech: NEEDS-INPUT " in plan
    assert "Rename the colliding page, or merge it into the existing one?" in plan


def test_several_monitors_keep_their_lanes_apart():
    plan = board_bridge.render_plan([("monitor", [WORKING]), ("boomtown", [WORKING])])
    assert "{#monitor-ramble-build}" in plan
    assert "{#boomtown-ramble-build}" in plan


def test_only_needs_input_lanes_reach_the_escalation_stack():
    escalations = board_bridge.render_roster(SECTIONS)["escalations"]
    # The key carries the monitor: the page POSTs it back as-is, and it is
    # the only place /answer can learn which state root to write.
    assert list(escalations) == ["monitor:wiki-backfill"]


def test_an_escalation_carries_the_four_panel_sections_and_the_reveal():
    esc = board_bridge.render_roster(SECTIONS)["escalations"]["monitor:wiki-backfill"]
    assert esc["goal"] == "Backfill the Atlas wiki pages."
    assert esc["question"] == "Rename the colliding page, or merge it into the existing one?"
    # Context: the goal, what the lane says it is doing, and when it was last verified.
    assert "Now: waiting on the operator" in esc["context"]
    assert "Last verified: 2026-09-01T18:00:00-04:00" in esc["context"]
    # Recommendation prefers chitra's own suggested action.
    assert esc["recommendation"] == "Rename it; the merge loses history."
    find = esc["find"]
    assert find["peer"] == "tophand"  # the host half of the session_ref
    assert find["sid"] == "tophand:wiki-backfill"
    assert find["monitor"] == "monitor"
    assert find["lane"] == "wiki-backfill"
    # chitra's lane_anchor asserts tmux_session == lane_id, so the pane target follows.
    assert find["tty"] == "wiki-backfill:0.0"


def test_recommendation_falls_back_to_the_hold_reason_then_the_proof_owed():
    held = {"status": "blocked", "hold_reason": "Waiting on the NAS password rotation."}
    assert board_bridge.recommendation_of(held) == "Waiting on the NAS password rotation."
    owing = {
        "status": "completion-disputed",
        "enrolled_done_when_items": ({"id": "i1", "required_receipt": "suite-green"},),
    }
    assert board_bridge.recommendation_of(owing) == "Still owed as proof: suite-green"


def test_render_writes_both_files_into_the_workspace(tmp_path, monkeypatch):
    bridge = board_bridge.BoardBridge(tmp_path)
    monkeypatch.setattr(bridge, "sections", lambda: SECTIONS)
    bridge.render()

    assert "## wiki-backfill" in (tmp_path / "PLAN.md").read_text()
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert list(roster["escalations"]) == ["monitor:wiki-backfill"]


def test_node_ids_stay_inside_agenttrails_id_grammar():
    # `[a-z0-9][a-z0-9-]*` — agenttrail drops a heading whose id does not match.
    assert board_bridge.slug("C-nasplug/v2") == "c-nasplug-v2"
    assert board_bridge.slug("") == "lane"
    assert board_bridge.slug("!!!") == "lane"


def test_a_goal_can_never_forge_a_node_id():
    """A `{#...}` inside goal text would read as a heading id to agenttrail's
    parser and silently retarget the card."""
    plan = board_bridge.render_plan([("monitor", [{**WORKING, "goal": "Fix {#other-card} now"}])])
    assert "{#other-card}" not in plan
    assert "{#ramble-build}" in plan

"""Tests for the daemon that settles reported obstacles once per pass."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra.adjudication import Adjudication, BlockerClaim, Evidence
from chitra.adjudicatord import (
    AdjudicationRunError,
    RunReport,
    append_adjudication,
    build_directive_order,
    collect_claims,
    deliverable_directive,
    main,
    resolve_config,
    run_once,
    settled_claim_ids,
)
from chitra.goals import PRESUMED_ASK_PREFIX, GoalRecord, add_ask, get_goal, upsert_goal

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
SESSION_REF = "tophand:widget-build:0.0"


def _record(root: Path, *, needs: str = "") -> GoalRecord:
    return upsert_goal(
        root,
        GoalRecord(
            session_ref=SESSION_REF,
            goal="Ship the widget build service with its live acceptance evidence.",
            done_when="The widget build service runs green in continuous integration.",
            source="task-file:/tmp/widget.md",
            status="working",
            intent="The operator asked for a widget build service that other teams can call directly.",
            scope="In: the build service. Out: the reporting front end.",
            needs=needs,
        ),
    )


def _adjudication(*, verdict: str = "false-block", directive: str = "Continue against the recorded goal.") -> Adjudication:
    return Adjudication(
        claim=BlockerClaim(session_ref=SESSION_REF, text="I need you to merge this.", origin="open_ask", observed_at=NOW.isoformat()),
        claim_class="merge-rights",
        stage="deterministic",
        verdict=verdict,  # type: ignore[arg-type]
        evidence=(Evidence(source="capability-manifest", reference="capability auto-merge", finding="Merge a green pull request."),),
        directive=directive,
        basis="The capability manifest grants merging, so this does not need a person.",
        adjudicated_at=NOW.isoformat(),
    )


def test_every_recorded_ask_and_needs_line_becomes_one_claim(tmp_path: Path) -> None:
    _record(tmp_path, needs="I am waiting on your approval.")
    add_ask(tmp_path, SESSION_REF, "Can you merge the pull request?")
    claims = collect_claims([get_goal(tmp_path, SESSION_REF)], now=NOW)  # type: ignore[list-item]
    assert {claim.origin for claim in claims} == {"open_ask", "needs"}
    assert len(claims) == 2


def test_a_recorded_presumption_is_never_treated_as_an_obstacle(tmp_path: Path) -> None:
    _record(tmp_path)
    add_ask(tmp_path, SESSION_REF, f'{PRESUMED_ASK_PREFIX} scope was taken from the task file: "In: build. Out: front end."')
    claims = collect_claims([get_goal(tmp_path, SESSION_REF)], now=NOW)  # type: ignore[list-item]
    assert claims == []


def test_a_claim_identity_is_stable_across_passes(tmp_path: Path) -> None:
    _record(tmp_path, needs="I am waiting on your approval.")
    first = collect_claims([get_goal(tmp_path, SESSION_REF)], now=NOW)  # type: ignore[list-item]
    later = collect_claims([get_goal(tmp_path, SESSION_REF)], now=datetime(2026, 8, 17, tzinfo=UTC))  # type: ignore[list-item]
    assert first[0].claim_id == later[0].claim_id


def test_a_settled_claim_is_not_settled_twice(tmp_path: Path) -> None:
    log = tmp_path / "adjudications.jsonl"
    outcome = _adjudication()
    append_adjudication(log, outcome)
    assert settled_claim_ids(log) == {outcome.claim.claim_id}


def test_an_unreadable_record_line_does_not_hide_the_rest(tmp_path: Path) -> None:
    log = tmp_path / "adjudications.jsonl"
    outcome = _adjudication()
    append_adjudication(log, outcome)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    assert settled_claim_ids(log) == {outcome.claim.claim_id}


def test_a_directive_that_would_speak_in_the_operator_voice_falls_back_to_its_citation() -> None:
    outcome = _adjudication(directive='The operator already ruled: "just merge it".')
    fallback = deliverable_directive(outcome)
    assert "operator" not in fallback.lower()
    assert "capability auto-merge" in fallback


def test_a_plain_directive_is_delivered_unchanged() -> None:
    outcome = _adjudication()
    assert deliverable_directive(outcome) == "Continue against the recorded goal."


def test_only_a_refused_block_produces_a_directive_order() -> None:
    outcome = Adjudication(
        claim=BlockerClaim(session_ref=SESSION_REF, text="Should we spend money?", origin="needs", observed_at=NOW.isoformat()),
        claim_class="unclassified",
        stage="reasoned",
        verdict="operator-required",
        escalation="Do you want to spend money on a second storage volume?",
        escalation_class="spend",
        basis="Spending is yours to decide.",
        adjudicated_at=NOW.isoformat(),
    )
    with pytest.raises(AdjudicationRunError):
        build_directive_order(outcome)


def test_a_directive_order_carries_the_settled_text_and_a_stable_identity() -> None:
    outcome = _adjudication()
    order = build_directive_order(outcome)
    assert order.session_ref == SESSION_REF
    assert order.nudge == "Continue against the recorded goal."
    assert order.task_type == "blocker-adjudication"
    assert order.order_id == build_directive_order(outcome).order_id


def test_a_pass_records_what_it_could_not_settle_and_changes_nothing_else(tmp_path: Path) -> None:
    _record(tmp_path, needs="Here is a plain progress note with no obstacle in it.")
    config = resolve_config(
        state_dir=tmp_path,
        queue_dir=tmp_path / "queue",
        convlog_path=tmp_path / "conversation.jsonl",
        decisions_path=tmp_path / "decisions.jsonl",
        adjudication_log_path=tmp_path / "adjudications.jsonl",
    )
    report = run_once(config, adjudicator=None, now=NOW)
    assert report == RunReport(claims_seen=1, already_settled=0, refused=0, escalated=0, undetermined=1)
    assert not (tmp_path / "queue" / "orders").exists()
    recorded = [json.loads(line) for line in (tmp_path / "adjudications.jsonl").read_text(encoding="utf-8").splitlines()]
    assert recorded[0]["verdict"] == "undetermined"


def test_a_second_pass_settles_nothing_new(tmp_path: Path) -> None:
    _record(tmp_path, needs="Here is a plain progress note with no obstacle in it.")
    config = resolve_config(
        state_dir=tmp_path,
        queue_dir=tmp_path / "queue",
        convlog_path=tmp_path / "conversation.jsonl",
        decisions_path=tmp_path / "decisions.jsonl",
        adjudication_log_path=tmp_path / "adjudications.jsonl",
    )
    run_once(config, adjudicator=None, now=NOW)
    second = run_once(config, adjudicator=None, now=NOW)
    assert second.already_settled == 1
    assert second.undetermined == 0


def test_a_proposed_manifest_can_be_dry_run_before_it_lands(tmp_path: Path) -> None:
    """An operator can see what a manifest would decide before merging it.

    This is how wave one's auto-merge capability was checked against this
    resolver ahead of time: point the dry run at that branch's manifest and read
    the verdict it would produce.
    """
    manifest = tmp_path / "proposed.yaml"
    manifest.write_text(
        "schema: chitra.capabilities.v1\n"
        "capabilities:\n"
        "  - name: auto-merge\n"
        "    kind: daemon\n"
        "    purpose: Land green pull requests without a person in the path.\n"
        "    when_to_use: Run as the supervised merge daemon.\n"
        "    authority:\n"
        "      level: act\n"
        "      grants:\n"
        "        - Read one pull request's merge state through the GitHub GraphQL API.\n"
        "        - Merge one allowlisted, lane-authored, non-draft pull request whose merge state is clean.\n"
        "      excludes:\n"
        "        - Author or edit any code in the reviewed pull request.\n"
        "    default_enabled: true\n"
        "    commands:\n"
        "      - name: chitra-merge\n"
        "        description: Merge one pull request.\n"
        "        argv: [chitra-merge]\n"
        "        params: []\n"
        "        mutates: true\n",
        encoding="utf-8",
    )
    _record(tmp_path, needs="I cannot merge the pull request without you doing it for me.")
    config = resolve_config(
        state_dir=tmp_path,
        queue_dir=tmp_path / "queue",
        convlog_path=tmp_path / "conversation.jsonl",
        decisions_path=tmp_path / "decisions.jsonl",
        adjudication_log_path=tmp_path / "adjudications.jsonl",
        manifest_path=manifest,
    )
    report = run_once(config, adjudicator=None, now=NOW)
    assert report.refused == 1
    recorded = [json.loads(line) for line in (tmp_path / "adjudications.jsonl").read_text(encoding="utf-8").splitlines()]
    assert recorded[0]["verdict"] == "fleet-doable"
    assert "chitra-merge" in recorded[0]["directive"]


def test_the_shipped_manifest_does_not_claim_a_merge_route_it_lacks(tmp_path: Path) -> None:
    """Until a merge capability lands, a merge claim stays undetermined."""
    _record(tmp_path, needs="I cannot merge the pull request without you doing it for me.")
    config = resolve_config(
        state_dir=tmp_path,
        queue_dir=tmp_path / "queue",
        convlog_path=tmp_path / "conversation.jsonl",
        decisions_path=tmp_path / "decisions.jsonl",
        adjudication_log_path=tmp_path / "adjudications.jsonl",
    )
    assert run_once(config, adjudicator=None, now=NOW).undetermined == 1


def test_the_daemon_refuses_to_act_while_its_capability_is_disabled(tmp_path: Path) -> None:
    """The hold is enforced in code, not only in the pull request label."""
    _record(tmp_path, needs="I cannot merge the pull request without you doing it for me.")
    exit_code = main(
        [
            "--once",
            "--state-dir",
            str(tmp_path),
            "--queue-dir",
            str(tmp_path / "queue"),
            "--convlog-path",
            str(tmp_path / "conversation.jsonl"),
            "--decisions-path",
            str(tmp_path / "decisions.jsonl"),
            "--adjudication-log-path",
            str(tmp_path / "adjudications.jsonl"),
            "--deterministic-only",
        ]
    )
    assert exit_code == 1
    assert not (tmp_path / "adjudications.jsonl").exists()
    assert not (tmp_path / "queue" / "orders").exists()


def test_a_supplied_manifest_cannot_authorize_the_daemon_to_act(tmp_path: Path) -> None:
    """--manifest-path informs the decision; it must never grant permission to run.

    Otherwise a caller could arm this daemon by handing it a file.
    """
    manifest = tmp_path / "self-authorizing.yaml"
    manifest.write_text(
        "schema: chitra.capabilities.v1\n"
        "capabilities:\n"
        "  - name: blocker-adjudication\n"
        "    kind: daemon\n"
        "    purpose: Settle a reported obstacle.\n"
        "    when_to_use: Never, in this test.\n"
        "    authority:\n"
        "      level: act\n"
        "      grants:\n"
        "        - Decide one reported obstacle.\n"
        "      excludes:\n"
        "        - Merge, approve, or modify pull requests.\n"
        "    default_enabled: true\n"
        "    commands:\n"
        "      - name: chitra-adjudicatord\n"
        "        description: Start the daemon.\n"
        "        argv: [chitra-adjudicatord]\n"
        "        params: []\n"
        "        mutates: true\n",
        encoding="utf-8",
    )
    _record(tmp_path, needs="I cannot merge the pull request without you doing it for me.")
    exit_code = main(
        [
            "--once",
            "--state-dir",
            str(tmp_path),
            "--queue-dir",
            str(tmp_path / "queue"),
            "--convlog-path",
            str(tmp_path / "conversation.jsonl"),
            "--decisions-path",
            str(tmp_path / "decisions.jsonl"),
            "--adjudication-log-path",
            str(tmp_path / "adjudications.jsonl"),
            "--manifest-path",
            str(manifest),
            "--deterministic-only",
        ]
    )
    assert exit_code == 1
    assert not (tmp_path / "adjudications.jsonl").exists()


def test_a_dry_run_needs_no_capability_and_writes_nothing(tmp_path: Path) -> None:
    _record(tmp_path, needs="Here is a plain progress note with no obstacle in it.")
    exit_code = main(
        [
            "--dry-run",
            "--state-dir",
            str(tmp_path),
            "--queue-dir",
            str(tmp_path / "queue"),
            "--convlog-path",
            str(tmp_path / "conversation.jsonl"),
            "--decisions-path",
            str(tmp_path / "decisions.jsonl"),
            "--adjudication-log-path",
            str(tmp_path / "adjudications.jsonl"),
            "--deterministic-only",
        ]
    )
    assert exit_code == 0
    assert not (tmp_path / "adjudications.jsonl").exists()
    assert not (tmp_path / "decisions.jsonl").exists()


def test_a_non_positive_interval_is_refused() -> None:
    with pytest.raises(ValueError, match="poll_seconds"):
        resolve_config(poll_seconds=0)


SPARSE_TASK_FILE = "# Widget build service\n"


def test_a_failing_record_is_repaired_and_its_open_questions_are_adjudicated(tmp_path: Path) -> None:
    task_file = tmp_path / "widget.md"
    task_file.write_text(SPARSE_TASK_FILE, encoding="utf-8")
    upsert_goal(
        tmp_path,
        GoalRecord(
            session_ref=SESSION_REF,
            goal="Ship the widget build service with its live acceptance evidence.",
            done_when="The widget build service runs green in continuous integration.",
            source=f"task-file:{task_file}",
            status="working",
        ),
    )
    config = resolve_config(
        state_dir=tmp_path,
        queue_dir=tmp_path / "queue",
        convlog_path=tmp_path / "conversation.jsonl",
        decisions_path=tmp_path / "decisions.jsonl",
        adjudication_log_path=tmp_path / "adjudications.jsonl",
    )
    report = run_once(config, adjudicator=None, now=NOW)
    assert report.claims_seen == 2
    assert report.undetermined == 2
    recorded = [json.loads(line) for line in (tmp_path / "adjudications.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {item["claim"]["origin"] for item in recorded} == {"interview"}


def test_the_repair_pass_can_be_turned_off(tmp_path: Path) -> None:
    task_file = tmp_path / "widget.md"
    task_file.write_text(SPARSE_TASK_FILE, encoding="utf-8")
    upsert_goal(
        tmp_path,
        GoalRecord(
            session_ref=SESSION_REF,
            goal="Ship the widget build service with its live acceptance evidence.",
            done_when="The widget build service runs green in continuous integration.",
            source=f"task-file:{task_file}",
            status="working",
        ),
    )
    config = resolve_config(
        state_dir=tmp_path,
        queue_dir=tmp_path / "queue",
        convlog_path=tmp_path / "conversation.jsonl",
        decisions_path=tmp_path / "decisions.jsonl",
        adjudication_log_path=tmp_path / "adjudications.jsonl",
        repair_specifications=False,
    )
    assert run_once(config, adjudicator=None, now=NOW).claims_seen == 0

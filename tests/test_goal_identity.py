"""Focused tests for the durable logical identity carried by each goal."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from _goal_fixtures import enrollment_fields, ingest_passing_receipt, passing_completion_evidence

from chitra.goals import (
    EnrolledDoneWhenItem,
    GoalRecord,
    GoalValidationError,
    InterviewReceipt,
    load_goals,
    mark_completion_gate_passed,
    record_review_restart,
    redirect_goal,
    transfer_goal,
    update_now,
    upsert_goal,
)


def _record(session_ref: str = "tophand:identity:0.0") -> GoalRecord:
    done_when = "The identity behavior has focused tests and passes validation."
    fields = enrollment_fields(done_when)
    return GoalRecord(
        session_ref=session_ref,
        goal="Preserve one logical goal identity through every lifecycle transition.",
        done_when=done_when,
        source="task-file:goal-identity.md",
        status="working",
        intent="Keep one logical goal traceable while its physical lane changes.",
        scope="Goal identity storage and lifecycle transitions only.",
        interview_receipt=cast(InterviewReceipt, fields["interview_receipt"]),
        enrolled_done_when_items=cast(tuple[EnrolledDoneWhenItem, ...], fields["enrolled_done_when_items"]),
    )


def test_enrollment_assigns_a_fresh_id_inside_the_store_write(tmp_path: Path) -> None:
    draft = _record()

    assert draft.goal_id == ""
    stored = upsert_goal(tmp_path, draft)

    assert stored.goal_id.startswith("goal-")
    assert load_goals(tmp_path)[0].goal_id == stored.goal_id
    persisted = json.loads((tmp_path / "goals.json").read_text(encoding="utf-8"))
    assert persisted["schema"] == "chitra.goals.v4"
    assert persisted["goals"][0]["goal_id"] == stored.goal_id

    other = upsert_goal(tmp_path, _record("tophand:identity-other:0.0"))
    assert other.goal_id != stored.goal_id


def test_normal_enrollment_does_not_accept_a_caller_enrollment_timestamp(tmp_path: Path) -> None:
    supplied_at = "2000-01-01T00:00:00+00:00"
    stored = upsert_goal(tmp_path, replace(_record(), enrolled_at=supplied_at))

    assert stored.enrolled_at != supplied_at
    assert stored.enrolled_done_when == stored.done_when


def test_legacy_load_backfills_the_same_visible_deterministic_id(tmp_path: Path) -> None:
    payload = {
        "schema": "chitra.goals.v1",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "goals": [
            {
                "session_ref": "tophand:legacy-identity:0.0",
                "goal": "Keep one legacy goal readable while the store gains identity.",
                "done_when": "The old record remains readable without migration.",
                "source": "main",
                "status": "working",
                "now": "loading",
                "last_verified": "",
                "created_at": "2026-07-09T00:00:00+00:00",
                "updated_at": "2026-07-09T00:00:00+00:00",
            }
        ],
    }
    path = tmp_path / "goals.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    first = load_goals(tmp_path)[0]
    second = load_goals(tmp_path)[0]

    assert first.goal_id.startswith("legacy-")
    assert first.goal_id == second.goal_id
    assert "goal_id" not in payload["goals"][0]

    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["goals"][0]["goal"] = "Keep the same legacy goal readable after a redirect."
    path.write_text(json.dumps(changed), encoding="utf-8")
    assert load_goals(tmp_path)[0].goal_id == first.goal_id


def test_legacy_id_ignores_mutable_document_updated_at(tmp_path: Path) -> None:
    payload = {
        "schema": "chitra.goals.v1",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "goals": [
            {
                "session_ref": "tophand:legacy-anchor:0.0",
                "goal": "Keep the legacy identity stable without a migration write.",
                "done_when": "The deterministic identity remains unchanged after reload.",
                "source": "main",
                "status": "working",
                "now": "loading",
                "last_verified": "",
                "created_at": "",
                "updated_at": "",
            }
        ],
    }
    path = tmp_path / "goals.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    first = load_goals(tmp_path)[0]
    payload["updated_at"] = "2099-12-31T23:59:59+00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    second = load_goals(tmp_path)[0]

    assert first.goal_id == second.goal_id
    assert first.enrolled_at == second.enrolled_at == "1970-01-01T00:00:00+00:00"


def test_legacy_transfer_chain_uses_one_root_id_and_lane(tmp_path: Path) -> None:
    root_ref = "tophand:legacy-chain:0.0"
    successor_ref = "tophand:legacy-chain-xfer:0.0"
    done_when = "The legacy transfer chain remains readable and traceable."
    payload = {
        "schema": "chitra.goals.v2",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "goals": [
            {
                "session_ref": root_ref,
                "goal": "Keep the legacy transfer root readable during migration.",
                "done_when": done_when,
                "source": "main",
                "status": "held",
                "now": "transferred",
                "last_verified": "",
                "created_at": "2026-07-09T00:00:00+00:00",
                "updated_at": "2026-07-09T00:00:00+00:00",
                "transferred_to": successor_ref,
            },
            {
                "session_ref": successor_ref,
                "goal": "Keep the legacy transfer successor readable during migration.",
                "done_when": done_when,
                "source": "main; digest:handoff",
                "status": "idle",
                "now": "scaffolded",
                "last_verified": "",
                "created_at": "2026-07-09T00:00:00+00:00",
                "updated_at": "2026-07-09T00:00:00+00:00",
                "successor_of": root_ref,
            },
        ],
    }
    (tmp_path / "goals.json").write_text(json.dumps(payload), encoding="utf-8")

    records = load_goals(tmp_path)

    assert records[0].goal_id.startswith("legacy-")
    assert records[1].goal_id == records[0].goal_id
    assert records[1].lane_id == records[0].lane_id == "legacy-chain"


def test_partial_v4_transfer_migration_uses_the_established_root_identity(tmp_path: Path) -> None:
    root_ref = "tophand:partial-chain:0.0"
    successor_ref = "tophand:partial-chain-xfer:0.0"
    root = _record(root_ref)
    root = replace(root, status="held", goal_id="goal-partial", transferred_to=successor_ref)
    successor = replace(
        root,
        session_ref=successor_ref,
        status="working",
        goal_id="",
        successor_of=root_ref,
        transferred_to="",
    )
    payload = {
        "schema": "chitra.goals.v4",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "goals": [root.to_dict(), successor.to_dict()],
    }
    (tmp_path / "goals.json").write_text(json.dumps(payload), encoding="utf-8")

    records = load_goals(tmp_path)

    assert [record.goal_id for record in records] == ["goal-partial", "goal-partial"]
    assert [record.lane_id for record in records] == ["partial-chain", "partial-chain"]


def test_established_v4_transfer_id_conflict_fails_closed(tmp_path: Path) -> None:
    root_ref = "tophand:conflicting-chain:0.0"
    successor_ref = "tophand:conflicting-chain-xfer:0.0"
    root = replace(_record(root_ref), status="held", goal_id="goal-root", transferred_to=successor_ref)
    successor = replace(
        _record(successor_ref),
        goal_id="goal-successor",
        lane_id=root.lane_id,
        successor_of=root_ref,
    )
    payload = {
        "schema": "chitra.goals.v4",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "goals": [root.to_dict(), successor.to_dict()],
    }
    (tmp_path / "goals.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="conflicting goal_id"):
        load_goals(tmp_path)


def test_lifecycle_transitions_preserve_goal_id(tmp_path: Path) -> None:
    enrolled = upsert_goal(tmp_path, _record())
    goal_id = enrolled.goal_id

    redirected = redirect_goal(tmp_path, enrolled.session_ref, reason="operator clarified scope", scope="Identity lifecycle only.")
    restarted = record_review_restart(
        tmp_path,
        enrolled.session_ref,
        previous_contract_id="contract-old",
        restarted_contract_id="contract-new",
        behavior_sha256="a" * 64,
    )
    assert redirected.goal_id == goal_id
    assert restarted.goal_id == goal_id

    ingest_passing_receipt(tmp_path, enrolled.session_ref)
    completed = mark_completion_gate_passed(
        tmp_path,
        enrolled.session_ref,
        now="all validators passed",
        last_verified="receipts/tests-green.json",
        completion_evidence=(passing_completion_evidence(),),
    )
    assert completed.goal_id == goal_id

    held, successor = transfer_goal(
        tmp_path,
        enrolled.session_ref,
        to_backend="claude",
        digest="handoff-identity",
        reason="provider replacement",
    )
    assert held.goal_id == goal_id
    assert successor.goal_id == goal_id
    assert successor.lane_id == enrolled.lane_id
    assert successor.enrolled_at == enrolled.enrolled_at
    assert successor.enrolled_done_when == enrolled.enrolled_done_when


def test_goal_id_cannot_be_replaced_on_an_existing_record(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())

    with pytest.raises(GoalValidationError, match="goal_id is immutable"):
        upsert_goal(tmp_path, replace(stored, goal_id="goal-replacement"))


def test_existing_update_rejects_a_duplicate_active_goal_id(tmp_path: Path) -> None:
    first = upsert_goal(tmp_path, _record())
    second = upsert_goal(tmp_path, _record("tophand:identity-other:0.0"))
    path = tmp_path / "goals.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["goals"][1]["goal_id"] = first.goal_id
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate active goal_id"):
        update_now(tmp_path, second.session_ref, now="must not bypass identity collision")

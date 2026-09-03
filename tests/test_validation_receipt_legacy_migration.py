"""Compatibility and isolation tests for pre-session validation receipts."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from _goal_fixtures import VALID_INTERVIEW_RECEIPT, ingest_passing_receipt

from chitra.goals import EnrolledDoneWhenItem, GoalRecord, render_done_when_items, upsert_goal
from chitra.validation_receipts import _copy_file_atomic, list_receipts, load_receipt_file, receipt_path, receipts_root, verify_receipt


def _goal(session_ref: str, receipt_name: str) -> GoalRecord:
    done_when = f"The receipt {receipt_name} passes for {session_ref}."
    return GoalRecord(
        session_ref=session_ref,
        goal=f"Keep {session_ref} under exact receipt oversight always.",
        done_when=done_when,
        source="test:legacy-receipt-migration",
        status="working",
        intent="Keep validation evidence bound to one exact session.",
        scope="Legacy receipt migration only.",
        interview_receipt=VALID_INTERVIEW_RECEIPT,
        enrolled_done_when_items=(
            EnrolledDoneWhenItem(
                id="done-1",
                text=done_when,
                validator="pytest",
                required_receipt=receipt_name,
            ),
        ),
    )


def _make_legacy(
    root: Path,
    session_ref: str,
    receipt_name: str,
    *,
    preserve_session_directory: bool = False,
) -> Path:
    """Move a newly written session receipt back to the old root layout."""
    canonical = ingest_passing_receipt(root, session_ref, receipt_name=receipt_name)
    legacy = receipts_root(root) / f"{receipt_name}.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(canonical, legacy)
    receipt, _raw = load_receipt_file(canonical)
    for artifact in receipt.artifacts:
        source = canonical.parent / artifact.path
        destination = legacy.parent / artifact.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    if preserve_session_directory:
        canonical.unlink()
    else:
        shutil.rmtree(canonical.parent)
    assert legacy.exists()
    return legacy


def test_mixed_canonical_and_legacy_layouts_are_isolated_and_migrated(tmp_path: Path) -> None:
    first = _goal("host:legacy-first:0.0", "first-green")
    second = _goal("host:legacy-second:0.0", "second-green")
    upsert_goal(tmp_path, first)
    upsert_goal(tmp_path, second)
    canonical = ingest_passing_receipt(tmp_path, first.session_ref, receipt_name="first-green")
    legacy = _make_legacy(tmp_path, second.session_ref, "second-green")

    assert verify_receipt(tmp_path, first.session_ref, "first-green").completion_eligible is True
    migrated = receipt_path(tmp_path, second.session_ref, "second-green")
    assert verify_receipt(tmp_path, second.session_ref, "second-green").completion_eligible is True
    assert migrated.exists()
    assert not legacy.exists()
    assert [item.receipt_name for item in list_receipts(tmp_path, first.session_ref)] == ["first-green"]
    assert [item.receipt_name for item in list_receipts(tmp_path, second.session_ref)] == ["second-green"]
    assert canonical.exists()


def test_legacy_receipt_merges_into_an_existing_session_directory(tmp_path: Path) -> None:
    session_ref = "host:mixed-layout:0.0"
    first_name = "first-green"
    second_name = "second-green"
    goal = _goal(session_ref, first_name)
    items = (
        *goal.enrolled_done_when_items,
        EnrolledDoneWhenItem(
            id="done-2",
            text="The second legacy receipt also passes.",
            validator="pytest",
            required_receipt=second_name,
        ),
    )
    goal = replace(goal, done_when=render_done_when_items(items), enrolled_done_when_items=items)
    upsert_goal(tmp_path, goal)
    first = ingest_passing_receipt(tmp_path, session_ref, receipt_name=first_name)
    legacy = _make_legacy(
        tmp_path,
        session_ref,
        second_name,
        preserve_session_directory=True,
    )

    second = receipt_path(tmp_path, session_ref, second_name)
    assert verify_receipt(tmp_path, session_ref, second_name).completion_eligible is True
    assert first.exists()
    assert second.exists()
    assert not legacy.exists()
    assert [item.receipt_name for item in list_receipts(tmp_path, session_ref)] == [
        first_name,
        second_name,
    ]


def test_same_name_legacy_receipt_is_rejected_for_every_ambiguous_session(tmp_path: Path) -> None:
    first = _goal("host:same-name-first:0.0", "shared-green")
    second = _goal("host:same-name-second:0.0", "shared-green")
    upsert_goal(tmp_path, first)
    _make_legacy(tmp_path, first.session_ref, "shared-green")
    upsert_goal(tmp_path, second)

    first_check = verify_receipt(tmp_path, first.session_ref, "shared-green")
    second_check = verify_receipt(tmp_path, second.session_ref, "shared-green")
    assert first_check.verified is False
    assert second_check.verified is False
    assert "not uniquely bound" in first_check.issues[0]
    assert "not uniquely bound" in second_check.issues[0]
    assert (receipts_root(tmp_path) / "shared-green.json").exists()
    assert not receipt_path(tmp_path, first.session_ref, "shared-green").exists()
    assert not receipt_path(tmp_path, second.session_ref, "shared-green").exists()


def test_failed_migration_keeps_legacy_receipt_and_leaves_no_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_ref = "host:rollback-lane:0.0"
    receipt_name = "rollback-green"
    upsert_goal(tmp_path, _goal(session_ref, receipt_name))
    legacy = _make_legacy(tmp_path, session_ref, receipt_name)
    destination = receipt_path(tmp_path, session_ref, receipt_name)

    def fail_copy(source: Path, target: Path) -> None:
        del source, target
        raise OSError("simulated migration copy failure")

    monkeypatch.setattr("chitra.validation_receipts._copy_file_atomic", fail_copy)
    failed = verify_receipt(tmp_path, session_ref, receipt_name)
    assert failed.verified is False
    assert "simulated migration copy failure" in failed.issues[0]
    assert legacy.exists()
    assert not destination.exists()
    assert not list(destination.parent.glob(".*.migration-*"))

    monkeypatch.setattr("chitra.validation_receipts._copy_file_atomic", _copy_file_atomic)
    recovered = verify_receipt(tmp_path, session_ref, receipt_name)
    assert recovered.completion_eligible is True
    assert destination.exists()
    assert not legacy.exists()

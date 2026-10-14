"""The ``chitra.goal-records.v1`` projection manifest loads or fails closed.

The hybrid watcher projects each lane's goal contract into
``goal-records.json`` next to the transcript bindings. Loading must yield
exactly the records written — a malformed or internally inconsistent
manifest raises rather than enrolling a partial contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chitra.goal_records import (
    GOAL_RECORDS_SCHEMA,
    ProjectedDoneItem,
    ProjectedGoalRecord,
    load_goal_records,
    project_goal_record,
)


def _record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "session_ref": "lane-a",
        "lane": "lane-a",
        "goal": "integrate the checks",
        "done_when": "receipts exist on both hosts",
        "source": "lane-run brief",
        "updated_at": "2026-10-14T12:00:00Z",
        "intent": "",
        "scope": "",
        "enrolled_done_when_items": [
            {"id": "done-1", "text": "receipts exist on both hosts",
             "required_receipt": "done-1", "validator": ""}
        ],
    }
    base.update(overrides)
    return base


def _manifest(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps({"schema": GOAL_RECORDS_SCHEMA, "records": records}) + "\n",
        encoding="utf-8",
    )
    return path


def test_load_goal_records_projects_the_lane_contract(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "goal-records.json", [_record()])
    (record,) = load_goal_records(manifest)
    assert record.session_ref == "lane-a"
    assert record.lane_id == "lane-a"
    assert record.status == "working"
    assert record.goal == "integrate the checks"
    assert record.done_when == "receipts exist on both hosts"
    assert [item.id for item in record.enrolled_done_when_items] == ["done-1"]
    assert record.enrolled_done_when_items[0].required_receipt == "done-1"


def test_load_goal_records_tolerates_an_absent_manifest(tmp_path: Path) -> None:
    assert load_goal_records(tmp_path / "goal-records.json") == ()
    assert load_goal_records(None) == ()


def test_load_goal_records_fails_closed_on_bad_input(tmp_path: Path) -> None:
    bad_schema = tmp_path / "bad-schema.json"
    bad_schema.write_text(json.dumps({"schema": "chitra.goal-records.v9", "records": []}))
    with pytest.raises(ValueError, match="invalid goal records manifest"):
        load_goal_records(bad_schema)

    not_json = tmp_path / "not-json.json"
    not_json.write_text("{")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_goal_records(not_json)

    dup = _manifest(tmp_path / "dup.json", [_record(), _record()])
    with pytest.raises(ValueError, match="not unique"):
        load_goal_records(dup)

    extra_key = _manifest(
        tmp_path / "extra.json", [_record(unexpected="key")]
    )
    with pytest.raises(ValueError, match="invalid goal records manifest"):
        load_goal_records(extra_key)


def test_project_goal_record_marks_items_validator_free() -> None:
    record = project_goal_record(
        ProjectedGoalRecord(
            session_ref="lane-a",
            lane="lane-a",
            goal="g",
            done_when="d",
            source="lane-run brief",
            updated_at="2026-10-14T12:00:00Z",
            enrolled_done_when_items=(
                ProjectedDoneItem(id="done-1", text="d", required_receipt="done-1"),
            ),
        )
    )
    item = record.enrolled_done_when_items[0]
    assert item.validator == ""
    assert item.required_receipt == "done-1"

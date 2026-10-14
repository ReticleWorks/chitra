"""The ``chitra.goal-records.v1`` lane goal projection manifest.

Hybrid-monitor lanes do not enroll through the interactive interview: the
watcher projects each lane's goal record from its durable brief (goal text
plus the ``Done when`` conditions) into ``goal-records.json`` next to the
transcript bindings. ``monitord`` consumes this manifest when it selects the
goal contract a lane is supervised against; ``goals.json`` records stay
authoritative and always win a ``session_ref`` conflict.

A projected item is a *declared* done condition: it carries a stable item id
and the receipt name the lane must produce, but no registered validator is
pinned yet — the validator registry contract arrives with the platform's
record formats. ``detect_false_done`` treats a missing or unbound receipt as
an unmet item, so projection alone never turns a claim green.

Like the binding manifest, this file is strict: a malformed manifest fails
closed (no records load) rather than silently changing goal authority.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from chitra.goals import EnrolledDoneWhenItem, GoalRecord

GOAL_RECORDS_SCHEMA = "chitra.goal-records.v1"
DEFAULT_GOAL_RECORDS_FILENAME = "goal-records.json"

_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class ProjectedDoneItem(BaseModel):
    """One declared done-when item: stable id, text, and the required receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StrictStr = Field(min_length=1)
    text: StrictStr = Field(min_length=1)
    required_receipt: StrictStr = Field(min_length=1)
    validator: StrictStr = ""

    @field_validator("id", "required_receipt")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError(f"unsafe done-item identifier: {value!r}")
        return value


class ProjectedGoalRecord(BaseModel):
    """One lane's projected goal contract from the watcher."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_ref: StrictStr = Field(min_length=1)
    lane: StrictStr = Field(min_length=1)
    goal: StrictStr = Field(min_length=1)
    done_when: StrictStr = Field(min_length=1)
    source: StrictStr = Field(min_length=1)
    updated_at: StrictStr = Field(min_length=1)
    intent: StrictStr = ""
    scope: StrictStr = ""
    enrolled_done_when_items: tuple[ProjectedDoneItem, ...] = ()


class GoalRecordsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    schema_name: Literal["chitra.goal-records.v1"] = Field(alias="schema")
    records: list[ProjectedGoalRecord]


def project_goal_record(record: ProjectedGoalRecord) -> GoalRecord:
    """Project one manifest row into the GoalRecord detectors consume.

    The record is enrolled *in the monitoring sense*: it has a frozen,
    validator-declared item inventory the detectors can bind findings to,
    but no interview receipt — interview enrollment is the interactive path
    in ``goals.json`` and stays authoritative there.
    """
    items = tuple(
        EnrolledDoneWhenItem(
            id=item.id,
            text=item.text,
            validator=item.validator,
            required_receipt=item.required_receipt,
        )
        for item in record.enrolled_done_when_items
    )
    return GoalRecord(
        session_ref=record.session_ref,
        goal=record.goal,
        done_when=record.done_when,
        source=record.source,
        status="working",
        lane_id=record.lane,
        enrolled_done_when=record.done_when,
        enrolled_at=record.updated_at,
        intent=record.intent,
        scope=record.scope,
        now="",
        last_verified="",
        created_at=record.updated_at,
        updated_at=record.updated_at,
        enrolled_done_when_items=items,
    )


def load_goal_records(path: Path | None) -> tuple[GoalRecord, ...]:
    """Load projected lane goal records; absent manifest means none.

    A malformed manifest raises — the caller decides whether the pass can
    continue unenrolled — rather than returning a partial reading of goal
    authority.
    """
    if path is None:
        return ()
    manifest_path = path.expanduser()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ValueError(f"goal records manifest cannot be read: {manifest_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"goal records manifest is not valid JSON: {manifest_path}: {exc}") from exc
    try:
        document = GoalRecordsDocument.model_validate(payload)
    except ValueError as exc:
        raise ValueError(f"invalid goal records manifest {manifest_path}: {exc}") from exc
    seen: set[str] = set()
    records: list[GoalRecord] = []
    for row in document.records:
        if row.session_ref in seen:
            raise ValueError(f"goal record session_ref is not unique: {row.session_ref!r}")
        seen.add(row.session_ref)
        records.append(project_goal_record(row))
    return tuple(records)


__all__ = [
    "DEFAULT_GOAL_RECORDS_FILENAME",
    "GOAL_RECORDS_SCHEMA",
    "GoalRecordsDocument",
    "ProjectedDoneItem",
    "ProjectedGoalRecord",
    "load_goal_records",
    "project_goal_record",
]

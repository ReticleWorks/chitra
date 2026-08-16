"""Tests for the presumptive short interview that repairs a failing goal record."""

from __future__ import annotations

from pathlib import Path

import pytest

from chitra.goals import PRESUMED_ASK_PREFIX, GoalNotFoundError, GoalRecord, check_specification, get_goal, upsert_goal
from chitra.interview import (
    derive_presumptions,
    presumed_asks,
    presumptive_repair,
    read_primary_source,
)
from chitra.lane_anchor import LaneLaunchRefused, ingestion_gate
from chitra.lane_config import LaneCredentials, LaneSpec

SESSION_REF = "tophand:widget-build:0.0"

TASK_FILE = """# Widget build service

The operator asked for a widget build service that other teams can call directly,
replacing the hand-run script that breaks every week.

## Deliverables
- A running service with live acceptance evidence recorded.

## Constraints
- In: the build service itself. Out: the reporting front end and its own data store.
"""

SPARSE_TASK_FILE = """# Widget build service
"""


def _write_task_file(tmp_path: Path, body: str = TASK_FILE) -> Path:
    path = tmp_path / "widget.md"
    path.write_text(body, encoding="utf-8")
    return path


def _enrol(
    root: Path,
    *,
    source: str,
    intent: str = "",
    scope: str = "",
    done_when: str = "The widget build service runs green in continuous integration.",
) -> GoalRecord:
    return upsert_goal(
        root,
        GoalRecord(
            session_ref=SESSION_REF,
            goal="Ship the widget build service with its live acceptance evidence.",
            done_when=done_when,
            source=source,
            status="working",
            intent=intent,
            scope=scope,
        ),
    )


def test_a_record_that_already_passes_is_left_alone(tmp_path: Path) -> None:
    task_file = _write_task_file(tmp_path)
    _enrol(
        tmp_path,
        source=f"task-file:{task_file}",
        intent="The operator asked for a widget build service other teams can call directly.",
        scope="In: the build service. Out: the front end.",
    )
    outcome = presumptive_repair(tmp_path, SESSION_REF)
    assert not outcome.attempted
    assert outcome.presumptions == ()
    assert outcome.passes_check


@pytest.mark.parametrize("separator", [":", " "])
def test_the_primary_source_is_read_through_either_separator(tmp_path: Path, separator: str) -> None:
    task_file = _write_task_file(tmp_path)
    record = _enrol(tmp_path, source=f"task-file{separator}{task_file}")
    source = read_primary_source(record)
    assert source is not None
    assert source.kind == "task-file"
    assert source.reference == str(task_file)
    assert "widget build service" in source.text


def test_a_branch_source_carries_only_its_own_name(tmp_path: Path) -> None:
    record = _enrol(tmp_path, source="branch feat/widget-build-service")
    source = read_primary_source(record)
    assert source is not None
    assert source.kind == "branch"
    assert source.text == "feat widget build service"


def test_a_first_transcript_message_is_read_as_the_primary_source(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type": "assistant", "message": {"content": "ignored"}}\n'
        '{"type": "user", "message": {"content": [{"type": "text", "text": "Build the widget service for the other teams."}]}}\n',
        encoding="utf-8",
    )
    record = _enrol(tmp_path, source=f"transcript-first-msg {transcript}")
    source = read_primary_source(record)
    assert source is not None
    assert source.text == "Build the widget service for the other teams."


def test_intent_and_scope_are_derived_from_the_named_task_file(tmp_path: Path) -> None:
    task_file = _write_task_file(tmp_path)
    record = _enrol(tmp_path, source=f"task-file:{task_file}")
    source = read_primary_source(record)
    assert source is not None
    presumptions, unanswered = derive_presumptions(record, source)
    fields = {presumption.field for presumption in presumptions}
    assert fields == {"intent", "scope"}
    assert unanswered == ()
    intent = next(presumption for presumption in presumptions if presumption.field == "intent")
    assert "widget build service" in intent.value
    assert str(task_file) in intent.derived_from


def test_a_repair_writes_the_presumed_values_and_records_each_one(tmp_path: Path) -> None:
    task_file = _write_task_file(tmp_path)
    _enrol(tmp_path, source=f"task-file:{task_file}")
    outcome = presumptive_repair(tmp_path, SESSION_REF)
    assert outcome.passes_check
    assert outcome.remaining_issues == ()
    stored = get_goal(tmp_path, SESSION_REF)
    assert stored is not None
    assert check_specification(stored) == []
    assert len(presumed_asks(stored)) == 2
    assert all(ask.startswith(PRESUMED_ASK_PREFIX) for ask in presumed_asks(stored))
    assert stored.goal_version == 2
    assert "short interview" in stored.goal_history[-1]["reason"]


def test_a_completion_condition_is_never_presumed(tmp_path: Path) -> None:
    task_file = _write_task_file(tmp_path)
    _enrol(tmp_path, source=f"task-file:{task_file}", done_when="Service runs.")
    outcome = presumptive_repair(tmp_path, SESSION_REF)
    assert not outcome.passes_check
    assert any("done_when" in issue for issue in outcome.remaining_issues)
    assert [item.question.key for item in outcome.unanswered] == ["done_when"]
    assert {presumption.field for presumption in outcome.presumptions} == {"intent", "scope"}


def test_a_source_with_nothing_to_derive_presumes_nothing(tmp_path: Path) -> None:
    task_file = _write_task_file(tmp_path, SPARSE_TASK_FILE)
    _enrol(tmp_path, source=f"task-file:{task_file}")
    outcome = presumptive_repair(tmp_path, SESSION_REF)
    assert outcome.presumptions == ()
    assert {item.question.field for item in outcome.unanswered} == {"intent", "scope"}
    assert outcome.remaining_issues


def test_an_unreadable_source_presumes_nothing_and_says_so(tmp_path: Path) -> None:
    _enrol(tmp_path, source=f"task-file:{tmp_path / 'absent.md'}")
    outcome = presumptive_repair(tmp_path, SESSION_REF)
    assert outcome.attempted
    assert outcome.presumptions == ()
    assert outcome.unanswered
    assert outcome.remaining_issues


def test_repairing_a_record_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(GoalNotFoundError):
        presumptive_repair(tmp_path, SESSION_REF)


def _lane(tmp_path: Path) -> LaneSpec:
    return LaneSpec(
        identifier="widget-build",
        account="chitra-widget",
        uid=4001,
        home=tmp_path / "home",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        state_dir=tmp_path,
        tmux_socket=tmp_path / "tmux.sock",
        tmux_session="widget-build",
        credentials=LaneCredentials(
            claude_credentials=tmp_path / "creds.json",
            ssh_dispatch_key=tmp_path / "id_ed25519",
        ),
    )


def test_the_launch_gate_repairs_before_it_refuses(tmp_path: Path) -> None:
    task_file = _write_task_file(tmp_path)
    _enrol(tmp_path, source=f"task-file:{task_file}")
    goal = ingestion_gate(_lane(tmp_path))
    assert check_specification(goal) == []
    assert presumed_asks(goal)


def test_the_launch_gate_still_refuses_what_the_source_cannot_settle(tmp_path: Path) -> None:
    task_file = _write_task_file(tmp_path, SPARSE_TASK_FILE)
    _enrol(tmp_path, source=f"task-file:{task_file}")
    with pytest.raises(LaneLaunchRefused, match="ingestion gate failed"):
        ingestion_gate(_lane(tmp_path))


def test_the_older_refusing_behaviour_is_still_available(tmp_path: Path) -> None:
    task_file = _write_task_file(tmp_path)
    _enrol(tmp_path, source=f"task-file:{task_file}")
    with pytest.raises(LaneLaunchRefused, match="ingestion gate failed"):
        ingestion_gate(_lane(tmp_path), repair=None)

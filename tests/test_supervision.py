from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from chitra.supervision import SupervisionLedger, SupervisionRecord, deterministic_order_id, goal_digest


def _record(*, revision: int = 1, state: str = "observing", event_id: str = "") -> SupervisionRecord:
    return SupervisionRecord(
        revision=revision,
        event_id=event_id,
        lane="lane_a",
        session_ref="host:lane_a:0",
        goal_version=1,
        goal_digest="d" * 64,
        state=state,  # type: ignore[arg-type]
        reason="test",
    )


def test_round_trip_and_monotonic_transition(tmp_path: Path) -> None:
    ledger = SupervisionLedger(tmp_path, "lane_a")
    first = ledger.append(_record())
    second = ledger.transition(
        state="action_pending",
        session_ref=first.session_ref,
        goal_version=first.goal_version,
        goal_digest_value=first.goal_digest,
        reason="finding",
    )
    assert second.revision == 2
    assert second.event_id.startswith("sha256:")
    assert ledger.latest() == second
    assert len(ledger.load()) == 2


def test_transition_rejection(tmp_path: Path) -> None:
    ledger = SupervisionLedger(tmp_path, "lane_a")
    first = ledger.append(_record())
    completed = ledger.transition(
        state="completion_verified",
        session_ref=first.session_ref,
        goal_version=1,
        goal_digest_value=first.goal_digest,
        reason="verified",
    )
    with pytest.raises(ValueError, match="invalid supervision transition"):
        ledger.transition(
            state="action_pending",
            session_ref=completed.session_ref,
            goal_version=1,
            goal_digest_value=completed.goal_digest,
            reason="late finding",
        )


def test_truncated_final_line_is_ignored(tmp_path: Path) -> None:
    ledger = SupervisionLedger(tmp_path, "lane_a")
    first = ledger.append(_record())
    path = ledger.path
    with path.open("ab") as handle:
        handle.write(b'{"schema":"chitra.supervision.v1","revision":2')
    assert ledger.latest() == first
    assert len(ledger.load()) == 1


def test_append_repairs_truncated_final_line_before_writing(tmp_path: Path) -> None:
    ledger = SupervisionLedger(tmp_path, "lane_a")
    first = ledger.append(_record())
    with ledger.path.open("ab") as handle:
        handle.write(b'{"schema":"chitra.supervision.v1","revision":2')
    second = ledger.transition(
        state="action_pending",
        session_ref=first.session_ref,
        goal_version=1,
        goal_digest_value=first.goal_digest,
        reason="reconciled",
    )
    assert second.revision == 2
    assert [row.revision for row in ledger.load()] == [1, 2]


def test_corrupt_interior_row_fails_closed(tmp_path: Path) -> None:
    ledger = SupervisionLedger(tmp_path, "lane_a")
    first = ledger.append(_record())
    second = ledger.transition(
        state="action_pending",
        session_ref=first.session_ref,
        goal_version=1,
        goal_digest_value=first.goal_digest,
        reason="finding",
    )
    rows = ledger.path.read_text(encoding="utf-8").splitlines()
    rows.insert(1, "not-json")
    ledger.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid supervision row"):
        ledger.load()
    assert second.revision == 2


class _Item(BaseModel):
    id: str
    text: str
    validator: str
    required_receipt: str


class _Goal(BaseModel):
    session_ref: str = "host:lane_a:0"
    lane_id: str = "lane_a"
    goal_version: int = 1
    goal: str = "Ship it"
    intent: str = "Deliver the outcome"
    scope: str = "The repository"
    done_when: str = "Tests pass"
    enrolled_done_when: str = "Tests pass"
    enrolled_done_when_items: tuple[_Item, ...] = (_Item(id="tests", text="Tests pass", validator="pytest", required_receipt="ok"),)


def test_goal_digest_is_sensitive_to_frozen_contract_fields() -> None:
    goal = _Goal()
    assert goal_digest(goal) == goal_digest(goal)
    assert goal_digest(goal.model_copy(update={"scope": "A different repository"})) != goal_digest(goal)
    assert goal_digest(goal.model_copy(update={"goal_version": 2})) != goal_digest(goal)
    assert goal_digest(goal.model_copy(update={"done_when": "A different proof"})) != goal_digest(goal)
    assert goal_digest(goal.model_copy(update={"session_ref": "host:other:0"})) != goal_digest(goal)


def test_order_ids_are_deterministic_and_stage_sensitive() -> None:
    first = deterministic_order_id("host:lane_a:0", 1, "f" * 64, "nudge")
    assert first == deterministic_order_id("host:lane_a:0", 1, "f" * 64, "nudge")
    assert first != deterministic_order_id("host:lane_a:0", 1, "f" * 64, "redirect")
    assert first != deterministic_order_id(
        "host:lane_a:0", 1, "f" * 64, "nudge", retry_attempt=1
    )


def test_action_and_consumption_lookups_survive_sibling_rows(tmp_path: Path) -> None:
    ledger = SupervisionLedger(tmp_path, "lane_a")
    digest = "d" * 64
    first = ledger.transition(
        state="action_pending",
        session_ref="host:lane_a:0",
        goal_version=1,
        goal_digest_value=digest,
        reason="first action",
        finding_fingerprint="first",
        stage="nudge",
    )
    ledger.transition(
        state="action_queued",
        session_ref=first.session_ref,
        goal_version=1,
        goal_digest_value=digest,
        reason="first queued",
        finding_fingerprint="first",
        stage="nudge",
    )
    consumed = ledger.transition(
        state="awaiting_progress",
        session_ref=first.session_ref,
        goal_version=1,
        goal_digest_value=digest,
        reason="first consumed",
        finding_fingerprint="first",
        stage="nudge",
        observed_event_id="user-1",
        turn_boundary_event_id="turn-1",
    )
    ledger.transition(
        state="action_pending",
        session_ref=first.session_ref,
        goal_version=1,
        goal_digest_value=digest,
        reason="sibling action",
        finding_fingerprint="second",
        stage="redirect",
        observed_event_id="",
        turn_boundary_event_id="",
    )

    assert ledger.latest_for_action("first", "nudge") == consumed
    assert ledger.latest_consumed_boundary(goal_digest_value=digest) == consumed
    assert ledger.latest_consumed_boundary(goal_digest_value="e" * 64) is None

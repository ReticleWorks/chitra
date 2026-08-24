"""Focused hostile tests for goal-blocker authorization.

The blocked-goal transition authorized here is a goal-level work-stoppage
concept, deliberately disjoint from the ordinary lane operational status
named ``blocked``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra.goals import (
    BlockerTurnReceipt,
    GOAL_BLOCKER_SCHEMA,
    GOAL_BLOCKED_MARKER,
    GOAL_STATUSES,
    GoalBlockerError,
    REQUIRED_BLOCKER_OBSERVATIONS,
    authorize_goal_block_transition,
    goal_blockers_path,
    load_goal_blocker_receipts,
    record_goal_blocker_receipts,
)

DECISION_CLOCK = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
ROUTE_VALID_UNTIL = "2026-08-24T18:00:00+00:00"
PROOF = "sha256:" + "a" * 64


def _receipt(
    index: int,
    *,
    sequence: int | None = None,
    observer: str = "ctrl-1",
    condition: str = "ext-cond-1",
    expires_at: str = ROUTE_VALID_UNTIL,
    observed_at: str | None = None,
    receipt_id: str | None = None,
) -> BlockerTurnReceipt:
    return BlockerTurnReceipt(
        receipt_id=receipt_id or f"receipt-{index}",
        condition_id=condition,
        observer_id=observer,
        turn_sequence=sequence if sequence is not None else index + 1,
        observed_at=observed_at or f"2026-08-24T{10 + index:02d}:00:00+00:00",
        route_expires_at=expires_at,
    )


def _authorized(receipts: list[BlockerTurnReceipt], assertor: str = "agent-7", **kwargs: object) -> object:
    return authorize_goal_block_transition(
        receipts=receipts,
        assertor_id=assertor,
        no_work_remaining_proof=kwargs.get("proof", PROOF),  # type: ignore[arg-type]
        now=DECISION_CLOCK,
        approver_id=kwargs.get("approver"),  # type: ignore[arg-type]
    )


def test_three_consecutive_controller_observations_authorize_the_stop() -> None:
    decision = _authorized([_receipt(0), _receipt(1), _receipt(2)])
    assert decision.authorized is True  # type: ignore[attr-defined]


def test_coordinator_silence_is_insufficient() -> None:
    decision = _authorized([_receipt(0), _receipt(1)])
    assert decision.authorized is False  # type: ignore[attr-defined]
    assert "silence" in decision.reason  # type: ignore[attr-defined]
    assert REQUIRED_BLOCKER_OBSERVATIONS == 3


def test_self_asserted_blocker_is_rejected() -> None:
    receipts = [_receipt(0, observer="agent-7"), _receipt(1), _receipt(2)]
    decision = _authorized(receipts)
    assert decision.authorized is False  # type: ignore[attr-defined]
    assert "self-created" in decision.reason  # type: ignore[attr-defined]


def test_self_approved_blocker_is_rejected() -> None:
    decision = _authorized([_receipt(0), _receipt(1), _receipt(2)], approver="agent-7")
    assert decision.authorized is False  # type: ignore[attr-defined]
    assert "created and approved" in decision.reason  # type: ignore[attr-defined]


def test_observer_matching_the_approver_is_rejected() -> None:
    receipts = [_receipt(0, observer="coordinator-9"), _receipt(1, observer="coordinator-9"), _receipt(2)]
    decision = authorize_goal_block_transition(
        receipts=receipts,
        assertor_id="agent-7",
        approver_id="coordinator-9",
        no_work_remaining_proof=PROOF,
        now=DECISION_CLOCK,
    )
    assert decision.authorized is False


def test_expired_route_or_lease_is_insufficient() -> None:
    stale_route = _receipt(0, expires_at="2026-08-24T06:00:00+00:00")
    decision = _authorized([stale_route, _receipt(1), _receipt(2)])
    assert decision.authorized is False  # type: ignore[attr-defined]
    assert "expired route or lease" in decision.reason  # type: ignore[attr-defined]


def test_missing_no_work_proof_is_rejected() -> None:
    for bogus in ("", "because the lane said so", "sha256:xyz"):
        decision = _authorized([_receipt(0), _receipt(1), _receipt(2)], proof=bogus)
        assert decision.authorized is False  # type: ignore[attr-defined]


def test_unstable_condition_id_is_rejected() -> None:
    receipts = [_receipt(0), _receipt(1, condition="ext-cond-2"), _receipt(2)]
    decision = _authorized(receipts)
    assert decision.authorized is False  # type: ignore[attr-defined]
    assert "not stable" in decision.reason  # type: ignore[attr-defined]


def test_replayed_turn_sequence_cannot_authorize() -> None:
    receipts = [_receipt(0), _receipt(1, sequence=1, receipt_id="fresh-id"), _receipt(2)]
    decision = _authorized(receipts)
    assert decision.authorized is False  # type: ignore[attr-defined]
    assert "replayed" in decision.reason  # type: ignore[attr-defined]


def test_duplicate_receipt_ids_cannot_authorize() -> None:
    receipts = [_receipt(0), _receipt(1), _receipt(2, receipt_id="receipt-1")]
    decision = _authorized(receipts)
    assert decision.authorized is False  # type: ignore[attr-defined]


def test_out_of_order_observations_are_not_consecutive_turns() -> None:
    receipts = [_receipt(0, sequence=3), _receipt(1, sequence=1), _receipt(2, sequence=2)]
    decision = _authorized(receipts)
    assert decision.authorized is False  # type: ignore[attr-defined]
    assert "consecutive" in decision.reason  # type: ignore[attr-defined]


def test_sorted_but_gapped_sequences_are_not_consecutive_turns() -> None:
    receipts = [_receipt(0, sequence=1), _receipt(1, sequence=5), _receipt(2, sequence=10)]
    decision = _authorized(receipts)
    assert decision.authorized is False  # type: ignore[attr-defined]
    assert "consecutive" in decision.reason  # type: ignore[attr-defined]


def test_future_dated_observation_is_rejected() -> None:
    receipts = [
        _receipt(0),
        _receipt(1),
        _receipt(2, observed_at="2026-08-24T23:00:00+00:00"),
    ]
    decision = _authorized(receipts)
    assert decision.authorized is False  # type: ignore[attr-defined]
    assert "future" in decision.reason  # type: ignore[attr-defined]


def test_malformed_timestamp_fails_closed() -> None:
    malformed = {
        "receipt_id": "receipt-bad",
        "condition_id": "ext-cond-1",
        "observer_id": "ctrl-1",
        "turn_sequence": 1,
        "observed_at": "not-a-timestamp",
        "route_expires_at": ROUTE_VALID_UNTIL,
    }
    with pytest.raises(GoalBlockerError):
        BlockerTurnReceipt.from_dict(malformed)


def test_goal_blocked_marker_stays_disjoint_from_lane_operational_states() -> None:
    assert GOAL_BLOCKED_MARKER not in GOAL_STATUSES
    assert GOAL_BLOCKED_MARKER == "goal-blocked"


def test_authorization_never_writes_goal_state(tmp_path: Path) -> None:
    _authorized([_receipt(0), _receipt(1), _receipt(2)])
    assert not (tmp_path / "goals.json").exists()
    record_goal_blocker_receipts(tmp_path, "node-a:lane-a-1", (_receipt(0),))
    assert (tmp_path / "goal_blockers.json").exists()
    assert not (tmp_path / "goals.json").exists()


def test_blocker_evidence_survives_reload_and_still_authorizes(tmp_path: Path) -> None:
    session_ref = "node-a:lane-a-1"
    record_goal_blocker_receipts(tmp_path, session_ref, (_receipt(0), _receipt(1)))
    record_goal_blocker_receipts(tmp_path, session_ref, (_receipt(2),))

    reloaded = load_goal_blocker_receipts(tmp_path, session_ref)
    assert len(reloaded) == 3
    decision = authorize_goal_block_transition(
        receipts=reloaded,
        assertor_id="agent-7",
        no_work_remaining_proof=PROOF,
        now=DECISION_CLOCK,
    )
    assert decision.authorized is True

    document = goal_blockers_path(tmp_path).read_text(encoding="utf-8")
    assert GOAL_BLOCKER_SCHEMA in document

    fresh_root_replay = load_goal_blocker_receipts(tmp_path, "node-a:lane-b-2")
    assert fresh_root_replay == ()


def test_store_refuses_replayed_evidence_without_erasing_history(tmp_path: Path) -> None:
    session_ref = "node-a:lane-a-1"
    record_goal_blocker_receipts(tmp_path, session_ref, (_receipt(0), _receipt(1), _receipt(2)))
    replay = (
        BlockerTurnReceipt(
            receipt_id="receipt-replay",
            condition_id="ext-cond-1",
            observer_id="ctrl-1",
            turn_sequence=1,
            observed_at="2026-08-24T10:00:00+00:00",
            route_expires_at=ROUTE_VALID_UNTIL,
        ),
    )
    with pytest.raises(GoalBlockerError, match="[Ss]tale or replayed"):
        record_goal_blocker_receipts(tmp_path, session_ref, replay)
    assert len(load_goal_blocker_receipts(tmp_path, session_ref)) == 3


def test_store_refuses_duplicate_receipt_ids_across_batches(tmp_path: Path) -> None:
    session_ref = "node-a:lane-a-1"
    record_goal_blocker_receipts(tmp_path, session_ref, (_receipt(0),))
    duplicate_id = _receipt(5, sequence=5, receipt_id="receipt-0")
    with pytest.raises(GoalBlockerError, match="[Dd]uplicate blocker receipt"):
        record_goal_blocker_receipts(tmp_path, session_ref, (duplicate_id,))
    stored = load_goal_blocker_receipts(tmp_path, session_ref)
    assert [receipt.receipt_id for receipt in stored] == ["receipt-0"]

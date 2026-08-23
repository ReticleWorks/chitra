"""Focused tests for the versioned shared lane contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitra.session_contract import (
    CloseResult,
    ContractValidationError,
    JoinedLaneRecord,
    LaneUpdate,
    OperatingFact,
    PendingProviderOperation,
    Problem,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    RecoveryState,
    RoadmapStep,
    UsageReport,
    calculate_progress,
    is_valid_update,
    validate_update,
)

FIXTURES = Path(__file__).parent / "fixtures" / "session_contract"


def _update(*, sequence: int = 1, plan_version: int = 1, revision_note: str = "") -> LaneUpdate:
    return LaneUpdate(
        lane_id="lane-a",
        goal_id="goal-123",
        session_ref="tophand:lane-a-1",
        goal_version=2,
        sequence=sequence,
        observed_at="2026-08-23T14:00:00+00:00",
        plan_version=plan_version,
        revision_note=revision_note,
        steps=(
            RoadmapStep(id="design", status="done"),
            RoadmapStep(id="implement", status="active"),
        ),
        current_action="Implement the current step",
        next_action="Continue implementation",
    )


def test_update_fixture_round_trips_and_rejects_wrong_version_or_extra() -> None:
    payload = json.loads((FIXTURES / "update-v1.json").read_text(encoding="utf-8"))
    update = LaneUpdate.from_dict(payload)
    assert update.to_dict() == payload
    assert LaneUpdate.from_dict(update.to_dict()) == update
    with pytest.raises(ValueError, match="session-update.v1"):
        LaneUpdate.from_dict({**payload, "schema": "chitra.session-update.v2"})
    with pytest.raises(ValueError):
        LaneUpdate.from_dict({**payload, "unexpected": True})


def test_update_rejects_duplicate_or_multiple_active_steps() -> None:
    with pytest.raises(ValueError, match="active"):
        LaneUpdate(
            lane_id="lane-a",
            goal_id="goal-123",
            session_ref="tophand:lane-a-1",
            goal_version=1,
            sequence=1,
            observed_at="2026-08-23T14:00:00+00:00",
            plan_version=1,
            steps=(RoadmapStep(id="a", status="active"), RoadmapStep(id="b", status="active")),
            current_action="Choose the active step",
            next_action="wait",
        )
    with pytest.raises(ValueError, match="unique"):
        LaneUpdate(
            lane_id="lane-a",
            goal_id="goal-123",
            session_ref="tophand:lane-a-1",
            goal_version=1,
            sequence=1,
            observed_at="2026-08-23T14:00:00+00:00",
            plan_version=1,
            steps=(RoadmapStep(id="a", status="pending"), RoadmapStep(id="a", status="done")),
            next_action="wait",
        )


def test_update_sequence_and_plan_revision_rules() -> None:
    first = _update(sequence=5)
    same_plan = _update(sequence=6)
    assert is_valid_update(first, same_plan)
    with pytest.raises(ContractValidationError, match="sequence"):
        validate_update(first, _update(sequence=5))
    with pytest.raises(ContractValidationError, match="step IDs"):
        validate_update(first, same_plan.model_copy(update={"steps": (RoadmapStep(id="new", status="active"),)}))
    with pytest.raises(ContractValidationError, match="revision"):
        validate_update(first, _update(sequence=6).model_copy(update={"plan_version": 2}))
    revised = _update(sequence=6, plan_version=2, revision_note="Add provider reconciliation")
    assert is_valid_update(first, revised)
    with pytest.raises(ContractValidationError, match="goal_id"):
        validate_update(first, same_plan.model_copy(update={"goal_id": "other-goal"}))


def test_goal_revision_and_problem_history_need_explicit_non_destructive_changes() -> None:
    first = _update(sequence=5).model_copy(
        update={
            "problems": (
                Problem(id="p1", summary="Provider is unavailable", owner="chitra", state="resolved", resolution="retry later"),
            )
        }
    )
    revised_goal = _update(sequence=6, plan_version=2, revision_note="Goal contract was revised").model_copy(
        update={"goal_version": 3, "problems": first.problems}
    )
    assert is_valid_update(first, revised_goal)
    rewritten = _update(sequence=6).model_copy(
        update={
            "problems": (
                Problem(id="p1", summary="Different summary", owner="chitra", state="resolved", resolution="retry later"),
            )
        }
    )
    with pytest.raises(ContractValidationError, match="immutable"):
        validate_update(first, rewritten)
    reopened = _update(sequence=6).model_copy(
        update={
            "problems": (
                Problem(
                    id="p1",
                    summary="Provider is unavailable",
                    owner="chitra",
                    state="open",
                    reopen_event="provider-observed-again",
                ),
            )
        }
    )
    with pytest.raises(ContractValidationError, match="reopen_event"):
        validate_update(first, reopened.model_copy(update={"problems": (reopened.problems[0].model_copy(update={"reopen_event": None}),)}))


def test_progress_is_unavailable_for_untrusted_plan_states() -> None:
    valid = calculate_progress(_update(), plan_state="valid")
    assert (valid.percentage, valid.completed_steps, valid.total_steps) == (50.0, 1, 2)
    for state in ("forming", "invalid", "missing", "stale", "conflicting"):
        result = calculate_progress(_update(), plan_state=state)
        assert result.percentage is None
        assert result.reason == f"plan-{state}"
    all_done = _update().model_copy(
        update={"steps": (RoadmapStep(id="design", status="done"), RoadmapStep(id="implement", status="done"))}
    )
    assert calculate_progress(all_done, plan_state="valid").percentage == 100.0
    dropped = _update().model_copy(
        update={"steps": (RoadmapStep(id="design", status="dropped"), RoadmapStep(id="implement", status="active"))}
    )
    assert calculate_progress(dropped, plan_state="valid").percentage == 0.0


def test_provider_capabilities_and_operation_result_keep_acceptance_separate_from_consumption() -> None:
    capabilities = ProviderCapabilities.from_supported(("create_or_resume", "send", "usage"))
    assert capabilities.send and not capabilities.close
    with pytest.raises(ValueError, match="resume_after_close"):
        ProviderCapabilities(resume_after_close=True)
    pending = PendingProviderOperation(
        operation_id="send-1",
        kind="send",
        lane_id="lane-a",
        provider_handle="tophand-lane-a",
        idempotency_key="idem-send-1",
        payload_digest="sha256-send",
        created_at="2026-08-23T14:00:00+00:00",
    )
    accepted = ProviderOperationResult(
        operation_id="send-1",
        kind="send",
        lane_id="lane-a",
        provider_handle="tophand-lane-a",
        idempotency_key="idem-send-1",
        payload_digest="sha256-send",
        status="accepted",
        accepted=True,
        consumed=False,
        observed_at="2026-08-23T14:00:01+00:00",
    )
    assert accepted.transport_accepted is True
    ProviderOperationResult.model_validate(accepted.to_dict(), strict=True)
    with pytest.raises(ValueError, match="consumed result"):
        ProviderOperationResult(
            operation_id="send-1",
            kind="send",
            lane_id="lane-a",
            provider_handle="tophand-lane-a",
            idempotency_key="idem-send-1",
            payload_digest="sha256-send",
            status="consumed",
            accepted=True,
            consumed=False,
            observed_at="2026-08-23T14:00:01+00:00",
        )
    assert pending.operation_id == accepted.operation_id


def test_operating_fact_requires_current_authority_and_freshness_for_action() -> None:
    now = datetime(2026, 8, 23, 14, tzinfo=UTC)
    fact = OperatingFact(
        name="provider-route",
        value="tophand",
        state="known",
        source="fixture",
        revision=1,
        observed_at="2026-08-23T13:59:00+00:00",
        freshness="fresh",
        fresh_until="2026-08-23T14:15:00+00:00",
        within_authority=True,
    )
    assert fact.is_current(now=now)
    assert not fact.model_copy(update={"state": "conflicting"}).is_current(now=now)
    assert not fact.model_copy(update={"within_authority": False}).is_current(now=now)
    assert not fact.model_copy(update={"fresh_until": (now - timedelta(seconds=1)).isoformat()}).is_current(now=now)


def test_usage_fixture_keeps_ceiling_unknown_and_incomplete_report_non_compliant() -> None:
    complete = UsageReport.from_dict(json.loads((FIXTURES / "usage-complete.json").read_text()))
    assert complete.ceiling is None
    assert complete.complies_with_ceiling() is None
    incomplete = UsageReport.from_dict(json.loads((FIXTURES / "usage-incomplete.json").read_text()))
    assert incomplete.complies_with_ceiling() is None
    assert incomplete.to_dict() == json.loads((FIXTURES / "usage-incomplete.json").read_text())
    with pytest.raises(ValueError, match="total"):
        UsageReport(
            parent=complete.parent,
            children=complete.children,
            child_roster=complete.child_roster,
            child_roster_complete=True,
            child_roster_evidence="parent-update-1",
            total={"name": "total", "amount": 1, "unit": "usd"},
            evidence_source=complete.evidence_source,
            observed_at=complete.observed_at,
            complete=True,
        )
    zero_children = UsageReport(
        parent={"name": "parent", "amount": 1, "unit": "usd"},
        children=(),
        child_roster=(),
        child_roster_complete=True,
        child_roster_evidence="parent-update-zero-children",
        total={"name": "total", "amount": 1, "unit": "usd"},
        evidence_source="provider",
        observed_at="2026-08-23T14:00:00+00:00",
        complete=True,
    )
    assert zero_children.complies_with_ceiling() is None


def test_close_archive_and_joined_record_preserve_identity_without_goal_copy() -> None:
    close = CloseResult.model_validate(json.loads((FIXTURES / "close-archived.json").read_text()), strict=True)
    assert close.archived and not close.closed and close.later_resume_supported
    update = LaneUpdate.from_dict(json.loads((FIXTURES / "update-v1.json").read_text()))
    joined = JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-123",
        goal_version=2,
        session_ref="tophand:lane-a-1",
        provider=ProviderIdentity(
            kind="tophand",
            handle="tophand-lane-a",
            capabilities=ProviderCapabilities.from_supported(("create_or_resume", "send", "read_updates")),
        ),
        current_update=update,
        recovery=RecoveryState(stage="waiting", failure_signature="provider-latency", attempt_count=1),
        next_check={"at": "2026-08-23T14:15:00+00:00", "reason": "Check for a material update"},
    )
    payload = joined.to_dict()
    assert payload["schema"] == "chitra.lanes.v1"
    assert "goal" not in payload
    assert JoinedLaneRecord.from_dict(payload) == joined
    with pytest.raises(ValueError, match="identity"):
        JoinedLaneRecord(
            lane_id="lane-a",
            goal_id="goal-123",
            goal_version=3,
            session_ref="tophand:lane-a-1",
            provider=joined.provider,
            current_update=update,
        )


def test_joined_record_requires_consumed_update_evidence_and_terminal_close_evidence() -> None:
    provider = ProviderIdentity(
        kind="tophand",
        handle="tophand-lane-a",
        capabilities=ProviderCapabilities.from_supported(("create_or_resume", "send", "read_updates")),
    )
    update = LaneUpdate.from_dict(json.loads((FIXTURES / "update-v1.json").read_text())).model_copy(
        update={"operation_id": "status-1", "idempotency_key": "status-idem-1"}
    )
    accepted = ProviderOperationResult(
        operation_id="status-1",
        kind="status",
        lane_id="lane-a",
        provider_handle="tophand-lane-a",
        idempotency_key="status-idem-1",
        payload_digest="sha256-status",
        status="accepted",
        accepted=True,
        consumed=False,
        observed_at="2026-08-23T14:00:01+00:00",
    )
    with pytest.raises(ValueError, match="consumed"):
        JoinedLaneRecord(
            lane_id="lane-a",
            goal_id="goal-123",
            goal_version=2,
            session_ref="tophand:lane-a-1",
            provider=provider,
            current_update=update,
            last_operation_result=accepted,
        )
    consumed = accepted.model_copy(update={"status": "consumed", "consumed": True})
    joined = JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-123",
        goal_version=2,
        session_ref="tophand:lane-a-1",
        provider=provider,
        current_update=update,
        last_operation_result=consumed,
    )
    assert joined.last_operation_result == consumed
    with pytest.raises(ValueError, match="matching close evidence"):
        JoinedLaneRecord(
            lane_id="lane-a",
            goal_id="goal-123",
            goal_version=2,
            session_ref="tophand:lane-a-1",
            lifecycle="archived",
            provider=provider,
            current_update=update.model_copy(update={"operation_id": None, "idempotency_key": None}),
        )

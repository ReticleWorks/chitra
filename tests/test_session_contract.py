"""Focused tests for the versioned shared lane contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitra.session_contract import (
    ChildRosterEntry,
    CloseResult,
    ContractValidationError,
    JoinedLaneRecord,
    LaneUpdate,
    OperatingFact,
    OperationReference,
    OwnerIdentity,
    OwnerProcessIdentity,
    PendingProviderOperation,
    Problem,
    ProblemHistoryEvent,
    ProviderCapabilities,
    ProviderIdentity,
    ProviderOperationResult,
    ReopenReceipt,
    RecoveryState,
    RoadmapMilestone,
    RoadmapStep,
    UsageReport,
    calculate_progress,
    is_valid_update,
    migrate_legacy_record,
    validate_active_owner_set,
    validate_operation_result,
    validate_pending_operation,
    validate_record_transition,
    validate_update,
    validate_usage_against_lane,
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


def test_same_plan_freezes_titles_owners_and_milestones_but_allows_status_progress() -> None:
    first = _update(sequence=5).model_copy(
        update={
            "steps": (
                RoadmapStep(id="design", status="done", title="Design", owner="lane", milestone_id="m1"),
                RoadmapStep(id="implement", status="active", title="Implement", owner="lane", milestone_id="m1"),
            ),
            "milestones": (RoadmapMilestone(id="m1", title="Build"),),
        }
    )
    blocked = first.model_copy(
        update={
            "sequence": 6,
            "steps": (
                RoadmapStep(id="design", status="done", title="Design", owner="lane", milestone_id="m1"),
                RoadmapStep(id="implement", status="blocked", title="Implement", owner="lane", milestone_id="m1"),
            ),
        }
    )
    assert is_valid_update(first, blocked)
    owner_changed = blocked.model_copy(
        update={
            "sequence": 7,
            "steps": (
                RoadmapStep(id="design", status="done", title="Design", owner="other", milestone_id="m1"),
                RoadmapStep(id="implement", status="blocked", title="Implement", owner="lane", milestone_id="m1"),
            ),
        }
    )
    with pytest.raises(ContractValidationError, match="without a plan revision"):
        validate_update(blocked, owner_changed)
    revised = owner_changed.model_copy(update={"plan_version": 2, "revision_note": "Move design ownership"})
    assert is_valid_update(blocked, revised)


def test_all_blocked_roadmap_is_valid_without_inventing_an_active_step() -> None:
    update = LaneUpdate(
        lane_id="lane-a",
        goal_id="goal-123",
        session_ref="tophand:lane-a-1",
        goal_version=1,
        sequence=1,
        observed_at="2026-08-23T14:00:00+00:00",
        plan_version=1,
        steps=(
            RoadmapStep(id="a", status="blocked", owner="lane"),
            RoadmapStep(id="b", status="blocked", owner="lane"),
        ),
        next_action="Wait for the durable recovery check",
    )
    assert update.current_action == ""


def test_goal_revision_and_problem_history_need_explicit_non_destructive_changes() -> None:
    first = _update(sequence=5).model_copy(
        update={
            "problems": (
                Problem(
                    id="p1",
                    summary="Provider is unavailable",
                    owner="chitra",
                    state="resolved",
                    history=(
                        ProblemHistoryEvent(
                            event_id="resolve-p1",
                            kind="resolved",
                            observed_at="2026-08-23T14:00:00+00:00",
                            note="retry later",
                        ),
                    ),
                ),
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
                Problem(
                    id="p1",
                    summary="Different summary",
                    owner="chitra",
                    state="resolved",
                    history=first.problems[0].history,
                ),
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
                    history=(
                        *first.problems[0].history,
                        ProblemHistoryEvent(
                            event_id="reopen-p1",
                            kind="reopened",
                            observed_at="2026-08-23T14:01:00+00:00",
                            note="provider-observed-again",
                        ),
                    ),
                ),
            )
        }
    )
    with pytest.raises(ContractValidationError, match="problem history"):
        broken_problem = reopened.problems[0].model_copy(update={"history": first.problems[0].history})
        validate_update(first, reopened.model_copy(update={"problems": (broken_problem,)}))


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


def test_operation_results_are_causally_after_pending_and_operation_keys_survive_gaps() -> None:
    pending = PendingProviderOperation(
        operation_id="send-1",
        kind="send",
        lane_id="lane-a",
        provider_handle="tophand-lane-a",
        idempotency_key="idem-send-1",
        payload_digest="sha256-send",
        created_at="2026-08-23T14:00:10+00:00",
    )
    result = ProviderOperationResult(
        operation_id="send-1",
        kind="send",
        lane_id="lane-a",
        provider_handle="tophand-lane-a",
        idempotency_key="idem-send-1",
        payload_digest="sha256-send",
        status="accepted",
        accepted=True,
        consumed=False,
        observed_at="2026-08-23T14:00:09+00:00",
    )
    with pytest.raises(ContractValidationError, match="predates"):
        validate_operation_result(pending, result)
    reference = OperationReference(
        operation_id="send-1",
        idempotency_key="idem-send-1",
        payload_digest="sha256-send",
        kind="send",
        created_at="2026-08-23T14:00:00+00:00",
    )
    first = _update(sequence=1).model_copy(update={"operation_history": (reference,)})
    gap = _update(sequence=3).model_copy(update={"operation_history": (reference,)})
    reused = gap.model_copy(update={"operation_id": "send-1", "idempotency_key": "idem-send-1"})
    with pytest.raises(ContractValidationError, match="reused"):
        validate_update(first, reused)


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
    assert not fact.model_copy(update={"observed_at": "2026-08-23T14:01:00+00:00"}).is_current(now=now)


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
        update={
            "operation_id": "status-1",
            "idempotency_key": "status-idem-1",
            "operation_history": (
                OperationReference(
                    operation_id="status-1",
                    idempotency_key="status-idem-1",
                    payload_digest="sha256-status",
                    kind="status",
                    created_at="2026-08-23T14:00:00+00:00",
                ),
            ),
        }
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
            operation_history=update.operation_history,
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
        operation_history=update.operation_history,
        last_operation_result=consumed,
    )
    assert joined.last_operation_result == consumed
    with pytest.raises(ValueError, match="logical close"):
        JoinedLaneRecord(
            lane_id="lane-a",
            goal_id="goal-123",
            goal_version=2,
            session_ref="tophand:lane-a-1",
            lifecycle="archived",
            provider=provider,
            current_update=update.model_copy(update={"operation_id": None, "idempotency_key": None}),
        )


def test_plan_structure_is_frozen_but_all_blocked_plan_is_valid() -> None:
    blocked = _update().model_copy(
        update={
            "steps": (
                RoadmapStep(id="design", status="blocked", title="Design", owner="lane"),
                RoadmapStep(id="implement", status="blocked", title="Implement", owner="lane"),
            ),
            "current_action": "",
        }
    )
    assert blocked.steps[0].status == "blocked"
    changed_status = blocked.model_copy(
        update={"sequence": 2, "steps": (blocked.steps[0].model_copy(update={"status": "done"}), blocked.steps[1])}
    )
    assert is_valid_update(blocked, changed_status)
    changed_title = changed_status.model_copy(
        update={"sequence": 3, "steps": (changed_status.steps[0], changed_status.steps[1].model_copy(update={"title": "New title"}) )}
    )
    with pytest.raises(ContractValidationError, match="structure"):
        validate_update(changed_status, changed_title)


def test_record_transition_fences_owner_revision_identity_and_active_owner_set() -> None:
    owner_a = OwnerIdentity(owner_id="chitra-a", instance_id="instance-a")
    owner_b = OwnerIdentity(owner_id="chitra-b", instance_id="instance-b")
    validate_active_owner_set((owner_a,))
    with pytest.raises(ContractValidationError, match="active owner"):
        validate_active_owner_set((owner_a, owner_b))
    previous = JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-123",
        goal_version=2,
        session_ref="tophand:lane-a-1",
        owner=owner_a,
        provider=ProviderIdentity(kind="tophand", handle="thread-a", capabilities=ProviderCapabilities()),
    )
    current = previous.model_copy(update={"revision": 2, "owner": owner_b, "chitra_ownership_epoch": 2})
    validate_record_transition(previous, current, active_owners=(owner_b,))
    with pytest.raises(ContractValidationError, match="revision"):
        validate_record_transition(previous, current.model_copy(update={"revision": 1}))
    with pytest.raises(ContractValidationError, match="immutable"):
        validate_record_transition(previous, current.model_copy(update={"revision": 3, "lane_id": "other"}))


def test_record_transition_requires_explicit_transfer_for_session_and_provider_swap() -> None:
    provider_a = ProviderIdentity(
        kind="tophand",
        handle="thread-a",
        instance_id="instance-a",
        generation=1,
        capabilities=ProviderCapabilities.from_supported(("create_or_resume",)),
    )
    provider_b = provider_a.model_copy(update={"handle": "thread-b", "instance_id": "instance-b", "generation": 2})
    previous = JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-123",
        goal_version=2,
        session_ref="tophand:lane-a-1",
        physical_session_generation=1,
        provider=provider_a,
    )
    changed = previous.model_copy(
        update={"revision": 2, "session_ref": "tophand:lane-a-2", "provider": provider_b}
    )
    with pytest.raises(ContractValidationError, match="session_ref"):
        validate_record_transition(previous, previous.model_copy(update={"revision": 2, "session_ref": "tophand:lane-a-2"}))
    with pytest.raises(ContractValidationError, match="explicit provider-transfer"):
        validate_record_transition(previous, changed)
    with pytest.raises(ContractValidationError, match="ownership epoch"):
        validate_record_transition(previous, changed, transition="provider-transfer")
    transferred = changed.model_copy(update={"chitra_ownership_epoch": 2, "physical_session_generation": 2})
    validate_record_transition(previous, transferred, transition="provider-transfer")
    with pytest.raises(ContractValidationError, match="physical session generation"):
        validate_record_transition(
            previous,
            transferred.model_copy(update={"physical_session_generation": 1}),
            transition="provider-transfer",
        )


def test_record_transition_rejects_goal_rollback_and_destructive_active_clears() -> None:
    problem = Problem(
        id="p1",
        summary="Provider is unavailable",
        owner="chitra",
        state="open",
    )
    previous_update = _update().model_copy(update={"problems": (problem,)})
    previous = JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-123",
        goal_version=2,
        session_ref="tophand:lane-a-1",
        provider=ProviderIdentity(kind="tophand", handle="thread-a", capabilities=ProviderCapabilities()),
        current_update=previous_update,
    )
    validate_record_transition(previous, previous.model_copy(update={"revision": 2}))
    with pytest.raises(ContractValidationError, match="goal_version"):
        validate_record_transition(previous, previous.model_copy(update={"revision": 2, "goal_version": 1, "current_update": None}))
    with pytest.raises(ContractValidationError, match="current_update"):
        validate_record_transition(previous, previous.model_copy(update={"revision": 2, "current_update": None}))
    cleared_problems = previous_update.model_copy(update={"sequence": 2, "problems": ()})
    with pytest.raises(ContractValidationError, match="problem"):
        validate_record_transition(
            previous,
            previous.model_copy(update={"revision": 2, "current_update": cleared_problems}),
        )
    with pytest.raises(ValueError, match="active owner"):
        JoinedLaneRecord(
            lane_id="lane-a",
            goal_id="goal-123",
            goal_version=2,
            session_ref="tophand:lane-a-1",
            owner=OwnerIdentity(owner_id="chitra", active=False),
            provider=previous.provider,
        )


def test_usage_must_match_update_roster_and_parent_ancestry() -> None:
    child = ChildRosterEntry(
        child_id="child-a",
        parent_id="lane-a",
        ancestry=("lane-a", "child-a"),
        retained_state="retained",
        material_result=False,
    )
    update = _update().model_copy(update={"child_roster": (child,)})
    report = UsageReport(
        parent={"name": "lane-a", "amount": 1, "unit": "usd"},
        children=({"name": "child-a", "amount": 0, "unit": "usd"},),
        child_roster=(child,),
        child_roster_complete=True,
        child_roster_evidence="update-1",
        total={"name": "total", "amount": 1, "unit": "usd"},
        evidence_source="provider",
        observed_at="2026-08-23T14:00:00+00:00",
        complete=True,
    )
    validate_usage_against_lane(update, report)
    with pytest.raises(ContractValidationError, match="child roster"):
        validate_usage_against_lane(update, report.model_copy(update={"child_roster": ()}))


def test_inline_usage_binds_material_result_to_cursor_and_exact_aggregate() -> None:
    cursor = "amp:T-11111111-1111-4111-8111-111111111111:offset:3:boundary:M-2:prefix:" + "a" * 64
    child = ChildRosterEntry(
        child_id="inline-child-1",
        parent_id="lane-a",
        ancestry=("lane-a", "inline-child-1"),
        retained_state="retained",
        material_result=True,
        material_result_ref="sha256:" + "b" * 64,
        transcript_cursor=cursor,
    )
    report = UsageReport(
        parent={"name": "lane-a", "amount": 9, "unit": "tokens"},
        children=(),
        child_roster=(child,),
        child_roster_complete=True,
        child_roster_evidence="T-11111111-1111-4111-8111-111111111111",
        total={"name": "total", "amount": 9, "unit": "tokens"},
        evidence_source="amp-thread-usage",
        observed_at="2026-08-23T14:00:00+00:00",
        complete=True,
        child_evidence_mode="inline",
        usage_evidence_hash="sha256:" + "c" * 64,
    )
    assert report.to_dict()["child_evidence_mode"] == "inline"
    assert report.to_dict()["child_roster"][0]["transcript_cursor"] == cursor
    validate_usage_against_lane(_update(), report)

    with pytest.raises(ValueError, match="aggregate usage evidence hash"):
        invalid = report.model_dump(mode="python")
        invalid["usage_evidence_hash"] = None
        UsageReport.model_validate(
            invalid,
            strict=True,
        )


def test_orb_child_identity_and_reviewed_version_round_trip() -> None:
    child = ChildRosterEntry(
        child_id="inline-child-2",
        parent_id="lane-a",
        ancestry=("lane-a", "inline-child-2"),
        retained_state="retained",
        material_result=True,
        material_result_ref="sha256:" + "d" * 64,
        transcript_cursor="amp:T-2:offset:2:boundary:M-2:prefix:" + "e" * 64,
        provider_handle="T-2",
        provider_session_id="T-2",
        provider_instance_id="amp-instance-2",
        provider_generation=3,
    )
    report = UsageReport(
        parent={"name": "lane-a", "amount": 2, "unit": "tokens"},
        children=(),
        child_roster=(child,),
        child_roster_complete=True,
        child_roster_evidence="T-2",
        total={"name": "total", "amount": 2, "unit": "tokens"},
        evidence_source="amp-thread-usage",
        observed_at="2026-08-23T14:00:00+00:00",
        complete=True,
        child_evidence_mode="inline",
        usage_evidence_hash="sha256:" + "f" * 64,
        amp_version="0.0.reviewed",
    )
    serialized = report.to_dict()
    assert serialized["amp_version"] == "0.0.reviewed"
    assert serialized["child_roster"][0]["provider_instance_id"] == "amp-instance-2"
    assert UsageReport.from_dict(serialized) == report


def test_provider_fences_unknown_identity_and_capability_bound_operations() -> None:
    unknown = ProviderIdentity(kind="tophand", handle="thread-a", capabilities=ProviderCapabilities())
    assert unknown.instance_id is None and unknown.generation is None
    operation = PendingProviderOperation(
        operation_id="op-1",
        kind="send",
        lane_id="lane-a",
        provider_handle="thread-a",
        idempotency_key="idem-1",
        payload_digest="digest-1",
        created_at="2026-08-23T14:00:00+00:00",
    )
    with pytest.raises(ContractValidationError, match="does not support"):
        validate_pending_operation(unknown, operation)
    close = operation.model_copy(update={"kind": "close"})
    provider = ProviderIdentity(
        kind="tophand",
        handle="thread-a",
        instance_id="instance-a",
        generation=1,
        capabilities=ProviderCapabilities.from_supported(("close",)),
    )
    with pytest.raises(ContractValidationError, match="checkpoint"):
        validate_pending_operation(provider, close)


def test_historical_operation_evidence_survives_capability_changes_but_new_pending_does_not() -> None:
    provider_with_send = ProviderIdentity(
        kind="tophand",
        handle="thread-a",
        instance_id="instance-a",
        generation=1,
        capabilities=ProviderCapabilities.from_supported(("send",)),
    )
    reference = OperationReference(
        operation_id="send-1",
        idempotency_key="idem-send-1",
        payload_digest="digest-send-1",
        kind="send",
        created_at="2026-08-23T14:00:00+00:00",
    )
    result = ProviderOperationResult(
        operation_id="send-1",
        kind="send",
        lane_id="lane-a",
        provider_handle="thread-a",
        provider_instance_id="instance-a",
        provider_generation=1,
        idempotency_key="idem-send-1",
        payload_digest="digest-send-1",
        status="consumed",
        accepted=True,
        consumed=True,
        observed_at="2026-08-23T14:00:01+00:00",
    )
    previous = JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-123",
        goal_version=2,
        session_ref="tophand:lane-a-1",
        provider=provider_with_send,
        operation_history=(reference,),
        last_operation_result=result,
    )
    provider_without_send = provider_with_send.model_copy(
        update={"capabilities": ProviderCapabilities.from_supported(())}
    )
    current = previous.model_copy(update={"revision": 2, "provider": provider_without_send})
    validate_record_transition(previous, current)
    pending = PendingProviderOperation(
        operation_id="send-2",
        kind="send",
        lane_id="lane-a",
        provider_handle="thread-a",
        provider_instance_id="instance-a",
        provider_generation=1,
        idempotency_key="idem-send-2",
        payload_digest="digest-send-2",
        created_at="2026-08-23T14:01:00+00:00",
    )
    with pytest.raises((ContractValidationError, ValueError), match="does not support"):
        JoinedLaneRecord(
            lane_id="lane-a",
            goal_id="goal-123",
            goal_version=2,
            session_ref="tophand:lane-a-1",
            provider=provider_without_send,
            pending_operation=pending,
            operation_history=(reference, OperationReference(
                operation_id="send-2",
                idempotency_key="idem-send-2",
                payload_digest="digest-send-2",
                kind="send",
                created_at="2026-08-23T14:01:00+00:00",
            )),
        )


def test_close_evidence_preserves_provider_state_and_same_thread_resume() -> None:
    close = CloseResult.model_validate(json.loads((FIXTURES / "close-amp-later-resume.json").read_text()), strict=True)
    old_owner = OwnerProcessIdentity(
        pid=101,
        uid=1000,
        gid=1000,
        start_token="old-process",
        comm="amp",
        exe="/usr/local/bin/amp",
    )
    new_owner = old_owner.model_copy(update={"pid": 202, "start_token": "new-process"})
    close = close.model_copy(update={"owner_process": old_owner})
    operation_history = (
        OperationReference(
            operation_id=close.operation_id,
            idempotency_key=close.idempotency_key,
            payload_digest=close.payload_digest,
            kind="close",
            created_at="2026-08-23T14:00:00+00:00",
        ),
    )
    provider = ProviderIdentity(
        kind="amp",
        handle="amp-thread-a",
        instance_id="amp-instance-1",
        generation=1,
        process_start_token=old_owner.start_token,
        observed_process={
            **old_owner.model_dump(mode="json"),
            "process_start_token": old_owner.start_token,
        },
        capabilities=ProviderCapabilities.from_supported(("close", "checkpoint", "create_or_resume", "resume_after_close")),
    )
    inactive = JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-123",
        goal_version=2,
        session_ref="amp:lane-a-1",
        lifecycle="inactive",
        provider=provider,
        operation_history=operation_history,
        last_close_result=close,
        checkpoint_reference=close.checkpoint_ref,
    )
    reopen = ReopenReceipt(
        operation_id="resume-amp-1",
        close_operation_id=close.operation_id,
        lane_id=inactive.lane_id,
        goal_id=inactive.goal_id,
        goal_version=inactive.goal_version,
        session_ref=inactive.session_ref,
        provider_session_id="amp-session-a",
        provider_handle=provider.handle,
        provider_instance_id="amp-instance-1",
        provider_generation=1,
        checkpoint_ref=close.checkpoint_ref,
        prior_owner_process=old_owner,
        owner_process=new_owner,
        created_new_lane=False,
        created_new_session=False,
        observed_at="2026-08-23T14:01:00+00:00",
        evidence="same provider thread reopened under a fresh process",
    )
    resume_result = ProviderOperationResult(
        operation_id=reopen.operation_id,
        kind="create_or_resume",
        lane_id=inactive.lane_id,
        provider_handle=provider.handle,
        provider_session_id=reopen.provider_session_id,
        process_start_token=new_owner.start_token,
        idempotency_key="resume-amp-idem-1",
        payload_digest="sha256-resume-amp",
        provider_instance_id="amp-instance-1",
        provider_generation=1,
        status="consumed",
        accepted=True,
        consumed=True,
        observed_at=reopen.observed_at,
        evidence=reopen.evidence,
        reopen_receipt=reopen,
    )
    resumed_provider = provider.model_copy(
        update={
            "process_start_token": new_owner.start_token,
            "observed_process": {
                **new_owner.model_dump(mode="json"),
                "process_start_token": new_owner.start_token,
            },
        }
    )
    resumed = inactive.model_copy(
        update={
            "revision": 2,
            "lifecycle": "active",
            "provider": resumed_provider,
            "operation_history": (
                *operation_history,
                OperationReference(
                    operation_id=resume_result.operation_id,
                    idempotency_key=resume_result.idempotency_key,
                    payload_digest=resume_result.payload_digest,
                    kind=resume_result.kind,
                    created_at=reopen.observed_at,
                ),
            ),
            "last_operation_result": resume_result,
            "last_close_result": None,
        }
    )
    validate_record_transition(inactive, resumed, transition="resume")
    with pytest.raises(ContractValidationError, match="provider"):
        validate_record_transition(
            inactive,
            resumed.model_copy(update={"provider": provider.model_copy(update={"handle": "other"})}),
            transition="resume",
        )
    with pytest.raises(ContractValidationError, match="resume_after_close"):
        validate_record_transition(
            inactive,
            resumed.model_copy(
                update={
                    "provider": provider.model_copy(
                        update={
                            "capabilities": ProviderCapabilities.from_supported(
                                ("close", "checkpoint", "create_or_resume")
                            )
                        }
                    )
                },
            ),
            transition="resume",
        )
    native = CloseResult.model_validate(json.loads((FIXTURES / "close-native.json").read_text()), strict=True)
    assert native.closed and not native.archived
    amp_native = native.model_copy(
        update={
            "provider_handle": "amp-thread-a",
            "provider_instance_id": "amp-instance-1",
            "provider_thread_ref": "amp-thread-a",
        }
    )
    with pytest.raises(ValueError, match="archived"):
        JoinedLaneRecord(
            lane_id="lane-a",
            goal_id="goal-123",
            goal_version=2,
            session_ref="amp:lane-a-1",
            lifecycle="inactive",
            provider=provider,
            operation_history=(
                OperationReference(
                    operation_id=native.operation_id,
                    idempotency_key=native.idempotency_key,
                    payload_digest=native.payload_digest,
                    kind="close",
                    created_at="2026-08-23T14:00:00+00:00",
                ),
            ),
            last_close_result=amp_native,
            checkpoint_reference=amp_native.checkpoint_ref,
        )


def test_later_resume_evidence_requires_the_current_resume_capability() -> None:
    close = CloseResult.model_validate(json.loads((FIXTURES / "close-amp-later-resume.json").read_text()), strict=True)
    with pytest.raises(ValueError, match="resume_after_close"):
        JoinedLaneRecord(
            lane_id="lane-a",
            goal_id="goal-123",
            goal_version=2,
            session_ref="amp:lane-a-1",
            lifecycle="inactive",
            provider=ProviderIdentity(
                kind="amp",
                handle="amp-thread-a",
                instance_id="amp-instance-1",
                generation=1,
                capabilities=ProviderCapabilities.from_supported(("close", "checkpoint", "create_or_resume")),
            ),
            operation_history=(
                OperationReference(
                    operation_id=close.operation_id,
                    idempotency_key=close.idempotency_key,
                    payload_digest=close.payload_digest,
                    kind="close",
                    created_at="2026-08-23T14:00:00+00:00",
                ),
            ),
            last_close_result=close,
            checkpoint_reference=close.checkpoint_ref,
        )


def test_migration_rejects_legacy_shape_and_wake_intervention_have_one_home() -> None:
    with pytest.raises(ContractValidationError, match="legacy"):
        migrate_legacy_record({"lane_id": "lane-a"})
    assert "wake_condition" not in JoinedLaneRecord.model_fields
    assert "last_intervention" not in RecoveryState.model_fields
    assert "intervention_consumed" not in RecoveryState.model_fields
    assert "useful_work_resumed" not in RecoveryState.model_fields


def test_failure_and_provider_parity_fixtures_are_version_neutral() -> None:
    for name in ("lost-create.json", "sent-unobserved.json"):
        result = ProviderOperationResult.model_validate(json.loads((FIXTURES / name).read_text()), strict=True)
        assert result.operation_id and result.provider_instance_id and result.provider_generation == 1
    fallback = json.loads((FIXTURES / "corrupt-fallback.json").read_text())
    assert fallback["expected_fallback_source"] == "previous"
    parity = json.loads((FIXTURES / "provider-parity.json").read_text())
    assert {entry["kind"] for entry in parity["providers"]} == {"tophand", "amp"}
    assert tuple(parity["operations"]) == (
        "create_or_resume",
        "status",
        "send",
        "read_updates",
        "checkpoint",
        "usage",
        "cancel_current_turn",
        "close",
    )

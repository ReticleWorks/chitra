"""Fixtures for production-path joined-lane reporting tests."""

from __future__ import annotations

from chitra.session_contract import (
    JoinedLaneRecord,
    LaneUpdate,
    NextCheck,
    PlanAssessment,
    Problem,
    ProblemHistoryEvent,
    ProviderCapabilities,
    ProviderIdentity,
    RecoveryState,
    RoadmapStep,
)


def joined_report_record(
    *,
    lane_id: str = "ramble-build",
    goal_id: str = "goal-ramble",
    session_ref: str = "roundtop:ramble-build",
) -> JoinedLaneRecord:
    return JoinedLaneRecord(
        lane_id=lane_id,
        goal_id=goal_id,
        goal_version=2,
        session_ref=session_ref,
        provider=ProviderIdentity(
            kind="tophand",
            handle="tophand-ramble",
            instance_id="instance-1",
            generation=1,
            capabilities=ProviderCapabilities.from_supported(("create_or_resume", "send", "read_updates", "checkpoint")),
        ),
        current_update=LaneUpdate(
            lane_id=lane_id,
            goal_id=goal_id,
            session_ref=session_ref,
            goal_version=2,
            sequence=1,
            observed_at="2026-08-23T14:00:00+00:00",
            plan_version=2,
            revision_note="Split proof from implementation",
            steps=(
                RoadmapStep(id="design", status="done", title="Design the report", owner="lane"),
                RoadmapStep(id="proof", status="active", title="Run the proof", owner="lane"),
            ),
            current_action="Running the proof",
            next_action="Publish the proof result",
            problems=(
                Problem(
                    id="provider-wait",
                    summary="Provider is waiting",
                    owner="chitra",
                    state="open",
                    need="Refresh the provider",
                ),
                Problem(
                    id="old-report",
                    summary="Old report was stale",
                    owner="lane",
                    state="resolved",
                    history=(
                        ProblemHistoryEvent(
                            event_id="old-report-resolved",
                            kind="resolved",
                            observed_at="2026-08-23T13:59:00+00:00",
                            note="Published a fresh report",
                        ),
                    ),
                ),
            ),
        ),
        plan_assessment=PlanAssessment(state="valid", reason="joined update passed validation"),
        recovery=RecoveryState(
            stage="relaunch",
            cycle_id="cycle-ramble",
            attempted_remedy="checkpoint",
            execution_objective="Complete the proof and publish its result",
            execution_plan=("Run the proof", "Publish the proof result"),
            handoff_id="ramble-build-cycle-ramble-context",
            handoff_reference="recovery-handoffs/ramble-build-cycle-ramble-context.json",
            handoff_digest="handoff-digest",
        ),
        checkpoint_reference="checkpoint-ramble",
        next_check=NextCheck(
            at="2026-08-23T14:15:00+00:00",
            reason="Check for the proof result",
            wake_condition="A newer report is observed",
        ),
    )

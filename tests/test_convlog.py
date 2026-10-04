"""Kept unit guard: ``route_operator_brief(authority_evidence_complete=False)``
is an in-process flag -- every caller in ``src/`` (the convlog CLI write path)
leaves it at the default ``True``, so the durable-foreground-task deny path it
gates is unreachable through the real boundary.
"""

from __future__ import annotations

from pathlib import Path

from _goal_fixtures import enrollment_fields

from chitra.autonomy import AutonomyPolicy, CapabilityGrant
from chitra.convlog import (
    OperatorBrief,
    read_entries,
    route_operator_brief,
    validate_brief,
)
from chitra.evidence import EvidenceHandle
from chitra.goals import GoalRecord, get_goal, upsert_goal


class _AcceptingResolver:
    """Stub resolver for tests that exercise structure, not evidence lookup."""

    def resolve(self, handle: EvidenceHandle) -> str | None:
        return None


def _attempt(action: str, result: str, evidence: object) -> dict[str, object]:
    return {"action": action, "result": result, "evidence": evidence}


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_ref": "host-b:feeds:0.0",
        "program": "Feeds digest redesign (F2)",
        "subject": "Feeds digest compiler",
        "progress": "implementation-ready; final interface choice pending",
        "stage": "The implementation is ready for the final interface choice.",
        "category": "decision",
        "decision": "Should the digest ship as one combined feed?",
        "recommendation": "Ship one combined feed because the tested readers preferred it.",
        "recommendation_basis": "research",
        "options": [
            {"label": "Combined feed", "consequence": "Readers get one ranked digest."},
            {"label": "Separate feeds", "consequence": "Readers choose a source first."},
        ],
        "exhaustion": {
            "reason": "attempts_exhausted",
            "attempts": [
                {
                    "action": "Shipped the combined-feed prototype to the reader test",
                    "result": "it passed the preference check.",
                    "evidence": {"kind": "command", "ref": "chitra-tmux-capture feeds-reader --pane harness-pi", "exit_status": 0},
                },
                {
                    "action": "Asked the work session to pick a default feed layout",
                    "result": "it deferred the product call.",
                    "evidence": {"kind": "order", "ref": "ord-feeds-layout-1"},
                },
            ],
            "residual_blocker": "Only the operator can pick between one combined feed and separate feeds.",
        },
        "source_quote": ["The combined prototype passed the reader test.", "I need the operator's product decision."],
        "source_ref": "transcripts/feeds.jsonl",
    }
    payload.update(changes)
    return payload


def _brief(**changes: object) -> OperatorBrief:
    return validate_brief(_payload(**changes), evidence_resolver=_AcceptingResolver())


def _policy_goal(tmp_path: Path, *, policy: AutonomyPolicy | None = None) -> GoalRecord:
    goal = GoalRecord(
        session_ref="host-b:feeds:0.0",
        goal="Deliver the feed redesign with proof",
        done_when="The focused feed tests pass and the redesign artifact exists",
        source="operator:test",
        status="working",
        intent="Complete the feed redesign",
        scope="feed source and focused tests",
        autonomy_policy=policy or AutonomyPolicy(),
        **enrollment_fields("The focused feed tests pass and the redesign artifact exists"),
    )
    return upsert_goal(tmp_path, goal)


def test_incomplete_legacy_authority_evidence_becomes_a_durable_foreground_task(tmp_path: Path) -> None:
    _policy_goal(
        tmp_path,
        policy=AutonomyPolicy(
            grants=(CapabilityGrant(grant_id="feed-credential", capability="credential_use"),),
        ),
    )
    path = tmp_path / "conversation.jsonl"
    brief = _brief(
        exhaustion={
            "reason": "credential",
            "attempts": [_attempt(
                "Presented the retry token to the registry",
                "the registry refused it outright.",
                {"kind": "verb_refusal", "ref": "chitra-registry-push"},
            )],
            "residual_blocker": "The authority evidence is incomplete for this route.",
        }
    )

    route = route_operator_brief(
        brief,
        goal_root=tmp_path,
        evidence_resolver=_AcceptingResolver(),
        authority_evidence_complete=False,
    )

    assert route.disposition == "foreground_residual"
    assert route.foreground_recorded is True
    assert read_entries(path) == []
    stored = get_goal(tmp_path, "host-b:feeds:0.0")
    assert stored is not None
    assert len(stored.foreground_tasks) == 1
    assert stored.foreground_tasks[0].kind == "investigate"
    assert stored.foreground_tasks[0].source == "convlog"

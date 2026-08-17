"""Tests for the two-stage adjudication of reported obstacles."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import pytest

from chitra.adjudication import (
    Adjudication,
    AdjudicationContext,
    AdjudicatorProcessError,
    AdjudicatorReply,
    BlockerClaim,
    ClaudeProcessAdjudicator,
    Evidence,
    EvidenceSources,
    adjudicate,
    adjudicate_deterministic,
    classify_claim,
    decision_entry,
    escalation_brief,
    grants_merging,
    read_transcript_tail,
    resolve_approval,
    resolve_capability_denial,
    resolve_merge_rights,
    resolve_usage,
    split_claim_text,
)
from chitra.capabilities import CapabilityManifest
from chitra.convlog import ConversationEntry
from chitra.decisions import DecisionEntry
from chitra.goals import GoalRecord
from chitra.usage_export import FleetExportVerdict, FleetVerdict

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _claim(
    text: str,
    *,
    session_ref: str = "tophand:widget-build:0.0",
    origin: Literal["open_ask", "needs"] = "open_ask",
) -> BlockerClaim:
    return BlockerClaim(session_ref=session_ref, text=text, origin=origin, observed_at=NOW.isoformat())


def _goal(session_ref: str = "tophand:widget-build:0.0") -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Ship the widget build service with its live acceptance evidence.",
        done_when="The widget build service runs green in continuous integration.",
        source="task-file:/tmp/widget.md",
        status="working",
        intent="The operator asked for a widget build service that other teams can call directly.",
        scope="In: the build service. Out: the reporting front end.",
    )


def _manifest(*, grants: Sequence[str], excludes: Sequence[str]) -> CapabilityManifest:
    return CapabilityManifest.model_validate(
        {
            "schema": "chitra.capabilities.v1",
            "capabilities": [
                {
                    "name": "auto-merge",
                    "kind": "daemon",
                    "purpose": "Land green pull requests without a person in the path.",
                    "when_to_use": "Run as the supervised merge daemon.",
                    "authority": {"level": "act", "grants": list(grants), "excludes": list(excludes)},
                    "default_enabled": True,
                    "commands": [
                        {
                            "name": "chitra-merged",
                            "description": "Start the merge daemon.",
                            "argv": ["chitra-merged"],
                            "params": [],
                            "mutates": True,
                        }
                    ],
                }
            ],
        }
    )


def _usage(host: str, backend: str, verdict: str) -> FleetExportVerdict:
    return FleetExportVerdict(
        host=host,
        backend=backend,
        verdict=cast(FleetVerdict, verdict),
        captured_at=NOW.isoformat(),
        age_seconds=30,
        account="account",
        five_hour_pct=10.0,
        long_window_key="7d" if backend == "claude" else "weekly",
        long_window_pct=12.0,
        binding_window="7d",
        resume_at_epoch=0,
        policy_rev="rev",
        error="",
        path=f"/tmp/{host}/{backend}.json",
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I hit my usage limit and cannot continue.", "usage"),
        ("I cannot merge the pull request without you.", "merge-rights"),
        ("Waiting on your approval before I touch the config.", "approval"),
        ("I am unable to read the screen at all.", "capability-denial"),
        ("Here is a status note with nothing blocking.", "unclassified"),
    ],
)
def test_classify_claim_picks_the_most_specific_kind(text: str, expected: str) -> None:
    assert classify_claim(text) == expected


def test_merge_claim_is_refused_when_a_capability_grants_merging() -> None:
    sources = EvidenceSources(manifest=_manifest(grants=["Merge a green pull request."], excludes=["Author code."]))
    resolution = resolve_merge_rights(_claim("I cannot merge the pull request without you."), sources)
    assert resolution.verdict == "fleet-doable"
    assert resolution.evidence
    assert "chitra-merged" in resolution.directive


def test_merge_claim_is_left_open_when_no_capability_grants_merging() -> None:
    sources = EvidenceSources(manifest=_manifest(grants=["Read one pull request."], excludes=["Merge pull requests."]))
    resolution = resolve_merge_rights(_claim("Please merge the pull request for me."), sources)
    assert resolution.verdict == "undetermined"
    assert resolution.evidence
    assert not resolution.directive


@pytest.mark.parametrize(
    ("authority_line", "expected"),
    [
        ("Merge one allowlisted, lane-authored, non-draft pull request whose merge state is clean.", True),
        ("Merge, approve, or modify pull requests.", True),
        ("Merges a green pull request on the operator's behalf.", True),
        ("Read one pull request's merge state through the GitHub GraphQL API.", False),
        ("Report the merge status of every open pull request.", False),
        ("Author or edit any code in the reviewed pull request.", False),
    ],
)
def test_only_a_line_that_performs_a_merge_counts_as_granting_one(authority_line: str, expected: bool) -> None:
    assert grants_merging(authority_line) is expected


def test_reading_merge_state_does_not_make_a_capability_a_merge_route() -> None:
    """Wave one's auto-merge entry reads merge state and also merges.

    Only the second line may arm the resolver, so a future read-only capability
    cannot make this refuse a merge claim the fleet genuinely cannot perform.
    """
    sources = EvidenceSources(
        manifest=_manifest(
            grants=["Read one pull request's merge state through the GitHub GraphQL API."],
            excludes=["Merge, approve, or modify pull requests."],
        )
    )
    assert resolve_merge_rights(_claim("I cannot merge the pull request."), sources).verdict == "undetermined"


def test_merge_claim_is_left_open_when_the_manifest_is_unreadable() -> None:
    resolution = resolve_merge_rights(_claim("Merge the pull request."), EvidenceSources())
    assert resolution.verdict == "undetermined"
    assert resolution.evidence == ()


def test_approval_claim_is_refused_by_a_recorded_decision() -> None:
    decision = DecisionEntry(
        decision_id="d1",
        at=NOW.isoformat(),
        kind="doctrine-override",
        decision="Merging a green pull request never waits for approval.",
        basis="The standing direction removed approval from the merge path.",
        citation="conversation 2026-08-12",
        authority="The operator ruled this in chat.",
    )
    sources = EvidenceSources(decisions=(decision,))
    resolution = resolve_approval(_claim("I need approval before merging this green pull request."), sources)
    assert resolution.verdict == "false-block"
    assert resolution.evidence[0].source == "decisions-ledger"


def test_approval_claim_is_left_open_without_a_matching_ruling() -> None:
    decision = DecisionEntry(
        decision_id="d1",
        at=NOW.isoformat(),
        kind="pause",
        decision="The reporting front end pauses until next month.",
        basis="The team that owns it is away.",
        citation="conversation 2026-08-01",
        authority="The operator ruled this in chat.",
    )
    resolution = resolve_approval(_claim("I need approval to buy another storage volume."), EvidenceSources(decisions=(decision,)))
    assert resolution.verdict == "undetermined"


def test_approval_claim_matches_an_operator_ruling_in_the_conversation_log() -> None:
    entry = ConversationEntry(
        thread_id="t1",
        seq=2,
        kind="operator_ruling",
        at=NOW.isoformat(),
        session_ref="tophand:widget-build:0.0",
        payload={"text": "Never wait on approval to merge a green pull request again.", "via": "chat"},
    )
    resolution = resolve_approval(_claim("Do I have approval to merge this green pull request?"), EvidenceSources(rulings=(entry,)))
    assert resolution.verdict == "false-block"
    assert resolution.evidence[0].source == "conversation-ledger"


def test_usage_claim_is_refused_when_every_export_for_the_host_reads_clear() -> None:
    sources = EvidenceSources(usage=(_usage("tophand", "claude", "ok"), _usage("tophand", "codex", "ok")))
    resolution = resolve_usage(_claim("I have hit my usage limit for the week."), sources)
    assert resolution.verdict == "false-block"
    assert len(resolution.evidence) == 2


def test_usage_claim_is_left_open_when_an_export_reports_a_pause() -> None:
    sources = EvidenceSources(usage=(_usage("tophand", "claude", "ok"), _usage("tophand", "codex", "pause")))
    resolution = resolve_usage(_claim("I have hit my usage limit for the week."), sources)
    assert resolution.verdict == "undetermined"


def test_usage_claim_is_left_open_when_a_stale_export_hides_the_answer() -> None:
    sources = EvidenceSources(usage=(_usage("tophand", "claude", "stale-export"), _usage("tophand", "codex", "ok")))
    assert resolve_usage(_claim("rate-limit reached"), sources).verdict == "undetermined"


def test_usage_claim_is_left_open_for_a_host_with_no_exports() -> None:
    sources = EvidenceSources(usage=(_usage("trinity", "claude", "ok"),))
    assert resolve_usage(_claim("rate-limit reached"), sources).verdict == "undetermined"


def test_capability_denial_is_refused_by_the_session_s_own_history() -> None:
    transcript = "\n".join(
        [
            "I cannot capture the screen at all.",
            "Captured the screen and saved the image to disk.",
        ]
    )
    sources = EvidenceSources(transcripts=(("widget-build", transcript),))
    resolution = resolve_capability_denial(_claim("I cannot capture the screen at all."), sources)
    assert resolution.verdict == "false-block"
    assert "Captured the screen" in resolution.evidence[0].finding


def test_capability_denial_ignores_lines_that_are_themselves_failures() -> None:
    transcript = "The screen capture failed again and I cannot capture the screen."
    sources = EvidenceSources(transcripts=(("widget-build", transcript),))
    assert resolve_capability_denial(_claim("I cannot capture the screen."), sources).verdict == "undetermined"


def test_capability_denial_is_left_open_without_a_recorded_history() -> None:
    assert resolve_capability_denial(_claim("I cannot capture the screen."), EvidenceSources()).verdict == "undetermined"


def test_deterministic_stage_leaves_an_unclassified_claim_open() -> None:
    outcome = adjudicate_deterministic(_claim("A plain progress note."), EvidenceSources(), now=NOW)
    assert outcome.claim_class == "unclassified"
    assert outcome.verdict == "undetermined"
    assert outcome.stage == "deterministic"


def test_a_refused_block_must_cite_and_direct() -> None:
    with pytest.raises(ValueError, match="cite the evidence"):
        Adjudication(
            claim=_claim("blocked"),
            claim_class="usage",
            stage="deterministic",
            verdict="false-block",
            directive="Continue against the recorded goal.",
            basis="Nothing supports the report.",
            adjudicated_at=NOW.isoformat(),
        )


def test_an_escalation_may_not_also_direct_the_session() -> None:
    with pytest.raises(ValueError, match="cannot also direct"):
        Adjudication(
            claim=_claim("blocked"),
            claim_class="unclassified",
            stage="reasoned",
            verdict="operator-required",
            directive="Keep going.",
            escalation="Do you want to spend money on a second storage volume?",
            escalation_class="spend",
            basis="Spending is yours to decide.",
            adjudicated_at=NOW.isoformat(),
        )


def test_an_escalation_must_name_one_of_the_three_kinds() -> None:
    with pytest.raises(ValueError, match="presence, spend, or a change of scope"):
        Adjudication(
            claim=_claim("blocked"),
            claim_class="unclassified",
            stage="reasoned",
            verdict="operator-required",
            escalation="Something needs you.",
            basis="Nothing settled this.",
            adjudicated_at=NOW.isoformat(),
        )


class _FixedAdjudicator:
    """A stand-in reasoning stage that returns exactly what a test needs."""

    def __init__(self, reply: AdjudicatorReply) -> None:
        self.reply = reply
        self.calls = 0

    def adjudicate(self, claim: BlockerClaim, context: AdjudicationContext, evidence: Sequence[Evidence]) -> AdjudicatorReply:
        self.calls += 1
        return self.reply


def _context() -> AdjudicationContext:
    return AdjudicationContext(goal=_goal(), doctrine="Ask upward only about presence, spend, or scope.", canonical_decisions=())


def test_the_reasoning_stage_only_runs_on_what_stage_one_left_open() -> None:
    sources = EvidenceSources(usage=(_usage("tophand", "claude", "ok"), _usage("tophand", "codex", "ok")))
    stage_two = _FixedAdjudicator(
        AdjudicatorReply(
            claim_id="sha256:0",
            verdict="operator-required",
            escalation="Do you want to spend money here?",
            escalation_class="spend",
        )
    )
    outcome = adjudicate(_claim("I hit my usage limit."), _context(), sources, adjudicator=stage_two, now=NOW)
    assert outcome.stage == "deterministic"
    assert outcome.verdict == "false-block"
    assert stage_two.calls == 0


def test_the_reasoning_stage_escalates_only_a_qualifying_question() -> None:
    claim = _claim("Should we buy a second storage volume for this?")
    reply = AdjudicatorReply(
        claim_id=claim.claim_id,
        verdict="operator-required",
        escalation="Do you want to spend money on a second storage volume?",
        escalation_class="spend",
    )
    outcome = adjudicate(claim, _context(), EvidenceSources(), adjudicator=_FixedAdjudicator(reply), now=NOW)
    assert outcome.stage == "reasoned"
    assert outcome.verdict == "operator-required"
    assert outcome.escalation_class == "spend"
    assert outcome.reaches_operator


def test_a_reasoned_refusal_carries_its_citations_forward() -> None:
    claim = _claim("A plain progress note that needs a ruling.")
    reply = AdjudicatorReply(
        claim_id=claim.claim_id,
        verdict="fleet-doable",
        directive="Use the recorded route and continue against the recorded goal.",
        citations=("the recorded goal scope line",),
    )
    outcome = adjudicate(claim, _context(), EvidenceSources(), adjudicator=_FixedAdjudicator(reply), now=NOW)
    assert outcome.verdict == "fleet-doable"
    assert outcome.evidence[-1].finding == "the recorded goal scope line"


def test_a_reply_bound_to_a_different_claim_is_discarded() -> None:
    claim = _claim("A plain progress note.")
    reply = AdjudicatorReply(
        claim_id="sha256:not-this-claim",
        verdict="operator-required",
        escalation="Do you want to spend money here?",
        escalation_class="spend",
    )
    outcome = adjudicate(claim, _context(), EvidenceSources(), adjudicator=_FixedAdjudicator(reply), now=NOW)
    assert outcome.verdict == "undetermined"


def test_an_escalation_that_would_not_read_plainly_is_discarded() -> None:
    claim = _claim("A plain progress note.")
    reply = AdjudicatorReply(
        claim_id=claim.claim_id,
        verdict="operator-required",
        escalation="lane needs presence at the machine",
        escalation_class="presence",
    )
    outcome = adjudicate(claim, _context(), EvidenceSources(), adjudicator=_FixedAdjudicator(reply), now=NOW)
    assert outcome.verdict == "undetermined"
    assert "would not read plainly" in outcome.basis
    assert "lane" in outcome.basis


def test_the_reasoning_prompt_states_the_vocabulary_rule_it_is_judged_against() -> None:
    """The word that tripped the readability gate in a live run is now ruled out up front."""
    prompt = ClaudeProcessAdjudicator._prompt(_claim("A note."), _context(), ())
    assert 'say "work session", never "lane"' in prompt


def test_a_reasoning_stage_that_cannot_run_leaves_the_claim_open() -> None:
    class _Failing:
        def adjudicate(self, claim: BlockerClaim, context: AdjudicationContext, evidence: Sequence[Evidence]) -> AdjudicatorReply:
            raise AdjudicatorProcessError("the process died")

    outcome = adjudicate(_claim("A plain progress note."), _context(), EvidenceSources(), adjudicator=_Failing(), now=NOW)
    assert outcome.verdict == "undetermined"


def test_a_reply_must_choose_between_directing_and_escalating() -> None:
    with pytest.raises(ValueError, match="must carry a directive"):
        AdjudicatorReply(claim_id="sha256:0", verdict="false-block", citations=("a citation",))


def test_an_escalation_reply_must_be_one_line() -> None:
    with pytest.raises(ValueError, match="exactly one line"):
        AdjudicatorReply(
            claim_id="sha256:0",
            verdict="operator-required",
            escalation="Do you want to spend money here?\nAnd on what?",
            escalation_class="spend",
        )


def test_an_adjudication_renders_into_the_existing_decisions_record() -> None:
    outcome = Adjudication(
        claim=_claim("I cannot merge the pull request."),
        claim_class="merge-rights",
        stage="deterministic",
        verdict="fleet-doable",
        evidence=(Evidence(source="capability-manifest", reference="capability auto-merge", finding="Merge a green pull request."),),
        directive="The recorded merge route already carries this. Continue against the recorded goal.",
        basis="The capability manifest grants merging, so this does not need a person.",
        adjudicated_at=NOW.isoformat(),
    )
    entry = decision_entry(outcome, authority="The adjudication service decided this under standing direction.", decision_id="abc")
    assert entry.kind == "adjudication"
    assert entry.citation.startswith("capability-manifest")


def test_an_escalation_renders_into_the_existing_operator_brief() -> None:
    outcome = Adjudication(
        claim=_claim("Should we buy a second storage volume?"),
        claim_class="unclassified",
        stage="reasoned",
        verdict="operator-required",
        escalation="Do you want to spend money on a second storage volume?",
        escalation_class="spend",
        basis="Spending is yours to decide.",
        adjudicated_at=NOW.isoformat(),
    )
    brief = escalation_brief(outcome, program="Widget build service", source_ref="tophand:widget-build:0.0 open_ask")
    assert brief.category == "decision"
    assert brief.decision == "Do you want to spend money on a second storage volume?"
    assert brief.source_quote == ["Should we buy a second storage volume?"]


#: Verbatim from the conductor-audit goal record on 2026-08-16. Four questions
#: with four different answers, recorded as one line. A dry run against real
#: fleet state adjudicated the whole thing as one claim, which is the defect
#: split_claim_text exists to fix.
REAL_FOUR_ASK_NEEDS = (
    "Four operator answers, all at the end of the evidence file: (1) Slack export or read-only "
    "token for D0B8CQVUQSC and C0B7NDQSFV2 covering 2-14 Aug, the only way to close the "
    "delivery-record gap since the governed slack.conversations.history call is refused; "
    "(2) whether the proposed warden actuator is wanted, since it would give a read-only "
    "component restart authority; (3) whether to add the audit account to adm on tophand, "
    "without which the ansible-pull failure cause stays unreadable; (4) whether the "
    "observability floor should go fleet-wide or tophand-only is deliberate."
)


def test_a_real_recorded_line_carrying_four_asks_becomes_four_claims() -> None:
    parts = split_claim_text(REAL_FOUR_ASK_NEEDS)
    assert len(parts) == 4
    assert all(part.startswith("Four operator answers, all at the end of the evidence file:") for part in parts)
    assert "Slack export" in parts[0]
    assert "warden actuator" in parts[1]
    assert "audit account to adm" in parts[2]
    assert "observability floor" in parts[3]
    assert "warden actuator" not in parts[0]


def test_one_ask_stays_one_claim() -> None:
    assert split_claim_text("Can you merge the pull request?") == ["Can you merge the pull request?"]


def test_a_stray_reference_number_never_splits_a_sentence() -> None:
    text = "The check failed on run (3) of the retry loop and I cannot get past it."
    assert split_claim_text(text) == [text]


def test_numbering_that_does_not_run_from_one_is_left_alone() -> None:
    text = "See (2) above and (5) below for the detail that blocks this."
    assert split_claim_text(text) == [text]


def test_a_line_leading_numbered_list_is_split_by_the_existing_reader() -> None:
    text = "Open asks:\n1. Grant the token for the export.\n2. Confirm the restart authority.\n"
    parts = split_claim_text(text)
    assert len(parts) == 2
    assert "Grant the token" in parts[0]


def test_only_the_recent_slice_of_a_recorded_history_is_read(tmp_path: Path) -> None:
    path = tmp_path / "tmux-transcript.log"
    path.write_text("first line that must not be quoted\n" + ("filler line\n" * 200), encoding="utf-8")
    tail = read_transcript_tail(path, max_bytes=64)
    assert "first line that must not be quoted" not in tail
    assert len(tail) <= 64


def test_the_reasoning_process_is_granted_no_tools_and_no_memory() -> None:
    """The instruction not to fetch anything must be enforced, not just asked for."""
    captured: list[list[str]] = []

    def _runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        reply = AdjudicatorReply(
            claim_id=_claim("A plain progress note.").claim_id,
            verdict="operator-required",
            escalation="Do you want to spend money on a second storage volume?",
            escalation_class="spend",
        )
        return subprocess.CompletedProcess(command, 0, reply.model_dump_json(), "")

    adjudicator = ClaudeProcessAdjudicator(runner=_runner)
    adjudicator.adjudicate(_claim("A plain progress note."), _context(), ())
    command = captured[0]
    assert "--no-session-persistence" in command
    assert command[command.index("--allowed-tools") + 1] == ""


def test_a_reasoning_process_that_exits_non_zero_is_an_error() -> None:
    def _runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "the model was unavailable")

    with pytest.raises(AdjudicatorProcessError, match="the model was unavailable"):
        ClaudeProcessAdjudicator(runner=_runner).adjudicate(_claim("A note."), _context(), ())


#: The exact packaging a real model used on 2026-08-16, which the strict parser
#: rejected until the unwrapping below existed.
REAL_FENCED_REPLY = (
    '```json\n{\n  "claim_id": "{claim_id}",\n  "verdict": "operator-required",\n'
    '  "escalation": "Do you want to spend money on a second storage volume?",\n'
    '  "escalation_class": "spend"\n}\n```'
)


# The unwrapping rule itself is tested at its home, in test_goal_enforcement.py.
# What belongs here is that this adjudicator actually applies it, and that
# applying it does not soften the reply contract — the two tests below.


def test_a_fenced_reply_from_a_real_model_is_accepted() -> None:
    claim = _claim("A plain progress note.")
    payload = REAL_FENCED_REPLY.replace("{claim_id}", claim.claim_id)

    def _runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, payload, "")

    reply = ClaudeProcessAdjudicator(runner=_runner).adjudicate(claim, _context(), ())
    assert reply.verdict == "operator-required"
    assert reply.escalation_class == "spend"


def test_unwrapping_never_rescues_a_reply_that_breaks_the_contract() -> None:
    """Tolerating packaging must not tolerate a wrong answer."""
    claim = _claim("A plain progress note.")
    payload = (
        f'```json\n{{"claim_id": "{claim.claim_id}", "verdict": "operator-required", '
        '"escalation": "Something needs you."}\n```'
    )

    def _runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, payload, "")

    with pytest.raises(AdjudicatorProcessError, match="unusable answer"):
        ClaudeProcessAdjudicator(runner=_runner).adjudicate(claim, _context(), ())


def test_a_null_directive_on_an_escalation_is_read_as_absent() -> None:
    """Seen in a live run: the unused field comes back null rather than empty."""
    reply = AdjudicatorReply.model_validate(
        {
            "claim_id": "sha256:0",
            "verdict": "operator-required",
            "directive": None,
            "escalation": "Do you want to spend money on a second storage volume?",
            "escalation_class": "spend",
        }
    )
    assert reply.directive == ""
    assert reply.escalation_class == "spend"


def test_an_empty_escalation_class_on_a_refusal_is_read_as_absent() -> None:
    """Seen in a live run: the unused field comes back as an empty string."""
    reply = AdjudicatorReply.model_validate(
        {
            "claim_id": "sha256:0",
            "verdict": "false-block",
            "directive": "Continue against the recorded goal.",
            "escalation": "",
            "escalation_class": "",
            "citations": ["the recorded scope line"],
        }
    )
    assert reply.escalation_class is None
    assert reply.verdict == "false-block"


def test_normalizing_empty_fields_never_rescues_a_broken_answer() -> None:
    with pytest.raises(ValueError, match="presence, spend, or a change of scope"):
        AdjudicatorReply.model_validate(
            {
                "claim_id": "sha256:0",
                "verdict": "operator-required",
                "directive": None,
                "escalation": "Something needs you.",
                "escalation_class": "",
            }
        )
    with pytest.raises(ValueError, match="must cite what refuted it"):
        AdjudicatorReply.model_validate(
            {
                "claim_id": "sha256:0",
                "verdict": "false-block",
                "directive": "Continue against the recorded goal.",
                "escalation_class": None,
                "citations": None,
            }
        )


def test_a_reasoning_process_that_answers_with_prose_is_an_error() -> None:
    def _runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "I think this one probably needs the operator.", "")

    with pytest.raises(AdjudicatorProcessError, match="unusable answer"):
        ClaudeProcessAdjudicator(runner=_runner).adjudicate(_claim("A note."), _context(), ())


def test_the_reasoning_prompt_carries_every_layer_and_the_claim() -> None:
    claim = _claim("A plain progress note.")
    prompt = ClaudeProcessAdjudicator._prompt(claim, _context(), ())
    assert claim.claim_id in prompt
    assert "presence" in prompt
    assert "Ask upward only about presence, spend, or scope." in prompt

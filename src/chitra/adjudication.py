"""Two-stage adjudication of blocker claims raised by watched sessions.

Stage one is deterministic. It assembles evidence for a claim and settles the
mechanically checkable kinds: a claim that merging is blocked, checked against
the capability manifest; a claim that approval is missing, checked against the
recorded decisions and operator rulings; a claim about provider usage, checked
against the exported fleet usage tree; and a claim that the session cannot do
something, checked against that session's own transcript.

Stage two is prompted. Whatever stage one could not settle goes to one bounded
adjudicator process that is handed the goal record, the doctrine layer, and the
canonical-decision layer. It must answer with a cited directive back to the
session, or with a single escalation line naming physical presence, spend, or a
genuine change of scope. Nothing else may reach the operator.

This module never talks to a tmux pane and never merges anything. It reads
already-recorded state, decides, and hands the result to its caller.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from chitra.capabilities import Capability, CapabilityManifest, load_manifest
from chitra.convlog import ConversationEntry, OperatorBrief, read_entries
from chitra.decisions import DecisionEntry, read_decisions
from chitra.goals import GoalRecord, lane_id_from_session_ref, session_host
from chitra.lexicon import OPERATOR_GATE_PATTERNS
from chitra.plain_english import plain_english_issues
from chitra.usage_export import FleetExportVerdict, read_fleet_exports

logger = structlog.get_logger(__name__)

SCHEMA: Literal["chitra.adjudication.v1"] = "chitra.adjudication.v1"

ClaimClass = Literal["merge-rights", "approval", "usage", "capability-denial", "unclassified"]
Verdict = Literal["false-block", "fleet-doable", "operator-required", "undetermined"]
EscalationClass = Literal["presence", "spend", "scope-change"]
EvidenceSource = Literal[
    "capability-manifest",
    "decisions-ledger",
    "conversation-ledger",
    "usage-export",
    "session-transcript",
]

#: The only three question kinds allowed to reach the operator. Anything else
#: the adjudicator must either answer itself or leave undetermined.
ESCALATION_CLASSES: tuple[EscalationClass, ...] = ("presence", "spend", "scope-change")

#: Verdicts that refuse the claimed block and therefore owe a directive.
REFUSING_VERDICTS: frozenset[str] = frozenset({"false-block", "fleet-doable"})

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]*")
_STOPWORDS = frozenset(
    {
        "about", "after", "again", "also", "because", "been", "before", "being", "cannot", "could",
        "does", "doing", "done", "from", "have", "having", "here", "into", "just", "more", "most",
        "need", "needs", "only", "other", "over", "same", "should", "since", "some", "such", "than",
        "that", "them", "then", "there", "these", "they", "this", "those", "through", "under",
        "until", "very", "were", "what", "when", "where", "which", "while", "will", "with", "would",
        "your",
    }
)

#: Ordered so a compound claim lands on its most specific kind. "I cannot merge
#: the pull request" is a merge-rights claim, not a generic capability denial.
_CLASSIFIERS: tuple[tuple[ClaimClass, re.Pattern[str]], ...] = (
    (
        "usage",
        re.compile(
            r"rate[-\s]?limit|usage\s+(?:cap|limit)|weekly\s+cap|out\s+of\s+credit|quota|"
            r"hit\s+(?:my|the)\s+limit|token\s+budget",
            re.IGNORECASE,
        ),
    ),
    (
        "merge-rights",
        re.compile(r"\bmerg\w*|\bpull\s+request\b|\bPR\s*#?\d+\b|\bland\s+the\s+(?:branch|change)", re.IGNORECASE),
    ),
    (
        "approval",
        re.compile(
            r"\bapprov\w*|\bsign[-\s]?off\b|\bauthoriz\w*|\bpermission\s+to\b|\bgreen[-\s]?light\b|"
            r"\bconfirm\s+before\b|\bwaiting\s+on\s+(?:a\s+)?(?:ruling|decision)",
            re.IGNORECASE,
        ),
    ),
    (
        "capability-denial",
        re.compile(
            r"\bcan(?:no|')?t\b|\bcannot\b|\bunable\s+to\b|\bno\s+access\b|\bnot\s+able\s+to\b|"
            r"\black(?:s|ing)?\s+(?:access|permission|the\s+ability)|\bblocked\s+on\b",
            re.IGNORECASE,
        ),
    ),
)

_DENIAL_MARKERS = re.compile(
    r"\bcan(?:no|')?t\b|\bcannot\b|\bunable\b|\bno\s+access\b|\bblocked\b|\bfail(?:ed|ure)?\b|\brefus\w*",
    re.IGNORECASE,
)

_MERGE_GRANT = re.compile(r"\bmerg\w*", re.IGNORECASE)

#: How much of a claim's distinctive vocabulary a recorded ruling must share
#: before the ruling is treated as already covering that claim.
RULING_MATCH_MIN_TERMS = 3
RULING_MATCH_MIN_RATIO = 0.34

#: A transcript line must share this many of the claim's distinctive terms, and
#: carry no denial wording, to count as the session having done the thing before.
TRANSCRIPT_MATCH_MIN_TERMS = 2
TRANSCRIPT_QUOTE_MAX_CHARS = 300

#: Recorded session histories run to megabytes. Only the most recent slice is
#: read, so this module cannot become the reason a sweep exhausts memory.
DEFAULT_TRANSCRIPT_TAIL_BYTES = 256 * 1024


class AdjudicationError(ValueError):
    """Raised when an adjudication contract cannot be satisfied."""


class AdjudicatorProcessError(AdjudicationError):
    """Raised when the bounded adjudicator process fails or answers badly."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _utc_now(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


def _stem(word: str) -> str:
    """Fold the common English endings so one verb form matches another.

    This is a deliberately small rule set, not a language model. It exists so a
    session reporting that it "cannot capture the screen" is matched against its
    own earlier line saying it "captured the screen".
    """
    for suffix in ("ing", "ed", "es"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    else:
        if word.endswith("s") and not word.endswith("ss") and len(word) >= 5:
            word = word[:-1]
    return word[:-1] if word.endswith("e") and len(word) >= 5 else word


def _terms(text: str) -> set[str]:
    """Return the distinctive, ending-folded words of one piece of text."""
    return {_stem(word) for word in _WORD_RE.findall(text.casefold()) if len(word) >= 4 and word not in _STOPWORDS}


class BlockerClaim(_FrozenModel):
    """One claim, raised by a watched session, that its work is blocked."""

    session_ref: str = Field(min_length=1)
    text: str = Field(min_length=1)
    origin: Literal["open_ask", "needs", "interview"]
    """Where the claim came from: a recorded ask, the recorded ``needs`` line,
    or a short-interview question the record's own primary source could not
    answer."""
    observed_at: str = Field(min_length=1)

    @property
    def claim_id(self) -> str:
        """Return the content identity of this claim, stable across sweeps."""
        digest = hashlib.sha256(f"{self.session_ref}\x1f{self.text.strip()}".encode()).hexdigest()
        return f"sha256:{digest}"

    @property
    def terms(self) -> set[str]:
        return _terms(self.text)


class Evidence(_FrozenModel):
    """One recorded fact the adjudication rests on."""

    source: EvidenceSource
    reference: str = Field(min_length=1)
    finding: str = Field(min_length=1)


class Adjudication(_FrozenModel):
    """The decided outcome for one blocker claim."""

    schema_: Literal["chitra.adjudication.v1"] = Field(default=SCHEMA, alias="schema")
    claim: BlockerClaim
    claim_class: ClaimClass
    stage: Literal["deterministic", "reasoned"]
    verdict: Verdict
    evidence: tuple[Evidence, ...] = ()
    directive: str = ""
    escalation: str = ""
    escalation_class: EscalationClass | None = None
    basis: str = Field(min_length=1)
    adjudicated_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_outcome(self) -> Adjudication:
        """Keep every outcome shape self-consistent and citable."""
        if self.verdict in REFUSING_VERDICTS:
            if not self.directive.strip():
                raise ValueError("a refused block must carry the directive that replaces it")
            if not self.evidence:
                raise ValueError("a refused block must cite the evidence that refuted it")
            if self.escalation.strip() or self.escalation_class is not None:
                raise ValueError("a refused block cannot also escalate")
        if self.verdict == "operator-required":
            if not self.escalation.strip():
                raise ValueError("an escalation must carry its one-line question")
            if self.escalation_class is None:
                raise ValueError("an escalation must name presence, spend, or a change of scope")
            if self.directive.strip():
                raise ValueError("an escalation cannot also direct the session")
        if self.verdict == "undetermined" and (self.directive.strip() or self.escalation.strip()):
            raise ValueError("an undetermined claim carries neither a directive nor an escalation")
        return self

    @property
    def reaches_operator(self) -> bool:
        return self.verdict == "operator-required"


def read_transcript_tail(path: Path, *, max_bytes: int = DEFAULT_TRANSCRIPT_TAIL_BYTES) -> str:
    """Read only the final slice of a recorded session history.

    The first partial line is dropped so a truncated fragment can never be
    quoted back to a session as if it were a whole recorded line.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    with path.open("rb") as source:
        source.seek(0, 2)
        size = source.tell()
        start = max(size - max_bytes, 0)
        source.seek(start)
        payload = source.read()
    text = payload.decode("utf-8", errors="replace")
    if start and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def classify_claim(text: str) -> ClaimClass:
    """Return the most specific mechanically checkable kind for one claim."""
    for claim_class, pattern in _CLASSIFIERS:
        if pattern.search(text):
            return claim_class
    return "unclassified"


@dataclass(frozen=True, slots=True)
class EvidenceSources:
    """Everything stage one is allowed to read, gathered once per sweep."""

    manifest: CapabilityManifest | None = None
    decisions: tuple[DecisionEntry, ...] = ()
    rulings: tuple[ConversationEntry, ...] = ()
    usage: tuple[FleetExportVerdict, ...] = ()
    transcripts: tuple[tuple[str, str], ...] = ()

    def transcript_for(self, session_ref: str) -> str:
        """Return the recorded text for one session, or an empty string."""
        wanted = lane_id_from_session_ref(session_ref)
        for key, text in self.transcripts:
            if key in (session_ref, wanted):
                return text
        return ""


def load_evidence_sources(
    *,
    state_dir: Path | None = None,
    convlog_path: Path | None = None,
    decisions_path: Path | None = None,
    fleet_usage_dir: Path | None = None,
    transcripts: Sequence[tuple[str, Path]] = (),
    transcript_tail_bytes: int = DEFAULT_TRANSCRIPT_TAIL_BYTES,
    manifest_path: Path | str | None = None,
) -> EvidenceSources:
    """Read every stage-one source, treating an absent source as absent.

    A source that cannot be read is left empty rather than raised, because a
    missing decisions log must not stop the whole run. Every resolver already
    reports "undetermined" when its own source has nothing to say, so an empty
    source degrades into handing the claim to stage two.
    """
    manifest: CapabilityManifest | None
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        logger.warning("adjudication_manifest_unreadable", error=str(exc))
        manifest = None

    try:
        decisions = tuple(read_decisions(decisions_path))
    except (OSError, ValueError) as exc:
        logger.warning("adjudication_decisions_unreadable", error=str(exc))
        decisions = ()

    try:
        rulings = tuple(entry for entry in read_entries(convlog_path) if entry.kind == "operator_ruling")
    except (OSError, ValueError) as exc:
        logger.warning("adjudication_convlog_unreadable", error=str(exc))
        rulings = ()

    usage: tuple[FleetExportVerdict, ...] = ()
    if fleet_usage_dir is not None:
        try:
            usage = tuple(read_fleet_exports(fleet_usage_dir))
        except (OSError, ValueError) as exc:
            logger.warning("adjudication_usage_unreadable", error=str(exc))
            usage = ()

    recorded: list[tuple[str, str]] = []
    for key, path in transcripts:
        try:
            recorded.append((key, read_transcript_tail(path, max_bytes=transcript_tail_bytes)))
        except OSError as exc:
            logger.warning("adjudication_transcript_unreadable", session=key, error=str(exc))
    _ = state_dir
    return EvidenceSources(
        manifest=manifest,
        decisions=decisions,
        rulings=rulings,
        usage=usage,
        transcripts=tuple(recorded),
    )


@dataclass(frozen=True, slots=True)
class Resolution:
    """One stage-one resolver's finding for a claim."""

    verdict: Verdict
    evidence: tuple[Evidence, ...]
    basis: str
    directive: str = ""


def _merge_capabilities(manifest: CapabilityManifest) -> tuple[Capability, ...]:
    """Return manifest capabilities whose declared authority grants merging."""
    return tuple(
        capability
        for capability in manifest.capabilities
        if any(_MERGE_GRANT.search(grant) for grant in capability.authority.grants)
    )


def resolve_merge_rights(claim: BlockerClaim, sources: EvidenceSources) -> Resolution:
    """Check a merge-is-blocked claim against the declared capability manifest.

    The manifest is the record of what this software is permitted to do. When it
    declares a capability that grants merging, a claim that a merge needs the
    operator is refused and routed to that capability. When no capability grants
    merging, the claim is left undetermined rather than guessed at, because the
    merge path may live outside this package.
    """
    if sources.manifest is None:
        return Resolution(
            verdict="undetermined",
            evidence=(),
            basis="the capability manifest could not be read, so a merge claim cannot be settled mechanically.",
        )
    granting = _merge_capabilities(sources.manifest)
    if granting:
        names = ", ".join(capability.name for capability in granting)
        commands = ", ".join(command.name for capability in granting for command in capability.commands)
        evidence = tuple(
            Evidence(
                source="capability-manifest",
                reference=f"capability {capability.name}",
                finding="; ".join(capability.authority.grants),
            )
            for capability in granting
        )
        return Resolution(
            verdict="fleet-doable",
            evidence=evidence,
            basis=f"the capability manifest grants merging to {names}, so this does not need the operator.",
            directive=(
                f"The recorded capability {names} already carries this merge, through {commands}. "
                "Do not wait on a person for it. Continue against the recorded goal."
            ),
        )
    excluding = tuple(
        capability
        for capability in sources.manifest.capabilities
        if any(_MERGE_GRANT.search(exclusion) for exclusion in capability.authority.excludes)
    )
    evidence = tuple(
        Evidence(
            source="capability-manifest",
            reference=f"capability {capability.name}",
            finding="; ".join(capability.authority.excludes),
        )
        for capability in excluding[:3]
    )
    return Resolution(
        verdict="undetermined",
        evidence=evidence,
        basis="no declared capability grants merging, so whether this merge is genuinely blocked is not settled here.",
    )


_RulingCandidate = tuple[str, str, EvidenceSource]


def _best_ruling_match(claim: BlockerClaim, candidates: Sequence[_RulingCandidate]) -> _RulingCandidate | None:
    """Return the recorded ruling that best covers a claim, if one does."""
    claim_terms = claim.terms
    if not claim_terms:
        return None
    best: tuple[str, str, EvidenceSource] | None = None
    best_score = 0
    for reference, text, source in candidates:
        shared = claim_terms & _terms(text)
        if len(shared) < RULING_MATCH_MIN_TERMS:
            continue
        if len(shared) / len(claim_terms) < RULING_MATCH_MIN_RATIO:
            continue
        if len(shared) > best_score:
            best = (reference, text, source)
            best_score = len(shared)
    return best


def resolve_approval(claim: BlockerClaim, sources: EvidenceSources) -> Resolution:
    """Check an approval-is-missing claim against rulings already recorded.

    A claim that repeats a question the operator has already ruled on is
    refused, and the recorded ruling becomes the directive's citation.
    """
    candidates: list[_RulingCandidate] = []
    for decision in sources.decisions:
        candidates.append((f"decision {decision.decision_id}", f"{decision.decision} {decision.basis}", "decisions-ledger"))
    for ruling in sources.rulings:
        text = ruling.payload.get("text")
        if isinstance(text, str) and text.strip():
            candidates.append((f"ruling in thread {ruling.thread_id} entry {ruling.seq}", text, "conversation-ledger"))
    match = _best_ruling_match(claim, candidates)
    if match is None:
        return Resolution(
            verdict="undetermined",
            evidence=(),
            basis="no recorded ruling covers this approval claim, so it is not settled mechanically.",
        )
    reference, text, source = match
    quote = text.strip()[:TRANSCRIPT_QUOTE_MAX_CHARS]
    return Resolution(
        verdict="false-block",
        evidence=(Evidence(source=source, reference=reference, finding=quote),),
        basis=f"a recorded ruling at {reference} already settles this, so asking again is not warranted.",
        directive=(
            f"This is already settled by a recorded ruling at {reference}: \"{quote}\". "
            "Act on that ruling and continue against the recorded goal."
        ),
    )


def resolve_usage(claim: BlockerClaim, sources: EvidenceSources) -> Resolution:
    """Check a provider-usage claim against the exported fleet usage tree.

    A claim of being capped is refused only when every export for the claiming
    host is fresh and reads clear. A real pause, a missing export, or a stale
    export all leave the claim undetermined, because each of those is a genuine
    condition that the usage machinery, not this module, owns.
    """
    if not sources.usage:
        return Resolution(
            verdict="undetermined",
            evidence=(),
            basis="no exported usage reading is available, so a usage claim cannot be settled mechanically.",
        )
    host = session_host(claim.session_ref)
    relevant = [verdict for verdict in sources.usage if verdict.host == host]
    if not relevant:
        return Resolution(
            verdict="undetermined",
            evidence=(),
            basis=f"no host named {host} appears in the exported usage tree, so the claim is not settled here.",
        )
    evidence = tuple(
        Evidence(
            source="usage-export",
            reference=f"{verdict.host} {verdict.backend} export",
            finding=(
                f"reading is {verdict.verdict}, captured {verdict.age_seconds} seconds ago, "
                f"binding window {verdict.binding_window or 'not reported'} "
                f"at {verdict.long_window_pct if verdict.long_window_pct is not None else 'unreported'} percent"
            ),
        )
        for verdict in relevant
    )
    if all(verdict.verdict == "ok" for verdict in relevant):
        return Resolution(
            verdict="false-block",
            evidence=evidence,
            basis=f"every exported usage reading for {host} is fresh and clear, so no usage cap is holding this work.",
            directive=(
                f"The exported usage readings for {host} are fresh and clear, so no provider cap is holding this work. "
                "Continue against the recorded goal."
            ),
        )
    states = ", ".join(sorted({verdict.verdict for verdict in relevant}))
    return Resolution(
        verdict="undetermined",
        evidence=evidence,
        basis=f"the exported usage readings for {host} are {states}, which the usage machinery owns rather than this decision.",
    )


def resolve_capability_denial(claim: BlockerClaim, sources: EvidenceSources) -> Resolution:
    """Check a cannot-do-this claim against the session's own recorded history.

    A session that has already done the thing it now says it cannot do is
    refused, and the earlier line is quoted back. This check is deliberately
    narrow: it only fires on a recorded line that shares the claim's distinctive
    vocabulary and carries no wording of failure. Everything else it leaves for
    stage two.
    """
    transcript = sources.transcript_for(claim.session_ref)
    if not transcript.strip():
        return Resolution(
            verdict="undetermined",
            evidence=(),
            basis="no recorded history is available for this session, so the claim is not settled mechanically.",
        )
    claim_terms = claim.terms
    if len(claim_terms) < TRANSCRIPT_MATCH_MIN_TERMS:
        return Resolution(
            verdict="undetermined",
            evidence=(),
            basis="the claim carries too little distinctive wording to match against recorded history.",
        )
    for line in transcript.splitlines():
        stripped = line.strip()
        if not stripped or _DENIAL_MARKERS.search(stripped):
            continue
        if len(claim_terms & _terms(stripped)) < TRANSCRIPT_MATCH_MIN_TERMS:
            continue
        quote = stripped[:TRANSCRIPT_QUOTE_MAX_CHARS]
        return Resolution(
            verdict="false-block",
            evidence=(
                Evidence(
                    source="session-transcript",
                    reference=f"recorded history for {claim.session_ref}",
                    finding=quote,
                ),
            ),
            basis="this session's own recorded history shows it already doing what it now reports it cannot do.",
            directive=(
                f"This session already did this once. Its own record shows: \"{quote}\". "
                "Use that same route again and continue against the recorded goal."
            ),
        )
    return Resolution(
        verdict="undetermined",
        evidence=(),
        basis="the recorded history shows no earlier success at this, so the claim is not settled mechanically.",
    )


_RESOLVERS: dict[ClaimClass, Callable[[BlockerClaim, EvidenceSources], Resolution]] = {
    "merge-rights": resolve_merge_rights,
    "approval": resolve_approval,
    "usage": resolve_usage,
    "capability-denial": resolve_capability_denial,
}


def adjudicate_deterministic(claim: BlockerClaim, sources: EvidenceSources, *, now: datetime | None = None) -> Adjudication:
    """Run stage one: classify the claim, then apply its evidence resolver."""
    claim_class = classify_claim(claim.text)
    resolver = _RESOLVERS.get(claim_class)
    if resolver is None:
        return Adjudication(
            claim=claim,
            claim_class=claim_class,
            stage="deterministic",
            verdict="undetermined",
            basis="this claim matches no mechanically checkable kind, so it needs a reasoned answer.",
            adjudicated_at=_utc_now(now),
        )
    resolution = resolver(claim, sources)
    return Adjudication(
        claim=claim,
        claim_class=claim_class,
        stage="deterministic",
        verdict=resolution.verdict,
        evidence=resolution.evidence,
        directive=resolution.directive,
        basis=resolution.basis,
        adjudicated_at=_utc_now(now),
    )


class AdjudicatorReply(BaseModel):
    """The strict answer contract for one bounded adjudicator process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(min_length=1)
    verdict: Literal["false-block", "fleet-doable", "operator-required"]
    directive: str = ""
    escalation: str = ""
    escalation_class: EscalationClass | None = None
    citations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_reply(self) -> AdjudicatorReply:
        if self.verdict in REFUSING_VERDICTS:
            if not self.directive.strip():
                raise ValueError("a refused block must carry a directive")
            if not self.citations:
                raise ValueError("a refused block must cite what refuted it")
            if self.escalation.strip() or self.escalation_class is not None:
                raise ValueError("a refused block cannot also escalate")
        else:
            if not self.escalation.strip():
                raise ValueError("an escalation must carry its one-line question")
            if self.escalation_class is None:
                raise ValueError("an escalation must name presence, spend, or a change of scope")
            if self.directive.strip():
                raise ValueError("an escalation cannot also direct the session")
            if len(self.escalation.strip().splitlines()) != 1:
                raise ValueError("an escalation must be exactly one line")
        return self


@dataclass(frozen=True, slots=True)
class AdjudicationContext:
    """The three recorded layers a reasoned adjudication must decide from."""

    goal: GoalRecord
    doctrine: str
    canonical_decisions: tuple[str, ...]


class BlockerAdjudicator(Protocol):
    """One bounded reasoning invocation over a single unresolved claim."""

    def adjudicate(self, claim: BlockerClaim, context: AdjudicationContext, evidence: Sequence[Evidence]) -> AdjudicatorReply: ...


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]

DOCTRINE_PROMPT_MAX_CHARS = 12000

_ADJUDICATOR_INSTRUCTION = (
    "You adjudicate ONE claim that a watched work session is blocked. Decide only from the supplied "
    "goal record, doctrine, canonical decisions, and evidence. Do not invent facts and do not fetch anything. "
    "Standing operator direction: a claim of being blocked on a person is almost never true, and only three "
    "kinds of question may reach the operator -- physical presence at a machine, spend, and a genuine change "
    "of the agreed scope. Everything else you must answer yourself. "
    "Return exactly one JSON object with claim_id, verdict, directive, escalation, escalation_class, and citations. "
    'verdict is "false-block" when the supplied layers show the obstacle does not hold, "fleet-doable" when the '
    'work can proceed by a route the session already has, and "operator-required" only when the question is '
    "genuinely about presence, spend, or a change of scope. "
    "A refusing verdict needs a directive addressed to the session and at least one citation drawn from the "
    "supplied material. An escalation needs exactly one line and an escalation_class of presence, spend, or "
    "scope-change, and no directive. Preserve claim_id exactly. Write plain sentences and never quote or "
    "impersonate the operator."
)


class ClaudeProcessAdjudicator:
    """Run one fresh, bounded ``claude -p`` process for a single claim."""

    def __init__(
        self,
        *,
        command: str = "claude",
        model: str | None = None,
        timeout_seconds: int = 180,
        runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.command = command
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    @staticmethod
    def _prompt(claim: BlockerClaim, context: AdjudicationContext, evidence: Sequence[Evidence]) -> str:
        request = {
            "claim_id": claim.claim_id,
            "claim": {"session_ref": claim.session_ref, "text": claim.text, "origin": claim.origin},
            "goal_record": context.goal.to_dict(),
            "doctrine": context.doctrine[:DOCTRINE_PROMPT_MAX_CHARS],
            "canonical_decisions": list(context.canonical_decisions),
            "deterministic_evidence": [item.model_dump(mode="json") for item in evidence],
            "escalation_classes": list(ESCALATION_CLASSES),
        }
        return _ADJUDICATOR_INSTRUCTION + "\nINPUT=" + json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def adjudicate(self, claim: BlockerClaim, context: AdjudicationContext, evidence: Sequence[Evidence]) -> AdjudicatorReply:
        command = [self.command, "-p", self._prompt(claim, context, evidence), "--output-format", "text"]
        if self.model is not None:
            command.extend(["--model", self.model])
        try:
            completed = self.runner(command, check=False, capture_output=True, text=True, timeout=self.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdjudicatorProcessError(f"the adjudicator process could not run: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
            raise AdjudicatorProcessError(f"the adjudicator process failed: {detail}")
        try:
            return AdjudicatorReply.model_validate_json(completed.stdout.strip())
        except ValueError as exc:
            raise AdjudicatorProcessError(f"the adjudicator process returned an unusable answer: {exc}") from exc


def _reply_text_issues(reply: AdjudicatorReply) -> tuple[str, ...]:
    """Return readability problems in text the reply would put in front of a person."""
    if reply.verdict == "operator-required":
        return plain_english_issues(reply.escalation, field="escalation")
    return ()


def adjudicate(
    claim: BlockerClaim,
    context: AdjudicationContext,
    sources: EvidenceSources,
    *,
    adjudicator: BlockerAdjudicator | None = None,
    now: datetime | None = None,
) -> Adjudication:
    """Settle one claim: stage one first, then the bounded reasoning stage.

    A stage-two answer that does not satisfy the reply contract, or whose
    escalation would not read plainly to a person, leaves the claim
    undetermined. An undetermined claim stays visible as unfinished work; it
    never becomes a question to the operator by default.
    """
    first = adjudicate_deterministic(claim, sources, now=now)
    if first.verdict != "undetermined" or adjudicator is None:
        return first
    try:
        reply = adjudicator.adjudicate(claim, context, first.evidence)
    except AdjudicationError as exc:
        logger.warning("adjudication_stage_two_failed", claim_id=claim.claim_id, error=str(exc))
        return first
    if reply.claim_id != claim.claim_id:
        logger.warning("adjudication_stage_two_identity_mismatch", claim_id=claim.claim_id)
        return first
    issues = _reply_text_issues(reply)
    if issues:
        logger.warning("adjudication_stage_two_unreadable", claim_id=claim.claim_id, issues=list(issues))
        return first
    evidence = (
        *first.evidence,
        *(
            Evidence(source="decisions-ledger", reference="reasoned adjudication citation", finding=citation)
            for citation in reply.citations
        ),
    )
    basis = (
        "the recorded goal, doctrine, and canonical decisions settle this claim without the operator."
        if reply.verdict in REFUSING_VERDICTS
        else f"this question is genuinely about {reply.escalation_class}, which only the operator can answer."
    )
    return Adjudication(
        claim=claim,
        claim_class=first.claim_class,
        stage="reasoned",
        verdict=reply.verdict,
        evidence=evidence,
        directive=reply.directive,
        escalation=reply.escalation,
        escalation_class=reply.escalation_class,
        basis=basis,
        adjudicated_at=_utc_now(now),
    )


def decision_entry(adjudication: Adjudication, *, authority: str, decision_id: str) -> DecisionEntry:
    """Render one adjudication into the existing monitor decisions record."""
    citation = (
        f"{adjudication.evidence[0].source}: {adjudication.evidence[0].reference}"
        if adjudication.evidence
        else f"claim {adjudication.claim.claim_id}"
    )
    session_ref = adjudication.claim.session_ref
    if adjudication.verdict in REFUSING_VERDICTS:
        decision = f"Refused a reported obstacle for {session_ref} and directed the work to continue."
    elif adjudication.verdict == "operator-required":
        decision = (
            f"Raised a reported obstacle for {session_ref} to the operator "
            f"as a question about {adjudication.escalation_class}."
        )
    else:
        decision = f"Left a reported obstacle for {session_ref} undecided pending better evidence."
    return DecisionEntry(
        decision_id=decision_id,
        at=adjudication.adjudicated_at,
        kind="adjudication",
        decision=decision,
        basis=adjudication.basis,
        citation=citation,
        authority=authority,
    )


def escalation_brief(adjudication: Adjudication, *, program: str, source_ref: str) -> OperatorBrief:
    """Render an escalation into the existing operator brief record."""
    if adjudication.verdict != "operator-required":
        raise AdjudicationError("only an escalation becomes an operator brief")
    quote = adjudication.claim.text.strip()[:400]
    return OperatorBrief(
        session_ref=adjudication.claim.session_ref,
        program=program,
        stage="A work session reported an obstacle that only you can clear.",
        category="decision",
        decision=adjudication.escalation,
        recommendation="",
        recommendation_basis="operator-preference",
        source_quote=[quote],
        source_ref=source_ref,
    )


def directive_gate_reasons(text: str) -> tuple[str, ...]:
    """Return operator-gate wording found in a directive before it is sent."""
    return tuple(dict.fromkeys(reason for reason, pattern in OPERATOR_GATE_PATTERNS if pattern.search(text)))

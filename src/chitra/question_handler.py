"""Deterministic answers and residuals for questions about one goal.

The handler is intentionally smaller than a conversational agent.  It can
answer only facts that are present in the enrolled goal contract or in the
append-only decisions log: a recorded ruling that shares enough content words
with the question answers it, always citing the decision id so a wrong match
stays contestable. Questions neither source settles become residuals for the
foreground Chitra reasoning path. Only questions that request protected
authority are operator-gated, and questions about credentials, spend, or
irreversible actions never take the decisions-log shortcut.

There is no model call and there is no inferred reviewer or approval source in
this module.  The ``request_id`` binds the result to the exact question and
the frozen goal digest, so a stale answer cannot silently follow a changed
goal.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chitra.autonomy import Capability, CapabilityUse, authorize_action, capability_target_from_text
from chitra.decisions import DecisionEntry
from chitra.goals import GoalRecord, done_when_with_delta
from chitra.supervision import goal_digest

QuestionDisposition = Literal["answered", "residual", "operator_required"]
QuestionKind = Literal["next", "scope", "done_when", "small_delta", "unknown"]
QuestionSource = Literal["frozen_goal", "foreground_reasoning", "operator_required", "decisions_log"]
GateReason = Literal[
    "credentials",
    "spend",
    "irreversible",
    "security_boundary",
    "new_dependency",
    "new_schema",
    "new_hook",
    "strategic_scope_change",
    "unknown_or_ambiguous",
    "invalid_frozen_goal",
]


class QuestionHandlerResult(BaseModel):
    """A queue-safe answer, foreground residual, or protected authority gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    session_ref: str = Field(min_length=1)
    goal_version: int = Field(ge=1)
    goal_digest: str = Field(min_length=1)
    question: str = Field(min_length=1)
    # The journal event id of the turn that asked; folded into ``request_id``
    # so a re-ask of the same text is a new, separately delivered request.
    occurrence: str = ""
    kind: QuestionKind
    disposition: QuestionDisposition
    source: QuestionSource
    answer: str | None = None
    reason: str = Field(min_length=1)
    gate_reasons: tuple[GateReason, ...] = ()

    @field_validator("question")
    @classmethod
    def _question_is_not_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be whitespace")
        return value

    @field_validator("goal_digest")
    @classmethod
    def _digest_is_sha256(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("goal_digest must be a lowercase SHA-256 digest")
        return value

    @field_validator("answer")
    @classmethod
    def _answer_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("answer must not be blank")
        return value

    @property
    def queue_key(self) -> str:
        """Return the stable queue identity for this question result."""
        return self.request_id


_NEXT_RE = re.compile(
    r"\b(?:what\s+should\s+(?:i|we)\s+do\s+next|what\s+should\s+happen\s+next|"
    r"what\s+(?:do\s+we\s+do|is|'s)\s+(?:the\s+)?next|what\s+(?:is|'s)\s+next|"
    r"next\s+(?:step|action)|how\s+do\s+i\s+proceed)\b",
    re.IGNORECASE,
)
_DONE_RE = re.compile(
    r"\b(?:what\s+(?:proves?|counts?\s+as)\s+(?:the\s+)?(?:goal\s+)?(?:done|complete)|"
    r"what\s+(?:proves?|counts?\s+as)\s+(?:the\s+)?(?:goal\s+)?is\s+(?:done|complete)|"
    r"how\s+do\s+we\s+know\s+(?:the\s+)?(?:goal\s+is\s+)?(?:done|complete)|"
    r"what\s+are\s+the\s+(?:completion\s+)?criteria|"
    r"when\s+is\s+(?:the\s+)?(?:goal|work)\s+(?:done|complete)|"
    r"what\s+is\s+the\s+(?:frozen\s+)?done\s+condition)\b",
    re.IGNORECASE,
)
_SCOPE_RE = re.compile(
    r"^(?:is|does)\s+(?P<item>.+?)\s+(?P<polarity>out\s+of|not\s+in|in|within)\s+(?:the\s+)?(?:frozen\s+)?scope\??$",
    re.IGNORECASE,
)
_SCOPE_LISTED_RE = re.compile(
    r"^(?:is|does)\s+(?P<item>.+?)\s+(?:explicitly\s+)?listed\s+(?:in\s+the\s+)?(?:frozen\s+)?scope\??$",
    re.IGNORECASE,
)
_SMALL_DELTA_RE = re.compile(
    r"^(?:should|can|may)\s+(?:i|we)\s+(?:make\s+)?(?:a\s+)?"
    r"(?:small|bounded)\s+reversible\s+(?:change|redesign|refactor|adjustment|revision)"
    r"\s+(?:to|of)\s+(?P<item>.+?)\??$",
    re.IGNORECASE,
)

_CREDENTIAL_RE = re.compile(r"\b(?:credential(?:s)?|secret(?:s)?|password(?:s)?|api\s*key|oauth|login|token(?:s)?)\b", re.I)
_SPEND_RE = re.compile(r"\b(?:spend|cost|budget|buy|purchase|paid|billing|price|money)\b|\$\s*\d", re.I)
_IRREVERSIBLE_RE = re.compile(
    r"\b(?:irreversible|delete|deletions|destroy|drop|force\s+push|revoke|close|deploy|release|send|restart|kill|terminate)\b",
    re.I,
)
_SECURITY_RE = re.compile(
    r"\b(?:security\s+boundary|authorization|authz|permissions?|access\s+control|"
    r"identity\s+boundary|privilege(?:s)?|sandbox|firewall|production\s+access|"
    r"root\s+access|public\s+exposure|encrypt(?:ion|ed)?|signing\s+key)\b",
    re.I,
)
_AUTHENTICATION_RE = re.compile(r"\b(?:authentication|authenticate|authn|oauth|login|identity\s+provider)\b", re.I)
_DEPENDENCY_RE = re.compile(r"\b(?:new\s+)?dependenc(?:y|ies)|\b(?:install|add)\s+(?:a\s+)?(?:package|library)\b", re.I)
_SCHEMA_RE = re.compile(r"\b(?:new\s+)?schema|\bschema\s+(?:change|migration)|\bmigration\b", re.I)
_HOOK_RE = re.compile(r"\b(?:new\s+)?hook(?:s)?|\b(?:plugin|integration|endpoint)\b", re.I)
_STRATEGIC_SCOPE_RE = re.compile(
    r"\b(?:change|expand|narrow|redirect|revise|redefine|replace|pivot|add|remove)\b[^?\n]{0,80}\b(?:scope|goal|strategy|objective)\b|"
    r"\b(?:scope|goal|strategy|objective)\b[^?\n]{0,80}\b(?:change|expand|narrow|redirect|revise|redefine|replace|pivot)\b",
    re.I,
)
_FROZEN_OUTCOME_RE = re.compile(
    r"\b(?:change|redirect|revise|redefine|replace|pivot)\b[^?\n]{0,80}\b(?:goal|objective|outcome|done\s+condition)\b|"
    r"\b(?:goal|objective|outcome|done\s+condition)\b[^?\n]{0,80}\b(?:change|redirect|revise|redefine|replace|pivot)\b",
    re.I,
)
_NEGATIVE_SCOPE_RE = re.compile(
    r"\b(?:out\s+of\s+scope|outside\s+(?:the\s+)?scope|not\s+in\s+scope|excluded|exclude|"
    r"must\s+not|do\s+not|don't|never|no\s+|without)\b",
    re.I,
)
_LEADING_SCOPE_LABEL_RE = re.compile(r"^(?:in\s+scope|scope)\s*:\s*", re.I)
_LEADING_BULLET_RE = re.compile(r"^(?:[-*•]\s*|\d+[.)]\s*)")


def _normalized(value: str) -> str:
    value = value.strip().strip("\"'`“”‘’")
    value = re.sub(r"\s+", " ", value)
    return value.rstrip("?.!;:").strip().casefold()


def _scope_entries(scope: str) -> tuple[str, ...]:
    entries: list[str] = []
    for raw in re.split(r"[;\n]+", scope):
        entry = _LEADING_BULLET_RE.sub("", raw.strip())
        entry = _LEADING_SCOPE_LABEL_RE.sub("", entry).strip()
        if entry:
            entries.append(entry)
    return tuple(entries)


def _scope_match(scope: str, item: str) -> bool | None:
    """Return true/false for one explicit item, or None if unsettled."""
    wanted = _normalized(item)
    if not wanted:
        return None
    matches: list[bool] = []
    for entry in _scope_entries(scope):
        candidate = _normalized(entry)
        explicit_item = (
            candidate == wanted
            or candidate.startswith(f"{wanted} is ")
            or candidate.startswith(f"{wanted} are ")
            or candidate.startswith(f"{wanted} - ")
            or candidate.startswith(f"{wanted}: ")
            or candidate.startswith(f"no {wanted}")
            or candidate.startswith(f"excluded {wanted}")
            or candidate.startswith(f"exclude {wanted}")
        )
        if explicit_item:
            matches.append(not _NEGATIVE_SCOPE_RE.search(entry))
    if not matches or len(set(matches)) != 1:
        return None
    return matches[0]


_CAPABILITY_GATE_REASON: dict[Capability, GateReason] = {
    "replan": "strategic_scope_change",
    "small_redesign": "strategic_scope_change",
    "dependency_change": "new_dependency",
    "schema_change": "new_schema",
    "hook_change": "new_hook",
    "credential_use": "credentials",
    "authentication": "security_boundary",
    "security_change": "security_boundary",
    "irreversible_action": "irreversible",
    "spend": "spend",
}
_SPEND_AMOUNT_RE = re.compile(r"\$\s*(?P<amount>\d+(?:\.\d{1,2})?)")


def _capability_uses(
    question: str,
    *,
    scope_query: bool,
    small_delta: bool,
) -> tuple[tuple[CapabilityUse, ...], bool]:
    capabilities: list[Capability] = []
    checks: tuple[tuple[Capability, re.Pattern[str]], ...] = (
        ("credential_use", _CREDENTIAL_RE),
        ("spend", _SPEND_RE),
        ("irreversible_action", _IRREVERSIBLE_RE),
        ("authentication", _AUTHENTICATION_RE),
        ("security_change", _SECURITY_RE),
        ("dependency_change", _DEPENDENCY_RE),
        ("schema_change", _SCHEMA_RE),
        ("hook_change", _HOOK_RE),
    )
    if not scope_query or small_delta:
        for capability, pattern in checks:
            if pattern.search(question):
                capabilities.append(capability)
    if small_delta:
        capabilities.append("small_redesign")
    changes_frozen_outcome = not scope_query and _FROZEN_OUTCOME_RE.search(question) is not None
    if not scope_query and _STRATEGIC_SCOPE_RE.search(question) and not changes_frozen_outcome:
        capabilities.append("replan")

    uses: list[CapabilityUse] = []
    target = capability_target_from_text(question)
    for capability in dict.fromkeys(capabilities):
        if capability == "spend":
            amount_match = _SPEND_AMOUNT_RE.search(question)
            uses.append(
                CapabilityUse(
                    capability="spend",
                    target=target,
                    amount=None if amount_match is None else Decimal(amount_match.group("amount")),
                    currency=None if amount_match is None else "USD",
                )
            )
        else:
            uses.append(CapabilityUse(capability=capability, target=target))
    return tuple(uses), changes_frozen_outcome


# Ask gate: a recorded monitor decision answers a lane's question before the
# question becomes an operator-facing item. The match is a deliberately naive,
# deterministic word overlap — the newest decision in append order that
# shares enough content words wins, so a later ruling supersedes the one it
# reverses. dispatchd recomputes the answer before delivery, so a queued
# answer displaced by a newer ruling is rejected instead of relayed stale.
# Every auto-answer cites the decision id, which keeps a wrong match
# contestable.
_ASK_GATE_MIN_SHARED_WORDS = 3

# Function words long enough to pass the length filter but present in almost
# every question; counting them lets an unrelated ruling answer.
_ASK_GATE_STOPWORDS = frozenset(
    {
        "about", "also", "been", "before", "could", "does", "done", "each", "from", "have",
        "here", "into", "just", "like", "more", "much", "must", "need", "only", "other",
        "over", "same", "should", "some", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "those", "want", "were", "what", "when", "where",
        "which", "while", "will", "with", "would", "your",
    }
)  # fmt: skip

# A word-overlap ruling is never authority for these capability classes; a
# question that needs one keeps the native approval path no matter what the
# decisions log contains.
_ASK_GATE_PROTECTED_CAPABILITIES: frozenset[Capability] = frozenset(
    {"credential_use", "authentication", "security_change", "irreversible_action", "spend"}
)

# Mirror of dispatch._BANNED (kept separate because dispatch imports this
# module through orders). A ruling whose text cannot be relayed verbatim is
# skipped rather than queued as an answer build_question_order would reject.
_ASK_GATE_UNDELIVERABLE_RE = re.compile(r"\boperator\b|\bthe monitor\b|\bchitra (?:wants|says|needs|relays)\b", re.I)


def _decision_words(text: str) -> frozenset[str]:
    return frozenset(
        word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 3 and word not in _ASK_GATE_STOPWORDS
    )


def _decision_is_bound_to(entry: DecisionEntry, *, session_ref: str, goal_version: int, goal_digest_value: str) -> bool:
    """Return whether a bound ruling still refers to this exact contract.

    Entries carrying no binding fields are historical, unbound rulings and
    stay eligible.  A bound entry that disagrees with the live contract on
    any recorded field can no longer answer for it.
    """
    if entry.session_ref and entry.session_ref != session_ref:
        return False
    if entry.goal_version and entry.goal_version != goal_version:
        return False
    return not (entry.goal_digest and entry.goal_digest != goal_digest_value)


def _decision_match_text(entry: DecisionEntry) -> str:
    """Return the text a ruling is matched against: its verbatim ask first."""
    return entry.question or entry.answer or entry.decision


def _decision_deliverable(entry: DecisionEntry) -> str:
    """Return the text relayed to the lane: its verbatim answer first."""
    return entry.answer or entry.decision


def _match_decision(
    question: str,
    decisions: Sequence[DecisionEntry],
    *,
    session_ref: str = "",
    goal_version: int = 0,
    goal_digest_value: str = "",
) -> DecisionEntry | None:
    """Return the newest recorded ruling covering ``question``, or ``None``."""
    question_words = _decision_words(question)
    if len(question_words) < _ASK_GATE_MIN_SHARED_WORDS:
        return None
    for entry in reversed(decisions):
        if not _decision_is_bound_to(
            entry, session_ref=session_ref, goal_version=goal_version, goal_digest_value=goal_digest_value
        ):
            continue
        if _ASK_GATE_UNDELIVERABLE_RE.search(_decision_deliverable(entry)):
            continue
        if len(question_words & _decision_words(_decision_match_text(entry))) >= _ASK_GATE_MIN_SHARED_WORDS:
            return entry
    return None


def _request_id(question: str, digest: str, occurrence: str = "") -> str:
    payload = [digest, question] if not occurrence else [digest, question, occurrence]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _invalid_contract(goal: GoalRecord, *, needs_scope: bool = False) -> bool:
    if not goal.session_ref.strip() or goal.goal_version < 1 or not goal.goal.strip() or not goal.done_when.strip():
        return True
    return needs_scope and not goal.scope.strip()


def _result(
    goal: GoalRecord,
    question: str,
    *,
    kind: QuestionKind,
    disposition: QuestionDisposition,
    source: QuestionSource,
    answer: str | None,
    reason: str,
    gate_reasons: tuple[GateReason, ...] = (),
    occurrence: str = "",
) -> QuestionHandlerResult:
    digest = goal_digest(goal)
    return QuestionHandlerResult(
        request_id=_request_id(question, digest, occurrence),
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest=digest,
        question=question,
        occurrence=occurrence,
        kind=kind,
        disposition=disposition,
        source=source,
        answer=answer,
        reason=reason,
        gate_reasons=gate_reasons,
    )


def _decision_answer(goal: GoalRecord, question: str, ruling: DecisionEntry, *, occurrence: str = "") -> QuestionHandlerResult:
    """Answer from a recorded ruling, citing its decision id for contest."""
    return _result(
        goal,
        question,
        kind="unknown",
        disposition="answered",
        source="decisions_log",
        answer=f"{_decision_deliverable(ruling)} (decision {ruling.decision_id})",
        reason="A recorded decision already settles this question; the decision id is cited so a wrong match can be contested.",
        occurrence=occurrence,
    )


def handle_question(
    goal: GoalRecord,
    question: str,
    *,
    decisions: Sequence[DecisionEntry] = (),
    occurrence: str = "",
) -> QuestionHandlerResult:
    """Answer a frozen-goal question or return a foreground residual.

    The function never guesses what an absent scope item means, never uses
    mutable tactical state as authority, and never returns an action approval.
    When ``decisions`` is supplied, a recorded ruling that clears the ask-gate
    match answers the question with its decision id cited; questions needing a
    protected capability never take that shortcut.
    """
    if not isinstance(question, str) or not question.strip():
        return _result(
            goal,
            "<empty question>",
            kind="unknown",
            disposition="residual",
            source="foreground_reasoning",
            answer=None,
            reason="The question is empty or not text; foreground reasoning must inspect the current lane state.",
            gate_reasons=("unknown_or_ambiguous",),
            occurrence=occurrence,
        )
    text = question.strip()
    scope_match = _SCOPE_RE.fullmatch(text) or _SCOPE_LISTED_RE.fullmatch(text)
    small_delta_match = _SMALL_DELTA_RE.fullmatch(text)
    scope_query = scope_match is not None or small_delta_match is not None
    if small_delta_match is not None and not _invalid_contract(goal, needs_scope=True):
        item = small_delta_match.group("item")
        if _scope_match(goal.scope, item) is False:
            return _result(
                goal,
                text,
                kind="small_delta",
                disposition="answered",
                source="frozen_goal",
                answer=(
                    f"Do not change {item.strip()}; it is explicitly out of the frozen scope. "
                    f"Continue the frozen goal and prove completion with: {done_when_with_delta(goal)}"
                ),
                reason="The exact design surface is explicitly excluded by the frozen contract.",
                occurrence=occurrence,
            )
    capability_uses, changes_frozen_outcome = _capability_uses(
        text,
        scope_query=scope_query,
        small_delta=small_delta_match is not None,
    )
    authority = authorize_action(
        goal.autonomy_policy,
        capability_uses,
        changes_frozen_outcome=changes_frozen_outcome,
    )
    ruling = (
        None
        if any(use.capability in _ASK_GATE_PROTECTED_CAPABILITIES for use in capability_uses)
        else _match_decision(
            text,
            decisions,
            session_ref=goal.session_ref,
            goal_version=goal.goal_version,
            goal_digest_value=goal_digest(goal),
        )
    )
    if authority.disposition == "operator_required":
        if ruling is not None:
            return _decision_answer(goal, text, ruling, occurrence=occurrence)
        reasons = tuple(
            dict.fromkeys(
                ("strategic_scope_change" if changes_frozen_outcome else _CAPABILITY_GATE_REASON[use.capability]) for use in capability_uses
            )
        )
        if not reasons:
            reasons = ("strategic_scope_change",)
        return _result(
            goal,
            text,
            kind="scope" if scope_query else "unknown",
            disposition="operator_required",
            source="operator_required",
            answer=None,
            reason="The frozen goal policy has no valid grant for this action, exceeds its limit, or the action changes the outcome.",
            gate_reasons=reasons,
            occurrence=occurrence,
        )
    if authority.disposition == "foreground_residual":
        return _result(
            goal,
            text,
            kind="scope" if scope_query else "unknown",
            disposition="residual",
            source="foreground_reasoning",
            answer=None,
            reason="The grant may cover this action, but its limits cannot be checked from current evidence; investigate and replan.",
            gate_reasons=("unknown_or_ambiguous",),
            occurrence=occurrence,
        )

    if scope_match is not None:
        if _invalid_contract(goal, needs_scope=True):
            return _result(
                goal,
                text,
                kind="scope",
                disposition="residual",
                source="foreground_reasoning",
                answer=None,
                reason="The frozen goal does not contain a valid scope to inspect; foreground reasoning must repair or replan it.",
                gate_reasons=("invalid_frozen_goal",),
                occurrence=occurrence,
            )
        item = scope_match.group("item")
        explicit = _scope_match(goal.scope, item)
        if explicit is None:
            return _result(
                goal,
                text,
                kind="scope",
                disposition="residual",
                source="foreground_reasoning",
                answer=None,
                reason=(
                    "The frozen scope does not settle that exact item; foreground reasoning must "
                    "investigate and choose the next in-scope path."
                ),
                gate_reasons=("unknown_or_ambiguous",),
                occurrence=occurrence,
            )
        return _result(
            goal,
            text,
            kind="scope",
            disposition="answered",
            source="frozen_goal",
            answer=f"{item.strip()} is {'in' if explicit else 'out of'} the frozen scope.",
            reason="The item is explicitly settled by the frozen scope.",
            occurrence=occurrence,
        )

    if small_delta_match is not None:
        if _invalid_contract(goal, needs_scope=True):
            return _result(
                goal,
                text,
                kind="small_delta",
                disposition="residual",
                source="foreground_reasoning",
                answer=None,
                reason=(
                    "The frozen goal does not contain a valid scope for this design change; foreground reasoning must repair or replan it."
                ),
                gate_reasons=("invalid_frozen_goal",),
                occurrence=occurrence,
            )
        item = small_delta_match.group("item")
        explicit = _scope_match(goal.scope, item)
        if explicit is None:
            return _result(
                goal,
                text,
                kind="small_delta",
                disposition="residual",
                source="foreground_reasoning",
                answer=None,
                reason=(
                    "The frozen scope does not settle that exact design surface; foreground reasoning must investigate before changing it."
                ),
                gate_reasons=("unknown_or_ambiguous",),
                occurrence=occurrence,
            )
        if not explicit:
            answer = (
                f"Do not change {item.strip()}; it is explicitly out of the frozen scope. "
                f"Continue the frozen goal and prove completion with: {done_when_with_delta(goal)}"
            )
        else:
            answer = (
                f"A small reversible change to {item.strip()} is within the frozen scope. "
                "Pursue the redesign through the successive steps needed for the outcome, "
                f"and verify it against: {done_when_with_delta(goal)}"
            )
        return _result(
            goal,
            text,
            kind="small_delta",
            disposition="answered",
            source="frozen_goal",
            answer=answer,
            reason="The exact design surface and completion proof are settled by the frozen contract.",
            occurrence=occurrence,
        )

    if _DONE_RE.search(text):
        if _invalid_contract(goal):
            return _result(
                goal,
                text,
                kind="done_when",
                disposition="residual",
                source="foreground_reasoning",
                answer=None,
                reason="The frozen goal does not contain a valid completion condition; foreground reasoning must repair or replan it.",
                gate_reasons=("invalid_frozen_goal",),
                occurrence=occurrence,
            )
        return _result(
            goal,
            text,
            kind="done_when",
            disposition="answered",
            source="frozen_goal",
            answer=f"The completion condition is: {done_when_with_delta(goal)}",
            reason="The completion condition is copied from the frozen goal.",
            occurrence=occurrence,
        )

    if _NEXT_RE.search(text):
        if _invalid_contract(goal):
            return _result(
                goal,
                text,
                kind="next",
                disposition="residual",
                source="foreground_reasoning",
                answer=None,
                reason=(
                    "The frozen goal is not complete enough to determine the next direction; "
                    "foreground reasoning must investigate and replan it."
                ),
                gate_reasons=("invalid_frozen_goal",),
                occurrence=occurrence,
            )
        return _result(
            goal,
            text,
            kind="next",
            disposition="answered",
            source="frozen_goal",
            answer=(
                "Continue the frozen goal within its stated scope and produce proof for every completion condition: "
                f"{done_when_with_delta(goal)}"
            ),
            reason="The next bounded direction is determined by the frozen goal and completion condition.",
            occurrence=occurrence,
        )

    if ruling is not None:
        return _decision_answer(goal, text, ruling, occurrence=occurrence)
    return _result(
        goal,
        text,
        kind="unknown",
        disposition="residual",
        source="foreground_reasoning",
        answer=None,
        reason=(
            "The frozen goal does not deterministically settle this question; foreground reasoning must investigate and continue pursuit."
        ),
        gate_reasons=("unknown_or_ambiguous",),
        occurrence=occurrence,
    )


# --- Turn-text question extraction ---------------------------------------
#
# Detection here is deliberately deterministic and conservative: a line only
# counts as a question when a question mark sits in a natural-language shape
# or the line declares a human blocker in words. Code punctuation (ternary
# operators, optional-type markers, URL query strings, regex fragments) never
# qualifies.

# A question-marked line counts when it opens with an interrogative word or
# ends with the question mark (quotes and brackets may follow it). ``?`` in
# the middle of a line that does neither — ``ok ? a : b`` — is code-shaped.
_QUESTION_OPENER_RE = re.compile(
    r"^(?:do|does|did|is|are|was|were|am|can|could|should|would|will|won|may|might|must|shall|"
    r"what|which|who|whom|whose|when|where|why|how|have|has|had)\b",
    re.IGNORECASE,
)
_QUESTION_TAIL_RE = re.compile(r"\?+[\"'”’)\]}>*`]*\s*$")
_LEADING_MARKUP_RE = re.compile(r"^(?:#{1,6}\s+|>{1,2}\s*|[-*•+]\s+|\d+[.)]\s+)+")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`?")
_URL_RE = re.compile(r"https?://\S+")

# Declarative blocker phrasing — questions that never end in ``?`` but still
# ask the lane to wait for a human. Each pattern requires a waiting/blocked
# verb AND a human or authority word so status prose cannot false-positive.
_DECLARATIVE_BLOCKER_RES: tuple[re.Pattern[str], ...] = (
    # "waiting for your confirmation/answer/approval/decision/…"
    re.compile(
        r"\b(?:waiting|awaiting)\s+(?:for|on)\b[^.!?\n]{0,80}"
        r"\b(?:your|you|the\s+operator|an?\s+(?:operator|human)|a\s+human)\b[^.!?\n]{0,60}"
        r"\b(?:answer|repl(?:y|ies)|response|confir\w*|approv\w*|decision|input|sign[\s-]?off|go[\s-]?ahead|green\s*light|ruling|guidance|direction)s?\b",
        re.IGNORECASE,
    ),
    # "I will wait / I'll hold off … until/for you/the operator confirms"
    re.compile(
        r"\b(?:i|we)\s*(?:'ll|will|shall|must|need\s+to|have\s+to|'m\s+going\s+to|am\s+going\s+to)\s+"
        r"(?:wait|hold|pause|stop|stand\s+by|hold\s+off|proceed\s+only|continue\s+only)\b[^.!?\n]{0,60}"
        r"\b(?:until|for|pending|after|before|without)\b[^.!?\n]{0,60}"
        r"\b(?:your|you|the\s+operator|operator|confir\w*|approv\w*|decision|answer|input|sign[\s-]?off|go[\s-]?ahead)\b",
        re.IGNORECASE,
    ),
    # "I need your/the operator's/an operator's decision/approval/input"
    re.compile(
        r"\b(?:i|we)\s+(?:need|require)\b[^.!?\n]{0,60}"
        r"\b(?:your|the\s+operator'?s?|an?\s+operator'?s?)\b[^.!?\n]{0,40}"
        r"\b(?:decision|answer|confir\w*|approv\w*|sign[\s-]?off|input|guidance|ruling|direction)s?\b",
        re.IGNORECASE,
    ),
    # "please confirm/advise/decide/choose/approve" and "let me know which/what/if/…"
    re.compile(
        r"\bplease\s+(?:confirm|advise|decide|choose|pick|select|specify|indicate|approve)\b"
        r"|\blet\s+me\s+know\b[^.!?\n]{0,60}\b(?:which|what|whether|how|when|if|approv|confirm|decid|your\s+preference)\b",
        re.IGNORECASE,
    ),
    # "blocked pending your decision" / "cannot proceed without your approval"
    re.compile(
        r"\b(?:blocked|held|paus(?:ed|ing)|stopp?(?:ed|ing)|cannot\s+proceed|can\s+not\s+proceed|"
        r"won'?t\s+proceed|will\s+not\s+proceed|unable\s+to\s+proceed)\b[^.!?\n]{0,60}"
        r"\b(?:until|pending|without|waiting\s+for|awaiting|before)\b[^.!?\n]{0,60}"
        r"\b(?:your|the\s+operator'?s?|an?\s+operator'?s?|operator)\b[^.!?\n]{0,40}"
        r"\b(?:decision|answer|confir\w*|approv\w*|sign[\s-]?off|input|guidance|ruling|direction|word)s?\b",
        re.IGNORECASE,
    ),
)


def _strip_fenced_code(text: str) -> str:
    """Drop fenced code blocks; an unclosed trailing fence is dropped too."""
    parts = text.split("```")
    return "\n".join(part for index, part in enumerate(parts) if index % 2 == 0)


def extract_questions(text: str) -> tuple[str, ...]:
    """Return the lane's verbatim open questions from one response text.

    Fenced code blocks, inline code spans, and URLs are stripped first so
    code punctuation cannot masquerade as a question. A remaining line
    qualifies when a question mark sits in a natural-language shape
    (interrogative opener or question mark at the line's end) or when the
    line declares a human blocker in words ("waiting for your confirmation",
    "please confirm", "I need your decision"). Each returned item is the
    stripped line, deduplicated case-insensitively in first-seen order.
    """
    questions: list[str] = []
    seen: set[str] = set()
    for raw_line in _strip_fenced_code(text).splitlines():
        line = _URL_RE.sub(" ", _INLINE_CODE_RE.sub(" ", raw_line))
        line = _LEADING_MARKUP_RE.sub("", line).strip().strip("\"'“”‘’")
        if not line or not re.search(r"[a-zA-Z]", line):
            continue
        if not any(pattern.search(line) for pattern in _DECLARATIVE_BLOCKER_RES):
            if "?" not in line:
                continue
            if _QUESTION_OPENER_RE.match(line) is None and _QUESTION_TAIL_RE.search(line) is None:
                continue
        normalized = " ".join(line.casefold().split())
        if normalized in seen:
            continue
        seen.add(normalized)
        questions.append(line)
    return tuple(questions)


answer_question = handle_question

__all__ = [
    "GateReason",
    "QuestionDisposition",
    "QuestionHandlerResult",
    "QuestionKind",
    "QuestionSource",
    "answer_question",
    "extract_questions",
    "handle_question",
]

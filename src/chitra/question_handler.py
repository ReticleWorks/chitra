"""Bounded, deterministic answers to routine questions about one goal.

The handler is intentionally smaller than a conversational agent.  It can
answer only facts that are present in the enrolled goal contract.  Every
other question produces an operator-gated result, which a monitor may later
place on its queue without treating the result as permission to act.

There is no model call and there is no inferred reviewer or approval source in
this module.  The ``request_id`` binds the result to the exact question and
the frozen goal digest, so a stale answer cannot silently follow a changed
goal.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chitra.goals import GoalRecord, done_when_with_delta
from chitra.supervision import goal_digest

QuestionDisposition = Literal["answered", "operator_required"]
QuestionKind = Literal["next", "scope", "done_when", "small_delta", "unknown"]
QuestionSource = Literal["frozen_goal", "operator_required"]
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
    """A queue-safe answer or an explicit operator-gated question."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    session_ref: str = Field(min_length=1)
    goal_version: int = Field(ge=1)
    goal_digest: str = Field(min_length=1)
    question: str = Field(min_length=1)
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
    r"\b(?:security\s+boundary|auth(?:entication|orization|n|z)?|permissions?|access\s+control|"
    r"identity\s+(?:provider|boundary)|privilege(?:s)?|sandbox|firewall|production\s+access|"
    r"root\s+access|public\s+exposure|encrypt(?:ion|ed)?|signing\s+key)\b",
    re.I,
)
_DEPENDENCY_RE = re.compile(r"\b(?:new\s+)?dependenc(?:y|ies)|\b(?:install|add)\s+(?:a\s+)?(?:package|library)\b", re.I)
_SCHEMA_RE = re.compile(r"\b(?:new\s+)?schema|\bschema\s+(?:change|migration)|\bmigration\b", re.I)
_HOOK_RE = re.compile(r"\b(?:new\s+)?hook(?:s)?|\b(?:plugin|integration|endpoint)\b", re.I)
_STRATEGIC_SCOPE_RE = re.compile(
    r"\b(?:change|expand|narrow|redirect|revise|redefine|replace|pivot|add|remove)\b[^?\n]{0,80}\b(?:scope|goal|strategy|objective)\b|"
    r"\b(?:scope|goal|strategy|objective)\b[^?\n]{0,80}\b(?:change|expand|narrow|redirect|revise|redefine|replace|pivot)\b",
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


def _gate_reasons(question: str, *, scope_query: bool) -> tuple[GateReason, ...]:
    reasons: list[GateReason] = []
    checks: tuple[tuple[GateReason, re.Pattern[str]], ...] = (
        ("credentials", _CREDENTIAL_RE),
        ("spend", _SPEND_RE),
        ("irreversible", _IRREVERSIBLE_RE),
        ("security_boundary", _SECURITY_RE),
        ("new_dependency", _DEPENDENCY_RE),
        ("new_schema", _SCHEMA_RE),
        ("new_hook", _HOOK_RE),
    )
    for reason, pattern in checks:
        if pattern.search(question):
            reasons.append(reason)
    if not scope_query and _STRATEGIC_SCOPE_RE.search(question):
        reasons.append("strategic_scope_change")
    return tuple(reasons)


def _request_id(question: str, digest: str) -> str:
    payload = json.dumps([digest, question], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


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
) -> QuestionHandlerResult:
    digest = goal_digest(goal)
    return QuestionHandlerResult(
        request_id=_request_id(question, digest),
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest=digest,
        question=question,
        kind=kind,
        disposition=disposition,
        source=source,
        answer=answer,
        reason=reason,
        gate_reasons=gate_reasons,
    )


def handle_question(goal: GoalRecord, question: str) -> QuestionHandlerResult:
    """Answer a narrow frozen-goal question or return an operator gate.

    The function never guesses what an absent scope item means, never uses
    mutable tactical state as authority, and never returns an action approval.
    """
    if not isinstance(question, str) or not question.strip():
        return _result(
            goal,
            "<empty question>",
            kind="unknown",
            disposition="operator_required",
            source="operator_required",
            answer=None,
            reason="The question is empty or not text.",
            gate_reasons=("unknown_or_ambiguous",),
        )
    text = question.strip()
    scope_match = _SCOPE_RE.fullmatch(text) or _SCOPE_LISTED_RE.fullmatch(text)
    small_delta_match = _SMALL_DELTA_RE.fullmatch(text)
    scope_query = scope_match is not None or small_delta_match is not None
    reasons = _gate_reasons(text, scope_query=scope_query)
    if reasons:
        return _result(
            goal,
            text,
            kind="scope" if scope_query else "unknown",
            disposition="operator_required",
            source="operator_required",
            answer=None,
            reason="This question requests an operator-controlled change or authority.",
            gate_reasons=reasons,
        )

    if scope_match is not None:
        if _invalid_contract(goal, needs_scope=True):
            return _result(
                goal,
                text,
                kind="scope",
                disposition="operator_required",
                source="operator_required",
                answer=None,
                reason="The frozen goal does not contain a valid scope to inspect.",
                gate_reasons=("invalid_frozen_goal",),
            )
        item = scope_match.group("item")
        explicit = _scope_match(goal.scope, item)
        if explicit is None:
            return _result(
                goal,
                text,
                kind="scope",
                disposition="operator_required",
                source="operator_required",
                answer=None,
                reason="The frozen scope does not settle that exact item.",
                gate_reasons=("unknown_or_ambiguous",),
            )
        return _result(
            goal,
            text,
            kind="scope",
            disposition="answered",
            source="frozen_goal",
            answer=f"{item.strip()} is {'in' if explicit else 'out of'} the frozen scope.",
            reason="The item is explicitly settled by the frozen scope.",
        )

    if small_delta_match is not None:
        if _invalid_contract(goal, needs_scope=True):
            return _result(
                goal,
                text,
                kind="small_delta",
                disposition="operator_required",
                source="operator_required",
                answer=None,
                reason="The frozen goal does not contain a valid scope for a bounded design change.",
                gate_reasons=("invalid_frozen_goal",),
            )
        item = small_delta_match.group("item")
        explicit = _scope_match(goal.scope, item)
        if explicit is None:
            return _result(
                goal,
                text,
                kind="small_delta",
                disposition="operator_required",
                source="operator_required",
                answer=None,
                reason="The frozen scope does not settle that exact design surface.",
                gate_reasons=("unknown_or_ambiguous",),
            )
        if not explicit:
            answer = (
                f"Do not change {item.strip()}; it is explicitly out of the frozen scope. "
                f"Continue the frozen goal and prove completion with: {done_when_with_delta(goal)}"
            )
        else:
            answer = (
                f"A small reversible change to {item.strip()} is within the frozen scope. "
                "Keep the change bounded, avoid new dependencies, schemas, hooks, and authorization boundaries, "
                f"then verify it against: {done_when_with_delta(goal)}"
            )
        return _result(
            goal,
            text,
            kind="small_delta",
            disposition="answered",
            source="frozen_goal",
            answer=answer,
            reason="The exact design surface and completion proof are settled by the frozen contract.",
        )

    if _DONE_RE.search(text):
        if _invalid_contract(goal):
            return _result(
                goal,
                text,
                kind="done_when",
                disposition="operator_required",
                source="operator_required",
                answer=None,
                reason="The frozen goal does not contain a valid completion condition.",
                gate_reasons=("invalid_frozen_goal",),
            )
        return _result(
            goal,
            text,
            kind="done_when",
            disposition="answered",
            source="frozen_goal",
            answer=f"The completion condition is: {done_when_with_delta(goal)}",
            reason="The completion condition is copied from the frozen goal.",
        )

    if _NEXT_RE.search(text):
        if _invalid_contract(goal):
            return _result(
                goal,
                text,
                kind="next",
                disposition="operator_required",
                source="operator_required",
                answer=None,
                reason="The frozen goal is not complete enough to determine the next bounded direction.",
                gate_reasons=("invalid_frozen_goal",),
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
        )

    return _result(
        goal,
        text,
        kind="unknown",
        disposition="operator_required",
        source="operator_required",
        answer=None,
        reason="The frozen goal does not deterministically settle this question.",
        gate_reasons=("unknown_or_ambiguous",),
    )


answer_question = handle_question

__all__ = [
    "GateReason",
    "QuestionDisposition",
    "QuestionHandlerResult",
    "QuestionKind",
    "QuestionSource",
    "answer_question",
    "handle_question",
]

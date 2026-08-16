"""The short interview from the monitor doctrine, made executable.

The doctrine has always required a short interview whenever a goal record
fails its specification check: answer from primary sources first, ask only what
those sources do not settle, repair the record, and check again. Only the check
itself was ever code. This module supplies the rest, and it supplies it in the
presumptive form the operator asked for.

On a failing check this module reads the record's own primary source, derives
the values that source supports, marks them as presumed, and repairs the record
so the work can start immediately. Each presumption is recorded as a standing
invitation to correct it. A question the source cannot answer is returned as a
claim for the adjudication service, and only a question about physical
presence, spend, or a change of the agreed scope ever reaches a person.

This module derives values only from the primary source named in the record. It
never invents one, and a field it cannot derive is reported unanswered rather
than filled with something plausible.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog

from chitra.goals import (
    PRESUMED_ASK_PREFIX,
    GoalNotFoundError,
    GoalRecord,
    add_ask,
    check_specification,
    get_goal,
    redirect_goal,
)

logger = structlog.get_logger(__name__)

StrategicField = Literal["intent", "done_when", "scope", "source"]
SourceKind = Literal["task-file", "branch", "transcript-first-msg"]

@dataclass(frozen=True, slots=True)
class InterviewQuestion:
    """One doctrine question and the record field it repairs."""

    key: str
    field: StrategicField
    question: str
    derivable: bool


#: The four doctrine questions, each tied to the record field it repairs.
#: ``derivable`` marks the fields this module is allowed to presume. A short
#: completion condition is deliberately not derivable: it is the contract the
#: work is later judged against, so presuming one would move the finish line
#: rather than fill in a blank.
INTERVIEW_QUESTIONS: tuple[InterviewQuestion, ...] = (
    InterviewQuestion(
        key="intent",
        field="intent",
        question="Original intent, in your words: what outcome, and for whom?",
        derivable=True,
    ),
    InterviewQuestion(
        key="done_when",
        field="done_when",
        question="What concrete artifact or check closes this?",
        derivable=False,
    ),
    InterviewQuestion(
        key="scope",
        field="scope",
        question="What is explicitly out of scope?",
        derivable=True,
    ),
    InterviewQuestion(
        key="constraints",
        field="scope",
        question="What material constraints apply here, such as spend, tools, order, or approvals?",
        derivable=True,
    ),
)

_QUESTION_BY_FIELD: dict[StrategicField, InterviewQuestion] = {
    "intent": INTERVIEW_QUESTIONS[0],
    "done_when": INTERVIEW_QUESTIONS[1],
    "scope": INTERVIEW_QUESTIONS[2],
    "source": INTERVIEW_QUESTIONS[0],
}

SOURCE_PREFIXES: tuple[SourceKind, ...] = ("task-file", "branch", "transcript-first-msg")

INTENT_MIN_WORDS = 8
INTENT_MAX_WORDS = 60
SCOPE_MIN_WORDS = 4
SCOPE_MAX_WORDS = 60
PRIMARY_SOURCE_MAX_BYTES = 256 * 1024

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_DONE_HEADING_RE = re.compile(r"deliverable|definition of done|done[-\s]?when|acceptance|evidence", re.IGNORECASE)
_SCOPE_HEADING_RE = re.compile(r"scope|constraint|out of scope|non[-\s]?goal|boundar", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PrimarySource:
    """The one recorded origin a presumption may be derived from."""

    kind: SourceKind
    reference: str
    """The bare locator, such as a file path or a branch name."""
    locator: str
    """The same origin named as prose, for the correction invitation."""
    text: str


@dataclass(frozen=True, slots=True)
class Presumption:
    """One strategic value derived from a primary source and presumed."""

    field: StrategicField
    value: str
    derived_from: str

    def as_ask(self) -> str:
        """Render the standing correction invitation recorded on the goal."""
        return f'{PRESUMED_ASK_PREFIX} {self.field} was taken from {self.derived_from}: "{self.value}". Correct it if that is wrong.'


@dataclass(frozen=True, slots=True)
class UnansweredQuestion:
    """One doctrine question the primary source does not settle."""

    question: InterviewQuestion
    reason: str


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    """What one presumptive repair derived, wrote, and could not settle."""

    session_ref: str
    attempted: bool
    presumptions: tuple[Presumption, ...]
    unanswered: tuple[UnansweredQuestion, ...]
    remaining_issues: tuple[str, ...]
    record: GoalRecord | None

    @property
    def passes_check(self) -> bool:
        """Whether the record satisfies the specification check now."""
        return not self.remaining_issues


def _words(text: str) -> list[str]:
    return text.split()


def _clamp_words(text: str, limit: int) -> str:
    words = _words(text)
    return text.strip() if len(words) <= limit else " ".join(words[:limit]).rstrip(",;:") + " ..."


def read_primary_source(record: GoalRecord, *, max_bytes: int = PRIMARY_SOURCE_MAX_BYTES) -> PrimarySource | None:
    """Read the primary source the record names, if it names a readable one.

    The record's ``source`` field carries the locator. A task file is read as
    text, a first transcript message is read out of that transcript, and a
    branch name is its own only text.
    """
    raw = record.source.strip()
    if not raw:
        return None
    kind: SourceKind | None = next((prefix for prefix in SOURCE_PREFIXES if raw.startswith(prefix)), None)
    # Records in the wild separate the prefix from its locator with either a
    # space or a colon, so both are stripped before the locator is read.
    locator = raw[len(kind) :].lstrip(": \t") if kind is not None else raw
    if kind is None:
        candidate = Path(locator)
        if candidate.is_file():
            kind = "task-file"
        else:
            return None
    if kind == "branch":
        return PrimarySource(
            kind="branch",
            reference=locator,
            locator=f"the branch named {locator}",
            text=locator.replace("-", " ").replace("/", " "),
        )
    path = Path(locator)
    if not path.is_file():
        return None
    try:
        payload = path.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except OSError as exc:
        logger.warning("interview_primary_source_unreadable", path=str(path), error=str(exc))
        return None
    if kind == "transcript-first-msg":
        text = _first_user_message(payload)
        if not text:
            return None
        return PrimarySource(
            kind="transcript-first-msg",
            reference=str(path),
            locator=f"the first message in {path}",
            text=text,
        )
    return PrimarySource(kind="task-file", reference=str(path), locator=f"the task file {path}", text=payload)


def _first_user_message(payload: str) -> str:
    """Return the first user message recorded in a transcript, if there is one."""
    for line in payload.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
            joined = "\n".join(part for part in parts if isinstance(part, str) and part.strip())
            if joined.strip():
                return joined.strip()
    return ""


@dataclass(frozen=True, slots=True)
class _Section:
    title: str
    body: str


def _sections(text: str) -> list[_Section]:
    """Split a source document into its headed sections, in order."""
    sections: list[_Section] = []
    title = ""
    body: list[str] = []
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading is None:
            body.append(line)
            continue
        if title or any(item.strip() for item in body):
            sections.append(_Section(title=title, body="\n".join(body)))
        title = heading.group("title")
        body = []
    if title or any(item.strip() for item in body):
        sections.append(_Section(title=title, body="\n".join(body)))
    return sections


def _lead_prose(text: str, *, min_words: int, max_words: int) -> str:
    """Return the opening prose of a document, without its headings or bullets."""
    collected: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _HEADING_RE.match(line):
            if collected:
                break
            continue
        collected.append(_BULLET_RE.sub("", stripped))
        if len(_words(" ".join(collected))) >= min_words:
            break
    joined = " ".join(collected).strip()
    if len(_words(joined)) < min_words:
        return ""
    sentences = _SENTENCE_SPLIT_RE.split(joined)
    kept: list[str] = []
    for sentence in sentences:
        kept.append(sentence)
        if len(_words(" ".join(kept))) >= min_words:
            break
    return _clamp_words(" ".join(kept), max_words)


def _section_prose(text: str, pattern: re.Pattern[str], *, min_words: int, max_words: int) -> tuple[str, str]:
    """Return prose from the first matching section, and that section's title."""
    for section in _sections(text):
        if not pattern.search(section.title):
            continue
        lines = [_BULLET_RE.sub("", line.strip()) for line in section.body.splitlines() if line.strip()]
        joined = " ".join(lines).strip()
        if len(_words(joined)) >= min_words:
            return _clamp_words(joined, max_words), section.title
    return "", ""


def derive_presumptions(record: GoalRecord, source: PrimarySource) -> tuple[tuple[Presumption, ...], tuple[UnansweredQuestion, ...]]:
    """Derive every strategic value the primary source supports.

    Only the fields the record actually fails on are touched, and only the
    fields the doctrine's repair gate names as derivable. Anything the source
    does not support comes back unanswered.
    """
    failing = _failing_fields(record)
    presumptions: list[Presumption] = []
    unanswered: list[UnansweredQuestion] = []

    if "source" in failing:
        presumptions.append(
            Presumption(field="source", value=f"{source.kind} {source.reference}", derived_from=source.locator)
        )

    if "intent" in failing:
        value = _lead_prose(source.text, min_words=INTENT_MIN_WORDS, max_words=INTENT_MAX_WORDS)
        if value:
            presumptions.append(Presumption(field="intent", value=value, derived_from=f"the opening of {source.locator}"))
        else:
            unanswered.append(
                UnansweredQuestion(
                    question=_QUESTION_BY_FIELD["intent"],
                    reason=f"{source.locator} carries no opening statement of the intended outcome.",
                )
            )

    if "scope" in failing:
        value, title = _section_prose(source.text, _SCOPE_HEADING_RE, min_words=SCOPE_MIN_WORDS, max_words=SCOPE_MAX_WORDS)
        if value:
            presumptions.append(
                Presumption(field="scope", value=value, derived_from=f'the "{title}" section of {source.locator}')
            )
        else:
            unanswered.append(
                UnansweredQuestion(
                    question=_QUESTION_BY_FIELD["scope"],
                    reason=f"{source.locator} carries no section stating what is in and out of the work.",
                )
            )

    if "done_when" in failing:
        found, title = _section_prose(source.text, _DONE_HEADING_RE, min_words=5, max_words=SCOPE_MAX_WORDS)
        reason = (
            f'{source.locator} states a completion condition under "{title}", but a completion condition is the '
            "contract this work is judged against and is not presumed."
            if found
            else f"{source.locator} states no completion condition."
        )
        unanswered.append(UnansweredQuestion(question=_QUESTION_BY_FIELD["done_when"], reason=reason))

    return tuple(presumptions), tuple(unanswered)


def _failing_fields(record: GoalRecord) -> set[StrategicField]:
    """Return the strategic fields the specification check reports on."""
    failing: set[StrategicField] = set()
    for issue in check_specification(record):
        if issue.startswith("intent"):
            failing.add("intent")
        elif issue.startswith("scope"):
            failing.add("scope")
        elif issue.startswith("done_when"):
            failing.add("done_when")
        elif issue.startswith("source"):
            failing.add("source")
    return failing


def repair_reason(presumptions: Sequence[Presumption], *, now: datetime | None = None) -> str:
    """Return the recorded reason a repair changed strategic values."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    fields = ", ".join(sorted({presumption.field for presumption in presumptions}))
    return (
        f"Repair under the monitor doctrine short interview at {stamp}: {fields} derived from the recorded "
        "primary source and marked presumed, so the work could start immediately."
    )


def presumptive_repair(
    root: Path | None,
    session_ref: str,
    *,
    now: datetime | None = None,
) -> RepairOutcome:
    """Repair one failing record from its primary source and mark what was presumed.

    Returns without changing anything when the record already passes, when it
    does not exist, or when its primary source cannot be read. In the last case
    every failing question comes back unanswered, which is the honest result:
    nothing was learned, so nothing may be presumed.
    """
    record = get_goal(root, session_ref)
    if record is None:
        raise GoalNotFoundError(session_ref)
    issues = tuple(check_specification(record))
    if not issues:
        return RepairOutcome(
            session_ref=session_ref,
            attempted=False,
            presumptions=(),
            unanswered=(),
            remaining_issues=(),
            record=record,
        )

    source = read_primary_source(record)
    if source is None:
        unanswered = tuple(
            UnansweredQuestion(
                question=_QUESTION_BY_FIELD[field],
                reason=f"the record names no readable primary source, so {field} cannot be derived.",
            )
            for field in sorted(_failing_fields(record))
        )
        return RepairOutcome(
            session_ref=session_ref,
            attempted=True,
            presumptions=(),
            unanswered=unanswered,
            remaining_issues=issues,
            record=record,
        )

    presumptions, unanswered = derive_presumptions(record, source)
    if not presumptions:
        return RepairOutcome(
            session_ref=session_ref,
            attempted=True,
            presumptions=(),
            unanswered=unanswered,
            remaining_issues=issues,
            record=record,
        )

    values = {presumption.field: presumption.value for presumption in presumptions}
    updated = redirect_goal(
        root,
        session_ref,
        reason=repair_reason(presumptions, now=now),
        intent=values.get("intent"),
        scope=values.get("scope"),
        source=values.get("source"),
    )
    for presumption in presumptions:
        updated = add_ask(root, session_ref, presumption.as_ask())
    logger.info(
        "goal_presumptively_repaired",
        session_ref=session_ref,
        presumed=[presumption.field for presumption in presumptions],
        unanswered=[item.question.key for item in unanswered],
    )
    return RepairOutcome(
        session_ref=session_ref,
        attempted=True,
        presumptions=presumptions,
        unanswered=unanswered,
        remaining_issues=tuple(check_specification(updated)),
        record=updated,
    )


def presumed_asks(record: GoalRecord) -> tuple[str, ...]:
    """Return the record's standing presumption-correction invitations."""
    return tuple(ask for ask in record.open_asks if ask.startswith(PRESUMED_ASK_PREFIX))

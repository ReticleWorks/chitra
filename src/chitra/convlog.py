"""Validate, render, and record operator-brief conversation threads.

The fleet monitor's messages to the operator today mostly echo a lane's raw
text. This module is the deterministic half of an interpretive translation
layer: the CALLER (the monitor harness LLM) composes an OperatorBrief — session
context, process stage, the pending decision, a recommendation with research
already folded in — and this module validates it, renders it in a fixed BLUF
(bottom-line-up-front) layout, and records the full four-state exchange (raw
session message → operator brief → operator ruling → lane directive) in an
append-only conversation log. No LLM calls here: chitra validates, renders,
and logs; it never composes.

No LLM calls in this module's own code path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from chitra._fsio import parse_iso8601
from chitra.plain_english import plain_english_issues, require_plain_english
from chitra.state_paths import default_convlog_path

logger = structlog.get_logger(__name__)

SCHEMA: Literal["chitra.convlog.v2"] = "chitra.convlog.v2"
type EntryKind = Literal["session_msg", "operator_brief", "operator_ruling", "lane_directive", "conversation_resolution"]
type ConversationResolution = Literal["moot", "superseded"]
type BriefCategory = Literal["decision", "incident", "milestone", "fyi"]
type RecommendationBasis = Literal["research", "operator-preference"]
_BARE_CODENAME = re.compile(r"(?:[A-Za-z]*-?\d+|\d+-\d+)", re.IGNORECASE)
type ExhaustionReason = Literal["credential", "irreversible_consent", "operator_decision", "attempts_exhausted"]
_ATTEMPT_SEPARATOR = "->"
_OBSERVED_RESULT = f"an observed result after the '{_ATTEMPT_SEPARATOR}' separator"
_ATTEMPT_MIN_LENGTH = 20
_ATTEMPT_ACTION_MIN_LENGTH = 8
_ATTEMPT_RESULT_MIN_LENGTH = 5
_BLOCKER_MIN_LENGTH = 20
_RESULT_SEGMENT_ERROR = (
    f"each attempt must split on its last '{_ATTEMPT_SEPARATOR}' separator into a non-blank action part "
    f"of at least {_ATTEMPT_ACTION_MIN_LENGTH} characters and a non-blank observed-result part of at least "
    f"{_ATTEMPT_RESULT_MIN_LENGTH} characters (both measured after stripping surrounding whitespace)"
)
_MISSING_EXHAUSTION_ERROR = (
    "a decision may go to the operator only with an exhaustion record: "
    "list >= 2 distinct attempts with observed results (reason attempts_exhausted); "
    "only credential, irreversible_consent, or operator_decision excuse going to the operator with fewer"
)
_SMUGGLED_ASK_MARKERS: tuple[str, ...] = (
    "operator must",
    "operator needs to",
    "operator should",
    "you must",
    "you need to",
    "you should run",
    "please run",
    "please install",
    "can you ",
    "could you ",
)


class BriefValidationError(ValueError):
    """Raised when caller-supplied operator-brief data is invalid."""


class ConversationNotFoundError(KeyError):
    """Raised when an operation requires a conversation thread that is absent."""


class BriefOption(BaseModel):
    """One numbered answer choice supplied by the monitor harness."""

    label: str = Field(min_length=1)
    consequence: str = Field(min_length=1)

    @field_validator("label", "consequence")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @field_validator("consequence")
    @classmethod
    def _consequence_is_plain_english(cls, value: str) -> str:
        return require_plain_english(value, field="option consequence")


class ExhaustionRecord(BaseModel):
    """Proof that agent-resolvable work was exhausted before a brief asked the operator.

    The exhaustion gate enforces STRUCTURE, not vocabulary: an explicit reason,
    an explicit residual blocker, and attempts that each record an action tried
    with its observed result. Structure makes every record a falsifiable claim
    that an auditor can check against the session transcript. There are no
    keyword whitelists anywhere in this gate: vocabulary tests reject truthful
    records while passing magic-word stuffing. Fabrication is deterred by
    post-hoc audit (reviewer codes plus random deep audits), not by string
    matching.
    """

    reason: ExhaustionReason
    attempts: list[str] = Field(default_factory=list)
    residual_blocker: str


class OperatorBrief(BaseModel):
    """A caller-composed, deterministic-to-render operator brief."""

    session_ref: str = Field(min_length=1)
    program: str = Field(min_length=1)
    subject: str = ""
    progress: str = ""
    stage: str = Field(min_length=1, max_length=140)
    category: BriefCategory
    decision: str | None = None
    recommendation: str = ""
    recommendation_basis: RecommendationBasis = "research"
    options: list[BriefOption] = Field(default_factory=list)
    exhaustion: ExhaustionRecord | None = None
    source_quote: list[str] = Field(min_length=1, max_length=4)
    source_ref: str = Field(min_length=1)

    @field_validator("session_ref", "source_ref")
    @classmethod
    def _required_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @field_validator("program")
    @classmethod
    def _program_is_plain_language(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("program must be non-empty")
        if _BARE_CODENAME.fullmatch(stripped) or (":" in stripped and " " not in stripped):
            raise ValueError("use the plain-language program name, optionally with the codename in parentheses")
        return value

    @field_validator("stage")
    @classmethod
    def _stage_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("stage must be non-empty")
        return require_plain_english(value, field="stage")

    @field_validator("decision")
    @classmethod
    def _decision_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("decision must be non-empty when provided")
        return require_plain_english(value, field="decision") if value is not None else None

    @field_validator("recommendation")
    @classmethod
    def _recommendation_is_plain_english(cls, value: str) -> str:
        return require_plain_english(value, field="recommendation") if value.strip() else value

    @field_validator("source_quote")
    @classmethod
    def _source_quotes_are_verbatim_anchors(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.strip():
                raise ValueError("source quotes must be non-empty")
            if len(value) > 400:
                raise ValueError("source quotes must be at most 400 characters")
        return values

    @model_validator(mode="after")
    def _decision_has_research_or_explicit_preference(self) -> OperatorBrief:
        if self.category == "decision" and self.decision is None:
            raise ValueError("decision must be non-empty when category is decision")
        if self.decision is not None and not self.recommendation.strip() and self.recommendation_basis != "operator-preference":
            raise ValueError(
                "the monitor does the research first and folds the result in — "
                'a decision brief may not punt with "would benefit from more research"'
            )
        return self


class ConversationEntry(BaseModel):
    """One append-only record in the four-rung operator conversation log."""

    schema_: Literal["chitra.convlog.v1", "chitra.convlog.v2"] = Field(default=SCHEMA, alias="schema")
    thread_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    kind: EntryKind
    at: str = Field(min_length=1)
    session_ref: str = Field(min_length=1)
    payload: dict[str, Any]


class _SessionMessagePayload(BaseModel):
    text: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)


class _OperatorBriefPayload(BaseModel):
    brief: OperatorBrief
    rendered: str


class _OperatorRulingPayload(BaseModel):
    text: str = Field(min_length=1)
    via: Literal["chat", "in-pane", "slack"]


class _LaneDirectivePayload(BaseModel):
    text: str = Field(min_length=1)
    order_id: str | None


class _ConversationResolutionPayload(BaseModel):
    state: ConversationResolution
    basis: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    authority: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ConversationThread:
    """The current, derived state of one append-only conversation thread."""

    thread_id: str
    session_ref: str
    opened_at: str
    latest_brief: OperatorBrief
    latest_brief_at: str
    pending: bool
    state: Literal["pending", "ruled", "moot", "superseded"]
    entries: tuple[ConversationEntry, ...]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_at(value: str) -> datetime:
    return parse_iso8601(
        value,
        timezone_message="conversation entry timestamp must be timezone-aware",
        require_timezone=True,
    )


def _collapsed_key(value: str) -> str:
    """Normalize an attempt so case, padding, and spacing cannot fake a new try."""
    return " ".join(value.casefold().strip().split())


def _exhaustion_problems(brief: OperatorBrief) -> list[str]:
    """Return every structural defect in the brief's exhaustion record."""
    record = brief.exhaustion
    if record is None:
        return []
    problems: list[str] = []
    if "\n" in record.residual_blocker or "\r" in record.residual_blocker:
        problems.append("residual_blocker must be one line: '\\n' and '\\r' are rejected")
    seen: dict[str, int] = {}
    for index, attempt in enumerate(record.attempts, start=1):
        key = _collapsed_key(attempt)
        if not key:
            problems.append(f"attempt {index} must be non-empty")
        elif key in seen:
            problems.append(
                f"attempt {index} repeats attempt {seen[key]} once compared without case or extra whitespace; "
                "attempts must be distinct records of distinct tries"
            )
        else:
            seen[key] = index
        if "\n" in attempt or "\r" in attempt:
            problems.append(f"attempt {index} must be one line: '\\n' and '\\r' are rejected")
        stripped = attempt.strip()
        if len(stripped) < _ATTEMPT_MIN_LENGTH:
            problems.append(
                f"attempt {index} must be at least {_ATTEMPT_MIN_LENGTH} characters and name the action tried and {_OBSERVED_RESULT}"
            )
        action, _, result = attempt.rpartition(_ATTEMPT_SEPARATOR)
        if len(action.strip()) < _ATTEMPT_ACTION_MIN_LENGTH or len(result.strip()) < _ATTEMPT_RESULT_MIN_LENGTH:
            problems.append(f"attempt {index}: {_RESULT_SEGMENT_ERROR}")
        problems.extend(plain_english_issues(attempt, field=f"attempt {index}"))
    stripped_blocker = record.residual_blocker.strip()
    if record.reason == "attempts_exhausted":
        if len(record.attempts) < 2:
            problems.append(
                f"reason attempts_exhausted requires at least 2 distinct attempts, each naming the action tried and {_OBSERVED_RESULT}"
            )
    else:
        if len(stripped_blocker) < _BLOCKER_MIN_LENGTH:
            problems.append(
                f"reason {record.reason} requires a residual_blocker of at least {_BLOCKER_MIN_LENGTH} characters "
                "naming what still stands after the attempts"
            )
        if record.reason == "credential" and len(record.attempts) < 1:
            problems.append("reason credential requires at least 1 attempt: record the access you tried and the refusal you observed")
        if record.reason == "operator_decision" and brief.recommendation_basis != "operator-preference":
            problems.append('reason operator_decision is valid only when recommendation_basis is "operator-preference"')
    problems.extend(plain_english_issues(record.residual_blocker, field="residual_blocker"))
    return problems


def _smuggled_ask_problems(brief: OperatorBrief) -> list[str]:
    """Reject operator-directed imperatives smuggled into decisionless briefs."""
    if brief.decision is not None:
        return []
    problems: list[str] = []
    for field_name, text in (("recommendation", brief.recommendation), ("stage", brief.stage), ("progress", brief.progress)):
        lowered = text.casefold()
        marker = next((candidate for candidate in _SMUGGLED_ASK_MARKERS if candidate in lowered), None)
        if marker is not None:
            problems.append(
                f"{field_name} hides an operator-directed ask ({marker.strip()!r}): "
                "promote the ask into decision with an exhaustion record, or remove it from the brief"
            )
    return problems


def _write_path_problems(brief: OperatorBrief) -> list[str]:
    problems: list[str] = []
    if brief.decision is not None and brief.exhaustion is None:
        problems.append(_MISSING_EXHAUSTION_ERROR)
    problems.extend(_exhaustion_problems(brief))
    problems.extend(_smuggled_ask_problems(brief))
    return problems


def validate_brief(payload: object) -> OperatorBrief:
    """Validate one caller payload on the strict write path and normalize errors for callers."""
    try:
        brief = OperatorBrief.model_validate(payload)
    except ValidationError as exc:
        raise BriefValidationError(str(exc)) from exc
    problems = _write_path_problems(brief)
    if problems:
        raise BriefValidationError("; ".join(problems))
    return brief


def read_stored_brief(payload: object) -> OperatorBrief:
    """Load one stored brief leniently so records written before a rule change stay readable.

    Stored records never re-run the write-path gates (exhaustion structure,
    smuggled asks); only the shape checks that predate their storage apply.
    """
    try:
        return OperatorBrief.model_validate(payload)
    except ValidationError as exc:
        raise BriefValidationError(str(exc)) from exc


def _validated_payload(entry: ConversationEntry) -> dict[str, Any]:
    """Validate one kind-specific envelope payload before exposing it to readers."""
    match entry.kind:
        case "session_msg":
            return _SessionMessagePayload.model_validate(entry.payload).model_dump(mode="json")
        case "operator_brief":
            return _OperatorBriefPayload.model_validate(entry.payload).model_dump(mode="json")
        case "operator_ruling":
            return _OperatorRulingPayload.model_validate(entry.payload).model_dump(mode="json")
        case "lane_directive":
            return _LaneDirectivePayload.model_validate(entry.payload).model_dump(mode="json")
        case "conversation_resolution":
            return _ConversationResolutionPayload.model_validate(entry.payload).model_dump(mode="json")


def _grounding_line(brief: OperatorBrief) -> str:
    """Render the v2 context lead-in while keeping v1 records readable."""
    line = f"This is {brief.program} ({brief.session_ref})"
    if brief.subject.strip():
        line = f"{line} working on {brief.subject}"
    if brief.progress.strip():
        line = f"{line}: {brief.progress}"
    return f"{line}."


def _exhaustion_lines(brief: OperatorBrief) -> list[str]:
    record = brief.exhaustion
    if record is None:
        return []
    lines: list[str] = []
    if record.attempts:
        lines.append("TRIED:")
        lines.extend(f"- {attempt}" for attempt in record.attempts)
    if record.reason == "attempts_exhausted":
        lines.append(record.residual_blocker)
    else:
        lines.append(f"OPERATOR-ONLY BECAUSE: {record.residual_blocker}")
    return lines


def render_brief(brief: OperatorBrief) -> str:
    """Render one brief in the fixed plain-text BLUF layout."""
    if brief.decision is not None:
        lines = [
            _grounding_line(brief),
            f"🔴 {brief.program} ({brief.session_ref}) — needs you: {brief.decision}",
            f"Stage: {brief.stage}",
        ]
        if brief.recommendation_basis == "operator-preference":
            recommendation = "Recommendation: your call — no research applies."
            if brief.recommendation:
                recommendation = f"{recommendation} {brief.recommendation}"
            lines.append(recommendation)
        else:
            lines.append(f"Recommendation: {brief.recommendation}")
        if brief.options:
            lines.append("Options (reply by number):")
            lines.extend(f"  {index}. {option.label} — {option.consequence}" for index, option in enumerate(brief.options, start=1))
    else:
        marker = {"incident": "🟧", "milestone": "✅", "fyi": "🟦"}[brief.category]
        lines = [
            _grounding_line(brief),
            f"{marker} {brief.program} ({brief.session_ref}) — {brief.category}; nothing to answer yet.",
            f"Stage: {brief.stage}",
        ]
        if brief.recommendation:
            lines.append(f"Recommendation: {brief.recommendation}")
    lines.extend(_exhaustion_lines(brief))
    lines.append("— from the session, verbatim —")
    lines.extend(f"> {quote}" for quote in brief.source_quote)
    return "\n".join(lines)


def _age_label(opened_at: str, now: datetime) -> str:
    elapsed_seconds = max(0, int((now - _parse_at(opened_at)).total_seconds()))
    if elapsed_seconds < 3600:
        return f"open {elapsed_seconds // 60}m"
    if elapsed_seconds < 86400:
        return f"open {elapsed_seconds // 3600}h"
    return f"open {elapsed_seconds // 86400}d"


def render_group(briefs_or_pending: Sequence[OperatorBrief | ConversationThread], *, now: datetime | None = None) -> str:
    """Render several briefs as one numbered operator message."""
    current = datetime.now(UTC) if now is None else now
    sections: list[str] = []
    for index, item in enumerate(briefs_or_pending, start=1):
        if isinstance(item, ConversationThread):
            heading = f"[{index}] — {_age_label(item.latest_brief_at, current)}"
            brief = item.latest_brief
        else:
            heading = f"[{index}]"
            brief = item
        body = "\n".join(f"  {line}" if line else "" for line in render_brief(brief).splitlines())
        sections.append(f"{heading}\n{body}")
    return "\n\n".join(sections)


def read_entries(convlog_path: Path | None = None) -> list[ConversationEntry]:
    """Load valid log entries, logging and skipping malformed JSONL records."""
    path = default_convlog_path() if convlog_path is None else convlog_path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    entries: list[ConversationEntry] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = ConversationEntry.model_validate_json(line)
            entry.payload = _validated_payload(entry)
        except ValidationError:
            logger.warning("convlog_malformed_line", path=str(path), line_number=line_number)
            continue
        entries.append(entry)
    return entries


def entries_for_thread(convlog_path: Path | None, thread_id: str) -> list[ConversationEntry]:
    """Return one thread's valid entries in persisted order."""
    return [entry for entry in read_entries(convlog_path) if entry.thread_id == thread_id]


def _next_seq(convlog_path: Path, thread_id: str) -> int:
    entries = entries_for_thread(convlog_path, thread_id)
    return max((entry.seq for entry in entries), default=0) + 1


def append_entry(
    convlog_path: Path,
    *,
    thread_id: str,
    kind: EntryKind,
    session_ref: str,
    payload: dict[str, Any],
    at: str | None = None,
) -> ConversationEntry:
    """Append one flushed JSONL entry, never rewriting an existing record."""
    entry = ConversationEntry(
        thread_id=thread_id,
        seq=_next_seq(convlog_path, thread_id),
        kind=kind,
        at=at or _utc_now(),
        session_ref=session_ref,
        payload=payload,
    )
    convlog_path.parent.mkdir(parents=True, exist_ok=True)
    line = entry.model_dump_json(by_alias=True) + "\n"
    with convlog_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def _require_thread(convlog_path: Path, thread_id: str) -> list[ConversationEntry]:
    entries = entries_for_thread(convlog_path, thread_id)
    if not entries:
        raise ConversationNotFoundError(thread_id)
    return entries


def append_session_message(convlog_path: Path, *, thread_id: str, session_ref: str, text: str, source_ref: str) -> ConversationEntry:
    """Record one full, verbatim upstream session message."""
    if not text:
        raise ValueError("session message text must be non-empty")
    if not source_ref.strip():
        raise ValueError("source_ref must be non-empty")
    return append_entry(
        convlog_path,
        thread_id=thread_id,
        kind="session_msg",
        session_ref=session_ref,
        payload={"text": text, "source_ref": source_ref},
    )


def append_operator_brief(convlog_path: Path, *, thread_id: str, brief: OperatorBrief) -> ConversationEntry:
    """Record a validated brief and its deterministic rendering."""
    brief = validate_brief(brief)
    _require_thread(convlog_path, thread_id)
    rendered = render_brief(brief)
    return append_entry(
        convlog_path,
        thread_id=thread_id,
        kind="operator_brief",
        session_ref=brief.session_ref,
        payload={"brief": brief.model_dump(mode="json"), "rendered": rendered},
    )


def open_thread(convlog_path: Path, *, brief: OperatorBrief, raw_text: str) -> str:
    """Open a new thread by recording its raw message before its first brief."""
    thread_id = uuid.uuid4().hex[:12]
    validate_brief(brief)
    append_session_message(convlog_path, thread_id=thread_id, session_ref=brief.session_ref, text=raw_text, source_ref=brief.source_ref)
    append_operator_brief(convlog_path, thread_id=thread_id, brief=brief)
    return thread_id


def append_ruling(convlog_path: Path, *, thread_id: str, text: str, via: Literal["chat", "in-pane", "slack"] = "chat") -> ConversationEntry:
    """Record one explicit operator ruling for an existing thread."""
    entries = _require_thread(convlog_path, thread_id)
    if not text:
        raise ValueError("operator ruling text must be non-empty")
    return append_entry(
        convlog_path,
        thread_id=thread_id,
        kind="operator_ruling",
        session_ref=entries[-1].session_ref,
        payload={"text": text, "via": via},
    )


def append_directive(convlog_path: Path, *, thread_id: str, text: str, order_id: str | None = None) -> ConversationEntry:
    """Record the exact directive sent down to the lane."""
    entries = _require_thread(convlog_path, thread_id)
    if not text:
        raise ValueError("lane directive text must be non-empty")
    return append_entry(
        convlog_path,
        thread_id=thread_id,
        kind="lane_directive",
        session_ref=entries[-1].session_ref,
        payload={"text": text, "order_id": order_id},
    )


def append_resolution(
    convlog_path: Path,
    *,
    thread_id: str,
    state: ConversationResolution,
    basis: str,
    citation: str,
    authority: str,
) -> ConversationEntry:
    """Retire a thread truthfully when events or a newer decision resolve it."""
    entries = _require_thread(convlog_path, thread_id)
    require_plain_english(basis, field="resolution basis")
    require_plain_english(authority, field="resolution authority")
    if not citation.strip():
        raise ValueError("citation must be non-empty")
    return append_entry(
        convlog_path,
        thread_id=thread_id,
        kind="conversation_resolution",
        session_ref=entries[-1].session_ref,
        payload={"state": state, "basis": basis, "citation": citation, "authority": authority},
    )


def _entry_carries_decision(entry: ConversationEntry) -> bool:
    payload_brief = entry.payload.get("brief") if isinstance(entry.payload, dict) else None
    decision = payload_brief.get("decision") if isinstance(payload_brief, dict) else None
    return isinstance(decision, str) and bool(decision.strip())


def _thread_from_entries(thread_id: str, entries: list[ConversationEntry]) -> ConversationThread | None:
    """Derive thread state from the last decision-bearing brief.

    A decisionless follow-up brief must not retire a pending ask, so the ask's
    brief stays authoritative until a newer decision-bearing brief replaces it.
    """
    brief_entries = [entry for entry in entries if entry.kind == "operator_brief"]
    if not brief_entries:
        return None
    decision_bearing = [entry for entry in brief_entries if _entry_carries_decision(entry)]
    latest = decision_bearing[-1] if decision_bearing else brief_entries[-1]
    try:
        brief = read_stored_brief(latest.payload["brief"])
    except (BriefValidationError, KeyError):
        logger.warning("convlog_invalid_brief_entry", thread_id=thread_id, seq=latest.seq)
        return None
    ruled_after = any(entry.kind == "operator_ruling" and entry.seq > latest.seq for entry in entries)
    resolutions = [entry for entry in entries if entry.kind == "conversation_resolution" and entry.seq > latest.seq]
    state: Literal["pending", "ruled", "moot", "superseded"]
    if resolutions:
        state = resolutions[-1].payload["state"]
    elif ruled_after:
        state = "ruled"
    else:
        state = "pending"
    return ConversationThread(
        thread_id=thread_id,
        session_ref=latest.session_ref,
        opened_at=entries[0].at,
        latest_brief=brief,
        latest_brief_at=latest.at,
        pending=brief.decision is not None and state == "pending",
        state=state,
        entries=tuple(entries),
    )


def list_threads(convlog_path: Path | None = None, *, session_ref: str | None = None) -> list[ConversationThread]:
    """Derive current conversation-thread state in opening order."""
    by_thread: dict[str, list[ConversationEntry]] = {}
    for entry in read_entries(convlog_path):
        by_thread.setdefault(entry.thread_id, []).append(entry)
    threads = [thread for thread_id, entries in by_thread.items() if (thread := _thread_from_entries(thread_id, entries)) is not None]
    if session_ref is not None:
        threads = [thread for thread in threads if thread.session_ref == session_ref]
    return sorted(threads, key=lambda thread: (_parse_at(thread.opened_at), thread.thread_id))


def pending_threads(convlog_path: Path | None = None) -> list[ConversationThread]:
    """Return unresolved decision threads, oldest first; silence never retires one."""
    return [thread for thread in list_threads(convlog_path) if thread.pending]


def _read_json_argument(path: str) -> object:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_raw_argument(raw: str | None, raw_file: Path | None) -> str | None:
    if raw is not None:
        return raw
    if raw_file is not None:
        return raw_file.read_text(encoding="utf-8")
    return None


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the ``chitra-convo`` command interface."""
    parser = argparse.ArgumentParser(prog="chitra-convo", description="Validate, render, and log caller-composed operator briefs.")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_convlog_path(command: argparse.ArgumentParser) -> None:
        command.add_argument("--convlog-path", type=Path, default=default_convlog_path())

    brief_command = commands.add_parser("brief", help="Validate, render, and append an operator brief.")
    add_convlog_path(brief_command)
    brief_command.add_argument("--session-ref", required=True)
    brief_command.add_argument("--json", required=True)
    raw_group = brief_command.add_mutually_exclusive_group()
    raw_group.add_argument("--raw")
    raw_group.add_argument("--raw-file", type=Path)
    brief_command.add_argument("--thread")

    rule_command = commands.add_parser("rule", help="Append an explicit operator ruling to one or more threads.")
    add_convlog_path(rule_command)
    rule_command.add_argument("--thread", action="append", required=True)
    rule_command.add_argument("--text", required=True)
    rule_command.add_argument("--via", choices=("chat", "in-pane", "slack"), default="chat")

    directive_command = commands.add_parser("directive", help="Append a directive sent down to a lane.")
    add_convlog_path(directive_command)
    directive_command.add_argument("--thread", required=True)
    directive_command.add_argument("--text", required=True)
    directive_command.add_argument("--order-id")

    retire_command = commands.add_parser("retire", help="Mark a decision thread as resolved by events or replaced by a newer decision.")
    add_convlog_path(retire_command)
    retire_command.add_argument("--thread", action="append", required=True)
    retire_command.add_argument("--state", choices=("moot", "superseded"), required=True)
    retire_command.add_argument("--basis", required=True)
    retire_command.add_argument("--citation", required=True)
    retire_command.add_argument("--authority", required=True)

    pending_command = commands.add_parser("pending", help="Render all unresolved decision briefs.")
    add_convlog_path(pending_command)

    show_command = commands.add_parser("show", help="Print one thread's JSONL entries.")
    add_convlog_path(show_command)
    show_command.add_argument("--thread", required=True)

    list_command = commands.add_parser("list", help="List derived conversation-thread states.")
    add_convlog_path(list_command)
    list_command.add_argument("--session-ref")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the conversation-log CLI and return a conventional exit status."""
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "brief":
            brief = validate_brief(_read_json_argument(args.json))
            if brief.session_ref != args.session_ref:
                raise ValueError("--session-ref must match the brief session_ref")
            raw_text = _read_raw_argument(args.raw, args.raw_file)
            if args.thread is None:
                if raw_text is None:
                    raise ValueError("--raw or --raw-file is required when opening a new thread")
                thread_id = open_thread(args.convlog_path, brief=brief, raw_text=raw_text)
            else:
                _require_thread(args.convlog_path, args.thread)
                if raw_text is not None:
                    append_session_message(
                        args.convlog_path, thread_id=args.thread, session_ref=brief.session_ref, text=raw_text, source_ref=brief.source_ref
                    )
                append_operator_brief(args.convlog_path, thread_id=args.thread, brief=brief)
                thread_id = args.thread
            print(render_brief(brief))
            print(f"thread={thread_id}", file=sys.stderr)
        elif args.command == "rule":
            for thread_id in args.thread:
                append_ruling(args.convlog_path, thread_id=thread_id, text=args.text, via=args.via)
        elif args.command == "directive":
            append_directive(args.convlog_path, thread_id=args.thread, text=args.text, order_id=args.order_id)
        elif args.command == "retire":
            for thread_id in args.thread:
                append_resolution(
                    args.convlog_path,
                    thread_id=thread_id,
                    state=args.state,
                    basis=args.basis,
                    citation=args.citation,
                    authority=args.authority,
                )
        elif args.command == "pending":
            threads = pending_threads(args.convlog_path)
            print(render_group(threads) if threads else "No pending decisions.")
        elif args.command == "show":
            for entry in _require_thread(args.convlog_path, args.thread):
                print(entry.model_dump_json(by_alias=True))
        else:
            for thread in list_threads(args.convlog_path, session_ref=args.session_ref):
                print(f"{thread.thread_id}\t{thread.session_ref}\t{thread.latest_brief.category}\t{thread.state}\t{thread.opened_at}")
    except (BriefValidationError, ConversationNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-convo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

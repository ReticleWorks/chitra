"""Deterministic close-time inventory checks for operator-stated goals.

This module reads a lane's frozen enrollment items and exact named receipts.
It never generates, expands, or rewrites done conditions, and it no longer
accepts free-form delivered-items prose or operator acknowledgements as
substitutes for receipted evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from chitra.completion_gate import CompletionEvidence, completion_receipt_issues

BARE_DELIVERABLE_PLURALS: frozenset[str] = frozenset({"clients", "consumers", "integrations"})

_COUNT_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_COUNT_TOKEN = rf"(?:\d+|{'|'.join(_COUNT_WORDS)})"
_COUNTED_DELIVERABLE_RE = re.compile(
    rf"\b(?P<count>{_COUNT_TOKEN})\s+(?:[a-z][a-z0-9_-]*\s+){{0,3}}(?P<noun>{'|'.join(sorted(BARE_DELIVERABLE_PLURALS))})\b",
    re.IGNORECASE,
)
_ENUMERATOR_RE = re.compile(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_ITEM_SEPARATOR_RE = re.compile(r"\s*(?:\n+|;+|,+|\band\b)\s*", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+#./-]*", re.IGNORECASE)
_IDENTITY_NOISE = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "both",
        "by",
        "complete",
        "completed",
        "defer",
        "deferred",
        "descoped",
        "done",
        "for",
        "follow",
        "following",
        "future",
        "in",
        "is",
        "item",
        "items",
        "live",
        "of",
        "on",
        "operator",
        "out",
        "pass",
        "passed",
        "passes",
        "required",
        "scope",
        "the",
        "to",
        "updated",
        "validation",
        "was",
        "were",
        "work",
    }
)
_GENERIC_SINGLE_TOKENS = BARE_DELIVERABLE_PLURALS | frozenset(
    {"client", "consumer", "integration", "check", "checks", "documentation", "docs", "test", "tests"}
)


@dataclass(frozen=True, slots=True)
class RequiredItem:
    """One literal condition fragment and any explicit quantity it states."""

    text: str
    quantity: int = 1
    counted_noun: str | None = None


class StructuredDoneItem(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def validator(self) -> str: ...

    @property
    def required_receipt(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StructuredCloseVerdict:
    verdict: Literal["PASS", "FAIL"]
    required_item_ids: tuple[str, ...]
    issues: tuple[str, ...]
    summary: str


class StructuredCloseGateError(ValueError):
    """Raised when exact named completion receipts do not cover frozen items."""

    def __init__(self, verdict: StructuredCloseVerdict) -> None:
        self.verdict = verdict
        super().__init__(verdict.summary)


def evaluate_structured_close_inventory(
    enrolled_items: Sequence[StructuredDoneItem],
    evidence: Sequence[CompletionEvidence],
) -> StructuredCloseVerdict:
    """Compare exact frozen item IDs, receipt names, validators, and results."""
    if not enrolled_items:
        return StructuredCloseVerdict(
            verdict="FAIL",
            required_item_ids=(),
            issues=("completion close requires at least one frozen done item",),
            summary="FAIL: completion close requires at least one frozen done item.",
        )
    issues = tuple(completion_receipt_issues(enrolled_items, evidence))
    if issues:
        return StructuredCloseVerdict(
            verdict="FAIL",
            required_item_ids=tuple(item.id for item in enrolled_items),
            issues=issues,
            summary="FAIL: " + "; ".join(issues),
        )
    return StructuredCloseVerdict(
        verdict="PASS",
        required_item_ids=tuple(item.id for item in enrolled_items),
        issues=(),
        summary="PASS: every frozen done item has its exact passing named receipt.",
    )


def require_structured_close_inventory(
    enrolled_items: Sequence[StructuredDoneItem],
    evidence: Sequence[CompletionEvidence],
) -> StructuredCloseVerdict:
    """Return a passing exact-receipt verdict or raise a typed close error."""
    verdict = evaluate_structured_close_inventory(enrolled_items, evidence)
    if verdict.verdict == "FAIL":
        raise StructuredCloseGateError(verdict)
    return verdict


def parse_required_items(done_when: str) -> tuple[RequiredItem, ...]:
    """Split explicit enumerators in an existing done condition conservatively."""
    enumerator_stripped = _ENUMERATOR_RE.sub("\n", done_when.strip())
    fragments = [fragment.strip(" \t\r\n:.-") for fragment in _ITEM_SEPARATOR_RE.split(enumerator_stripped)]
    items: list[RequiredItem] = []
    for fragment in fragments:
        if not fragment:
            continue
        if fragment.casefold().startswith("both "):
            fragment = fragment[5:].strip()
        counted = _COUNTED_DELIVERABLE_RE.search(fragment)
        if counted is None:
            items.append(RequiredItem(text=fragment))
            continue
        count_token = counted.group("count").casefold()
        quantity = int(count_token) if count_token.isdigit() else _COUNT_WORDS[count_token]
        plural_noun = counted.group("noun").casefold()
        items.append(RequiredItem(text=fragment, quantity=quantity, counted_noun=plural_noun.removesuffix("s")))
    return tuple(items)


def _identity_tokens(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in (match.group(0).casefold().strip("-./") for match in _WORD_RE.finditer(text))
        if token and token not in _IDENTITY_NOISE and not token.isdigit()
    )


def _token_sets_match(left: frozenset[str], right: frozenset[str]) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    if not smaller.issubset(larger):
        return False
    if len(smaller) >= 2:
        return True
    token = next(iter(smaller))
    return token not in _GENERIC_SINGLE_TOKENS and len(token) >= 3


def _item_matches(required: RequiredItem, candidate: str) -> bool:
    candidate_tokens = _identity_tokens(candidate)
    if required.counted_noun is not None:
        return required.counted_noun in candidate_tokens or f"{required.counted_noun}s" in candidate_tokens
    return _token_sets_match(_identity_tokens(required.text), candidate_tokens)


def _recorded_descopes(
    enrolled_done_when: str,
    current_done_when: str,
    *,
    goal_version: int = 1,
    goal_history: Sequence[Mapping[str, str]] = (),
) -> tuple[RequiredItem, ...]:
    """Return enrolled/history items absent now, regardless of version."""
    current = parse_required_items(current_done_when)
    descoped: list[RequiredItem] = []
    prior_conditions = (enrolled_done_when, *(entry["done_when"] for entry in goal_history if "done_when" in entry))
    for prior_done_when in prior_conditions:
        for prior_item in parse_required_items(prior_done_when):
            if any(_item_matches(item, prior_item.text) for item in current):
                continue
            if not any(_item_matches(item, prior_item.text) for item in descoped):
                descoped.append(prior_item)
    return tuple(descoped)

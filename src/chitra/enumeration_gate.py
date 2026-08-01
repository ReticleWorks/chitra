"""Fail-closed enumeration review at goal adoption and completion."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

AnnexItemStatus = Literal["required", "carried", "descoped"]

# These are deliberately public extension points. Terms here identify language
# that can quietly reduce a source inventory to a self-selected sample.
AGGREGATE_NOUN_TERMS: tuple[str, ...] = (
    "representative",
    "some",
    "several",
    "various",
    "a number of",
)
DELIVERABLE_NOUNS: tuple[str, ...] = (
    "client",
    "consumer",
    "deliverable",
    "endpoint",
    "integration",
    "item",
    "service",
    "task",
)
RECLASSIFICATION_PHRASES: tuple[str, ...] = (
    "follow-on",
    "out of scope",
    "out-of-scope",
    "deferred to",
    "future work",
)

_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_NUMBER_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_COUNTED_NOUN_RE = re.compile(
    rf"\b(?:all|exactly|at\s+least|at\s+most)?\s*(?:\d+|{_NUMBER_WORDS})\s+"
    rf"(?:[a-z0-9_-]+\s+){{0,2}}(?:{'|'.join(DELIVERABLE_NOUNS)})s?\b",
    re.IGNORECASE,
)
_BOTH_RE = re.compile(r"\bboth\b", re.IGNORECASE)
_REPEATED_NOUN_RE = re.compile(
    rf"\b(?P<noun>{'|'.join(DELIVERABLE_NOUNS)})\s+(?P<first>[A-Za-z0-9_.:-]+)\b"
    rf"[^.;\n]*\band\b[^.;\n]*\b(?P=noun)\s+(?P<second>[A-Za-z0-9_.:-]+)\b",
    re.IGNORECASE,
)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "be",
        "for",
        "in",
        "is",
        "it",
        "live",
        "of",
        "on",
        "pass",
        "passes",
        "required",
        "the",
        "to",
        "validation",
        "with",
    }
)


class EnumerationGateError(ValueError):
    """Base class for a rejected enumeration lifecycle transition."""


class AdoptionGateError(EnumerationGateError):
    """Raised when a drafted completion contract loses source inventory."""


class CloseInventoryError(EnumerationGateError):
    """Raised when a close claim does not deliver its bound inventory."""


@dataclass(frozen=True, slots=True)
class NormativeAnnexItem:
    """One stable source deliverable bound into a goal contract."""

    id: str
    text: str
    status: AnnexItemStatus = "required"
    reason: str = ""
    operator_ack: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "status": self.status,
            "reason": self.reason,
            "operator_ack": self.operator_ack,
        }

    @classmethod
    def from_dict(cls, payload: object) -> NormativeAnnexItem:
        if not isinstance(payload, Mapping):
            raise ValueError("normative annex items must be objects")
        allowed = {"id", "text", "status", "reason", "operator_ack"}
        if not set(payload).issubset(allowed):
            raise ValueError("normative annex items contain unsupported fields")
        item_id = payload.get("id")
        text = payload.get("text")
        status = payload.get("status", "required")
        reason = payload.get("reason", "")
        operator_ack = payload.get("operator_ack", False)
        if not isinstance(item_id, str) or not isinstance(text, str):
            raise ValueError("normative annex item id and text must be strings")
        if status not in {"required", "carried", "descoped"}:
            raise ValueError("normative annex item status must be required, carried, or descoped")
        if not isinstance(reason, str) or not isinstance(operator_ack, bool):
            raise ValueError("normative annex item reason must be a string and operator_ack must be a boolean")
        return cls(id=item_id, text=text, status=status, reason=reason, operator_ack=operator_ack)


@dataclass(frozen=True, slots=True)
class AdoptionReview:
    accepted: bool
    issues: tuple[str, ...]
    explicit_count: bool
    aggregate_terms: tuple[str, ...]
    uncovered_item_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CloseInventoryReview:
    accepted: bool
    issues: tuple[str, ...]
    required_item_ids: tuple[str, ...]
    delivered_item_ids: tuple[str, ...]
    missing_item_ids: tuple[str, ...]
    reclassified_item_ids: tuple[str, ...]


def _singular(token: str) -> str:
    lowered = token.lower()
    if lowered.endswith("ies") and len(lowered) > 3:
        return lowered[:-3] + "y"
    if lowered.endswith("s") and len(lowered) > 3:
        return lowered[:-1]
    return lowered


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_singular(token) for token in _TOKEN_RE.findall(text))


def _contains_token_sequence(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(tuple(haystack[index : index + len(needle)]) == tuple(needle) for index in range(len(haystack) - len(needle) + 1))


def has_determinable_count(done_when: str) -> bool:
    """Return whether text fixes a deliverable count rather than a sample."""
    if _BOTH_RE.search(done_when) or _COUNTED_NOUN_RE.search(done_when):
        return True
    return any(
        match.group("first").lower() != match.group("second").lower()
        for match in _REPEATED_NOUN_RE.finditer(done_when)
    )


def lint_aggregate_nouns(done_when: str) -> tuple[str, ...]:
    """Return quantifier-eroding terms and bare deliverable plurals."""
    lowered = done_when.lower()
    matches = [term for term in AGGREGATE_NOUN_TERMS if re.search(rf"\b{re.escape(term)}\b", lowered)]
    matches.extend(noun + "s" for noun in DELIVERABLE_NOUNS if re.search(rf"\b{re.escape(noun)}s\b", lowered))
    return tuple(dict.fromkeys(matches))


def _annex_issues(annex: Sequence[NormativeAnnexItem]) -> list[str]:
    issues: list[str] = []
    ids = [item.id for item in annex]
    duplicates = sorted(item_id for item_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        issues.append(f"normative annex item ids must be unique: {duplicates!r}")
    for item in annex:
        if not _ITEM_ID_RE.fullmatch(item.id):
            issues.append(f"normative annex item id must be stable and identifier-like: {item.id!r}")
        if not item.text.strip():
            issues.append(f"normative annex item {item.id!r} text must be non-empty")
        if item.status == "descoped" and (not item.reason.strip() or not item.operator_ack):
            issues.append(f"descoped annex item {item.id!r} requires a reason and explicit operator ack")
    return issues


def _covered_required_items(done_when: str, annex: Sequence[NormativeAnnexItem]) -> set[str]:
    done_tokens = _tokens(done_when)
    required = [item for item in annex if item.status == "required"]
    salient_by_id: dict[str, set[str]] = {}
    for item in required:
        salient_by_id[item.id] = {
            token for token in _tokens(f"{item.id} {item.text}") if token not in _STOP_WORDS
        }
    token_counts = Counter(token for tokens in salient_by_id.values() for token in tokens)
    covered: set[str] = set()
    for item in required:
        id_tokens = _tokens(item.id)
        if _contains_token_sequence(done_tokens, id_tokens):
            covered.add(item.id)
            continue
        discriminators = {token for token in salient_by_id[item.id] if token_counts[token] == 1}
        if discriminators.intersection(done_tokens):
            covered.add(item.id)
            continue
        if len(required) == 1 and salient_by_id[item.id].intersection(done_tokens):
            covered.add(item.id)
    return covered


def review_adoption(done_when: str, annex: Sequence[NormativeAnnexItem] = ()) -> AdoptionReview:
    """Review a newly minted/transferred completion contract in isolation."""
    issues = _annex_issues(annex)
    explicit_count = has_determinable_count(done_when)
    aggregate_terms = lint_aggregate_nouns(done_when)
    if not explicit_count and not annex:
        issues.append("done_when neither enumerates countable deliverables nor carries a source-enumeration annex")
    covered = _covered_required_items(done_when, annex)
    uncovered = tuple(item.id for item in annex if item.status == "required" and item.id not in covered)
    if uncovered:
        issues.append(f"done_when dropped or collapsed required annex item(s): {', '.join(uncovered)}")
    if aggregate_terms and not explicit_count and not annex:
        issues.append("aggregate-noun lint requires an explicit count or count-pinning annex: " + ", ".join(aggregate_terms))
    return AdoptionReview(
        accepted=not issues,
        issues=tuple(issues),
        explicit_count=explicit_count,
        aggregate_terms=aggregate_terms,
        uncovered_item_ids=uncovered,
    )


def require_adoption(done_when: str, annex: Sequence[NormativeAnnexItem] = ()) -> None:
    review = review_adoption(done_when, annex)
    if not review.accepted:
        raise AdoptionGateError("adoption enumeration gate rejected goal: " + "; ".join(review.issues))


def review_close_inventory(
    annex: Sequence[NormativeAnnexItem],
    delivered_item_ids: Iterable[str],
    close_claim: str = "",
) -> CloseInventoryReview:
    """Diff hash-bound required/carried items against one close inventory."""
    issues = _annex_issues(annex)
    required_ids = tuple(item.id for item in annex if item.status in {"required", "carried"})
    delivered = tuple(dict.fromkeys(item_id.strip() for item_id in delivered_item_ids if item_id.strip()))
    delivered_set = set(delivered)
    missing = tuple(item_id for item_id in required_ids if item_id not in delivered_set)
    lowered_claim = close_claim.lower()
    has_reclassification = any(phrase in lowered_claim for phrase in RECLASSIFICATION_PHRASES)
    reclassified = missing if has_reclassification else ()
    if missing:
        issues.append("undelivered required annex item(s): " + ", ".join(missing))
    if reclassified:
        issues.append("unapproved follow-on/out-of-scope reclassification of required annex item(s): " + ", ".join(reclassified))
    return CloseInventoryReview(
        accepted=not issues,
        issues=tuple(issues),
        required_item_ids=required_ids,
        delivered_item_ids=delivered,
        missing_item_ids=missing,
        reclassified_item_ids=reclassified,
    )


def require_close_inventory(
    annex: Sequence[NormativeAnnexItem],
    delivered_item_ids: Iterable[str],
    close_claim: str = "",
) -> None:
    review = review_close_inventory(annex, delivered_item_ids, close_claim)
    if not review.accepted:
        raise CloseInventoryError("close inventory gate rejected close: " + "; ".join(review.issues))

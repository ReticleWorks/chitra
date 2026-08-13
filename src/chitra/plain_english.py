"""Deterministic checks for operator-facing plain English.

Quoted source material is deliberately not passed through these checks. A
durable record must preserve its evidence exactly.
"""

from __future__ import annotations

import re

_CODENAME = re.compile(r"(?:[A-Za-z]+-?\d+|[A-Z]{2,})(?![^()]*\))")
_JARGON = {
    "bluf": "bottom line first",
    "descoped": "removed from the agreed work",
    "ingestion gate": "requirement to record work before it starts",
    "lane": "work session",
    "moot": "resolved by events",
    "nudge": "follow-up message",
}


def plain_english_issues(text: str, *, field: str = "text") -> tuple[str, ...]:
    """Return concrete readability problems without changing the supplied text."""
    stripped = text.strip()
    issues: list[str] = []
    if not stripped:
        return (f"{field} must be non-empty",)
    lowered = stripped.casefold()
    for jargon, gloss in _JARGON.items():
        if re.search(rf"\b{re.escape(jargon)}\b", lowered) and f"{jargon} (" not in lowered:
            issues.append(f'{field} uses "{jargon}" without explaining it; for example, "{gloss}"')
    if _CODENAME.fullmatch(stripped):
        issues.append(f"{field} is only a codename; add its plain-English name")
    words = re.findall(r"[A-Za-z0-9]+", stripped)
    if len(words) >= 4 and stripped[-1] not in ".?!):]}'\"`":
        issues.append(f"{field} reads like a fragment; write a complete sentence")
    return tuple(issues)


def require_plain_english(text: str, *, field: str = "text") -> str:
    """Return text unchanged, or raise with actionable plain-English advice."""
    issues = plain_english_issues(text, field=field)
    if issues:
        raise ValueError("; ".join(issues))
    return text

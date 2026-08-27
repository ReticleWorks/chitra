"""Immutable, user-supplied context carried into every governed session.

The bundle is deliberately small and declarative.  Chitra records its canonical
form with each launch so a later reviewer can establish exactly which system
facts and operating rules a session received.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

KNOWLEDGE_BUNDLE_SCHEMA = "chitra.knowledge-bundle.v1"
_BUNDLE_FIELDS = (
    "system_facts",
    "architecture_principles",
    "code_patterns",
    "decision_rules",
    "canonical_references",
)


def _text_items(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"knowledge_bundle.{field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True, slots=True)
class KnowledgeBundle:
    """Canonical system knowledge supplied by the operator for one lane."""

    system_facts: tuple[str, ...] = ()
    architecture_principles: tuple[str, ...] = ()
    code_patterns: tuple[str, ...] = ()
    decision_rules: tuple[str, ...] = ()
    canonical_references: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> KnowledgeBundle:
        return cls()

    @classmethod
    def from_mapping(cls, value: Any) -> KnowledgeBundle:
        if value is None:
            return cls.empty()
        if not isinstance(value, dict):
            raise ValueError("knowledge_bundle must be a mapping")
        unknown = sorted(set(value) - set(_BUNDLE_FIELDS))
        if unknown:
            raise ValueError(f"knowledge_bundle has unsupported fields: {', '.join(unknown)}")
        return cls(**{field: _text_items(value.get(field), field=field) for field in _BUNDLE_FIELDS})

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": KNOWLEDGE_BUNDLE_SCHEMA,
            "system_facts": list(self.system_facts),
            "architecture_principles": list(self.architecture_principles),
            "code_patterns": list(self.code_patterns),
            "decision_rules": list(self.decision_rules),
            "canonical_references": list(self.canonical_references),
        }

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def render(self) -> str:
        """Render a readable, stable section for a provider setup note."""
        sections = (
            ("System and environment facts", self.system_facts),
            ("Architecture principles", self.architecture_principles),
            ("Existing code patterns", self.code_patterns),
            ("Decision rules and exceptions", self.decision_rules),
            ("Canonical references", self.canonical_references),
        )
        lines = ["# Chitra canonical knowledge", f"Bundle SHA-256: {self.sha256}"]
        for heading, items in sections:
            lines.extend(("", f"## {heading}"))
            if items:
                lines.extend(f"- {item}" for item in items)
            else:
                lines.append("- No entries supplied.")
        return "\n".join(lines) + "\n"

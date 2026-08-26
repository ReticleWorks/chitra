"""Typed, exact evidence for configured canonical choices."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, Field, model_validator

from chitra.journal.models import CanonicalEvent, CanonicalType

if TYPE_CHECKING:
    from chitra.detect.detectors import Finding

CanonicalChoiceKind = Literal[
    "required_path",
    "pinned_version",
    "host_role",
    "model_route",
    "deprecated_path",
]

_REGISTRY_KEY_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_TARGET_FIELDS = frozenset({"file_path", "path", "target", "target_path", "files", "paths"})


class CanonicalChoice(BaseModel):
    """One typed choice addressed by a stable registry key."""

    kind: CanonicalChoiceKind
    subject: str
    canonical_value: str

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if not self.subject.strip():
            raise ValueError("subject must be a non-empty string")
        if not self.canonical_value.strip():
            raise ValueError("canonical_value must be a non-empty string")
        if self.kind == "deprecated_path":
            for field_name in ("subject", "canonical_value"):
                value = getattr(self, field_name)
                if not value.startswith("/"):
                    raise ValueError(f"{field_name} must be an absolute path for deprecated_path")
                if _lexical_path(value) is None:
                    raise ValueError(f"{field_name} must be a valid absolute path")
            if _lexical_path(self.subject) == _lexical_path(self.canonical_value):
                raise ValueError("canonical_value must replace the deprecated path")
        return self


class CanonicalChoicesPolicy(BaseModel):
    """Registry mapping stable rule keys to typed canonical choices."""

    choices: dict[str, CanonicalChoice] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_choice_keys(self) -> Self:
        for key in self.choices:
            if _REGISTRY_KEY_RE.fullmatch(key) is None:
                raise ValueError("choices keys must be a stable registry key")
        return self


def _lexical_path(value: str, *, cwd: str | None = None) -> str | None:
    """Normalize a path without filesystem access or symlink resolution."""
    if not value or "\x00" in value:
        return None
    candidate = value
    if not candidate.startswith("/"):
        if cwd is None or not cwd.startswith("/"):
            return None
        candidate = posixpath.join(cwd, candidate)
    return posixpath.normpath(candidate)


def _event_cwd(event: CanonicalEvent, input_value: dict[str, object]) -> str | None:
    input_cwd = input_value.get("cwd")
    if isinstance(input_cwd, str):
        return input_cwd
    payload_cwd = event.payload.get("cwd")
    return payload_cwd if isinstance(payload_cwd, str) else None


def _explicit_targets(input_value: object) -> tuple[str, ...]:
    if not isinstance(input_value, dict):
        return ()
    targets: list[str] = []
    for field_name, value in input_value.items():
        if field_name not in _TARGET_FIELDS:
            continue
        if isinstance(value, str) and value:
            targets.append(value)
        elif field_name in {"files", "paths"} and isinstance(value, list):
            targets.extend(item for item in value if isinstance(item, str) and item)
    return tuple(targets)


def _first_unmet_item(enrolled_items: Sequence[object]) -> str:
    for item in enrolled_items:
        item_id = getattr(item, "id", None)
        if isinstance(item_id, str):
            return item_id
    return ""


def detect_canonical_choices(
    events: Sequence[CanonicalEvent],
    policy: CanonicalChoicesPolicy,
    *,
    enrolled_items: Sequence[object] = (),
) -> list[Finding]:
    """Find exact deprecated-path writes backed by explicit tool fields.

    Reads, free text, substring matches, and relative paths without an
    event-local working directory are not evidence. The resolver never probes
    the filesystem, so lexical normalization is deterministic.
    """
    from chitra.detect.detectors import Finding

    findings: list[Finding] = []
    unmet = _first_unmet_item(enrolled_items)
    path_choices = tuple((key, choice) for key, choice in policy.choices.items() if choice.kind == "deprecated_path")
    for event in events:
        if event.normalized_type is not CanonicalType.TOOL_CALL:
            continue
        tool_name = event.payload.get("tool_name")
        if not isinstance(tool_name, str) or tool_name not in _WRITE_TOOLS:
            continue
        input_value = event.payload.get("input")
        if not isinstance(input_value, dict):
            continue
        cwd = _event_cwd(event, input_value)
        targets = tuple(
            normalized
            for raw_target in _explicit_targets(input_value)
            if (normalized := _lexical_path(raw_target, cwd=cwd)) is not None
        )
        if not targets:
            continue
        for rule_key, choice in path_choices:
            subject = _lexical_path(choice.subject)
            canonical_value = _lexical_path(choice.canonical_value)
            if subject is None or canonical_value is None or subject not in targets:
                continue
            findings.append(
                Finding(
                    detector="canonical_choices.deprecated_path",
                    fingerprint_seed={
                        "rule_key": rule_key,
                        "kind": choice.kind,
                        "subject": subject,
                        "canonical_value": canonical_value,
                    },
                    event_refs=(event.event_id,),
                    unmet_item=unmet,
                    expected_next_progress=f"write to the approved canonical path {canonical_value}",
                    detail=f"{tool_name} targeted deprecated path {subject}; use {canonical_value}",
                )
            )
    return findings


__all__ = ["CanonicalChoice", "CanonicalChoicesPolicy", "detect_canonical_choices"]

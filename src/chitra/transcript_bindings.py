"""Strict, versioned declarations for monitor-owned agent transcripts.

The monitor cannot safely infer which session a transcript belongs to from a
directory name or from recency.  This document binds a concrete JSONL path to
the exact enrolled session and durable lane before normalization begins.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from chitra.journal import SUPPORTED_VERSIONS, Client

SCHEMA = "chitra.transcript-bindings.v1"
DEFAULT_FILENAME = "transcript-bindings.json"


class TranscriptBinding(BaseModel):
    """One exact transcript-to-session binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_ref: StrictStr = Field(min_length=1)
    lane: StrictStr = Field(min_length=1)
    path: StrictStr = Field(min_length=1)
    client: Client
    client_version: StrictStr = Field(min_length=1)
    instance: StrictStr = Field(min_length=1)

    @model_validator(mode="after")
    def validate_binding(self) -> TranscriptBinding:
        for name in ("session_ref", "lane", "path", "client_version", "instance"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.client_version not in SUPPORTED_VERSIONS[self.client]:
            supported = ", ".join(sorted(SUPPORTED_VERSIONS[self.client]))
            raise ValueError(
                f"unsupported {self.client.value} version {self.client_version!r}; "
                f"fixture-gated versions: {supported}"
            )
        return self

    def resolved_path(self, *, manifest_path: Path, transcript_root: Path | None) -> Path:
        """Resolve the path and constrain relative paths to the transcript root."""
        candidate = Path(self.path).expanduser()
        if candidate.is_absolute():
            return candidate
        root = (transcript_root or manifest_path.parent).expanduser().resolve()
        resolved = (root / candidate).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"relative transcript path escapes transcript_root: {self.path!r} "
                f"(root {str(root)!r})"
            )
        return resolved


class TranscriptBindingsDocument(BaseModel):
    """The on-disk transcript binding manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    schema_name: Literal["chitra.transcript-bindings.v1"] = Field(alias="schema")
    bindings: list[TranscriptBinding]


def load_transcript_bindings(
    path: Path | None,
    *,
    transcript_root: Path | None = None,
) -> tuple[TranscriptBinding, ...]:
    """Load and validate a binding manifest.

    A missing default manifest means that only legacy journals are available;
    malformed manifests fail closed instead of silently changing ownership.
    """
    if path is None:
        return ()
    manifest_path = path.expanduser()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ValueError(f"transcript binding manifest cannot be read: {manifest_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"transcript binding manifest is not valid JSON: {manifest_path}: {exc}") from exc

    try:
        document = TranscriptBindingsDocument.model_validate(payload)
    except ValueError as exc:
        raise ValueError(f"invalid transcript binding manifest {manifest_path}: {exc}") from exc

    seen: dict[str, set[str]] = {"session_ref": set(), "lane": set(), "path": set()}
    bindings: list[TranscriptBinding] = []
    for binding in document.bindings:
        resolved = binding.resolved_path(manifest_path=manifest_path, transcript_root=transcript_root)
        values = {
            "session_ref": binding.session_ref,
            "lane": binding.lane,
            "path": str(resolved),
        }
        for name, value in values.items():
            if value in seen[name]:
                raise ValueError(f"transcript binding {name} is not unique: {value!r}")
            seen[name].add(value)
        bindings.append(binding)
    return tuple(bindings)


__all__ = ["DEFAULT_FILENAME", "SCHEMA", "TranscriptBinding", "TranscriptBindingsDocument", "load_transcript_bindings"]

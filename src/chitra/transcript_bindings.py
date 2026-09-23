"""Strict, versioned declarations for monitor-owned agent transcripts.

The monitor cannot safely infer which session a transcript belongs to from a
directory name or from recency.  This document binds a concrete JSONL path to
the exact enrolled session and durable lane before normalization begins.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from chitra.journal import Client

SCHEMA = "chitra.transcript-bindings.v1"
DEFAULT_FILENAME = "transcript-bindings.json"

# A binding ``path`` may be a URI locator (``amp-orb:<slug>``) instead of a
# filesystem path when the lane's evidence stream is not a local JSONL file.
_URI_LOCATOR_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]+:")


class TranscriptBinding(BaseModel):
    """One exact transcript-to-session binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_ref: StrictStr = Field(min_length=1)
    lane: StrictStr = Field(min_length=1)
    path: StrictStr = Field(min_length=1)
    # Known clients stay enum members; a plug-owned harness name (e.g. "amp")
    # is kept as a plain string, matching CanonicalEvent.client.
    client: Client | str
    client_version: StrictStr = Field(min_length=1)
    instance: StrictStr = Field(min_length=1)

    @field_validator("client", mode="before")
    @classmethod
    def _known_client(cls, value: Any) -> Any:
        try:
            return Client(value)
        except ValueError:
            return value

    @model_validator(mode="after")
    def validate_binding(self) -> TranscriptBinding:
        for name in ("session_ref", "lane", "path", "client_version", "instance"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        return self

    @property
    def is_uri_locator(self) -> bool:
        """True when ``path`` is a locator URI, not a transcript file."""
        return _URI_LOCATOR_RE.match(self.path) is not None

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

    def resolved_ref(self, *, manifest_path: Path, transcript_root: Path | None) -> Path | str:
        """The binding's resolved locator: a Path for files, the URI verbatim."""
        if self.is_uri_locator:
            return self.path
        return self.resolved_path(manifest_path=manifest_path, transcript_root=transcript_root)


@dataclass(frozen=True, slots=True)
class BoundTranscript:
    """A loaded binding with its evidence locator resolved for one pass.

    ``path`` is a ``Path`` for JSONL transcripts and the verbatim URI string
    for URI locators (``amp-orb:<slug>``), which name no local transcript.
    """

    binding: TranscriptBinding
    path: Path | str

    @property
    def is_uri(self) -> bool:
        return isinstance(self.path, str)

    @property
    def client(self) -> str:
        return str(self.binding.client)

    @property
    def session_ref(self) -> str:
        return self.binding.session_ref

    @property
    def lane(self) -> str:
        return self.binding.lane

    @property
    def instance(self) -> str:
        return self.binding.instance


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
        resolved = binding.resolved_ref(manifest_path=manifest_path, transcript_root=transcript_root)
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


__all__ = [
    "DEFAULT_FILENAME",
    "SCHEMA",
    "BoundTranscript",
    "TranscriptBinding",
    "TranscriptBindingsDocument",
    "load_transcript_bindings",
]

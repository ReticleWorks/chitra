"""Typed loader for chitra's operational completion-evasion codes.

The broader documentation-only taxonomy lives in ``docs/evasion-taxonomy.md``.
Each entry's ``phrases`` are that code's shipped cue vocabulary: the
completion gate's deferral scan reads its phrase list from the
``DEFERRAL_STUB`` entry here.

No LLM calls. Deterministic JSON loading + Pydantic validation only.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path

from pydantic import BaseModel


class TaxonomyEntry(BaseModel):
    """One completion-evasion code operationalized by chitra."""

    code: str
    cue: str
    phrases: tuple[str, ...] = ()


@lru_cache(maxsize=1)
def _load_packaged_taxonomy() -> tuple[TaxonomyEntry, ...]:
    """Load and validate the shipped taxonomy, cached after first call."""
    raw = json.loads(resources.files("chitra").joinpath("taxonomy.json").read_text(encoding="utf-8"))
    return tuple(TaxonomyEntry.model_validate(item) for item in raw["entries"])


def load_taxonomy(path: str | Path | None = None) -> tuple[TaxonomyEntry, ...]:
    """Load the shipped taxonomy, or validate a configured replacement file."""
    if path is None:
        return _load_packaged_taxonomy()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(TaxonomyEntry.model_validate(item) for item in raw["entries"])


def cue_phrases(code: str, path: str | Path | None = None) -> tuple[str, ...]:
    """Return the cue phrases declared for one operationalized code."""
    return tuple(phrase for entry in load_taxonomy(path) if entry.code == code for phrase in entry.phrases)

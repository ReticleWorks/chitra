"""Durable backend-neutral pane activity facts emitted by monitord's pane sensing.

The rate-limit guard's quiescence check is a one-shot process, while monitord
is the component that already observes pane changes. This small state file
bridges those two lifetimes without teaching the guard to inspect
conversation content.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from chitra._fsio import exclusive_lock, write_json_atomic
from chitra.state_paths import state_dir

SCHEMA = "chitra.lane-activity.v1"
LaneBackend = Literal["claude", "codex", "opencode", "unknown"]


@dataclass(frozen=True, slots=True)
class LaneActivity:
    """Last pane-change and attachment facts for one tracked session."""

    session_ref: str
    pane_id: str
    last_change_at: str
    last_seen_at: str
    attached: bool
    backend: LaneBackend = "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "session_ref": self.session_ref,
            "pane_id": self.pane_id,
            "last_change_at": self.last_change_at,
            "last_seen_at": self.last_seen_at,
            "attached": self.attached,
            "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, payload: object) -> LaneActivity:
        if not isinstance(payload, dict):
            raise ValueError("lane activity record must be an object")
        strings: dict[str, str] = {}
        for name in ("session_ref", "pane_id", "last_change_at", "last_seen_at"):
            value = payload.get(name)
            if not isinstance(value, str):
                raise ValueError(f"lane activity {name} must be a string")
            strings[name] = value
        attached = payload.get("attached")
        if not isinstance(attached, bool):
            raise ValueError("lane activity attached must be a boolean")
        backend = payload.get("backend", "unknown")
        if backend not in ("claude", "codex", "opencode", "unknown"):
            raise ValueError("lane activity backend must be claude, codex, opencode, or unknown")
        return cls(**strings, attached=attached, backend=cast(LaneBackend, backend))


def activity_path(root: Path | None = None) -> Path:
    """Return the pane-activity state path beneath ``root``."""
    return (state_dir() if root is None else root) / "lane_activity.json"


@contextlib.contextmanager
def _activity_lock(root: Path | None) -> Iterator[None]:
    path = activity_path(root)
    with exclusive_lock(path.with_name(f".{path.name}.lock"), mode=0o600):
        yield


def load_lane_activity(root: Path | None = None) -> list[LaneActivity]:
    """Load current activity facts; a missing file means none observed yet."""
    path = activity_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("lane_activity.json is not a chitra.lane-activity.v1 document")
    raw = payload.get("lanes")
    if not isinstance(raw, list):
        raise ValueError("lane_activity.json lanes must be a list")
    return [LaneActivity.from_dict(item) for item in raw]


def _write_activity(root: Path | None, records: list[LaneActivity]) -> None:
    path = activity_path(root)
    payload = {"schema": SCHEMA, "lanes": [record.to_dict() for record in records]}
    write_json_atomic(path, payload)


def upsert_lane_activity(root: Path | None, records: Iterable[LaneActivity]) -> None:
    """Atomically merge one sensing pass's activity facts by session reference."""
    incoming = list(records)
    if not incoming:
        return
    with _activity_lock(root):
        merged = {record.session_ref: record for record in load_lane_activity(root)}
        merged.update((record.session_ref, record) for record in incoming)
        _write_activity(root, sorted(merged.values(), key=lambda record: record.session_ref))

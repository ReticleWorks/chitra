"""Advisory, per-writer presence for shared Chitra resources."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SHARED_DIR_ENV_VAR = "CHITRA_SHARED_DIR"
DEFAULT_SHARED_DIR = Path("/var/lib/polyphony-chitra-coordination")
_INSTANCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_STATES = frozenset({"using", "released"})


class PresenceError(ValueError):
    """A presence record or operation is invalid."""


@dataclass(frozen=True, slots=True)
class PresenceRecord:
    """One append-only declaration in an instance-owned presence file."""

    instance: str
    session: str
    resource: str
    lanes: tuple[str, ...]
    state: str
    since: str
    note: str
    goal_refs: tuple[str, ...]
    purpose: str
    journal_ref: str

    def to_dict(self) -> dict[str, object]:
        return {
            "instance": self.instance,
            "session": self.session,
            "resource": self.resource,
            "lanes": list(self.lanes),
            "state": self.state,
            "since": self.since,
            "note": self.note,
            "goal_refs": list(self.goal_refs),
            "purpose": self.purpose,
            "journal_ref": self.journal_ref,
        }

    @classmethod
    def from_dict(cls, payload: object) -> PresenceRecord:
        if not isinstance(payload, dict):
            raise PresenceError("presence record must be a JSON object")
        instance = payload.get("instance")
        session = payload.get("session")
        resource = payload.get("resource")
        lanes = payload.get("lanes")
        state = payload.get("state")
        since = payload.get("since")
        note = payload.get("note")
        goal_refs = payload.get("goal_refs")
        purpose = payload.get("purpose")
        journal_ref = payload.get("journal_ref")
        if not isinstance(instance, str):
            raise PresenceError("presence instance must be a string")
        _validate_instance(instance)
        if not isinstance(session, str) or not session.strip():
            raise PresenceError("presence session must be a non-empty string")
        if not isinstance(resource, str) or not resource.strip():
            raise PresenceError("presence resource must be a non-empty string")
        if not isinstance(lanes, list) or not all(isinstance(lane, str) and lane.strip() for lane in lanes):
            raise PresenceError("presence lanes must be non-empty strings")
        if state not in _STATES:
            raise PresenceError("presence state must be using or released")
        if not isinstance(since, str):
            raise PresenceError("presence since must be an ISO-8601 string")
        _normalize_time(since)
        if not isinstance(note, str):
            raise PresenceError("presence note must be a string")
        if not isinstance(goal_refs, list) or not all(isinstance(ref, str) and ref.strip() for ref in goal_refs):
            raise PresenceError("presence goal_refs must be non-empty strings")
        if not isinstance(purpose, str):
            raise PresenceError("presence purpose must be a string")
        if not isinstance(journal_ref, str):
            raise PresenceError("presence journal_ref must be a string")
        return cls(
            instance,
            session,
            resource,
            tuple(lanes),
            state,
            since,
            note,
            tuple(goal_refs),
            purpose,
            journal_ref,
        )


def shared_dir(path: Path | None = None) -> Path:
    """Resolve the coordination root from an argument, the environment, or the fleet default."""
    if path is not None:
        return path
    return Path(os.environ.get(SHARED_DIR_ENV_VAR, str(DEFAULT_SHARED_DIR))).expanduser()


def _validate_instance(instance: str) -> None:
    if _INSTANCE_RE.fullmatch(instance) is None:
        raise PresenceError("instance must contain only letters, digits, dot, underscore, and hyphen")


def _normalize_time(value: datetime | str | None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PresenceError("since must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PresenceError("since must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _presence_path(root: Path, instance: str) -> Path:
    _validate_instance(instance)
    return root / "presence" / f"{instance}.jsonl"


def append_presence(
    instance: str,
    resource: str,
    *,
    session: str,
    lanes: tuple[str, ...] | list[str] = (),
    state: str,
    note: str = "",
    since: datetime | str | None = None,
    goal_refs: tuple[str, ...] | list[str] = (),
    purpose: str = "",
    journal_ref: str = "",
    root: Path | None = None,
) -> PresenceRecord:
    """Append to the calling instance's file without locking any peer writer."""
    if not resource.strip():
        raise PresenceError("resource must be non-empty")
    if not session.strip():
        raise PresenceError("session must be non-empty")
    if state not in _STATES:
        raise PresenceError("state must be using or released")
    normalized_lanes = tuple(dict.fromkeys(lane.strip() for lane in lanes if lane.strip()))
    if len(normalized_lanes) != len(lanes):
        raise PresenceError("lanes must be unique, non-empty strings")
    normalized_goal_refs = tuple(dict.fromkeys(ref.strip() for ref in goal_refs if ref.strip()))
    if len(normalized_goal_refs) != len(goal_refs):
        raise PresenceError("goal_refs must be unique, non-empty strings")
    record = PresenceRecord(
        instance=instance,
        session=session,
        resource=resource,
        lanes=normalized_lanes,
        state=state,
        since=_normalize_time(since),
        note=note,
        goal_refs=normalized_goal_refs,
        purpose=purpose,
        journal_ref=journal_ref,
    )
    path = _presence_path(shared_dir(root), instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
    return record


def list_presence(*, root: Path | None = None, include_released: bool = False) -> list[PresenceRecord]:
    """Merge every writer file and return the latest declaration per instance, session, and resource.

    The merged identity includes the session, so one session's release can
    never hide another still-active session's use of the same resource.
    """
    directory = shared_dir(root) / "presence"
    if not directory.is_dir():
        return []
    latest: dict[tuple[str, str, str], PresenceRecord] = {}
    for path in sorted(directory.glob("*.jsonl")):
        expected_instance = path.name.removesuffix(".jsonl")
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if not line.endswith("\n"):
                        continue
                    try:
                        record = PresenceRecord.from_dict(json.loads(line))
                    except (json.JSONDecodeError, PresenceError):
                        continue
                    if record.instance == expected_instance:
                        latest[(record.instance, record.session, record.resource)] = record
        except OSError:
            continue
    records = latest.values() if include_released else (record for record in latest.values() if record.state == "using")
    return sorted(records, key=lambda record: (record.instance, record.resource))


def peers_using(instance: str, resource: str, *, root: Path | None = None) -> list[PresenceRecord]:
    """Return current peer users of a resource; this advisory check never waits or claims it."""
    _validate_instance(instance)
    return [record for record in list_presence(root=root) if record.instance != instance and record.resource == resource]


def announce_using(
    instance: str,
    resource: str,
    *,
    session: str,
    lanes: tuple[str, ...] | list[str] = (),
    note: str = "",
    since: datetime | str | None = None,
    goal_refs: tuple[str, ...] | list[str] = (),
    purpose: str = "",
    journal_ref: str = "",
    root: Path | None = None,
) -> list[PresenceRecord]:
    """Declare use and return visible peers without acquiring authority or blocking."""
    append_presence(
        instance,
        resource,
        session=session,
        lanes=lanes,
        state="using",
        note=note,
        since=since,
        goal_refs=goal_refs,
        purpose=purpose,
        journal_ref=journal_ref,
        root=root,
    )
    return peers_using(instance, resource, root=root)


def announce_released(
    instance: str,
    resource: str,
    *,
    session: str,
    lanes: tuple[str, ...] | list[str] = (),
    note: str = "",
    since: datetime | str | None = None,
    goal_refs: tuple[str, ...] | list[str] = (),
    purpose: str = "",
    journal_ref: str = "",
    root: Path | None = None,
) -> PresenceRecord:
    """Append an explicit release; records never expire implicitly."""
    return append_presence(
        instance,
        resource,
        session=session,
        lanes=lanes,
        state="released",
        note=note,
        since=since,
        goal_refs=goal_refs,
        purpose=purpose,
        journal_ref=journal_ref,
        root=root,
    )

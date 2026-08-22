"""Deterministic storage for monitor-owned per-lane goal state.

No LLM calls in this module's own code path — it only records the monitor's
stated goal, completion condition, and current state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from pydantic import ConfigDict, SkipValidation, TypeAdapter, ValidationInfo, model_validator
from pydantic.dataclasses import dataclass as pydantic_dataclass

from chitra._fsio import locked_json_store, parse_iso8601, write_json_atomic
from chitra.close_gate import RequiredItem, _recorded_descopes, require_structured_close_inventory
from chitra.completion_gate import CompletionEvidence, require_completion_receipts
from chitra.plain_english import require_plain_english
from chitra.state_paths import state_dir
from chitra.validation_receipts import ReceiptError, require_verified_completion_receipts

logger = structlog.get_logger(__name__)

GoalStatus = Literal[
    "working",
    "held",
    "idle",
    "blocked",
    "turn-finished-unverified",
    "completion-disputed",
    "done-pending-verification",
    "done-pending-close",
]
GOAL_STATUSES: tuple[GoalStatus, ...] = (
    "working",
    "held",
    "idle",
    "blocked",
    "turn-finished-unverified",
    "completion-disputed",
    "done-pending-verification",
    "done-pending-close",
)
SCHEMA = "chitra.goals.v3"
# v3 adds the machine-enforced interview receipt, frozen structured done items,
# and completion proofs. v1/v2 remain readable for display and administrative
# disposal, but their missing v3 enrollment contract can never pass launch,
# enter a done state, or use completion close.
SUPPORTED_SCHEMAS = (SCHEMA, "chitra.goals.v2", "chitra.goals.v1")
# Reads tolerate every chitra.goals.v<N> document (unknown fields are ignored
# on load), so a file written by a newer package can never crash a daemon at
# load time; GOALS_SCHEMA_RE is the read gate, not SUPPORTED_SCHEMAS. Writes
# keep the file's own schema label and refuse a file newer than SCHEMA unless
# the caller explicitly migrates (see GoalsSchemaNewerError).
GOALS_SCHEMA_RE = re.compile(r"^chitra\.goals\.v([0-9]+)$")
GOALS_SCHEMA_NEWER_MESSAGE = "goals schema newer than installed package"

INTERVIEW_QUESTION_IDS: tuple[str, ...] = ("intent", "done_when", "out_of_scope", "constraints")
INTERVIEW_QUESTIONS: dict[str, str] = {
    "intent": "What outcome is intended, and for whom?",
    "done_when": "What concrete artifacts or checks prove the goal is done?",
    "out_of_scope": "What work is explicitly out of scope?",
    "constraints": "What constraints apply, including tools, order, spend, and approvals?",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Shared hold_reason convention: a hold_reason starting with this prefix
# (e.g. "rate-limit:5h") marks a timed pause driven by provider usage
# thresholds (see chitra.rate_limit_guard), distinct from an operator- or
# throttle-initiated hold. goals.py itself stays decision-free -- this is
# just the string convention two callers (chitra.rate_limit_guard, which
# sets it, and chitra.dispatchd, which reads it to freeze a held session's
# queue) need to agree on.
RATE_LIMIT_HOLD_REASON_PREFIX = "rate-limit:"
LOAD_SHED_HOLD_REASON_PREFIX = "load-shed:"
DONE_STATUSES = frozenset(("done-pending-verification", "done-pending-close"))
LEGACY_ENROLLED_AT = "1970-01-01T00:00:00+00:00"


class GoalValidationError(ValueError):
    """Raised when a goal record is not valid monitor doctrine."""


class GoalRedirectRequiredError(GoalValidationError):
    """Raised when a strategic goal revision must use the redirect path."""


class EnrolledScopeImmutableError(GoalValidationError):
    """Raised when a write attempts to replace a lane's enrollment anchor."""


class GoalNotFoundError(KeyError):
    """Raised when an operation requires a goal record that is absent."""


class GoalsSchemaNewerError(ValueError):
    """Raised when a write would rewrite a goals.json newer than this package."""


@pydantic_dataclass(frozen=True, slots=True, config=ConfigDict(strict=True))
class InterviewReceipt:
    """Immutable proof that all four enrollment questions were answered."""

    name: str
    completed_at: str
    answers_sha256: str
    provenance: tuple[str, ...]


@pydantic_dataclass(frozen=True, slots=True, config=ConfigDict(strict=True))
class EnrolledDoneWhenItem:
    """One immutable completion condition and its exact required proof name."""

    id: str
    text: str
    validator: str
    required_receipt: str


@pydantic_dataclass(frozen=True, slots=True, config=ConfigDict(strict=True, extra="ignore"))
class GoalRecord:
    """The five canonical fields plus monitor-maintained tactical metadata.

    ``extra="ignore"`` is the persisted-read contract: fields this package
    does not know (written by a newer schema) are dropped in memory instead
    of failing the load.
    """

    session_ref: str
    goal: str
    done_when: str
    source: str
    status: SkipValidation[GoalStatus]
    lane_id: str = ""
    enrolled_done_when: str = ""
    enrolled_at: str = ""
    intent: str = ""
    scope: str = ""
    goal_version: int = 1
    goal_history: tuple[dict[str, str], ...] = ()
    now: str = ""
    last_verified: str = ""
    created_at: str = ""
    updated_at: str = ""
    open_asks: tuple[str, ...] = ()
    retired_asks: tuple[dict[str, str], ...] = ()
    needs: str = ""
    hold_reason: str = ""
    resume_at: str = ""
    # Backend-transfer linkage (chitra.goals.v2). ``successor_of`` names the
    # held original this record took over from; ``transferred_to`` names the
    # successor on the held one. Both empty on a lane that has never moved.
    successor_of: str = ""
    transferred_to: str = ""
    interview_receipt: InterviewReceipt | None = None
    enrolled_done_when_items: tuple[EnrolledDoneWhenItem, ...] = ()
    completion_proofs: tuple[CompletionEvidence, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _GOAL_RECORD_ADAPTER.dump_python(self, mode="json"))

    @classmethod
    def from_dict(cls, payload: object, *, legacy_enrolled_at: str = "") -> GoalRecord:
        return _GOAL_RECORD_ADAPTER.validate_python(
            payload,
            strict=False,
            context={"persisted": True, "legacy_enrolled_at": legacy_enrolled_at},
        )

    @model_validator(mode="before")
    @classmethod
    def validate_persisted(cls, payload: object, info: ValidationInfo) -> object:
        """Validate required fields and apply the exact v1 legacy defaults."""
        if not info.context or not info.context.get("persisted"):
            return payload
        if not isinstance(payload, dict):
            raise ValueError("goal record must be an object")
        normalized = dict(payload)
        required_fields = (
            "session_ref",
            "goal",
            "done_when",
            "source",
            "status",
            "now",
            "last_verified",
            "created_at",
            "updated_at",
        )
        for field in required_fields:
            value = payload.get(field)
            if not isinstance(value, str):
                raise ValueError(f"goal record {field} must be a string")
            normalized[field] = value
        for field in (
            "lane_id",
            "enrolled_done_when",
            "enrolled_at",
            "intent",
            "scope",
            "needs",
            "hold_reason",
            "resume_at",
            "successor_of",
            "transferred_to",
        ):
            value = payload.get(field, "")
            if not isinstance(value, str):
                raise ValueError(f"goal record {field} must be a string")
            normalized[field] = value
        session_ref = cast(str, normalized["session_ref"])
        done_when = cast(str, normalized["done_when"])
        created_at = cast(str, normalized["created_at"])
        normalized["lane_id"] = normalized["lane_id"] or lane_id_from_session_ref(session_ref)
        normalized["enrolled_done_when"] = normalized["enrolled_done_when"] or done_when
        legacy_enrolled_at = info.context.get("legacy_enrolled_at", "")
        if not isinstance(legacy_enrolled_at, str):
            legacy_enrolled_at = ""
        normalized["enrolled_at"] = normalized["enrolled_at"] or created_at or legacy_enrolled_at or LEGACY_ENROLLED_AT
        raw_open_asks = payload.get("open_asks", [])
        if not isinstance(raw_open_asks, list) or not all(isinstance(ask, str) for ask in raw_open_asks):
            raise ValueError("goal record open_asks must be a list of strings")
        normalized["open_asks"] = raw_open_asks
        raw_retired_asks = payload.get("retired_asks", [])
        retirement_fields = {"ask", "state", "basis", "citation", "authority", "retired_at"}
        if not isinstance(raw_retired_asks, list):
            raise ValueError("goal record retired_asks must be a list of objects")
        for entry in raw_retired_asks:
            if not isinstance(entry, dict) or set(entry) != retirement_fields:
                raise ValueError("goal record retired_asks entries must contain the complete retirement record")
            if not all(isinstance(value, str) for value in entry.values()):
                raise ValueError("goal record retired_asks entries must contain strings")
            if entry["state"] not in ("resolved-by-operator", "retired-by-monitor-with-cited-basis"):
                raise ValueError("goal record retired ask state is invalid")
        normalized["retired_asks"] = raw_retired_asks
        goal_version = payload.get("goal_version", 1)
        if not isinstance(goal_version, int) or isinstance(goal_version, bool):
            raise ValueError("goal record goal_version must be an integer")
        normalized["goal_version"] = goal_version
        raw_history = payload.get("goal_history", [])
        if not isinstance(raw_history, list):
            raise ValueError("goal record goal_history must be a list of objects")
        strategic_fields = {"goal", "done_when", "intent", "scope", "revised_at", "reason"}
        review_restart_fields = {
            "event",
            "previous_contract_id",
            "restarted_contract_id",
            "behavior_sha256",
            "revised_at",
            "reason",
        }
        for entry in raw_history:
            if not isinstance(entry, dict) or set(entry) not in (strategic_fields, review_restart_fields):
                raise ValueError("goal record goal_history entries must contain strategic prior values or a review restart event")
            if not all(isinstance(value, str) for value in entry.values()):
                raise ValueError("goal record goal_history entries must contain strings")
        normalized["goal_history"] = raw_history
        normalized["interview_receipt"] = payload.get("interview_receipt")
        for field in ("enrolled_done_when_items", "completion_proofs"):
            raw_items = payload.get(field, [])
            if not isinstance(raw_items, list):
                raise ValueError(f"goal record {field} must be a list")
            normalized[field] = raw_items
        return normalized


_GOAL_RECORD_ADAPTER = TypeAdapter(GoalRecord)


def session_host(session_ref: str) -> str:
    """Return the host component, gracefully retaining a bare token."""
    return session_ref.split(":", 1)[0]


def session_name(session_ref: str) -> str:
    """Return the session component, or a bare token when no host is known."""
    parts = session_ref.split(":")
    return parts[1] if len(parts) >= 2 and parts[1] else parts[0]


def lane_id_from_session_ref(session_ref: str) -> str:
    """Return the durable lane name without host or volatile instance suffix."""
    return session_name(session_ref)


def render_done_when_items(items: Sequence[EnrolledDoneWhenItem]) -> str:
    """Render the human display from the frozen item inventory."""
    return "; ".join(item.text.strip() for item in items)


def validate_enrollment_contract(rec: GoalRecord) -> list[str]:
    """Return violations of the v3 atomic interview enrollment contract."""
    issues: list[str] = []
    receipt = rec.interview_receipt
    if receipt is None:
        issues.append("interview_receipt is required")
    else:
        if not receipt.name.strip():
            issues.append("interview_receipt name must be non-empty")
        try:
            parse_iso8601(
                receipt.completed_at,
                invalid_message="interview_receipt completed_at must be an ISO8601 datetime",
                timezone_message="interview_receipt completed_at must include a timezone",
                require_timezone=True,
            )
        except ValueError as exc:
            issues.append(str(exc))
        if _SHA256_RE.fullmatch(receipt.answers_sha256) is None:
            issues.append("interview_receipt answers_sha256 must be a lowercase SHA-256 digest")
        if len(receipt.provenance) != len(INTERVIEW_QUESTION_IDS) or any(
            not item.startswith(("operator:", "source:")) or not item.partition(":")[2].strip() for item in receipt.provenance
        ):
            issues.append("interview_receipt provenance must contain four operator: or source: entries")
    if not rec.enrolled_done_when_items:
        issues.append("enrolled_done_when_items must contain at least one item")
    seen_ids: set[str] = set()
    for item in rec.enrolled_done_when_items:
        if not item.id.strip():
            issues.append("enrolled done item id must be non-empty")
        elif item.id in seen_ids:
            issues.append(f"enrolled done item id {item.id!r} is duplicated")
        seen_ids.add(item.id)
        if not item.text.strip():
            issues.append(f"enrolled done item {item.id!r} text must be non-empty")
        if not item.validator.strip():
            issues.append(f"enrolled done item {item.id!r} validator must be non-empty")
        if not item.required_receipt.strip():
            issues.append(f"enrolled done item {item.id!r} required_receipt must be non-empty")
    rendered = render_done_when_items(rec.enrolled_done_when_items)
    if rec.enrolled_done_when_items and rec.done_when.strip() != rendered:
        issues.append("done_when must be generated from enrolled_done_when_items")
    if rec.enrolled_done_when and rec.enrolled_done_when != rendered:
        issues.append("enrolled_done_when must be generated from enrolled_done_when_items")
    return issues


def validate_goal(rec: GoalRecord, *, require_enrollment: bool = True) -> list[str]:
    """Return deterministic doctrine violations for a prospective goal record."""
    issues: list[str] = []
    if not rec.goal.strip():
        issues.append("goal must be non-empty")
    elif len(rec.goal.split()) < 6:
        issues.append("goal must contain at least six words")
    if not rec.done_when.strip():
        issues.append("done_when must be non-empty")
    if not rec.source.strip():
        issues.append("source must be non-empty")
    if rec.status not in GOAL_STATUSES:
        issues.append(f"status must be one of {', '.join(GOAL_STATUSES)}")
    if require_enrollment:
        issues.extend(validate_enrollment_contract(rec))
    return issues


def check_specification(rec: GoalRecord) -> list[str]:
    """Return stricter deterministic interview-bypass criteria for a goal."""
    issues: list[str] = []
    if not rec.intent.strip() or len(rec.intent.split()) < 8:
        issues.append("intent must be non-empty and contain at least eight words")
    if len(rec.goal.split()) < 6:
        issues.append("goal must contain at least six words")
    if not rec.done_when.strip() or len(rec.done_when.split()) < 5:
        issues.append("done_when must be non-empty and contain at least five words")
    if not rec.scope.strip() or len(rec.scope.split()) < 4:
        issues.append("scope must be non-empty and contain at least four words")
    if not rec.source.startswith(("task-file", "branch", "transcript-first-msg")):
        issues.append("source must start with task-file, branch, or transcript-first-msg")
    issues.extend(validate_enrollment_contract(rec))
    return issues


def goals_path(root: Path | None = None) -> Path:
    """Return the persistent goals document path for ``root``."""
    return (state_dir() if root is None else root) / "goals.json"


def _schema_version(schema: object) -> int | None:
    """Return the numeric version of a chitra.goals.v<N> string, else None."""
    if not isinstance(schema, str):
        return None
    match = GOALS_SCHEMA_RE.fullmatch(schema)
    return int(match.group(1)) if match else None


def _installed_schema_version() -> int:
    version = _schema_version(SCHEMA)
    assert version is not None
    return version


def stored_file_schema(root: Path | None = None) -> str:
    """Return this store's on-disk schema string without validating records.

    Returns "" when the store does not exist yet or its document does not
    carry a chitra.goals.v<N> schema.
    """
    try:
        payload: Any = json.loads(goals_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    schema = payload.get("schema")
    return schema if isinstance(schema, str) and _schema_version(schema) is not None else ""


def schema_is_newer_than_installed(schema: str) -> bool:
    """Return whether a chitra.goals.v<N> string is newer than this package."""
    version = _schema_version(schema)
    return version is not None and version > _installed_schema_version()


def goals_schema_newer_than_installed(root: Path | None = None) -> str | None:
    """Return the store's file schema when it is newer than this package, else None.

    Daemons use this to notice a store they must treat as read-only and to
    journal that fact instead of exiting into a supervisor restart loop.
    """
    schema = stored_file_schema(root)
    return schema if schema_is_newer_than_installed(schema) else None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def load_goals_document(root: Path | None = None) -> tuple[list[GoalRecord], str]:
    """Load records plus the file's own ``file_schema``, backfilling legacy anchors.

    Any ``chitra.goals.v<N>`` document loads; unknown top-level and per-record
    fields are ignored in memory so a newer writer's file stays readable here
    instead of crashing the reader. The returned second element is the file's
    own schema label, which writers must keep unless an explicit migration
    upgrades it (see ``GoalsSchemaNewerError``).

    Records written before enrollment anchors existed use their current
    ``done_when`` once, and persist that normalized anchor on their next write.
    """
    path = goals_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], ""
    if not isinstance(payload, dict):
        raise ValueError("goals.json must be an object")
    schema = payload.get("schema")
    if not isinstance(schema, str) or _schema_version(schema) is None:
        raise ValueError(f"goals.json schema must match {GOALS_SCHEMA_RE.pattern}, got {schema!r}")
    raw_goals = payload.get("goals")
    if not isinstance(raw_goals, list):
        raise ValueError("goals.json goals must be a list")
    document_updated_at = payload.get("updated_at", "")
    if not isinstance(document_updated_at, str):
        raise ValueError("goals.json updated_at must be a string")
    records = [GoalRecord.from_dict(item, legacy_enrolled_at=document_updated_at) for item in raw_goals]
    return records, schema


def load_goals(root: Path | None = None) -> list[GoalRecord]:
    """Load records, backfilling legacy enrollment anchors in memory.

    Records written before enrollment anchors existed use their current
    ``done_when`` once, and persist that normalized anchor on their next write.
    """
    records, _ = load_goals_document(root)
    return records


def _write_goals(root: Path | None, records: list[GoalRecord], *, migrate: bool = False) -> None:
    """Atomically rewrite the store, keeping its own schema label.

    A file labeled with a schema newer than this package's SCHEMA is refused
    unless ``migrate`` is set: an installed 0.x daemon must never silently
    rewrite a document it cannot fully understand. Any other readable file
    keeps its own label -- writes never bump the schema implicitly.
    """
    path = goals_path(root)
    file_schema = stored_file_schema(root)
    target_schema = SCHEMA
    if file_schema:
        if schema_is_newer_than_installed(file_schema):
            if not migrate:
                raise GoalsSchemaNewerError(
                    f"goals.json file schema {file_schema} is newer than installed package schema {SCHEMA}; "
                    "refusing to rewrite it without --migrate"
                )
        else:
            target_schema = file_schema
    payload = {"schema": target_schema, "updated_at": _utc_now(), "goals": [record.to_dict() for record in records]}
    write_json_atomic(path, payload)


def upsert_goal(
    root: Path | None,
    rec: GoalRecord,
    *,
    clear_open_asks: bool = False,
    migrate: bool = False,
) -> GoalRecord:
    """Validate and atomically insert or update one record by ``session_ref``.

    An update with no incoming asks preserves any stored asks, so routine
    status or ``now`` revisions cannot silently retire an operator request.
    Pass ``clear_open_asks=True`` only for an explicit retirement path.
    Pass ``migrate=True`` only for an operator-approved rewrite of a store
    whose file schema is newer than this package's SCHEMA.
    """
    issues = validate_goal(rec, require_enrollment=False)
    if issues:
        raise GoalValidationError("; ".join(issues))
    with locked_json_store(goals_path(root)):
        stored = _upsert_goal_locked(root, rec, clear_open_asks=clear_open_asks, migrate=migrate)
    logger.info("goal_mutated", session_ref=stored.session_ref, action="upsert")
    return stored


def _upsert_goal_locked(
    root: Path | None,
    rec: GoalRecord,
    *,
    clear_open_asks: bool = False,
    allow_strategic_change: bool = False,
    allow_goal_metadata_change: bool = False,
    allow_done_transition: bool = False,
    allow_completion_proofs: bool = False,
    allow_legacy_administrative: bool = False,
    mutation_time: str | None = None,
    migrate: bool = False,
) -> GoalRecord:
    """The body of ``upsert_goal``, assuming the caller already holds
    ``locked_json_store``. Callers that must read-then-modify an existing
    record (``add_ask``, ``resolve_ask``, ``hold_goal``, ``resume_goal``,
    ``update_now``) take the lock ONCE around their own read AND this write
    so the read cannot go stale before the write lands -- calling the public
    ``upsert_goal`` (which re-acquires the lock itself) from inside an
    already-locked section would either deadlock (a second ``flock`` from
    the same process on a fresh fd still blocks on the first) or, worse,
    leave a real window between the caller's own read and the write. See
    docs/SOL-ADVERSARIAL-REVIEW finding #9.
    """
    records = load_goals(root)
    existing = next((record for record in records if record.session_ref == rec.session_ref), None)
    if existing is not None and validate_enrollment_contract(existing) and not allow_legacy_administrative:
        raise GoalValidationError("legacy goals are display-only; use a reasoned administrative redirect or discard")
    if existing is not None and not _strategic_fields_match(existing, rec) and not allow_strategic_change:
        raise GoalRedirectRequiredError("strategic goal fields changed; use chitra-goals redirect --reason ...")
    if existing is None:
        enrollment_issues = validate_enrollment_contract(rec)
        if enrollment_issues:
            raise GoalValidationError("; ".join(enrollment_issues))
    now = _utc_now() if mutation_time is None else mutation_time
    derived_lane_id = lane_id_from_session_ref(rec.session_ref)
    lane_id = derived_lane_id
    if existing is not None:
        if rec.lane_id.strip() and rec.lane_id.strip() != existing.lane_id:
            raise GoalValidationError("lane_id is immutable once a goal is enrolled")
        lane_id = existing.lane_id
        if rec.enrolled_done_when and rec.enrolled_done_when != existing.enrolled_done_when:
            raise EnrolledScopeImmutableError("enrolled_done_when is immutable once a goal is enrolled")
        if rec.enrolled_at and rec.enrolled_at != existing.enrolled_at:
            raise EnrolledScopeImmutableError("enrolled_at is immutable once a goal is enrolled")
        if rec.interview_receipt != existing.interview_receipt:
            raise EnrolledScopeImmutableError("interview_receipt is immutable once a goal is enrolled")
        if rec.enrolled_done_when_items != existing.enrolled_done_when_items:
            raise EnrolledScopeImmutableError("enrolled_done_when_items are immutable once a goal is enrolled")
        if rec.completion_proofs != existing.completion_proofs and not allow_completion_proofs:
            raise GoalValidationError("completion proofs may only be written by the completion gate")
        enrolled_done_when = existing.enrolled_done_when
        enrolled_at = existing.enrolled_at
    else:
        if rec.lane_id.strip() and rec.lane_id.strip() != derived_lane_id:
            raise GoalValidationError("lane_id must be derived from the durable session name")
        rendered_done_when = render_done_when_items(rec.enrolled_done_when_items)
        if rec.enrolled_done_when and rec.enrolled_done_when != rendered_done_when:
            raise EnrolledScopeImmutableError("enrolled_done_when must be generated from enrolled_done_when_items")
        enrolled_done_when = rendered_done_when
        enrolled_at = now
    conflicting_lane = next(
        (record for record in records if record.session_ref != rec.session_ref and record.lane_id == lane_id),
        None,
    )
    if conflicting_lane is not None:
        raise GoalRedirectRequiredError(
            f"lane {lane_id!r} already has an open goal at {conflicting_lane.session_ref}; use chitra-goals redirect --reason ..."
        )
    if rec.status in DONE_STATUSES and (existing is None or rec.status != existing.status) and not allow_done_transition:
        raise GoalValidationError("done-* status transitions require the completion gate")
    open_asks = rec.open_asks
    if existing is not None and not open_asks and existing.open_asks and not clear_open_asks:
        open_asks = existing.open_asks
    hold_reason = rec.hold_reason
    resume_at = rec.resume_at
    if existing is not None and rec.status == existing.status and not hold_reason and not resume_at:
        hold_reason = existing.hold_reason
        resume_at = existing.resume_at
    stored = GoalRecord(
        session_ref=rec.session_ref,
        goal=rec.goal,
        done_when=rec.done_when,
        source=rec.source,
        status=rec.status,
        lane_id=lane_id,
        enrolled_done_when=enrolled_done_when,
        enrolled_at=enrolled_at,
        intent=rec.intent,
        scope=rec.scope,
        goal_version=(rec.goal_version if existing is None or allow_goal_metadata_change else existing.goal_version),
        goal_history=(rec.goal_history if existing is None or allow_goal_metadata_change else existing.goal_history),
        now=rec.now,
        last_verified=rec.last_verified,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
        open_asks=open_asks,
        retired_asks=rec.retired_asks,
        needs=rec.needs,
        hold_reason=hold_reason,
        resume_at=resume_at,
        # Transfer linkage is a historical fact, so it only ever gets set, not
        # cleared. A routine status or ``now`` write that says nothing about
        # the link must not quietly sever it -- an original whose
        # ``transferred_to`` vanished would look transferable a second time,
        # and two lanes on one branch is the collision the doctrine forbids.
        successor_of=rec.successor_of or (existing.successor_of if existing is not None else ""),
        transferred_to=rec.transferred_to or (existing.transferred_to if existing is not None else ""),
        interview_receipt=rec.interview_receipt,
        enrolled_done_when_items=rec.enrolled_done_when_items,
        completion_proofs=rec.completion_proofs,
    )
    records = [record for record in records if record.session_ref != rec.session_ref]
    records.append(stored)
    _write_goals(root, records, migrate=migrate)
    return stored


def _strategic_fields_match(left: GoalRecord, right: GoalRecord) -> bool:
    """Compare strategic fields while treating whitespace-only revisions alike."""
    return all(
        getattr(left, field).strip() == getattr(right, field).strip() for field in ("goal", "done_when", "intent", "scope", "source")
    )


def redirect_goal(
    root: Path | None,
    session_ref: str,
    *,
    reason: str,
    goal: str | None = None,
    done_when: str | None = None,
    intent: str | None = None,
    scope: str | None = None,
    source: str | None = None,
) -> GoalRecord:
    """Replace strategic values after recording the prior operator direction."""
    if not reason.strip():
        raise ValueError("redirect reason must be non-empty")
    with locked_json_store(goals_path(root)):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        if done_when is not None and done_when.strip() != existing.done_when.strip():
            raise EnrolledScopeImmutableError("done_when is frozen from enrolled_done_when_items and cannot be redirected")
        redirected = replace(
            existing,
            goal=existing.goal if goal is None else goal,
            done_when=existing.done_when if done_when is None else done_when,
            intent=existing.intent if intent is None else intent,
            scope=existing.scope if scope is None else scope,
            source=existing.source if source is None else source,
            status="working" if existing.status in DONE_STATUSES else existing.status,
            last_verified="" if existing.status in DONE_STATUSES else existing.last_verified,
        )
        if _strategic_fields_match(existing, redirected):
            raise ValueError("redirect must change at least one strategic field")
        issues = validate_goal(redirected, require_enrollment=existing.interview_receipt is not None)
        if issues:
            raise GoalValidationError("; ".join(issues))
        revised_at = _utc_now()
        history_entry = {
            "goal": existing.goal,
            "done_when": existing.done_when,
            "intent": existing.intent,
            "scope": existing.scope,
            "revised_at": revised_at,
            "reason": reason,
        }
        candidate = replace(
            redirected,
            goal_version=existing.goal_version + 1,
            goal_history=(*existing.goal_history, history_entry),
            created_at=existing.created_at,
            updated_at=revised_at,
        )
        stored = _upsert_goal_locked(
            root,
            candidate,
            allow_strategic_change=True,
            allow_goal_metadata_change=True,
            allow_legacy_administrative=True,
            mutation_time=revised_at,
        )
    logger.info("goal_mutated", session_ref=session_ref, action="administrative-redirect-not-done", reason=reason)
    return stored


def record_review_restart(
    root: Path | None,
    session_ref: str,
    *,
    previous_contract_id: str,
    restarted_contract_id: str,
    behavior_sha256: str,
) -> GoalRecord:
    """Append the required revert trail for an automatic redirect restart.

    This is monitor-owned history, not an operator ask or a lane message. It
    deliberately leaves every strategic field and the goal version unchanged;
    ``redirect_goal`` already recorded the strategic revision itself.
    """
    with locked_json_store(goals_path(root)):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        event = {
            "event": "adversarial-review-redirect-restart",
            "previous_contract_id": previous_contract_id,
            "restarted_contract_id": restarted_contract_id,
            "behavior_sha256": behavior_sha256,
            "revised_at": _utc_now(),
            "reason": "goal redirected during watched-session review; automatically restarted with one reviewer",
        }
        revised_at = event["revised_at"]
        stored = _upsert_goal_locked(
            root,
            replace(existing, goal_history=(*existing.goal_history, event), updated_at=revised_at),
            allow_goal_metadata_change=True,
            mutation_time=revised_at,
        )
    logger.info("goal_review_restarted", session_ref=session_ref, behavior_sha256=behavior_sha256)
    return stored


def update_now(
    root: Path | None,
    session_ref: str,
    *,
    now: str | None = None,
    status: GoalStatus | None = None,
    last_verified: str | None = None,
    migrate: bool = False,
) -> GoalRecord:
    """Update only the current tactical state of an existing goal record."""
    if status in DONE_STATUSES:
        raise GoalValidationError("update_now cannot set a done-* status; use the completion-gate path")
    with locked_json_store(goals_path(root)):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        stored = _upsert_goal_locked(
            root,
            replace(
                existing,
                now=existing.now if now is None else now,
                status=existing.status if status is None else status,
                last_verified=existing.last_verified if last_verified is None else last_verified,
            ),
            migrate=migrate,
        )
    logger.info("goal_mutated", session_ref=stored.session_ref, action="upsert")
    return stored


def mark_completion_gate_passed(
    root: Path | None,
    session_ref: str,
    *,
    now: str,
    last_verified: str,
    completion_evidence: Sequence[CompletionEvidence],
) -> GoalRecord:
    """Record Watchd's already-passed completion audit and goal review."""
    with locked_json_store(goals_path(root)):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        enrollment_issues = validate_enrollment_contract(existing)
        if enrollment_issues:
            raise GoalValidationError("completion requires a valid interview enrollment: " + "; ".join(enrollment_issues))
        try:
            verified_evidence = require_verified_completion_receipts(
                root,
                existing.session_ref,
                existing.enrolled_done_when_items,
                completion_evidence,
            )
            require_completion_receipts(existing.enrolled_done_when_items, verified_evidence)
        except (ReceiptError, ValueError) as exc:
            raise GoalValidationError(str(exc)) from exc
        stored = _upsert_goal_locked(
            root,
            replace(
                existing,
                now=now,
                status="done-pending-close",
                last_verified=last_verified,
                completion_proofs=verified_evidence,
            ),
            allow_done_transition=True,
            allow_completion_proofs=True,
        )
    logger.info("goal_mutated", session_ref=stored.session_ref, action="completion-gate-passed")
    return stored


def hold_goal(root: Path | None, session_ref: str, *, reason: str, resume_at: str = "") -> GoalRecord:
    """Mark an existing lane held while retaining its re-arm payload."""
    if not reason.strip():
        raise ValueError("hold reason must be non-empty")
    if resume_at:
        parse_iso8601(
            resume_at,
            invalid_message="resume_at must be an ISO8601 datetime",
            timezone_message="resume_at must be an ISO8601 datetime with timezone",
            require_timezone=True,
        )
    with locked_json_store(goals_path(root)):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        held = _upsert_goal_locked(root, replace(existing, status="held", hold_reason=reason, resume_at=resume_at))
    logger.info("goal_mutated", session_ref=session_ref, action="hold")
    return held


def successor_session_ref(session_ref: str, taken: Iterable[str]) -> str:
    """Return the next free ``<host>:<lane>-xfer[n]:<pane>`` for one lane."""
    parts = session_ref.split(":")
    host = parts[0] if len(parts) >= 2 else ""
    lane = session_name(session_ref)
    pane = parts[2] if len(parts) >= 3 else ""
    existing = set(taken)
    for attempt in range(1, 100):
        suffix = "-xfer" if attempt == 1 else f"-xfer{attempt}"
        candidate = ":".join(part for part in (host, f"{lane}{suffix}", pane) if part)
        if candidate not in existing:
            return candidate
    raise GoalValidationError(f"{session_ref} has already been transferred 99 times; this needs a person")


def transfer_goal(
    root: Path | None,
    session_ref: str,
    *,
    to_backend: str,
    digest: str,
    reason: str,
    resume_at: str = "",
) -> tuple[GoalRecord, GoalRecord]:
    """Hold a lane and scaffold its successor on the other backend, atomically.

    Returns ``(held_original, successor)``. This writes records only. It starts
    no pane and no job: ``chitra-goals check`` remains the standing ingestion
    gate, and launch happens after it passes.

    Strategic fields transfer verbatim. A backend swap is tactical -- the lane
    is doing the same work for the same reason -- so ``goal``, ``done_when``,
    ``intent`` and ``scope`` are copied unchanged, and only ``source`` grows,
    by the handoff digest appended to it.

    Both writes happen under one lock. A hold that landed without its successor
    would leave the work stopped with nothing recorded to pick it up, which is
    the failure this verb exists to prevent.
    """
    if to_backend not in ("claude", "codex"):
        raise GoalValidationError("to_backend must be claude or codex")
    if not digest.strip():
        raise GoalValidationError("digest must be non-empty; the successor needs its handoff context")
    if not reason.strip():
        raise GoalValidationError("transfer reason must be non-empty")
    if resume_at:
        parse_iso8601(
            resume_at,
            invalid_message="resume_at must be an ISO8601 datetime",
            timezone_message="resume_at must be an ISO8601 datetime with timezone",
            require_timezone=True,
        )
    with locked_json_store(goals_path(root)):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        if existing.transferred_to:
            # Two successors driving one branch is the shared-worktree
            # collision, arrived at by a different road. Refuse loudly.
            raise GoalValidationError(
                f"{session_ref} was already transferred to {existing.transferred_to}; "
                "close or resume that successor rather than forking a second one"
            )
        successor_ref = successor_session_ref(session_ref, [record.session_ref for record in load_goals(root)])
        successor = GoalRecord(
            session_ref=successor_ref,
            goal=existing.goal,
            done_when=existing.done_when,
            source=f"{existing.source}; digest:{digest.strip()}",
            status="idle",
            intent=existing.intent,
            scope=existing.scope,
            interview_receipt=existing.interview_receipt,
            enrolled_done_when_items=existing.enrolled_done_when_items,
            successor_of=session_ref,
            now=f"scaffolded by transfer to {to_backend}: {reason}",
        )
        issues = validate_goal(successor)
        if issues:
            raise GoalValidationError("; ".join(issues))
        held = _upsert_goal_locked(
            root,
            replace(
                existing,
                status="held",
                hold_reason=reason,
                resume_at=resume_at,
                transferred_to=successor_ref,
            ),
        )
        stored_successor = _upsert_goal_locked(root, successor)
    logger.info(
        "goal_mutated",
        session_ref=session_ref,
        action="transfer",
        successor_ref=successor_ref,
        to_backend=to_backend,
    )
    return held, stored_successor


def resume_goal(root: Path | None, session_ref: str) -> GoalRecord:
    """Return an explicitly held lane to working state and clear hold metadata."""
    with locked_json_store(goals_path(root)):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        if existing.status != "held":
            raise ValueError("goal is not held")
        resumed = _upsert_goal_locked(root, replace(existing, status="working", hold_reason="", resume_at=""))
    logger.info("goal_mutated", session_ref=session_ref, action="resume")
    return resumed


def due_goals(root: Path | None = None, *, now: datetime | None = None) -> list[GoalRecord]:
    """Return timed held lanes due at or before ``now`` in stable operator order."""
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    due: list[tuple[datetime, GoalRecord]] = []
    for record in load_goals(root):
        if record.status != "held" or not record.resume_at:
            continue
        resume_at = parse_iso8601(
            record.resume_at,
            invalid_message="resume_at must be an ISO8601 datetime",
            timezone_message="resume_at must be an ISO8601 datetime with timezone",
            require_timezone=True,
        )
        if resume_at <= current:
            due.append((resume_at, record))
    return [record for _, record in sorted(due, key=lambda item: (item[0], item[1].session_ref))]


def add_ask(root: Path | None, session_ref: str, ask: str) -> GoalRecord:
    """Persist one exact open operator ask for an existing lane, once only."""
    with locked_json_store(goals_path(root)):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        if ask in existing.open_asks:
            return existing
        stored = _upsert_goal_locked(root, replace(existing, open_asks=(*existing.open_asks, ask)))
    logger.info("goal_mutated", session_ref=stored.session_ref, action="upsert")
    return stored


def resolve_ask(
    root: Path | None,
    session_ref: str,
    *,
    ask: str | None = None,
    index: int | None = None,
    all: bool = False,
    retired_by: Literal["operator", "monitor"] = "operator",
    basis: str = "Operator answered the ask.",
    citation: str = "operator-ruling",
    authority: str = "operator",
) -> GoalRecord:
    """Retire asks while preserving who decided and the cited basis."""
    selector_count = int(ask is not None) + int(index is not None) + int(all)
    if selector_count != 1:
        raise ValueError("select exactly one of ask, index, or all")
    with locked_json_store(goals_path(root)):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        if all:
            remaining: tuple[str, ...] = ()
            removed = existing.open_asks
        elif ask is not None:
            if ask not in existing.open_asks:
                raise ValueError("open ask was not found")
            remaining = tuple(item for item in existing.open_asks if item != ask)
            removed = (ask,)
        else:
            assert index is not None
            if index < 0 or index >= len(existing.open_asks):
                raise ValueError("open ask index is out of range")
            remaining = existing.open_asks[:index] + existing.open_asks[index + 1 :]
            removed = (existing.open_asks[index],)
        if retired_by == "monitor" and (not basis.strip() or not citation.strip() or not authority.strip()):
            raise ValueError("monitor retirement requires a non-empty basis, citation, and authority")
        if retired_by == "monitor":
            require_plain_english(basis, field="ask-retirement basis")
            require_plain_english(authority, field="ask-retirement authority")
        state = "retired-by-monitor-with-cited-basis" if retired_by == "monitor" else "resolved-by-operator"
        retired_at = _utc_now()
        retirements = tuple(
            {"ask": item, "state": state, "basis": basis, "citation": citation, "authority": authority, "retired_at": retired_at}
            for item in removed
        )
        stored = _upsert_goal_locked(
            root,
            replace(existing, open_asks=remaining, retired_asks=(*existing.retired_asks, *retirements)),
            clear_open_asks=True,
        )
    logger.info("goal_mutated", session_ref=stored.session_ref, action="upsert")
    return stored


def get_goal(root: Path | None, session_ref: str) -> GoalRecord | None:
    """Return the record for ``session_ref``, if the monitor has stored one."""
    return next((record for record in load_goals(root) if record.session_ref == session_ref), None)


def list_goals(root: Path | None = None) -> list[GoalRecord]:
    """Return all current goal records in their persisted order."""
    return load_goals(root)


def descope_delta(record: GoalRecord) -> tuple[RequiredItem, ...]:
    """Return every enrolled/history item absent from the current condition."""
    return _recorded_descopes(
        record.enrolled_done_when or record.done_when,
        record.done_when,
        goal_history=record.goal_history,
    )


def done_when_with_delta(record: GoalRecord) -> str:
    """Render current conditions without hiding any dropped enrolled items."""
    dropped = descope_delta(record)
    if not dropped:
        return record.done_when
    return f"{record.done_when} (dropping: {', '.join(item.text for item in dropped)})"


def close_goal(
    root: Path | None,
    session_ref: str,
    *,
    delivered_items: Sequence[str] = (),
    completion_evidence: Sequence[CompletionEvidence] = (),
    close_notes: Sequence[str] = (),
    operator_acknowledged_items: Sequence[str] = (),
    administrative: bool = False,
    administrative_reason: str = "",
) -> GoalRecord:
    """Remove a record.

    A completion close (the default) must first satisfy the operator-stated
    inventory diff. An ``administrative`` close is a discard of a dead lane
    (e.g. a superseded hold being reconciled by the sweep janitor), NOT a
    completion claim, so the delivery-inventory gate does not apply to it.
    """
    with locked_json_store(goals_path(root)):
        records = load_goals(root)
        closed = next((record for record in records if record.session_ref == session_ref), None)
        if closed is None:
            raise GoalNotFoundError(session_ref)
        if administrative:
            if not administrative_reason.strip():
                raise GoalValidationError("administrative discard requires a non-empty reason and is not a completion close")
        else:
            if delivered_items or operator_acknowledged_items:
                raise GoalValidationError(
                    "--delivered-item and --operator-acknowledged-item are not completion receipts and cannot satisfy close"
                )
            if closed.status != "done-pending-close":
                raise GoalValidationError("completion close requires done-pending-close from the completion gate")
            enrollment_issues = validate_enrollment_contract(closed)
            if enrollment_issues:
                raise GoalValidationError("completion close requires a valid interview enrollment: " + "; ".join(enrollment_issues))
            claimed_proofs = tuple(completion_evidence) or closed.completion_proofs
            proofs = require_verified_completion_receipts(
                root,
                closed.session_ref,
                closed.enrolled_done_when_items,
                claimed_proofs,
            )
            require_structured_close_inventory(closed.enrolled_done_when_items, proofs)
        _write_goals(root, [record for record in records if record.session_ref != session_ref])
    logger.info(
        "goal_mutated",
        session_ref=session_ref,
        action="administrative-discard-not-done" if administrative else "completion-close",
        reason=administrative_reason if administrative else "",
    )
    return closed


def main(argv: list[str] | None = None) -> int:
    """Retain the historical Python entry point while keeping CLI imports out of store-core."""
    from chitra.goals_cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

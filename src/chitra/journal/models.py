"""Schema models for the append-only canonical event journal."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CanonicalType(StrEnum):
    """Transcript and externally proven lifecycle events understood by W1."""

    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_ERROR = "tool_error"
    FINAL_RESPONSE = "final_response"
    COMPACTION = "compaction"
    RESUME = "resume"
    UNKNOWN = "unknown"


class Client(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class ByteRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=0)
    end: int = Field(ge=0)


class TranscriptIdentity(BaseModel):
    """Identity of one incarnation of a transcript path."""

    model_config = ConfigDict(frozen=True)

    path: str
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    generation: int = Field(default=0, ge=0)


class RawRecord(BaseModel):
    """One complete newline-terminated source record and its byte evidence."""

    model_config = ConfigDict(frozen=True)

    transcript: TranscriptIdentity
    byte_range: ByteRange
    raw_sha256: str
    record: dict[str, Any] | None
    decode_error: str | None = None


class LifecycleReceipt(BaseModel):
    """External proof for a fact that native JSONL cannot establish."""

    model_config = ConfigDict(frozen=True)

    receipt_id: str
    event_type: Literal["resume"]
    occurred_at: str
    session_id: str
    resume_id: str
    method: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class CanonicalEvent(BaseModel):
    """One immutable normalized event."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, serialize_by_alias=True)

    schema_name: Literal["chitra.journal.event.v1"] = Field(default="chitra.journal.event.v1", alias="schema", serialization_alias="schema")
    event_id: str
    instance: str
    lane: str
    client: Client
    client_version: str
    process_id: str | None
    transcript: TranscriptIdentity
    session_id: str
    resume_id: str | None
    observed_at: str
    native_time: str | None
    native_type: str
    native_join_id: str | None
    raw_byte_range: ByteRange | None
    raw_sha256: str | None
    lifecycle_receipt: LifecycleReceipt | None = None
    normalized_type: CanonicalType
    goal_ref: str | None = None
    # A transcript event is only recovery evidence for the goal revision that
    # observed it.  ``None`` keeps older journal rows readable, but callers
    # must not treat an unbound event as evidence for a current goal.
    goal_version: int | None = Field(default=None, ge=1)
    item_ref: str | None = None
    payload_digest: str
    normalizer_version: str
    payload: dict[str, Any]
    raw_record: dict[str, Any] | None

    @field_validator("goal_version")
    @classmethod
    def reject_bool_goal_version(cls, value: int | None) -> int | None:
        if isinstance(value, bool):
            raise ValueError("goal_version must be an integer")
        return value


class ProgressClass(StrEnum):
    PROGRESS = "progress"
    NON_PROGRESS = "non_progress"
    UNKNOWN = "unknown"


class ProgressClassification(BaseModel):
    """Appendable derivation; it never replaces its source events."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, serialize_by_alias=True)

    schema_name: Literal["chitra.journal.progress.v1"] = Field(
        default="chitra.journal.progress.v1", alias="schema", serialization_alias="schema"
    )
    derivation_id: str
    classification: ProgressClass
    reason: str
    source_event_ids: tuple[str, ...]
    goal_version: str
    classifier_version: str

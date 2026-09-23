"""Canonical transcript adapters and append-only per-lane event journals."""

from .ingest import IngestResult, JournalIngestor
from .models import (
    ByteRange,
    CanonicalEvent,
    CanonicalType,
    Client,
    LifecycleReceipt,
    ProgressClass,
    ProgressClassification,
    RawRecord,
    TranscriptIdentity,
)
from .normalizers import (
    NORMALIZER_VERSION,
    NormalizationContext,
    make_normalizer,
    native_session_identity,
)
from .reader import JsonlTailReader, ReadBatch, Rotation
from .store import CLASSIFIER_VERSION, EventJournal, classify_progress, derive_progress_rows

__all__ = [
    "CLASSIFIER_VERSION",
    "NORMALIZER_VERSION",
    "ByteRange",
    "CanonicalEvent",
    "CanonicalType",
    "Client",
    "EventJournal",
    "IngestResult",
    "JournalIngestor",
    "JsonlTailReader",
    "LifecycleReceipt",
    "NormalizationContext",
    "ProgressClass",
    "ProgressClassification",
    "RawRecord",
    "ReadBatch",
    "Rotation",
    "TranscriptIdentity",
    "classify_progress",
    "derive_progress_rows",
    "make_normalizer",
    "native_session_identity",
]

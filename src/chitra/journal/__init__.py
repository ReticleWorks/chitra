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
    SUPPORTED_VERSIONS,
    NormalizationContext,
    UnsupportedClientVersion,
    make_normalizer,
    native_session_identity,
)
from .reader import JsonlTailReader, ReadBatch, Rotation
from .store import CLASSIFIER_VERSION, EventJournal, classify_progress

__all__ = [
    "CLASSIFIER_VERSION",
    "NORMALIZER_VERSION",
    "SUPPORTED_VERSIONS",
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
    "UnsupportedClientVersion",
    "classify_progress",
    "make_normalizer",
    "native_session_identity",
]

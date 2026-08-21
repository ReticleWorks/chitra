"""Compose tail reading, version-gated normalization, and journal writes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import CanonicalEvent, LifecycleReceipt
from .normalizers import NormalizationContext, TranscriptNormalizer, make_normalizer
from .reader import JsonlTailReader, Rotation
from .store import EventJournal


@dataclass(frozen=True)
class IngestResult:
    observed: tuple[CanonicalEvent, ...]
    appended: tuple[CanonicalEvent, ...]
    rotations: tuple[Rotation, ...]


class JournalIngestor:
    """Incrementally ingest one transcript into one lane's durable journal."""

    def __init__(
        self,
        *,
        state_root: Path,
        transcript_path: Path,
        context: NormalizationContext,
        chunk_size: int = 64 * 1024,
    ) -> None:
        self.reader = JsonlTailReader(transcript_path, chunk_size=chunk_size)
        self.normalizer: TranscriptNormalizer = make_normalizer(context)
        self.journal = EventJournal(state_root, context.lane)

    def close(self) -> None:
        self.reader.close()

    def __enter__(self) -> JournalIngestor:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def poll(self) -> IngestResult:
        batch = self.reader.poll()
        observed = tuple(event for record in batch.records for event in self.normalizer.normalize(record))
        appended = self.journal.append(observed)
        return IngestResult(observed=observed, appended=appended, rotations=batch.rotations)

    def record_resume(self, receipt: LifecycleReceipt) -> CanonicalEvent:
        identity = self.reader.identity
        if identity is None:
            raise RuntimeError("poll the transcript before binding a resume receipt")
        event = self.normalizer.bind_resume(receipt, identity)
        self.journal.append((event,))
        return event

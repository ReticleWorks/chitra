"""Compose tail reading, normalization, and journal writes."""

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
        self._normalizer_generation: int | None = None
        # First raw sha256 of every generation observed. An inode replacement
        # that restarts the same transcript (atomic sync rewrite) re-presents
        # a leading record this ingestor already saw, which is how a replay
        # is told apart from a genuinely different file at the same path.
        self._generation_heads: set[str] = set()

    def close(self) -> None:
        self.reader.close()

    def __enter__(self) -> JournalIngestor:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def poll(self) -> IngestResult:
        batch = self.reader.poll()
        rewritten_generations = {
            rotation.current.generation
            for rotation in batch.rotations
            if (rotation.previous.device, rotation.previous.inode)
            == (rotation.current.device, rotation.current.inode)
        }
        observed: list[CanonicalEvent] = []
        for record in batch.records:
            generation = record.transcript.generation
            if generation != self._normalizer_generation:
                # A generation is a full re-read from byte zero. For a
                # same-inode rewrite that is always a replay of the current
                # transcript. For an inode replacement it is a replay only
                # when the new file restarts the same content, detected by
                # its first record matching a generation head already seen.
                if (
                    generation in rewritten_generations
                    or record.raw_sha256 in self._generation_heads
                ):
                    self.normalizer.begin_replay()
                self._generation_heads.add(record.raw_sha256)
                self._normalizer_generation = generation
            observed.extend(self.normalizer.normalize(record))
        appended = self.journal.append(tuple(observed))
        return IngestResult(observed=tuple(observed), appended=appended, rotations=batch.rotations)

    def record_resume(self, receipt: LifecycleReceipt) -> CanonicalEvent:
        identity = self.reader.identity
        if identity is None:
            raise RuntimeError("poll the transcript before binding a resume receipt")
        event = self.normalizer.bind_resume(receipt, identity)
        self.journal.append((event,))
        return event

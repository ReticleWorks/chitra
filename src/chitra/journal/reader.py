"""Incremental JSONL reader with byte-accurate rotation handling."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .models import ByteRange, RawRecord, TranscriptIdentity


@dataclass(frozen=True)
class Rotation:
    previous: TranscriptIdentity
    current: TranscriptIdentity
    abandoned_partial_bytes: int


@dataclass(frozen=True)
class ReadBatch:
    records: tuple[RawRecord, ...]
    rotations: tuple[Rotation, ...] = ()


class JsonlTailReader:
    """Read only completed lines while retaining partial append bytes."""

    def __init__(self, path: Path, *, chunk_size: int = 64 * 1024) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self.path = path
        self.chunk_size = chunk_size
        self._handle: BinaryIO | None = None
        self._identity: TranscriptIdentity | None = None
        self._offset = 0
        self._buffer = bytearray()
        self._buffer_start = 0
        self._generation = 0
        self._anchor = b""

    @property
    def identity(self) -> TranscriptIdentity | None:
        return self._identity

    @property
    def offset(self) -> int:
        return self._offset

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
        self._handle = None

    def __enter__(self) -> JsonlTailReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def poll(self) -> ReadBatch:
        rotations: list[Rotation] = []
        if self._handle is None:
            if not self.path.exists():
                return ReadBatch(())
            self._open_current()

        path_stat = self._safe_stat()
        assert self._identity is not None
        if path_stat is None:
            return ReadBatch(tuple(self._drain()))

        current_key = (self._identity.device, self._identity.inode)
        path_key = (path_stat.st_dev, path_stat.st_ino)
        replaced = path_key != current_key
        rewritten = path_key == current_key and (path_stat.st_size < self._offset or not self._anchor_matches())
        records = self._drain() if replaced else []
        if replaced or rewritten:
            previous = self._identity
            abandoned = len(self._buffer)
            self.close()
            self._generation += 1
            self._offset = 0
            self._buffer = bytearray()
            self._buffer_start = 0
            self._anchor = b""
            self._open_current()
            assert self._identity is not None
            rotations.append(Rotation(previous, self._identity, abandoned))
            records.extend(self._drain())
        elif not replaced:
            records.extend(self._drain())

        # Catch a rename that raced the read above. The old descriptor is
        # already drained, so switching now loses no completed record.
        final_stat = self._safe_stat()
        assert self._identity is not None
        if final_stat is not None and (final_stat.st_dev, final_stat.st_ino) != (
            self._identity.device,
            self._identity.inode,
        ):
            previous = self._identity
            abandoned = len(self._buffer)
            self.close()
            self._generation += 1
            self._offset = 0
            self._buffer = bytearray()
            self._buffer_start = 0
            self._anchor = b""
            self._open_current()
            assert self._identity is not None
            rotations.append(Rotation(previous, self._identity, abandoned))
            records.extend(self._drain())
        return ReadBatch(tuple(records), tuple(rotations))

    def follow(
        self,
        *,
        stop: Callable[[], bool] | None = None,
        poll_interval: float = 0.1,
    ) -> Iterator[ReadBatch]:
        """Yield append or rotation batches until a caller-controlled stop.

        There is deliberately no elapsed-time cutoff or lease expiry. With no
        ``stop`` callback this iterator follows the path indefinitely.
        """
        if poll_interval < 0:
            raise ValueError("poll_interval cannot be negative")
        while stop is None or not stop():
            batch = self.poll()
            if batch.records or batch.rotations:
                yield batch
            elif poll_interval:
                time.sleep(poll_interval)

    def _safe_stat(self) -> os.stat_result | None:
        try:
            return self.path.stat()
        except FileNotFoundError:
            return None

    def _open_current(self) -> None:
        handle = self.path.open("rb")
        file_stat = os.fstat(handle.fileno())
        self._handle = handle
        self._identity = TranscriptIdentity(
            path=str(self.path),
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            generation=self._generation,
        )

    def _anchor_matches(self) -> bool:
        if not self._anchor:
            return True
        assert self._handle is not None
        start = self._offset - len(self._anchor)
        return os.pread(self._handle.fileno(), len(self._anchor), start) == self._anchor

    def _drain(self) -> list[RawRecord]:
        assert self._handle is not None
        assert self._identity is not None
        records: list[RawRecord] = []
        while True:
            chunk = self._handle.read(self.chunk_size)
            if not chunk:
                break
            chunk_start = self._offset
            self._offset += len(chunk)
            self._anchor = (self._anchor + chunk)[-64:]
            if not self._buffer:
                self._buffer_start = chunk_start
            self._buffer.extend(chunk)
            while True:
                newline = self._buffer.find(b"\n")
                if newline < 0:
                    break
                raw_line = bytes(self._buffer[: newline + 1])
                line_start = self._buffer_start
                line_end = line_start + len(raw_line)
                del self._buffer[: newline + 1]
                self._buffer_start = line_end
                if raw_line.strip():
                    records.append(self._decode(raw_line, line_start, line_end))
        return records

    def _decode(self, raw_line: bytes, start: int, end: int) -> RawRecord:
        digest = hashlib.sha256(raw_line).hexdigest()
        record: dict[str, Any] | None = None
        error: str | None = None
        try:
            value = json.loads(raw_line)
            if isinstance(value, dict):
                record = value
            else:
                error = "record is not a JSON object"
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = str(exc)
        assert self._identity is not None
        return RawRecord(
            transcript=self._identity,
            byte_range=ByteRange(start=start, end=end),
            raw_sha256=digest,
            record=record,
            decode_error=error,
        )

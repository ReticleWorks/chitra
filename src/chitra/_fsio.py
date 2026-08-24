"""Shared filesystem and timestamp primitives for Chitra state."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path


def fsync_directory(directory: Path) -> None:
    """Make a directory entry update durable on the containing filesystem."""

    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def locked_json_store(path: Path) -> Iterator[None]:
    """Serialize a full read-modify-write transaction for ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write_json_atomic(
    path: Path,
    obj: object,
    *,
    temporary_path: Path | None = None,
    trailing_newline: bool = True,
    sort_keys: bool = True,
    fsync: bool = False,
    cleanup_on_error: bool = True,
) -> None:
    """Serialize ``obj`` and atomically replace ``path`` with the result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with (
            tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            )
            if temporary_path is None
            else temporary_path.open("w", encoding="utf-8")
        ) as tmp:
            tmp_name = tmp.name
            json.dump(obj, tmp, indent=2, sort_keys=sort_keys)
            if trailing_newline:
                tmp.write("\n")
            if fsync:
                tmp.flush()
                os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        if fsync:
            # The replacement is durable only after the containing directory
            # entry is durable as well.  A crash after the file fsync but
            # before this directory fsync can lose the new name.
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if cleanup_on_error and tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _rooted_directory_fd(root: Path, directory: str) -> tuple[int, int]:
    """Open one state directory without following a symlink."""

    if not directory or "/" in directory or directory in {".", ".."}:
        raise ValueError("rooted directory name is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(str(root), flags)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory, 0o700, dir_fd=root_fd)
        directory_fd = os.open(directory, flags, dir_fd=root_fd)
    except Exception:
        os.close(root_fd)
        raise
    return root_fd, directory_fd


def read_json_rooted(root: Path, directory: str, name: str) -> object:
    """Read one JSON file under a no-follow, root-confined directory fd."""

    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("rooted file name is unsafe")
    root_fd, directory_fd = _rooted_directory_fd(root, directory)
    try:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                return json.load(stream)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(directory_fd)
        os.close(root_fd)


def write_json_rooted(
    root: Path,
    directory: str,
    name: str,
    obj: object,
    *,
    trailing_newline: bool = True,
    sort_keys: bool = True,
) -> None:
    """Atomically write JSON under a no-follow, root-confined directory fd."""

    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("rooted file name is unsafe")
    root_fd, directory_fd = _rooted_directory_fd(root, directory)
    temporary_name: str | None = None
    try:
        for attempt in range(100):
            candidate = f".{name}.{os.getpid()}.{attempt}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        else:
            raise OSError("could not allocate rooted JSON temporary file")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, indent=2, sort_keys=sort_keys)
            if trailing_newline:
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        temporary_name = None
    finally:
        if temporary_name is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)
        os.close(root_fd)


def parse_iso8601(
    value: str,
    *,
    invalid_message: str | None = None,
    timezone_message: str | None = None,
    require_timezone: bool = False,
    require_utc: bool = False,
    normalize_utc: bool = False,
    error_type: type[ValueError] = ValueError,
) -> datetime:
    """Parse ISO8601 while retaining each caller's validation policy."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        if invalid_message is None:
            raise
        raise error_type(invalid_message) from exc
    if require_utc and (parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed)):
        raise error_type(timezone_message or invalid_message or "datetime must use UTC")
    if require_timezone and parsed.tzinfo is None:
        raise error_type(timezone_message or invalid_message or "datetime must include a timezone")
    return parsed.astimezone(UTC) if normalize_utc else parsed


def env_path(name: str, default: Path) -> Path:
    """Resolve an optional environment path override and expand ``~``."""
    return Path(os.environ.get(name, str(default))).expanduser()


def env_csv(name: str) -> tuple[str, ...]:
    """Resolve a comma-separated environment variable to unique, non-empty values."""
    return tuple(dict.fromkeys(value.strip() for value in os.environ.get(name, "").split(",") if value.strip()))

"""Compact a lane journal by dropping replay-duplicated rows.

Transcript syncs that atomically replace the transcript path historically
replayed the whole file without resetting event identity, so journals could
hold dozens of copies of every native row. Compaction rewrites the journal
keeping the first row per (native key, payload digest), atomically via a
same-directory temporary file, and refuses to run while the journal's lane
lock is held.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from chitra._fsio import exclusive_lock

from .store import EventJournal, journal_row_dedupe_key


class JournalCompactionLockedError(RuntimeError):
    """The journal's lane lock is held by a live writer."""


def _compact_lines(lines: Iterable[bytes]) -> tuple[list[bytes], int]:
    """Keep the first line per dedupe key; unkeyed lines are always kept."""
    kept: list[bytes] = []
    seen: set[tuple[str, str, str, str]] = set()
    removed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # An unparseable tail is preserved untouched; load() reports it.
            kept.append(line)
            continue
        key = journal_row_dedupe_key(row) if isinstance(row, dict) else None
        if key is not None:
            if key in seen:
                removed += 1
                continue
            seen.add(key)
        kept.append(line)
    return kept, removed


def compact_journal(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Rewrite ``path`` without its replay-duplicated rows.

    Returns before/after counts. The lane lock must be free: a held lock
    means a live writer owns the file, so compaction refuses rather than
    racing an append.
    """
    path = Path(path)
    lock_path = path.with_suffix(".lock")
    with exclusive_lock(lock_path, mode=0o600, timeout=0) as held:
        if not held:
            raise JournalCompactionLockedError(
                f"journal lock {lock_path} is held; a live writer owns this journal"
            )
        if not path.exists():
            return {
                "journal": str(path),
                "before": 0,
                "after": 0,
                "removed": 0,
                "dry_run": dry_run,
            }
        lines = path.read_bytes().splitlines(keepends=True)
        kept, removed = _compact_lines(lines)
        if removed and not dry_run:
            fd, tmp_name = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as tmp:
                    tmp.write(b"".join(kept))
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, path)
            except BaseException:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                raise
        return {
            "journal": str(path),
            "before": len(lines),
            "after": len(kept),
            "removed": removed,
            "dry_run": dry_run,
        }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-journal-compact",
        description=(
            "Rewrite a lane journal keeping the first row per (native key, "
            "payload digest). Refuses while the journal's lane lock is held."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="Path to the journal .jsonl file to compact.",
    )
    source.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Monitor state root containing journal/<lane>.jsonl; requires --lane.",
    )
    parser.add_argument("--lane", default=None, help="Lane name (with --state-dir).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print before/after counts without rewriting the journal.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.journal is not None:
        path = args.journal
    else:
        if not args.lane:
            build_arg_parser().error("--lane is required with --state-dir")
        path = EventJournal(args.state_dir, args.lane).path
    try:
        result = compact_journal(path, dry_run=args.dry_run)
    except JournalCompactionLockedError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

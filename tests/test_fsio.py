"""Focused checks for the shared advisory-lock primitive in ``chitra._fsio``."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from chitra._fsio import exclusive_lock


def test_exclusive_lock_excludes_a_contender_then_releases(tmp_path: Path) -> None:
    """The context manager must hold a real exclusive flock on the lock file."""
    lock_path = tmp_path / "state.json.lock"
    contender = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with exclusive_lock(lock_path), pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(contender)
    contender = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_UN)
    finally:
        os.close(contender)


def test_exclusive_lock_with_zero_timeout_yields_false_under_contention(tmp_path: Path) -> None:
    """A non-blocking attempt reports refusal instead of waiting."""
    lock_path = tmp_path / "merge.lock"
    with exclusive_lock(lock_path) as first:
        assert first
        with exclusive_lock(lock_path, timeout=0) as second:
            assert not second
    with exclusive_lock(lock_path, timeout=0) as third:
        assert third

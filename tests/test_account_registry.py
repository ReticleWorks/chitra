"""Kept unit guards: the naive-``now`` guard is reachable only through the
in-process ``update_registry`` API -- every real entry point (the sweep CLI and
daemon) passes a timezone-aware ``datetime.now(UTC)``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from chitra.account_registry import update_registry


def test_update_registry_rejects_naive_datetime(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        update_registry(tmp_path, [], now=datetime(2026, 7, 12))  # noqa: DTZ001 -- deliberately naive, testing the guard

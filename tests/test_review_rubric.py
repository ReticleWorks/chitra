"""Held unit guard: a deny path unreachable through the chitra-review CLI.

``ClaudeProcessReviewer`` refuses a nonpositive attempt count at construction,
before any reviewer process exists. The CLI hard-codes ``REVIEWER_ATTEMPTS``
and exposes no flag for it, so the guard cannot be reached through the real
boundary. Every other rubric contract moved to
``test_review_boundary_contracts.py``.
"""

from __future__ import annotations

import pytest

from chitra.goal_enforcement import ClaudeProcessReviewer


def test_a_nonpositive_attempt_count_is_still_refused_before_any_process() -> None:
    with pytest.raises(ValueError):
        ClaudeProcessReviewer(runner=None, attempts=0)  # type: ignore[arg-type]

"""Kept unit guard: ``ClaudeProcessReviewer(attempts=...)`` is an in-process
constructor argument -- the review CLI and daemon always use the fixed
``REVIEWER_ATTEMPTS`` budget, so a non-positive count is unreachable through
the real boundary.
"""

from __future__ import annotations

import pytest

from chitra.goal_enforcement import ClaudeProcessReviewer


def test_a_non_positive_attempt_count_is_refused() -> None:
    with pytest.raises(ValueError, match="attempts must be a positive integer"):
        ClaudeProcessReviewer(attempts=0)

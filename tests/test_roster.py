"""Kept unit guard: ``compute_marker`` is the deeper redundant refusal for an
uncolorable status -- the goal store load refuses an unknown status before the
roster CLI can ever reach the marker path, so only this unit test covers it.
"""

from __future__ import annotations

from typing import cast

import pytest

from chitra.board import compute_marker
from chitra.goals import GoalRecord, GoalStatus


def _record(
    session_ref: str,
    status: GoalStatus,
    *,
    goal: str = "Keep this durable roster objective clear and verifiable.",
    now: str = "running checks",
    open_asks: tuple[str, ...] = (),
    needs: str = "",
    goal_version: int = 1,
) -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal=goal,
        done_when="Every required validation command passes cleanly.",
        source="branch",
        status=status,
        goal_version=goal_version,
        now=now,
        open_asks=open_asks,
        needs=needs,
    )


def test_compute_marker_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="uncolorable status"):
        compute_marker(_record("host:lane:0.0", cast(GoalStatus, "unknown")))

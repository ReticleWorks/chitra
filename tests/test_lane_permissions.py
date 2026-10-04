"""Held unit test for the full-permission launch contract.

The only assertion the launch boundary cannot reach: the launcher's internal
``_require_full_permissions`` check refuses a flag-less command before anything
is created. The launch CLI always builds its agent command through
``_agent_command``, which already carries the flag, so no real boundary input
can produce a flag-less command. Everything else this file asserted is covered
at the launch boundary by ``tests/test_safety_gate_boundaries.py``.
"""

from __future__ import annotations

import pytest

from chitra.lane_anchor import LaneLaunchRefused, _require_full_permissions


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_a_command_without_the_flag_is_refused_before_anything_is_created(backend: str) -> None:
    """The cheap half of the self-test, which costs no model call."""
    with pytest.raises(LaneLaunchRefused, match="partial permissions"):
        _require_full_permissions(backend, [backend, "--model", "sonnet"])

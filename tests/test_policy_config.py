"""Held unit test for the merge-policy deny-all default.

The only assertion the policy boundary cannot reach: ``PolicyConfig().merge``
is disabled with empty allowlists until an operator configures it. The
``chitra-usage policy`` CLI prints only the usage section, so the merge
default is not observable through a real boundary. Everything else this file
asserted is covered at the ``chitra-usage policy`` boundary by
``tests/test_safety_gate_boundaries.py``.
"""

from __future__ import annotations

from chitra.policy_config import PolicyConfig


def test_merge_is_off_and_allows_nothing_until_an_operator_configures_it() -> None:
    policy = PolicyConfig().merge
    assert policy.enabled is False
    assert policy.allowed_repos == []
    assert policy.lane_authors == []
    # Both conventions ship on. A brake wired only to a label the repository
    # never applies would not stop anything there.
    assert policy.hold_labels == ["chitra-hold", "hold"]

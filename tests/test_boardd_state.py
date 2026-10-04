"""boardd state-loader tests against the bundled fixture state dir."""

from pathlib import Path

import pytest

from boardd import config
from boardd.state import build_view
from boardd.translate import TranslationCache

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "boardd_state"


@pytest.fixture(scope="module")
def view():
    tc = TranslationCache(config.TRANSLATION_SEED)
    return build_view(FIXTURE_DIR, tc)


def test_nothing_masquerades_as_tracked(view):
    """Every lane except ws-paper (structured items, exercised below) has no
    enrolled_done_when_items, so its plain-text clauses stay honestly unbound."""
    for lane in view["lanes"]:
        if lane["session_ref"] == "twinridge:ws-paper":
            continue
        assert lane["done_when"]["proven"] == 0
        for cond in lane["done_when"]["conditions"]:
            assert cond["proof"]["state"] == "unbound"
            assert "no evidence source is linked" in cond["proof"]["label"].lower()


def test_agent_results_never_verified(view):
    for ev in view["events"]:
        assert ev["verified"] is False
        assert ev["verified_label"] == "Boardd has not verified this."
    for lane in view["lanes"]:
        if lane["latest_result"] is not None:
            assert lane["latest_result"]["verified"] is False

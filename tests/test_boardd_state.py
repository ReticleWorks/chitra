"""boardd state-loader tests against the bundled fixture state dir."""

import json
from pathlib import Path

import pytest

from boardd import config
from boardd.state import build_view, split_conditions
from boardd.translate import TranslationCache

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "boardd_state"


@pytest.fixture(scope="module")
def view():
    tc = TranslationCache(config.TRANSLATION_SEED)
    return build_view(FIXTURE_DIR, tc)


def test_all_lanes_load(view):
    assert view["schema"] == "boardd.state.v2"
    assert view["source"]["goals_schema"] == "chitra.goals.v3"
    assert view["source"]["errors"] == []
    assert len(view["lanes"]) == 10
    assert view["summary"]["lane_count"] == 10


def test_needs_you_comes_from_open_asks_and_review_statuses(view):
    # Fixture state: boardd-build has 2 asks, wiki-backfill has 1. The three
    # review-queue statuses without asks (completion-disputed, turn-finished-
    # unverified, done-pending-verification) each contribute one status-only item.
    refs = [n["lane_ref"] for n in view["needs_you"]]
    assert refs.count("twinridge:boardd-build") == 2
    assert refs.count("tophand:wiki-backfill") == 1
    assert refs.count("twinridge:c912-scenarios") == 1
    assert refs.count("trailhead:feeds-service") == 1
    assert refs.count("twinridge:ws-paper") == 1
    assert len(view["needs_you"]) == 6


def test_needs_you_sorted_oldest_first(view):
    since = [n["since"] for n in view["needs_you"]]
    assert since == sorted(since)


def test_needs_you_status_only_items_have_plain_reason(view):
    item = next(n for n in view["needs_you"] if n["lane_ref"] == "twinridge:c912-scenarios")
    assert item["question"]["translated"] is True
    assert "needs review" in item["question"]["text"].lower()


def test_needs_review_badge_on_lanes(view):
    flagged = {ln["session_ref"] for ln in view["lanes"] if ln["needs_review"]}
    assert flagged == {
        "twinridge:boardd-build",
        "tophand:wiki-backfill",
        "twinridge:c912-scenarios",
        "trailhead:feeds-service",
        "twinridge:ws-paper",
    }


def test_scope_delta_detected_on_ws_paper(view):
    lane = next(ln for ln in view["lanes"] if ln["session_ref"] == "twinridge:ws-paper")
    assert lane["scope"]["narrowed"] is True
    dropped = " ".join(d["raw"] for d in lane["scope"]["dropped"])
    assert "reviewer 3" in dropped


def test_no_scope_delta_when_same(view):
    lane = next(ln for ln in view["lanes"] if ln["session_ref"] == "roundtop:ramble-build")
    assert lane["scope"]["narrowed"] is False
    assert lane["scope"]["dropped"] == []


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


def test_structured_done_when_items_read_completion_proofs(view):
    """ws-paper carries two enrolled_done_when_items: one has a passing
    completion_proof (verified), the other has none (unbound)."""
    lane = next(ln for ln in view["lanes"] if ln["session_ref"] == "twinridge:ws-paper")
    done_when = lane["done_when"]
    assert done_when["total"] == 2
    assert done_when["proven"] == 1
    states = {c["raw"]: c["proof"]["state"] for c in done_when["conditions"]}
    assert states["all reviewer 2 comments dispositioned"] == "verified"
    assert states["sections 3 and 5 rewritten"] == "unbound"


def test_banned_phrasing_absent(view):
    """The operator killed 'N of M machine-checkable conditions is verified'."""
    text = json.dumps(view)
    assert "machine-checkable" not in text


def test_agent_results_never_verified(view):
    for ev in view["events"]:
        assert ev["verified"] is False
        assert ev["verified_label"] == "Boardd has not verified this."
    for lane in view["lanes"]:
        if lane["latest_result"] is not None:
            assert lane["latest_result"]["verified"] is False


def test_translation_seed_covers_fixture_state(view):
    """The demo must read well: every rendered line in the fixture state has a
    seeded translation. Untranslated fallback is exercised in its own test."""
    for lane in view["lanes"]:
        assert lane["goal"]["translated"], lane["session_ref"]
        for cond in lane["done_when"]["conditions"]:
            assert cond["translated"], cond["raw"]
        for ask in lane["open_asks"]:
            assert ask["translated"], ask["raw"]
        assert lane["movement"]["now"]["translated"], lane["session_ref"]
    for ev in view["events"]:
        assert ev["summary"]["translated"], ev["summary"]["raw"]


def test_untranslated_fallback_is_honest():
    tc = TranslationCache(None)
    out = tc.get("bisecting flaky e2e; likely clock race")
    assert out["translated"] is False
    assert out["text"] == out["raw"] == "bisecting flaky e2e; likely clock race"


def test_stale_data_states_age(view):
    src = view["source"]
    assert src["goals_age_seconds"] is not None
    # Frozen fixture snapshot from 2026-08-08 16:40 — well past the threshold.
    assert src["data_stale"] is True


def test_split_conditions():
    assert split_conditions("a; b;c;") == ["a", "b", "c"]
    assert split_conditions("") == []


def test_history_categories_present(view):
    labels = {ev["category"]["label"] for ev in view["events"]}
    assert "No change" in labels
    assert "Blocked" in labels
    for ev in view["events"]:
        assert ev["category"]["tone"] in {"ok", "warn", "bad", "hold", "ramble"}


def test_missing_dir_reports_errors(tmp_path):
    tc = TranslationCache(None)
    view = build_view(tmp_path, tc)
    assert len(view["source"]["errors"]) == 2
    assert view["lanes"] == []

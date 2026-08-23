"""Production-path tests for joined-lane boardd reporting."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from _joined_report_fixtures import joined_report_record
from fastapi.testclient import TestClient

from boardd import app as app_module
from boardd import config
from boardd.state import build_view
from boardd.translate import TranslationCache
from chitra.joined_lane import JoinedLaneStore

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "boardd_state"


def _state_with_joined_lane(tmp_path: Path) -> Path:
    for name in ("goals.json", "sweep-digest.json"):
        shutil.copy(FIXTURE_DIR / name, tmp_path / name)
    JoinedLaneStore(tmp_path).create(joined_report_record())
    return tmp_path


def test_real_joined_lane_file_reaches_boardd_view_and_preserves_legacy_lanes(tmp_path: Path) -> None:
    state_dir = _state_with_joined_lane(tmp_path)

    view = build_view(state_dir, TranslationCache(None))

    joined = next(lane for lane in view["lanes"] if lane["session_ref"] == "roundtop:ramble-build")
    report = joined["joined_session"]
    assert report["progress"] == {
        "percentage": 50.0,
        "completed_steps": 1,
        "total_steps": 2,
        "reason": "available",
    }
    assert report["roadmap"]["position"]["id"] == "proof"
    assert report["now"]["text"] == "Running the proof"
    assert report["next"]["text"] == "Publish the proof result"
    assert report["next_check"]["wake_condition"]["text"] == "A newer report is observed"
    assert [problem["id"] for problem in report["open_problems"]] == ["provider-wait"]
    assert [problem["id"] for problem in report["resolved_problems"]] == ["old-report"]
    assert report["recovery_action"]["text"] == "checkpoint"
    assert report["owner"]["id"] == "lane"
    assert report["provider"]["kind"] == "tophand"
    assert "pid" not in json.dumps(report).lower()
    assert "tmux" not in json.dumps(report).lower()

    legacy = [lane for lane in view["lanes"] if lane["session_ref"] != "roundtop:ramble-build"]
    assert legacy
    assert all("joined_session" not in lane for lane in legacy)
    assert view["source"]["joined_lane_count"] == 1


def test_browser_api_payload_contains_the_joined_report(tmp_path: Path, monkeypatch) -> None:
    state_dir = _state_with_joined_lane(tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    payload = TestClient(app_module.app).get("/api/state").json()

    lane = next(item for item in payload["lanes"] if item["session_ref"] == "roundtop:ramble-build")
    assert lane["joined_session"]["roadmap"]["position"]["title"]["text"] == "Run the proof"
    assert lane["joined_session"]["progress"]["percentage"] == 50.0
    assert lane["joined_session"]["recovery_action"]["text"] == "checkpoint"

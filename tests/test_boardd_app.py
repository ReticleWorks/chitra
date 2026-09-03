"""boardd endpoint and SSE smoke tests against the bundled fixture state dir."""

import importlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "boardd_state"

os.environ["BOARDD_STATE_DIR"] = str(FIXTURE_DIR)
# BOARDD_DEV=1 is the one escape hatch discovery.py honours: without it,
# boardd only finds monitors via systemctl/glob, neither of which exists in
# a test sandbox. Leaving BOARDD_STATE_ROOTS unset makes discovery fall back
# to the single default "monitor" id backed by BOARDD_STATE_DIR above.
os.environ["BOARDD_DEV"] = "1"
os.environ.pop("BOARDD_STATE_ROOTS", None)

# Reload so config re-reads the env var even if another test module imported
# boardd first, then reload app so its module-level cache picks up the config.
from boardd import config  # noqa: E402

importlib.reload(config)
assert config.STATE_DIR == FIXTURE_DIR

from boardd import app as app_module  # noqa: E402

importlib.reload(app_module)

client = TestClient(app_module.app)


def test_index_serves_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "boardd" in r.text
    assert 'name="viewport"' in r.text


def test_api_state():
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert data["schema"] == "boardd.state.v2"
    assert len(data["lanes"]) == 10
    assert data["summary"]["needs_you_count"] == 6


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_sse_initial_state():
    """SSE smoke: the stream's first event is a full state push.

    Reads the endpoint's generator directly with a bounded await instead of
    TestClient streaming: TestClient never delivers http.disconnect to an
    infinite StreamingResponse, so an HTTP-level read of /events hangs the
    suite. Under uvicorn the disconnect path works (exercised manually with
    curl; see docs/boardd.md).
    """
    import asyncio

    async def first_event() -> str:
        resp = await app_module.events()
        assert resp.media_type == "text/event-stream"
        agen = resp.body_iterator
        try:
            return await asyncio.wait_for(agen.__anext__(), timeout=10)
        finally:
            await agen.aclose()

    chunk = asyncio.run(first_event())
    assert chunk.startswith("event: state\n")
    payload = json.loads(chunk.split("data: ", 1)[1].strip())
    assert payload["schema"] == "boardd.state.v2"
    assert len(payload["lanes"]) == 10


def test_api_monitors():
    r = client.get("/api/monitors")
    assert r.status_code == 200
    monitors = r.json()
    assert len(monitors) == 1
    assert monitors[0]["id"] == "monitor"
    assert monitors[0]["lane_count"] == 10
    assert monitors[0]["needs_feedback_count"] == 6
    assert monitors[0]["has_state_root"] is True


def test_api_state_unknown_monitor_404():
    r = client.get("/api/state?monitor=nope")
    assert r.status_code == 404


def _enrolled_lane(session_ref: str, open_asks: tuple[str, ...]):
    """A fully v3-enrolled GoalRecord — the display fixture's lanes are
    intentionally not enrolled (chitra.goals treats an unenrolled record as
    legacy and refuses any write to it, enrolled or not), so the write
    endpoints need one of these instead."""
    from chitra.goals import EnrolledDoneWhenItem, GoalRecord, InterviewReceipt, render_done_when_items

    items = (EnrolledDoneWhenItem(id="item-1", text="ship it", validator="suite", required_receipt="receipt-1"),)
    return GoalRecord(
        session_ref=session_ref,
        goal="Test goal.",
        done_when=render_done_when_items(items),
        source="task-file:PLAN.md",
        status="blocked",
        now="waiting on the operator",
        last_verified="",
        created_at="2026-09-01T00:00:00-04:00",
        updated_at="2026-09-01T00:00:00-04:00",
        open_asks=open_asks,
        enrolled_done_when_items=items,
        interview_receipt=InterviewReceipt(
            name="enroll",
            completed_at="2026-09-01T00:00:00-04:00",
            answers_sha256="0" * 64,
            provenance=("operator:a", "operator:b", "operator:c", "operator:d"),
        ),
    )


def _write_endpoint_env(tmp_path):
    """A one-lane, fully-enrolled goals.json in an isolated temp dir, plus an
    empty sweep-digest.json, so write-endpoint tests never touch the shared
    display fixture other tests read."""
    import json

    from chitra.goals import SCHEMA

    target = tmp_path / "boardd_state"
    target.mkdir()
    record = _enrolled_lane("roundtop:wiki-backfill", ("Rename the colliding page?",))
    (target / "goals.json").write_text(
        json.dumps({"schema": SCHEMA, "updated_at": "2026-09-01T00:00:00-04:00", "goals": [record.to_dict()]})
    )
    digest = {"schema": "sim.sweep-digest.v1", "sweep_at": "2026-09-01T00:00:00-04:00", "events": []}
    (target / "sweep-digest.json").write_text(json.dumps(digest))
    return target


def test_ack_clears_open_asks(tmp_path):
    state_dir = _write_endpoint_env(tmp_path)
    old = os.environ.get("BOARDD_STATE_ROOTS")
    os.environ["BOARDD_STATE_ROOTS"] = f"monitor={state_dir}"
    try:
        r = client.post("/api/lanes/wiki-backfill/ack")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["changed"] is True
        assert body["lane"]["open_asks"] == []
        state = client.get("/api/state").json()
        lane = next(ln for ln in state["lanes"] if ln["lane_id"] == "wiki-backfill")
        assert lane["open_asks"] == []
    finally:
        if old is None:
            os.environ.pop("BOARDD_STATE_ROOTS", None)
        else:
            os.environ["BOARDD_STATE_ROOTS"] = old


def test_ack_on_lane_with_no_open_asks_is_409(tmp_path):
    """Second ack on a lane with nothing left to resolve must not return a
    false 200 — that is the exact honesty failure state.py's invariants
    guard against everywhere else."""
    state_dir = _write_endpoint_env(tmp_path)
    old = os.environ.get("BOARDD_STATE_ROOTS")
    os.environ["BOARDD_STATE_ROOTS"] = f"monitor={state_dir}"
    try:
        r1 = client.post("/api/lanes/wiki-backfill/ack")
        assert r1.status_code == 200, r1.text
        r2 = client.post("/api/lanes/wiki-backfill/ack")
        assert r2.status_code == 409, r2.text
        assert r2.json()["detail"]
    finally:
        if old is None:
            os.environ.pop("BOARDD_STATE_ROOTS", None)
        else:
            os.environ["BOARDD_STATE_ROOTS"] = old


def test_answer_records_basis_and_clears_asks(tmp_path):
    state_dir = _write_endpoint_env(tmp_path)
    old = os.environ.get("BOARDD_STATE_ROOTS")
    os.environ["BOARDD_STATE_ROOTS"] = f"monitor={state_dir}"
    try:
        r = client.post("/api/lanes/wiki-backfill/answer", json={"text": "Rename it to atlas-compute-graph."})
        assert r.status_code == 200, r.text
        state = client.get("/api/state").json()
        lane = next(ln for ln in state["lanes"] if ln["lane_id"] == "wiki-backfill")
        assert lane["open_asks"] == []

        from chitra.goals import load_goals

        record = next(rec for rec in load_goals(state_dir, allow_newer=True) if rec.lane_id == "wiki-backfill")
        assert all(item["basis"] == "Rename it to atlas-compute-graph." for item in record.retired_asks)
    finally:
        if old is None:
            os.environ.pop("BOARDD_STATE_ROOTS", None)
        else:
            os.environ["BOARDD_STATE_ROOTS"] = old


def test_answer_rejects_empty_text(tmp_path):
    state_dir = _write_endpoint_env(tmp_path)
    old = os.environ.get("BOARDD_STATE_ROOTS")
    os.environ["BOARDD_STATE_ROOTS"] = f"monitor={state_dir}"
    try:
        r = client.post("/api/lanes/wiki-backfill/answer", json={"text": "  "})
        assert r.status_code == 400
    finally:
        if old is None:
            os.environ.pop("BOARDD_STATE_ROOTS", None)
        else:
            os.environ["BOARDD_STATE_ROOTS"] = old


def test_answer_rejects_oversized_body(tmp_path):
    state_dir = _write_endpoint_env(tmp_path)
    old = os.environ.get("BOARDD_STATE_ROOTS")
    os.environ["BOARDD_STATE_ROOTS"] = f"monitor={state_dir}"
    try:
        big = json.dumps({"text": "y" * (70 * 1024)})
        r = client.post(
            "/api/lanes/wiki-backfill/answer",
            content=big,
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 413
    finally:
        if old is None:
            os.environ.pop("BOARDD_STATE_ROOTS", None)
        else:
            os.environ["BOARDD_STATE_ROOTS"] = old


def test_answer_rejects_non_json_body(tmp_path):
    state_dir = _write_endpoint_env(tmp_path)
    old = os.environ.get("BOARDD_STATE_ROOTS")
    os.environ["BOARDD_STATE_ROOTS"] = f"monitor={state_dir}"
    try:
        r = client.post(
            "/api/lanes/wiki-backfill/answer",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
    finally:
        if old is None:
            os.environ.pop("BOARDD_STATE_ROOTS", None)
        else:
            os.environ["BOARDD_STATE_ROOTS"] = old


def test_answer_rejects_empty_body(tmp_path):
    state_dir = _write_endpoint_env(tmp_path)
    old = os.environ.get("BOARDD_STATE_ROOTS")
    os.environ["BOARDD_STATE_ROOTS"] = f"monitor={state_dir}"
    try:
        r = client.post(
            "/api/lanes/wiki-backfill/answer",
            content=b"",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
    finally:
        if old is None:
            os.environ.pop("BOARDD_STATE_ROOTS", None)
        else:
            os.environ["BOARDD_STATE_ROOTS"] = old


class _FakeAgenttrailHandler(BaseHTTPRequestHandler):
    """Stand-in for the real vendored agenttrail process — enough to prove
    the proxy allows a listed GET path through and blocks everything else,
    without spawning Node."""

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's naming convention
        if self.path == "/world":
            body = b'{"ok": true, "from": "fake-agenttrail"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # keep test output quiet


def test_activity_proxy_allows_listed_path_and_blocks_others(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _FakeAgenttrailHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(config, "AGENTTRAIL_PUBLIC_URL", f"http://127.0.0.1:{port}/")

        r = client.get("/activity/world")
        assert r.status_code == 200, r.text
        assert r.json()["from"] == "fake-agenttrail"

        # Not on agenttrail's own UI-fetch allowlist — never even reaches
        # the fake upstream, which would 404 it anyway if it did.
        r_spawn = client.get("/activity/spawn")
        assert r_spawn.status_code == 404

        # The proxy route registers GET only — a mutating verb against an
        # otherwise-allowed path has no matching route at all.
        r_post = client.post("/activity/world")
        assert r_post.status_code in (404, 405)
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_agenttrail_status_snapshot_is_per_monitor(tmp_path, monkeypatch):
    """Two monitors must not overwrite each other's last-posted snapshot.

    The snapshot was one flat process-wide dict while sync_lanes only ever
    returns the lanes of the root it was handed, so switching monitors
    wiped the first one's history and re-emitted SessionStart for lanes
    agenttrail already tracked.
    """
    seen_prev = []

    def fake_sync(hook_url, cwd, lanes, prev):
        seen_prev.append(dict(prev))
        return {"lane-a": "working"}

    monkeypatch.setattr(app_module, "_agenttrail_status", {})
    monkeypatch.setattr(app_module, "load_goals", lambda root, allow_newer=True: [])
    monkeypatch.setattr(app_module.agenttrail_bridge, "sync_lanes", fake_sync)

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    app_module._sync_agenttrail(root_a)
    app_module._sync_agenttrail(root_b)
    app_module._sync_agenttrail(root_a)

    # Third call sees root_a's own snapshot; root_b started from empty and
    # never clobbered it.
    assert seen_prev == [{}, {}, {"lane-a": "working"}]


def test_hook_post_survives_a_malformed_hook_url(caplog):
    """A bad BOARDD_AGENTTRAIL_HOOK_URL must not kill the SSE stream.

    urllib raises ValueError, not OSError, for a URL with no scheme, and the
    old contextlib.suppress(OSError) let it escape `_sync_agenttrail` and
    end the stream for every connected client — over a side channel this
    module documents as never blocking boardd.
    """
    from boardd import agenttrail_bridge

    with caplog.at_level("WARNING"):
        agenttrail_bridge.post_hook_event("not-a-url", {"hook_event_name": "SessionStart"})
    assert "agenttrail hook post to not-a-url failed" in caplog.text


def test_manifest_icons_exist_and_are_served():
    """An empty "icons" array means Android never offers the install
    prompt, so the PWA the mobile UI was built for cannot be installed."""
    manifest = client.get("/static/manifest.webmanifest").json()
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert sizes == {"192x192", "512x512"}
    for icon in manifest["icons"]:
        assert icon["type"] == "image/png"
        r = client.get(icon["src"])
        assert r.status_code == 200, icon["src"]
        assert r.content.startswith(b"\x89PNG")


def test_activity_iframes_are_sandboxed():
    """Without a sandbox attribute the framed agenttrail page can navigate
    boardd's own top-level window. allow-top-navigation is not granted."""
    html = client.get("/").text
    frames = [line for line in html.splitlines() if "<iframe" in line]
    assert len(frames) == 2, frames
    for frame in frames:
        assert 'sandbox="allow-scripts allow-same-origin"' in frame

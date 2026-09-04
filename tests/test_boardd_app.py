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


def test_root_with_no_agenttrail_is_a_502_not_a_500():
    """Nothing is vendored-served from Python any more: / is the board,
    proxied from the agenttrail process. With no process up that is a
    plain 502, never an unhandled error."""
    r = client.get("/")
    assert r.status_code == 502
    assert "agenttrail unreachable" in r.text


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


def _write_endpoint_env(tmp_path, lane="wiki-backfill", name="boardd_state", open_asks=("Rename the colliding page?",)):
    """A one-lane, fully-enrolled goals.json in an isolated temp dir, plus an
    empty sweep-digest.json, so write-endpoint tests never touch the shared
    display fixture other tests read."""
    import json

    from chitra.goals import SCHEMA

    target = tmp_path / name
    target.mkdir()
    record = _enrolled_lane(f"roundtop:{lane}", open_asks)
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


ANSWERS_LOG: list[str] = []  # what the fake upstream was actually POSTed
ANSWER_BODIES: list[dict] = []  # and the /answer payloads it recorded


class _FakeAgenttrailHandler(BaseHTTPRequestHandler):
    """Stand-in for the real vendored agenttrail process — enough to prove
    the proxy allows a listed GET path through and blocks everything else,
    without spawning Node."""

    def _ok(self, body: bytes, content_type: str = "application/json"):
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's naming convention
        path = self.path.split("?")[0]
        if path == "/":
            self._ok(b"<!doctype html><title>agenttrail</title>", "text/html")
        elif path in ("/world", "/escalations", "/tree-of", "/events"):
            self._ok(b'{"ok": true, "from": "fake-agenttrail"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's naming convention
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path in ("/answer", "/hook"):
            ANSWERS_LOG.append(self.path)
            if self.path == "/answer":
                ANSWER_BODIES.append(json.loads(raw))
            self._ok(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # keep test output quiet


def test_root_proxy_serves_the_board_and_blocks_the_rest(monkeypatch):
    """The board is mounted at `/`, so its own root-absolute fetches
    resolve — that was the defect behind the old `/activity/` mount, where
    the page asked for `/world` and got boardd's 404. Every mutating route
    but the board's own two stays refused."""
    server = HTTPServer(("127.0.0.1", 0), _FakeAgenttrailHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ANSWERS_LOG.clear()
    try:
        monkeypatch.setattr(config, "AGENTTRAIL_PUBLIC_URL", f"http://127.0.0.1:{port}/")

        assert "agenttrail" in client.get("/").text

        # Root-absolute, exactly as the page requests them.
        for path in ("/world", "/escalations", "/tree-of?port=5330", "/events"):
            r = client.get(path)
            assert r.status_code == 200, (path, r.text)
            assert r.json()["from"] == "fake-agenttrail", path

        # Never reaches the upstream: /spawn launches a detached Node
        # process from an unauthenticated POST body.
        for path in ("/spawn", "/setup-board", "/suggest", "/nudge"):
            assert client.get(path).status_code == 404, path
            assert client.post(path).status_code in (404, 405), path

        # POST /hook is the board's own event intake and passes through.
        assert client.post("/hook", json={"hook_event_name": "SessionStart"}).status_code == 200
        assert ANSWERS_LOG == ["/hook"]
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_board_answer_logs_upstream_and_resolves_the_ask(tmp_path, monkeypatch):
    """"Send to session" has two destinations: agenttrail's answers.log,
    for parity with the approved board, and chitra-goals resolve-ask, which
    is what actually retires the ask."""
    monkeypatch.setenv("BOARDD_STATE_ROOTS", f"monitor={_write_endpoint_env(tmp_path)}")

    server = HTTPServer(("127.0.0.1", 0), _FakeAgenttrailHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ANSWERS_LOG.clear()
    try:
        monkeypatch.setattr(config, "AGENTTRAIL_PUBLIC_URL", f"http://127.0.0.1:{port}/")

        r = client.post("/answer", json={"key": "monitor:wiki-backfill", "answer": "Merge it.", "at": "now"})
        assert r.status_code == 200, r.text
        assert r.json()["logged"] is True
        assert ANSWERS_LOG == ["/answer"]
        assert r.json()["lane"]["open_asks"] == []

        assert client.post("/answer", json={"key": "monitor:no-such-lane", "answer": "x"}).status_code == 404
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_answers_log_records_what_reached_chitra(tmp_path, monkeypatch):
    """The log used to be written first and unconditionally, so it recorded
    what the operator typed, not what landed. A refused answer is logged as
    a failure."""
    root = _write_endpoint_env(tmp_path)
    monkeypatch.setenv("BOARDD_STATE_ROOTS", f"monitor={root}")

    server = HTTPServer(("127.0.0.1", 0), _FakeAgenttrailHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ANSWER_BODIES.clear()
    try:
        monkeypatch.setattr(config, "AGENTTRAIL_PUBLIC_URL", f"http://127.0.0.1:{port}/")

        assert client.post("/answer", json={"key": "monitor:nope", "answer": "x", "at": ""}).status_code == 404
        assert ANSWER_BODIES[-1]["answer"].startswith("failed: ")
        assert "nope" in ANSWER_BODIES[-1]["answer"]

        assert client.post("/answer", json={"key": "monitor:wiki-backfill", "answer": "Do it.", "at": ""}).status_code == 200
        assert ANSWER_BODIES[-1]["answer"] == "Do it."
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_a_status_only_lane_accepts_an_answer(tmp_path, monkeypatch):
    """A blocked lane with no literal ask is on the stack and offers a Send.
    That Send used to 409 every time, because resolve-ask had nothing to
    retire. The answer is recorded as the basis of the board's review ask."""
    root = _write_endpoint_env(tmp_path, lane="status-only", open_asks=())
    monkeypatch.setenv("BOARDD_STATE_ROOTS", f"monitor={root}")

    server = HTTPServer(("127.0.0.1", 0), _FakeAgenttrailHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(config, "AGENTTRAIL_PUBLIC_URL", f"http://127.0.0.1:{port}/")

        r = client.post("/answer", json={"key": "monitor:status-only", "answer": "Unblock it.", "at": "now"})
        assert r.status_code == 200, r.text
        assert r.json()["lane"]["open_asks"] == []
        retired = json.loads((root / "goals.json").read_text())["goals"][0]["retired_asks"]
        assert retired[-1]["basis"] == "Unblock it."
        assert retired[-1]["authority"] == "operator"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_an_answer_lands_on_the_monitor_its_card_came_from(tmp_path, monkeypatch):
    """Two monitors, one lane each. The escalation key carries the monitor,
    so monitor B's card resolves B's ask and never touches A's."""
    a = _write_endpoint_env(tmp_path, lane="only-in-a", name="root-a")
    b = _write_endpoint_env(tmp_path, lane="only-in-b", name="root-b")
    monkeypatch.setenv("BOARDD_STATE_ROOTS", f"monitor={a},mb={b}")

    server = HTTPServer(("127.0.0.1", 0), _FakeAgenttrailHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(config, "AGENTTRAIL_PUBLIC_URL", f"http://127.0.0.1:{port}/")

        r = client.post("/answer", json={"key": "mb:only-in-b", "answer": "Ship B.", "at": "now"})
        assert r.status_code == 200, r.text
        assert r.json()["lane"]["lane_id"] == "only-in-b"
        assert r.json()["lane"]["open_asks"] == []

        # A's lane is untouched, and B's key does not resolve against A.
        assert json.loads((a / "goals.json").read_text())["goals"][0]["open_asks"] != []
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

    from boardd import board_bridge

    monkeypatch.setattr(board_bridge.agenttrail_bridge, "sync_lanes", fake_sync)
    bridge = board_bridge.BoardBridge(tmp_path / "workspace")

    bridge.post_hooks([("a", [])])
    bridge.post_hooks([("b", [])])
    bridge.post_hooks([("a", [])])

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


def test_no_iframe_survives_the_cockpit():
    """The sandbox that #134 added guarded an iframe onto agenttrail. The
    board is the top-level document now, so there is no frame to guard —
    and no second surface that could reintroduce one."""
    assert not (config.PKG_DIR / "static" / "index.html").exists()
    assert "<iframe" not in (config.PKG_DIR / "vendor" / "agenttrail" / "public" / "index.html").read_text()

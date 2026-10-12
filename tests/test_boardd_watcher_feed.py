"""Independent watcher files through the existing board API, SSE and native bridge."""

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from boardd import app as app_module
from boardd import board_bridge, config, discovery
from boardd.state import WATCHER_LABELS


def _write_feed(root, host, *, labels=("working",), stamp=None):
    root.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(UTC).isoformat()
    doc = {
        "host": host, "updated_at": stamp, "health": {"watcher": "running", "classifier": "available"},
        "lanes": [{
            "id": f"lane-{index}", "harness": "codex", "native_session_id": f"native-{host}-{index}",
            "classification": {
                "label": label, "provider": "typesafe", "model": "jev-1.13.0", "confidence": 0.88,
                "probabilities": {label: 1.0}, "packet_hash": "a" * 64, "observed_at": stamp,
                "usage": {"input_tokens": 3}, "latency_ms": 12, "reason": "synthetic isolated observation",
            },
            "goal": "Check the feed", "evidence": None, "delivery": None,
        } for index, label in enumerate(labels)],
    }
    event = {"id": f"{host}-initial", "host": host, "lane": "lane-0", "kind": "lane-change", "emitted_at": stamp}
    for filename, text in (
        ("lanes.json", json.dumps(doc)), ("events.jsonl", json.dumps(event) + "\n" + json.dumps(event) + "\n"),
    ):
        path = root / filename
        path.write_text(text)
        path.chmod(0o600)
        assert path.read_text() == text
    return doc


def _configure(monkeypatch, roots):
    monkeypatch.setenv("BOARDD_WATCHER_ROOTS", ",".join(f"{host}={root}" for host, root in roots.items()))
    monkeypatch.delenv("BOARDD_DEV", raising=False)
    monkeypatch.setattr(discovery, "discover_state_roots", lambda errors=None: {})
    monkeypatch.setattr(discovery, "discover_units", lambda: {})
    monkeypatch.setattr(config, "HEARTBEAT_SECONDS", 0.03)
    monkeypatch.setattr(config, "MONITORS_TICK_SECONDS", 0.06)


def _payload(chunk):
    return json.loads(chunk.split("data: ", 1)[1].strip())


def test_watcher_host_feed_api_sse_bridge_and_launchd(tmp_path, monkeypatch):
    roots = {"roundtop": tmp_path / "roundtop", "renegade": tmp_path / "renegade"}
    labels = ("working", *sorted(WATCHER_LABELS - {"working"}))
    roundtop = _write_feed(roots["roundtop"], "roundtop", labels=labels)
    _write_feed(roots["renegade"], "renegade")
    _configure(monkeypatch, roots)
    monkeypatch.setattr(config, "FEED_ONLY", True)
    with TestClient(app_module.app) as feed_client:
        assert feed_client.get("/api/state?monitor=roundtop").status_code == 200
        assert feed_client.post("/answer", json={"key": "fixture", "answer": "ignored"}).status_code == 405
        assert feed_client.post("/api/lanes/fixture/ack").status_code == 405
    monkeypatch.setenv("BOARDD_LAUNCHD_LABELS", "roundtop=example.watcher,roundtop=example.dispatchd,renegade=example.peer")
    native_calls = []

    def native_read(argv, **kwargs):
        assert argv[:2] == ["launchctl", "print"]
        assert argv[2].startswith("gui/")
        assert kwargs["timeout"] == 3.0
        native_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="\tstate = running\n", stderr="")

    monkeypatch.setattr(discovery.subprocess, "run", native_read)
    monkeypatch.setattr(discovery, "discover_units", discovery.discover_launchd_units)
    client = TestClient(app_module.app)
    response = client.get("/api/state?monitor=roundtop")
    assert response.status_code == 200
    host = response.json()
    assert host["host"] == "roundtop" and host["health"] == roundtop["health"]
    assert host["source"]["errors"] == [] and host["source"]["data_stale"] is False
    assert host["summary"]["needs_you_count"] is None  # Model labels do not settle operator authority.
    for actual, original in zip(host["lanes"], roundtop["lanes"], strict=True):
        assert actual["classification"] == original["classification"]
        assert actual["native_session_id"] == original["native_session_id"]
        assert actual["evidence"] is None and actual["delivery"] is None
        assert actual["movement"]["status"] == original["classification"]["label"]
    assert len(host["events"]) == 1  # Stable retry IDs do not duplicate history.
    all_hosts = client.get("/api/state?monitor=all").json()
    assert set(all_hosts["hosts"]) == set(roots) and "host" not in all_hosts
    monitors = client.get("/api/monitors").json()
    assert {row["unit_active_state"] for row in monitors} == {"running"}
    assert native_calls
    assert client.get("/healthz").json()["ok"] is True
    monkeypatch.setenv("BOARDD_DEV", "1")
    assert {row["id"] for row in client.get("/api/monitors").json()} == set(roots)
    assert client.get("/healthz").json()["ok"] is True  # No synthetic missing legacy root in an isolated watcher smoke.
    monkeypatch.delenv("BOARDD_DEV")

    bridge = board_bridge.BoardBridge(tmp_path / "workspace")
    sections = bridge.render()
    assert board_bridge.render_roster(sections)["escalations"] == {}
    plan = (tmp_path / "workspace" / "PLAN.md").read_text()
    assert "hybrid watcher" in plan and "jev-1.13.0" in plan
    assert "ready-to-close" in plan and "confidence: 0.88" in plan
    assert "- [x]" not in plan  # Model completion label is never a receipt.
    assert (tmp_path / "workspace" / "PLAN.md").stat().st_mode & 0o777 == 0o600

    async def observe_change():
        response = await app_module.events("all")
        stream = response.body_iterator
        try:
            initial = await asyncio.wait_for(anext(stream), 3)
            assert set(_payload(initial)["hosts"]) == set(roots)
            emitted = [await asyncio.wait_for(anext(stream), 3) for _ in range(2)]
            assert {_payload(chunk)["id"] for chunk in emitted} == {"roundtop-initial", "renegade-initial"}
            assert all("event: watcher\n" in chunk and chunk.startswith("id: ") for chunk in emitted)
            roundtop["lanes"][0]["classification"]["label"] = "blocked"
            roundtop["lanes"][0]["classification"]["probabilities"] = {"blocked": 1.0}
            path = roots["roundtop"] / "lanes.json"
            path.write_text(json.dumps(roundtop))
            assert json.loads(path.read_text()) == roundtop
            event = {
                "id": "roundtop-change", "host": "roundtop", "lane": "lane-0",
                "kind": "lane-change", "emitted_at": datetime.now(UTC).isoformat(),
            }
            with (roots["roundtop"] / "events.jsonl").open("a") as log:
                log.write(json.dumps(event) + "\n")
            assert json.loads((roots["roundtop"] / "events.jsonl").read_text().splitlines()[-1]) == event
            while True:
                chunk = await asyncio.wait_for(anext(stream), 3)
                if "event: state\n" in chunk:
                    changed = _payload(chunk)
                    assert changed["hosts"]["roundtop"]["lanes"][0]["classification"]["label"] == "blocked"
                    assert changed["hosts"]["renegade"]["lanes"][0]["classification"]["label"] == "working"
                    break
            chunk = await asyncio.wait_for(anext(stream), 3)
            assert _payload(chunk) == {**event, "monitor": "roundtop"}
            chunk = await asyncio.wait_for(anext(stream), 3)
            if "event: monitors\n" in chunk:
                chunk = await asyncio.wait_for(anext(stream), 3)
            assert "event: heartbeat\n" in chunk  # No unchanged state or native event resend.
        finally:
            await stream.aclose()

    asyncio.run(asyncio.wait_for(observe_change(), 8))


@pytest.mark.parametrize("failure", [
    "malformed", "stale", "missing", "foreign-host", "bad-classifier", "nonfinite", "bad-event", "unknown-time", "conflicting-retry",
])
def test_bad_peer_is_honest_without_hiding_good_host(tmp_path, monkeypatch, failure):
    roots = {"roundtop": tmp_path / "roundtop", "renegade": tmp_path / "renegade"}
    _write_feed(roots["roundtop"], "roundtop")
    if failure != "missing":
        stamp = (datetime.now(UTC) - timedelta(seconds=config.STALE_AFTER_SECONDS + 1)).isoformat() if failure == "stale" else None
        peer = _write_feed(roots["renegade"], "renegade", stamp=stamp)
        path = roots["renegade"] / "lanes.json"
        if failure == "malformed":
            path.write_text("{")
        elif failure == "foreign-host":
            peer["host"] = "another-host"
            path.write_text(json.dumps(peer))
        elif failure == "bad-classifier":
            peer["lanes"][0]["classification"]["label"] = "invented-label"
            path.write_text(json.dumps(peer))
        elif failure == "nonfinite":
            peer["health"]["latency"] = float("nan")
            path.write_text(json.dumps(peer))
        elif failure == "unknown-time":
            peer["updated_at"] = "unknown"
            peer["lanes"][0]["classification"]["observed_at"] = None
            peer["lanes"][0]["classification"]["label"] = None
            peer["lanes"][0]["classification"]["reason"] = "all providers unavailable"
            path.write_text(json.dumps(peer))
        elif failure == "bad-event":
            (roots["renegade"] / "events.jsonl").write_text("{\n")
        elif failure == "conflicting-retry":
            log = roots["renegade"] / "events.jsonl"
            original = json.loads(log.read_text().splitlines()[0])
            with log.open("a") as stream:
                stream.write(json.dumps({**original, "kind": "different-state"}) + "\n")
            assert json.loads(log.read_text().splitlines()[-1])["kind"] == "different-state"
        assert path.read_text()
    _configure(monkeypatch, roots)
    client = TestClient(app_module.app)
    view = client.get("/api/state?monitor=all").json()
    assert view["hosts"]["roundtop"]["source"]["errors"] == []
    assert view["hosts"]["roundtop"]["lanes"][0]["classification"]["label"] == "working"
    peer = view["hosts"]["renegade"]
    if failure == "stale":
        assert peer["source"]["data_stale"] is True
        assert peer["lanes"][0]["classification"]["label"] == "working"  # Last observation remains available.
        assert peer["lanes"][0]["movement"]["status"] is None
    else:
        assert peer["source"]["errors"]
        assert client.get("/healthz").json()["ok"] is False
    if failure in {"malformed", "missing", "foreign-host", "nonfinite"}:
        assert peer["lanes"] == [] and peer["health"] is None and peer["source"]["age_seconds"] is None
        assert view["summary"]["lane_count"] is None
    if failure in {"bad-classifier", "unknown-time"}:
        assert peer["lanes"][0]["classification"]["label"] is None
        assert peer["lanes"][0]["classification"]["reason"]
    if failure == "missing":
        assert client.get("/api/monitors").json()[1]["has_state_root"] is False
    # A watcher model cannot authorize writes to a goals root or fabricate an answer transaction.
    assert client.post("/api/lanes/lane-0/answer?monitor=renegade", json={"text": "Proceed"}).status_code == 409
    assert not (roots["renegade"] / "goals.json").exists()

    async def first_state():
        response = await app_module.events("all")
        stream = response.body_iterator
        try:
            return _payload(await asyncio.wait_for(anext(stream), 3))
        finally:
            await stream.aclose()

    streamed = asyncio.run(first_state())
    assert set(streamed["hosts"]) == set(roots)
    assert streamed["hosts"]["renegade"]["source"]["data_stale"] == peer["source"]["data_stale"]

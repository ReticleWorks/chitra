"""boardd endpoint and SSE smoke tests against the bundled fixture state dir."""

import importlib
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "boardd_state"

os.environ["BOARDD_STATE_DIR"] = str(FIXTURE_DIR)

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
    assert data["schema"] == "boardd.state.v1"
    assert len(data["lanes"]) == 10
    assert data["summary"]["needs_you_count"] == 3


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
    assert payload["schema"] == "boardd.state.v1"
    assert len(payload["lanes"]) == 10

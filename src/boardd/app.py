"""boardd — live fleet dashboard. Pure reader over the chitra state dir.

Endpoints:
  GET /            the one-page UI (cockpit, drawer, history)
  GET /api/state   full view JSON — also Ramble's roster read path
  GET /events      Server-Sent Events: initial state, per-change state, heartbeats
  GET /static/*    css/js assets

boardd never writes fleet state and never spawns sessions.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from watchfiles import awatch

from . import config
from .state import build_view
from .translate import TranslationCache

app = FastAPI(title="boardd", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=config.PKG_DIR / "static"), name="static")

_tcache = TranslationCache(config.TRANSLATION_SEED)


def current_view() -> dict[str, Any]:
    return build_view(config.STATE_DIR, _tcache)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.PKG_DIR / "static" / "index.html")


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(current_view())


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/events")
async def events() -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def watcher() -> None:
            try:
                async for _changes in awatch(config.STATE_DIR):
                    await queue.put("change")
            except Exception as e:  # surfaced to the client, not swallowed
                await queue.put(f"watch-error: {e}")

        task = asyncio.create_task(watcher())
        try:
            yield _sse("state", current_view())
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=config.HEARTBEAT_SECONDS)
                    # collapse bursts of change notifications into one push
                    while not queue.empty():
                        queue.get_nowait()
                    if isinstance(msg, str) and msg.startswith("watch-error"):
                        yield _sse("error", {"detail": msg})
                    else:
                        yield _sse("state", current_view())
                except TimeoutError:
                    yield _sse(
                        "heartbeat",
                        {"ts": datetime.now(UTC).isoformat()},
                    )
        finally:
            task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    view = current_view()
    return JSONResponse(
        {
            "ok": not view["source"]["errors"],
            "state_dir": view["source"]["state_dir"],
            "errors": view["source"]["errors"],
        }
    )

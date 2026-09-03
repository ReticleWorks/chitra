"""boardd — live fleet dashboard. Pure reader over discovered chitra state roots.

Endpoints:
  GET  /                       the one-page UI (cockpit, drawer, history)
  GET  /api/monitors           discovered Chitra monitor instances on this host
  GET  /api/state?monitor=     full view JSON for one monitor id, or "all"
  GET  /events?monitor=        Server-Sent Events: initial state, per-change state, heartbeats
  POST /api/lanes/{id}/ack     clear a lane's open asks (chitra-goals resolve-ask --all)
  POST /api/lanes/{id}/answer  clear a lane's open asks with the answer as basis
  GET  /static/*               css/js assets, manifest, service worker

boardd never writes fleet state directly and never spawns sessions; its two
write endpoints shell out to the existing chitra-goals CLI (see actions.py).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from watchfiles import awatch

from chitra.goals import load_goals

from . import actions, agenttrail_bridge, config, discovery
from .state import REVIEW_STATUSES, build_view
from .translate import TranslationCache

app = FastAPI(title="boardd", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=config.PKG_DIR / "static"), name="static")

_tcache = TranslationCache(config.TRANSLATION_SEED)
_agenttrail_status: dict[str, str] = {}  # session_ref -> status, last posted to agenttrail


def current_monitors() -> dict[str, discovery.MonitorInfo]:
    """Discover every monitor on this host and describe each one."""
    roots = discovery.discover_monitors()
    units = {} if discovery.is_dev_mode() else discovery.discover_units()
    out: dict[str, discovery.MonitorInfo] = {}
    for monitor_id, root in roots.items():
        view = build_view(root, _tcache)
        out[monitor_id] = discovery.MonitorInfo(
            id=monitor_id,
            state_root=str(root),
            unit_active_state=units.get(monitor_id, "unknown"),
            lane_count=view["summary"]["lane_count"],
            needs_feedback_count=view["summary"]["needs_you_count"],
            has_state_root=root.is_dir(),
        )
    return out


def _resolve_root(monitor_id: str, roots: dict[str, Path] | None = None) -> Path:
    roots = discovery.discover_monitors() if roots is None else roots
    if monitor_id not in roots:
        raise HTTPException(status_code=404, detail=f"unknown monitor {monitor_id!r}")
    return roots[monitor_id]


def _default_monitor_id(roots: dict[str, Path]) -> str:
    return discovery.DEFAULT_MONITOR_ID if discovery.DEFAULT_MONITOR_ID in roots else sorted(roots)[0]


def _view_for(monitor_id: str | None) -> dict[str, Any]:
    roots = discovery.discover_monitors()
    if not roots:
        return build_view(config.STATE_DIR, _tcache)
    if monitor_id == "all":
        return _combined_view(roots)
    resolved = monitor_id or _default_monitor_id(roots)
    root = _resolve_root(resolved, roots)
    view = build_view(root, _tcache)
    view["monitor"] = resolved
    view["agenttrail_url"] = config.AGENTTRAIL_PUBLIC_URL
    return view


def _combined_view(roots: dict[str, Path]) -> dict[str, Any]:
    """Merge every monitor's lanes/events/needs_you into one view, tagged by monitor id."""
    lanes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    needs_you: list[dict[str, Any]] = []
    errors: list[str] = []
    counts: dict[str, int] = {}
    last_view: dict[str, Any] | None = None
    for monitor_id, root in sorted(roots.items()):
        view = build_view(root, _tcache)
        last_view = view
        for lane in view["lanes"]:
            lanes.append({**lane, "monitor": monitor_id})
        for ev in view["events"]:
            events.append({**ev, "monitor": monitor_id})
        for item in view["needs_you"]:
            needs_you.append({**item, "monitor": monitor_id})
        errors.extend(f"[{monitor_id}] {e}" for e in view["source"]["errors"])
        for status, n in view["summary"]["counts"].items():
            counts[status] = counts.get(status, 0) + n
    needs_you.sort(key=lambda item: item["since"] or "")
    merged = last_view or build_view(config.STATE_DIR, _tcache)
    merged = {**merged, "monitor": "all"}
    merged["source"] = {**merged["source"], "state_dir": "all", "errors": errors}
    merged["lanes"] = lanes
    merged["events"] = events
    merged["needs_you"] = needs_you
    merged["summary"] = {
        "lane_count": len(lanes),
        "counts": counts,
        "sentence": f"{len(roots)} monitor{'s' if len(roots) != 1 else ''}, {len(lanes)} lanes total.",
        "needs_you_count": len(needs_you),
    }
    merged["agenttrail_url"] = config.AGENTTRAIL_PUBLIC_URL
    return merged


def current_view() -> dict[str, Any]:
    return _view_for(None)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.PKG_DIR / "static" / "index.html")


@app.get("/api/monitors")
async def api_monitors() -> JSONResponse:
    return JSONResponse([vars(m) for m in current_monitors().values()])


@app.get("/api/state")
async def api_state(monitor: str | None = None) -> JSONResponse:
    return JSONResponse(_view_for(monitor))


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sync_agenttrail(root: Path) -> None:
    global _agenttrail_status
    try:
        lanes = [record.to_dict() for record in load_goals(root, allow_newer=True)]
    except (ValueError, OSError):
        return
    _agenttrail_status = agenttrail_bridge.sync_lanes(config.AGENTTRAIL_HOOK_URL, config.AGENTTRAIL_CWD, lanes, _agenttrail_status)


@app.get("/events")
async def events(monitor: str | None = None) -> StreamingResponse:
    # Resolved before the stream starts: an unknown monitor id must come back
    # as a normal 404, not fail silently mid-stream.
    roots = discovery.discover_monitors()
    watch_root: Path | None
    if monitor == "all":
        watch_root = None  # no single dir to watch; the client still gets fresh state on the heartbeat/monitors tick
    elif roots:
        watch_root = _resolve_root(monitor or _default_monitor_id(roots), roots)
    else:
        watch_root = config.STATE_DIR

    async def stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def watcher() -> None:
            if watch_root is None:
                return
            try:
                async for _changes in awatch(watch_root):
                    await queue.put("change")
            except Exception as e:  # surfaced to the client, not swallowed
                await queue.put(f"watch-error: {e}")

        task = asyncio.create_task(watcher())
        loop = asyncio.get_event_loop()
        last_monitors_push = loop.time()
        try:
            yield _sse("state", _view_for(monitor))
            if watch_root is not None:
                _sync_agenttrail(watch_root)
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=config.HEARTBEAT_SECONDS)
                    while not queue.empty():
                        queue.get_nowait()
                    if isinstance(msg, str) and msg.startswith("watch-error"):
                        yield _sse("error", {"detail": msg})
                    else:
                        yield _sse("state", _view_for(monitor))
                        if watch_root is not None:
                            _sync_agenttrail(watch_root)
                except TimeoutError:
                    now = loop.time()
                    if now - last_monitors_push >= config.MONITORS_TICK_SECONDS:
                        last_monitors_push = now
                        yield _sse("monitors", [vars(m) for m in current_monitors().values()])
                    yield _sse("heartbeat", {"ts": datetime.now(UTC).isoformat()})
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


def _root_for_write(monitor: str | None) -> Path:
    roots = discovery.discover_monitors()
    if not roots:
        return config.STATE_DIR
    return _resolve_root(monitor or _default_monitor_id(roots), roots)


@app.post("/api/lanes/{lane_id}/ack")
async def ack_lane(lane_id: str, monitor: str | None = None) -> JSONResponse:
    root = _root_for_write(monitor)
    try:
        actions.ack_lane(root, lane_id)
    except actions.LaneActionError as e:
        raise HTTPException(status_code=404 if e.not_found else 502, detail=str(e)) from e
    return JSONResponse({"ok": True})


@app.post("/api/lanes/{lane_id}/answer")
async def answer_lane(lane_id: str, request: Request, monitor: str | None = None) -> JSONResponse:
    root = _root_for_write(monitor)
    body = await request.json()
    text = str(body.get("text", "")) if isinstance(body, dict) else ""
    try:
        actions.answer_lane(root, lane_id, text)
    except actions.LaneActionError as e:
        raise HTTPException(status_code=404 if e.not_found else 400, detail=str(e)) from e
    return JSONResponse({"ok": True})


# Re-exported so callers/tests reach the review-queue statuses via app, not a
# separate import of boardd.state.
__all__ = ["app", "REVIEW_STATUSES"]

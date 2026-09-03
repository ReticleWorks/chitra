"""boardd — live fleet dashboard. Pure reader over discovered chitra state roots.

Endpoints:
  GET  /                       the one-page UI (cockpit, drawer, history)
  GET  /api/monitors           discovered Chitra monitor instances on this host
  GET  /api/state?monitor=     full view JSON for one monitor id, or "all"
  GET  /events?monitor=        Server-Sent Events: initial state, per-change state, heartbeats
  POST /api/lanes/{id}/ack     clear a lane's open asks (chitra-goals resolve-ask --all)
  POST /api/lanes/{id}/answer  clear a lane's open asks with the answer as basis
  GET  /activity/*             proxy onto the co-located, boardd-supervised agenttrail process
  GET  /static/*               css/js assets, manifest, service worker

boardd never writes fleet state directly and never spawns sessions; its two
write endpoints shell out to the existing chitra-goals CLI (see actions.py).
"""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from watchfiles import awatch

from chitra.goals import load_goals

from . import actions, agenttrail_bridge, agenttrail_supervisor, config, discovery
from .state import REVIEW_STATUSES, build_view
from .translate import TranslationCache

_supervisor = agenttrail_supervisor.AgenttrailSupervisor()


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # BOARDD_DEV=1 (tests, local non-systemd smoke) never spawns a real
    # Node process — same escape hatch discovery.py already uses.
    if not discovery.is_dev_mode():
        _supervisor.start()
    try:
        yield
    finally:
        if not discovery.is_dev_mode():
            _supervisor.stop()


app = FastAPI(title="boardd", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=config.PKG_DIR / "static"), name="static")

MAX_REQUEST_BODY_BYTES = 64 * 1024


class AnswerBody(BaseModel):
    text: str = Field(default="", max_length=4096)


async def _read_bounded_json(request: Request, model: type[BaseModel]) -> BaseModel:
    """Read+validate a JSON body, bounded before it ever reaches a subprocess argv.

    413 over the size cap, 400 for anything that isn't a valid JSON object
    matching ``model`` (empty body included) — never an unhandled 500.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            too_big = int(content_length) > MAX_REQUEST_BODY_BYTES
        except ValueError:
            too_big = False  # malformed header; the real read below still enforces the cap
        if too_big:
            raise HTTPException(status_code=413, detail="request body exceeds 64 KB")
    body = await request.body()
    if len(body) > MAX_REQUEST_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request body exceeds 64 KB")
    if not body.strip():
        raise HTTPException(status_code=400, detail="request body must be valid JSON")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"request body must be valid JSON: {e}") from e
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

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


# Paths agenttrail's own public/index.html actually fetches from its UI —
# the only ones proxied. Everything else, including every mutating route
# (`/spawn` takes an arbitrary filesystem path from an unauthenticated POST
# and launches a detached Node process — see NOTICE.md), is refused: this
# route only ever registers GET, and an unlisted path 404s before an
# upstream request is even made.
ACTIVITY_ALLOWED_PATHS = frozenset({"", "world", "tree-of", "escalations", "events"})


# A blocking upstream read runs in the default executor's fixed-size thread
# pool. A synchronous socket read can't be cancelled from an awaiting
# coroutine, so a client that vanishes mid-stream (closes the tab) while a
# read is in flight leaves that thread pinned until the read itself returns.
# Bound every read so an abandoned connection is reclaimed within one
# timeout instead of indefinitely.
#
# request.is_disconnected() was tried here and dropped: polling it from
# inside a StreamingResponse generator raced the ASGI server's own use of
# the same receive channel and, measured against the live process, could
# leave the *entire* app unresponsive to new connections for the length of
# the read timeout — a known Starlette footgun, not specific to this proxy.
# Ending the stream on a read timeout is simpler and carries no such risk:
# EventSource reconnects automatically, so an idle Activity tab just
# reconnects roughly every UPSTREAM_READ_TIMEOUT seconds instead of holding
# one connection open forever.
UPSTREAM_READ_TIMEOUT = 30.0


async def _proxy_get(url: str) -> StreamingResponse:
    loop = asyncio.get_event_loop()

    def _connect() -> tuple[int, str, Any]:
        try:
            resp = urllib.request.urlopen(url, timeout=UPSTREAM_READ_TIMEOUT)  # noqa: S310 — loopback, GET-only, allowlisted
            return resp.status, resp.headers.get("content-type", "application/octet-stream"), resp
        except urllib.error.HTTPError as e:
            content_type = e.headers.get("content-type", "text/plain") if e.headers else "text/plain"
            return e.code, content_type, e

    try:
        status, content_type, upstream = await loop.run_in_executor(None, _connect)
    except OSError as e:
        raise HTTPException(status_code=502, detail=f"agenttrail unreachable: {e}") from e

    async def body() -> AsyncIterator[bytes]:
        # .read1(), not .read(): for a chunked, still-open stream (agenttrail's
        # SSE /events), .read(n) blocks until n bytes have accumulated or the
        # connection closes — measured against the live process, it silently
        # sat for 5+ seconds after the initial event that had already
        # arrived. .read1() returns whatever is already available, the way a
        # passthrough proxy needs.
        read1 = getattr(upstream, "read1", None)
        reader = read1 or upstream.read
        try:
            while True:
                try:
                    chunk = await loop.run_in_executor(None, reader, 8192)
                except TimeoutError:
                    break  # idle this long — end the stream; the client reconnects
                if not chunk:
                    break
                yield chunk
        finally:
            # Not a plain upstream.close(): io.BufferedReader (what
            # http.client hands back) serializes read/close behind one
            # internal lock. On client disconnect, cancellation can land
            # here *while* a read from the loop above is still in flight
            # in its executor thread, holding that lock — a same-thread
            # close() then blocks the whole event loop (not just this
            # request) until that read returns, measured against the live
            # process as a 30-second stall of the entire app. Closing in
            # the executor keeps the wait off the event loop.
            await loop.run_in_executor(None, upstream.close)

    return StreamingResponse(body(), status_code=status, media_type=content_type)


@app.get("/activity/{path:path}")
async def activity_proxy(path: str, request: Request) -> StreamingResponse:
    if path not in ACTIVITY_ALLOWED_PATHS:
        raise HTTPException(status_code=404, detail="not proxied")
    port = agenttrail_supervisor.agenttrail_port()
    url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    return await _proxy_get(url)


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


def _lane_action_status(e: actions.LaneActionError, *, default: int) -> int:
    if e.not_found:
        return 404
    if e.no_op:
        return 409  # nothing to resolve — never a false-success 200
    return default


@app.post("/api/lanes/{lane_id}/ack")
async def ack_lane(lane_id: str, monitor: str | None = None) -> JSONResponse:
    root = _root_for_write(monitor)
    try:
        record = actions.ack_lane(root, lane_id)
    except actions.LaneActionError as e:
        raise HTTPException(status_code=_lane_action_status(e, default=502), detail=str(e)) from e
    return JSONResponse({"ok": True, "changed": True, "lane": record.to_dict()})


@app.post("/api/lanes/{lane_id}/answer")
async def answer_lane(lane_id: str, request: Request, monitor: str | None = None) -> JSONResponse:
    body = await _read_bounded_json(request, AnswerBody)
    assert isinstance(body, AnswerBody)
    root = _root_for_write(monitor)
    try:
        record = actions.answer_lane(root, lane_id, body.text)
    except actions.LaneActionError as e:
        raise HTTPException(status_code=_lane_action_status(e, default=400), detail=str(e)) from e
    return JSONResponse({"ok": True, "changed": True, "lane": record.to_dict()})


# Re-exported so callers/tests reach the review-queue statuses via app, not a
# separate import of boardd.state.
__all__ = ["app", "REVIEW_STATUSES"]

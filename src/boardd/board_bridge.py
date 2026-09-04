"""Feed the board from chitra goal state.

The board is the vendored agenttrail page (src/boardd/vendor/agenttrail).
It reads three things out of the workspace directory the agenttrail process
was started against:

- ``PLAN.md``   — one ``##`` component per lane; that component IS the
                  session card on the canvas.
- ``roster.json`` — its ``escalations`` object; that object IS the red
                  escalation stack on the right edge.
- ``/hook`` POSTs — Claude-Code-shaped events that light a card up live.

This module writes the first two and drives the third, the same way the
Orchestra board's own ``bridge.py`` did on 2026-09-01, but reading chitra
GoalRecords instead of process tables and transcripts.

Nothing here writes chitra state. The workspace directory is boardd's own
(``BOARDD_AGENTTRAIL_CWD``) and must never be a chitra state root: the SSE
watcher in app.py watches those, and a render into one would loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from watchfiles import awatch

from chitra.goals import load_goals, session_name

from . import agenttrail_bridge, config, discovery

logger = logging.getLogger("boardd.board_bridge")

# Marker vocabulary, unchanged from the approved board: [~] working,
# [!] needs the operator, [x] done, [ ] idle or held.
WORKING = "~"
NEEDS_INPUT = "!"
DONE = "x"
IDLE = " "

# Statuses that put a lane in front of the operator even with no literal
# ask. Same set boardd's own review queue uses (state.REVIEW_STATUSES) plus
# the two done states that still want a human, so the stack and the JSON
# API never disagree about who needs attention.
NEEDS_INPUT_STATUSES = frozenset(
    {"blocked", "turn-finished-unverified", "completion-disputed", "done-pending-verification"}
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    """An agenttrail node id: ``[a-z0-9][a-z0-9-]*``, never empty."""
    out = _SLUG_RE.sub("-", text.lower()).strip("-")
    return out if out and out[0].isalnum() else f"lane-{out}" if out else "lane"


def clean(text: str, limit: int = 0) -> str:
    """One line, collapsed whitespace, and never a stray ``{#`` that would
    read as a node id to agenttrail's PLAN.md parser."""
    out = " ".join(str(text or "").split()).replace("{#", "{ #")
    if limit and len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def local_hm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return str(iso)[11:16]


def lane_key(record: dict[str, Any]) -> str:
    """The name the board shows and ``POST /answer`` routes back on.

    ``actions._find_record`` accepts either a lane_id or a session_ref, so
    whichever this returns is a valid write target.
    """
    return str(record.get("lane_id") or session_name(str(record.get("session_ref", ""))) or "lane")


def mark_for(record: dict[str, Any]) -> str:
    """Map one chitra status onto the board's four markers.

    An open ask wins over everything else: it is a live, unanswered request
    to the operator no matter what the lane's status says.
    """
    if record.get("open_asks"):
        return NEEDS_INPUT
    status = record.get("status", "")
    if status in NEEDS_INPUT_STATUSES:
        return NEEDS_INPUT
    if status == "working":
        return WORKING
    if status == "done-pending-close":
        return DONE
    return IDLE


def ask_of(record: dict[str, Any]) -> str:
    """The sentence the operator is being asked. Falls back to the reason
    the lane is in the queue when it carries no literal ask."""
    asks = [clean(a) for a in record.get("open_asks", ()) if clean(a)]
    if asks:
        return " ".join(asks)
    hold = clean(record.get("hold_reason", ""))
    if hold:
        return hold
    status = record.get("status", "")
    return f"Status is {status} — review this lane." if status else ""


def recommendation_of(record: dict[str, Any]) -> str:
    """Chitra's own suggested action when the record carries one.

    Foreground tasks are exactly that: durable items chitra raised for its
    foreground agent. Failing that, the hold reason, then the proof the
    lane still owes.
    """
    for task in record.get("foreground_tasks", ()) or ():
        text = clean(task.get("text", "")) if isinstance(task, dict) else ""
        if text:
            return text
    hold = clean(record.get("hold_reason", ""))
    if hold:
        return hold
    owed = [
        clean(str(item.get("required_receipt", "")))
        for item in (record.get("enrolled_done_when_items", ()) or ())
        if isinstance(item, dict) and item.get("required_receipt")
    ]
    if owed:
        return "Still owed as proof: " + ", ".join(owed)
    return clean(record.get("done_when", "")) or "No suggested action recorded."


def context_of(record: dict[str, Any]) -> str:
    """Goal, what the lane says it is doing, and when it was last verified —
    the three lines the answer panel's Context section carried."""
    parts = [clean(record.get("goal", ""))]
    now = clean(record.get("now", "")) or clean(record.get("hold_reason", ""))
    if now:
        parts.append(f"Now: {now}")
    verified = clean(record.get("last_verified", ""))
    parts.append(f"Last verified: {verified}" if verified else "Never verified.")
    return "\n".join(p for p in parts if p)


def find_fields(record: dict[str, Any], monitor_id: str) -> dict[str, str]:
    """The "Find the session" reveal.

    ``peer``/``tty``/``sid``/``kind`` are the four rows the board already
    renders; ``monitor``, ``lane`` and ``how`` ride along as extra rows.
    The tmux session name is the lane id — chitra's own lane_anchor asserts
    that equality (``goal.lane_id != lane.tmux_session`` is an error there),
    so no second lookup is needed.
    """
    session_ref = str(record.get("session_ref", ""))
    host = session_ref.split(":")[0] if ":" in session_ref else ""
    lane_id = str(record.get("lane_id") or "")
    return {
        "peer": host,
        "tty": f"{lane_id}:0.0" if lane_id else "",
        "sid": session_ref,
        "kind": "chitra",
        "monitor": monitor_id,
        "lane": lane_key(record),
        "how": "Send to session writes the answer through chitra-goals resolve-ask; the monitor delivers it to the lane.",
    }


# ------------------------------------------------------------------ render


def render_plan(sections: list[tuple[str, list[dict[str, Any]]]]) -> str:
    """PLAN.md: one component per lane, grouped by monitor.

    agenttrail's plan convention has no node above a component, so a
    "section per monitor" is a contiguous run of that monitor's lanes with
    monitor-prefixed ids — the cards land together on the canvas and the
    ids stay unique across monitors.
    """
    out = [
        "# Chitra board",
        "",
        "One card per lane, fed from chitra goal state. Markers: [~] working · "
        "[!] waiting on you (an open ask, or a status that needs review) · "
        "[x] done, pending close · [ ] idle or held. Regenerated by boardd; do not edit by hand.",
        "",
    ]
    for monitor_id, records in sections:
        for record in records:
            key = lane_key(record)
            cid = slug(f"{monitor_id}-{key}") if len(sections) > 1 else slug(key)
            goal = clean(record.get("goal", "")) or "no goal recorded"
            out.append(f"## {key} — {clean(goal, 70)} {{#{cid}}}")

            tech = ["chitra", str(record.get("session_ref", "")), f"monitor {monitor_id}"]
            if record.get("scope"):
                tech.append("scope " + clean(record["scope"], 60))
            if record.get("goal_version"):
                tech.append(f"goal v{record['goal_version']}")
            out.append("tech: " + " · ".join(t for t in tech if t))

            mark = mark_for(record)
            out.append(f"- [{mark}] {goal} {{#{cid}-goal}}")
            out.append("  by: chitra")
            if mark == NEEDS_INPUT:
                stamp = local_hm(str(record.get("updated_at", "")))
                line = f"NEEDS-INPUT {stamp} — {clean(ask_of(record), 240)}"
            else:
                line = clean(record.get("now", "") or record.get("hold_reason", ""), 240) or "no movement reported"
            out.append(f"  tech: {line}")
            out.append("")
    return "\n".join(out)


def render_roster(sections: list[tuple[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    """roster.json: every [!] lane, in the shape the board's /escalations
    route reads and its answer panel renders."""
    escalations: dict[str, Any] = {}
    for monitor_id, records in sections:
        for record in records:
            if mark_for(record) != NEEDS_INPUT:
                continue
            escalations[lane_key(record)] = {
                "at": record.get("updated_at", ""),
                "goal": clean(record.get("goal", "")),
                "question": clean(ask_of(record)),
                "context": context_of(record),
                "recommendation": recommendation_of(record),
                "find": {k: v for k, v in find_fields(record, monitor_id).items() if v},
            }
    return {"updated": datetime.now().astimezone().isoformat(timespec="seconds"), "escalations": escalations}


# ------------------------------------------------------------------ driver


class BoardBridge:
    """Renders the board's two input files and posts its hook events.

    One instance per boardd process. ``monitor_filter`` is the ``?monitor=``
    the operator last asked for.

    ponytail: one process-wide filter, not one per viewer — this is a
    loopback board with one operator in front of it. Per-viewer filtering
    would need agenttrail itself to filter, which it cannot.
    """

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace or config.AGENTTRAIL_CWD)
        self.monitor_filter: str | None = None
        self._statuses: dict[str, dict[str, str]] = {}
        self._plan_text: str | None = None
        self._roster_text: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- state -> files ------------------------------------------------

    def sections(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Every discovered monitor's records, honouring ``monitor_filter``."""
        roots = discovery.discover_monitors() or {discovery.DEFAULT_MONITOR_ID: config.STATE_DIR}
        out: list[tuple[str, list[dict[str, Any]]]] = []
        for monitor_id, root in sorted(roots.items()):
            if self.monitor_filter and self.monitor_filter not in ("all", monitor_id):
                continue
            try:
                records = [r.to_dict() for r in load_goals(root, allow_newer=True)]
            except (ValueError, OSError) as e:
                logger.warning("monitor %s unreadable, rendered as empty: %s", monitor_id, e)
                records = []
            out.append((monitor_id, records))
        return out

    def render(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Write PLAN.md and roster.json if either changed. Returns the
        sections it rendered so the caller can post hook events for them."""
        sections = self.sections()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._plan_text = self._write_if_changed("PLAN.md", render_plan(sections), self._plan_text)
        roster = json.dumps(render_roster(sections), indent=2, ensure_ascii=False)
        # The timestamp changes every render; compare on the escalations only.
        key = json.dumps(json.loads(roster)["escalations"], sort_keys=True)
        if key != self._roster_text:
            self._write_if_changed("roster.json", roster, None)
            self._roster_text = key
        return sections

    def _write_if_changed(self, name: str, text: str, previous: str | None) -> str:
        if text == previous:
            return text
        path = self.workspace / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(path)
        return text

    def post_hooks(self, sections: list[tuple[str, list[dict[str, Any]]]]) -> None:
        """Synthesize agenttrail hook events for every changed lane, so the
        card moves without waiting for the next PLAN.md read."""
        for monitor_id, records in sections:
            prev = self._statuses.get(monitor_id, {})
            self._statuses[monitor_id] = agenttrail_bridge.sync_lanes(
                config.AGENTTRAIL_HOOK_URL, str(self.workspace), records, prev
            )

    def tick(self) -> None:
        """One full render + publish. Blocking; call it off the event loop."""
        self.post_hooks(self.render())

    def set_monitor(self, monitor: str | None) -> None:
        """Change the ``?monitor=`` filter and re-render at once."""
        if monitor == self.monitor_filter:
            return
        self.monitor_filter = monitor
        self._plan_text = self._roster_text = None
        self.tick()

    # -- loop ----------------------------------------------------------

    async def run(self) -> None:
        """Re-render on any state-file change, and at least every 30 s."""
        while not self._stop.is_set():
            roots = list(discovery.discover_monitors().values()) or [config.STATE_DIR]
            watched = {p for p in roots if p.is_dir()}
            try:
                await asyncio.to_thread(self.tick)
                if not watched:  # nothing to watch yet — wait out the tick and re-discover
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), config.MONITORS_TICK_SECONDS)
                    continue
                async for _changes in awatch(
                    *watched,
                    stop_event=self._stop,
                    rust_timeout=int(config.MONITORS_TICK_SECONDS * 1000),
                    yield_on_timeout=True,
                ):
                    if self._stop.is_set():
                        return
                    await asyncio.to_thread(self.tick)
                    if watched != {p for p in discovery.discover_monitors().values() if p.is_dir()}:
                        break  # a monitor came or went; rebuild the watcher
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad tick must never end the bridge
                logger.exception("board bridge tick failed; retrying")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), config.MONITORS_TICK_SECONDS)

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="board-bridge")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

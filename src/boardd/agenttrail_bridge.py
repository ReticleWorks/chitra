"""Drive the vendored agenttrail process from boardd's own state changes.

See src/boardd/vendor/agenttrail/NOTICE.md for why boardd proxies through
hook events instead of re-implementing agenttrail's UI: its `public/`
page is the client half of a single Node service with its own repo-rooted
world model (PLAN.md components, a multi-board registry, graph layout) that
has no chitra analog. The one clean, already-proven seam is `/hook` — the
same Claude Code hook event shape the Orchestra board bridge
(bridge.py, this repo's sibling deployment) already posts successfully.

boardd never depends on agenttrail being reachable: every call here is
best-effort, swallows its own errors, and logs one line on failure. A lane
change that fails to reach agenttrail is not lost — it is still in
goals.json and still shows on boardd's own page.
"""

import contextlib
import json
import urllib.request
from typing import Any

AGENTTRAIL_TOOL_NAME = "chitra-lane"


def hook_events_for_lane(prev_status: str | None, lane: dict[str, Any], *, session_id: str, cwd: str) -> list[dict[str, Any]]:
    """Map one lane's status transition to Claude-Code-shaped hook events.

    ``lane`` is one GoalRecord-derived dict (session_ref, status, now,
    hold_reason). ``prev_status`` is None on first sight of this lane.
    """
    status = lane.get("status", "")
    now_text = (lane.get("now") or lane.get("hold_reason") or status or "").strip()
    events: list[dict[str, Any]] = []
    if prev_status is None:
        events.append({"hook_event_name": "SessionStart"})
    if status in ("idle", "done-pending-close"):
        events.append({"hook_event_name": "Stop"})
    elif now_text:
        detail = now_text[:200]
        events.append({"hook_event_name": "PreToolUse", "tool_name": AGENTTRAIL_TOOL_NAME, "tool_input": {"description": detail}})
        events.append({"hook_event_name": "PostToolUse", "tool_name": AGENTTRAIL_TOOL_NAME, "tool_input": {"description": detail}})
    for ev in events:
        ev["session_id"] = session_id
        ev["cwd"] = cwd
        ev["agent"] = "chitra"
    return events


def post_hook_event(hook_url: str, event: dict[str, Any], *, timeout: float = 2.0) -> None:
    body = json.dumps(event, default=str).encode()
    req = urllib.request.Request(hook_url, data=body, headers={"content-type": "application/json"})
    # ponytail: best-effort side channel — agenttrail being down never blocks boardd
    with contextlib.suppress(OSError):
        urllib.request.urlopen(req, timeout=timeout).read()


def sync_lanes(hook_url: str, cwd: str, lanes: list[dict[str, Any]], prev_statuses: dict[str, str]) -> dict[str, str]:
    """Diff lanes against prev_statuses, post events for every change, return the new snapshot."""
    next_statuses: dict[str, str] = {}
    for lane in lanes:
        session_ref = lane.get("session_ref", "")
        if not session_ref:
            continue
        status = lane.get("status", "")
        prev = prev_statuses.get(session_ref)
        next_statuses[session_ref] = status
        if prev == status:
            continue
        for event in hook_events_for_lane(prev, lane, session_id=session_ref, cwd=cwd):
            post_hook_event(hook_url, event)
    return next_statuses

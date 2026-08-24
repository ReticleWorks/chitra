"""State loading and view building.

boardd is a pure reader. This module reads the daemon-owned goals and digest
files plus canonical joined-lane records, never writes them, and builds the
single view dict served by /api/state and pushed over SSE.

Honesty rules enforced here, not in the template:
- Agent-reported results always carry verified=False and the UI mark
  "Boardd has not verified this." There is no code path that sets
  verified=True without an evidence record.
- A done-when condition renders as machine-tracked ONLY if the goal carries
  an evidence binding for it. The mock state carries none, so every
  condition says so in plain words.
- Stale data states its age; ages are computed from file timestamps, never
  invented.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chitra.joined_lane import JoinedLaneError, JoinedLaneStore
from chitra.session_contract import JoinedLaneRecord, Problem, RoadmapStep
from chitra.session_view import GoalProjection, JoinedSessionView, build_joined_session_view

from .config import DIGEST_FILE, GOALS_FILE, STALE_AFTER_SECONDS
from .translate import TranslationCache

# ---------------------------------------------------------------- loading


def load_state_files(state_dir: Path) -> dict[str, Any]:
    """Read the two state files. Missing or bad files are reported, not hidden."""
    errors: list[str] = []
    out: dict[str, Any] = {"goals": None, "digest": None, "errors": errors}
    for name, slot in ((GOALS_FILE, "goals"), (DIGEST_FILE, "digest")):
        p = state_dir / name
        try:
            out[slot] = json.loads(p.read_text())
        except FileNotFoundError:
            errors.append(f"{name} not found in {state_dir}")
        except (json.JSONDecodeError, OSError) as e:
            errors.append(f"{name} unreadable: {e}")
    return out


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _event_ts(hhmm: str, sweep_at: datetime | None) -> str | None:
    """Digest events carry 'HH:MM' only; anchor them to the sweep date."""
    if sweep_at is None or not re.fullmatch(r"\d{2}:\d{2}", hhmm or ""):
        return None
    h, m = hhmm.split(":")
    return sweep_at.replace(hour=int(h), minute=int(m), second=0).isoformat()


@dataclass(frozen=True, slots=True)
class _GoalProjection(GoalProjection):
    """The goal fields needed to join a lane report without a second store."""

    goal_id: str
    lane_id: str
    goal: str
    done_when: str
    status: str
    snapshot: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _JoinedReport:
    record: JoinedLaneRecord
    view: JoinedSessionView


def _goal_entries(goals_doc: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    entries = goals_doc.get("goals", [])
    if not isinstance(entries, list):
        return ()
    return tuple(entry for entry in entries if isinstance(entry, dict))


def _goal_for_joined(record: JoinedLaneRecord, goals: tuple[dict[str, Any], ...]) -> _GoalProjection | None:
    """Join a lane to its existing goal identity, including physical rotation."""

    candidate = next((goal for goal in goals if goal.get("session_ref") == record.session_ref), None)
    if candidate is None:
        candidate = next((goal for goal in goals if goal.get("goal_id") == record.goal_id), None)
    if candidate is None:
        candidate = next((goal for goal in goals if goal.get("lane_id") == record.lane_id), None)
    if candidate is None:
        return None
    return _GoalProjection(
        goal_id=str(candidate.get("goal_id") or record.goal_id),
        lane_id=str(candidate.get("lane_id") or record.lane_id),
        goal=str(candidate.get("goal") or ""),
        done_when=str(candidate.get("done_when") or ""),
        status=str(candidate.get("status") or "working"),
        snapshot=dict(candidate),
    )


def _load_joined_lanes(state_dir: Path) -> tuple[tuple[JoinedLaneRecord, ...], list[str]]:
    """Read canonical joined records and surface corruption as source errors."""

    try:
        records = tuple(JoinedLaneStore(state_dir).list())
    except (JoinedLaneError, OSError) as exc:
        return (), [f"joined-lanes unreadable: {exc}"]
    return records, []


def _line(tc: TranslationCache, value: str | None) -> dict[str, Any] | None:
    return tc.get(value) if value else None


def _step_payload(step: RoadmapStep, tc: TranslationCache) -> dict[str, Any]:
    return {
        "id": step.id,
        "status": step.status,
        "title": tc.get(step.title or step.id),
        "owner": step.owner or None,
        "milestone_id": step.milestone_id,
    }


def _problem_payload(problem: Problem, tc: TranslationCache) -> dict[str, Any]:
    return {
        "id": problem.id,
        "summary": tc.get(problem.summary),
        "owner": problem.owner,
        "state": problem.state,
        "need": _line(tc, problem.need),
        "resolution": _line(tc, problem.resolution),
        "reopen_event": _line(tc, problem.reopen_event),
    }


def _close_evidence_payload(view: JoinedSessionView, tc: TranslationCache) -> dict[str, Any] | None:
    """Expose recorded close facts without provider payloads or controls."""

    close = view.close_evidence
    if close is None:
        return None
    return {
        "state": close.state,
        "provider_thread_ref": close.provider_thread_ref,
        "same_provider_thread": close.same_provider_thread,
        "later_resume_supported": close.later_resume_supported,
        "checkpoint_ref": close.checkpoint_ref,
        "quiescent": close.quiescent,
        "observed_at": close.observed_at,
        "evidence": tc.get(close.evidence),
    }


def _joined_payload(
    view: JoinedSessionView,
    tc: TranslationCache,
    *,
    owner_id: str,
    owner_role: str,
) -> dict[str, Any]:
    """Return only user-facing joined fields for the board browser payload."""

    progress = view.progress
    current_step = view.current_step
    next_check = view.next_check
    provider = view.provider
    update = view.steps
    return {
        "schema": view.schema,
        "lane_id": view.lane_id,
        "goal_id": view.goal_id,
        "goal_version": view.goal_version,
        "session_ref": view.session_ref,
        "lifecycle": view.lifecycle,
        "physical_session_generation": view.physical_session_generation,
        "goal": _line(tc, view.goal) or tc.get("unavailable (goal record not joined)"),
        "done_when": _line(tc, view.done_when) or tc.get("unavailable (goal record not joined)"),
        "goal_status": view.goal_status,
        "goal_snapshot": view.goal_snapshot,
        "progress": None
        if progress is None
        else {
            "percentage": progress.percentage,
            "completed_steps": progress.completed_steps,
            "total_steps": progress.total_steps,
            "reason": progress.reason,
        },
        "roadmap": {
            "version": view.plan_version,
            "assessment": view.plan_state,
            "assessment_reason": _line(tc, view.plan_assessment_reason),
            "revision_note": _line(tc, view.plan_revision_note),
            "position": None
            if current_step is None
            else _step_payload(current_step, tc),
            "steps": [_step_payload(step, tc) for step in update],
        },
        "now": _line(tc, view.current_work),
        "next": _line(tc, view.next_action),
        "next_check": None
        if next_check is None
        else {
            "at": next_check.at,
            "reason": tc.get(next_check.reason),
            "wake_condition": _line(tc, next_check.wake_condition),
        },
        "owner": {
            "id": view.owner or owner_id,
            "role": "lane-step" if view.owner else owner_role,
        },
        "provider": {
            "kind": provider.kind,
            "handle": provider.handle,
            "generation": provider.generation,
        },
        "open_problems": [_problem_payload(problem, tc) for problem in view.open_problems],
        "resolved_problems": [_problem_payload(problem, tc) for problem in view.resolved_problems],
        "chitra_action": _line(tc, view.chitra_action),
        "recovery_action": tc.get(view.recovery.attempted_remedy or "none recorded."),
        "reframe_progress": {
            "active": bool(view.tactical_objective or view.tactical_plan),
            "stage": view.recovery_stage,
            "attempt_count": view.recovery_attempt_count,
            "objective": _line(tc, view.tactical_objective),
            "steps": [_line(tc, step) for step in view.tactical_plan],
        },
        "tactical_plan": {
            "objective": _line(tc, view.tactical_objective),
            "steps": [_line(tc, step) for step in view.tactical_plan],
        },
        "handoff": {
            "status": view.handoff_status,
            "id": view.handoff_id,
            "reference": view.handoff_reference,
            "digest": view.handoff_digest,
        },
        "checkpoint_reference": view.checkpoint_reference,
        "pending_operation": None
        if view.pending_operation is None
        else {
            "operation_id": view.pending_operation.operation_id,
            "kind": view.pending_operation.kind,
            "provider_handle": view.pending_operation.provider_handle,
            "provider_session_id": view.pending_operation.provider_session_id,
            "created_at": view.pending_operation.created_at,
            "attempt": view.pending_operation.attempt,
        },
        "close_evidence": _close_evidence_payload(view, tc),
        "resume_state": view.resume_state,
        "last_useful_progress": None
        if view.last_useful_progress is None
        else {
            "summary": tc.get(view.last_useful_progress.summary),
            "observed_at": view.last_useful_progress.observed_at,
            "update_sequence": view.last_useful_progress.update_sequence,
        },
        "observed_at": view.observed_at,
        "update_sequence": view.update_sequence,
    }


def _joined_reports(
    records: tuple[JoinedLaneRecord, ...],
    goals: tuple[dict[str, Any], ...],
) -> tuple[tuple[_JoinedReport, ...], list[str]]:
    reports: list[_JoinedReport] = []
    errors: list[str] = []
    for record in records:
        goal = _goal_for_joined(record, goals)
        try:
            view = build_joined_session_view(record, goal=goal)
        except ValueError as exc:
            errors.append(f"joined-lane {record.lane_id} omitted: {exc}")
            continue
        reports.append(_JoinedReport(record=record, view=view))
    return tuple(reports), errors


def _report_for_goal(goal: dict[str, Any], reports: tuple[_JoinedReport, ...]) -> _JoinedReport | None:
    session_ref = goal.get("session_ref")
    goal_id = goal.get("goal_id")
    lane_id = goal.get("lane_id")
    for report in reports:
        if session_ref and report.record.session_ref == session_ref:
            return report
    for report in reports:
        if goal_id and report.record.goal_id == goal_id:
            return report
    for report in reports:
        if lane_id and report.record.lane_id == lane_id:
            return report
    return None


# ------------------------------------------------------------ done-when


def split_conditions(done_when: str) -> list[str]:
    return [c.strip() for c in (done_when or "").split(";") if c.strip()]


def build_done_when(goal: dict[str, Any], tc: TranslationCache) -> dict[str, Any]:
    """Build the condition list with per-condition evidence labels.

    Evidence bindings, when the daemons provide them, are expected as
    goal["evidence"]: a list of {condition, verified, method, at}. The mock
    state has none, so every condition is honestly unbound.
    """
    conditions: list[dict[str, Any]] = []
    evidence = {e.get("condition"): e for e in goal.get("evidence", [])}
    proven = 0
    tracked = 0
    for text in split_conditions(goal.get("done_when", "")):
        ev = evidence.get(text)
        if ev is None:
            proof = {
                "state": "unbound",
                "label": "Not checked automatically — no evidence source is linked. Needs review to call done.",
            }
        elif ev.get("verified"):
            proven += 1
            tracked += 1
            proof = {
                "state": "verified",
                "label": f"Proven by {ev.get('method', 'an automatic check')} at {ev.get('at', 'an unrecorded time')}.",
            }
        else:
            tracked += 1
            proof = {
                "state": "pending",
                "label": f"Checked automatically by {ev.get('method', 'an automatic check')}; no passing evidence yet.",
            }
        conditions.append({**tc.get(text), "proof": proof})

    total = len(conditions)
    unproven = total - proven
    # Plain sentences only. The banned phrasing ("N of M machine-checkable
    # conditions is verified") must not reappear.
    if total == 0:
        summary = "No finish conditions are recorded for this lane."
    elif proven == total:
        summary = f"All {_num(total)} finish conditions are proven."
    elif proven == 0 and tracked == 0:
        summary = (
            f"None of the {_num(total)} finish conditions has automatic proof yet. "
            "All of them need review."
        )
    elif proven == 0:
        summary = (
            f"None of the {_num(total)} finish conditions is proven yet. "
            f"{_num(tracked).capitalize()} are checked automatically; the rest need review."
        )
    else:
        summary = (
            f"{_num(proven).capitalize()} of {_num(total)} finish conditions "
            f"{'is' if proven == 1 else 'are'} proven. "
            f"The other {_num(unproven)} need{'s' if unproven == 1 else ''} review."
        )
    return {"conditions": conditions, "proven": proven, "total": total, "summary": summary}


_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def _num(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


# ------------------------------------------------------------- movement

STATUS_PILLS = {
    "working": ("ok", "Moving"),
    "held": ("hold", "Held"),
    "blocked": ("bad", "Blocked"),
    "idle": ("warn", "Quiet"),
    "turn-finished-unverified": ("warn", "Awaiting audit"),
    "done-pending-verification": ("ok", "Done, pending proof"),
}


def build_movement(goal: dict[str, Any], tc: TranslationCache) -> dict[str, Any]:
    status = goal.get("status", "unknown")
    tone, label = STATUS_PILLS.get(status, ("hold", status or "Unknown"))
    now_line = tc.get(goal.get("now", ""))
    hold = tc.get(goal.get("hold_reason", "")) if goal.get("hold_reason") else None
    if status == "blocked" and goal.get("open_asks"):
        sentence = "The lane is waiting on your answer below."
    elif status == "held" and hold:
        sentence = hold["text"]
    elif status == "turn-finished-unverified":
        sentence = "The agent reports its turn finished. The completion audit has not confirmed it."
    elif status == "done-pending-verification":
        sentence = "The work is reported done; the proof window is still running."
    elif status == "idle":
        sentence = "No session is active on this lane."
    else:
        sentence = now_line["text"]
    return {
        "status": status,
        "pill": {"tone": tone, "label": label},
        "sentence": sentence,
        "now": now_line,
        "hold_reason": hold,
    }


# ---------------------------------------------------------- scope delta


def build_scope(goal: dict[str, Any], tc: TranslationCache) -> dict[str, Any]:
    current = goal.get("done_when", "")
    enrolled = goal.get("enrolled_done_when", "")
    if not enrolled or enrolled.strip().lower() == "same as done_when" or enrolled == current:
        return {"narrowed": False, "dropped": []}
    cur = set(split_conditions(current))
    dropped = [tc.get(c) for c in split_conditions(enrolled) if c not in cur]
    return {"narrowed": bool(dropped), "dropped": dropped}


# ------------------------------------------------------------ the view


def build_view(state_dir: Path, tc: TranslationCache, now: datetime | None = None) -> dict[str, Any]:
    raw = load_state_files(state_dir)
    now = now or datetime.now(UTC)
    goals_doc = raw["goals"] or {}
    digest_doc = raw["digest"] or {}
    goals = _goal_entries(goals_doc)
    joined_records, joined_errors = _load_joined_lanes(state_dir)
    raw["errors"].extend(joined_errors)
    joined_reports, report_errors = _joined_reports(joined_records, goals)
    raw["errors"].extend(report_errors)

    goals_at = _parse_ts(goals_doc.get("updated_at"))
    sweep_at = _parse_ts(digest_doc.get("sweep_at"))

    status_by_lane = {g.get("session_ref"): g.get("status") for g in goals}
    events = []
    for ev in digest_doc.get("events", []):
        events.append(
            {
                "lane": ev.get("lane"),
                "ts": _event_ts(ev.get("ts", ""), sweep_at),
                "ts_raw": ev.get("ts"),
                "category": categorize_event(ev, status_by_lane.get(ev.get("lane"))),
                **{"summary": tc.get(ev.get("text", ""))},
                # Digest summaries are monitor-authored reports of agent work.
                # Nothing here was independently verified by boardd.
                "verified": False,
                "verified_label": "Boardd has not verified this.",
            }
        )

    latest_by_lane: dict[str, dict[str, Any]] = {}
    for ev in events:  # digest is newest-first
        if ev["lane"] and ev["lane"] not in latest_by_lane:
            latest_by_lane[ev["lane"]] = ev

    lanes = []
    needs_you = []
    matched_reports: set[str] = set()
    for g in goals:
        report = _report_for_goal(g, joined_reports)
        ref = report.record.session_ref if report is not None else g.get("session_ref", "")
        movement = build_movement(g, tc)
        done_when = build_done_when(g, tc)
        scope = build_scope(g, tc)
        latest = latest_by_lane.get(ref)
        asks = [tc.get(a) for a in g.get("open_asks", [])]
        lane = {
            "session_ref": ref,
            "title": g.get("title", ref),
            "goal": tc.get(g.get("goal", "")),
            "intent": tc.get(g.get("intent", "")) if g.get("intent") else None,
            "movement": movement,
            "latest_result": latest,  # None means: no finished thing reported yet
            "done_when": done_when,
            "scope": scope,
            "open_asks": asks,
            "goal_version": g.get("goal_version"),
            "updated_ts": (
                (latest or {}).get("ts")
                or (report.view.observed_at if report is not None else None)
                or (goals_at.isoformat() if goals_at else None)
            ),
        }
        if report is not None:
            matched_reports.add(report.record.lane_id)
            lane["joined_session"] = _joined_payload(
                report.view,
                tc,
                owner_id=report.record.owner.owner_id,
                owner_role=report.record.owner.role,
            )
        lanes.append(lane)
        for ask in asks:
            needs_you.append(
                {
                    "lane_ref": ref,
                    "lane_title": lane["title"],
                    "question": ask,
                    "context": movement["sentence"],
                }
            )

    for report in joined_reports:
        if report.record.lane_id in matched_reports:
            continue
        view = report.view
        ref = report.record.session_ref
        fallback_goal: dict[str, Any] = {
            "session_ref": ref,
            "status": "working" if view.lifecycle == "active" else "idle",
            "now": view.current_work or "",
            "done_when": view.done_when or "",
            "open_asks": [],
        }
        latest = latest_by_lane.get(ref)
        lane = {
            "session_ref": ref,
            "title": ref,
            "goal": tc.get(view.goal or ""),
            "intent": None,
            "movement": build_movement(fallback_goal, tc),
            "latest_result": latest,
            "done_when": build_done_when(fallback_goal, tc),
            "scope": {"narrowed": False, "dropped": []},
            "open_asks": [],
            "goal_version": report.record.goal_version,
            "updated_ts": (latest or {}).get("ts") or view.observed_at,
            "joined_session": _joined_payload(
                view,
                tc,
                owner_id=report.record.owner.owner_id,
                owner_role=report.record.owner.role,
            ),
        }
        lanes.append(lane)

    counts: dict[str, int] = {}
    for lane in lanes:
        counts[lane["movement"]["status"]] = counts.get(lane["movement"]["status"], 0) + 1

    goals_age = (now - goals_at).total_seconds() if goals_at else None
    data_stale = goals_age is not None and goals_age > STALE_AFTER_SECONDS

    return {
        "schema": "boardd.state.v1",
        "generated_at": now.isoformat(),
        "source": {
            "state_dir": str(state_dir),
            "goals_schema": goals_doc.get("schema"),
            "goals_updated_at": goals_at.isoformat() if goals_at else None,
            "sweep_at": sweep_at.isoformat() if sweep_at else None,
            "goals_age_seconds": goals_age,
            "data_stale": data_stale,
            "note": goals_doc.get("note"),
            "joined_lane_count": len(joined_reports),
            "errors": raw["errors"],
        },
        "summary": {
            "lane_count": len(lanes),
            "counts": counts,
            "sentence": summary_sentence(counts, len(lanes)),
            "needs_you_count": len(needs_you),
        },
        "needs_you": needs_you,
        "lanes": lanes,
        "events": events,
    }


def categorize_event(ev: dict[str, Any], lane_status: str | None) -> dict[str, Any]:
    """A plain-words tag for the history view.

    If the digest ever carries an explicit `kind`, that wins. Otherwise this
    is boardd's reading of the summary text plus the lane's current status,
    and the UI legend says so.
    """
    if ev.get("kind"):
        return {"tone": "hold", "label": str(ev["kind"]), "derived": False}
    text = (ev.get("text") or "").lower()
    if text.startswith(("no change", "no activity")):
        return {"tone": "hold", "label": "No change", "derived": True}
    if "blocked" in text or lane_status == "blocked":
        return {"tone": "bad", "label": "Blocked", "derived": True}
    if lane_status == "held" or "holds for" in text:
        return {"tone": "hold", "label": "Holding", "derived": True}
    if "reports" in text or lane_status == "turn-finished-unverified":
        return {"tone": "warn", "label": "Reported done", "derived": True}
    return {"tone": "ok", "label": "Progress", "derived": True}


def summary_sentence(counts: dict[str, int], total: int) -> str:
    if total == 0:
        return "No lanes are enrolled."
    parts = []
    for status, phrase in (
        ("working", "moving"),
        ("held", "held on purpose"),
        ("blocked", "blocked"),
        ("idle", "quiet"),
        ("turn-finished-unverified", "awaiting a completion audit"),
        ("done-pending-verification", "done pending proof"),
    ):
        n = counts.get(status, 0)
        if n:
            parts.append(f"{_num(n)} {phrase}")
    body = ", ".join(parts) if parts else "state unknown"
    return f"{_num(total).capitalize()} lanes: {body}."

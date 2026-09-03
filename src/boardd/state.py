"""State loading and view building.

boardd is a pure reader. This module reads goals.json (chitra.goals.v3,
loaded through chitra's own GoalRecord validator so a v3 record is never
hand-parsed twice) and sweep-digest.json (still boardd's own since chitra
has no loader for it), never writes either, and builds the single view dict
served by /api/state and pushed over SSE.

Honesty rules enforced here, not in the template:
- Agent-reported results always carry verified=False and the UI mark
  "Boardd has not verified this." There is no code path that sets
  verified=True without an evidence record.
- A done-when condition renders as machine-tracked ONLY if the goal carries
  a matching, passing chitra.completion_gate.CompletionEvidence record for
  it. A lane with no enrolled_done_when_items falls back to its plain-text
  done_when clauses, all honestly unbound.
- Stale data states its age; ages are computed from file timestamps, never
  invented.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chitra.goals import GoalsSchemaNewerError, load_goals_document, session_name

from .config import DIGEST_FILE, GOALS_FILE, STALE_AFTER_SECONDS
from .translate import TranslationCache

SCHEMA = "boardd.state.v2"

# ---------------------------------------------------------------- loading


def load_state_files(state_dir: Path) -> dict[str, Any]:
    """Read the two state files. Missing or bad files are reported, not hidden."""
    errors: list[str] = []
    out: dict[str, Any] = {"goals": None, "digest": None, "errors": errors}

    goals_path = state_dir / GOALS_FILE
    try:
        raw_doc: Any = json.loads(goals_path.read_text())
    except FileNotFoundError:
        errors.append(f"{GOALS_FILE} not found in {state_dir}")
        raw_doc = None
    except (json.JSONDecodeError, OSError) as e:
        errors.append(f"{GOALS_FILE} unreadable: {e}")
        raw_doc = None
    if raw_doc is not None:
        try:
            records, schema = load_goals_document(state_dir, allow_newer=True)
        except (ValueError, GoalsSchemaNewerError) as e:
            errors.append(f"{GOALS_FILE} unreadable: {e}")
        else:
            out["goals"] = {
                "schema": schema,
                "updated_at": raw_doc.get("updated_at", "") if isinstance(raw_doc, dict) else "",
                "note": raw_doc.get("note") if isinstance(raw_doc, dict) else None,
                "goals": [record.to_dict() for record in records],
            }

    digest_path = state_dir / DIGEST_FILE
    try:
        out["digest"] = json.loads(digest_path.read_text())
    except FileNotFoundError:
        errors.append(f"{DIGEST_FILE} not found in {state_dir}")
    except (json.JSONDecodeError, OSError) as e:
        errors.append(f"{DIGEST_FILE} unreadable: {e}")
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


# ------------------------------------------------------------ done-when


def split_conditions(done_when: str) -> list[str]:
    return [c.strip() for c in (done_when or "").split(";") if c.strip()]


def _structured_proof(item: dict[str, Any], proofs: list[dict[str, Any]]) -> dict[str, str]:
    """One enrolled_done_when_item's proof state, from completion_proofs.

    Mirrors the exact-match chain chitra.completion_gate.completion_receipt_issues
    enforces before a lane may close: the item id, then its named receipt,
    then its validator, then a passing result. boardd only reads this chain;
    it never runs a validator or grants a passing result itself.
    """
    item_proofs = [p for p in proofs if p.get("done_when_item_id") == item.get("id")]
    named = [p for p in item_proofs if p.get("receipt_name") == item.get("required_receipt")]
    validated = [p for p in named if p.get("validator") == item.get("validator")]
    passing = [p for p in validated if p.get("validator_result") == "pass"]
    validator = item.get("validator", "an automatic check")
    if passing:
        return {
            "state": "verified",
            "label": f"Proven by {validator}, receipt {item.get('required_receipt')}.",
        }
    if validated:
        return {
            "state": "pending",
            "label": f"Checked by {validator}; no passing result yet.",
        }
    return {
        "state": "unbound",
        "label": "Not checked automatically — no evidence source is linked. Needs review to call done.",
    }


def build_done_when(goal: dict[str, Any], tc: TranslationCache) -> dict[str, Any]:
    """Build the condition list with per-condition proof labels.

    A lane enrolled under chitra.goals.v3 with structured done items
    (goal["enrolled_done_when_items"]) is checked against its
    completion_proofs. A lane without structured items (legacy, or not yet
    enrolled) falls back to its plain-text done_when clauses, which have no
    machine binding and render honestly as unbound.
    """
    conditions: list[dict[str, Any]] = []
    proven = 0
    tracked = 0
    items = goal.get("enrolled_done_when_items") or []
    proofs = goal.get("completion_proofs") or []
    if items:
        for item in items:
            proof = _structured_proof(item, proofs)
            if proof["state"] == "verified":
                proven += 1
                tracked += 1
            elif proof["state"] == "pending":
                tracked += 1
            conditions.append({**tc.get(item.get("text", "")), "proof": proof})
    else:
        for text in split_conditions(goal.get("done_when", "")):
            proof = {
                "state": "unbound",
                "label": "Not checked automatically — no evidence source is linked. Needs review to call done.",
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
    "completion-disputed": ("bad", "Completion disputed"),
    "done-pending-verification": ("ok", "Done, pending proof"),
    "done-pending-close": ("ok", "Done, pending close"),
}

# The four statuses that always belong in the needs-feedback review queue,
# whether or not the lane also carries an open ask.
REVIEW_STATUSES = {"completion-disputed", "done-pending-verification", "turn-finished-unverified", "blocked"}


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
    elif status == "completion-disputed":
        sentence = "The agent claims this is done. The registered validator did not confirm a passing receipt."
    elif status == "done-pending-verification":
        sentence = "The work is reported done; the proof window is still running."
    elif status == "done-pending-close":
        sentence = "The work is proven done and is waiting on your close."
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

    goals_at = _parse_ts(goals_doc.get("updated_at"))
    sweep_at = _parse_ts(digest_doc.get("sweep_at"))

    status_by_lane = {g.get("session_ref"): g.get("status") for g in goals_doc.get("goals", [])}
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
    for g in goals_doc.get("goals", []):
        ref = g.get("session_ref", "")
        lane_id = g.get("lane_id") or session_name(ref)
        movement = build_movement(g, tc)
        done_when = build_done_when(g, tc)
        scope = build_scope(g, tc)
        latest = latest_by_lane.get(ref)
        asks = [tc.get(a) for a in g.get("open_asks", [])]
        status = g.get("status", "unknown")
        needs_review = status in REVIEW_STATUSES or bool(asks)
        lane = {
            "session_ref": ref,
            "lane_id": lane_id,
            "title": session_name(ref) if ref else lane_id,
            "goal": tc.get(g.get("goal", "")),
            "intent": tc.get(g.get("intent", "")) if g.get("intent") else None,
            "movement": movement,
            "latest_result": latest,  # None means: no finished thing reported yet
            "done_when": done_when,
            "scope": scope,
            "open_asks": asks,
            "goal_version": g.get("goal_version"),
            "needs_review": needs_review,
            "updated_ts": (latest or {}).get("ts") or g.get("updated_at") or (goals_at.isoformat() if goals_at else None),
        }
        lanes.append(lane)
        if not needs_review:
            continue
        since = g.get("updated_at") or lane["updated_ts"]
        if asks:
            for ask in asks:
                needs_you.append(
                    {
                        "lane_ref": ref,
                        "lane_id": lane_id,
                        "lane_title": lane["title"],
                        "goal": lane["goal"],
                        "question": ask,
                        "context": movement["sentence"],
                        "since": since,
                    }
                )
        else:
            # A status-triggered review item carries no literal ask text;
            # boardd's own plain-words reason stands in for one, marked as
            # already-translated so it never shows the "not yet translated"
            # mark that belongs to raw session lines.
            reason = f"Status is {STATUS_PILLS.get(status, ('', status))[1].lower()} — needs review."
            needs_you.append(
                {
                    "lane_ref": ref,
                    "lane_id": lane_id,
                    "lane_title": lane["title"],
                    "goal": lane["goal"],
                    "question": {"text": reason, "raw": reason, "translated": True},
                    "context": movement["sentence"],
                    "since": since,
                }
            )
    # Oldest ask first. "since" is the record's own last-write time — v3
    # carries no per-ask timestamp, so this is the closest honest proxy for
    # how long a lane has been waiting on the operator.
    needs_you.sort(key=lambda item: item["since"] or "")

    counts: dict[str, int] = {}
    for lane in lanes:
        counts[lane["movement"]["status"]] = counts.get(lane["movement"]["status"], 0) + 1

    goals_age = (now - goals_at).total_seconds() if goals_at else None
    data_stale = goals_age is not None and goals_age > STALE_AFTER_SECONDS

    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "source": {
            "state_dir": str(state_dir),
            "goals_schema": goals_doc.get("schema"),
            "goals_updated_at": goals_at.isoformat() if goals_at else None,
            "sweep_at": sweep_at.isoformat() if sweep_at else None,
            "goals_age_seconds": goals_age,
            "data_stale": data_stale,
            "note": goals_doc.get("note"),
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
        ("completion-disputed", "disputed"),
        ("done-pending-verification", "done pending proof"),
        ("done-pending-close", "done pending close"),
    ):
        n = counts.get(status, 0)
        if n:
            parts.append(f"{_num(n)} {phrase}")
    body = ", ".join(parts) if parts else "state unknown"
    return f"{_num(total).capitalize()} lanes: {body}."

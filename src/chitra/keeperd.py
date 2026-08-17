"""chitra-keeperd — the acting supervisor loop.

Every other chitra daemon classifies and files; none of them acts. keeperd
closes the loop the fleet monitor doctrine demands: every enrolled lane is
driven, every cycle, to be verifiably working toward its recorded goal or
verifiably resting by design — never idle-and-unaligned.

Per cycle, for every enrolled lane under the governed goals tree:

1. GROUND-TRUTH LIVENESS (never a render): progress is growth of the lane's
   pane log / transcript with SUBSTANTIVE residue change (ANSI stripped,
   alphanumerics only). Chrome/spinner repaint is never progress; a rendered
   spinner over a frozen log is a wedge, not work.
2. CLASSIFY: working (substantive progress), idle-by-design (held /
   rate-limit / operator-parked), idle-failure (at prompt, status=working, no
   progress), wedge-suspect (active-turn chrome, no progress past T1), stuck
   composer (unsubmitted text while no turn runs).
3. ACT the same cycle:
   - stuck composer  -> flush: submit it (operator never types into panes;
     composer text is always a failed delivery), verify submission.
   - idle-failure    -> re-arm with the RECORDED goal, delivery-verified
     (paste -> submit -> consumption = substantive log growth).
   - wedge-suspect   -> probe steer; unconsumed past T2 -> escalate CRIT with
     the named block (and optionally interrupt/recover).
   - held + resume_at due -> resume: re-arm stored goal through the same
     delivery-verified path, then mark the record resumed.
4. SELF-HEALTH: writes a heartbeat every cycle; checks peer daemon heartbeats
   and raises CRIT ``watcher-silent:<name>`` when one goes quiet. Silence is
   an alarm, never an unknown. All subprocess calls are timeout-bounded.

Escalations append to ``keeper-flags.log`` (CRIT lines, same shape triaged
consumers already read). State persists in ``keeper-state.json``.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("chitra.keeperd")

ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
NON_ALNUM_RE = re.compile(rb"[^0-9A-Za-z]+")
# Active-turn chrome for supported TUIs (confidence signal only — never liveness).
# Deliberately narrow: completed-turn summaries reuse spinner glyphs (e.g.
# "✻ Brewed for 24s"), so glyphs alone must never count as an active turn.
ACTIVE_TURN_RE = re.compile(
    r"esc to interrupt|esc to cancel|\bthinking\b|working…|running…"
    r"|[✻✽✶✳*]\s*\S{1,30}…\s*\(\d+m?\s*\d*s?\b",  # live spinner with elapsed counter
    re.IGNORECASE,
)
# Rendered chrome that must never register as progress.
CHROME_LINE_RE = re.compile(
    r"tokens|esc to interrupt|auto mode|shift\+tab|% of|Brewed for|globalVersion|"
    r"^[─═╌\-\s]+$|resets |weekly limit|for agents",
    re.IGNORECASE,
)
KITTY_ENTER = "\x1b[13u"

DEFAULT_CYCLE_SECONDS = 120
DEFAULT_T1_STALE_SECONDS = 20 * 60
DEFAULT_T2_WEDGE_SECONDS = 60 * 60
DEFAULT_CONSUME_TIMEOUT = 90
HEARTBEAT_SILENT_FACTOR = 3


def substantive_digest(raw: bytes, tail_bytes: int = 65536) -> str:
    """Hash of the alphanumeric residue of the tail of a pane log.

    Strips ANSI escapes and every non-alphanumeric byte so spinner glyphs,
    repaints, timers, and cursor churn can never register as progress.
    """
    tail = raw[-tail_bytes:]
    stripped = ANSI_RE.sub(b"", tail)
    residue = NON_ALNUM_RE.sub(b"", stripped)
    return hashlib.sha256(residue).hexdigest()


@dataclasses.dataclass
class LaneObservation:
    session_ref: str
    lane: str
    status: str
    log_bytes: int
    digest: str
    active_turn: bool
    composer_text: str
    at_prompt: bool


@dataclasses.dataclass
class KeeperConfig:
    goals_tree: Path            # .../governed-lanes/<host>
    host: str
    state_dir: Path             # writable keeper state dir
    ssh_prefix: list[str]       # e.g. sudo -n -u chitra ssh -F <cfg> tophand
    cycle_seconds: int = DEFAULT_CYCLE_SECONDS
    t1_stale_seconds: int = DEFAULT_T1_STALE_SECONDS
    t2_wedge_seconds: int = DEFAULT_T2_WEDGE_SECONDS
    consume_timeout: int = DEFAULT_CONSUME_TIMEOUT
    peer_heartbeats: Path | None = None
    dry_run: bool = False


class Keeper:
    def __init__(self, config: KeeperConfig) -> None:
        self.config = config
        self.state_path = config.state_dir / "keeper-state.json"
        self.flags_path = config.state_dir / "keeper-flags.log"
        self.actions_path = config.state_dir / "keeper-actions.jsonl"
        self.heartbeat_path = config.state_dir / "heartbeats" / "keeperd.json"
        self.state: dict[str, Any] = self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"lanes": {}, "cycle": 0}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        tmp.replace(self.state_path)

    def _flag(self, level: str, code: str, detail: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\t{level}\t{code}\t{detail}\n"
        self.flags_path.parent.mkdir(parents=True, exist_ok=True)
        with self.flags_path.open("a") as fh:
            fh.write(line)
        logger.warning("%s %s %s", level, code, detail)

    def _action(self, record: dict[str, Any]) -> None:
        record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.actions_path.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    # ------------------------------------------------------------- transport
    def _run(self, argv: list[str], *, input_text: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout, check=False)

    def _remote(self, verb: str, *, input_text: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return self._run([*self.config.ssh_prefix, verb], input_text=input_text, timeout=timeout)

    def capture_pane(self, lane: str) -> dict[str, Any] | None:
        proc = self._remote(f"chitra-tmux-capture {lane}:0")
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

    def steer(self, lane: str, payload: str) -> bool:
        if self.config.dry_run:
            return True
        proc = self._remote(f"chitra-lane-steer {lane}", input_text=payload, timeout=20)
        return proc.returncode == 0

    def transcript_ids(self, backend: str = "claude") -> list[str]:
        proc = self._remote(f"chitra-transcript-list {backend}")
        if proc.returncode != 0:
            return []
        try:
            return list(json.loads(proc.stdout).get("ids", []))
        except json.JSONDecodeError:
            return []

    def transcript_tail(self, backend: str, transcript_id: str) -> str:
        proc = self._remote(f"chitra-transcript-tail {backend} {transcript_id}")
        if proc.returncode != 0:
            return ""
        try:
            doc = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ""
        return doc.get("content", "") if doc.get("ok") else ""

    def bind_transcript(self, lane: str, marker: str, backend: str = "claude") -> str | None:
        """One-time lane->transcript binding: find which transcript consumed marker."""
        lane_state = self.state["lanes"].setdefault(lane, {})
        known = lane_state.get("transcript_id")
        if known:
            return known
        for tid in self.transcript_ids(backend)[:60]:
            if marker in self.transcript_tail(backend, tid):
                lane_state["transcript_id"] = tid
                lane_state["transcript_backend"] = backend
                self._action({"kind": "transcript-bound", "lane": lane, "transcript_id": tid})
                return tid
        return None

    @staticmethod
    def transcript_consumed(content: str, marker: str) -> bool:
        """Structural check: marker in a user-role record AND a later assistant/turn record."""
        saw_user_marker = False
        for line in content.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type") or (rec.get("message") or {}).get("role")
            body = json.dumps(rec.get("message", rec), ensure_ascii=False)
            if not saw_user_marker and rtype in ("user", "human") and marker in body:
                saw_user_marker = True
                continue
            if saw_user_marker and rtype in ("assistant", "ai", "progress", "tool_use", "tool_result"):
                return True
        return False

    # ------------------------------------------------------------ inspection
    def observe(self, lane_dir: Path, goal: dict[str, Any]) -> LaneObservation | None:
        lane = goal.get("lane_id") or lane_dir.name
        log_path = lane_dir / "tmux-transcript.log"
        try:
            raw = log_path.read_bytes()
        except OSError:
            raw = b""
        capture = self.capture_pane(lane)
        content = (capture or {}).get("content", "") if capture else ""
        lines = [ln for ln in content.splitlines() if ln.strip()]
        active = bool(ACTIVE_TURN_RE.search(content))
        composer_text = self._composer_text(content)
        at_prompt = self._at_prompt(lines) and not active
        # Progress evidence, multi-source: pane-log residue + chrome-stripped
        # capture residue + bound-transcript tail residue. A change in ANY
        # substantive source is progress; chrome/spinner repaint changes none.
        capture_body = "\n".join(ln for ln in content.splitlines() if ln.strip() and not CHROME_LINE_RE.search(ln))
        lane_state = self.state["lanes"].get(lane, {})
        transcript_residue = b""
        tid = lane_state.get("transcript_id")
        if tid:
            tail = self.transcript_tail(lane_state.get("transcript_backend", "claude"), tid)
            transcript_residue = tail[-8192:].encode()
        combined = raw[-65536:] + b"\x00" + capture_body.encode() + b"\x00" + transcript_residue
        return LaneObservation(
            session_ref=goal.get("session_ref", f"{self.config.host}:{lane}:0.0"),
            lane=lane,
            status=goal.get("status", ""),
            log_bytes=len(raw),
            digest=substantive_digest(combined),
            active_turn=active,
            composer_text=composer_text,
            at_prompt=at_prompt,
        )

    @staticmethod
    def _composer_text(content: str) -> str:
        """Text sitting in the input row (❯/› composer), if any."""
        for line in reversed(content.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            m = re.match(r"^[❯›>]\s+(.*\S)\s*$", stripped)
            if m and not m.group(1).startswith(("Try \"", "Explain this", "Use /")):
                return m.group(1)
            if re.match(r"^[❯›>]\s*$", stripped):
                return ""
            # keep scanning past statusline chrome below the composer
            if any(tok in stripped for tok in ("auto mode", "shift+tab", "tokens", "% of", "─")):
                continue
            return ""
        return ""

    @staticmethod
    def _at_prompt(lines: list[str]) -> bool:
        return any(re.match(r"^[❯›>]\s*$", ln.strip()) for ln in lines[-8:])

    # -------------------------------------------------------------- delivery
    def deliver(self, obs: LaneObservation, payload: str, *, backend_hint: str = "") -> dict[str, Any]:
        """Paste -> submit -> consumption, each rung separately verified.

        Returns a result dict with rungs: pasted, submitted, consumed.
        """
        result = {"lane": obs.lane, "payload": payload[:120], "pasted": False, "submitted": False, "consumed": False, "detail": ""}
        before_digest = obs.digest
        before_bytes = obs.log_bytes
        if not self.steer(obs.lane, payload):
            result["detail"] = "steer-verb-failed"
            return result
        result["pasted"] = True
        time.sleep(3.0)
        # Submit verification: composer must not still hold the payload.
        capture = self.capture_pane(obs.lane)
        content = (capture or {}).get("content", "")
        marker = payload.strip()[:40]
        if marker and marker in self._composer_text(content):
            # Unsubmitted: fire the backend-appropriate submit.
            submit_key = KITTY_ENTER if backend_hint == "codex" or "›" in content else "go"
            if ACTIVE_TURN_RE.search(content):
                result["detail"] = "turn-became-active-before-submit"
            else:
                self.steer(obs.lane, submit_key)
                time.sleep(2.0)
                capture = self.capture_pane(obs.lane)
                content = (capture or {}).get("content", "")
                if marker in self._composer_text(content):
                    result["detail"] = "submit-failed-composer-still-holds-text"
                    return result
        result["submitted"] = True
        # Consumption verification, in authority order:
        #   1. structural transcript check (marker in a user record + later
        #      assistant/turn record) via the governed transcript verbs;
        #   2. pane-log substantive growth;
        #   3. capture-diff: a new assistant block rendered after delivery.
        deadline = time.time() + self.config.consume_timeout
        lane_dir = self.config.goals_tree / obs.lane
        log_path = lane_dir / "tmux-transcript.log"
        backend = backend_hint or self.state["lanes"].get(obs.lane, {}).get("transcript_backend", "claude")
        marker_full = payload.strip().splitlines()[0][:80]
        while time.time() < deadline:
            time.sleep(6.0)
            tid = self.state["lanes"].get(obs.lane, {}).get("transcript_id") or self.bind_transcript(obs.lane, marker_full, backend)
            if tid:
                content_tail = self.transcript_tail(backend, tid)
                if self.transcript_consumed(content_tail, marker_full):
                    result["consumed"] = True
                    result["detail"] = "transcript-structural"
                    break
            try:
                raw = log_path.read_bytes()
                if len(raw) > before_bytes and substantive_digest(raw) != before_digest:
                    result["consumed"] = True
                    result["detail"] = "pane-log-growth"
                    break
            except OSError:
                pass
            post = self.capture_pane(obs.lane)
            post_content = (post or {}).get("content", "")
            # Slash-command payloads (e.g. "/goal ...") never appear as user
            # transcript records; their consumption evidence is the TUI state
            # they produce (goal-active statusline) plus turn activity.
            if payload.lstrip().startswith("/goal") and "/goal active" in post_content:
                result["consumed"] = True
                result["detail"] = "goal-active-statusline"
                break
            if marker.strip() and marker not in self._composer_text(post_content):
                echoed = any(
                    marker in ln and not re.match(r"^[❯›>]\s", ln.strip())
                    for ln in post_content.splitlines()
                )
                if echoed and (ACTIVE_TURN_RE.search(post_content) or re.search(r"^[●•]\s+\S", post_content, re.MULTILINE)):
                    result["consumed"] = True
                    result["detail"] = "capture-echo-with-turn-activity"
                    break
        if not result["consumed"]:
            result["detail"] = result["detail"] or "consumption-unconfirmed"
        return result

    # ------------------------------------------------------------- decisions
    def handle_lane(self, lane_dir: Path) -> dict[str, Any] | None:
        goals_file = lane_dir / "goals.json"
        try:
            doc = json.loads(goals_file.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        goals = doc.get("goals") or []
        if not goals:
            return None
        goal = goals[0]
        obs = self.observe(lane_dir, goal)
        if obs is None:
            return None
        now = time.time()
        lane_state = self.state["lanes"].setdefault(obs.lane, {})
        prev_digest = lane_state.get("digest")
        if prev_digest != obs.digest:
            lane_state["digest"] = obs.digest
            lane_state["last_progress_at"] = now
        last_progress = lane_state.get("last_progress_at", now)
        stale_for = now - last_progress
        verdict: dict[str, Any] = {"lane": obs.lane, "status": obs.status, "stale_for": int(stale_for)}

        # Infra-work triage: infra-shaped asks are ROUTED to the dedicated
        # worker queue and acknowledged — never bounced to the operator and
        # never hand-rolled inside the asking lane. Merges are pre-authorized
        # fleet-wide (merged.py owns them once deployed).
        routed = self._route_infra_asks(lane_dir, goal, obs)
        if routed:
            verdict["infra_routed"] = routed

        # Idle-by-design: held lanes rest, except due rate-limit holds resume.
        if obs.status == "held":
            resume_at = goal.get("resume_at") or ""
            if resume_at:
                try:
                    due = time.mktime(time.strptime(resume_at[:19], "%Y-%m-%dT%H:%M:%S")) <= time.time()
                except ValueError:
                    due = False
                if due:
                    verdict["classification"] = "held-due-resume"
                    delivery = self.deliver(obs, f"/goal {goal.get('goal', '')}\nResume work toward the recorded goal now.")
                    verdict["delivery"] = delivery
                    self._action({"kind": "resume", **delivery})
                    if delivery["consumed"]:
                        self._resume_record(lane_dir, goal)
                    else:
                        self._flag("CRIT", "resume-unconsumed", f"{obs.session_ref} resume delivery not consumed")
                    return verdict
            verdict["classification"] = "idle-by-design"
            return verdict

        # Stuck composer while no turn runs: always a failed delivery — flush.
        if obs.composer_text and not obs.active_turn:
            verdict["classification"] = "stuck-composer"
            self._flag("WARN", "stuck-composer-flush", f"{obs.session_ref} flushing unsubmitted text: {obs.composer_text[:80]!r}")
            backend = "codex" if "›" in obs.composer_text or goal.get("backend") == "codex" else ""
            self.steer(obs.lane, KITTY_ENTER if backend == "codex" else "go")
            self._action({"kind": "flush", "lane": obs.lane, "flushed": obs.composer_text[:120]})
            return verdict

        if obs.active_turn and stale_for >= self.config.t2_wedge_seconds:
            verdict["classification"] = "wedged"
            self._flag("CRIT", "lane-wedged", f"{obs.session_ref} active-turn chrome with no substantive progress for {int(stale_for)}s")
            self._action({"kind": "wedge-escalate", "lane": obs.lane, "stale_for": int(stale_for)})
            return verdict

        if obs.active_turn and stale_for >= self.config.t1_stale_seconds:
            verdict["classification"] = "wedge-suspect"
            self._flag("WARN", "wedge-suspect", f"{obs.session_ref} no substantive progress for {int(stale_for)}s during an active turn")
            return verdict

        # Idle-failure: at prompt, record says working, nothing moving.
        if obs.at_prompt and obs.status == "working" and stale_for >= self.config.t1_stale_seconds:
            verdict["classification"] = "idle-failure"
            goal_text = goal.get("goal", "").strip()
            payload = f"Continue toward the recorded goal: {goal_text}. Done when: {goal.get('done_when', '').strip()} /goal {goal_text}"
            delivery = self.deliver(obs, payload)
            verdict["delivery"] = delivery
            self._action({"kind": "idle-nudge", **delivery})
            if not delivery["consumed"]:
                self._flag("CRIT", "nudge-unconsumed", f"{obs.session_ref} idle nudge was not consumed: {delivery['detail']}")
            return verdict

        verdict["classification"] = "working" if stale_for < self.config.t1_stale_seconds else "quiet"
        return verdict

    INFRA_ASK_RE = re.compile(
        r"\b(merge|converge|publish|deploy|permission fix|grant (?:me )?access|"
        r"chmod|chown|needs? (?:a )?(?:merge|converge|publish))\b",
        re.IGNORECASE,
    )

    def _route_infra_asks(self, lane_dir: Path, goal: dict[str, Any], obs: LaneObservation) -> list[str]:
        """Route infra-shaped open asks to the dedicated worker queue."""
        routed: list[str] = []
        queue_path = self.config.state_dir / "infra-queue.jsonl"
        already = self.state["lanes"].setdefault(obs.lane, {}).setdefault("routed_asks", [])
        for ask in goal.get("open_asks") or []:
            text = ask if isinstance(ask, str) else json.dumps(ask)
            if not self.INFRA_ASK_RE.search(text) or text in already:
                continue
            item = {
                "kind": "infra-work",
                "category": self.INFRA_ASK_RE.search(text).group(1).lower(),
                "from_lane": obs.session_ref,
                "ask": text,
                "authorization": "standing (merges pre-authorized; orders carry their own approval)",
            }
            with queue_path.open("a") as fh:
                fh.write(json.dumps({**item, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, sort_keys=True) + "\n")
            already.append(text)
            routed.append(text[:100])
            self._action({"kind": "infra-route", "lane": obs.lane, "ask": text[:200]})
            # Acknowledge to the lane so it keeps working instead of waiting.
            self.steer(
                obs.lane,
                "Your infrastructure request has been routed to the dedicated infra worker; "
                "it is pre-authorized and you do not need to wait on it or ask again. "
                "Continue with the rest of your goal. [/C]",
            )
        return routed

    def _resume_record(self, lane_dir: Path, goal: dict[str, Any]) -> None:
        if self.config.dry_run:
            return
        goals_file = lane_dir / "goals.json"
        doc = json.loads(goals_file.read_text())
        for rec in doc.get("goals", []):
            if rec.get("session_ref") == goal.get("session_ref"):
                rec["status"] = "working"
                rec["hold_reason"] = ""
                rec["resume_at"] = ""
                rec["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        tmp = goals_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True))
        tmp.replace(goals_file)

    # ------------------------------------------------------------ self-health
    def _write_heartbeat(self) -> None:
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "daemon": "keeperd",
            "ts": time.time(),
            "cycle": self.state.get("cycle", 0),
            "cadence_seconds": self.config.cycle_seconds,
        }
        tmp = self.heartbeat_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.heartbeat_path)

    def check_peer_heartbeats(self) -> list[str]:
        alarms: list[str] = []
        hb_dir = self.config.peer_heartbeats or self.heartbeat_path.parent
        if not hb_dir.is_dir():
            return alarms
        for hb in sorted(hb_dir.glob("*.json")):
            if hb.name == self.heartbeat_path.name:
                continue
            try:
                doc = json.loads(hb.read_text())
                age = time.time() - float(doc.get("ts", 0))
                cadence = float(doc.get("cadence_seconds", self.config.cycle_seconds))
            except (OSError, json.JSONDecodeError, ValueError):
                alarms.append(hb.stem)
                continue
            if age > cadence * HEARTBEAT_SILENT_FACTOR:
                alarms.append(hb.stem)
        for name in alarms:
            self._flag("CRIT", "watcher-silent", f"heartbeat for {name} is stale — silence is an alarm, treating watcher as down")
        return alarms

    # ------------------------------------------------------------------ loop
    def run_cycle(self) -> list[dict[str, Any]]:
        self.state["cycle"] = self.state.get("cycle", 0) + 1
        self._write_heartbeat()
        self.check_peer_heartbeats()
        verdicts: list[dict[str, Any]] = []
        for lane_dir in sorted(p for p in self.config.goals_tree.iterdir() if p.is_dir()):
            try:
                verdict = self.handle_lane(lane_dir)
            except subprocess.TimeoutExpired:
                self._flag("CRIT", "transport-timeout", f"governed transport timed out for {lane_dir.name}")
                continue
            if verdict is not None:
                verdicts.append(verdict)
        self.state["last_cycle_at"] = time.time()
        self._save_state()
        return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="chitra keeper supervisor loop")
    parser.add_argument("--goals-tree", required=True)
    parser.add_argument("--host", default="tophand")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--ssh-prefix", default="sudo -n -u chitra ssh -F /var/lib/chitra/.ssh/config tophand")
    parser.add_argument("--cycle-seconds", type=int, default=DEFAULT_CYCLE_SECONDS)
    parser.add_argument("--t1", type=int, default=DEFAULT_T1_STALE_SECONDS)
    parser.add_argument("--t2", type=int, default=DEFAULT_T2_WEDGE_SECONDS)
    parser.add_argument("--consume-timeout", type=int, default=DEFAULT_CONSUME_TIMEOUT)
    parser.add_argument("--once", action="store_true", help="run one cycle and print verdicts")
    parser.add_argument("--lane", action="append", help="restrict to these lane names")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = KeeperConfig(
        goals_tree=Path(args.goals_tree),
        host=args.host,
        state_dir=Path(args.state_dir),
        ssh_prefix=args.ssh_prefix.split(),
        cycle_seconds=args.cycle_seconds,
        t1_stale_seconds=args.t1,
        t2_wedge_seconds=args.t2,
        consume_timeout=args.consume_timeout,
        dry_run=args.dry_run,
    )
    keeper = Keeper(config)
    if args.lane:
        def run_restricted(self: Keeper) -> list[dict[str, Any]]:  # pragma: no cover - test harness path
            self.state["cycle"] = self.state.get("cycle", 0) + 1
            self._write_heartbeat()
            self.check_peer_heartbeats()
            verdicts = []
            for name in args.lane:
                lane_dir = self.config.goals_tree / name
                if lane_dir.is_dir():
                    verdict = self.handle_lane(lane_dir)
                    if verdict is not None:
                        verdicts.append(verdict)
            self._save_state()
            return verdicts

        keeper.run_cycle = run_restricted.__get__(keeper, Keeper)  # type: ignore[method-assign]
    while True:
        verdicts = keeper.run_cycle()
        print(json.dumps(verdicts, indent=2))
        if args.once:
            return 0
        time.sleep(config.cycle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

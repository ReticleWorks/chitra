"""monitord -- the single Chitra monitor entrypoint.

One daemon composes the observation plane that W2's architecture review
collapsed out of watchd, triaged, and sweepd:

1. **Journal** -- incrementally ingest each tracked lane's client transcript
   into its durable canonical journal.
2. **Detectors and ladder** -- run the deterministic failure-mode detectors
   over the observed events and feed every finding through the response
   ladder, which advances only on recurrence after proven consumption.
3. **Enrollment and receipts** -- read each lane's enrolled goal contract,
   execute registered completion validators for enrolled items, verify their
   receipts, and feed the result into false-completion detection when a final
   response makes a completion claim.
4. **Presence** -- publish one advisory presence record per pass so peers can
   see which instance is observing which lanes.

The daemon is deterministic and read-only toward tmux: it captures nothing
itself, dispatches no orders, and never restarts or steers a lane. Findings
become incident records and (in a later integration) dispatchd orders; this
module only observes, classifies, records, and publishes.

``watchd``, ``triaged``, and ``sweepd`` remain shipped for existing
declarations but are deprecated by this entrypoint; new deployments declare
one ``monitord`` process per instance instead of the three-daemon chain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from chitra.detect import (
    Finding,
    IncidentRecord,
    IncidentStore,
    LadderActionRecord,
    ResponseLadder,
    detect_document_dithering,
    detect_drift,
    detect_excessive_testing,
    detect_false_done,
    detect_stuck,
    detect_unnecessary_steps,
)
from chitra.dispatch import enqueue_dispatch_order
from chitra.goals import get_goal, list_goals
from chitra.journal import (
    CanonicalEvent,
    CanonicalType,
    JournalIngestor,
    NormalizationContext,
    ProgressClassification,
    classify_progress,
)
from chitra.journal.store import EventJournal
from chitra.orders import DispatchOrder
from chitra.presence import append_presence
from chitra.state_paths import state_dir as default_state_dir
from chitra.systemd_notify import notify_ready, notify_watchdog
from chitra.validation_receipts import record_enrolled_validator_runs

logger = structlog.get_logger(__name__)

DEFAULT_POLL_SECONDS = 60.0
PRESENCE_INSTANCE = "chitra-monitord"
MONITORD_SCHEMA = "chitra.monitord.pass.v1"

_DETECTOR_ORDER = ("stuck", "drift", "unnecessary_steps", "excessive_testing", "document_dithering")


@dataclass(frozen=True, slots=True)
class MonitordConfig:
    """All filesystem paths and timing required by one monitord pass."""

    state_dir: Path
    transcript_root: Path | None
    findings_path: Path
    poll_seconds: float
    shadow_mode: bool
    dispatch_queue_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class LanePassResult:
    """The compact result of one monitoring pass over one lane."""

    lane: str
    ingested_events: int
    findings_opened: int
    ladder_actions: tuple[str, ...]
    completion_disputed: bool
    validator_receipts_recorded: int


def resolve_config(
    *,
    state_dir: Path | None = None,
    transcript_root: Path | None = None,
    findings_path: Path | None = None,
    poll_seconds: float | None = None,
    shadow_mode: bool | None = None,
    dispatch_queue_dir: Path | None = None,
) -> MonitordConfig:
    """Resolve CLI arguments, then explicit environment overrides, then defaults."""
    resolved_state_dir = state_dir or default_state_dir()
    resolved_findings_path = findings_path or resolved_state_dir / "monitord-findings.jsonl"
    if poll_seconds is None:
        poll_seconds = DEFAULT_POLL_SECONDS
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be a positive number")
    if shadow_mode is None:
        # Shadow mode is the safe default: findings are recorded but never
        # leave the monitor's own state until an operator turns the mode off.
        # An operator opts a unit out explicitly with CHITRA_MONITORD_SHADOW_MODE=0
        # (the shipped unit example pins it on).
        shadow_mode = os.environ.get("CHITRA_MONITORD_SHADOW_MODE", "").strip() != "0"
    return MonitordConfig(
        state_dir=resolved_state_dir,
        transcript_root=transcript_root,
        findings_path=resolved_findings_path,
        poll_seconds=poll_seconds,
        shadow_mode=shadow_mode,
        dispatch_queue_dir=dispatch_queue_dir,
    )


def _lane_roots(state_dir: Path) -> list[Path]:
    """Return one journal path per journaled lane under the state root.

    ``EventJournal`` writes flat at ``journal/<lane>.jsonl``; discovery
    mirrors that layout and the caller recovers the lane from the stem.
    """
    journal_root = state_dir / "journal"
    if not journal_root.is_dir():
        return []
    return sorted(
        path
        for path in journal_root.glob("*.jsonl")
        if not path.name.endswith(".progress.jsonl")
    )


def ingest_lane_transcripts(
    config: MonitordConfig,
    lane: str,
    transcripts: tuple[tuple[Path, NormalizationContext], ...],
) -> tuple[CanonicalEvent, ...]:
    """Ingest every declared transcript for one lane into its journal."""
    observed: list[CanonicalEvent] = []
    for transcript_path, context in transcripts:
        with JournalIngestor(
            state_root=config.state_dir,
            transcript_path=transcript_path,
            context=context,
        ) as ingestor:
            observed.extend(ingestor.poll().observed)
    logger.info("monitord_ingested", lane=lane, events=len(observed))
    return tuple(observed)


def load_lane_events(config: MonitordConfig, lane: str) -> tuple[CanonicalEvent, ...]:
    """Load one lane's durable canonical journal."""
    return tuple(EventJournal(config.state_dir, lane).load())


def _final_response(events: tuple[CanonicalEvent, ...]) -> CanonicalEvent | None:
    for event in reversed(events):
        if event.normalized_type is CanonicalType.FINAL_RESPONSE:
            return event
    return None


def run_detectors(
    config: MonitordConfig,
    lane: str,
    goal: object,
    events: tuple[CanonicalEvent, ...],
    *,
    progress_rows: tuple[ProgressClassification, ...] | None = None,
) -> list[Finding]:
    """Run the deterministic detector set over one lane's journal."""
    scope_text = str(getattr(goal, "scope", "") or "")
    intent_text = str(getattr(goal, "intent", "") or "")
    goal_text = str(getattr(goal, "goal", "") or "")
    goal_is_document = "documentation" in f"{intent_text}\n{goal_text}".lower()
    if progress_rows is None:
        try:
            journal = EventJournal(config.state_dir, lane)
        except ValueError:
            # Direct detector callers may use an in-memory lane label that is
            # not a durable journal filename. They still get the same derived
            # classifications, but there is no safe path to persist them.
            journal = None
            progress_rows = ()
        else:
            progress_rows = tuple(journal.load_progress()) if journal is not None else ()
        goal_version = str(getattr(goal, "goal_version", 0) or 0)
        derived = tuple(
            classify_progress(event, goal_version=goal_version, related_events=events)
            for event in events
        )
        if derived and journal is not None:
            journal.append_progress(derived)
            known = {row.derivation_id for row in progress_rows}
            progress_rows = progress_rows + tuple(row for row in derived if row.derivation_id not in known)
        elif derived:
            progress_rows = derived
    findings: list[Finding] = []
    findings.extend(detect_stuck(events, progress_rows=progress_rows))
    findings.extend(detect_drift(events, scope_text=scope_text, declared_worktree=""))
    findings.extend(detect_unnecessary_steps(events))
    findings.extend(detect_excessive_testing(events))
    findings.extend(detect_document_dithering(events, goal_is_document=goal_is_document))
    return [finding for name in _DETECTOR_ORDER for finding in findings if finding.detector == name]


def evaluate_findings(
    config: MonitordConfig,
    lane: str,
    findings: list[Finding],
    *,
    order_marker: str = "[M] monitord",
    journal_events: tuple[CanonicalEvent, ...] = (),
    governed_restart: Callable[[Finding, IncidentRecord], bool] | None = None,
    relaunch: Callable[[Finding, IncidentRecord], bool] | None = None,
) -> list[str]:
    """Log every finding's ladder action and execute only outside shadow mode.

    The nudge uses the existing dispatch queue contract. Restart and relaunch
    callbacks are explicit seams for the governed rescue and checkpoint
    owner; without them, monitord records the required action but fails closed.
    """
    store = IncidentStore(config.state_dir, lane)
    ladder = ResponseLadder(store, journal_events=journal_events)
    actions: list[str] = []
    for finding in findings:
        decision = ladder.evaluate(lane=lane, finding=finding, order_marker=order_marker)
        action = ladder.action_for(decision)
        acted = False
        action_reason = "shadow mode records the action without acting"
        if not config.shadow_mode:
            if action == "nudge":
                if config.dispatch_queue_dir is None:
                    action_reason = "dispatch queue is not configured"
                else:
                    order_id = f"monitord-{hashlib.sha256(f'{lane}:{finding.fingerprint}'.encode()).hexdigest()[:24]}"
                    enqueue_dispatch_order(
                        config.dispatch_queue_dir,
                        DispatchOrder(
                            order_id=order_id,
                            session_ref=lane,
                            nudge=finding.expected_next_progress,
                            task_type="stuck-nudge",
                        ),
                    )
                    acted = True
                    action_reason = "nudge enqueued through the dispatch contract"
            elif action == "governed_restart":
                if governed_restart is not None:
                    acted = governed_restart(finding, decision.record)
                    action_reason = "governed restart callback completed" if acted else "governed restart callback declined"
                else:
                    action_reason = "governed restart requires the rescue and checkpoint owner"
            elif action == "relaunch":
                if relaunch is not None:
                    acted = relaunch(finding, decision.record)
                    action_reason = "relaunch callback completed" if acted else "relaunch callback declined"
                else:
                    action_reason = "relaunch requires a governed rescue bundle and checkpoint receipt"
            elif action == "mark_and_surface":
                acted = True
                action_reason = "relaunch-stage recurrence was durably marked and surfaced"
        store.record_action(
            LadderActionRecord(
                lane=lane,
                fingerprint=finding.fingerprint,
                detector=finding.detector,
                stage=decision.stage,
                action=action,
                acted=acted,
                reason=action_reason,
                recorded_at=datetime.now(UTC).isoformat(),
            )
        )
        actions.append(action)
        logger.info(
            "monitord_ladder_decision",
            lane=lane,
            detector=finding.detector,
            action=decision.action,
            stage=decision.stage,
            reason=decision.reason,
            action_reason=action_reason,
            acted=acted,
            shadow_mode=config.shadow_mode,
        )
    return actions


def check_enrollment_and_receipts(
    config: MonitordConfig,
    session_ref: str,
) -> tuple[int, bool, list[Finding]]:
    """Execute validators for enrolled items and dispute unproven completion claims.

    Returns ``(receipts recorded, completion disputed, findings)``. A missing
    or held goal enrolls nothing and disputes nothing.
    """
    try:
        goal = get_goal(config.state_dir, session_ref)
    except Exception:
        return 0, False, []
    if goal is None or goal.status not in {"working", "turn-finished-unverified"}:
        return 0, False, []
    items = tuple(getattr(goal, "enrolled_done_when_items", ()) or ())
    if not items:
        return 0, False, []
    evidence = record_enrolled_validator_runs(config.state_dir, session_ref, items)
    passing_receipts = {
        item.required_receipt for item in items if any(
            proof.receipt_name == item.required_receipt and proof.validator_result == "pass"
            for proof in evidence
        )
    }
    disputed = any(item.required_receipt not in passing_receipts for item in items)
    findings: list[Finding] = []
    if disputed:
        findings.extend(
            detect_false_done(
                final_response=None,
                enrolled_items=items,
                receipt_names_by_item={},
                receipt_roots={session_ref: config.state_dir},
                session_ref=session_ref,
            )
        )
    return len(evidence), disputed, findings


def append_finding_records(config: MonitordConfig, lane: str, findings: list[Finding]) -> int:
    """Append one JSONL record per finding to the monitor findings log."""
    if not findings:
        return 0
    config.findings_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    with config.findings_path.open("a", encoding="utf-8") as handle:
        for finding in findings:
            record = {
                "schema": MONITORD_SCHEMA,
                "recorded_at": now,
                "lane": lane,
                "shadow_mode": config.shadow_mode,
                "detector": finding.detector,
                "fingerprint": finding.fingerprint,
                "event_refs": list(finding.event_refs),
                "unmet_item": finding.unmet_item,
                "expected_next_progress": finding.expected_next_progress,
                "detail": finding.detail,
            }
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return len(findings)


def run_once(config: MonitordConfig) -> dict[str, Any]:
    """Run one full observe-classify-record-publish pass and return its summary."""
    results: list[LanePassResult] = []
    for lane_path in _lane_roots(config.state_dir):
        lane = lane_path.stem
        events = load_lane_events(config, lane)
        if not events:
            continue
        session_ref = next((event.lane for event in events), lane)
        goals = {goal.session_ref: goal for goal in list_goals(config.state_dir)}
        goal = goals.get(session_ref) or next(iter(goals.values()), None)
        receipts_recorded, completion_disputed, enrollment_findings = check_enrollment_and_receipts(
            config, session_ref
        )
        detection_findings = run_detectors(config, lane, goal, events)
        findings = detection_findings + [
            finding for finding in enrollment_findings if finding.detector == "false_done"
        ]
        actions = evaluate_findings(config, lane, findings, journal_events=events)
        append_finding_records(config, lane, findings)
        results.append(
            LanePassResult(
                lane=lane,
                ingested_events=len(events),
                findings_opened=len(findings),
                ladder_actions=tuple(actions),
                completion_disputed=completion_disputed,
                validator_receipts_recorded=receipts_recorded,
            )
        )
        append_presence(
            PRESENCE_INSTANCE,
            f"chitra-journal:{lane}",
            session=session_ref,
            lanes=(lane,),
            mode="using",
            purpose="observe-only monitor pass",
            root=config.state_dir,
        )
    summary: dict[str, Any] = {
        "schema": MONITORD_SCHEMA,
        "lanes_observed": len(results),
        "findings_opened": sum(result.findings_opened for result in results),
        "completion_disputed": any(result.completion_disputed for result in results),
        "validator_receipts_recorded": sum(result.validator_receipts_recorded for result in results),
        "shadow_mode": config.shadow_mode,
        "results": [
            {
                "lane": result.lane,
                "ingested_events": result.ingested_events,
                "findings_opened": result.findings_opened,
                "ladder_actions": list(result.ladder_actions),
                "completion_disputed": result.completion_disputed,
                "validator_receipts_recorded": result.validator_receipts_recorded,
            }
            for result in results
        ],
    }
    logger.info("monitord_pass_complete", **summary)
    return summary


def run_forever(config: MonitordConfig, *, stop_event: threading.Event | None = None) -> None:
    """Run the composed monitor passes until a service signal stops the process."""
    active_stop_event = stop_event or threading.Event()
    logger.info("monitord_started", state_dir=str(config.state_dir), poll_seconds=config.poll_seconds)
    notify_ready()
    while not active_stop_event.is_set():
        run_once(config)
        notify_watchdog()
        active_stop_event.wait(config.poll_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the intentionally small daemon CLI."""
    parser = argparse.ArgumentParser(
        prog="chitra-monitord",
        description="Compose journal ingestion, detectors, ladder, enrollment receipts, and presence.",
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--transcript-root", type=Path, default=None)
    parser.add_argument("--findings-path", type=Path, default=None)
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--dispatch-queue-dir", type=Path, default=None)
    parser.add_argument("--no-shadow-mode", dest="shadow_mode", action="store_false", help="Record findings outside shadow mode.")
    parser.add_argument("--once", action="store_true", help="Run one pass and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the daemon; malformed persisted input deliberately terminates it."""
    args = build_arg_parser().parse_args(argv)
    config = resolve_config(
        state_dir=args.state_dir,
        transcript_root=args.transcript_root,
        findings_path=args.findings_path,
        poll_seconds=args.poll_seconds,
        shadow_mode=args.shadow_mode,
        dispatch_queue_dir=args.dispatch_queue_dir,
    )
    if args.once:
        print(json.dumps(run_once(config), indent=2, sort_keys=True))
        return 0

    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_forever(config, stop_event=stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""monitord -- the single Chitra monitor entrypoint.

One daemon composes the observation plane that W2's architecture review
collapsed out of watchd, triaged, and sweepd:

1. **Journal** -- incrementally ingest each tracked lane's client transcript
   into its durable canonical journal.
2. **Detectors and ladder** -- run the deterministic failure-mode detectors
   over the observed events and feed every finding through the response
   ladder, which advances only on recurrence after proven consumption.
3. **Persistent action** -- record corrective intent before publishing a
   goal-bound order, reconcile queue and signed delivery proof after a crash,
   and wait for a completed agent turn before judging recurrence.
4. **Enrollment and receipts** -- run registered validators only for an exact
   completion claim, isolate receipts by goal session, and close only after
   the stored evidence verifies independently.
5. **Presence** -- publish one advisory presence record per pass so peers can
   see which instance is observing which lanes.

The daemon never writes to tmux. It publishes durable orders to ``dispatchd``,
which remains the sole terminal writer. It also answers only questions that
the frozen goal settles exactly; protected or ambiguous questions hold the
goal and become explicit asks.

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

from chitra._fsio import locked_json_store, write_json_atomic
from chitra.canonical_choices import CanonicalChoicesPolicy, detect_canonical_choices
from chitra.completion_gate import CompletionEvidence, extract_completion_evidence, has_structured_completion_line, is_completion_claim
from chitra.detect import (
    Finding,
    IncidentStore,
    LadderDecision,
    ResponseLadder,
    detect_document_dithering,
    detect_drift,
    detect_excessive_testing,
    detect_false_done,
    detect_unnecessary_steps,
)
from chitra.goals import (
    GoalRecord,
    GoalsSchemaNewerError,
    GoalValidationError,
    add_ask,
    add_foreground_task,
    get_goal,
    hold_goal,
    list_goals,
    mark_completion_gate_passed,
    update_now,
)
from chitra.journal import (
    CanonicalEvent,
    CanonicalType,
    JournalIngestor,
    NormalizationContext,
    native_session_identity,
)
from chitra.journal.store import EventJournal
from chitra.policy_config import load_policy_config
from chitra.presence import append_presence
from chitra.question_handler import handle_question
from chitra.recovery import get_lane_lifecycle
from chitra.state_paths import state_dir as default_state_dir
from chitra.supervision import SupervisionLedger, goal_digest
from chitra.supervisor import reconcile_corrective_action, reconcile_question_action, record_observing
from chitra.systemd_notify import notify_ready, notify_watchdog
from chitra.transcript_bindings import DEFAULT_FILENAME, TranscriptBinding, load_transcript_bindings
from chitra.validation_receipts import record_enrolled_validator_runs

logger = structlog.get_logger(__name__)

DEFAULT_POLL_SECONDS = 60.0
PRESENCE_INSTANCE = "chitra-monitord"
MONITORD_SCHEMA = "chitra.monitord.pass.v1"
IDLE_PURSUIT_SCHEMA = "chitra.monitord.idle-pursuit.v1"
_DETECTOR_ORDER = (
    "canonical_choices.deprecated_path",
    "drift",
    "unnecessary_steps",
    "excessive_testing",
    "document_dithering",
)


@dataclass(frozen=True, slots=True)
class MonitordConfig:
    """All filesystem paths and timing required by one monitord pass."""

    state_dir: Path
    transcript_root: Path | None
    findings_path: Path
    poll_seconds: float
    shadow_mode: bool
    transcript_bindings_path: Path | None = None
    dispatch_queue_dir: Path | None = None
    ledger_path: Path | None = None
    ledger_key_path: Path | None = None
    retry_delay_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class LanePassResult:
    """The compact result of one monitoring pass over one lane."""

    lane: str
    ingested_events: int
    findings_opened: int
    ladder_actions: tuple[str, ...]
    completion_disputed: bool
    completion_verified: bool
    question_outcome: str
    validator_receipts_recorded: int


def resolve_config(
    *,
    state_dir: Path | None = None,
    transcript_root: Path | None = None,
    findings_path: Path | None = None,
    poll_seconds: float | None = None,
    shadow_mode: bool | None = None,
    transcript_bindings_path: Path | None = None,
    dispatch_queue_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    retry_delay_seconds: float = 60.0,
) -> MonitordConfig:
    """Resolve CLI arguments, then explicit environment overrides, then defaults."""
    resolved_state_dir = state_dir or default_state_dir()
    resolved_findings_path = findings_path or resolved_state_dir / "monitord-findings.jsonl"
    if poll_seconds is None:
        poll_seconds = DEFAULT_POLL_SECONDS
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be a positive number")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative")
    if shadow_mode is None:
        # Shadow mode is the safe default: findings are recorded but never
        # leave the monitor's own state until an operator turns the mode off.
        # An operator opts a unit out explicitly with CHITRA_MONITORD_SHADOW_MODE=0
        # (the shipped unit example pins it on).
        shadow_mode = os.environ.get("CHITRA_MONITORD_SHADOW_MODE", "").strip() != "0"
    resolved_bindings_path = transcript_bindings_path
    if resolved_bindings_path is None:
        resolved_bindings_path = (
            transcript_root / DEFAULT_FILENAME
            if transcript_root is not None
            else resolved_state_dir / DEFAULT_FILENAME
        )
    return MonitordConfig(
        state_dir=resolved_state_dir,
        transcript_root=transcript_root,
        findings_path=resolved_findings_path,
        poll_seconds=poll_seconds,
        shadow_mode=shadow_mode,
        transcript_bindings_path=resolved_bindings_path,
        dispatch_queue_dir=dispatch_queue_dir or resolved_state_dir / "queue",
        ledger_path=ledger_path or resolved_state_dir / "ledger.jsonl",
        ledger_key_path=ledger_key_path or resolved_state_dir / "ledger.key",
        retry_delay_seconds=retry_delay_seconds,
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


def ingest_transcript_bindings(
    config: MonitordConfig,
    bindings: tuple[TranscriptBinding, ...],
) -> tuple[CanonicalEvent, ...]:
    """Ingest every explicitly bound JSONL transcript before journal discovery."""
    observed: list[CanonicalEvent] = []
    manifest_path = config.transcript_bindings_path or config.state_dir / DEFAULT_FILENAME
    for binding in bindings:
        transcript_path = _resolved_binding_path(config, binding, manifest_path=manifest_path)
        context = NormalizationContext(
            instance=binding.instance,
            lane=binding.lane,
            client=binding.client,
            client_version=binding.client_version,
            goal_ref=binding.session_ref,
        )
        with JournalIngestor(
            state_root=config.state_dir,
            transcript_path=transcript_path,
            context=context,
        ) as ingestor:
            observed.extend(ingestor.poll().observed)
    if bindings:
        logger.info("monitord_bound_transcripts_ingested", bindings=len(bindings), events=len(observed))
    return tuple(observed)


def _resolved_binding_path(
    config: MonitordConfig,
    binding: TranscriptBinding,
    *,
    manifest_path: Path | None = None,
) -> Path:
    """Return the canonical path used by ingestion and event filtering."""
    resolved_manifest = manifest_path or config.transcript_bindings_path or config.state_dir / DEFAULT_FILENAME
    return binding.resolved_path(
        manifest_path=resolved_manifest,
        transcript_root=config.transcript_root,
    ).expanduser().resolve(strict=False)


def _event_matches_binding(
    event: CanonicalEvent,
    binding: TranscriptBinding,
    *,
    transcript_path: Path,
    native_session_id: str | None,
) -> bool:
    """Accept only events from the complete current transcript binding."""
    return bool(
        native_session_id
        and event.transcript.path == str(transcript_path)
        and event.session_id == native_session_id
        and event.lane == binding.lane
        and event.goal_ref == binding.session_ref
        and event.client == binding.client
        and event.client_version == binding.client_version
        and event.instance == binding.instance
    )


def _idle_pursuit_path(config: MonitordConfig, lane: str) -> Path:
    return config.state_dir / "idle-pursuit" / f"{lane}.json"


def _progress_digest(events: tuple[CanonicalEvent, ...]) -> str:
    """Digest only explicit scoped-progress evidence in the current journal."""
    progress_ids = [
        event.event_id
        for event in events
        if isinstance(event.payload.get("progress_evidence"), dict)
        and any(value is True for value in event.payload["progress_evidence"].values())
    ]
    encoded = json.dumps(progress_ids, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _idle_pursuit_finding(
    config: MonitordConfig,
    lane: str,
    goal: GoalRecord | None,
    events: tuple[CanonicalEvent, ...],
    findings: list[Finding],
    question_outcome: str,
) -> Finding | None:
    """Persist clean-pass count and emit one deterministic idle finding."""
    path = _idle_pursuit_path(config, lane)
    goal_digest_value = goal_digest(goal) if goal is not None else ""
    latest_supervision = SupervisionLedger(config.state_dir, lane).latest()
    delivery_pending = bool(
        latest_supervision is not None
        and latest_supervision.goal_digest == goal_digest_value
        and latest_supervision.state in {"action_pending", "action_queued", "awaiting_progress"}
    )
    actionable = (
        goal is not None
        and goal.status in {"working", "blocked", "turn-finished-unverified", "completion-disputed"}
        and bool(goal.enrolled_done_when_items)
        and bool(events)
        and not findings
        and question_outcome == "none"
        and not delivery_pending
        and not goal.open_asks
        and not goal.needs
    )
    if not actionable:
        with locked_json_store(path):
            write_json_atomic(
                path,
                {"schema": IDLE_PURSUIT_SCHEMA, "lane": lane, "count": 0},
                fsync=True,
            )
        return None

    assert goal is not None
    digest = goal_digest_value
    progress_digest = _progress_digest(events)
    source = {
        "path": events[0].transcript.path,
        "native_session_id": events[0].session_id,
        "lane": events[0].lane,
        "goal_ref": events[0].goal_ref,
        "client": events[0].client.value,
        "client_version": events[0].client_version,
        "instance": events[0].instance,
    }
    payload: dict[str, Any] = {}
    with locked_json_store(path):
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
            if isinstance(loaded, dict):
                payload = loaded
        same_identity = (
            payload.get("schema") == IDLE_PURSUIT_SCHEMA
            and payload.get("lane") == lane
            and payload.get("session_ref") == goal.session_ref
            and payload.get("goal_version") == goal.goal_version
            and payload.get("goal_digest") == digest
            and payload.get("source") == source
        )
        previous_progress_digest = payload.get("progress_digest") if same_identity else None
        previous_count = payload.get("count") if same_identity else 0
        count = previous_count + 1 if previous_progress_digest == progress_digest and isinstance(previous_count, int) else 1
        if previous_progress_digest is not None and previous_progress_digest != progress_digest:
            count = 0
        anchor = payload.get("anchor_event_id") if same_identity else None
        if not isinstance(anchor, str) or not anchor:
            anchor = events[0].event_id
        write_json_atomic(
            path,
            {
                "schema": IDLE_PURSUIT_SCHEMA,
                "lane": lane,
                "session_ref": goal.session_ref,
                "goal_version": goal.goal_version,
                "goal_digest": digest,
                "source": source,
                "progress_digest": progress_digest,
                "anchor_event_id": anchor,
                "count": count,
            },
            fsync=True,
        )
    idle_pursuit_passes = goal.autonomy_policy.idle_pursuit_passes
    if count < idle_pursuit_passes:
        return None
    first_item = goal.enrolled_done_when_items[0]
    return Finding(
        detector="idle_pursuit",
        fingerprint_seed={
            "anchor_event_id": anchor,
            "session_ref": goal.session_ref,
            "done_when_item_id": first_item.id,
        },
        event_refs=tuple(event.event_id for event in events[-3:]),
        unmet_item=first_item.id,
        expected_next_progress=f"take the next reversible in-scope action toward: {first_item.text}",
        detail=f"the enrolled goal produced no new scoped progress for {idle_pursuit_passes} clean monitor passes",
    )


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
    canonical_choices_policy: CanonicalChoicesPolicy | None = None,
) -> list[Finding]:
    """Run the deterministic detector set over one lane's journal."""
    scope_text = str(getattr(goal, "scope", "") or "")
    intent_text = str(getattr(goal, "intent", "") or "")
    goal_text = str(getattr(goal, "goal", "") or "")
    goal_is_document = "documentation" in f"{intent_text}\n{goal_text}".lower()
    enrolled_items = tuple(getattr(goal, "enrolled_done_when_items", ()) or ())
    policy = canonical_choices_policy or load_policy_config().canonical_choices
    findings: list[Finding] = []
    findings.extend(detect_canonical_choices(events, policy, enrolled_items=enrolled_items))
    findings.extend(detect_drift(events, scope_text=scope_text, declared_worktree="", enrolled_items=enrolled_items))
    findings.extend(detect_unnecessary_steps(events, enrolled_items=enrolled_items))
    findings.extend(detect_excessive_testing(events, enrolled_items=enrolled_items))
    findings.extend(detect_document_dithering(events, goal_is_document=goal_is_document, enrolled_items=enrolled_items))
    return [finding for name in _DETECTOR_ORDER for finding in findings if finding.detector == name]


def _bind_findings_to_goal(findings: list[Finding], goal: GoalRecord) -> list[Finding]:
    """Bind detector identities to the exact frozen goal being observed.

    Incident records remain keyed by their existing finding fingerprint.  The
    monitor therefore incorporates the current goal version and digest into a
    fresh fingerprint before a finding reaches the ladder.  A recurrence from
    an earlier goal revision cannot reuse or advance that revision's incident.
    """
    digest = goal_digest(goal)
    return [
        Finding(
            detector=finding.detector,
            fingerprint_seed={
                "finding_fingerprint": finding.fingerprint,
                "goal_version": goal.goal_version,
                "goal_digest": digest,
            },
            event_refs=finding.event_refs,
            unmet_item=finding.unmet_item,
            expected_next_progress=finding.expected_next_progress,
            detail=finding.detail,
        )
        for finding in findings
    ]


def evaluate_findings(
    config: MonitordConfig,
    lane: str,
    findings: list[Finding],
    *,
    order_marker: str = "[M] monitord",
    on_decision: Callable[[Finding, LadderDecision], None] | None = None,
    journal_events: tuple[CanonicalEvent, ...] = (),
    ledger_key: bytes | None = None,
) -> list[str]:
    """Feed every finding through the response ladder and return its actions."""
    ladder = ResponseLadder(
        IncidentStore(config.state_dir, lane),
        journal_events=journal_events,
        ledger_key=ledger_key,
    )
    actions: list[str] = []
    for finding in findings:
        marker = f"{order_marker}:{finding.fingerprint[:16]}"
        decision = ladder.evaluate(lane=lane, finding=finding, order_marker=marker)
        actions.append(decision.action)
        if on_decision is not None:
            on_decision(finding, decision)
        logger.info(
            "monitord_ladder_decision",
            lane=lane,
            detector=finding.detector,
            action=decision.action,
            stage=decision.stage,
            reason=decision.reason,
            shadow_mode=config.shadow_mode,
        )
    return actions


def check_enrollment_and_receipts(
    config: MonitordConfig,
    session_ref: str,
    final_response: CanonicalEvent | None = None,
) -> tuple[int, bool, list[Finding]]:
    """Verify an explicit completion claim against its enrolled contract.

    Validators run only when the latest unconsumed final response contains a
    structured completion line. The lane's claimed result is ignored; Chitra
    executes and stores each enrolled validator itself. A missing or held
    goal and a turn without a completion claim are silent.
    """
    try:
        goal = get_goal(config.state_dir, session_ref)
    except Exception:
        return 0, False, []
    if goal is None or goal.status not in {
        "working",
        "blocked",
        "turn-finished-unverified",
        "completion-disputed",
    }:
        return 0, False, []
    final_text = ""
    if final_response is not None:
        payload_text = final_response.payload.get("text")
        final_text = payload_text if isinstance(payload_text, str) else ""
    if not final_text or not is_completion_claim(final_text):
        return 0, False, []

    items = tuple(getattr(goal, "enrolled_done_when_items", ()) or ())
    if not items:
        findings = [
            Finding(
                detector="false_done",
                fingerprint_seed={"session_ref": session_ref, "reason": "unenrolled-completion"},
                event_refs=(final_response.event_id,) if final_response is not None else (),
                unmet_item="frozen completion contract",
                expected_next_progress="enroll exact done conditions and their independent validators before claiming completion",
                detail="completion was claimed without frozen enrolled done items",
            )
        ]
        if not config.shadow_mode:
            update_now(
                config.state_dir,
                session_ref,
                now=findings[0].detail,
                status="completion-disputed",
            )
        return 0, True, findings

    claimed_evidence = tuple(extract_completion_evidence(final_text))
    claim_bindings: dict[str, str] = {}
    for item in items:
        if any(
            proof.done_when_item_id == item.id
            and proof.receipt_name == item.required_receipt
            and proof.validator == item.validator
            for proof in claimed_evidence
        ):
            claim_bindings[item.id] = item.required_receipt

    run_evidence: tuple[CompletionEvidence, ...] = ()
    if has_structured_completion_line(final_text):
        run_evidence = record_enrolled_validator_runs(config.state_dir, session_ref, items)

    material_questions = (*goal.open_asks, *((goal.needs,) if goal.needs else ()))
    findings = detect_false_done(
        final_response=final_response,
        enrolled_items=items,
        receipt_names_by_item=claim_bindings,
        receipt_roots={session_ref: config.state_dir},
        session_ref=session_ref,
        material_questions=material_questions,
    )
    if not findings and not config.shadow_mode:
        try:
            mark_completion_gate_passed(
                config.state_dir,
                session_ref,
                now="independent enrolled validators passed for the exact completion claim",
                last_verified=final_response.event_id if final_response is not None else "",
                completion_evidence=run_evidence,
            )
        except GoalValidationError as exc:
            findings = [
                Finding(
                    detector="false_done",
                    fingerprint_seed={"session_ref": session_ref, "reason": "completion-store-rejected"},
                    event_refs=(final_response.event_id,) if final_response is not None else (),
                    unmet_item="verified completion receipts",
                    expected_next_progress="produce current verified receipts bound to this exact goal session",
                    detail=f"the completion store rejected the claimed evidence: {exc}",
                )
            ]

    disputed = bool(findings)
    if disputed and not config.shadow_mode:
        update_now(
            config.state_dir,
            session_ref,
            now="; ".join(finding.detail for finding in findings),
            status="completion-disputed",
        )
    return len(run_evidence), disputed, findings


def handle_agent_question(
    config: MonitordConfig,
    goal: GoalRecord,
    final_response: CanonicalEvent | None,
    *,
    journal_events: tuple[CanonicalEvent, ...] = (),
    lane: str | None = None,
) -> str:
    """Answer one routine frozen-goal question or record a foreground residual.

    The answer order is deterministic and dispatchd recomputes it from the
    current goal before any pane write. Ambiguous questions become a durable
    foreground-reasoning task. Protected authority classes remain explicit
    operator gates.
    """
    if final_response is None:
        return "none"
    payload_text = final_response.payload.get("text")
    if not isinstance(payload_text, str):
        return "none"
    question_lines = tuple(line.strip() for line in payload_text.splitlines() if "?" in line and line.strip())
    if not question_lines:
        return "none"

    if len(question_lines) == 1:
        question = question_lines[0]
        result = handle_question(goal, question)
    else:
        question = " ".join(question_lines)
        result = None

    if result is None or result.disposition == "residual":
        if not config.shadow_mode:
            reason = (
                "the completed turn asked multiple material questions"
                if result is None
                else result.reason
            )
            add_foreground_task(
                config.state_dir,
                goal.session_ref,
                kind="question",
                text=f"{reason}. Question: {question}",
                source="monitord",
            )
        return "reasoning_required"

    if result.disposition == "operator_required":
        if not config.shadow_mode:
            reason = f"the question requests operator-controlled authority: {result.reason}"
            add_ask(config.state_dir, goal.session_ref, f"{reason}. Question: {question}")
            hold_goal(config.state_dir, goal.session_ref, reason=f"operator-required question: {reason}")
        return "operator_required"

    assert result.answer is not None
    if config.shadow_mode:
        return "shadow_answer"
    if config.dispatch_queue_dir is None:
        raise ValueError("dispatch queue is required for autonomous goal answers")
    action = reconcile_question_action(
        state_root=config.state_dir,
        queue_dir=config.dispatch_queue_dir,
        lane=lane or goal.lane_id or final_response.lane.replace(":", "."),
        goal=goal,
        question_result=result,
        journal_events=journal_events,
        ledger_path=config.ledger_path,
        ledger_key_path=config.ledger_key_path,
        retry_delay_seconds=config.retry_delay_seconds,
    )
    if action.state == "action_queued":
        return "answer_queued"
    if action.state == "awaiting_progress":
        return "answer_awaiting_progress"
    if action.state == "blocked":
        return "answer_blocked"
    return action.state


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
    bindings = load_transcript_bindings(
        config.transcript_bindings_path,
        transcript_root=config.transcript_root,
    )
    ingest_transcript_bindings(config, bindings)
    bindings_by_lane = {binding.lane: binding for binding in bindings}
    binding_paths = {
        binding.lane: _resolved_binding_path(config, binding)
        for binding in bindings
    }
    binding_native_session_ids = {
        lane: native_session_identity(path)
        for lane, path in binding_paths.items()
    }
    try:
        goals_by_session = {goal.session_ref: goal for goal in list_goals(config.state_dir)}
    except GoalsSchemaNewerError as exc:
        # Keep the service alive so it can recover after an operator installs
        # a compatible reader, but never supervise against a partial reading
        # of a goal contract written by a newer schema.
        blocked_summary: dict[str, Any] = {
            "schema": MONITORD_SCHEMA,
            "lanes_observed": 0,
            "findings_opened": 0,
            "completion_disputed": False,
            "completion_verified": False,
            "question_answers_queued": 0,
            "questions_operator_required": 0,
            "validator_receipts_recorded": 0,
            "shadow_mode": config.shadow_mode,
            "blocked_reason": "goals-schema-newer-than-installed",
            "results": [],
        }
        logger.error("monitord_goals_schema_newer_than_installed", error=str(exc), **blocked_summary)
        return blocked_summary
    results: list[LanePassResult] = []
    for lane_path in _lane_roots(config.state_dir):
        lane = lane_path.stem
        binding = bindings_by_lane.get(lane)
        loaded_events = load_lane_events(config, lane)
        if binding is not None:
            events = tuple(
                event
                for event in loaded_events
                if _event_matches_binding(
                    event,
                    binding,
                    transcript_path=binding_paths[lane],
                    native_session_id=binding_native_session_ids.get(lane),
                )
            )
        else:
            events = loaded_events
        if not events:
            continue
        session_ref = binding.session_ref if binding is not None else lane
        goal = goals_by_session.get(binding.session_ref) if binding is not None else None
        lifecycle = get_lane_lifecycle(config.state_dir, session_ref)
        if lifecycle is not None and not lifecycle.enforcement_enabled:
            results.append(
                LanePassResult(
                    lane=lane,
                    ingested_events=len(events),
                    findings_opened=0,
                    ladder_actions=(),
                    completion_disputed=False,
                    completion_verified=False,
                    question_outcome="none",
                    validator_receipts_recorded=0,
                )
            )
            continue
        if binding is not None and goal is not None and (not goal.lane_id or goal.lane_id != binding.lane):
            logger.warning(
                "monitord_goal_binding_mismatch",
                lane=lane,
                session_ref=binding.session_ref,
                goal_lane_id=goal.lane_id,
                binding_lane=binding.lane,
            )
            goal = None
        if binding is not None and goal is None:
            logger.warning(
                "monitord_goal_binding_unresolved",
                lane=lane,
                session_ref=binding.session_ref,
                binding_lane=binding.lane,
            )
        detector_events = events
        supervision = SupervisionLedger(config.state_dir, lane)
        if goal is not None:
            latest_supervision = supervision.latest_consumed_boundary(
                goal_digest_value=goal_digest(goal)
            )
            if latest_supervision is not None and latest_supervision.turn_boundary_event_id:
                positions = {
                    event.event_id: index for index, event in enumerate(events)
                }
                boundary = positions.get(latest_supervision.turn_boundary_event_id)
                if boundary is not None:
                    detector_events = events[boundary + 1 :]
        if goal is not None:
            final_response = _final_response(detector_events)
            receipts_recorded, completion_disputed, enrollment_findings = check_enrollment_and_receipts(
                config,
                goal.session_ref,
                final_response,
            )
            refreshed_goal = get_goal(config.state_dir, goal.session_ref)
            if refreshed_goal is not None:
                goal = refreshed_goal
        else:
            # Legacy or unresolved bindings remain observable, but no other
            # goal may be borrowed for detector context or receipt checks.
            receipts_recorded, completion_disputed, enrollment_findings = 0, False, []
            final_response = None

        completion_verified = bool(goal is not None and goal.status == "done-pending-close")
        if completion_verified and goal is not None:
            latest_supervision = supervision.latest()
            if latest_supervision is None or latest_supervision.state != "completion_verified":
                final_response = _final_response(detector_events)
                supervision.transition(
                    state="completion_verified",
                    session_ref=goal.session_ref,
                    goal_version=goal.goal_version,
                    goal_digest_value=goal_digest(goal),
                    reason="exact completion claim passed independently executed enrolled validators",
                    finding_fingerprint="",
                    stage="",
                    order_id="",
                    order_marker="",
                    observed_event_id=final_response.event_id if final_response is not None else "",
                    turn_boundary_event_id=final_response.event_id if final_response is not None else "",
                    attempt=0,
                    next_retry_at="",
                    obstacle="",
                )

        detection_findings = (
            []
            if goal is not None and goal.status in {"held", "done-pending-verification", "done-pending-close"}
            else run_detectors(config, lane, goal, detector_events)
        )
        findings = detection_findings + [
            finding for finding in enrollment_findings if finding.detector == "false_done"
        ]
        question_outcome = (
            handle_agent_question(config, goal, final_response, journal_events=events, lane=lane)
            if goal is not None
            and goal.status not in {"held", "done-pending-verification", "done-pending-close"}
            else "none"
        )
        if question_outcome == "operator_required" and goal is not None and not config.shadow_mode:
            refreshed_goal = get_goal(config.state_dir, goal.session_ref)
            if refreshed_goal is not None:
                goal = refreshed_goal
        idle_finding = _idle_pursuit_finding(
            config,
            lane,
            goal,
            events,
            findings,
            question_outcome,
        )
        if idle_finding is not None:
            findings.append(idle_finding)
        if goal is not None:
            findings = _bind_findings_to_goal(findings, goal)
        def supervise_finding(
            finding: Finding,
            decision: LadderDecision,
            active_goal: GoalRecord | None = goal,
            active_lane: str = lane,
            active_events: tuple[CanonicalEvent, ...] = events,
        ) -> None:
            if active_goal is None:
                return
            if active_goal.status in {"held", "done-pending-verification", "done-pending-close"}:
                return
            if config.dispatch_queue_dir is None:
                raise ValueError("dispatch queue is required for persistent supervision")
            reconcile_corrective_action(
                state_root=config.state_dir,
                queue_dir=config.dispatch_queue_dir,
                lane=active_lane,
                goal=active_goal,
                finding=finding,
                decision=decision,
                shadow_mode=config.shadow_mode,
                journal_events=active_events,
                ledger_path=config.ledger_path,
                ledger_key_path=config.ledger_key_path,
                retry_delay_seconds=config.retry_delay_seconds,
            )

        if goal is None:
            # The journal remains observable for diagnosis, but an unresolved
            # or mismatched binding is not allowed to create or mutate an
            # incident that a later goal could accidentally inherit.
            scheduled_findings = []
            actions: list[str] = []
        else:
            # Every present finding is pursued in deterministic detector order.
            scheduled_findings = findings
            actions = evaluate_findings(
                config,
                lane,
                scheduled_findings,
                on_decision=supervise_finding,
                journal_events=events,
                ledger_key=(
                    config.ledger_key_path.read_bytes()
                    if config.ledger_key_path is not None and config.ledger_key_path.is_file()
                    else None
                ),
            )
        if goal is not None and not findings and question_outcome == "none":
            record_observing(
                state_root=config.state_dir,
                lane=lane,
                goal=goal,
                reason="exact bound goal observed with no corrective finding",
            )
        append_finding_records(config, lane, findings)
        results.append(
            LanePassResult(
                lane=lane,
                ingested_events=len(events),
                findings_opened=len(scheduled_findings),
                ladder_actions=tuple(actions),
                completion_disputed=completion_disputed,
                completion_verified=completion_verified,
                question_outcome=question_outcome,
                validator_receipts_recorded=receipts_recorded,
            )
        )
        append_presence(
            PRESENCE_INSTANCE,
            f"chitra-journal:{lane}",
            session=session_ref,
            lanes=(lane,),
            mode="using",
            purpose="persistent oversight monitor pass",
            root=config.state_dir,
        )
    summary: dict[str, Any] = {
        "schema": MONITORD_SCHEMA,
        "lanes_observed": len(results),
        "findings_opened": sum(result.findings_opened for result in results),
        "completion_disputed": any(result.completion_disputed for result in results),
        "completion_verified": any(result.completion_verified for result in results),
        "question_answers_queued": sum(result.question_outcome == "answer_queued" for result in results),
        "questions_reasoning_required": sum(result.question_outcome == "reasoning_required" for result in results),
        "questions_operator_required": sum(result.question_outcome == "operator_required" for result in results),
        "validator_receipts_recorded": sum(result.validator_receipts_recorded for result in results),
        "shadow_mode": config.shadow_mode,
        "results": [
            {
                "lane": result.lane,
                "ingested_events": result.ingested_events,
                "findings_opened": result.findings_opened,
                "ladder_actions": list(result.ladder_actions),
                "completion_disputed": result.completion_disputed,
                "completion_verified": result.completion_verified,
                "question_outcome": result.question_outcome,
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
        description="Persistently supervise exact goal-bound agent sessions through verified completion.",
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--transcript-root", type=Path, default=None)
    parser.add_argument("--transcript-bindings-path", type=Path, default=None)
    parser.add_argument("--dispatch-queue-dir", type=Path, default=None)
    parser.add_argument("--ledger-path", type=Path, default=None)
    parser.add_argument("--ledger-key-path", type=Path, default=None)
    parser.add_argument("--retry-delay-seconds", type=float, default=60.0)
    parser.add_argument("--findings-path", type=Path, default=None)
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--no-shadow-mode", dest="shadow_mode", action="store_false", help="Record findings outside shadow mode.")
    parser.add_argument("--once", action="store_true", help="Run one pass and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the daemon; malformed persisted input deliberately terminates it."""
    args = build_arg_parser().parse_args(argv)
    config = resolve_config(
        state_dir=args.state_dir,
        transcript_root=args.transcript_root,
        transcript_bindings_path=args.transcript_bindings_path,
        dispatch_queue_dir=args.dispatch_queue_dir,
        ledger_path=args.ledger_path,
        ledger_key_path=args.ledger_key_path,
        retry_delay_seconds=args.retry_delay_seconds,
        findings_path=args.findings_path,
        poll_seconds=args.poll_seconds,
        shadow_mode=args.shadow_mode,
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

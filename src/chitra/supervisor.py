"""Crash-safe corrective action composition for :mod:`chitra.monitord`.

The monitor decides what should happen and records that intent before it
publishes a queue order.  ``dispatchd`` remains the sole terminal writer.
On restart this module reconciles the deterministic order identity against
every queue location before it considers publishing anything again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chitra.detect import Finding, IncidentStore, LadderDecision
from chitra.detect.ladder import discover_consumption_proof, discover_delivery_consumption_proof
from chitra.dispatch import directive_voice_violation, enqueue_dispatch_order
from chitra.goals import GoalRecord
from chitra.journal import CanonicalEvent
from chitra.ledger import verify_delivery
from chitra.orders import DispatchOrder, DispatchResult, DispatchStatus
from chitra.question_handler import QuestionHandlerResult
from chitra.queue_state import QueueLayout, locate_order
from chitra.supervision import SupervisionLedger, deterministic_order_id, goal_digest


@dataclass(frozen=True, slots=True)
class CorrectiveActionResult:
    """One monitor pass's durable action outcome."""

    state: str
    order_id: str
    order_marker: str
    enqueued: bool
    reason: str


def order_marker(finding: Finding) -> str:
    """Return stable transcript text that binds consumption to one incident."""
    return f"[C] oversight:{finding.fingerprint[:16]}"


def build_corrective_order(
    goal: GoalRecord,
    finding: Finding,
    decision: LadderDecision,
    *,
    retry_attempt: int = 0,
) -> DispatchOrder:
    """Build one deterministic, goal-bound correction for dispatchd."""
    marker = decision.record.order_marker
    stage = decision.stage
    order_id = deterministic_order_id(
        goal.session_ref,
        goal.goal_version,
        finding.fingerprint,
        stage,
        retry_attempt=retry_attempt,
    )
    nudge = (
        f"{marker} Continue against the frozen goal: {goal.goal} "
        f"The observed obstacle is: {finding.detail}. "
        f"Take the next in-scope action that produces this progress: {finding.expected_next_progress}. "
        "Work through reversible, in-authority obstacles yourself. "
        "Escalate only missing credentials, spending, an action that cannot be undone, "
        "a security or authorization change, or a strategic choice the frozen goal does not settle. "
        f"Do not claim completion until this condition has independent proof: {goal.done_when} "
        f"This is the {stage} correction for the cited incident. Record the command, artifact, "
        "or receipt that proves the next state."
    )
    violation = directive_voice_violation(nudge)
    if violation is not None:
        raise ValueError(f"generated corrective order violates dispatch voice policy: {violation}")
    return DispatchOrder(
        order_id=order_id,
        session_ref=goal.session_ref,
        nudge=nudge,
        tag="[C]",
        task_type="persistent-oversight",
        goal_version=goal.goal_version,
        goal_digest=goal_digest(goal),
    )


def _same_action(latest: object | None, order: DispatchOrder, finding: Finding, stage: str) -> bool:
    return bool(
        latest is not None
        and getattr(latest, "order_id", "") == order.order_id
        and getattr(latest, "finding_fingerprint", "") == finding.fingerprint
        and getattr(latest, "stage", "") == stage
    )


def _same_incident(latest: object | None, finding: Finding, stage: str, digest: str) -> bool:
    return bool(
        latest is not None
        and getattr(latest, "finding_fingerprint", "") == finding.fingerprint
        and getattr(latest, "stage", "") == stage
        and getattr(latest, "goal_digest", "") == digest
    )


def _retry_due(value: str) -> bool:
    if not value:
        return True
    try:
        retry_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if retry_at.tzinfo is None:
        return False
    return datetime.now(UTC) >= retry_at.astimezone(UTC)


def _stored_result(path: Path | None) -> DispatchResult | None:
    if path is None:
        return None
    try:
        return DispatchResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"dispatch result is invalid: {path}: {exc}") from exc


def reconcile_corrective_action(
    *,
    state_root: Path,
    queue_dir: Path,
    lane: str,
    goal: GoalRecord,
    finding: Finding,
    decision: LadderDecision,
    shadow_mode: bool,
    journal_events: tuple[CanonicalEvent, ...] = (),
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    max_action_attempts: int = 3,
    retry_delay_seconds: float = 60.0,
) -> CorrectiveActionResult:
    """Persist, publish, or recover one correction without duplicate delivery."""
    if max_action_attempts < 1:
        raise ValueError("max_action_attempts must be at least 1")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative")
    ledger = SupervisionLedger(state_root, lane)
    digest = goal_digest(goal)
    marker = decision.record.order_marker
    stage = decision.stage
    latest = ledger.latest_for_action(finding.fingerprint, stage)
    same_incident = _same_incident(latest, finding, stage, digest)
    attempt = latest.attempt if same_incident and latest is not None else 0
    order = build_corrective_order(
        goal,
        finding,
        decision,
        retry_attempt=attempt,
    )

    if shadow_mode:
        if latest is None:
            ledger.transition(
                state="observing",
                session_ref=goal.session_ref,
                goal_version=goal.goal_version,
                goal_digest_value=digest,
                reason="shadow mode observed a corrective finding without acting",
                finding_fingerprint=finding.fingerprint,
                stage=stage,
                order_id=order.order_id,
                order_marker=marker,
                observed_event_id="",
                turn_boundary_event_id="",
                attempt=0,
                next_retry_at="",
                obstacle="",
            )
        return CorrectiveActionResult(
            state="observing",
            order_id=order.order_id,
            order_marker=marker,
            enqueued=False,
            reason="shadow mode",
        )

    if latest is not None and latest.goal_digest != digest:
        latest = ledger.transition(
            state="blocked",
            session_ref=goal.session_ref,
            goal_version=goal.goal_version,
            goal_digest_value=digest,
            reason="frozen goal changed; stale correction was not reused",
            finding_fingerprint=finding.fingerprint,
            stage=stage,
            order_id=order.order_id,
            order_marker=marker,
            observed_event_id="",
            turn_boundary_event_id="",
            obstacle="goal_changed",
            attempt=0,
            next_retry_at="",
        )
        attempt = 0
        order = build_corrective_order(goal, finding, decision, retry_attempt=attempt)

    if (
        latest is not None
        and _same_incident(latest, finding, stage, digest)
        and latest.state == "blocked"
    ):
        if latest.obstacle == "dispatch_retry_exhausted":
            return CorrectiveActionResult(
                "blocked",
                latest.order_id,
                latest.order_marker,
                False,
                "bounded dispatch retries are exhausted",
            )
        if not _retry_due(latest.next_retry_at):
            return CorrectiveActionResult(
                "blocked",
                order.order_id,
                marker,
                False,
                f"retry waits until {latest.next_retry_at}",
            )

    artifacts = locate_order(QueueLayout(queue_dir), order.order_id)
    result = _stored_result(artifacts.result_path)
    if result is not None:
        if result.order_id != order.order_id or result.session_ref != goal.session_ref:
            raise ValueError("dispatch result does not match the corrective order binding")
        if result.status is DispatchStatus.SENT:
            key = (
                ledger_key_path.read_bytes()
                if ledger_key_path is not None and ledger_key_path.is_file()
                else None
            )
            delivery_entry = (
                verify_delivery(
                    ledger_path,
                    key=key,
                    order_id=order.order_id,
                    session_ref=goal.session_ref,
                    nudge=order.nudge,
                )
                if result.delivery_ledger_verified
                and ledger_path is not None
                and key is not None
                else None
            )
            if delivery_entry is None:
                reason = "delivery result lacks valid signed-ledger proof"
                if not (
                    latest is not None
                    and _same_action(latest, order, finding, stage)
                    and latest.state == "action_queued"
                ):
                    ledger.transition(
                        state="action_queued",
                        session_ref=goal.session_ref,
                        goal_version=goal.goal_version,
                        goal_digest_value=digest,
                        reason=reason,
                        finding_fingerprint=finding.fingerprint,
                        stage=stage,
                        order_id=order.order_id,
                        order_marker=marker,
                        observed_event_id="",
                        turn_boundary_event_id="",
                        attempt=attempt,
                        next_retry_at="",
                        obstacle="",
                    )
                return CorrectiveActionResult("action_queued", order.order_id, marker, False, reason)

            assert key is not None
            proof = discover_consumption_proof(
                decision.record,
                journal_events,
                delivery_entry,
                key,
            )
            if proof is None:
                reason = "signed delivery is proven; waiting for a bound completed agent turn"
                if not (
                    latest is not None
                    and _same_action(latest, order, finding, stage)
                    and latest.state == "awaiting_progress"
                    and not latest.turn_boundary_event_id
                ):
                    ledger.transition(
                        state="awaiting_progress",
                        session_ref=goal.session_ref,
                        goal_version=goal.goal_version,
                        goal_digest_value=digest,
                        reason=reason,
                        finding_fingerprint=finding.fingerprint,
                        stage=stage,
                        order_id=order.order_id,
                        order_marker=marker,
                        observed_event_id="",
                        turn_boundary_event_id="",
                        attempt=attempt,
                        next_retry_at="",
                        obstacle="",
                    )
                return CorrectiveActionResult("awaiting_progress", order.order_id, marker, False, reason)

            if decision.record.consumption != proof:
                IncidentStore(state_root, lane).attach_consumption(
                    fingerprint=finding.fingerprint,
                    order_marker=marker,
                    proof=proof,
                )
            reason = "signed delivery and the bound completed agent turn are proven"
            if not (
                latest is not None
                and _same_action(latest, order, finding, stage)
                and latest.state == "awaiting_progress"
                and latest.observed_event_id == proof.user_event_id
                and latest.turn_boundary_event_id == proof.turn_event_id
            ):
                ledger.transition(
                    state="awaiting_progress",
                    session_ref=goal.session_ref,
                    goal_version=goal.goal_version,
                    goal_digest_value=digest,
                    reason=reason,
                    finding_fingerprint=finding.fingerprint,
                    stage=stage,
                    order_id=order.order_id,
                    order_marker=marker,
                    observed_event_id=proof.user_event_id,
                    turn_boundary_event_id=proof.turn_event_id,
                    attempt=attempt,
                    next_retry_at="",
                    obstacle="",
                )
            return CorrectiveActionResult("awaiting_progress", order.order_id, marker, False, reason)
        reason = f"dispatch ended {result.status.value}: {result.reason or 'no reason recorded'}"
        next_attempt = attempt + 1
        non_retryable = result.reason.startswith(("directive-voice:", "stale-goal-contract"))
        exhausted = non_retryable or next_attempt >= max_action_attempts
        next_retry_at = ""
        obstacle = "dispatch_retry_exhausted" if exhausted else "dispatch_terminal_failure"
        if not exhausted:
            next_retry_at = (
                datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
            ).isoformat()
        if not (
            latest is not None
            and _same_action(latest, order, finding, stage)
            and latest.state == "blocked"
            and latest.attempt == next_attempt
        ):
            ledger.transition(
                state="blocked",
                session_ref=goal.session_ref,
                goal_version=goal.goal_version,
                goal_digest_value=digest,
                reason=reason,
                finding_fingerprint=finding.fingerprint,
                stage=stage,
                order_id=order.order_id,
                order_marker=marker,
                observed_event_id="",
                turn_boundary_event_id="",
                obstacle=obstacle,
                attempt=next_attempt,
                next_retry_at=next_retry_at,
            )
        result_reason = (
            f"{reason}; bounded retry budget exhausted"
            if exhausted
            else f"{reason}; retry scheduled for {next_retry_at}"
        )
        return CorrectiveActionResult("blocked", order.order_id, marker, False, result_reason)

    if artifacts.order_paths:
        if (
            latest is not None
            and _same_action(latest, order, finding, stage)
            and latest.state == "awaiting_progress"
        ):
            return CorrectiveActionResult(
                "awaiting_progress",
                order.order_id,
                marker,
                False,
                "delivered action remains bound to its durable processed order",
            )
        if not (
            latest is not None
            and _same_action(latest, order, finding, stage)
            and latest.state == "action_queued"
        ):
            ledger.transition(
                state="action_queued",
                session_ref=goal.session_ref,
                goal_version=goal.goal_version,
                goal_digest_value=digest,
                reason="deterministic corrective order already exists in the dispatch queue",
                finding_fingerprint=finding.fingerprint,
                stage=stage,
                order_id=order.order_id,
                order_marker=marker,
                observed_event_id="",
                turn_boundary_event_id="",
                attempt=attempt,
                next_retry_at="",
                obstacle="",
            )
        return CorrectiveActionResult(
            "action_queued",
            order.order_id,
            marker,
            False,
            "existing durable queue order",
        )

    if decision.action == "hold" and latest is None:
        return CorrectiveActionResult(
            "blocked",
            order.order_id,
            marker,
            False,
            "held incident has no matching durable corrective intent",
        )

    if decision.action == "hold" and latest is not None:
        if not _same_incident(latest, finding, stage, digest):
            return CorrectiveActionResult(
                latest.state,
                latest.order_id,
                latest.order_marker,
                False,
                "held incident has no matching durable corrective intent",
            )
        if latest.state not in {"action_pending", "blocked"}:
            if latest.state == "action_queued":
                ledger.transition(
                    state="blocked",
                    session_ref=goal.session_ref,
                    goal_version=goal.goal_version,
                    goal_digest_value=digest,
                    reason="queued corrective action disappeared without a result; refusing an unproven repaste",
                    finding_fingerprint=finding.fingerprint,
                    stage=stage,
                    order_id=order.order_id,
                    order_marker=marker,
                    observed_event_id="",
                    turn_boundary_event_id="",
                    attempt=attempt,
                    next_retry_at="",
                    obstacle="queue_state_missing",
                )
                return CorrectiveActionResult(
                    "blocked",
                    order.order_id,
                    marker,
                    False,
                    "queue state disappeared; duplicate delivery is not safe",
                )
            return CorrectiveActionResult(
                latest.state,
                latest.order_id,
                latest.order_marker,
                False,
                "held incident does not authorize a new correction",
            )

    if not (
        latest is not None
        and _same_action(latest, order, finding, stage)
        and latest.state == "action_pending"
    ):
        ledger.transition(
            state="action_pending",
            session_ref=goal.session_ref,
            goal_version=goal.goal_version,
            goal_digest_value=digest,
            reason="corrective intent persisted before queue publication",
            finding_fingerprint=finding.fingerprint,
            stage=stage,
            order_id=order.order_id,
            order_marker=marker,
            observed_event_id="",
            turn_boundary_event_id="",
            attempt=attempt,
            next_retry_at="",
            obstacle="",
        )
    enqueue_dispatch_order(queue_dir, order)
    ledger.transition(
        state="action_queued",
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest_value=digest,
        reason="corrective order durably published for dispatchd",
        finding_fingerprint=finding.fingerprint,
        stage=stage,
        order_id=order.order_id,
        order_marker=marker,
        observed_event_id="",
        turn_boundary_event_id="",
        attempt=attempt,
        next_retry_at="",
        obstacle="",
    )
    return CorrectiveActionResult(
        "action_queued",
        order.order_id,
        marker,
        True,
        "corrective order durably published for dispatchd",
    )


def build_question_order(
    goal: GoalRecord,
    question_result: QuestionHandlerResult,
    *,
    retry_attempt: int = 0,
) -> DispatchOrder:
    """Build one deterministic, goal-bound answer order."""
    if question_result.disposition != "answered" or question_result.answer is None:
        raise ValueError("only an answered question can be dispatched")
    if question_result.session_ref != goal.session_ref or question_result.goal_version != goal.goal_version:
        raise ValueError("question result does not match the current goal")
    digest = goal_digest(goal)
    if question_result.goal_digest != digest:
        raise ValueError("question result is stale for the current goal")
    fingerprint = f"question:{question_result.request_id}"
    order_id = deterministic_order_id(
        goal.session_ref,
        goal.goal_version,
        fingerprint,
        "question",
        retry_attempt=retry_attempt,
    )
    violation = directive_voice_violation(question_result.answer)
    if violation is not None:
        raise ValueError(f"generated question answer violates dispatch voice policy: {violation}")
    return DispatchOrder(
        order_id=order_id,
        session_ref=goal.session_ref,
        nudge=question_result.answer,
        tag="[C]",
        task_type="persistent-oversight",
        goal_version=goal.goal_version,
        goal_digest=digest,
        message_kind="goal_contract_answer",
        question_result=question_result,
    )


def reconcile_question_action(
    *,
    state_root: Path,
    queue_dir: Path,
    lane: str,
    goal: GoalRecord,
    question_result: QuestionHandlerResult,
    journal_events: tuple[CanonicalEvent, ...] = (),
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    max_action_attempts: int = 3,
    retry_delay_seconds: float = 60.0,
) -> CorrectiveActionResult:
    """Persist, deliver, and reconcile a routine frozen-goal answer.

    Question answers use the same durable supervision ledger as corrective
    actions.  A restart can recover an existing queue/result, retry a bounded
    terminal failure with a new deterministic order id, and mark the answer
    consumed only after signed delivery plus a bound user turn and final
    response are present.
    """
    if max_action_attempts < 1:
        raise ValueError("max_action_attempts must be at least 1")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative")
    order = build_question_order(goal, question_result)
    fingerprint = f"question:{question_result.request_id}"
    stage = "question"
    marker = order.nudge
    digest = goal_digest(goal)
    ledger = SupervisionLedger(state_root / "question-actions", lane)
    latest = ledger.latest_for_action(fingerprint, stage)
    same = bool(
        latest is not None
        and latest.finding_fingerprint == fingerprint
        and latest.stage == stage
        and latest.goal_digest == digest
    )
    attempt = latest.attempt if same and latest is not None else 0

    if latest is not None and latest.goal_digest != digest:
        ledger.transition(
            state="blocked",
            session_ref=goal.session_ref,
            goal_version=goal.goal_version,
            goal_digest_value=digest,
            reason="frozen goal changed; stale question answer was not reused",
            finding_fingerprint=fingerprint,
            stage=stage,
            order_id=order.order_id,
            order_marker=marker,
            observed_event_id="",
            turn_boundary_event_id="",
            attempt=0,
            next_retry_at="",
            obstacle="goal_changed",
        )
        latest = ledger.latest_for_action(fingerprint, stage)
        attempt = 0

    if (
        latest is not None
        and latest.finding_fingerprint == fingerprint
        and latest.stage == stage
        and latest.goal_digest == digest
        and latest.state == "blocked"
    ):
        if latest.obstacle == "dispatch_retry_exhausted":
            return CorrectiveActionResult("blocked", latest.order_id, latest.order_marker, False, "bounded dispatch retries are exhausted")
        if not _retry_due(latest.next_retry_at):
            return CorrectiveActionResult("blocked", order.order_id, marker, False, f"retry waits until {latest.next_retry_at}")

    # Retries use a new deterministic queue identity. Rebuild after loading
    # the keyed action row so recovery never inspects or republishes r0.
    order = build_question_order(goal, question_result, retry_attempt=attempt)

    artifacts = locate_order(QueueLayout(queue_dir), order.order_id)
    result = _stored_result(artifacts.result_path)
    if result is not None:
        if result.order_id != order.order_id or result.session_ref != goal.session_ref:
            raise ValueError("dispatch result does not match the question answer binding")
        if result.status is DispatchStatus.SENT:
            key = ledger_key_path.read_bytes() if ledger_key_path is not None and ledger_key_path.is_file() else None
            delivery_entry = (
                verify_delivery(ledger_path, key=key, order_id=order.order_id, session_ref=goal.session_ref, nudge=order.nudge)
                if result.delivery_ledger_verified and ledger_path is not None and key is not None
                else None
            )
            if delivery_entry is None and key is not None and ledger_path is not None:
                delivery_entry = verify_delivery(
                    ledger_path,
                    key=key,
                    order_id=order.order_id,
                    session_ref=goal.session_ref,
                    nudge=order.nudge,
                )
            if delivery_entry is None:
                if not (
                    latest is not None
                    and latest.finding_fingerprint == fingerprint
                    and latest.stage == stage
                    and latest.state == "action_queued"
                ):
                    ledger.transition(
                        state="action_queued",
                        session_ref=goal.session_ref,
                        goal_version=goal.goal_version,
                        goal_digest_value=digest,
                        reason="question answer result exists; waiting for signed delivery proof",
                        finding_fingerprint=fingerprint,
                        stage=stage,
                        order_id=order.order_id,
                        order_marker=marker,
                        observed_event_id="",
                        turn_boundary_event_id="",
                    )
                return CorrectiveActionResult(
                    "action_queued",
                    order.order_id,
                    marker,
                    False,
                    "delivery result lacks valid signed-ledger proof",
                )
            assert key is not None
            proof = discover_delivery_consumption_proof(
                lane=lane,
                session_ref=goal.session_ref,
                order_marker=order.nudge,
                journal_events=journal_events,
                ledger_entry=delivery_entry,
                ledger_key=key,
            )
            if proof is None:
                if not (
                    latest is not None
                    and latest.finding_fingerprint == fingerprint
                    and latest.stage == stage
                    and latest.state == "awaiting_progress"
                    and not latest.turn_boundary_event_id
                ):
                    ledger.transition(
                        state="awaiting_progress",
                        session_ref=goal.session_ref,
                        goal_version=goal.goal_version,
                        goal_digest_value=digest,
                        reason="signed question delivery is proven; waiting for a bound completed agent turn",
                        finding_fingerprint=fingerprint,
                        stage=stage,
                        order_id=order.order_id,
                        order_marker=marker,
                        observed_event_id="",
                        turn_boundary_event_id="",
                    )
                return CorrectiveActionResult(
                    "awaiting_progress",
                    order.order_id,
                    marker,
                    False,
                    "signed delivery is proven; waiting for a bound completed agent turn",
                )
            if not (
                latest is not None
                and latest.finding_fingerprint == fingerprint
                and latest.stage == stage
                and latest.state == "awaiting_progress"
                and latest.observed_event_id == proof.user_event_id
                and latest.turn_boundary_event_id == proof.turn_event_id
            ):
                ledger.transition(
                    state="awaiting_progress",
                    session_ref=goal.session_ref,
                    goal_version=goal.goal_version,
                    goal_digest_value=digest,
                    reason="signed question delivery and the bound completed agent turn are proven",
                    finding_fingerprint=fingerprint,
                    stage=stage,
                    order_id=order.order_id,
                    order_marker=marker,
                    observed_event_id=proof.user_event_id,
                    turn_boundary_event_id=proof.turn_event_id,
                )
            return CorrectiveActionResult(
                "awaiting_progress",
                order.order_id,
                marker,
                False,
                "signed delivery and the bound completed agent turn are proven",
            )
        reason = f"dispatch ended {result.status.value}: {result.reason or 'no reason recorded'}"
        next_attempt = attempt + 1
        non_retryable = result.reason.startswith(("directive-voice:", "stale-goal-contract", "invalid-goal-contract-answer"))
        exhausted = non_retryable or next_attempt >= max_action_attempts
        next_retry_at = "" if exhausted else (datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)).isoformat()
        obstacle = "dispatch_retry_exhausted" if exhausted else "dispatch_terminal_failure"
        if not (
            latest is not None
            and latest.finding_fingerprint == fingerprint
            and latest.stage == stage
            and latest.state == "blocked"
            and latest.attempt == next_attempt
        ):
            ledger.transition(
                state="blocked",
                session_ref=goal.session_ref,
                goal_version=goal.goal_version,
                goal_digest_value=digest,
                reason=reason,
                finding_fingerprint=fingerprint,
                stage=stage,
                order_id=order.order_id,
                order_marker=marker,
                observed_event_id="",
                turn_boundary_event_id="",
                obstacle=obstacle,
                attempt=next_attempt,
                next_retry_at=next_retry_at,
            )
        suffix = "bounded retry budget exhausted" if exhausted else f"retry scheduled for {next_retry_at}"
        return CorrectiveActionResult("blocked", order.order_id, marker, False, f"{reason}; {suffix}")

    if artifacts.order_paths:
        if not (
            latest is not None
            and latest.finding_fingerprint == fingerprint
            and latest.stage == stage
            and latest.state == "action_queued"
        ):
            ledger.transition(
                state="action_queued",
                session_ref=goal.session_ref,
                goal_version=goal.goal_version,
                goal_digest_value=digest,
                reason="deterministic question answer already exists in the dispatch queue",
                finding_fingerprint=fingerprint,
                stage=stage,
                order_id=order.order_id,
                order_marker=marker,
                observed_event_id="",
                turn_boundary_event_id="",
            )
        return CorrectiveActionResult("action_queued", order.order_id, marker, False, "existing durable queue order")

    ledger.transition(
        state="action_pending",
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest_value=digest,
        reason="question answer intent persisted before queue publication",
        finding_fingerprint=fingerprint,
        stage=stage,
        order_id=order.order_id,
        order_marker=marker,
        observed_event_id="",
        turn_boundary_event_id="",
        attempt=attempt,
        next_retry_at="",
        obstacle="",
    )
    enqueue_dispatch_order(queue_dir, order)
    ledger.transition(
        state="action_queued",
        session_ref=goal.session_ref,
        goal_version=goal.goal_version,
        goal_digest_value=digest,
        reason="question answer durably published for dispatchd",
        finding_fingerprint=fingerprint,
        stage=stage,
        order_id=order.order_id,
        order_marker=marker,
        observed_event_id="",
        turn_boundary_event_id="",
        attempt=attempt,
        next_retry_at="",
        obstacle="",
    )
    return CorrectiveActionResult(
        "action_queued",
        order.order_id,
        marker,
        True,
        "question answer durably published for dispatchd",
    )


def record_observing(*, state_root: Path, lane: str, goal: GoalRecord, reason: str) -> None:
    """Create the initial durable observation state without masking live work."""
    ledger = SupervisionLedger(state_root, lane)
    latest = ledger.latest()
    if latest is None:
        ledger.transition(
            state="observing",
            session_ref=goal.session_ref,
            goal_version=goal.goal_version,
            goal_digest_value=goal_digest(goal),
            reason=reason,
        )
    elif latest.state == "awaiting_progress" and latest.turn_boundary_event_id:
        ledger.transition(
            state="observing",
            session_ref=goal.session_ref,
            goal_version=goal.goal_version,
            goal_digest_value=goal_digest(goal),
            reason="consumed correction cleared the prior finding; oversight continues",
        )


__all__ = [
    "CorrectiveActionResult",
    "build_corrective_order",
    "build_question_order",
    "order_marker",
    "reconcile_corrective_action",
    "reconcile_question_action",
    "record_observing",
]

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
from typing import Literal

from chitra.autonomy import autonomy_policy_sha256
from chitra.detect import Finding, IncidentStore, LadderDecision
from chitra.detect.ladder import discover_consumption_proof, discover_delivery_consumption_proof
from chitra.dispatch import directive_voice_violation, enqueue_dispatch_order
from chitra.goals import GoalRecord, add_foreground_task, hold_goal
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


DeliveryFailureDisposition = Literal["foreground_replan", "lifecycle_wait", "transport_retry"]

# Persistent doggedness is a Chitra goal, so the escalation is capped, never
# the pursuit: a lane whose transport keeps rejecting the same corrective
# nudge stops retrying every ``retry_delay_seconds`` after this many
# attempts and is instead held for foreground/operator attention (mirrors
# ``rate_limit_guard._escalate_or_retry``'s cap-then-escalate shape).
MAX_CORRECTIVE_RETRY_ATTEMPTS = 8
MAX_CORRECTIVE_RETRY_DELAY_SECONDS = 3600.0


def _autonomy_grant_summary(goal: GoalRecord) -> str:
    """Render the enrolled grants compactly for the supervised foreground."""
    grants: list[str] = []
    for grant in goal.autonomy_policy.grants:
        limits: list[str] = []
        if grant.max_amount is not None:
            limits.append(f"amount<={grant.max_amount} {grant.currency}")
        if grant.max_units is not None:
            limits.append(f"units<={grant.max_units}")
        if grant.expires_at is not None:
            limits.append(f"expires={grant.expires_at.isoformat()}")
        suffix = f" ({', '.join(limits)})" if limits else ""
        grants.append(f"{grant.capability}@{'|'.join(grant.targets)}{suffix}")
    return ", ".join(grants) if grants else "none"


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
        f"The frozen autonomy policy is sha256:{autonomy_policy_sha256(goal.autonomy_policy)} and grants: "
        f"{_autonomy_grant_summary(goal)}. "
        "Continue autonomously when an active grant covers the action target and limits, including credentials, spending, "
        "irreversible steps, security changes, dependencies, schemas, hooks, and tactical redesigns. "
        "When authority evidence is incomplete, investigate it and replan instead of stopping. "
        "Request a ruling only after policy evaluation proves a missing, expired, wrong-target, or over-limit grant, "
        "or the action would change the frozen outcome. "
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


def classify_delivery_failure(result: DispatchResult) -> DeliveryFailureDisposition:
    """Separate deterministic policy/state rejection from transport failure."""
    reason = result.reason
    if reason in {"goal-not-actionable", "goals-schema-newer-than-installed", "lane-lifecycle-closed"} or (
        reason.startswith("lane-lifecycle-") and reason.endswith("-deferred")
    ):
        return "lifecycle_wait"
    if result.status is DispatchStatus.COMPLETION_DISPUTE or reason.startswith(
        (
            "directive-voice:",
            "stale-goal-contract",
            "invalid-goal-contract-answer",
            "invalid-order:",
            "missing-transcript-binding",
            "lane-lifecycle-unavailable:",
            "lane-lifecycle-unknown:",
            "session namespace denied by prefix",
            "session namespace is not owned by this dispatcher",
            "remote dispatch to ",
            "unsupported session_ref ",
        )
    ):
        return "foreground_replan"
    return "transport_retry"


def _record_foreground_replan(
    state_root: Path,
    goal: GoalRecord,
    *,
    finding: str,
    reason: str,
) -> None:
    """Give the foreground supervisor a durable task instead of replaying semantics."""
    add_foreground_task(
        state_root,
        goal.session_ref,
        kind="replan",
        source="supervisor",
        text=(
            f"Dispatch for {finding} was rejected semantically: {reason}. "
            "Inspect current goal, evidence, and delivery contract; then replan or issue a corrected action."
        ),
    )


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
    retry_delay_seconds: float = 60.0,
    max_attempts: int = MAX_CORRECTIVE_RETRY_ATTEMPTS,
) -> CorrectiveActionResult:
    """Persist, publish, or recover one correction without duplicate delivery.

    A failed delivery records its attempt and returns to the durable pursuit
    loop after a backed-off retry delay. Attempts are evidence, never a
    reason to abandon an unfinished goal -- but past ``max_attempts`` the
    lane is held for foreground attention instead of nudging forever.
    """
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
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
        and not _retry_due(latest.next_retry_at)
    ):
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
        failure_disposition = classify_delivery_failure(result)
        if failure_disposition != "transport_retry":
            if failure_disposition == "foreground_replan":
                _record_foreground_replan(
                    state_root,
                    goal,
                    finding=f"corrective finding {finding.fingerprint}",
                    reason=reason,
                )
            if not (
                latest is not None
                and _same_action(latest, order, finding, stage)
                and latest.state == "blocked"
                and latest.obstacle == failure_disposition
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
                    obstacle=failure_disposition,
                    attempt=attempt,
                    next_retry_at="",
                )
            suffix = "foreground replan required" if failure_disposition == "foreground_replan" else "lifecycle wait required"
            return CorrectiveActionResult("blocked", order.order_id, marker, False, f"{reason}; {suffix}")
        next_attempt = attempt + 1
        if next_attempt >= max_attempts:
            hold_reason = (
                f"corrective-retry-exhausted: {next_attempt} attempts of stage "
                f"{stage!r} for finding {finding.fingerprint} all ended in "
                f"transport failure ({reason})"
            )
            hold_goal(state_root, goal.session_ref, reason=hold_reason)
            if not (
                latest is not None
                and _same_action(latest, order, finding, stage)
                and latest.state == "blocked"
                and latest.obstacle == "retry_cap_exceeded"
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
                    obstacle="retry_cap_exceeded",
                    attempt=next_attempt,
                    next_retry_at="",
                )
            return CorrectiveActionResult(
                "blocked",
                order.order_id,
                marker,
                False,
                f"{reason}; retry cap of {max_attempts} exceeded, lane held: {hold_reason}",
            )
        # Backoff doubles with each attempt, capped so a permanently broken
        # transport still gets checked on a bounded schedule rather than
        # spinning at the base interval forever.
        backoff_seconds = min(retry_delay_seconds * (2 ** (next_attempt - 1)), MAX_CORRECTIVE_RETRY_DELAY_SECONDS)
        next_retry_at = (datetime.now(UTC) + timedelta(seconds=backoff_seconds)).isoformat()
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
                obstacle="dispatch_terminal_failure",
                attempt=next_attempt,
                next_retry_at=next_retry_at,
            )
        return CorrectiveActionResult(
            "blocked",
            order.order_id,
            marker,
            False,
            f"{reason}; retry scheduled for {next_retry_at}",
        )

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
    retry_delay_seconds: float = 60.0,
) -> CorrectiveActionResult:
    """Persist, deliver, and reconcile a routine frozen-goal answer.

    Question answers use the same durable supervision ledger as corrective
    actions. A restart can recover an existing queue/result, retry every
    terminal failure with a new deterministic order id, and mark the answer
    consumed only after signed delivery plus a bound user turn and final
    response are present.
    """
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
        and not _retry_due(latest.next_retry_at)
    ):
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
        failure_disposition = classify_delivery_failure(result)
        if failure_disposition != "transport_retry":
            if failure_disposition == "foreground_replan":
                _record_foreground_replan(
                    state_root,
                    goal,
                    finding=f"question {question_result.request_id}",
                    reason=reason,
                )
            if not (
                latest is not None
                and latest.finding_fingerprint == fingerprint
                and latest.stage == stage
                and latest.state == "blocked"
                and latest.obstacle == failure_disposition
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
                    obstacle=failure_disposition,
                    attempt=attempt,
                    next_retry_at="",
                )
            suffix = "foreground replan required" if failure_disposition == "foreground_replan" else "lifecycle wait required"
            return CorrectiveActionResult("blocked", order.order_id, marker, False, f"{reason}; {suffix}")
        next_attempt = attempt + 1
        next_retry_at = (datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)).isoformat()
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
                obstacle="dispatch_terminal_failure",
                attempt=next_attempt,
                next_retry_at=next_retry_at,
            )
        return CorrectiveActionResult("blocked", order.order_id, marker, False, f"{reason}; retry scheduled for {next_retry_at}")

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

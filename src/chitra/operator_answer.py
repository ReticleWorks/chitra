"""Durable operator answers: one canonical ruling, one lane order, one resume.

A board answer (or ``chitra-goals answer``) must leave three durable facts:
the ruling in ``decisions.jsonl`` -- the store the question gate already
reads -- the lane's asks retired with the verbatim answer as their basis,
and a typed ``operator_relay`` order carrying the exact text to the lane
through the governed queue. A lane the question gate itself held is then
resumed; every other hold (rate-limit, transfer, manual) keeps its own
resume path and the order waits in ``deferred/`` for it.

Each step is idempotent and ordered so a crash between them leaves state
the next call can converge: the decision id is content-addressed, the order
id is deterministic, and ``enqueue_dispatch_order`` admits a duplicate
order exactly once. ``dispatchd`` remains the only writer to a lane pane.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import structlog

from chitra._fsio import exclusive_lock
from chitra.decisions import DecisionEntry, append_decision, read_decisions
from chitra.dispatch import enqueue_dispatch_order
from chitra.dispatchd import requeue_deferred_for_session
from chitra.goals import (
    GoalNotFoundError,
    GoalRecord,
    get_goal,
    resolve_ask,
    resolve_foreground_task,
    resume_goal,
)
from chitra.orders import DispatchOrder, DispatchResult, DispatchStatus
from chitra.queue_state import QueueLayout, locate_order
from chitra.recovery import get_lane_lifecycle
from chitra.state_paths import state_dir
from chitra.supervision import deterministic_order_id, goal_digest

logger = structlog.get_logger(__name__)

# The hold marker monitord writes for an operator-gated question. Only a
# hold carrying this prefix is released by an operator answer; rate-limit,
# transfer, and manual holds keep their own resume paths.
QUESTION_HOLD_PREFIX = "operator-required question:"

# The separator monitord embeds between its gate context and the lane's
# verbatim question inside one open ask.
QUESTION_MARKER = "Question: "

# Live order-file locations: an order in any of these is still claimable.
_LIVE_ORDER_DIRS = frozenset({"orders", "in_flight", "deferred"})

_MAX_RELAY_ATTEMPTS = 100


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def ask_question_text(ask: str) -> str:
    """Return the lane's verbatim question from a monitor-composed ask."""
    _, _, question = ask.partition(QUESTION_MARKER)
    return (question or ask).strip()


def _answer_decision_id(session_ref: str, goal_version: int, text: str) -> str:
    """Content-addressed id: resubmitting the same answer is the same ruling."""
    encoded = json.dumps([session_ref, goal_version, text], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"board-answer-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _relay_fingerprint(decision_id: str) -> str:
    return f"operator-answer:{decision_id}"


def _safe_name(value: str) -> str:
    """A session_ref as a single safe path component for the answer lock."""
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value) or "lane"


def _enqueue_relay_order(
    queue_dir: Path,
    goal: GoalRecord,
    text: str,
    decision: DecisionEntry,
) -> Path | None:
    """Enqueue the operator-answer relay once; return its order path.

    Order ids are deterministic per attempt. An identical resubmission whose
    relay is still live is a no-op; one already proven delivered is not
    re-pasted; one whose earlier attempts all reached a terminal result gets
    the next attempt id, so the retry is a distinct audited order.
    """
    layout = QueueLayout(queue_dir)
    for attempt in range(_MAX_RELAY_ATTEMPTS):
        order_id = deterministic_order_id(
            goal.session_ref,
            goal.goal_version,
            _relay_fingerprint(decision.decision_id),
            "operator-answer",
            retry_attempt=attempt,
        )
        artifacts = locate_order(layout, order_id)
        if not artifacts.exists:
            break
        if any(path.parent.name in _LIVE_ORDER_DIRS for path in artifacts.order_paths):
            return next(path for path in artifacts.order_paths if path.parent.name in _LIVE_ORDER_DIRS)
        if artifacts.result_path is not None:
            try:
                stored = DispatchResult.model_validate_json(artifacts.result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                stored = None
            if stored is not None and stored.status == DispatchStatus.SENT:
                logger.info(
                    "operator_answer_already_delivered",
                    session_ref=goal.session_ref,
                    order_id=order_id,
                )
                return artifacts.result_path
    else:
        raise ValueError(f"operator answer order for {decision.decision_id} exhausted {_MAX_RELAY_ATTEMPTS} attempts")
    order = DispatchOrder(
        order_id=order_id,
        session_ref=goal.session_ref,
        nudge=f"[O] {text}",
        tag="[O]",
        goal_version=goal.goal_version,
        goal_digest=goal_digest(goal),
        message_kind="operator_relay",
        created_at=decision.at,
    )
    return enqueue_dispatch_order(queue_dir, order)


def record_operator_answer(
    root: Path | None,
    session_ref: str,
    text: str,
    *,
    queue_dir: Path | None = None,
) -> GoalRecord:
    """Record one operator answer durably, relay it to the lane, and resume.

    The ruling is appended to ``decisions.jsonl`` first so it exists even if
    every later step fails: a re-ask then resolves through the ordinary ask
    gate. Open asks are retired with the verbatim answer as their basis, and
    open question-kind foreground tasks are resolved with it too -- a lane
    whose question went to the residual path still gets its answer. The
    ``operator_relay`` order is bound to the exact goal contract; while the
    lane is held, dispatchd parks it in ``deferred/`` instead of dying
    ``goal-not-actionable``, and this function's own requeue returns it to
    ``orders/`` once the hold that answered it is released.

    Automatic resume fires only when the goal is still held BY the question
    gate and no open ask remains -- a newer unanswered ask, a foreign hold
    (rate-limit, transfer, manual), or a closed lane keeps its own state.
    """
    text = text.strip()
    if not text:
        raise ValueError("answer text must not be empty")
    decisions_path = (root or state_dir()) / "decisions.jsonl"
    resolved_queue = queue_dir if queue_dir is not None else (root or state_dir()) / "queue"

    goal = get_goal(root, session_ref)
    if goal is None:
        raise GoalNotFoundError(session_ref)

    decision_id = _answer_decision_id(goal.session_ref, goal.goal_version, text)
    decisions = read_decisions(decisions_path)
    decision = next((entry for entry in decisions if entry.decision_id == decision_id), None)
    if decision is None:
        asked = " ".join(ask_question_text(ask) for ask in goal.open_asks)
        basis = (
            f"The operator submitted one answer through the board for {len(goal.open_asks)} open ask(s)."
            if goal.open_asks
            else "The operator submitted a note through the board while reviewing the work session's status."
        )
        decision = append_decision(
            decisions_path,
            DecisionEntry(
                decision_id=decision_id,
                at=_utc_now(),
                kind="ask-retirement",
                decision="The operator answered this work session's open ask on the board.",
                basis=basis,
                citation="board-answer",
                authority="operator",
                session_ref=goal.session_ref,
                goal_version=goal.goal_version,
                goal_digest=goal_digest(goal),
                question=" ".join(asked.split()),
                answer=text,
            ),
        )

    # Serialize against a racing dispatchd claim so the asks retire and the
    # resume lands before the queue can observe the relay order as stale.
    lock_dir = root / "locks" if root is not None else None
    lock_path = (lock_dir or (state_dir() / "locks")) / f"operator-answer-{_safe_name(session_ref)}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(lock_path):
        # Retire exactly the asks the operator saw when answering, one by
        # one: an ask added after that snapshot was never shown on the
        # board, so claiming this answer covers it would fabricate a ruling.
        # A suppressed "not found" means a racing retirement already won --
        # the audit record it wrote stands and this retry just converges.
        for ask_text in goal.open_asks:
            with contextlib.suppress(ValueError):
                resolve_ask(
                    root,
                    session_ref,
                    ask=ask_text,
                    retired_by="operator",
                    basis=text,
                    citation=f"decision:{decision.decision_id}",
                    authority="operator",
                )
        # A question-kind residual is the same open item on the foreground
        # queue; close each with the operator's text as its recorded basis.
        for task in goal.foreground_tasks:
            if task.kind == "question":
                with contextlib.suppress(ValueError):
                    resolve_foreground_task(root, session_ref, task_id=task.task_id, basis=text)

        order_path = _enqueue_relay_order(resolved_queue, goal, text, decision)

        refreshed = get_goal(root, session_ref)
        lifecycle = get_lane_lifecycle(root, session_ref)
        if (
            refreshed is not None
            and refreshed.status == "held"
            and refreshed.hold_reason.startswith(QUESTION_HOLD_PREFIX)
            and not refreshed.open_asks
            and (lifecycle is None or lifecycle.state != "closed")
        ):
            with contextlib.suppress(ValueError):
                # A concurrent resume that landed first is the same outcome.
                resume_goal(root, session_ref)
            refreshed = get_goal(root, session_ref)
            lifecycle = get_lane_lifecycle(root, session_ref)
        # Only an unheld, unpaused lane gets its deferred backlog back. While
        # any hold still stands the parked orders stay parked -- requeuing
        # them now would bounce every non-relay order through the held gate
        # into a terminal rejection, and a paused lifecycle owns its own
        # resume path.
        if (
            refreshed is not None
            and refreshed.status != "held"
            and (lifecycle is None or lifecycle.state not in {"paused", "shelved"})
        ):
            requeue_deferred_for_session(resolved_queue, session_ref)

    stored = get_goal(root, session_ref)
    if stored is None:
        raise GoalNotFoundError(session_ref)
    logger.info(
        "operator_answer_recorded",
        session_ref=session_ref,
        decision_id=decision.decision_id,
        order_path=None if order_path is None else str(order_path),
        status=stored.status,
    )
    return stored


__all__ = ["QUESTION_HOLD_PREFIX", "QUESTION_MARKER", "ask_question_text", "record_operator_answer"]

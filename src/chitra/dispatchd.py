"""dispatchd — deterministic daemon that drains a JSON order queue and
delivers each order via ``chitra.dispatch.dispatch_to_tmux``, enforcing the
single-writer rule via ``LaneLock``.

The queue's filesystem state -- subdirectory layout, claim/ownership
markers, send nonces, and retry sidecars -- is typed and owned by
``chitra.queue_state``; this module composes those primitives into the
delivery policy below.

Queue layout (default ``queue_dir``, overridable per call/CLI):

    queue_dir/orders/*.json      -- DispatchOrder JSON, one file per order
    queue_dir/in_flight/*.json   -- an order file a worker has atomically
                                     claimed and is currently delivering
    queue_dir/deferred/*.json    -- an order parked because its session or
                                     lane is held, paused, or shelved, or
                                     because a lane lock timed out (see
                                     below); no terminal result file exists
                                     for it yet
    queue_dir/results/<id>.json  -- DispatchResult JSON; a SENT result exists
                                     only after delivery ledger proof verifies
    queue_dir/processed/*.json   -- the order file, moved here only after a
                                     terminal result, and for SENT only after
                                     a matching signed ledger entry

Crash-safety:

- **Idempotent redelivery.** Once a result file exists for an order id, that
  order is never redispatched -- ``process_one_order`` checks for an
  existing result file (both before and again immediately after acquiring
  the lane lock -- see "Lane-lock recheck" below). A SENT result is accepted
  during recovery only when an already-existing signed ledger row proves the
  exact delivery; recovery never creates proof from the result itself.
- **Atomic reservation and claim.** Before moving an order file from
  ``orders/`` into ``in_flight/``, dispatchd creates its owner marker with
  exclusive-create semantics. Two workers racing the same order can each
  reserve it only once; the loser sees the marker and skips it. The reserver
  then renames the order into ``in_flight/``. This closes the otherwise real
  race between rename and owner-marker creation. See docs/SOL-ADVERSARIAL-
  REVIEW finding #5.
- **Send-nonce crash reconciliation.** The one gap atomic claim + lane lock
  cannot close on their own: a worker that dies *after* the pane paste
  actually lands but *before* ``_write_result_atomic`` runs leaves an order
  in ``in_flight/`` with no result. A naive restart would redispatch it --
  a real duplicate paste into a live pane. Before calling
  ``dispatch_to_tmux``, this module writes a small nonce marker file next to
  the claimed order in ``in_flight/``. If a later pass finds that marker
  already present for an order with no result, it does not blindly resend:
  it reconciles by grepping the target session's own transcript for the
  order's nudge marker (the same transcript-grep primitive
  ``dispatch_to_tmux`` itself uses to confirm delivery) -- if the transcript
  confirms delivery already happened, a ``SENT`` result is synthesized with
  no second paste. If consumption is not yet confirmed, later passes keep
  verifying until proof appears or authoritative lane state resolves the order.

Guard freeze and deferral (opt-in via ``goals_root``): immediately
before any delivery attempt -- **under the lane lock**, not before it (see
"TOCTOU" below) -- ``process_one_order`` checks whether the order's
``session_ref`` currently has a ``chitra.goals`` record held for a rate-limit
or load-shed reason (using the sibling prefixes declared in ``chitra.goals``
and set by ``chitra.rate_limit_guard``). If so, the order is atomically parked in
``deferred/`` -- no pane I/O, no result file written, so it is neither
delivered nor discarded. ``chitra.rate_limit_guard.apply_resume`` calls
``requeue_deferred_for_session`` once the hold actually clears, which
atomically returns every deferred order for that session to ``orders/`` in
its original FIFO arrival order (renaming a file never changes its mtime,
so ``run_once``'s FIFO-by-mtime glob sort naturally preserves it) --
each is then delivered exactly once by the same crash-safe idempotency
check every other order already relies on.

Lane lifecycle gate (also opt-in through the same ``goals_root``): immediately
before any delivery attempt -- under the same lane lock -- dispatchd re-reads
the recovery record for the order's session.  A queued ordinary order cannot
paste into a ``paused`` or ``shelved`` lane; it is moved to ``deferred/`` with
no result so a later resume can requeue it.  A ``closed`` lane receives a
terminal ``BLOCKED`` result instead.  That result retains the order and reason
in the normal audit paths because a closed lane can never resume.  The one
paused exception is a distinctly typed
``native-control-pause-prune`` order, which may remove a stale Claude
recurring hook but may not deliver ordinary work.  No order, including that
exception, is delivered while a lane is shelved or closed.  A missing
lifecycle record preserves legacy, pre-lifecycle dispatch behavior; an
unreadable lifecycle record fails closed.

TOCTOU: the freeze check reads and acts under the SAME lane-lock hold used
for delivery, so a guard hold that lands after the check and before a
paste (the classic time-of-check/time-of-use race) cannot slip an ordinary
order into a newly-frozen lane -- there is no window between "checked" and
"pasted" for the hold to appear in. See docs/SOL-ADVERSARIAL-REVIEW finding #7.

``DispatchOrder.bypass_rate_limit_freeze`` exempts
``chitra.rate_limit_guard``'s own checkpoint/stop/re-arm nudges from this
freeze, since they are the pause/resume mechanism itself. Setting that
boolean is not, by itself, sufficient to bypass the freeze: dispatchd only
honors it when the order's ``task_type`` is also one of its own sealed
internal task types (``_RATE_LIMIT_GUARD_TASK_TYPES``) -- an arbitrary queue
writer cannot invent a new bypass merely by setting the field, because
dispatchd (not the order) owns the allowlist.

Lane-lock deferral: a ``LaneLock`` timeout is transient rather than a terminal
delivery rejection. The timeout count is atomically recorded in a sidecar
under ``deferred/`` before the claimed order is moved there, so every later
``run_once`` pass can return it to ``orders/`` after newly pending work. No
terminal result is persisted for a transient lock or unconfirmed delivery.
Dispatchd keeps retrying until the order is delivered or lane state resolves it.

No LLM calls in this module's own code path -- it delivers orders to LLM-
driven sessions, but the content/timing/target of every order is decided by
the caller before it reaches this module; this module is deterministic
plumbing only -- including the optional completion-claim audit
(``chitra.completion_gate``) run in ``process_one_order`` before delivery,
which is itself pure keyword/field matching, not reasoning. See
``docs/evasion-taxonomy.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from . import ledger as ledger_mod
from ._fsio import write_json_atomic
from .completion_gate import evaluate_completion_claim, is_completion_claim
from .decisions import read_decisions
from .dispatch import (
    DISPATCH_VERIFY_WAIT_SECONDS,
    DispatchTuning,
    LaneLock,
    LaneLockError,
    TmuxRunner,
    dispatch_to_tmux,
    nudge_confirmation_marker,
    transcript_confirms_nudge,
)
from .goals import (
    GOALS_SCHEMA_NEWER_MESSAGE,
    LOAD_SHED_HOLD_REASON_PREFIX,
    RATE_LIMIT_HOLD_REASON_PREFIX,
    GoalsSchemaNewerError,
    get_goal,
    goals_schema_newer_than_installed,
)
from .goals import (
    SCHEMA as GOALS_INSTALLED_SCHEMA,
)
from .journal import native_session_identity
from .orders import (
    NATIVE_CONTROL_PAUSE_PRUNE_TASK_TYPE,
    DispatchOrder,
    DispatchResult,
    DispatchStatus,
)
from .policy_config import PolicyConfig, load_policy_config
from .question_handler import handle_question
from .queue_state import (
    LaneLockRetryTracker,
    QueueLayout,
    QueueSubdir,
    StoredResult,
    TerminalFinalization,
    _move_without_replace,
    _require_real_directory,
    _validate_pending_order_path,
    read_owner_pid,
    reclaim_stale_claims,
    requeue_deferred_to_orders,
    reserve_claim,
)
from .recovery import get_lane_lifecycle
from .routing_config import RoutingConfig, load_routing_config, resolve_route, resolve_routing_hint
from .run_pool import RunPool
from .state_paths import default_attestation_ledger_path, default_ledger_key_path, default_ledger_path, default_queue_dir
from .supervision import goal_digest
from .transcript_bindings import DEFAULT_FILENAME, load_transcript_bindings

logger = structlog.get_logger(__name__)

DEFAULT_POLL_SECONDS = 1.0

# Roots whose newer-than-installed goals.json has already been journaled; a
# long-running daemon notices once instead of writing the same warning into
# the journal on every poll.
_SCHEMA_NOTICED_ROOTS: set[str] = set()


def note_goals_schema_state(goals_root: Path | None) -> None:
    """Journal one fail-closed notice when this store's file schema is newer.

    The daemon process keeps running instead of entering a supervisor restart
    loop, but every goal-bound order is blocked because this package cannot
    verify a newer writer's contract.
    """
    file_schema = goals_schema_newer_than_installed(goals_root)
    if file_schema is None:
        return
    key = str(goals_root) if goals_root is not None else "<default-state-dir>"
    if key in _SCHEMA_NOTICED_ROOTS:
        return
    _SCHEMA_NOTICED_ROOTS.add(key)
    print(f"{GOALS_SCHEMA_NEWER_MESSAGE} goals_root={key} file_schema={file_schema} installed_schema={GOALS_INSTALLED_SCHEMA}")


def _goal_contract_rejection(order: DispatchOrder, goals_root: Path | None) -> str | None:
    """Return a pre-delivery rejection for a stale or non-actionable order."""
    if order.goal_digest is None and order.goal_version is None:
        return None
    try:
        current_goal = get_goal(goals_root, order.session_ref)
    except GoalsSchemaNewerError:
        # A newer writer's store cannot be verified by this package.  A goal-
        # bound order must not be delivered on an unverifiable contract, and
        # this rejection must happen in both the pre-lock and under-lock
        # checks so the claimed file is finalized rather than wedged in
        # ``in_flight/``.
        note_goals_schema_state(goals_root)
        return "goals-schema-newer-than-installed"
    if (
        current_goal is None
        or order.goal_digest is None
        or order.goal_version is None
        or current_goal.goal_version != order.goal_version
        or goal_digest(current_goal) != order.goal_digest
    ):
        return "stale-goal-contract"
    if current_goal.status == "held":
        return "goal-held"
    if current_goal.status in {"done-pending-verification", "done-pending-close"}:
        return "goal-not-actionable"
    if order.message_kind == "goal_contract_answer":
        decisions_path = goals_root / "decisions.jsonl" if goals_root is not None else None
        expected_question_result = (
            handle_question(
                current_goal,
                order.question_result.question,
                decisions=read_decisions(decisions_path),
                occurrence=order.question_result.occurrence,
            )
            if order.question_result is not None
            else None
        )
        if (
            expected_question_result is None
            or expected_question_result != order.question_result
            or expected_question_result.disposition != "answered"
            or expected_question_result.answer != order.nudge
        ):
            return "invalid-goal-contract-answer"
    return None


class _ConfigNotPreloaded:
    """Sentinel that keeps ``None`` available as a real routing default."""


_CONFIG_NOT_PRELOADED = _ConfigNotPreloaded()


# Sealed allowlist: the only task_types dispatchd itself will honor a
# caller-set bypass_rate_limit_freeze=True for. Owned here, not by the
# order -- see this module's docstring.
_RATE_LIMIT_GUARD_TASK_TYPES = frozenset(
    {
        "rate-limit-checkpoint",
        "rate-limit-stop",
        "rate-limit-resume",
        "load-shed-checkpoint",
        "load-shed-stop",
        "load-shed-resume",
    }
)
_STRICT_AUTONOMOUS_TASK_TYPES = frozenset({"persistent-oversight"})
SESSION_ALLOW_PREFIXES_ENV_VAR = "CHITRA_ALLOWED_SESSION_PREFIXES"
SESSION_DENY_PREFIXES_ENV_VAR = "CHITRA_DENIED_SESSION_PREFIXES"


def _ensure_queue_dirs(queue_dir: Path) -> tuple[Path, Path, Path]:
    """Create the standard queue subdirectories via the typed layout seam."""
    return QueueLayout(queue_dir).create()


def resolve_session_prefixes(prefixes: Sequence[str] | None, *, env_var: str) -> tuple[str, ...]:
    """Resolve an optional CLI namespace policy or its comma-separated environment fallback."""
    values = prefixes if prefixes is not None else os.environ.get(env_var, "").split(",")
    resolved: list[str] = []
    for raw_prefix in values:
        prefix = raw_prefix.strip()
        if prefix and prefix not in resolved:
            resolved.append(prefix)
    return tuple(resolved)


def session_scope_violation(
    session_ref: str,
    *,
    allowed_session_prefixes: tuple[str, ...] = (),
    denied_session_prefixes: tuple[str, ...] = (),
) -> str | None:
    """Return a deterministic namespace-policy rejection, if one applies.

    Invalid ``session_ref`` values deliberately return ``None`` here so the
    established dispatch parser reports its normal malformed-reference error.
    """
    parts = session_ref.split(":")
    if len(parts) != 3:
        return None
    session_name = parts[1]
    denied = next((prefix for prefix in denied_session_prefixes if session_name.startswith(prefix)), None)
    if denied is not None:
        return f"session namespace denied by prefix {denied!r}"
    if allowed_session_prefixes and not any(session_name.startswith(prefix) for prefix in allowed_session_prefixes):
        return "session namespace is not owned by this dispatcher"
    return None


def _write_result_atomic(
    results_dir: Path,
    result: DispatchResult,
    *,
    overwrite: bool = False,
) -> Path:
    """Publish one result without clobbering a prior writer by default.

    The first durable result is the queue's idempotency record. Replacement
    is reserved for recovery after dispatchd validates an old SENT result
    against an already-existing signed ledger row. New SENT results are
    always ledger-proven before their first write.
    """
    stored = StoredResult(order_id=result.order_id, path=results_dir / f"{result.order_id}.json")
    payload = result.model_dump(mode="json")
    if overwrite:
        stored.overwrite(payload)
    elif not stored.create_once(payload):
        raise FileExistsError(stored.path)
    return stored.path


def _finalize_claimed_order(
    claimed_path: Path,
    *,
    results_dir: Path,
    destination_dir: Path,
    result: DispatchResult,
    suppress_move_errors: bool = False,
    retry_state_dir: Path | None = None,
    retry_order_id: str | None = None,
) -> DispatchResult:
    """Persist one terminal result and move its claimed order exactly once.

    Thin composition over ``chitra.queue_state.TerminalFinalization``, which
    owns the single-writer semantics: a result that already exists is never
    overwritten (the first writer's record stands), the order-file move
    completes exactly once however often the transition runs, and the stale
    control markers (retry sidecar, send nonce) are cleared missing-safe.
    """
    order_key = retry_order_id or claimed_path.stem
    TerminalFinalization(
        claimed_path=claimed_path,
        order_id=order_key,
        results_dir=results_dir,
        destination_dir=destination_dir,
        result_payload=result.model_dump(mode="json"),
        retry_state_dir=retry_state_dir,
        # ``QueueLayout`` is rooted at the queue directory, while a claimed
        # path is rooted one level below it in ``in_flight/``.
        nonce_path=QueueLayout(claimed_path.parent.parent).send_nonce_path(order_key),
        suppress_move_errors=suppress_move_errors,
    ).apply()
    _clear_lifecycle_deferred_marker(retry_state_dir or claimed_path.parent.parent / "deferred", order_key)
    return result


def _ensure_delivery_ledger(
    order: DispatchOrder,
    result: DispatchResult,
    *,
    ledger_path: Path | None,
    ledger_key_path: Path | None,
    expected_transcript_path: Path | None = None,
    require_native_session_id: bool = False,
) -> ledger_mod.LedgerEntry:
    """Return signed proof for a SENT order, appending it when needed.

    The order id is part of the lookup. Matching only a session and message
    would incorrectly accept an older identical nudge as proof for this
    order. The post-append lookup also catches a short write or a writer that
    returned without making the proof durable.

    When the confirmed result names a lane transcript, its adapter-native
    session identity is normalized with the journal's own
    normalizers and bound into the signed row (signature version 5). The
    value never comes from ``routing_hint``, which stays opaque audit
    metadata. A transcript that yields no native identity
    still gets a valid v4 row for legacy orders; strict autonomous orders
    fail closed instead of trusting an unbound session.
    """
    resolved_ledger_path = ledger_path or default_ledger_path()
    resolved_key_path = ledger_key_path or (ledger_path.with_name("ledger.key") if ledger_path is not None else default_ledger_key_path())
    key = ledger_mod.load_or_create_signing_key(resolved_key_path)
    expected_native_session_id: str | None = None
    if require_native_session_id:
        if expected_transcript_path is None:
            raise OSError(f"strict delivery has no exact bound transcript for order {order.order_id}")
        expected_native_session_id = native_session_identity(expected_transcript_path)
        if not expected_native_session_id:
            raise OSError(f"strict bound transcript has no native session identity for order {order.order_id}")
    existing = ledger_mod.verify_delivery(
        resolved_ledger_path,
        key=key,
        order_id=order.order_id,
        session_ref=order.session_ref,
        nudge=order.nudge,
    )
    if existing is not None:
        if require_native_session_id and existing.native_session_id != expected_native_session_id:
            raise OSError(f"strict delivery ledger proof names another native session for order {order.order_id}")
        return existing

    if require_native_session_id:
        if expected_transcript_path is None or not result.transcript_path:
            raise OSError(f"strict SENT result has no exact bound transcript for order {order.order_id}")
        try:
            expected_path = expected_transcript_path.expanduser().resolve()
            result_path = Path(result.transcript_path).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise OSError(f"strict SENT result transcript path cannot be resolved for order {order.order_id}") from exc
        if result_path != expected_path:
            raise OSError(f"strict SENT result transcript path is not the bound path for order {order.order_id}")
        result.native_session_id = expected_native_session_id
    elif not result.native_session_id and result.transcript_path:
        result.native_session_id = native_session_identity(Path(result.transcript_path))
    ledger_mod.append_entry(
        resolved_ledger_path,
        order_id=order.order_id,
        session_ref=order.session_ref,
        tag=order.tag,
        routing_hint=result.routing_hint,
        task_type=order.task_type,
        resolved_zdr=result.resolved_zdr,
        nudge=order.nudge,
        key=key,
        native_session_id=result.native_session_id,
    )
    verified = ledger_mod.verify_delivery(
        resolved_ledger_path,
        key=key,
        order_id=order.order_id,
        session_ref=order.session_ref,
        nudge=order.nudge,
    )
    if verified is None:
        raise OSError(f"delivery ledger append did not produce proof for order {order.order_id}")
    if require_native_session_id and verified.native_session_id != expected_native_session_id:
        raise OSError(f"strict delivery ledger proof names another native session for order {order.order_id}")
    return verified


def _verify_existing_delivery_ledger(
    order: DispatchOrder,
    *,
    ledger_path: Path | None,
    ledger_key_path: Path | None,
) -> ledger_mod.LedgerEntry | None:
    """Verify an already-written delivery row without appending one."""
    resolved_ledger_path = ledger_path or default_ledger_path()
    resolved_key_path = ledger_key_path or (ledger_path.with_name("ledger.key") if ledger_path is not None else default_ledger_key_path())
    key = ledger_mod.load_or_create_signing_key(resolved_key_path)
    return ledger_mod.verify_delivery(
        resolved_ledger_path,
        key=key,
        order_id=order.order_id,
        session_ref=order.session_ref,
        nudge=order.nudge,
    )


def _strict_autonomous_order(order: DispatchOrder) -> bool:
    """Return whether this order requires an exact transcript binding."""
    return order.task_type in _STRICT_AUTONOMOUS_TASK_TYPES or order.message_kind == "goal_contract_answer"


_LANE_LIFECYCLE_STATES = frozenset({"active", "paused", "shelved", "closed"})
_LANE_LIFECYCLE_DEFER_STATES = frozenset({"paused", "shelved"})


def _defer_lifecycle_order(
    claimed_path: Path,
    *,
    deferred_dir: Path,
    retry_tracker: LaneLockRetryTracker,
    order: DispatchOrder,
    state: str,
    resolved_zdr: bool,
    attestation_id: str | None,
) -> DispatchResult:
    """Park an order while a reversible lane lifecycle state is inactive.

    Lifecycle deferrals intentionally do not receive a retry sidecar.  The
    ordinary retry requeue pass must not churn them back into ``orders/`` while
    the lane is paused or shelved.  The lifecycle resume path calls
    ``requeue_deferred_for_session`` once the lane is active again, and the
    original order then goes through the same idempotency and lock checks.
    """
    deferred_dir.mkdir(parents=True, exist_ok=True)
    retry_tracker.clear(order.order_id)
    _mark_lifecycle_deferred(deferred_dir, order.order_id)
    try:
        _move_without_replace(claimed_path, deferred_dir / claimed_path.name)
    except OSError as exc:
        # Keep the claim recoverable if the filesystem move races another
        # worker or fails.  The owner marker is released by the outer caller;
        # a later pass can reclaim the in-flight order without losing it.
        logger.error(
            "dispatchd_lane_lifecycle_defer_failed",
            order_id=order.order_id,
            session_ref=order.session_ref,
            state=state,
            error=str(exc),
        )
    logger.info(
        "dispatchd_order_deferred_lane_lifecycle",
        order_id=order.order_id,
        session_ref=order.session_ref,
        state=state,
    )
    return DispatchResult(
        order_id=order.order_id,
        session_ref=order.session_ref,
        status=DispatchStatus.DEFERRED,
        reason=f"lane-lifecycle-{state}-deferred",
        routing_hint=order.routing_hint,
        task_type=order.task_type,
        resolved_zdr=resolved_zdr,
        decision_attestation_id=attestation_id,
    )


def _defer_held_goal_order(
    claimed_path: Path,
    *,
    deferred_dir: Path,
    retry_tracker: LaneLockRetryTracker,
    order: DispatchOrder,
    resolved_zdr: bool,
    attestation_id: str | None,
) -> DispatchResult:
    """Park a goal-bound order whose lane is still held, without killing it.

    An ``operator_relay`` answer is recorded before the hold it resolves is
    released, so it routinely reaches the queue while the goal still reads
    ``held``. A terminal ``BLOCKED`` would lose the answer entirely. The
    order carries no lifecycle marker and no retry sidecar: only
    ``requeue_deferred_for_session`` -- called by the answer/resume path and
    by ``chitra-goals resume`` -- returns it to ``orders/``.
    """
    deferred_dir.mkdir(parents=True, exist_ok=True)
    retry_tracker.clear(order.order_id)
    try:
        _move_without_replace(claimed_path, deferred_dir / claimed_path.name)
    except OSError as exc:
        # Same recoverable shape as the lane-lock deferral: the claim is
        # released by the outer caller and the next pass reclaims it.
        logger.error(
            "dispatchd_goal_held_defer_failed",
            order_id=order.order_id,
            session_ref=order.session_ref,
            error=str(exc),
        )
    logger.info(
        "dispatchd_order_deferred_goal_held",
        order_id=order.order_id,
        session_ref=order.session_ref,
    )
    return DispatchResult(
        order_id=order.order_id,
        session_ref=order.session_ref,
        status=DispatchStatus.DEFERRED,
        reason="goal-held-deferred: bound order waits for the lane's hold to clear",
        routing_hint=order.routing_hint,
        task_type=order.task_type,
        resolved_zdr=resolved_zdr,
        decision_attestation_id=attestation_id,
    )


def _lifecycle_deferred_marker(deferred_dir: Path, order_id: str) -> Path:
    """Return the private marker that distinguishes lifecycle deferrals."""
    return deferred_dir / f".{order_id}.lifecycle"


def _mark_lifecycle_deferred(deferred_dir: Path, order_id: str) -> None:
    """Record that a deferred order is waiting for a lifecycle transition."""
    marker = _lifecycle_deferred_marker(deferred_dir, order_id)
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    except OSError as exc:
        logger.error("dispatchd_lane_lifecycle_marker_failed", order_id=order_id, path=str(marker), error=str(exc))
        return
    os.close(fd)


def _clear_lifecycle_deferred_marker(deferred_dir: Path, order_id: str) -> None:
    """Remove a lifecycle marker after its order leaves deferred storage."""
    with contextlib.suppress(OSError):
        _lifecycle_deferred_marker(deferred_dir, order_id).unlink()


def _requeue_lifecycle_deferred(
    queue_dir: Path,
    orders_dir: Path,
    *,
    goals_root: Path | None,
) -> list[Path]:
    """Return lifecycle deferrals when their lane can be reconciled.

    ``active`` lanes can resume their ordinary backlog.  ``closed`` lanes are
    also returned so the normal under-lock gate can publish a terminal
    ``BLOCKED`` audit result; they can never be delivered.  ``paused`` and
    ``shelved`` orders stay in ``deferred/``.  The marker keeps this pass from
    disturbing unrelated rate-limit or load-shed deferrals.
    """
    deferred_dir = QueueLayout(queue_dir).deferred
    if not deferred_dir.is_dir():
        return []
    requeued: list[Path] = []
    dated: list[tuple[int, int, Path]] = []
    for path in deferred_dir.glob("*.json"):
        if not _lifecycle_deferred_marker(deferred_dir, path.stem).exists():
            continue
        try:
            stat = path.stat()
            dated.append((stat.st_mtime_ns, stat.st_ino, path))
        except FileNotFoundError:
            continue
    for _, _, path in sorted(dated, key=lambda item: item[:2]):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("dispatchd_lifecycle_deferred_unreadable", path=str(path), error=str(exc))
            continue
        session_ref = payload.get("session_ref") if isinstance(payload, dict) else None
        if not isinstance(session_ref, str) or not session_ref:
            logger.error("dispatchd_lifecycle_deferred_missing_session", path=str(path))
            continue
        try:
            lifecycle = get_lane_lifecycle(goals_root, session_ref)
        except (OSError, ValueError) as exc:
            logger.error(
                "dispatchd_lane_lifecycle_unavailable",
                session_ref=session_ref,
                path=str(path),
                error=str(exc),
            )
            continue
        if lifecycle is None or lifecycle.state not in {"active", "closed"}:
            continue
        try:
            _move_without_replace(path, orders_dir / path.name)
        except OSError as exc:
            logger.error(
                "dispatchd_lane_lifecycle_requeue_failed",
                session_ref=session_ref,
                path=str(path),
                error=str(exc),
            )
            continue
        _clear_lifecycle_deferred_marker(deferred_dir, path.stem)
        requeued.append(orders_dir / path.name)
        logger.info(
            "dispatchd_lane_lifecycle_requeued",
            order_id=path.stem,
            session_ref=session_ref,
            state=lifecycle.state,
        )
    return requeued


def _load_transcript_binding_paths(
    bindings_path: Path | None,
    *,
    transcript_root: Path | None,
    default_path: Path,
) -> dict[str, Path]:
    """Load one v1 binding manifest and resolve its paths for this pass."""
    manifest_path = bindings_path or (transcript_root / DEFAULT_FILENAME if transcript_root is not None else default_path)
    bindings = load_transcript_bindings(manifest_path, transcript_root=transcript_root)
    return {
        binding.session_ref: binding.resolved_path(manifest_path=manifest_path, transcript_root=transcript_root)
        for binding in bindings
    }


def _complete_existing_result(
    claimed_path: Path,
    existing_result_path: Path,
    *,
    order: DispatchOrder,
    results_dir: Path,
    processed_dir: Path,
    deferred_dir: Path,
    ledger_path: Path | None,
    ledger_key_path: Path | None,
    transcript_binding_path: Path | None,
    strict_autonomous: bool,
) -> None:
    """Recover a claimed order whose result was written by an earlier pass.

    A result file is not itself a queue acknowledgment. SENT results from the
    old behavior can exist without ledger proof. Such a result remains
    claimed until an already-existing signed ledger row proves this exact
    order; recovery never creates a row from the result and never calls the
    pane transport again.
    """
    if existing_result_path.is_symlink():
        raise ValueError(f"result path must not be a symlink: {existing_result_path}")
    try:
        stored_result = DispatchResult.model_validate_json(existing_result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("dispatchd_existing_result_unreadable", order_id=order.order_id, error=str(exc))
        return

    if stored_result.order_id != order.order_id or stored_result.session_ref != order.session_ref:
        logger.error(
            "dispatchd_existing_result_mismatch",
            order_id=order.order_id,
            result_order_id=stored_result.order_id,
            result_session_ref=stored_result.session_ref,
            order_session_ref=order.session_ref,
        )
        return

    if stored_result.status == DispatchStatus.SENT:
        try:
            existing_ledger = _verify_existing_delivery_ledger(
                order,
                ledger_path=ledger_path,
                ledger_key_path=ledger_key_path,
            )
            if existing_ledger is None:
                logger.error(
                    "dispatchd_existing_sent_result_without_ledger_proof",
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                )
                return
            if strict_autonomous:
                bound_native_id = (
                    native_session_identity(transcript_binding_path)
                    if transcript_binding_path is not None
                    else None
                )
                try:
                    stored_transcript_path = (
                        Path(stored_result.transcript_path).expanduser().resolve()
                        if stored_result.transcript_path
                        else None
                    )
                    bound_transcript_path = (
                        transcript_binding_path.expanduser().resolve()
                        if transcript_binding_path is not None
                        else None
                    )
                except (OSError, RuntimeError):
                    stored_transcript_path = None
                    bound_transcript_path = None
                if (
                    not bound_native_id
                    or existing_ledger.native_session_id != bound_native_id
                    or stored_transcript_path is None
                    or stored_transcript_path != bound_transcript_path
                ):
                    logger.error(
                        "dispatchd_existing_strict_result_without_exact_transcript_proof",
                        order_id=order.order_id,
                        session_ref=order.session_ref,
                    )
                    return
            stored_result.native_session_id = existing_ledger.native_session_id
            stored_result.delivery_ledger_verified = True
            _write_result_atomic(results_dir, stored_result, overwrite=True)
        except Exception as exc:  # noqa: BLE001 -- retry on the next daemon pass
            logger.error(
                "dispatchd_delivery_ledger_pending",
                order_id=order.order_id,
                session_ref=order.session_ref,
                error=str(exc),
            )
            return

    # The result is durable (and, for SENT, ledger-proven above); only the
    # acknowledgment remains. The finalization moves the order exactly once,
    # however many passes repeat this recovery, and clears the stale control
    # markers missing-safe.
    TerminalFinalization(
        claimed_path=claimed_path,
        order_id=order.order_id,
        results_dir=results_dir,
        destination_dir=processed_dir,
        result_payload=None,
        retry_state_dir=deferred_dir,
        nonce_path=QueueLayout(claimed_path.parent.parent).send_nonce_path(order.order_id),
    ).apply()


def _reclaim_stale_in_flight(queue_dir: Path) -> None:
    """Return an orphaned ``in_flight/`` order to ``orders/`` for reclaiming.

    Delegates to :func:`chitra.queue_state.reclaim_stale_claims`: a claim
    whose owner pid is no longer alive was abandoned by a crashed worker and
    is safe to return to ``orders/`` for a fresh claim, while a claim whose
    owner is still alive is never touched. Called at the top of every
    ``run_once`` pass so a crash between claiming an order and writing its
    result is always eventually retried, never stranded. See
    docs/SOL-ADVERSARIAL-REVIEW findings #2 and #5.
    """
    reclaim_stale_claims(QueueLayout(queue_dir))


def requeue_deferred_for_session(queue_dir: Path, session_ref: str) -> list[str]:
    """Atomically return one session's deferred backlog to ``orders/`` FIFO.

    Called once a rate-limit hold on ``session_ref`` actually clears (see
    ``chitra.rate_limit_guard.apply_resume``). A deferred order has no
    result file (see ``process_one_order``'s freeze/defer branch), so moving
    it back to ``orders/`` lets the ordinary crash-safe idempotency check
    deliver it exactly once. Returns the requeued order ids in the order
    they are requeued (their original arrival order, oldest first).
    """
    orders_dir, _, _ = _ensure_queue_dirs(queue_dir)
    deferred_dir = QueueLayout(queue_dir).deferred

    def held_for_session(path: Path) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("session_ref") == session_ref

    outcome = requeue_deferred_to_orders(deferred_dir, orders_dir, eligible=held_for_session)
    for path in outcome.skipped_existing_target:
        logger.error("dispatchd_deferred_requeue_target_exists", source=str(path), target=str(orders_dir / path.name))
    for path in outcome.failed:
        logger.warning("dispatchd_deferred_requeue_failed", session_ref=session_ref, path=str(path))
    for path in outcome.requeued:
        _clear_lifecycle_deferred_marker(deferred_dir, path.stem)
    requeued = [path.stem for path in outcome.requeued]
    if requeued:
        logger.info("dispatchd_deferred_requeued", session_ref=session_ref, order_ids=requeued)
    return requeued


def _requeue_lane_lock_deferred(queue_dir: Path, orders_dir: Path) -> list[Path]:
    """Atomically return retryable lane-lock deferrals after current pending work.

    Rate-limit and load-shed deferrals intentionally have no retry sidecar;
    they remain parked until ``requeue_deferred_for_session`` is called after
    the hold clears. A lane-lock timeout first writes its sidecar and only
    then moves the order, so a crash at either point leaves a recoverable
    order plus an accurate retry count. Sidecars are not part of the move:
    each requeued order keeps its durable attempt count for its next attempt.
    """
    deferred_dir = QueueLayout(queue_dir).deferred
    retry_tracker = LaneLockRetryTracker(deferred_dir)
    outcome = requeue_deferred_to_orders(
        deferred_dir,
        orders_dir,
        eligible=lambda path: retry_tracker.state_path(path.stem).exists(),
    )
    for path in outcome.skipped_existing_target:
        logger.error("dispatchd_lane_lock_deferred_target_exists", source=str(path), target=str(orders_dir / path.name))
    for path in outcome.failed:
        logger.error("dispatchd_lane_lock_deferred_requeue_failed", path=str(path))
    if outcome.requeued:
        logger.info("dispatchd_lane_lock_deferred_requeued", order_ids=[path.stem for path in outcome.requeued])
    return outcome.requeued


_DELIVERY_RUN_SCHEMA = "chitra.delivery-run.v1"
_DELIVERY_RUN_MAX_ATTEMPTS = 3

_DELIVERY_WAKE = threading.Event()


def _wake_dispatch() -> None:
    """Wake the dispatch loop when a delivery worker finishes."""
    _DELIVERY_WAKE.set()


# One bounded pool carries the lane-lock wait, the tmux paste, and the
# up-to-15s transcript-confirmation poll off the dispatch pass. Keys are
# per-lane so a second order for one session queues behind the running
# delivery instead of contending for its lock on a second worker.
_DELIVERY_POOL = RunPool(
    max_workers=4,
    thread_name_prefix="chitra-delivery-run",
    on_complete=_wake_dispatch,
)


class _ClaimHeld:
    """Sentinel: the order stays claimed while its delivery worker runs."""


_CLAIM_HELD = _ClaimHeld()


@dataclass(frozen=True)
class _DeliveryCall:
    """Everything one claimed order's delivery stage needs, pass-invariant."""

    claimed_path: Path
    order: DispatchOrder
    layout: QueueLayout
    results_dir: Path
    deferred_dir: Path
    effective_lock_dir: Path | None
    goals_root: Path | None
    tuning: DispatchTuning
    policy: PolicyConfig
    dispatch_runner: TmuxRunner | None
    projects_root: Path | None
    transcript_binding_path: Path | None
    local_extra: set[str] | None
    tmux_socket: Path | None
    ledger_path: Path | None
    ledger_key_path: Path | None
    strict_autonomous: bool
    resolved_zdr: bool
    attestation_id: str | None


@dataclass(frozen=True)
class _DeliveryOutcome:
    """What the worker's delivery stage decided, as a durable record.

    ``action`` names the transition the drain applies on a later pass:
    ``result`` publishes/finalizes (or defers an UNCONFIRMED result),
    ``existing-result`` finishes an earlier pass's already-stored result,
    ``defer-lifecycle`` parks under the lane's lifecycle state,
    ``defer-hold`` parks under a rate/load guard hold, ``defer-goal-held``
    parks an ``operator_relay`` answer until the lane's hold clears,
    ``lock-timeout`` parks with a retry sidecar, and ``ledger-pending``
    releases the claim so a later pass retries while the nonce keeps it
    verify-only.
    """

    order_id: str
    action: str
    result: DispatchResult | None = None
    defer_state: str | None = None
    detail: str | None = None
    resolved_zdr: bool = False
    attestation_id: str | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "schema": _DELIVERY_RUN_SCHEMA,
            "order_id": self.order_id,
            "action": self.action,
            "result": self.result.model_dump(mode="json") if self.result is not None else None,
            "defer_state": self.defer_state,
            "detail": self.detail,
            "resolved_zdr": self.resolved_zdr,
            "attestation_id": self.attestation_id,
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    @classmethod
    def from_record(cls, raw: object) -> _DeliveryOutcome | None:
        if not isinstance(raw, dict) or raw.get("schema") != _DELIVERY_RUN_SCHEMA:
            return None
        order_id = raw.get("order_id")
        action = raw.get("action")
        if not isinstance(order_id, str) or not isinstance(action, str):
            return None
        if action not in {
            "result",
            "existing-result",
            "defer-lifecycle",
            "defer-hold",
            "defer-goal-held",
            "lock-timeout",
            "ledger-pending",
        }:
            return None
        result_raw = raw.get("result")
        result: DispatchResult | None = None
        if result_raw is not None:
            try:
                result = DispatchResult.model_validate(result_raw)
            except ValueError:
                return None
        defer_state = raw.get("defer_state")
        detail = raw.get("detail")
        attestation_id = raw.get("attestation_id")
        return cls(
            order_id=order_id,
            action=action,
            result=result,
            defer_state=defer_state if isinstance(defer_state, str) else None,
            detail=detail if isinstance(detail, str) else None,
            resolved_zdr=raw.get("resolved_zdr") is True,
            attestation_id=attestation_id if isinstance(attestation_id, str) else None,
        )


def _delivery_runs_dir(queue_dir: Path) -> Path:
    """Return the queue's durable delivery-record directory."""
    return queue_dir / "delivery-runs"


def _delivery_record_path(queue_dir: Path, order_id: str) -> Path:
    return _delivery_runs_dir(queue_dir) / f"{order_id}.json"


def _compute_delivery_outcome(call: _DeliveryCall) -> _DeliveryOutcome:
    """Run the slow delivery stage and return the outcome, never applying it.

    Everything under the lane lock — the tmux paste, the up-to-15s
    transcript-confirmation poll, the nonce crash reconciliation, the
    lifecycle and guard-hold rechecks — executes here, on a worker when the
    caller deferred delivery. The only mutation this performs is the send
    nonce mint (part of delivery itself) and the delivery-ledger append for
    a SENT result, which must precede the result's first publication.
    """
    order = call.order
    lock = LaneLock(order.session_ref, lock_dir=call.effective_lock_dir)
    try:
        lock.acquire(blocking=True, timeout_seconds=call.tuning.lane_lock_timeout_seconds)
    except LaneLockError as exc:
        return _DeliveryOutcome(order_id=order.order_id, action="lock-timeout", detail=str(exc))

    try:
        # Lane-lock recheck: a concurrent order for the same session could
        # have completed and written a result while this order waited on
        # the lock. See docs/SOL-ADVERSARIAL-REVIEW finding #5.
        if (call.results_dir / f"{order.order_id}.json").exists():
            logger.info("dispatchd_order_already_processed_under_lock", order_id=order.order_id)
            return _DeliveryOutcome(order_id=order.order_id, action="existing-result")

        # Recheck the exact goal contract while holding the same lane lock
        # used for delivery. Completion, hold, redirect, or question-answer
        # changes that land after the queue claim cannot race a stale paste.
        goal_contract_rejection = _goal_contract_rejection(order, call.goals_root)
        if goal_contract_rejection == "goal-held" and order.message_kind == "operator_relay":
            return _DeliveryOutcome(
                order_id=order.order_id,
                action="defer-goal-held",
                resolved_zdr=call.resolved_zdr,
                attestation_id=call.attestation_id,
            )
        if goal_contract_rejection is not None:
            return _DeliveryOutcome(
                order_id=order.order_id,
                action="result",
                result=DispatchResult(
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                    status=DispatchStatus.BLOCKED,
                    reason=goal_contract_rejection,
                    routing_hint=order.routing_hint,
                    task_type=order.task_type,
                    resolved_zdr=call.resolved_zdr,
                    decision_attestation_id=call.attestation_id,
                ),
            )

        # Lifecycle recheck: the order may have been queued while the lane
        # was active and then waited in the queue until a pause, shelving, or
        # close transition completed.  Read the recovery record only after
        # acquiring the existing lane lock so this decision is adjacent to
        # the eventual pane write.  A missing record preserves legacy lanes;
        # a malformed/unreadable record fails closed rather than guessing that
        # a lane is active.
        try:
            lifecycle = get_lane_lifecycle(call.goals_root, order.session_ref)
        except (OSError, ValueError) as exc:
            logger.error(
                "dispatchd_lane_lifecycle_unavailable",
                order_id=order.order_id,
                session_ref=order.session_ref,
                error=str(exc),
            )
            return _DeliveryOutcome(
                order_id=order.order_id,
                action="result",
                result=DispatchResult(
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                    status=DispatchStatus.BLOCKED,
                    reason=f"lane-lifecycle-unavailable: {exc}",
                    routing_hint=order.routing_hint,
                    task_type=order.task_type,
                    resolved_zdr=call.resolved_zdr,
                    decision_attestation_id=call.attestation_id,
                ),
            )

        if lifecycle is not None:
            lifecycle_state = lifecycle.state
            if lifecycle_state not in _LANE_LIFECYCLE_STATES:
                logger.error(
                    "dispatchd_lane_lifecycle_unknown",
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                    state=lifecycle_state,
                )
                return _DeliveryOutcome(
                    order_id=order.order_id,
                    action="result",
                    result=DispatchResult(
                        order_id=order.order_id,
                        session_ref=order.session_ref,
                        status=DispatchStatus.BLOCKED,
                        reason=f"lane-lifecycle-unknown: {lifecycle_state}",
                        routing_hint=order.routing_hint,
                        task_type=order.task_type,
                        resolved_zdr=call.resolved_zdr,
                        decision_attestation_id=call.attestation_id,
                    ),
                )

            # The prune control is narrowly allowed only for a paused lane. A
            # shelved lane is offline, so even cleanup controls stay deferred
            # until the lane is resumed.
            if lifecycle_state in _LANE_LIFECYCLE_DEFER_STATES and not (
                lifecycle_state == "paused" and order.task_type == NATIVE_CONTROL_PAUSE_PRUNE_TASK_TYPE
            ):
                logger.info(
                    "dispatchd_order_deferred_lane_lifecycle",
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                    state=lifecycle_state,
                )
                return _DeliveryOutcome(
                    order_id=order.order_id,
                    action="defer-lifecycle",
                    defer_state=lifecycle_state,
                )

            if lifecycle_state == "closed":
                # Closed is terminal by contract.  Preserve the order in the
                # processed queue and publish a BLOCKED result so the audit
                # records why it was never pasted.  In particular, the
                # paused prune exception is not valid once a lane is closed.
                return _DeliveryOutcome(
                    order_id=order.order_id,
                    action="result",
                    result=DispatchResult(
                        order_id=order.order_id,
                        session_ref=order.session_ref,
                        status=DispatchStatus.BLOCKED,
                        reason="lane-lifecycle-closed",
                        routing_hint=order.routing_hint,
                        task_type=order.task_type,
                        resolved_zdr=call.resolved_zdr,
                        decision_attestation_id=call.attestation_id,
                    ),
                )

        if call.strict_autonomous and call.transcript_binding_path is None:
            return _DeliveryOutcome(
                order_id=order.order_id,
                action="result",
                result=DispatchResult(
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                    status=DispatchStatus.BLOCKED,
                    reason="missing-transcript-binding",
                    routing_hint=order.routing_hint,
                    task_type=order.task_type,
                    resolved_zdr=call.resolved_zdr,
                    decision_attestation_id=call.attestation_id,
                ),
            )

        # Rate-limit freeze/defer check, UNDER the lane lock (TOCTOU fix --
        # see this module's docstring). bypass_rate_limit_freeze only takes
        # effect for dispatchd's own sealed internal task types.
        allowed_bypass = order.bypass_rate_limit_freeze and order.task_type in _RATE_LIMIT_GUARD_TASK_TYPES
        held = None
        if not allowed_bypass:
            try:
                held = get_goal(call.goals_root, order.session_ref)
            except GoalsSchemaNewerError:
                # Read-only degradation: the store refuses writes to this
                # package, so run without goal-informed freeze decisions and
                # keep draining the queue rather than exiting.
                note_goals_schema_state(call.goals_root)
        if (
            held is not None
            and held.status == "held"
            and held.hold_reason.startswith((RATE_LIMIT_HOLD_REASON_PREFIX, LOAD_SHED_HOLD_REASON_PREFIX))
        ):
            logger.info(
                "dispatchd_order_deferred_rate_limit_freeze",
                order_id=order.order_id,
                session_ref=order.session_ref,
                hold_reason=held.hold_reason,
                resume_at=held.resume_at,
            )
            return _DeliveryOutcome(
                order_id=order.order_id,
                action="defer-hold",
                result=DispatchResult(
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                    status=DispatchStatus.DEFERRED,
                    reason=(
                        f"load-shed-deferred: {held.hold_reason}"
                        if held.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX)
                        else f"rate-limit-deferred: {held.hold_reason} (resume_at={held.resume_at})"
                    ),
                    routing_hint=order.routing_hint,
                    task_type=order.task_type,
                    resolved_zdr=call.resolved_zdr,
                ),
            )

        # Send-nonce crash reconciliation: a marker already present here
        # means a PRIOR attempt got at least as far as (about to) paste before
        # this process/run restarted. Reconcile against the target transcript.
        # The nonce makes this a verify-only state: never paste again.
        nonce = call.layout.send_nonce(order.order_id)
        if nonce.exists():
            logger.warning("dispatchd_order_reconciling_after_possible_crash", order_id=order.order_id, session_ref=order.session_ref)
            parts = order.session_ref.split(":")
            host = parts[0] if len(parts) == 3 else ""
            confirmed, transcript_path = transcript_confirms_nudge(
                order.nudge,
                host=host,
                projects_root=call.projects_root,
                expected_transcript_path=call.transcript_binding_path,
                recency_seconds=call.tuning.transcript_recency_seconds,
                runner=call.dispatch_runner,
                local_extra=call.local_extra,
            )
            if confirmed:
                result = DispatchResult(
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                    status=DispatchStatus.SENT,
                    reason="sent: existing nonce reconciled from lane-bound consumption proof",
                    marker=nudge_confirmation_marker(order.nudge),
                    transcript_path=str(transcript_path) if transcript_path is not None else None,
                )
            else:
                # A nonce means an earlier attempt may already have touched
                # the pane. Verification and paste are separate states: an
                # unconsumed nonce is retried by verification only, never by
                # injecting the same text again.
                result = DispatchResult(
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                    status=DispatchStatus.DELIVERY_UNCONFIRMED,
                    reason="delivery-unconfirmed: existing nonce has no lane-bound consumption proof",
                    marker=nudge_confirmation_marker(order.nudge),
                )
        else:
            nonce.mint()
            result = dispatch_to_tmux(
                order,
                policy=call.policy,
                tuning=call.tuning,
                runner=call.dispatch_runner,
                projects_root=call.projects_root,
                expected_transcript_path=call.transcript_binding_path,
                local_extra=call.local_extra,
                tmux_socket=call.tmux_socket,
            )
    finally:
        lock.release()

    result.task_type = order.task_type
    result.routing_hint = order.routing_hint
    result.resolved_zdr = call.resolved_zdr
    result.decision_attestation_id = call.attestation_id
    logger.info(
        "dispatchd_order_processed",
        order_id=order.order_id,
        session_ref=order.session_ref,
        status=result.status.value,
    )
    if result.status == DispatchStatus.SENT:
        # Sign and verify the delivery before the record — and therefore the
        # result — can be published. If the ledger is unavailable, the claim
        # and nonce stay recoverable and no untrusted SENT result exists.
        try:
            _ensure_delivery_ledger(
                order,
                result,
                ledger_path=call.ledger_path,
                ledger_key_path=call.ledger_key_path,
                expected_transcript_path=call.transcript_binding_path,
                require_native_session_id=call.strict_autonomous,
            )
            result.delivery_ledger_verified = True
        except Exception as exc:  # noqa: BLE001 -- keep the order pending for the next pass
            logger.error(
                "dispatchd_delivery_ledger_pending",
                order_id=order.order_id,
                session_ref=order.session_ref,
                error=str(exc),
            )
            return _DeliveryOutcome(order_id=order.order_id, action="ledger-pending")
    return _DeliveryOutcome(order_id=order.order_id, action="result", result=result)


def _deliver_and_record(call: _DeliveryCall) -> None:
    """Worker entry: compute the delivery outcome and durably record it."""
    outcome = _compute_delivery_outcome(call)
    payload = outcome.to_record()
    # The drain rebuilds deferral results from these, so they ride with the
    # record rather than being recomputed from pass state.
    payload["resolved_zdr"] = call.resolved_zdr
    payload["attestation_id"] = call.attestation_id
    write_json_atomic(_delivery_record_path(call.layout.root, call.order.order_id), payload)


def _load_delivery_record(record_path: Path) -> _DeliveryOutcome | None:
    """Parse one delivery record; ``None`` when absent or malformed."""
    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _DeliveryOutcome.from_record(raw)


def _apply_delivery_outcome(
    call: _DeliveryCall,
    outcome: _DeliveryOutcome,
    *,
    processed_dir: Path,
    orders_dir: Path,
    retry_tracker: LaneLockRetryTracker,
) -> DispatchResult | None:
    """Apply one recorded delivery outcome to the queue on the drain pass.

    All queue-state mutations — result publication, claim moves, retry
    sidecars, nonce clearing — happen here on the loop, so the queue keeps
    exactly one writer of record however many workers computed outcomes.
    """
    order = call.order
    claimed_path = call.claimed_path
    results_dir = call.results_dir
    deferred_dir = call.deferred_dir
    if outcome.action == "existing-result":
        _complete_existing_result(
            claimed_path,
            results_dir / f"{order.order_id}.json",
            order=order,
            results_dir=results_dir,
            processed_dir=processed_dir,
            deferred_dir=deferred_dir,
            ledger_path=call.ledger_path,
            ledger_key_path=call.ledger_key_path,
            transcript_binding_path=call.transcript_binding_path,
            strict_autonomous=call.strict_autonomous,
        )
        return None
    if outcome.action == "ledger-pending":
        # The ledger could not prove the delivery. Leave the claim in
        # ``in_flight/`` — the drain drops the owner marker, and the next
        # pass's ``_reclaim_stale_in_flight`` returns it to ``orders/`` where
        # the minted nonce keeps the retry verify-only. This mirrors the old
        # inline ``return None`` that left the claim to be reclaimed.
        return None
    if outcome.action == "lock-timeout":
        attempts = retry_tracker.record_attempt(order.order_id)
        logger.warning(
            "dispatchd_lane_lock_failed",
            order_id=order.order_id,
            session_ref=order.session_ref,
            error=outcome.detail,
            attempts=attempts,
        )
        blocked = DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            resolved_zdr=outcome.resolved_zdr,
            status=DispatchStatus.BLOCKED,
            reason=f"lane lock unavailable: {outcome.detail}",
            decision_attestation_id=outcome.attestation_id,
        )
        deferred_dir.mkdir(parents=True, exist_ok=True)
        try:
            _move_without_replace(claimed_path, deferred_dir / claimed_path.name)
        except OSError as move_error:
            # The owner marker is removed by the drain after apply; the next
            # pass will reclaim the claimed file into orders/. The retry
            # sidecar is already durable, so this cannot lose or reset the
            # attempt count.
            logger.error(
                "dispatchd_lane_lock_defer_failed",
                order_id=order.order_id,
                path=str(claimed_path),
                error=str(move_error),
            )
        return blocked
    if outcome.action == "defer-lifecycle":
        return _defer_lifecycle_order(
            claimed_path,
            deferred_dir=deferred_dir,
            retry_tracker=retry_tracker,
            order=order,
            state=outcome.defer_state or "",
            resolved_zdr=outcome.resolved_zdr,
            attestation_id=outcome.attestation_id,
        )
    if outcome.action == "defer-goal-held":
        return _defer_held_goal_order(
            claimed_path,
            deferred_dir=deferred_dir,
            retry_tracker=retry_tracker,
            order=order,
            resolved_zdr=outcome.resolved_zdr,
            attestation_id=outcome.attestation_id,
        )
    result = outcome.result
    if result is None:
        logger.error("dispatchd_delivery_record_missing_result", order_id=order.order_id, action=outcome.action)
        return None
    if outcome.action == "defer-hold":
        # A guard hold changes this into a hold-owned deferral. Reset a
        # prior lane-lock retry marker so run_once does not churn it back
        # into orders while the hold remains active.
        retry_tracker.clear(order.order_id)
        deferred_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            _move_without_replace(claimed_path, deferred_dir / claimed_path.name)
        return result
    if result.status == DispatchStatus.DELIVERY_UNCONFIRMED:
        # An unconsumed delivery is never terminal (see
        # DispatchStatus.DELIVERY_UNCONFIRMED). Defer using the same durable
        # retry-attempts sidecar the lane-lock timeout path uses, so
        # ``_requeue_lane_lock_deferred`` returns it to ``orders/`` on a
        # later pass. Deliberately do NOT clear the
        # send-nonce written before this delivery attempt: the retried pass's
        # existing crash-reconciliation check (nonce present, no result)
        # re-greps the lane transcript without pasting again.
        attempts = retry_tracker.record_attempt(order.order_id)
        logger.warning(
            "dispatchd_delivery_unconfirmed",
            order_id=order.order_id,
            session_ref=order.session_ref,
            reason=result.reason,
            attempts=attempts,
        )
        deferred_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            _move_without_replace(claimed_path, deferred_dir / claimed_path.name)
        return result
    if result.status == DispatchStatus.SENT:
        # A worker records a SENT outcome only after its ledger append, but
        # the record itself is untrusted input: re-verify the signed row on
        # the drain so a forged or half-written record can never become a
        # published SENT result.
        try:
            verified = _verify_existing_delivery_ledger(
                order,
                ledger_path=call.ledger_path,
                ledger_key_path=call.ledger_key_path,
            )
        except Exception as exc:  # noqa: BLE001 -- keep the order pending for the next pass
            logger.error(
                "dispatchd_delivery_ledger_pending",
                order_id=order.order_id,
                session_ref=order.session_ref,
                error=str(exc),
            )
            verified = None
        if verified is None:
            logger.error(
                "dispatchd_delivery_ledger_pending",
                order_id=order.order_id,
                session_ref=order.session_ref,
                error="recorded SENT result has no delivery ledger proof",
            )
            # Same recoverable state as the worker's ledger-pending outcome:
            # claimed, nonce-bearing, ownerless after the drain drops the
            # marker — reclaimed on the next pass.
            return None
        # Publish the SENT result before finalizing, exactly as the inline
        # path did — a crash here leaves the claim and nonce recoverable.
        _write_result_atomic(results_dir, result)
    return _finalize_claimed_order(
        claimed_path,
        results_dir=results_dir,
        destination_dir=processed_dir,
        result=result,
        retry_state_dir=deferred_dir,
        retry_order_id=order.order_id,
    )


def _drain_delivery_results(
    queue_dir: Path,
    *,
    orders_dir: Path,
    results_dir: Path,
    processed_dir: Path,
    lock_dir: Path | None,
    ledger_path: Path | None,
    ledger_key_path: Path | None,
    tuning: DispatchTuning,
    policy: PolicyConfig,
    goals_root: Path | None,
    dispatch_runner: TmuxRunner | None,
    projects_root: Path | None,
    transcript_binding_paths: Mapping[str, Path] | None,
    local_extra: set[str] | None,
    tmux_socket: Path | None,
    out: list[DispatchResult],
) -> None:
    """Apply worker-recorded delivery outcomes left by earlier passes.

    A record whose claimed order still sits in ``in_flight/`` is applied and
    cleared; a record whose order vanished is stale and cleared. A claim
    owned by THIS process with no record and no in-flight worker is
    abandoned (the worker died between submit and record) and returns to
    ``orders/`` for the ordinary retry path.
    """
    layout = QueueLayout(queue_dir)
    deferred_dir = layout.deferred
    retry_tracker = LaneLockRetryTracker(deferred_dir)
    runs_dir = _delivery_runs_dir(queue_dir)
    if runs_dir.is_dir():
        for record_path in sorted(runs_dir.glob("*.json")):
            claimed_path = layout.in_flight / f"{record_path.stem}.json"
            outcome = _load_delivery_record(record_path)
            if outcome is None or outcome.order_id != record_path.stem or not claimed_path.exists():
                # No durable claim to apply against: either the record is
                # unreadable, or the order was already reclaimed. The
                # re-queued order re-delivers verify-only through its nonce.
                with contextlib.suppress(OSError):
                    record_path.unlink()
                continue
            try:
                order = DispatchOrder.model_validate_json(claimed_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                order = None
            if order is None:
                continue
            strict_autonomous = _strict_autonomous_order(order)
            call = _DeliveryCall(
                claimed_path=claimed_path,
                order=order,
                layout=layout,
                results_dir=results_dir,
                deferred_dir=deferred_dir,
                effective_lock_dir=lock_dir if lock_dir is not None else (goals_root / "locks" if goals_root is not None else None),
                goals_root=goals_root,
                tuning=tuning,
                policy=policy,
                dispatch_runner=dispatch_runner,
                projects_root=projects_root,
                transcript_binding_path=transcript_binding_paths.get(order.session_ref) if transcript_binding_paths is not None else None,
                local_extra=local_extra,
                tmux_socket=tmux_socket,
                ledger_path=ledger_path,
                ledger_key_path=ledger_key_path,
                strict_autonomous=strict_autonomous,
                resolved_zdr=order.routing_hint is not None,
                attestation_id=order.decision_attestation.attestation_id if order.decision_attestation is not None else None,
            )
            result = _apply_delivery_outcome(call, outcome, processed_dir=processed_dir, orders_dir=orders_dir, retry_tracker=retry_tracker)
            if result is not None:
                out.append(result)
            # The record was consumed: clear the attempt count so a session
            # that keeps delivering does not accumulate toward the inline
            # fallback ceiling.
            _DELIVERY_POOL.reset(f"{queue_dir}:{order.session_ref}")
            with contextlib.suppress(OSError):
                record_path.unlink()
            with contextlib.suppress(OSError):
                layout.owner_marker_path(order.order_id).unlink()

    # Recover claims this process still owns whose worker died before
    # recording: no delivery record and no in-flight worker means the claim
    # would otherwise sit forever behind a live owner marker.
    for claimed in sorted(layout.in_flight.glob("*.json")):
        marker = layout.owner_marker_path(claimed.stem)
        if read_owner_pid(marker) != os.getpid():
            continue
        try:
            order = DispatchOrder.model_validate_json(claimed.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            order = None
        if order is not None and _DELIVERY_POOL.in_flight(f"{queue_dir}:{order.session_ref}"):
            continue
        logger.warning("dispatchd_recovering_abandoned_claim", path=str(claimed))
        with contextlib.suppress(OSError):
            _move_without_replace(claimed, orders_dir / claimed.name)
        with contextlib.suppress(OSError):
            marker.unlink()


def process_one_order(
    order_path: Path,
    *,
    orders_dir: Path,
    results_dir: Path,
    processed_dir: Path,
    lock_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    attestation_ledger_path: Path | None = None,
    routing_config: RoutingConfig | None = None,
    policy: PolicyConfig | None = None,
    invalid_dir: Path | None = None,
    tuning: DispatchTuning | None = None,
    goals_root: Path | None = None,
    dispatch_runner: TmuxRunner | None = None,
    projects_root: Path | None = None,
    transcript_binding_paths: Mapping[str, Path] | None = None,
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
    allowed_session_prefixes: tuple[str, ...] = (),
    denied_session_prefixes: tuple[str, ...] = (),
    _defer_delivery: bool = False,
) -> DispatchResult | None:
    """Process a single order file. Returns the result, or None if skipped
    (already processed, claimed elsewhere, deferred by a rate-limit freeze
    or lane-lock timeout, or — under ``_defer_delivery`` — queued on the
    delivery worker with its outcome applied by a later pass's drain).

    With ``_defer_delivery`` set, the lane-lock wait, tmux paste, and
    transcript-confirmation poll run on ``_DELIVERY_POOL`` under a held
    claim, and the worker's durable ``delivery-runs/`` record is what a
    later ``run_once`` applies. The one-shot path leaves it unset so a
    single pass still delivers and reports synchronously.

    Crash-safe: if a result file already exists for this order id, the order
    is never re-dispatched. A non-SENT result is moved to ``processed/``. A
    SENT result is reconciled only with an already-existing signed delivery
    ledger row. If proof is absent, dispatchd leaves the order claimed and
    never invents proof from the result.

    ``routing_config``, if given, maps ``task_type`` to a routing selection
    (see ``chitra.routing_config``). If the order's ``routing_hint`` is not
    already set AND the order has a ``task_type``, the config is consulted
    before dispatch: a structured ``routes`` entry is RESOLVED to a concrete
    model+harness (+zdr) — recorded, with ``"route"`` provenance, on the
    result and signed ledger entry — otherwise a flat ``defaults`` entry
    fills in the opaque ``routing_hint`` (``"config"`` provenance). An
    explicit ``routing_hint`` from the caller always wins and skips this
    lookup entirely.

    ``goals_root`` selects the ``chitra.goals`` store consulted for the
    rate-limit freeze/defer check documented in this module's docstring
    (``None`` resolves to the default goals store, exactly like every other
    unset path in this function). A session with no goal record, or one
    held for any reason other than a rate-limit pause, is never frozen.

    ``dispatch_runner``/``projects_root``/``local_extra`` are optional test
    seams forwarded to both ``dispatch_to_tmux`` and the send-nonce crash
    reconciliation's transcript check (see this module's docstring);
    production callers leave them unset.

    Invalid orders produce a FAILED result using the source filename stem and
    are moved to ``invalid/`` (or ``invalid_dir``) so they cannot be retried
    as ordinary processed work.
    """
    _validate_pending_order_path(orders_dir, order_path)
    _require_real_directory(results_dir, label="results directory")
    _require_real_directory(processed_dir, label="processed directory")
    if invalid_dir is not None:
        _require_real_directory(invalid_dir, label="invalid directory")
    policy = policy or PolicyConfig()
    tuning = tuning or DispatchTuning()
    layout = QueueLayout(orders_dir.parent)
    deferred_dir = layout.deferred
    in_flight_dir = layout.in_flight
    in_flight_dir.mkdir(parents=True, exist_ok=True)

    # Atomically reserve the order before moving it out of orders/. The
    # reservation closes the former rename->owner-marker window that could
    # otherwise let another worker reclaim a live order as stale.
    reservation = reserve_claim(in_flight_dir, order_path.stem)
    if reservation is None:
        logger.info("dispatchd_order_reserved_elsewhere", path=str(order_path))
        return None
    try:
        claimed_path = reservation.claim(order_path)
    except FileNotFoundError:
        logger.info("dispatchd_order_claimed_elsewhere", path=str(order_path))
        reservation.release()
        return None
    except OSError as exc:
        logger.error("dispatchd_order_claim_failed", path=str(order_path), error=str(exc))
        reservation.release()
        return None

    # The reservation marker now records which live process holds this claim,
    # so a crashed worker's abandoned claim can be told apart from one still
    # legitimately in progress (see _reclaim_stale_in_flight). It is removed
    # once this claim is fully resolved, however it resolves — except while a
    # deferred delivery is queued: then the marker stays until a drain pass
    # applies the worker's record.
    claim_held = False
    try:
        resolved = _process_claimed_order(
            claimed_path,
            results_dir=results_dir,
            processed_dir=processed_dir,
            deferred_dir=deferred_dir,
            in_flight_dir=in_flight_dir,
            lock_dir=lock_dir,
            ledger_path=ledger_path,
            ledger_key_path=ledger_key_path,
            attestation_ledger_path=attestation_ledger_path,
            routing_config=routing_config,
            policy=policy,
            invalid_dir=invalid_dir,
            tuning=tuning,
            goals_root=goals_root,
            dispatch_runner=dispatch_runner,
            projects_root=projects_root,
            transcript_binding_paths=transcript_binding_paths,
            local_extra=local_extra,
            tmux_socket=tmux_socket,
            allowed_session_prefixes=allowed_session_prefixes,
            denied_session_prefixes=denied_session_prefixes,
            _defer_delivery=_defer_delivery,
        )
        claim_held = isinstance(resolved, _ClaimHeld)
        if isinstance(resolved, _ClaimHeld):
            return None
        return resolved
    finally:
        if not claim_held:
            reservation.release()


def _process_claimed_order(
    claimed_path: Path,
    *,
    results_dir: Path,
    processed_dir: Path,
    deferred_dir: Path,
    in_flight_dir: Path,
    lock_dir: Path | None,
    ledger_path: Path | None,
    ledger_key_path: Path | None,
    attestation_ledger_path: Path | None,
    routing_config: RoutingConfig | None,
    policy: PolicyConfig,
    invalid_dir: Path | None,
    tuning: DispatchTuning,
    goals_root: Path | None,
    dispatch_runner: TmuxRunner | None,
    projects_root: Path | None,
    transcript_binding_paths: Mapping[str, Path] | None,
    local_extra: set[str] | None,
    tmux_socket: Path | None,
    allowed_session_prefixes: tuple[str, ...],
    denied_session_prefixes: tuple[str, ...],
    _defer_delivery: bool = False,
) -> DispatchResult | _ClaimHeld | None:
    """The rest of order processing, once an order file is safely claimed
    (renamed into ``in_flight/`` with a live owner marker). Split out of
    ``process_one_order`` only so the owner-marker cleanup above can wrap it
    in one ``finally`` regardless of which of this function's many return
    points is taken. Returning the ``_CLAIM_HELD`` sentinel tells the caller
    to keep the owner marker: a delivery worker holds the claim until a
    drain pass applies its record.
    """
    # ``in_flight/`` always sits directly under the queue root, so the typed
    # layout (and every other queue path) derives from this call's own claim.
    layout = QueueLayout(in_flight_dir.parent)
    retry_tracker = LaneLockRetryTracker(deferred_dir)
    try:
        order = DispatchOrder.model_validate_json(claimed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("dispatchd_order_unreadable", path=str(claimed_path), error=str(exc))
        result = DispatchResult(
            order_id=claimed_path.stem,
            session_ref="",
            status=DispatchStatus.FAILED,
            reason=f"invalid-order: {exc}",
        )
        destination = invalid_dir or processed_dir.parent / QueueSubdir.INVALID
        return _finalize_claimed_order(
            claimed_path,
            results_dir=results_dir,
            destination_dir=destination,
            result=result,
            suppress_move_errors=True,
            retry_state_dir=deferred_dir,
        )

    resolved_zdr = False
    if order.routing_hint is None and order.task_type is not None:
        # A structured ``routes`` entry wins over a flat ``defaults`` hint:
        # chitra resolves model+harness (+zdr) into an opaque routing hint.
        route = resolve_route(order.task_type, routing_config)
        if route is not None:
            order.routing_hint = route.routing_hint
            resolved_zdr = route.zdr
        else:
            resolved_hint = resolve_routing_hint(order.task_type, routing_config)
            if resolved_hint is not None:
                order.routing_hint = resolved_hint

    strict_autonomous = _strict_autonomous_order(order)
    transcript_binding_path = (
        transcript_binding_paths.get(order.session_ref) if transcript_binding_paths is not None else None
    )
    attestation_id = order.decision_attestation.attestation_id if order.decision_attestation is not None else None
    # The caller-supplied results directory stays authoritative for the
    # idempotency lookup, matching where _write_result_atomic persists.
    existing_result = results_dir / f"{order.order_id}.json"
    if existing_result.is_symlink():
        raise ValueError(f"result path must not be a symlink: {existing_result}")
    if existing_result.exists():
        logger.info("dispatchd_order_already_processed", order_id=order.order_id)
        _complete_existing_result(
            claimed_path,
            existing_result,
            order=order,
            results_dir=results_dir,
            processed_dir=processed_dir,
            deferred_dir=deferred_dir,
            ledger_path=ledger_path,
            ledger_key_path=ledger_key_path,
            transcript_binding_path=transcript_binding_path,
            strict_autonomous=strict_autonomous,
        )
        return None

    goal_contract_rejection = _goal_contract_rejection(order, goals_root)
    if goal_contract_rejection == "goal-held" and order.message_kind == "operator_relay":
        return _defer_held_goal_order(
            claimed_path,
            deferred_dir=deferred_dir,
            retry_tracker=retry_tracker,
            order=order,
            resolved_zdr=resolved_zdr,
            attestation_id=attestation_id,
        )
    if goal_contract_rejection is not None:
        result = DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            status=DispatchStatus.BLOCKED,
            reason=goal_contract_rejection,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            resolved_zdr=resolved_zdr,
            decision_attestation_id=attestation_id,
        )
        return _finalize_claimed_order(
            claimed_path,
            results_dir=results_dir,
            destination_dir=processed_dir,
            result=result,
            retry_state_dir=deferred_dir,
            retry_order_id=order.order_id,
        )
    if order.decision_attestation is not None:
        try:
            ledger_mod.append_attestation(
                attestation_ledger_path or default_attestation_ledger_path(),
                order_id=order.order_id,
                session_ref=order.session_ref,
                attestation=order.decision_attestation,
            )
        except OSError as exc:
            logger.error("dispatchd_attestation_log_failed", order_id=order.order_id, error=str(exc))
            result = DispatchResult(
                order_id=order.order_id,
                session_ref=order.session_ref,
                status=DispatchStatus.FAILED,
                reason=f"attestation-log-failed: {exc}",
                decision_attestation_id=attestation_id,
            )
            return _finalize_claimed_order(
                claimed_path,
                results_dir=results_dir,
                destination_dir=processed_dir,
                result=result,
                retry_state_dir=deferred_dir,
                retry_order_id=order.order_id,
            )

    scope_violation = session_scope_violation(
        order.session_ref,
        allowed_session_prefixes=allowed_session_prefixes,
        denied_session_prefixes=denied_session_prefixes,
    )
    if scope_violation is not None:
        logger.warning(
            "dispatchd_order_blocked_session_scope",
            order_id=order.order_id,
            session_ref=order.session_ref,
            reason=scope_violation,
        )
        result = DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            status=DispatchStatus.BLOCKED,
            reason=scope_violation,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            resolved_zdr=resolved_zdr,
            decision_attestation_id=attestation_id,
        )
        return _finalize_claimed_order(
            claimed_path,
            results_dir=results_dir,
            destination_dir=processed_dir,
            result=result,
            retry_state_dir=deferred_dir,
            retry_order_id=order.order_id,
        )

    # Completion claims are recognized at this boundary even when a caller
    # omitted todo metadata. A disputed claim is never delivered as an
    # ordinary "sent" nudge -- it is surfaced as its own distinct status and
    # the tmux paste never happens. A clean claim proceeds to normal
    # dispatch below; the CLEAN audit itself (logged) is the proof an
    # operator can use to authorize a close -- this daemon never closes
    # anything itself, only classifies and surfaces.
    completion_gate_applies = order.task_type not in _RATE_LIMIT_GUARD_TASK_TYPES and (
        is_completion_claim(order.nudge) or order.completion_todo_items is not None
    )
    if completion_gate_applies:
        audit = evaluate_completion_claim(
            order.completion_todo_items or [],
            order.nudge,
            order.completion_evidence,
            policy=policy.completion_gate,
            open_asks=order.completion_open_asks,
            blockers=order.completion_blockers,
        )
        if audit.verdict == "COMPLETION_DISPUTE":
            logger.warning(
                "dispatchd_completion_dispute",
                order_id=order.order_id,
                session_ref=order.session_ref,
                summary=audit.summary,
            )
            result = DispatchResult(
                order_id=order.order_id,
                session_ref=order.session_ref,
                status=DispatchStatus.COMPLETION_DISPUTE,
                reason=audit.summary,
                routing_hint=order.routing_hint,
                task_type=order.task_type,
                resolved_zdr=resolved_zdr,
                decision_attestation_id=attestation_id,
            )
            return _finalize_claimed_order(
                claimed_path,
                results_dir=results_dir,
                destination_dir=processed_dir,
                result=result,
                retry_state_dir=deferred_dir,
                retry_order_id=order.order_id,
            )
        logger.info(
            "dispatchd_completion_clean",
            order_id=order.order_id,
            session_ref=order.session_ref,
            summary=audit.summary,
        )

    # Goal writers default to ``<goals_root>/locks``. Keep dispatch on that
    # same directory when no explicit ``--lock-dir`` was supplied, so a goal
    # hold, redirect, or completion cannot land between this lock's final
    # recheck and the paste.
    effective_lock_dir = lock_dir if lock_dir is not None else (goals_root / "locks" if goals_root is not None else None)
    delivery = _DeliveryCall(
        claimed_path=claimed_path,
        order=order,
        layout=layout,
        results_dir=results_dir,
        deferred_dir=deferred_dir,
        effective_lock_dir=effective_lock_dir,
        goals_root=goals_root,
        tuning=tuning,
        policy=policy,
        dispatch_runner=dispatch_runner,
        projects_root=projects_root,
        transcript_binding_path=transcript_binding_path,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
        ledger_path=ledger_path,
        ledger_key_path=ledger_key_path,
        strict_autonomous=strict_autonomous,
        resolved_zdr=resolved_zdr,
        attestation_id=attestation_id,
    )
    if _defer_delivery:
        delivery_key = f"{layout.root}:{order.session_ref}"
        if _DELIVERY_POOL.attempts(delivery_key) < _DELIVERY_RUN_MAX_ATTEMPTS:
            # The claim stays held: the owner marker keeps the order out of
            # ``orders/`` while the worker's tmux paste and up-to-15s
            # transcript confirmation run off the pass. A later pass drains
            # the durable delivery record.
            _DELIVERY_POOL.submit(delivery_key, lambda: _deliver_and_record(delivery))
            return _CLAIM_HELD
        # Worker attempts that never landed a record fall back to the inline
        # call rather than queueing forever.
        _DELIVERY_POOL.reset(delivery_key)
    outcome = _compute_delivery_outcome(delivery)
    return _apply_delivery_outcome(
        delivery,
        outcome,
        processed_dir=processed_dir,
        orders_dir=layout.orders,
        retry_tracker=retry_tracker,
    )


def run_once(
    queue_dir: Path | None = None,
    *,
    lock_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    attestation_ledger_path: Path | None = None,
    routing_config_path: Path | None = None,
    policy_config_path: Path | None = None,
    invalid_dir: Path | None = None,
    tuning: DispatchTuning | None = None,
    goals_root: Path | None = None,
    dispatch_runner: TmuxRunner | None = None,
    projects_root: Path | None = None,
    transcript_root: Path | None = None,
    transcript_bindings_path: Path | None = None,
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
    allowed_session_prefixes: tuple[str, ...] = (),
    denied_session_prefixes: tuple[str, ...] = (),
    _defer_delivery: bool = False,
    _preloaded_routing_config: RoutingConfig | None | _ConfigNotPreloaded = _CONFIG_NOT_PRELOADED,
    _preloaded_policy: PolicyConfig | _ConfigNotPreloaded = _CONFIG_NOT_PRELOADED,
) -> list[DispatchResult]:
    """Process every pending order in ``queue_dir/orders`` once, FIFO by mtime.

    Retryable lane-lock deferrals are atomically returned to ``orders/`` after
    the initial pending snapshot, so ordinary pending work runs first. Guard
    freeze deferrals do not carry the retry sidecar and remain parked until
    ``requeue_deferred_for_session`` is called after their hold clears.

    ``routing_config_path`` (or the ``CHITRA_ROUTING_CONFIG`` env var if
    unset) is loaded once per call and passed to every ``process_one_order``
    invocation — see ``chitra.routing_config`` for the lookup semantics.

    ``goals_root`` is forwarded to ``process_one_order``'s rate-limit
    freeze/defer check on every order (see that function's docstring).

    ``run_forever`` passes preloaded config values so it can fall back to the
    last successful load after a bad live edit. Ordinary callers, including
    ``--once``, leave those private arguments unset and get fail-loud config
    loading from this function.
    """
    queue_dir = queue_dir or default_queue_dir()
    orders_dir, results_dir, processed_dir = _ensure_queue_dirs(queue_dir)
    transcript_binding_paths = _load_transcript_binding_paths(
        transcript_bindings_path,
        transcript_root=transcript_root,
        default_path=queue_dir.parent / DEFAULT_FILENAME,
    )
    _reclaim_stale_in_flight(queue_dir)
    note_goals_schema_state(goals_root)
    if isinstance(_preloaded_routing_config, _ConfigNotPreloaded):
        routing_config = load_routing_config(routing_config_path)
    else:
        routing_config = _preloaded_routing_config
    policy = load_policy_config(policy_config_path) if isinstance(_preloaded_policy, _ConfigNotPreloaded) else _preloaded_policy
    # A resume may happen without a provider-specific callback reaching this
    # process. Reconcile lifecycle-marked deferrals on every pass: active
    # lanes get their backlog back, while closed lanes return to the ordinary
    # under-lock gate for a durable BLOCKED audit result. Other deferred work
    # stays owned by its rate/load guard until that guard explicitly requeues
    # it.
    _requeue_lifecycle_deferred(queue_dir, orders_dir, goals_root=goals_root)
    out: list[DispatchResult] = []
    # Apply worker-recorded delivery outcomes before claiming new work, so a
    # deferred delivery resolves as soon as its record lands and an abandoned
    # claim returns to ``orders/`` in time for this pass's pending scan.
    _drain_delivery_results(
        queue_dir,
        orders_dir=orders_dir,
        results_dir=results_dir,
        processed_dir=processed_dir,
        lock_dir=lock_dir,
        ledger_path=ledger_path,
        ledger_key_path=ledger_key_path,
        tuning=tuning or DispatchTuning(),
        policy=policy,
        goals_root=goals_root,
        dispatch_runner=dispatch_runner,
        projects_root=projects_root,
        transcript_binding_paths=transcript_binding_paths,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
        out=out,
    )
    dated: list[tuple[int, int, Path]] = []
    for order_path in orders_dir.glob("*.json"):
        try:
            stat = order_path.stat()
            dated.append((stat.st_mtime_ns, stat.st_ino, order_path))
        except FileNotFoundError:
            # Order file vanished between the glob and the stat (e.g. raced
            # by something else touching the queue dir). Skip it rather than
            # letting the stat's exception kill run_forever's loop.
            logger.warning("dispatchd_order_vanished_before_stat", path=str(order_path))
    # Snapshot ordinary pending work before moving retryable lane-lock
    # deferrals back into orders/. This makes every newly arrived order run
    # before a retry, while preserving FIFO within each group.
    pending = [path for _, _, path in sorted(dated, key=lambda item: item[:2])]
    pending.extend(_requeue_lane_lock_deferred(queue_dir, orders_dir))
    for order_path in pending:
        result = process_one_order(
            order_path,
            orders_dir=orders_dir,
            results_dir=results_dir,
            processed_dir=processed_dir,
            lock_dir=lock_dir,
            ledger_path=ledger_path,
            ledger_key_path=ledger_key_path,
            attestation_ledger_path=attestation_ledger_path,
            routing_config=routing_config,
            policy=policy,
            invalid_dir=invalid_dir,
            tuning=tuning,
            goals_root=goals_root,
            dispatch_runner=dispatch_runner,
            projects_root=projects_root,
            transcript_binding_paths=transcript_binding_paths,
            local_extra=local_extra,
            tmux_socket=tmux_socket,
            allowed_session_prefixes=allowed_session_prefixes,
            denied_session_prefixes=denied_session_prefixes,
            _defer_delivery=_defer_delivery,
        )
        if result is not None:
            out.append(result)
    return out


def run_forever(
    queue_dir: Path | None = None,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    lock_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    attestation_ledger_path: Path | None = None,
    routing_config_path: Path | None = None,
    policy_config_path: Path | None = None,
    invalid_dir: Path | None = None,
    tuning: DispatchTuning | None = None,
    goals_root: Path | None = None,
    transcript_root: Path | None = None,
    transcript_bindings_path: Path | None = None,
    tmux_socket: Path | None = None,
    allowed_session_prefixes: tuple[str, ...] = (),
    denied_session_prefixes: tuple[str, ...] = (),
) -> None:
    """Run the daemon loop: drain the queue, sleep, repeat. Runs until killed.

    Config reload errors are logged with their traceback and fall back to the
    last successfully loaded value for that pass. A fresh daemon with no
    usable config starts from the shipped routing/policy defaults instead of
    exiting into a supervisor restart loop.
    """
    queue_dir = queue_dir or default_queue_dir()
    logger.info("dispatchd_started", queue_dir=str(queue_dir), poll_seconds=poll_seconds)
    last_routing_config: RoutingConfig | None = None
    last_policy = PolicyConfig()
    while True:
        try:
            routing_config = load_routing_config(routing_config_path)
        except Exception:  # noqa: BLE001 -- every config-loader error has one safe daemon fallback
            logger.error(
                "dispatchd_routing_config_reload_failed",
                path=str(routing_config_path) if routing_config_path is not None else "CHITRA_ROUTING_CONFIG",
                exc_info=True,
            )
            routing_config = last_routing_config
        else:
            last_routing_config = routing_config

        try:
            policy = load_policy_config(policy_config_path)
        except Exception:  # noqa: BLE001 -- every config-loader error has one safe daemon fallback
            logger.error(
                "dispatchd_policy_config_reload_failed",
                path=str(policy_config_path) if policy_config_path is not None else "CHITRA_POLICY_CONFIG",
                exc_info=True,
            )
            policy = last_policy
        else:
            last_policy = policy

        # Cleared before the pass so a worker finishing during the pass still
        # wakes the loop for the pass that drains its record.
        _DELIVERY_WAKE.clear()
        run_once(
            queue_dir,
            lock_dir=lock_dir,
            ledger_path=ledger_path,
            ledger_key_path=ledger_key_path,
            attestation_ledger_path=attestation_ledger_path,
            routing_config_path=routing_config_path,
            policy_config_path=policy_config_path,
            invalid_dir=invalid_dir,
            tuning=tuning,
            goals_root=goals_root,
            transcript_root=transcript_root,
            transcript_bindings_path=transcript_bindings_path,
            tmux_socket=tmux_socket,
            allowed_session_prefixes=allowed_session_prefixes,
            denied_session_prefixes=denied_session_prefixes,
            _defer_delivery=True,
            _preloaded_routing_config=routing_config,
            _preloaded_policy=policy,
        )
        # Sleep in short slices so a finished delivery worker wakes the loop
        # for the pass that drains its record instead of waiting out the
        # whole poll interval.
        deadline = time.monotonic() + poll_seconds
        while not _DELIVERY_WAKE.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.25))


def run_lanes_once(
    lanes_file: Path | None = None,
    *,
    routing_config_path: Path | None = None,
    policy_config_path: Path | None = None,
    invalid_dir_name: str = "invalid",
    tuning: DispatchTuning | None = None,
    dispatch_runner: TmuxRunner | None = None,
    transcript_root: Path | None = None,
    transcript_bindings_path: Path | None = None,
) -> dict[str, list[DispatchResult]]:
    """Drain every enabled lane from one rendered declaration."""
    from chitra.lane_config import enabled_lanes

    results: dict[str, list[DispatchResult]] = {}
    for lane in enabled_lanes(lanes_file):
        results[lane.identifier] = run_once(
            lane.queue_dir,
            lock_dir=lane.state_dir / "locks",
            ledger_path=lane.state_dir / "ledger.jsonl",
            ledger_key_path=lane.state_dir / "ledger.key",
            attestation_ledger_path=lane.state_dir / "attestations.jsonl",
            routing_config_path=routing_config_path,
            policy_config_path=policy_config_path,
            invalid_dir=lane.queue_dir / invalid_dir_name,
            tuning=tuning,
            goals_root=lane.state_dir,
            dispatch_runner=dispatch_runner,
            projects_root=lane.config_dir / "projects",
            transcript_root=transcript_root or lane.config_dir / "projects",
            transcript_bindings_path=transcript_bindings_path or lane.state_dir / DEFAULT_FILENAME,
            tmux_socket=lane.tmux_socket,
            _defer_delivery=True,
        )
    return results


def run_lanes_forever(
    lanes_file: Path | None = None,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    routing_config_path: Path | None = None,
    policy_config_path: Path | None = None,
    tuning: DispatchTuning | None = None,
    transcript_root: Path | None = None,
    transcript_bindings_path: Path | None = None,
) -> None:
    """Run one shared dispatchd process over all enabled lane queues."""
    while True:
        _DELIVERY_WAKE.clear()
        run_lanes_once(
            lanes_file,
            routing_config_path=routing_config_path,
            policy_config_path=policy_config_path,
            tuning=tuning,
            transcript_root=transcript_root,
            transcript_bindings_path=transcript_bindings_path,
        )
        deadline = time.monotonic() + poll_seconds
        while not _DELIVERY_WAKE.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.25))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatchd", description="Deterministic tmux dispatch daemon (chitra phase 1).")
    parser.add_argument("--queue-dir", type=Path, default=None, help="Order/result/processed queue root (default: CHITRA_STATE_DIR/queue).")
    parser.add_argument(
        "--lanes-file",
        type=Path,
        default=None,
        help="Rendered lane declaration; when set, one process drains every enabled lane.",
    )
    parser.add_argument(
        "--lock-dir",
        type=Path,
        default=None,
        help="LaneLock directory (env CHITRA_LANE_LOCK_DIR, else a dir under the system temp dir).",
    )
    parser.add_argument("--ledger-path", type=Path, default=None, help="Delivery ledger JSONL path (default: next to the state dir).")
    parser.add_argument("--ledger-key-path", type=Path, default=None, help="HMAC signing key path (generated on first use if missing).")
    parser.add_argument(
        "--attestation-ledger-path",
        type=Path,
        default=None,
        help="Our-side decision-attestation JSONL path (default: CHITRA_STATE_DIR/attestations.jsonl).",
    )
    parser.add_argument(
        "--routing-config-path",
        type=Path,
        default=None,
        help="Path to a routing.yaml task_type->routing_hint lookup (env CHITRA_ROUTING_CONFIG, else no config/no-op).",
    )
    parser.add_argument(
        "--policy-config-path",
        type=Path,
        default=None,
        help="Path to policy.yaml (env CHITRA_POLICY_CONFIG, else shipped defaults).",
    )
    parser.add_argument("--invalid-orders-dir", type=Path, default=None, help="Invalid-order directory (default: <queue-dir>/invalid).")
    parser.add_argument(
        "--goals-root",
        type=Path,
        default=None,
        help="chitra.goals store root consulted for the guard freeze check (default: CHITRA_STATE_DIR).",
    )
    parser.add_argument(
        "--transcript-root",
        type=Path,
        default=None,
        help="Root used to resolve relative transcript paths in the binding manifest.",
    )
    parser.add_argument(
        "--transcript-bindings-path",
        type=Path,
        default=None,
        help="Strict chitra.transcript-bindings.v1 manifest for autonomous deliveries.",
    )
    parser.add_argument(
        "--allow-session-prefix",
        action="append",
        default=None,
        help="Only dispatch to tmux session names with this prefix (repeatable; default: CHITRA_ALLOWED_SESSION_PREFIXES).",
    )
    parser.add_argument(
        "--deny-session-prefix",
        action="append",
        default=None,
        help="Never dispatch to tmux session names with this prefix (repeatable; default: CHITRA_DENIED_SESSION_PREFIXES).",
    )
    parser.add_argument("--capture-lines", type=int, default=12)
    parser.add_argument("--post-paste-wait-seconds", type=float, default=DISPATCH_VERIFY_WAIT_SECONDS)
    parser.add_argument("--transcript-recency-seconds", type=float, default=300.0)
    parser.add_argument("--lane-lock-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Drain the queue once and exit (for tests/cron), instead of looping forever.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.transcript_bindings_path is not None and not args.transcript_bindings_path.exists():
        # load_transcript_bindings() treats a missing path as "legacy
        # journals only" and silently returns no bindings -- correct for an
        # unset default, but not when an explicit --transcript-bindings-path
        # names a file that should exist. The shipped systemd unit's
        # ConditionPathExists skips the service without an error, so
        # `systemctl start` reports success while nothing runs. Fail fast
        # here instead.
        logger.error(
            "dispatchd_transcript_bindings_missing",
            path=str(args.transcript_bindings_path),
            detail="dispatchd refuses to start with no autonomous-delivery bindings",
        )
        return 1
    queue_dir = args.queue_dir or default_queue_dir()
    allowed_session_prefixes = resolve_session_prefixes(args.allow_session_prefix, env_var=SESSION_ALLOW_PREFIXES_ENV_VAR)
    denied_session_prefixes = resolve_session_prefixes(args.deny_session_prefix, env_var=SESSION_DENY_PREFIXES_ENV_VAR)
    tuning = DispatchTuning(
        capture_lines=args.capture_lines,
        post_paste_wait_seconds=args.post_paste_wait_seconds,
        transcript_recency_seconds=args.transcript_recency_seconds,
        lane_lock_timeout_seconds=args.lane_lock_timeout_seconds,
    )
    if args.lanes_file is not None:
        if args.once:
            lane_results = run_lanes_once(
                args.lanes_file,
                routing_config_path=args.routing_config_path,
                policy_config_path=args.policy_config_path,
                tuning=tuning,
                transcript_root=args.transcript_root,
                transcript_bindings_path=args.transcript_bindings_path,
            )
            print(json.dumps({key: [item.model_dump(mode="json") for item in value] for key, value in lane_results.items()}, indent=2))
            return 0
        run_lanes_forever(
            args.lanes_file,
            poll_seconds=args.poll_seconds,
            routing_config_path=args.routing_config_path,
            policy_config_path=args.policy_config_path,
            tuning=tuning,
            transcript_root=args.transcript_root,
            transcript_bindings_path=args.transcript_bindings_path,
        )
        return 0
    if args.once:
        results = run_once(
            queue_dir,
            lock_dir=args.lock_dir,
            ledger_path=args.ledger_path,
            ledger_key_path=args.ledger_key_path,
            attestation_ledger_path=args.attestation_ledger_path,
            routing_config_path=args.routing_config_path,
            policy_config_path=args.policy_config_path,
            invalid_dir=args.invalid_orders_dir,
            tuning=tuning,
            goals_root=args.goals_root,
            transcript_root=args.transcript_root,
            transcript_bindings_path=args.transcript_bindings_path,
            allowed_session_prefixes=allowed_session_prefixes,
            denied_session_prefixes=denied_session_prefixes,
        )
        print(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
        return 0
    run_forever(
        queue_dir,
        poll_seconds=args.poll_seconds,
        lock_dir=args.lock_dir,
        ledger_path=args.ledger_path,
        ledger_key_path=args.ledger_key_path,
        attestation_ledger_path=args.attestation_ledger_path,
        routing_config_path=args.routing_config_path,
        policy_config_path=args.policy_config_path,
        invalid_dir=args.invalid_orders_dir,
        tuning=tuning,
        goals_root=args.goals_root,
        transcript_root=args.transcript_root,
        transcript_bindings_path=args.transcript_bindings_path,
        allowed_session_prefixes=allowed_session_prefixes,
        denied_session_prefixes=denied_session_prefixes,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

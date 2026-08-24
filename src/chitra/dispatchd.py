"""dispatchd — deterministic daemon that drains a JSON order queue and
delivers each order via ``chitra.dispatch.dispatch_to_tmux``, enforcing the
single-writer rule via ``LaneLock``.

Queue layout (default ``queue_dir``, overridable per call/CLI):

    queue_dir/orders/*.json      -- DispatchOrder JSON, one file per order
    queue_dir/in_flight/*.json   -- an order file a worker has atomically
                                     claimed and is currently delivering
    queue_dir/deferred/*.json    -- an order parked because its session is
                                     guard-held, or because a lane lock
                                     timed out (see below); no terminal
                                     result file exists for it yet
    queue_dir/results/<id>.json  -- DispatchResult JSON; a SENT result may be
                                     written before ledger proof is available
    queue_dir/processed/*.json   -- the order file, moved here only after a
                                     terminal result, and for SENT only after
                                     a matching signed ledger entry

Crash-safety:

- **Idempotent redelivery.** Once a result file exists for an order id, that
  order is never redispatched -- ``process_one_order`` checks for an
  existing result file (both before and again immediately after acquiring
  the lane lock -- see "Lane-lock recheck" below). A SENT result is recovered
  by retrying its ledger proof, not by pasting again; the order moves to
  ``processed/`` only after that proof exists.
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
  no second paste. If consumption is not yet confirmed, later passes repeat
  verification only until proof appears or the retry budget fails loudly.

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
``BLOCKED`` result is persisted for that transient condition; after
``DEFAULT_LANE_LOCK_RETRY_ATTEMPTS`` timeouts, dispatchd writes the terminal
``FAILED`` ``retry-exhausted`` result and moves the order to ``processed/``.

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
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import structlog

from . import ledger as ledger_mod
from ._fsio import write_json_atomic
from .completion_gate import evaluate_completion_claim, is_completion_claim
from .dispatch import (
    DISPATCH_VERIFY_WAIT_SECONDS,
    DispatchTuning,
    LaneLock,
    LaneLockError,
    TmuxRunner,
    _pid_alive,
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
from .joined_lane import JoinedLaneReconciler, JoinedLaneStore, ReconcileReport, build_filesystem_reconciler
from .journal import native_session_identity
from .lane_config import LaneCredentials, LaneSpec
from .operating_facts import OperatingFactsSources, read_operating_facts
from .orders import DispatchOrder, DispatchResult, DispatchStatus
from .policy_config import PolicyConfig, load_policy_config
from .provider_protocol import Provider
from .recovery import RecoveryFactsReader as EngineRecoveryFactsReader
from .recovery import RecoverySupervisor, run_recovery_supervision
from .recovery_provider import (
    RecoveryFactsReader,
    RecoveryProviderFactory,
    RecoverySink,
    RecoveryVerifier,
    build_recovery_provider_resolver,
    default_operating_facts_reader,
)
from .routing_config import RoutingConfig, load_routing_config, resolve_route, resolve_routing_hint
from .session_contract import JoinedLaneRecord
from .state_paths import default_attestation_ledger_path, default_ledger_key_path, default_ledger_path, default_queue_dir, state_dir

logger = structlog.get_logger(__name__)

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_LANE_LOCK_RETRY_ATTEMPTS = 20

# Roots whose newer-than-installed goals.json has already been journaled; a
# long-running daemon notices once instead of writing the same warning into
# the journal on every poll.
_SCHEMA_NOTICED_ROOTS: set[str] = set()


def note_goals_schema_state(goals_root: Path | None) -> None:
    """Journal one read-only notice when this store's file schema is newer.

    A newer goals.json never stops the queue: goal state is treated as
    read-only and the daemon keeps running instead of exiting into a
    supervisor restart loop (the chitra.goals.v5 outage class).
    """
    file_schema = goals_schema_newer_than_installed(goals_root)
    if file_schema is None:
        return
    key = str(goals_root) if goals_root is not None else "<default-state-dir>"
    if key in _SCHEMA_NOTICED_ROOTS:
        return
    _SCHEMA_NOTICED_ROOTS.add(key)
    print(
        f"{GOALS_SCHEMA_NEWER_MESSAGE} goals_root={key} file_schema={file_schema} "
        f"installed_schema={GOALS_INSTALLED_SCHEMA}"
    )


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
SESSION_ALLOW_PREFIXES_ENV_VAR = "CHITRA_ALLOWED_SESSION_PREFIXES"
SESSION_DENY_PREFIXES_ENV_VAR = "CHITRA_DENIED_SESSION_PREFIXES"


def _ensure_queue_dirs(queue_dir: Path) -> tuple[Path, Path, Path]:
    orders = queue_dir / "orders"
    results = queue_dir / "results"
    processed = queue_dir / "processed"
    for d in (orders, results, processed, queue_dir / "in_flight", queue_dir / "deferred"):
        d.mkdir(parents=True, exist_ok=True)
    return orders, results, processed


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


def _write_result_atomic(results_dir: Path, result: DispatchResult) -> Path:
    """Write a result JSON atomically (write to temp, rename)."""
    target = results_dir / f"{result.order_id}.json"
    write_json_atomic(
        target,
        result.model_dump(mode="json"),
        temporary_path=results_dir / f".{result.order_id}.json.tmp",
        trailing_newline=False,
        sort_keys=False,
        cleanup_on_error=False,
    )
    return target


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
    """Persist one terminal result and move its claimed order exactly once."""
    _write_result_atomic(results_dir, result)
    destination_dir.mkdir(parents=True, exist_ok=True)
    if suppress_move_errors:
        with contextlib.suppress(OSError):
            claimed_path.replace(destination_dir / claimed_path.name)
    else:
        claimed_path.replace(destination_dir / claimed_path.name)
    if retry_state_dir is not None:
        _remove_lane_lock_retry_attempts(retry_state_dir, retry_order_id or claimed_path.stem)
    return result


def _ensure_delivery_ledger(
    order: DispatchOrder,
    result: DispatchResult,
    *,
    ledger_path: Path | None,
    ledger_key_path: Path | None,
) -> ledger_mod.LedgerEntry:
    """Return signed proof for a SENT order, appending it when needed.

    The order id is part of the lookup. Matching only a session and message
    would incorrectly accept an older identical nudge as proof for this
    order. The post-append lookup also catches a short write or a writer that
    returned without making the proof durable.

    When the confirmed result names a lane transcript, its adapter-native
    session identity is normalized with the journal's own version-gated
    normalizers and bound into the signed row (signature version 5). The
    value never comes from ``routing_hint``, which stays opaque audit
    metadata. A transcript that yields no fixture-gated native identity
    still gets a valid v4 row; consumption then fails closed instead of
    trusting an unbound session.
    """
    resolved_ledger_path = ledger_path or default_ledger_path()
    resolved_key_path = ledger_key_path or (ledger_path.with_name("ledger.key") if ledger_path is not None else default_ledger_key_path())
    key = ledger_mod.load_or_create_signing_key(resolved_key_path)
    existing = ledger_mod.verify_delivery(
        resolved_ledger_path,
        key=key,
        order_id=order.order_id,
        session_ref=order.session_ref,
        nudge=order.nudge,
    )
    if existing is not None:
        return existing

    if not result.native_session_id and result.transcript_path:
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
    return verified


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
) -> None:
    """Recover a claimed order whose result was written by an earlier pass.

    A result file is not itself a queue acknowledgment. SENT results from the
    old behavior can exist without ledger proof, so recovery retries signing
    the already-recorded delivery and never calls the pane transport again.
    """
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
            _ensure_delivery_ledger(
                order,
                stored_result,
                ledger_path=ledger_path,
                ledger_key_path=ledger_key_path,
            )
            stored_result.delivery_ledger_verified = True
            _write_result_atomic(results_dir, stored_result)
        except Exception as exc:  # noqa: BLE001 -- retry on the next daemon pass
            logger.error(
                "dispatchd_delivery_ledger_pending",
                order_id=order.order_id,
                session_ref=order.session_ref,
                error=str(exc),
            )
            return

    processed_dir.mkdir(parents=True, exist_ok=True)
    claimed_path.replace(processed_dir / claimed_path.name)
    _remove_lane_lock_retry_attempts(deferred_dir, order.order_id)


def _reserve_owner_marker(in_flight_dir: Path, order_id: str) -> Path | None:
    """Atomically reserve an order before moving it into ``in_flight``.

    The marker is intentionally created *before* the order rename. A peer
    worker that sees the order still under ``orders/`` then sees the live
    reservation and skips it, rather than racing the tiny former window
    between the rename and owner-marker write.
    """
    owner_path = in_flight_dir / f".{order_id}.owner"
    try:
        with owner_path.open("x", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except FileExistsError:
        return None
    return owner_path


def _reclaim_stale_in_flight(queue_dir: Path) -> None:
    """Return an orphaned ``in_flight/`` order to ``orders/`` for reclaiming.

    Mirrors ``chitra.dispatch.LaneLock``'s own stale-lock reclaim: every
    successful claim writes an owner marker (this process's pid) next to the
    claimed order file; a claim whose owner pid is no longer alive was
    abandoned by a crashed worker and is safe to return to ``orders/`` for a
    fresh claim. A claim whose owner is still alive is a real
    currently-in-progress delivery and is never touched -- this must never
    steal a claim out from under a live worker. Called at the top of every
    ``run_once`` pass so a crash between claiming an order and writing its
    result is always eventually retried, never stranded. See
    docs/SOL-ADVERSARIAL-REVIEW findings #2 and #5.
    """
    in_flight_dir = queue_dir / "in_flight"
    orders_dir = queue_dir / "orders"
    if not in_flight_dir.is_dir():
        return
    for claimed in in_flight_dir.glob("*.json"):
        owner_path = in_flight_dir / f".{claimed.stem}.owner"
        try:
            pid = int(owner_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0  # no/corrupt owner marker -- treat as abandoned, safe to reclaim
        if pid and _pid_alive(pid):
            continue
        logger.warning("dispatchd_reclaiming_stale_in_flight_order", path=str(claimed), owner_pid=pid)
        with contextlib.suppress(OSError):
            claimed.replace(orders_dir / claimed.name)
        with contextlib.suppress(OSError):
            owner_path.unlink()

    # A worker can die after creating its reservation but before moving the
    # order file into in_flight/. Such an orphan marker must not permanently
    # block the still-pending order, but a live owner's short pre-rename window
    # must remain protected.
    for owner_path in in_flight_dir.glob(".*.owner"):
        order_id = owner_path.name[1 : -len(".owner")]
        if not order_id or (in_flight_dir / f"{order_id}.json").exists():
            continue
        try:
            pid = int(owner_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid and _pid_alive(pid):
            continue
        logger.warning("dispatchd_reclaiming_stale_reservation", path=str(owner_path), owner_pid=pid)
        with contextlib.suppress(OSError):
            owner_path.unlink()


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
    deferred_dir = queue_dir / "deferred"
    dated: list[tuple[int, int, Path]] = []
    for path in deferred_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("session_ref") != session_ref:
            continue
        try:
            stat = path.stat()
            dated.append((stat.st_mtime_ns, stat.st_ino, path))
        except FileNotFoundError:
            continue
    dated.sort(key=lambda item: item[:2])
    requeued: list[str] = []
    for _, _, path in dated:
        target = orders_dir / path.name
        try:
            path.replace(target)
        except OSError:
            logger.warning("dispatchd_deferred_requeue_failed", session_ref=session_ref, path=str(path))
            continue
        requeued.append(path.stem)
    if requeued:
        logger.info("dispatchd_deferred_requeued", session_ref=session_ref, order_ids=requeued)
    return requeued


def _lane_lock_retry_state_path(deferred_dir: Path, order_id: str) -> Path:
    """Return the sidecar that makes a lane-lock retry durable.

    The sidecar deliberately has no ``.json`` suffix: normal deferred order
    scans only consider JSON order files, so this control record can never be
    mistaken for a dispatch order.
    """
    return deferred_dir / f".{order_id}.lane-lock-attempts"


def _read_lane_lock_retry_attempts(deferred_dir: Path, order_id: str, *, retry_limit: int) -> int:
    """Read a retry count, failing closed if a manually-corrupt sidecar appears."""
    path = _lane_lock_retry_state_path(deferred_dir, order_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        attempts = payload["attempts"]
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ValueError("attempts must be a non-negative integer")
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Atomic writes prevent a process crash from producing this state.
        # Treat an externally corrupted record as exhausted rather than
        # resetting it and allowing an unbounded retry loop.
        logger.error("dispatchd_lane_lock_retry_state_invalid", path=str(path), error=str(exc))
        return retry_limit
    return attempts


def _record_lane_lock_retry_attempt(deferred_dir: Path, order_id: str, *, retry_limit: int) -> int:
    """Atomically increment and persist one lane-lock timeout count."""
    attempts = _read_lane_lock_retry_attempts(deferred_dir, order_id, retry_limit=retry_limit) + 1
    write_json_atomic(_lane_lock_retry_state_path(deferred_dir, order_id), {"attempts": attempts})
    return attempts


def _remove_lane_lock_retry_attempts(deferred_dir: Path, order_id: str) -> None:
    """Best-effort cleanup after a terminal result has made retry state moot."""
    with contextlib.suppress(OSError):
        _lane_lock_retry_state_path(deferred_dir, order_id).unlink()


def _joined_lane_deferred_marker_path(deferred_dir: Path, order_id: str) -> Path:
    """Return the durable marker identifying a joined-lane barrier defer."""

    return deferred_dir / f".{order_id}.joined-lane.json"


def _requeue_joined_lane_deferred(queue_dir: Path, report: ReconcileReport) -> list[Path]:
    """Return only joined-lane deferred orders whose barrier now allows them."""

    if report.errors:
        return []
    orders_dir = queue_dir / "orders"
    deferred_dir = queue_dir / "deferred"
    requeued: list[Path] = []
    for marker in sorted(deferred_dir.glob(".*.joined-lane.json")):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            order_id = payload["order_id"]
            session_ref = payload["session_ref"]
            if not isinstance(order_id, str) or not isinstance(session_ref, str):
                raise ValueError("invalid joined-lane defer marker")
            if not report.allows(session_ref):
                continue
            source = deferred_dir / f"{order_id}.json"
            target = orders_dir / source.name
            order = DispatchOrder.model_validate_json(source.read_text(encoding="utf-8"))
            if order.order_id != order_id or order.session_ref != session_ref:
                raise ValueError("joined-lane defer marker does not match order")
            if target.exists():
                continue
            source.replace(target)
            marker.unlink()
            requeued.append(target)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            logger.error("dispatchd_joined_lane_requeue_failed", marker=str(marker), error=str(exc))
    if requeued:
        logger.info("dispatchd_joined_lane_requeued", order_ids=[path.stem for path in requeued])
    return requeued


def _requeue_lane_lock_deferred(queue_dir: Path, orders_dir: Path) -> list[Path]:
    """Atomically return retryable lane-lock deferrals after current pending work.

    Rate-limit and load-shed deferrals intentionally have no retry sidecar;
    they remain parked until ``requeue_deferred_for_session`` is called after
    the hold clears. A lane-lock timeout first writes its sidecar and only
    then moves the order, so a crash at either point leaves a recoverable
    order plus an accurate retry count.
    """
    deferred_dir = queue_dir / "deferred"
    dated: list[tuple[int, int, Path]] = []
    for path in deferred_dir.glob("*.json"):
        if not _lane_lock_retry_state_path(deferred_dir, path.stem).exists():
            continue
        try:
            stat = path.stat()
            dated.append((stat.st_mtime_ns, stat.st_ino, path))
        except FileNotFoundError:
            continue
    dated.sort(key=lambda item: item[:2])

    requeued: list[Path] = []
    for _, _, path in dated:
        target = orders_dir / path.name
        if target.exists():
            logger.error("dispatchd_lane_lock_deferred_target_exists", source=str(path), target=str(target))
            continue
        try:
            path.replace(target)
        except OSError as exc:
            logger.error("dispatchd_lane_lock_deferred_requeue_failed", path=str(path), error=str(exc))
            continue
        requeued.append(target)
    if requeued:
        logger.info("dispatchd_lane_lock_deferred_requeued", order_ids=[path.stem for path in requeued])
    return requeued


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
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
    allowed_session_prefixes: tuple[str, ...] = (),
    denied_session_prefixes: tuple[str, ...] = (),
    lane_lock_retry_attempts: int = DEFAULT_LANE_LOCK_RETRY_ATTEMPTS,
    joined_lane_report: ReconcileReport | None = None,
) -> DispatchResult | None:
    """Process a single order file. Returns the result, or None if skipped
    (already processed, claimed elsewhere, or deferred by a rate-limit freeze
    or lane-lock timeout).

    Crash-safe: if a result file already exists for this order id, the order
    is never re-dispatched. A non-SENT result is moved to ``processed/``. A
    SENT result is first reconciled with its signed delivery ledger; if the
    proof is absent, dispatchd retries only the ledger write and leaves the
    order unacknowledged until that succeeds.

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
    if lane_lock_retry_attempts < 1:
        raise ValueError("lane_lock_retry_attempts must be at least 1")
    policy = policy or PolicyConfig()
    tuning = tuning or DispatchTuning()
    deferred_dir = orders_dir.parent / "deferred"
    in_flight_dir = orders_dir.parent / "in_flight"
    in_flight_dir.mkdir(parents=True, exist_ok=True)

    # Atomically reserve the order before moving it out of orders/. The
    # reservation closes the former rename->owner-marker window that could
    # otherwise let another worker reclaim a live order as stale.
    claimed_path = in_flight_dir / order_path.name
    owner_path = _reserve_owner_marker(in_flight_dir, order_path.stem)
    if owner_path is None:
        logger.info("dispatchd_order_reserved_elsewhere", path=str(order_path))
        return None
    try:
        order_path.rename(claimed_path)
    except FileNotFoundError:
        logger.info("dispatchd_order_claimed_elsewhere", path=str(order_path))
        with contextlib.suppress(OSError):
            owner_path.unlink()
        return None
    except OSError as exc:
        logger.error("dispatchd_order_claim_failed", path=str(order_path), error=str(exc))
        with contextlib.suppress(OSError):
            owner_path.unlink()
        return None

    # The reservation marker now records which live process holds this claim,
    # so a crashed worker's abandoned claim can be told apart from one still
    # legitimately in progress (see _reclaim_stale_in_flight). It is removed
    # unconditionally once this claim is fully resolved, however it resolves.
    try:
        return _process_claimed_order(
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
            local_extra=local_extra,
            tmux_socket=tmux_socket,
            allowed_session_prefixes=allowed_session_prefixes,
            denied_session_prefixes=denied_session_prefixes,
            lane_lock_retry_attempts=lane_lock_retry_attempts,
            joined_lane_report=joined_lane_report,
        )
    finally:
        with contextlib.suppress(OSError):
            owner_path.unlink()


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
    local_extra: set[str] | None,
    tmux_socket: Path | None,
    allowed_session_prefixes: tuple[str, ...],
    denied_session_prefixes: tuple[str, ...],
    lane_lock_retry_attempts: int,
    joined_lane_report: ReconcileReport | None,
) -> DispatchResult | None:
    """The rest of order processing, once an order file is safely claimed
    (renamed into ``in_flight/`` with a live owner marker). Split out of
    ``process_one_order`` only so the owner-marker cleanup above can wrap it
    in one ``finally`` regardless of which of this function's many return
    points is taken.
    """
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
        destination = invalid_dir or processed_dir.parent / "invalid"
        return _finalize_claimed_order(
            claimed_path,
            results_dir=results_dir,
            destination_dir=destination,
            result=result,
            suppress_move_errors=True,
            retry_state_dir=deferred_dir,
        )

    if joined_lane_report is not None and not joined_lane_report.allows(order.session_ref):
        blocked = next(
            (item for item in joined_lane_report.blocked if item.session_ref == order.session_ref),
            None,
        )
        reason = blocked.reason if blocked is not None else (
            joined_lane_report.errors[0] if joined_lane_report.errors else "joined-lane restart reconciliation blocked this session"
        )
        deferred_dir.mkdir(parents=True, exist_ok=True)
        try:
            write_json_atomic(
                _joined_lane_deferred_marker_path(deferred_dir, order.order_id),
                {"order_id": order.order_id, "session_ref": order.session_ref, "reason": reason},
            )
            claimed_path.replace(deferred_dir / claimed_path.name)
        except OSError as exc:
            logger.error("dispatchd_joined_lane_defer_failed", order_id=order.order_id, error=str(exc))
            return None
        logger.info("dispatchd_joined_lane_deferred", order_id=order.order_id, session_ref=order.session_ref, reason=reason)
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            status=DispatchStatus.DEFERRED,
            reason=f"joined-lane restart barrier: {reason}",
            routing_hint=order.routing_hint,
            task_type=order.task_type,
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

    existing_result = results_dir / f"{order.order_id}.json"
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
        )
        return None

    attestation_id = order.decision_attestation.attestation_id if order.decision_attestation is not None else None
    if _read_lane_lock_retry_attempts(deferred_dir, order.order_id, retry_limit=lane_lock_retry_attempts) >= lane_lock_retry_attempts:
        logger.error(
            "dispatchd_lane_lock_retry_exhausted",
            order_id=order.order_id,
            session_ref=order.session_ref,
            retry_limit=lane_lock_retry_attempts,
        )
        # Non-silent terminal failure: a lane that never got its retries
        # cleared before hitting the queue's exhaustion check is otherwise
        # only visible in the daemon's own log stream.
        logger.critical(
            "dispatchd_retry_exhausted",
            order_id=order.order_id,
            session_ref=order.session_ref,
            retry_limit=lane_lock_retry_attempts,
        )
        result = DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            status=DispatchStatus.FAILED,
            reason="retry-exhausted",
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

    lock = LaneLock(order.session_ref, lock_dir=lock_dir)
    try:
        lock.acquire(blocking=True, timeout_seconds=tuning.lane_lock_timeout_seconds)
    except LaneLockError as exc:
        attempts = _record_lane_lock_retry_attempt(deferred_dir, order.order_id, retry_limit=lane_lock_retry_attempts)
        logger.warning(
            "dispatchd_lane_lock_failed",
            order_id=order.order_id,
            session_ref=order.session_ref,
            error=str(exc),
            attempts=attempts,
            retry_limit=lane_lock_retry_attempts,
        )
        if attempts >= lane_lock_retry_attempts:
            logger.error(
                "dispatchd_lane_lock_retry_exhausted",
                order_id=order.order_id,
                session_ref=order.session_ref,
                retry_limit=lane_lock_retry_attempts,
            )
            logger.critical(
                "dispatchd_retry_exhausted",
                order_id=order.order_id,
                session_ref=order.session_ref,
                retry_limit=lane_lock_retry_attempts,
            )
            result = DispatchResult(
                order_id=order.order_id,
                session_ref=order.session_ref,
                routing_hint=order.routing_hint,
                task_type=order.task_type,
                resolved_zdr=resolved_zdr,
                status=DispatchStatus.FAILED,
                reason="retry-exhausted",
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

        result = DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            resolved_zdr=resolved_zdr,
            status=DispatchStatus.BLOCKED,
            reason=f"lane lock unavailable: {exc}",
            decision_attestation_id=attestation_id,
        )
        deferred_dir.mkdir(parents=True, exist_ok=True)
        try:
            claimed_path.replace(deferred_dir / claimed_path.name)
        except OSError as move_error:
            # The owner marker is removed by process_one_order's finally;
            # the next pass will reclaim the claimed file into orders/. The
            # retry sidecar is already durable, so this cannot lose or reset
            # the attempt count.
            logger.error(
                "dispatchd_lane_lock_defer_failed",
                order_id=order.order_id,
                path=str(claimed_path),
                error=str(move_error),
            )
        return result

    try:
        # Lane-lock recheck: a concurrent order for the same session could
        # have completed and written a result while this order waited on
        # the lock. See docs/SOL-ADVERSARIAL-REVIEW finding #5.
        if existing_result.exists():
            logger.info("dispatchd_order_already_processed_under_lock", order_id=order.order_id)
            _complete_existing_result(
                claimed_path,
                existing_result,
                order=order,
                results_dir=results_dir,
                processed_dir=processed_dir,
                deferred_dir=deferred_dir,
                ledger_path=ledger_path,
                ledger_key_path=ledger_key_path,
            )
            return None

        # Rate-limit freeze/defer check, UNDER the lane lock (TOCTOU fix --
        # see this module's docstring). bypass_rate_limit_freeze only takes
        # effect for dispatchd's own sealed internal task types.
        allowed_bypass = order.bypass_rate_limit_freeze and order.task_type in _RATE_LIMIT_GUARD_TASK_TYPES
        held = None
        if not allowed_bypass:
            try:
                held = get_goal(goals_root, order.session_ref)
            except GoalsSchemaNewerError:
                # Read-only degradation: the store refuses writes to this
                # package, so run without goal-informed freeze decisions and
                # keep draining the queue rather than exiting.
                note_goals_schema_state(goals_root)
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
            deferred_dir.mkdir(parents=True, exist_ok=True)
            # A guard hold changes this into a hold-owned deferral. Reset a
            # prior lane-lock retry marker so run_once does not churn it back
            # into orders while the hold remains active.
            _remove_lane_lock_retry_attempts(deferred_dir, order.order_id)
            with contextlib.suppress(OSError):
                claimed_path.replace(deferred_dir / claimed_path.name)
            return DispatchResult(
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
                resolved_zdr=resolved_zdr,
            )

        # Send-nonce crash reconciliation: a marker already present here
        # means a PRIOR attempt got at least as far as (about to) paste before
        # this process/run restarted. Reconcile against the target transcript.
        # The nonce makes this a verify-only state: never paste again.
        nonce_path = in_flight_dir / f".{order.order_id}.nonce"
        if nonce_path.exists():
            logger.warning("dispatchd_order_reconciling_after_possible_crash", order_id=order.order_id, session_ref=order.session_ref)
            parts = order.session_ref.split(":")
            host = parts[0] if len(parts) == 3 else ""
            confirmed, transcript_path = transcript_confirms_nudge(
                order.nudge,
                host=host,
                projects_root=projects_root,
                recency_seconds=tuning.transcript_recency_seconds,
                runner=dispatch_runner,
                local_extra=local_extra,
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
            nonce_path.write_text(uuid.uuid4().hex, encoding="utf-8")
            result = dispatch_to_tmux(
                order,
                policy=policy,
                tuning=tuning,
                runner=dispatch_runner,
                projects_root=projects_root,
                local_extra=local_extra,
                tmux_socket=tmux_socket,
            )
    finally:
        lock.release()

    result.task_type = order.task_type
    result.routing_hint = order.routing_hint
    result.resolved_zdr = resolved_zdr
    result.decision_attestation_id = attestation_id
    logger.info(
        "dispatchd_order_processed",
        order_id=order.order_id,
        session_ref=order.session_ref,
        status=result.status.value,
    )
    if result.status == DispatchStatus.DELIVERY_UNCONFIRMED:
        # An unconsumed delivery is never terminal (see
        # DispatchStatus.DELIVERY_UNCONFIRMED). Defer using the same durable
        # retry-attempts sidecar the lane-lock timeout path uses, so
        # ``_requeue_lane_lock_deferred`` returns it to ``orders/`` on a
        # later pass and the exhaustion check at the top of this function
        # (which already emits the terminal FAILED "retry-exhausted" result,
        # CRIT-logged) applies uniformly. Deliberately do NOT clear the
        # send-nonce written before this delivery attempt: the retried pass's
        # existing crash-reconciliation check (nonce present, no result)
        # re-greps the lane transcript without pasting again.
        attempts = _record_lane_lock_retry_attempt(deferred_dir, order.order_id, retry_limit=lane_lock_retry_attempts)
        logger.warning(
            "dispatchd_delivery_unconfirmed",
            order_id=order.order_id,
            session_ref=order.session_ref,
            reason=result.reason,
            attempts=attempts,
            retry_limit=lane_lock_retry_attempts,
        )
        if attempts >= lane_lock_retry_attempts:
            # Same inline exhaustion check the lane-lock timeout path applies
            # right after incrementing its own attempt counter -- catch it in
            # this same pass rather than deferring one more time only to have
            # the top-of-function pre-check reject it on the next.
            logger.error(
                "dispatchd_lane_lock_retry_exhausted",
                order_id=order.order_id,
                session_ref=order.session_ref,
                retry_limit=lane_lock_retry_attempts,
            )
            logger.critical(
                "dispatchd_retry_exhausted",
                order_id=order.order_id,
                session_ref=order.session_ref,
                retry_limit=lane_lock_retry_attempts,
            )
            result.status = DispatchStatus.FAILED
            result.reason = "retry-exhausted"
            with contextlib.suppress(OSError):
                (in_flight_dir / f".{order.order_id}.nonce").unlink()
            return _finalize_claimed_order(
                claimed_path,
                results_dir=results_dir,
                destination_dir=processed_dir,
                result=result,
                retry_state_dir=deferred_dir,
                retry_order_id=order.order_id,
            )
        deferred_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            claimed_path.replace(deferred_dir / claimed_path.name)
        return result
    if result.status == DispatchStatus.SENT:
        # Persist the successful transport result before attempting the ledger
        # write. If the ledger is temporarily unavailable, the next pass can
        # retry the proof from this durable result without pasting twice.
        _write_result_atomic(results_dir, result)
        try:
            _ensure_delivery_ledger(
                order,
                result,
                ledger_path=ledger_path,
                ledger_key_path=ledger_key_path,
            )
            result.delivery_ledger_verified = True
            _write_result_atomic(results_dir, result)
        except Exception as exc:  # noqa: BLE001 -- keep the order pending for the next pass
            logger.error(
                "dispatchd_delivery_ledger_pending",
                order_id=order.order_id,
                session_ref=order.session_ref,
                error=str(exc),
            )
            return None
    _finalize_claimed_order(
        claimed_path,
        results_dir=results_dir,
        destination_dir=processed_dir,
        result=result,
        retry_state_dir=deferred_dir,
        retry_order_id=order.order_id,
    )
    with contextlib.suppress(OSError):
        (in_flight_dir / f".{order.order_id}.nonce").unlink()
    return result


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
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
    allowed_session_prefixes: tuple[str, ...] = (),
    denied_session_prefixes: tuple[str, ...] = (),
    lane_lock_retry_attempts: int = DEFAULT_LANE_LOCK_RETRY_ATTEMPTS,
    joined_lane_root: Path | None = None,
    joined_lane_reconciler: JoinedLaneReconciler | None = None,
    reconciliation_gate: Callable[[], ReconcileReport] | None = None,
    recovery_supervisor: RecoverySupervisor | None = None,
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
    if lane_lock_retry_attempts < 1:
        raise ValueError("lane_lock_retry_attempts must be at least 1")
    queue_dir = queue_dir or default_queue_dir()
    orders_dir, results_dir, processed_dir = _ensure_queue_dirs(queue_dir)
    # This is deliberately before the pending-order snapshot and before any
    # claim.  A blocked unfinished lane leaves its order in the queue and
    # therefore cannot reach provider I/O until a later wake/requeue pass.
    if reconciliation_gate is not None:
        joined_lane_report = reconciliation_gate()
    elif joined_lane_reconciler is not None and recovery_supervisor is None:
        joined_lane_report = joined_lane_reconciler.reconcile_all()
    elif recovery_supervisor is not None and joined_lane_root is not None:
        # RecoverySupervisor owns the lane lock and canonical state transition.
        # There is no separate mutable reconciler barrier in this production
        # path; it would be a second state machine.
        joined_lane_report = ReconcileReport(())
    elif joined_lane_root is not None:
        joined_lane_report = ReconcileReport((), ("joined-lane reconciler is required when joined_lane_root is set",))
    else:
        # Even legacy queue callers pass through the barrier.  With no lane
        # documents there is nothing to reconcile; a corrupt or unfinished
        # document cannot silently bypass the gate.
        try:
            unfinished = JoinedLaneStore(queue_dir).unfinished()
        except Exception as exc:  # noqa: BLE001 - fail closed before claim
            joined_lane_report = ReconcileReport((), (f"joined-lane barrier load failed: {exc}",))
        else:
            joined_lane_report = (
                ReconcileReport((), ("joined-lane reconciler is required for unfinished lanes",))
                if unfinished
                else ReconcileReport(())
            )
    if recovery_supervisor is not None:
        run_recovery_supervision(recovery_supervisor)
        if reconciliation_gate is not None:
            joined_lane_report = reconciliation_gate()
        elif joined_lane_reconciler is not None and recovery_supervisor is None:
            joined_lane_report = joined_lane_reconciler.reconcile_all()
    _requeue_joined_lane_deferred(queue_dir, joined_lane_report)
    _reclaim_stale_in_flight(queue_dir)
    note_goals_schema_state(goals_root)
    if isinstance(_preloaded_routing_config, _ConfigNotPreloaded):
        routing_config = load_routing_config(routing_config_path)
    else:
        routing_config = _preloaded_routing_config
    policy = load_policy_config(policy_config_path) if isinstance(_preloaded_policy, _ConfigNotPreloaded) else _preloaded_policy
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
    out: list[DispatchResult] = []
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
            local_extra=local_extra,
            tmux_socket=tmux_socket,
            allowed_session_prefixes=allowed_session_prefixes,
            denied_session_prefixes=denied_session_prefixes,
            lane_lock_retry_attempts=lane_lock_retry_attempts,
            joined_lane_report=joined_lane_report,
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
    tmux_socket: Path | None = None,
    allowed_session_prefixes: tuple[str, ...] = (),
    denied_session_prefixes: tuple[str, ...] = (),
    lane_lock_retry_attempts: int = DEFAULT_LANE_LOCK_RETRY_ATTEMPTS,
    joined_lane_root: Path | None = None,
    joined_lane_reconciler: JoinedLaneReconciler | None = None,
    reconciliation_gate: Callable[[], ReconcileReport] | None = None,
    recovery_supervisor: RecoverySupervisor | None = None,
) -> None:
    """Run the daemon loop: drain the queue, sleep, repeat. Runs until killed.

    Config reload errors are logged with their traceback and fall back to the
    last successfully loaded value for that pass. A fresh daemon with no
    usable config starts from the shipped routing/policy defaults instead of
    exiting into a supervisor restart loop.
    """
    queue_dir = queue_dir or default_queue_dir()
    if recovery_supervisor is None and joined_lane_root is not None:
        recovery_supervisor = build_single_queue_recovery_supervisor(joined_lane_root)
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
            tmux_socket=tmux_socket,
            allowed_session_prefixes=allowed_session_prefixes,
            denied_session_prefixes=denied_session_prefixes,
            lane_lock_retry_attempts=lane_lock_retry_attempts,
            joined_lane_root=joined_lane_root,
            joined_lane_reconciler=joined_lane_reconciler,
            reconciliation_gate=reconciliation_gate,
            recovery_supervisor=recovery_supervisor,
            _preloaded_routing_config=routing_config,
            _preloaded_policy=policy,
        )
        time.sleep(poll_seconds)


def run_lanes_once(
    lanes_file: Path | None = None,
    *,
    routing_config_path: Path | None = None,
    policy_config_path: Path | None = None,
    invalid_dir_name: str = "invalid",
    tuning: DispatchTuning | None = None,
    dispatch_runner: TmuxRunner | None = None,
    ownership_socket_path: Path | None = None,
    tophand_factory: RecoveryProviderFactory | None = None,
    amp_factory: RecoveryProviderFactory | None = None,
    provider_factories: Mapping[str, RecoveryProviderFactory] | None = None,
    pending_sink: RecoverySink | None = None,
    cursor_sink: RecoverySink | None = None,
    result_sink: RecoverySink | None = None,
    event_sink: RecoverySink | None = None,
    checkpoint_verifier: RecoveryVerifier | None = None,
    cancel_verifier: RecoveryVerifier | None = None,
    facts_reader: RecoveryFactsReader | None = None,
    operating_facts_sources: OperatingFactsSources | None = None,
    top_hand_identity_resolver: Callable[[object, Sequence[object]], object | None] | None = None,
) -> dict[str, list[DispatchResult]]:
    """Drain every enabled lane from one rendered declaration.

    The shared ``--lanes-file`` entrypoint is the production path used by the
    systemd package.  Each lane has its own joined-lane store and delivery
    ledger.  RecoverySupervisor is the sole joined-lane mutation owner and
    holds the per-lane lock across reread, provider I/O, and persistence.
    """
    from chitra.lane_config import enabled_lanes

    results: dict[str, list[DispatchResult]] = {}
    resolved_facts_reader = facts_reader or default_operating_facts_reader(operating_facts_sources)
    for lane in enabled_lanes(lanes_file):
        if all(
            dependency is None
            for dependency in (
                tophand_factory,
                amp_factory,
                provider_factories,
                pending_sink,
                cursor_sink,
                result_sink,
                event_sink,
                checkpoint_verifier,
                cancel_verifier,
            )
        ):
            if facts_reader is None and operating_facts_sources is None:
                provider_resolver = build_recovery_provider_resolver(lane)
            elif facts_reader is None:
                provider_resolver = build_recovery_provider_resolver(
                    lane,
                    operating_facts_sources=operating_facts_sources,
                )
            elif operating_facts_sources is None:
                provider_resolver = build_recovery_provider_resolver(lane, facts_reader=facts_reader)
            else:
                provider_resolver = build_recovery_provider_resolver(
                    lane,
                    facts_reader=facts_reader,
                    operating_facts_sources=operating_facts_sources,
                )
        else:
            provider_resolver = build_recovery_provider_resolver(
                lane,
                tophand_factory=tophand_factory,
                amp_factory=amp_factory,
                provider_factories=provider_factories,
                pending_sink=pending_sink,
                cursor_sink=cursor_sink,
                result_sink=result_sink,
                event_sink=event_sink,
                checkpoint_verifier=checkpoint_verifier,
                cancel_verifier=cancel_verifier,
                facts_reader=facts_reader,
                operating_facts_sources=operating_facts_sources,
            )
        recovery_supervisor = RecoverySupervisor(
            lane.state_dir,
            provider_resolver,
            goal_root=lane.state_dir,
            ledger_key_path=lane.state_dir / "ledger.key",
            facts_reader=cast(EngineRecoveryFactsReader, resolved_facts_reader),
            lane_id=lane.identifier,
            identity_resolver=cast(Any, top_hand_identity_resolver),
            operating_facts_reader=lambda sources=operating_facts_sources: tuple(
                read_operating_facts(sources).facts
            ),
        )
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
            tmux_socket=lane.tmux_socket,
            joined_lane_root=lane.state_dir,
            recovery_supervisor=recovery_supervisor,
        )
    return results


def build_single_queue_recovery_supervisor(
    state_root: Path,
    *,
    facts_reader: RecoveryFactsReader | None = None,
) -> RecoverySupervisor:
    """Build the canonical recovery owner for the legacy single-queue mode.

    The multi-lane manifest supplies a complete ``LaneSpec``.  The legacy
    queue has only one Chitra state root, so this constructor derives the
    non-authoritative path fields from that root while preserving the exact
    lane identity carried by each joined-lane record.  It never reads
    credentials or starts a provider during construction.
    """

    def resolve(record: JoinedLaneRecord) -> Provider | None:
        lane_id = record.lane_id
        lane = LaneSpec(
            identifier=lane_id,
            account=lane_id,
            uid=os.getuid() if hasattr(os, "getuid") else 1,
            home=state_root,
            workdir=state_root,
            config_dir=state_root,
            state_dir=state_root,
            tmux_socket=state_root / "tmux.sock",
            tmux_session=lane_id,
            credentials=LaneCredentials(
                claude_credentials=state_root / "credentials.json",
                ssh_dispatch_key=state_root / "dispatch.key",
            ),
        )
        resolver = build_recovery_provider_resolver(lane)
        return resolver(record)

    return RecoverySupervisor(
        state_root,
        resolve,
        goal_root=state_root,
        ledger_key_path=state_root / "ledger.key",
        facts_reader=cast(EngineRecoveryFactsReader, facts_reader or default_operating_facts_reader()),
    )


def run_lanes_forever(
    lanes_file: Path | None = None,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    routing_config_path: Path | None = None,
    policy_config_path: Path | None = None,
    tuning: DispatchTuning | None = None,
    ownership_socket_path: Path | None = None,
    tophand_factory: RecoveryProviderFactory | None = None,
    amp_factory: RecoveryProviderFactory | None = None,
    provider_factories: Mapping[str, RecoveryProviderFactory] | None = None,
    pending_sink: RecoverySink | None = None,
    cursor_sink: RecoverySink | None = None,
    result_sink: RecoverySink | None = None,
    event_sink: RecoverySink | None = None,
    checkpoint_verifier: RecoveryVerifier | None = None,
    cancel_verifier: RecoveryVerifier | None = None,
    facts_reader: RecoveryFactsReader | None = None,
    operating_facts_sources: OperatingFactsSources | None = None,
    top_hand_identity_resolver: Callable[[object, Sequence[object]], object | None] | None = None,
) -> None:
    """Run one shared dispatchd process over all enabled lane queues."""
    while True:
        run_lanes_once(
            lanes_file,
            routing_config_path=routing_config_path,
            policy_config_path=policy_config_path,
            tuning=tuning,
            ownership_socket_path=ownership_socket_path,
            tophand_factory=tophand_factory,
            amp_factory=amp_factory,
            provider_factories=provider_factories,
            pending_sink=pending_sink,
            cursor_sink=cursor_sink,
            result_sink=result_sink,
            event_sink=event_sink,
            checkpoint_verifier=checkpoint_verifier,
            cancel_verifier=cancel_verifier,
            facts_reader=facts_reader,
            operating_facts_sources=operating_facts_sources,
            top_hand_identity_resolver=top_hand_identity_resolver,
        )
        time.sleep(poll_seconds)


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
    parser.add_argument(
        "--lane-lock-retry-attempts",
        type=int,
        default=DEFAULT_LANE_LOCK_RETRY_ATTEMPTS,
        help="Maximum lane-lock timeout attempts before processing fails (default: 20).",
    )
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--joined-lane-root", type=Path, default=None)
    parser.add_argument("--ownership-socket-path", type=Path, default=None)
    parser.add_argument("--once", action="store_true", help="Drain the queue once and exit (for tests/cron), instead of looping forever.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
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
                ownership_socket_path=args.ownership_socket_path,
            )
            print(json.dumps({key: [item.model_dump(mode="json") for item in value] for key, value in lane_results.items()}, indent=2))
            return 0
        run_lanes_forever(
            args.lanes_file,
            poll_seconds=args.poll_seconds,
            routing_config_path=args.routing_config_path,
            policy_config_path=args.policy_config_path,
            tuning=tuning,
            ownership_socket_path=args.ownership_socket_path,
        )
        return 0
    joined_lane_root = args.joined_lane_root or state_dir()
    joined_lane_reconciler = build_filesystem_reconciler(
        joined_lane_root,
        ledger_path=args.ledger_path or default_ledger_path(),
        ledger_key_path=args.ledger_key_path or default_ledger_key_path(),
        ownership_socket_path=args.ownership_socket_path or Path("/run/chitra-ownership/provider.sock"),
    )
    recovery_supervisor = build_single_queue_recovery_supervisor(joined_lane_root)
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
            allowed_session_prefixes=allowed_session_prefixes,
            denied_session_prefixes=denied_session_prefixes,
            lane_lock_retry_attempts=args.lane_lock_retry_attempts,
            joined_lane_root=joined_lane_root,
            joined_lane_reconciler=joined_lane_reconciler,
            recovery_supervisor=recovery_supervisor,
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
        allowed_session_prefixes=allowed_session_prefixes,
        denied_session_prefixes=denied_session_prefixes,
        lane_lock_retry_attempts=args.lane_lock_retry_attempts,
        joined_lane_root=joined_lane_root,
        joined_lane_reconciler=joined_lane_reconciler,
        recovery_supervisor=recovery_supervisor,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

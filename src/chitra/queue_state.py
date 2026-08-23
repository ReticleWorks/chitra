"""queue_state — typed claim/queue-state seam for ``chitra.dispatchd``.

Every filesystem fact a queue worker acts on has a name here: the fixed
subdirectory layout (``orders/``, ``in_flight/``, ``deferred/``,
``results/``, ``processed/``, ``invalid/``), the exclusive-create owner
marker that reserves an order before it is renamed into ``in_flight/``,
the stale-claim sweep that returns a dead worker's claim to ``orders/``
without ever stealing a live owner's, the send-nonce marker that gates
paste-free crash reconciliation, and the durable lane-lock retry sidecar
parked next to a deferred order. ``dispatchd`` composes these primitives
into its delivery policy (lane locks, freeze checks, ledger signing);
this module owns only what the on-disk state IS -- paths, markers, and
counters -- so queue state transitions are reviewable in one place.

On-disk compatibility: this module reproduces the exact file and directory
names dispatchd has always used, including the dot-prefixed control files
(``.{order_id}.owner``, ``.{order_id}.nonce``, ``.{order_id}.lane-lock-attempts``).
Queues written by earlier versions remain readable and writable with no
migration step, and two workers of mixed versions can safely share one
queue directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import structlog

from ._fsio import write_json_atomic
from .dispatch import _pid_alive

logger = structlog.get_logger(__name__)


class QueueSubdir(StrEnum):
    """The fixed subdirectory names under one queue root."""

    ORDERS = "orders"
    IN_FLIGHT = "in_flight"
    DEFERRED = "deferred"
    RESULTS = "results"
    PROCESSED = "processed"
    INVALID = "invalid"


@dataclass(frozen=True)
class QueueLayout:
    """Typed view of one dispatchd queue directory tree.

    Every queue path dispatchd reads or writes derives from an instance of
    this class, so the layout is declared once instead of being re-spelled
    as string literals at each transition. ``root`` is the queue directory
    itself; the properties are the standard subdirectories.
    """

    root: Path

    @property
    def orders(self) -> Path:
        """Pending order files awaiting a claim."""
        return self.root / QueueSubdir.ORDERS

    @property
    def in_flight(self) -> Path:
        """Claimed order files plus their owner markers and send nonces."""
        return self.root / QueueSubdir.IN_FLIGHT

    @property
    def deferred(self) -> Path:
        """Parked orders (guard-held or lane-lock-retryable) plus retry sidecars."""
        return self.root / QueueSubdir.DEFERRED

    @property
    def results(self) -> Path:
        """One DispatchResult JSON per delivered order id."""
        return self.root / QueueSubdir.RESULTS

    @property
    def processed(self) -> Path:
        """Order files whose terminal result exists (SENT only after ledger proof)."""
        return self.root / QueueSubdir.PROCESSED

    @property
    def invalid(self) -> Path:
        """Quarantine for order files that fail to parse."""
        return self.root / QueueSubdir.INVALID

    def create(self) -> tuple[Path, Path, Path]:
        """Create every standard subdirectory; return ``(orders, results, processed)``."""
        for subdir in (self.orders, self.results, self.processed, self.in_flight, self.deferred):
            subdir.mkdir(parents=True, exist_ok=True)
        return self.orders, self.results, self.processed

    def result_path(self, order_id: str) -> Path:
        """Path of the result JSON for ``order_id``."""
        return self.results / f"{order_id}.json"

    def owner_marker_path(self, order_id: str) -> Path:
        """Path of the claim-reservation marker for ``order_id``."""
        return self.in_flight / f".{order_id}.owner"

    def send_nonce_path(self, order_id: str) -> Path:
        """Path of the send-nonce crash marker for ``order_id``."""
        return self.in_flight / f".{order_id}.nonce"

    def send_nonce(self, order_id: str) -> SendNonce:
        """The send-nonce crash marker handle for ``order_id``."""
        return SendNonce(order_id=order_id, path=self.send_nonce_path(order_id))


@dataclass(frozen=True)
class ClaimReservation:
    """A live, exclusively-created claim on one pending order.

    The reservation exists from the moment the owner marker wins its
    exclusive create until the claim is fully resolved, however that
    resolves. It knows how to perform the atomic rename into
    ``in_flight/`` (``claim``) and how to give the claim back
    (``release``); a released-or-never-held marker must never outlive the
    process that created it, because reclaim treats a marker with a dead
    owner pid as abandoned work.
    """

    order_id: str
    marker_path: Path

    @property
    def in_flight_dir(self) -> Path:
        return self.marker_path.parent

    def claim(self, pending_path: Path) -> Path:
        """Rename a pending order file into ``in_flight/`` under this reservation.

        Returns the claimed path. Propagates ``FileNotFoundError`` when
        another worker renamed the order first -- callers treat that as
        "claimed elsewhere", not as a failure of this worker's own claim,
        which is still valid until released.
        """
        claimed_path = self.in_flight_dir / pending_path.name
        pending_path.rename(claimed_path)
        return claimed_path

    def release(self) -> None:
        """Remove the owner marker; a missing marker is not an error."""
        with contextlib.suppress(OSError):
            self.marker_path.unlink()


def reserve_claim(in_flight_dir: Path, order_id: str) -> ClaimReservation | None:
    """Atomically reserve an order before moving it into ``in_flight``.

    The marker is intentionally created *before* the order rename. A peer
    worker that sees the order still under ``orders/`` then sees the live
    reservation and skips it, rather than racing the tiny former window
    between the rename and owner-marker write. Returns ``None`` when the
    marker already exists -- the caller lost the race and must skip the
    order entirely.
    """
    in_flight_dir.mkdir(parents=True, exist_ok=True)
    marker_path = in_flight_dir / f".{order_id}.owner"
    temporary_path: str | None = None
    try:
        # Publish the PID-bearing marker with an exclusive hard-link. A plain
        # ``open(..., "x")`` leaves an observable empty-file window between
        # creation and the PID write; the concurrent stale-claim sweep could
        # mistake that live pre-rename reservation for a dead owner and
        # remove it. The temporary file is written completely in the same
        # directory, then ``link`` makes the final name appear atomically.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=in_flight_dir,
            prefix=f".{order_id}.owner.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, marker_path)
    except FileExistsError:
        return None
    finally:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary_path)
    return ClaimReservation(order_id=order_id, marker_path=marker_path)


def read_owner_pid(marker_path: Path) -> int:
    """Parse an owner marker's pid; 0 means unknown (missing or corrupt).

    Zero is the fail-safe reading: reclaim treats it as an abandoned claim
    and returns the work to ``orders/``, which is recoverable, rather than
    stranding an order behind an unreadable marker forever.
    """
    try:
        return int(marker_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def reclaim_stale_claims(layout: QueueLayout) -> None:
    """Return orphaned ``in_flight/`` orders to ``orders/`` for reclaiming.

    Mirrors ``chitra.dispatch.LaneLock``'s own stale-lock reclaim: every
    successful claim writes an owner marker (this process's pid) next to the
    claimed order file; a claim whose owner pid is no longer alive was
    abandoned by a crashed worker and is safe to return to ``orders/`` for a
    fresh claim. A claim whose owner is still alive is a real
    currently-in-progress delivery and is never touched -- this must never
    steal a claim out from under a live worker. Called at the top of every
    pass so a crash between claiming an order and writing its result is
    always eventually retried, never stranded.

    Also sweeps orphan reservations: a worker can die after creating its
    marker but before moving the order file into ``in_flight/``. Such an
    orphan marker must not permanently block the still-pending order, but a
    live owner's short pre-rename window must remain protected.
    """
    in_flight_dir = layout.in_flight
    if not in_flight_dir.is_dir():
        return
    for claimed in in_flight_dir.glob("*.json"):
        owner_path = layout.owner_marker_path(claimed.stem)
        pid = read_owner_pid(owner_path)
        if pid and _pid_alive(pid):
            continue
        logger.warning("dispatchd_reclaiming_stale_in_flight_order", path=str(claimed), owner_pid=pid)
        with contextlib.suppress(OSError):
            claimed.replace(layout.orders / claimed.name)
        with contextlib.suppress(OSError):
            owner_path.unlink()

    for owner_path in in_flight_dir.glob(".*.owner"):
        order_id = owner_path.name[1 : -len(".owner")]
        if not order_id or (in_flight_dir / f"{order_id}.json").exists():
            continue
        pid = read_owner_pid(owner_path)
        if pid and _pid_alive(pid):
            continue
        logger.warning("dispatchd_reclaiming_stale_reservation", path=str(owner_path), owner_pid=pid)
        with contextlib.suppress(OSError):
            owner_path.unlink()


@dataclass(frozen=True)
class SendNonce:
    """The crash-reconciliation marker written before every pane paste.

    A present nonce means a prior attempt got at least as far as (about to)
    paste before dying: the order's verify-only state. Later passes must
    reconcile against the target transcript instead of injecting the same
    text again, so ``exists`` is checked before any paste and ``mint`` runs
    immediately before the first one.
    """

    order_id: str
    path: Path

    def exists(self) -> bool:
        """Whether a prior attempt left a nonce for this order."""
        return self.path.exists()

    def mint(self) -> None:
        """Write this attempt's fresh random nonce."""
        self.path.write_text(uuid.uuid4().hex, encoding="utf-8")

    def clear(self) -> None:
        """Remove the nonce once the order reaches a terminal state; missing is fine."""
        with contextlib.suppress(OSError):
            self.path.unlink()


class LaneLockRetryTracker:
    """Durable per-order attempt counter parked beside a deferred order.

    One sidecar per order id under ``deferred/`` counts transient failures
    (lane-lock timeouts, unconfirmed deliveries) across daemon restarts, so
    a retry budget survives crashes and bounds a permanently-busy lane.
    """

    def __init__(self, deferred_dir: Path) -> None:
        self.deferred_dir = deferred_dir

    def state_path(self, order_id: str) -> Path:
        """Return the sidecar path for ``order_id``.

        The sidecar deliberately has no ``.json`` suffix: normal deferred
        order scans only consider JSON order files, so this control record
        can never be mistaken for a dispatch order.
        """
        return self.deferred_dir / f".{order_id}.lane-lock-attempts"

    def attempts(self, order_id: str, *, retry_limit: int) -> int:
        """Read a retry count, failing closed if a manually-corrupt sidecar appears."""
        path = self.state_path(order_id)
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

    def record_attempt(self, order_id: str, *, retry_limit: int) -> int:
        """Atomically increment and persist one failure count."""
        attempts = self.attempts(order_id, retry_limit=retry_limit) + 1
        write_json_atomic(self.state_path(order_id), {"attempts": attempts})
        return attempts

    def clear(self, order_id: str) -> None:
        """Best-effort cleanup after a terminal result has made retry state moot."""
        with contextlib.suppress(OSError):
            self.state_path(order_id).unlink()

"""queue_state — typed claim/queue-state seam for ``chitra.dispatchd``.

Every filesystem fact a queue worker acts on has a name here: the fixed
subdirectory layout (``orders/``, ``in_flight/``, ``deferred/``,
``results/``, ``processed/``, ``invalid/``), the exclusive-create owner
marker that reserves an order before it is renamed into ``in_flight/``,
the stale-claim sweep that returns a dead worker's claim to ``orders/``
without ever stealing a live owner's, the send-nonce marker that gates
paste-free crash reconciliation, the durable lane-lock retry sidecar
parked next to a deferred order, the single-writer terminal finalization
that publishes a result once and moves the claimed order once, and the
FIFO deferred-requeue sweep that returns parked orders to ``orders/``.
``dispatchd`` composes these primitives into its delivery policy (lane
locks, freeze checks, ledger signing); this module owns only what the
on-disk state IS -- paths, markers, counters, and transitions -- so queue
state transitions are reviewable in one place.

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
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

from ._fsio import write_json_atomic
from .dispatch import _pid_alive

logger = structlog.get_logger(__name__)

_ORDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,191}\Z")


def _require_real_directory(path: Path, *, label: str) -> Path:
    """Return a queue directory only when it is real or safely absent."""
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{label} must be a directory: {path}")
    return path


def _require_safe_order_id(order_id: str) -> str:
    if _ORDER_ID_RE.fullmatch(order_id) is None:
        raise ValueError(f"unsafe order id: {order_id!r}")
    return order_id


def _require_real_file(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    return path


def _validate_pending_order_path(orders_dir: Path, pending_path: Path) -> None:
    """Require one real, direct child of the queue's orders directory."""
    _require_real_directory(orders_dir, label="orders directory")
    expected = orders_dir / pending_path.name
    if pending_path != expected:
        raise ValueError(f"order path must be an exact child of {orders_dir}: {pending_path}")
    if pending_path.is_symlink():
        raise ValueError(f"order path must not be a symlink: {pending_path}")


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

    def __post_init__(self) -> None:
        _require_real_directory(self.root, label="queue root")
        for subdir in QueueSubdir:
            _require_real_directory(self.root / subdir, label=f"queue {subdir.value} directory")

    def _subdir(self, subdir: QueueSubdir) -> Path:
        _require_real_directory(self.root, label="queue root")
        return _require_real_directory(self.root / subdir, label=f"queue {subdir.value} directory")

    @property
    def orders(self) -> Path:
        """Pending order files awaiting a claim."""
        return self._subdir(QueueSubdir.ORDERS)

    @property
    def in_flight(self) -> Path:
        """Claimed order files plus their owner markers and send nonces."""
        return self._subdir(QueueSubdir.IN_FLIGHT)

    @property
    def deferred(self) -> Path:
        """Parked orders (guard-held or lane-lock-retryable) plus retry sidecars."""
        return self._subdir(QueueSubdir.DEFERRED)

    @property
    def results(self) -> Path:
        """One DispatchResult JSON per delivered order id."""
        return self._subdir(QueueSubdir.RESULTS)

    @property
    def processed(self) -> Path:
        """Order files whose terminal result exists (SENT only after ledger proof)."""
        return self._subdir(QueueSubdir.PROCESSED)

    @property
    def invalid(self) -> Path:
        """Quarantine for order files that fail to parse."""
        return self._subdir(QueueSubdir.INVALID)

    def create(self) -> tuple[Path, Path, Path]:
        """Create every standard subdirectory; return ``(orders, results, processed)``."""
        for subdir in (self.orders, self.results, self.processed, self.in_flight, self.deferred):
            subdir.mkdir(parents=True, exist_ok=True)
        return self.orders, self.results, self.processed

    def result_path(self, order_id: str) -> Path:
        """Path of the result JSON for ``order_id``."""
        path = self.results / f"{_require_safe_order_id(order_id)}.json"
        return _require_real_file(path, label="result path")

    def owner_marker_path(self, order_id: str) -> Path:
        """Path of the claim-reservation marker for ``order_id``."""
        path = self.in_flight / f".{_require_safe_order_id(order_id)}.owner"
        return _require_real_file(path, label="owner marker path")

    def send_nonce_path(self, order_id: str) -> Path:
        """Path of the send-nonce crash marker for ``order_id``."""
        path = self.in_flight / f".{_require_safe_order_id(order_id)}.nonce"
        return _require_real_file(path, label="send nonce path")

    def send_nonce(self, order_id: str) -> SendNonce:
        """The send-nonce crash marker handle for ``order_id``."""
        return SendNonce(order_id=order_id, path=self.send_nonce_path(order_id))


@dataclass(frozen=True)
class QueueOrderArtifacts:
    """Read-only locations that already own one dispatch order identity."""

    order_id: str
    order_paths: tuple[Path, ...]
    result_path: Path | None

    @property
    def exists(self) -> bool:
        """Whether the queue or its result store already knows this order."""
        return bool(self.order_paths) or self.result_path is not None


def locate_order(layout: QueueLayout, order_id: str) -> QueueOrderArtifacts:
    """Locate one order across every durable queue state without mutating it."""
    if _ORDER_ID_RE.fullmatch(order_id) is None:
        raise ValueError(f"unsafe order id: {order_id!r}")
    paths = tuple(
        path
        for directory in (
            layout.orders,
            layout.in_flight,
            layout.deferred,
            layout.processed,
            layout.invalid,
        )
        if (path := directory / f"{order_id}.json").is_file()
    )
    result_path = layout.result_path(order_id)
    return QueueOrderArtifacts(
        order_id=order_id,
        order_paths=paths,
        result_path=result_path if result_path.is_file() else None,
    )


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
        _require_real_directory(self.in_flight_dir, label="in_flight directory")
        _validate_pending_order_path(self.in_flight_dir.parent / QueueSubdir.ORDERS, pending_path)
        claimed_path = self.in_flight_dir / pending_path.name
        if claimed_path.is_symlink():
            raise ValueError(f"claimed order path must not be a symlink: {claimed_path}")
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
    _require_safe_order_id(order_id)
    _require_real_directory(in_flight_dir, label="in_flight directory")
    in_flight_dir.mkdir(parents=True, exist_ok=True)
    marker_path = _require_real_file(in_flight_dir / f".{order_id}.owner", label="owner marker path")
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
            _move_without_replace(claimed, layout.orders / claimed.name)
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

    def _validate_path(self) -> None:
        _require_real_directory(self.path.parent, label="in_flight directory")
        if self.path.is_symlink():
            raise ValueError(f"send nonce path must not be a symlink: {self.path}")

    def exists(self) -> bool:
        """Whether a prior attempt left a nonce for this order."""
        self._validate_path()
        return self.path.exists()

    def mint(self) -> None:
        """Write this attempt's fresh random nonce."""
        self._validate_path()
        self.path.write_text(uuid.uuid4().hex, encoding="utf-8")

    def clear(self) -> None:
        """Remove the nonce once the order reaches a terminal state; missing is fine."""
        self._validate_path()
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
        _require_safe_order_id(order_id)
        _require_real_directory(self.deferred_dir, label="deferred directory")
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


def _move_without_replace(source: Path, target: Path) -> bool:
    """Move one queue file without ever replacing a peer's target.

    ``Path.replace`` is atomic, but it is also destructive when another
    worker has created ``target`` between an existence check and the rename.
    Queue subdirectories share one filesystem, so an exclusive hard-link
    followed by unlink gives us the needed no-clobber move. The source stays
    intact if the link loses the target race, and a failed unlink removes the
    link we just created before re-raising.

    Return ``False`` when ``target`` already exists. Let the caller decide
    whether that means "skip" or "already finalized".
    """
    if source.is_symlink() or target.is_symlink():
        raise ValueError(f"queue move paths must not be symlinks: {source}, {target}")
    try:
        os.link(source, target)
    except FileExistsError:
        return False
    try:
        source.unlink()
    except OSError:
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    return True


@dataclass(frozen=True)
class StoredResult:
    """Handle on the durable terminal result JSON for one order id.

    The result file is the queue's single-writer record of an order's
    outcome: whoever publishes it first wins, and no losing writer may
    clobber it. ``create_once`` enforces that with an exclusive hard-link
    create (the same primitive ``reserve_claim`` uses for owner markers);
    ``overwrite`` exists only for the narrow proof-bit updates dispatchd
    makes to a result it has already validated as its own.
    """

    order_id: str
    path: Path

    def _validate_path(self) -> None:
        _require_safe_order_id(self.order_id)
        _require_real_directory(self.path.parent, label="results directory")
        if self.path.is_symlink():
            raise ValueError(f"result path must not be a symlink: {self.path}")

    def exists(self) -> bool:
        """Whether a terminal result has been published for this order id."""
        self._validate_path()
        return self.path.exists()

    def read_payload(self) -> dict[str, Any] | None:
        """The stored result payload, or ``None`` when absent or unreadable."""
        self._validate_path()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def create_once(self, payload: Mapping[str, Any]) -> bool:
        """Publish this result unless one already exists; never overwrite.

        The temporary file is written completely in ``results/``, fsynced,
        then ``link`` makes the final name appear atomically -- so a crash
        mid-publish can never leave a partial result, and two workers racing
        to publish see exactly one winner. Returns ``True`` iff THIS call
        created the file; on ``FileExistsError`` the existing result stands
        and this call's payload is discarded.
        """
        self._validate_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.order_id}.json.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump(dict(payload), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary_path, self.path)
            return True
        except FileExistsError:
            return False
        finally:
            if temporary_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary_path)

    def overwrite(self, payload: Mapping[str, Any]) -> None:
        """Atomically replace an already-stored result (proof-bit flips).

        Only for callers that have already validated the stored result is
        theirs (matching order id and session) -- e.g. setting
        ``delivery_ledger_verified`` after retrying the ledger write. Byte-
        for-byte identical serialization to the historical result writer.
        """
        self._validate_path()
        write_json_atomic(
            self.path,
            dict(payload),
            trailing_newline=False,
            sort_keys=False,
            cleanup_on_error=False,
        )


@dataclass(frozen=True)
class TerminalFinalization:
    """The single-writer terminal transition for one claimed order.

    Binds everything one terminal outcome touches: the claimed order file
    in ``in_flight/``, the ``results/`` directory receiving its terminal
    result JSON, the destination directory (``processed/`` or ``invalid/``)
    receiving the order file itself, and the stale control markers (retry
    sidecar, send nonce) a terminal state makes moot. ``apply`` performs
    the durable steps in crash-safe order:

    1. publish the result unless one already exists -- a losing or repeated
       finalization never overwrites the published record;
    2. move the claimed order to its destination exactly once -- a claimed
       path that is already gone means a previous apply finished the move,
       which is success, not an error;
    3. clear the now-stale control markers, each missing-safe.

    Pass ``result_payload=None`` when the result JSON is already durable and
    only the move plus cleanup remain (crash recovery finishing a previous
    pass). Returns from ``apply`` whether THIS call published the result.
    """

    claimed_path: Path
    order_id: str
    results_dir: Path
    destination_dir: Path
    result_payload: Mapping[str, Any] | None
    retry_state_dir: Path | None = None
    nonce_path: Path | None = None
    suppress_move_errors: bool = False

    def stored_result(self) -> StoredResult:
        """The result-file handle this finalization would publish."""
        return StoredResult(order_id=self.order_id, path=self.results_dir / f"{self.order_id}.json")

    def apply(self) -> bool:
        """Execute the transition; returns True iff this call wrote the result."""
        _require_real_directory(self.results_dir, label="results directory")
        _require_real_directory(self.destination_dir, label="queue destination directory")
        if self.retry_state_dir is not None:
            _require_real_directory(self.retry_state_dir, label="deferred directory")
        wrote_result = False
        if self.result_payload is not None:
            wrote_result = self.stored_result().create_once(self.result_payload)
        self.destination_dir.mkdir(parents=True, exist_ok=True)
        destination_path = self.destination_dir / self.claimed_path.name
        try:
            moved = _move_without_replace(self.claimed_path, destination_path)
            if not moved:
                # A prior pass may have linked the same inode and died before
                # unlinking its source. Treat that half-complete move as
                # success, but never discard a different destination file.
                if self.claimed_path.exists() and destination_path.exists() and os.path.samefile(
                    self.claimed_path, destination_path
                ):
                    self.claimed_path.unlink()
                elif self.claimed_path.exists():
                    raise FileExistsError(destination_path)
        except FileNotFoundError:
            if not destination_path.exists():
                raise
            # The source disappeared after a prior successful link/unlink.
            # A durable destination is enough to complete an idempotent pass.
        except OSError:
            if not self.suppress_move_errors:
                raise
        if self.retry_state_dir is not None:
            LaneLockRetryTracker(self.retry_state_dir).clear(self.order_id)
        if self.nonce_path is not None:
            with contextlib.suppress(OSError):
                self.nonce_path.unlink()
        return wrote_result


@dataclass(frozen=True)
class RequeueOutcome:
    """What one deferred-requeue sweep did, in FIFO arrival order."""

    requeued: list[Path] = field(default_factory=list)
    """Moved order files, expressed as their new ``orders/`` paths, oldest first."""
    skipped_existing_target: list[Path] = field(default_factory=list)
    """Sources left parked because ``orders/`` already holds a file of that name."""
    failed: list[Path] = field(default_factory=list)
    """Sources whose rename failed with an unexpected OS error."""


def requeue_deferred_to_orders(
    deferred_dir: Path,
    orders_dir: Path,
    *,
    eligible: Callable[[Path], bool] | None = None,
) -> RequeueOutcome:
    """Atomically return eligible deferred orders to ``orders/``, FIFO by arrival.

    Eligibility (which deferred orders belong in this sweep) is the caller's
    policy, decided by ``eligible``; everything else here is mechanics: the
    surviving files are sorted by ``(mtime_ns, inode)`` -- a rename never
    changes either, so original arrival order survives every prior move --
    then renamed back to ``orders/`` oldest first. An order whose name
    already exists under ``orders/`` is skipped rather than clobbering the
    pending work already there. Retry sidecars are dot-prefixed control
    records, never matched by the ``*.json`` scan, so a requeued order's
    durable attempt count survives untouched until its next attempt resolves.
    """
    dated: list[tuple[int, int, Path]] = []
    for path in deferred_dir.glob("*.json"):
        if eligible is not None and not eligible(path):
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        dated.append((stat.st_mtime_ns, stat.st_ino, path))
    dated.sort(key=lambda item: item[:2])

    outcome = RequeueOutcome()
    for _, _, path in dated:
        target = orders_dir / path.name
        if target.exists():
            outcome.skipped_existing_target.append(path)
            continue
        try:
            moved = _move_without_replace(path, target)
        except OSError:
            outcome.failed.append(path)
            continue
        if moved:
            outcome.requeued.append(target)
        else:
            outcome.skipped_existing_target.append(path)
    return outcome

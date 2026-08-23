"""Tests for chitra.queue_state: the typed claim/queue-state seam.

These cover the seam primitives at the level dispatchd composes them from:
layout paths, exclusive-create claim reservations, stale-claim reclaim,
send nonces, and the durable lane-lock retry sidecar. The end-to-end
behaviors built on top of them (crash recovery, retries under run_once,
concurrent workers) stay covered by tests/test_dispatchd.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chitra.queue_state import (
    LaneLockRetryTracker,
    QueueLayout,
    SendNonce,
    read_owner_pid,
    reclaim_stale_claims,
    reserve_claim,
)

# A pid essentially guaranteed not to be alive on any real system (the same
# convention tests/test_dispatchd.py uses).
_DEAD_PID = "999999999"


def _write_pending(orders_dir: Path, name: str, content: str = "{}") -> Path:
    orders_dir.mkdir(parents=True, exist_ok=True)
    path = orders_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def test_layout_maps_the_documented_queue_subdirectories(tmp_path: Path) -> None:
    layout = QueueLayout(tmp_path)

    assert layout.root == tmp_path
    assert layout.orders == tmp_path / "orders"
    assert layout.in_flight == tmp_path / "in_flight"
    assert layout.deferred == tmp_path / "deferred"
    assert layout.results == tmp_path / "results"
    assert layout.processed == tmp_path / "processed"
    assert layout.invalid == tmp_path / "invalid"


def test_layout_create_makes_every_standard_directory_and_returns_dispatchds_tuple(tmp_path: Path) -> None:
    layout = QueueLayout(tmp_path)

    created = layout.create()

    assert created == (tmp_path / "orders", tmp_path / "results", tmp_path / "processed")
    for subdir in (layout.orders, layout.results, layout.processed, layout.in_flight, layout.deferred):
        assert subdir.is_dir()


def test_layout_control_file_paths_match_the_on_disk_names_dispatchd_has_always_used(tmp_path: Path) -> None:
    """The dot-prefixed control-file names are load-bearing: older and newer
    workers must interoperate on one queue directory without migration."""
    layout = QueueLayout(tmp_path)

    assert layout.result_path("ord-1") == tmp_path / "results" / "ord-1.json"
    assert layout.owner_marker_path("ord-1") == tmp_path / "in_flight" / ".ord-1.owner"
    assert layout.send_nonce_path("ord-1") == tmp_path / "in_flight" / ".ord-1.nonce"


def test_reserve_claim_writes_an_exclusive_owner_marker_with_this_pid(tmp_path: Path) -> None:
    in_flight_dir = tmp_path / "in_flight"
    in_flight_dir.mkdir()

    reservation = reserve_claim(in_flight_dir, "ord-1")

    assert reservation is not None
    assert reservation.marker_path == in_flight_dir / ".ord-1.owner"
    assert int(reservation.marker_path.read_text(encoding="utf-8")) == os.getpid()


def test_second_reservation_of_a_live_claim_loses_the_race_and_skips(tmp_path: Path) -> None:
    """Two workers racing the same order: exclusive create means exactly one
    wins the reservation; the loser must skip rather than double-claim."""
    in_flight_dir = tmp_path / "in_flight"
    in_flight_dir.mkdir()

    first = reserve_claim(in_flight_dir, "ord-race")
    second = reserve_claim(in_flight_dir, "ord-race")

    assert first is not None
    assert second is None


def test_claim_renames_the_pending_order_into_in_flight_under_the_reservation(tmp_path: Path) -> None:
    in_flight_dir = tmp_path / "in_flight"
    in_flight_dir.mkdir()
    pending = _write_pending(tmp_path / "orders", "ord-1.json", '{"order_id": "ord-1"}')
    reservation = reserve_claim(in_flight_dir, "ord-1")
    assert reservation is not None

    claimed = reservation.claim(pending)

    assert claimed == in_flight_dir / "ord-1.json"
    assert claimed.exists()
    assert not pending.exists()


def test_release_removes_the_marker_and_is_safe_to_repeat(tmp_path: Path) -> None:
    in_flight_dir = tmp_path / "in_flight"
    in_flight_dir.mkdir()
    reservation = reserve_claim(in_flight_dir, "ord-1")
    assert reservation is not None

    reservation.release()
    assert not reservation.marker_path.exists()
    reservation.release()  # idempotent cleanup must never raise


def test_reclaim_returns_a_dead_owners_claim_to_orders_without_touching_a_live_one(
    tmp_path: Path,
) -> None:
    """The core safety property: reclaim recovers abandoned work but must
    never steal an order out from under a live worker's claim."""
    layout = QueueLayout(tmp_path)
    layout.create()  # run_once creates the queue tree before any reclaim pass
    dead_claim = layout.in_flight / "dead.json"
    dead_claim.write_text("{}", encoding="utf-8")
    layout.owner_marker_path("dead").write_text(_DEAD_PID, encoding="utf-8")
    live_claim = layout.in_flight / "live.json"
    live_claim.write_text("{}", encoding="utf-8")
    layout.owner_marker_path("live").write_text(str(os.getpid()), encoding="utf-8")

    reclaim_stale_claims(layout)

    assert (layout.orders / "dead.json").exists()
    assert not dead_claim.exists()
    assert not layout.owner_marker_path("dead").exists()
    # The live worker keeps its claim and its marker.
    assert live_claim.exists()
    assert layout.owner_marker_path("live").exists()
    assert not (layout.orders / "live.json").exists()


def test_reclaim_treats_a_missing_or_corrupt_owner_marker_as_abandoned(tmp_path: Path) -> None:
    """Fail-safe reading: unreadable ownership means recoverable work, never
    a stranded order."""
    layout = QueueLayout(tmp_path)
    layout.create()  # run_once creates the queue tree before any reclaim pass
    orphaned = layout.in_flight / "orphaned.json"
    orphaned.write_text("{}", encoding="utf-8")
    corrupted = layout.in_flight / "corrupted.json"
    corrupted.write_text("{}", encoding="utf-8")
    layout.owner_marker_path("corrupted").write_text("not-a-pid", encoding="utf-8")

    assert read_owner_pid(layout.owner_marker_path("orphaned")) == 0
    assert read_owner_pid(layout.owner_marker_path("corrupted")) == 0

    reclaim_stale_claims(layout)

    assert (layout.orders / "orphaned.json").exists()
    assert (layout.orders / "corrupted.json").exists()


def test_reclaim_clears_a_dead_pre_rename_reservation_but_keeps_a_live_one(tmp_path: Path) -> None:
    """A crash between creating the marker and renaming the order leaves an
    orphan marker; it must not block the still-pending order forever -- while
    a live owner's short pre-rename window stays protected."""
    layout = QueueLayout(tmp_path)
    layout.in_flight.mkdir(parents=True)
    dead_marker = layout.owner_marker_path("pending-dead")
    dead_marker.write_text(_DEAD_PID, encoding="utf-8")
    live_marker = layout.owner_marker_path("pending-live")
    live_marker.write_text(str(os.getpid()), encoding="utf-8")
    _write_pending(layout.orders, "pending-dead.json")
    _write_pending(layout.orders, "pending-live.json")

    reclaim_stale_claims(layout)

    assert not dead_marker.exists()
    assert live_marker.exists()
    assert (layout.orders / "pending-dead.json").exists()
    assert (layout.orders / "pending-live.json").exists()


def test_reclaim_leaves_an_active_claims_marker_alone_during_the_orphan_sweep(tmp_path: Path) -> None:
    """A marker whose order file is present in ``in_flight/`` belongs to the
    active-claim loop above; the orphan sweep must skip it even when that
    loop has just moved the claim away mid-scan semantics would allow."""
    layout = QueueLayout(tmp_path)
    layout.in_flight.mkdir(parents=True)
    active_marker = layout.owner_marker_path("active")
    active_marker.write_text(str(os.getpid()), encoding="utf-8")
    (layout.in_flight / "active.json").write_text("{}", encoding="utf-8")

    reclaim_stale_claims(layout)

    assert active_marker.exists()
    assert (layout.in_flight / "active.json").exists()


def test_send_nonce_round_trip_and_absent_clear(tmp_path: Path) -> None:
    nonce = SendNonce(order_id="ord-1", path=tmp_path / ".ord-1.nonce")

    assert not nonce.exists()
    nonce.mint()
    assert nonce.exists()
    assert nonce.path.read_text(encoding="utf-8")  # a fresh random nonce
    nonce.clear()
    assert not nonce.exists()
    nonce.clear()  # clearing an already-gone nonce is silent


def test_retry_tracker_reads_zero_when_no_sidecar_exists(tmp_path: Path) -> None:
    tracker = LaneLockRetryTracker(tmp_path)

    assert tracker.state_path("ord-1") == tmp_path / ".ord-1.lane-lock-attempts"
    assert tracker.attempts("ord-1", retry_limit=20) == 0


def test_retry_tracker_increments_and_persists_across_instances(tmp_path: Path) -> None:
    """Durability contract: a fresh tracker (the next daemon pass or restart)
    reads what the previous one recorded."""
    writer = LaneLockRetryTracker(tmp_path)
    reader = LaneLockRetryTracker(tmp_path)

    first = writer.record_attempt("ord-1", retry_limit=20)
    second = writer.record_attempt("ord-1", retry_limit=20)

    assert first == 1
    assert second == 2
    assert reader.attempts("ord-1", retry_limit=20) == 2
    assert json.loads(writer.state_path("ord-1").read_text(encoding="utf-8")) == {"attempts": 2}


@pytest.mark.parametrize("corrupt_payload", ['{"attempts": true}', '{"attempts": -3}', '{"attempts": "many"}', "not json"])
def test_retry_tracker_fails_closed_on_a_corrupt_sidecar(tmp_path: Path, corrupt_payload: str) -> None:
    """An externally corrupted record is treated as exhausted (retry_limit),
    never reset to zero -- resetting would allow an unbounded retry loop."""
    tracker = LaneLockRetryTracker(tmp_path)
    tracker.state_path("ord-1").write_text(corrupt_payload, encoding="utf-8")

    assert tracker.attempts("ord-1", retry_limit=7) == 7


def test_retry_tracker_clear_removes_the_sidecar_and_is_safe_to_repeat(tmp_path: Path) -> None:
    tracker = LaneLockRetryTracker(tmp_path)
    tracker.record_attempt("ord-1", retry_limit=20)

    tracker.clear("ord-1")

    assert not tracker.state_path("ord-1").exists()
    assert tracker.attempts("ord-1", retry_limit=20) == 0
    tracker.clear("ord-1")  # idempotent

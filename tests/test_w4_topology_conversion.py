from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import cast

import pytest

from tools.convert import (
    ConversionError,
    WriterObservation,
    build_authority_handoff_receipt,
    convert_state_root,
    convert_w10_snapshot,
    restore_snapshot,
    run_shadow_scan,
    snapshot_state_root,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _legacy_state(root: Path) -> None:
    _write_json(
        root / "goals.json",
        {
            "schema": "chitra.goals.v1",
            "updated_at": "2026-08-21T00:00:00+00:00",
            "goals": [
                {
                    "session_ref": "host:lane-a:%1",
                    "goal": "finish one scoped migration task safely",
                    "done_when": "the migration receipt exists",
                    "source": "task-file:test",
                    "status": "working",
                    "now": "",
                    "last_verified": "",
                    "created_at": "2026-08-21T00:00:00+00:00",
                    "updated_at": "2026-08-21T00:00:00+00:00",
                },
                {
                    "session_ref": "host:lane-b:%2",
                    "goal": "preserve terminal legacy status for display",
                    "done_when": "the old status remains visible",
                    "source": "task-file:test",
                    "status": "done-pending-close",
                    "now": "",
                    "last_verified": "",
                    "created_at": "2026-08-21T00:00:00+00:00",
                    "updated_at": "2026-08-21T00:00:00+00:00",
                },
            ],
        },
    )
    (root / "queue.tsv").write_text("1\thost:lane-a:%1\tfirst\n2\thost:lane-b:%2\tsecond\n", encoding="utf-8")
    _write_json(root / "queue" / "orders" / "order-3.json", {"order_id": "order-3", "session_ref": "host:lane-c:%3"})
    _write_json(root / "queue" / "results" / "order-1.json", {"order_id": "order-1", "status": "sent"})


def test_conversion_reconciles_counts_hashes_and_marks_legacy_records_not_done(tmp_path: Path) -> None:
    state = tmp_path / "state"
    out = tmp_path / "out"
    _legacy_state(state)

    receipt = convert_state_root(state, out)
    goals = json.loads((out / "legacy-goals-conversion.json").read_text(encoding="utf-8"))
    queue = json.loads((out / "dispatch-queue-replay.json").read_text(encoding="utf-8"))

    assert receipt["reconciliation"] == {
        "goal_counts_match": True,
        "queue_count_order_identity_hashes_preserved": True,
        "legacy_goals_marked_display_dispose_only": True,
        "legacy_goals_may_claim_done": False,
        "status": "PASS",
    }
    assert receipt["input_counts"] == {"legacy_goals": 2, "dispatch_queue_entries": 3}
    assert goals["output_counts"]["legacy_goal_records"] == 2
    assert all(record["disposition"]["done_transition_allowed"] is False for record in goals["records"])
    assert all(record["disposition"]["completion_eligible"] is False for record in goals["records"])
    assert [entry["relative_path"] for entry in queue["entries"]] == [
        "queue.tsv",
        "queue/orders/order-3.json",
        "queue/results/order-1.json",
    ]
    assert [entry["ordinal"] for entry in queue["entries"]] == [0, 1, 2]


def test_w10_snapshot_metadata_reconciles_expected_legacy_topology(tmp_path: Path) -> None:
    snapshot = Path("/mnt/opshome/scratch/roundtop/chitra-parallel-program-20260821/reports/w10-topology-temp-twinridge.json")
    if not snapshot.exists():
        pytest.skip("authoritative W10 snapshot is available in program workspace")

    receipt = convert_w10_snapshot(snapshot, tmp_path / "w10-out")

    assert receipt["mode"] == "w10-snapshot"
    input_counts = cast(dict[str, object], receipt["input_counts"])
    output_counts = cast(dict[str, object], receipt["output_counts"])
    reconciliation = cast(dict[str, object], receipt["reconciliation"])
    assert input_counts["legacy_goals"] == 27
    assert output_counts["legacy_goal_records"] == 27
    assert reconciliation["goal_counts_match"] is True
    queue = json.loads((tmp_path / "w10-out" / "dispatch-queue-replay.json").read_text(encoding="utf-8"))
    queue_entries = cast(list[dict[str, object]], queue["entries"])
    assert {entry["sha256"] for entry in queue_entries if entry["relative_path"] == "queue.tsv"} == {
        "928b85f9fa176dc4faafe3c649a0a81bb25d80dabe3a9826ae074ba737f3c8c3",
        "82efb72affc7cee21c4299205cb1a5c466c3163c9ab2bc9b5a89ea8d82d3c2b3",
    }


def test_shadow_handoff_and_rollback_prove_no_dispatch_one_writer_and_no_state_loss(tmp_path: Path) -> None:
    state = tmp_path / "state"
    shadow = tmp_path / "shadow"
    snapshot = tmp_path / "snapshot"
    _legacy_state(state)

    findings = run_shadow_scan(state, shadow)
    assert findings["dispatch_attempted"] is False
    finding_items = cast(list[dict[str, object]], findings["findings"])
    assert {finding["code"] for finding in finding_items} == {"legacy_goals_display_only", "dispatch_state_replay_required"}
    with pytest.raises(ConversionError, match="shadow output"):
        run_shadow_scan(state, state / "shadow")

    old_writer = WriterObservation(
        name="old-dispatchd",
        role="old",
        unit="chitra-dispatchd.service",
        process="pid:100",
        package="chitra 0.11.0",
        stopped=True,
        started=False,
        can_write=False,
        last_order_sha256="a" * 64,
    )
    new_writer = WriterObservation(
        name="new-dispatchd",
        role="new",
        unit="polyphony-chitra-dispatchd@monitor.service",
        process="pid:200",
        package="chitra 0.14.10",
        stopped=False,
        started=True,
        can_write=True,
        action_receipt_sha256="b" * 64,
    )
    handoff = build_authority_handoff_receipt(
        instance="monitor",
        old_writer=old_writer,
        new_writer=new_writer,
        pre_state_sha256="c" * 64,
        post_state_sha256="d" * 64,
        rollback_checkpoint_sha256="e" * 64,
    )
    assert handoff["exactly_one_writer"] is True
    old_writer_still_active = WriterObservation(
        name="old-dispatchd",
        role="old",
        unit="chitra-dispatchd.service",
        process="pid:100",
        package="chitra 0.11.0",
        stopped=True,
        started=False,
        can_write=True,
        last_order_sha256="a" * 64,
    )
    with pytest.raises(ConversionError, match="old writer can still write"):
        build_authority_handoff_receipt(
            instance="monitor",
            old_writer=old_writer_still_active,
            new_writer=new_writer,
            pre_state_sha256="c" * 64,
            post_state_sha256="d" * 64,
            rollback_checkpoint_sha256="e" * 64,
        )

    checkpoint = snapshot_state_root(state, snapshot)
    (state / "goals.json").write_text("changed", encoding="utf-8")
    shutil.rmtree(state / "queue")
    rollback = restore_snapshot(snapshot, state)
    assert rollback["status"] == "PASS"
    assert rollback["restored_manifest_sha256"] == checkpoint["manifest_sha256"]
    assert (state / "queue" / "orders" / "order-3.json").exists()

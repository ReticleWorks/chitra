from __future__ import annotations

import json
import shutil
import tomllib
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


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        "metadata_preflight_only": False,
        "w1_journal_records_bound": True,
        "w2_enrollment_records_bound": True,
        "queue_count_order_identity_hashes_preserved": True,
        "legacy_goals_marked_display_dispose_only": True,
        "legacy_goals_may_claim_done": False,
        "status": "PASS",
    }
    assert receipt["input_counts"] == {"legacy_goals": 2, "dispatch_queue_entries": 4}
    assert goals["output_counts"]["legacy_goal_records"] == 2
    assert all(isinstance(record["w1_journal_record"], dict) for record in goals["records"])
    assert all(isinstance(record["w2_enrollment_record"], dict) for record in goals["records"])
    assert all(record["disposition"]["done_transition_allowed"] is False for record in goals["records"])
    assert all(record["disposition"]["completion_eligible"] is False for record in goals["records"])
    assert [entry["relative_path"] for entry in queue["entries"]] == [
        "queue.tsv",
        "queue.tsv",
        "queue/orders/order-3.json",
        "queue/results/order-1.json",
    ]
    assert [entry["ordinal"] for entry in queue["entries"]] == [0, 1, 2, 3]
    assert [entry["identity"] for entry in queue["entries"]] == ["1", "2", "order-3", "order-1"]
    artifacts = cast(dict[str, dict[str, str]], receipt["artifacts"])
    assert artifacts["legacy_goals_conversion"]["sha256"] == _sha256_file(out / "legacy-goals-conversion.json")
    assert artifacts["dispatch_queue_replay"]["sha256"] == _sha256_file(out / "dispatch-queue-replay.json")


def test_w10_snapshot_metadata_reconciles_expected_legacy_topology(tmp_path: Path) -> None:
    snapshot = Path("/mnt/opshome/scratch/roundtop/chitra-parallel-program-20260821/reports/w10-topology-temp-twinridge.json")
    if not snapshot.exists():
        pytest.skip("authoritative W10 snapshot is available in program workspace")

    receipt = convert_w10_snapshot(snapshot, tmp_path / "w10-out")

    assert receipt["mode"] == "w10-topology-preflight"
    input_counts = cast(dict[str, object], receipt["input_counts"])
    output_counts = cast(dict[str, object], receipt["output_counts"])
    reconciliation = cast(dict[str, object], receipt["reconciliation"])
    assert input_counts["legacy_goals"] == 0
    assert output_counts["legacy_goal_records"] == 0
    assert reconciliation["metadata_preflight_only"] is True
    assert reconciliation["status"] == "PREFLIGHT_ONLY"
    goals = json.loads((tmp_path / "w10-out" / "legacy-goals-conversion.json").read_text(encoding="utf-8"))
    assert goals["input_counts"]["metadata_goal_count"] == 27
    assert len(goals["goal_file_manifests"]) == 2
    assert goals["records"] == []
    queue = json.loads((tmp_path / "w10-out" / "dispatch-queue-replay.json").read_text(encoding="utf-8"))
    queue_entries = cast(list[dict[str, object]], queue["entries"])
    assert {entry["sha256"] for entry in queue_entries if entry["relative_path"] == "queue.tsv"} == {
        "928b85f9fa176dc4faafe3c649a0a81bb25d80dabe3a9826ae074ba737f3c8c3",
        "82efb72affc7cee21c4299205cb1a5c466c3163c9ab2bc9b5a89ea8d82d3c2b3",
    }


def _instance_bindings() -> dict[str, str]:
    return {
        "namespace": "monitor",
        "state_root": "/var/lib/polyphony-chitra",
        "goals_sha256": "1" * 64,
        "queue_sha256": "2" * 64,
        "old_unit": "chitra-dispatchd.service",
        "new_unit": "polyphony-chitra-dispatchd@monitor.service",
        "old_process": "pid:100",
        "new_process": "pid:200",
        "old_package": "chitra 0.11.0",
        "new_package": "chitra 0.14.10",
        "old_tmux_socket": "/tmp/old.sock",
        "new_tmux_socket": "/tmp/new.sock",
        "lane_worktrees_sha256": "3" * 64,
        "last_old_order_sha256": "a" * 64,
        "last_old_event_sha256": "4" * 64,
        "pre_state_sha256": "c" * 64,
        "post_state_sha256": "d" * 64,
        "rollback_checkpoint_sha256": "e" * 64,
    }


def _lifecycle_receipts() -> list[dict[str, object]]:
    return [
        {
            "kind": kind,
            "observer": "external-test-harness",
            "subject": f"monitor:{kind}",
            "observed_at": "2026-08-22T00:00:00+00:00",
            "artifact_sha256": hex_digit * 64,
        }
        for kind, hex_digit in (
            ("old_drained", "5"),
            ("old_stopped", "6"),
            ("old_write_denied", "7"),
            ("new_started", "8"),
            ("new_write_proved", "9"),
        )
    ]


def _native_client() -> dict[str, object]:
    return {
        "client_name": "claude",
        "version": "2.1.238",
        "path": "/usr/local/bin/claude",
        "path_sha256": "b" * 64,
        "update_suppression": False,
    }


def test_shadow_scan_never_writes_inside_live_state_or_dispatches(tmp_path: Path) -> None:
    state = tmp_path / "state"
    shadow = tmp_path / "shadow"
    _legacy_state(state)

    findings = run_shadow_scan(state, shadow)
    assert findings["dispatch_attempted"] is False
    finding_items = cast(list[dict[str, object]], findings["findings"])
    assert {finding["code"] for finding in finding_items} == {"legacy_goals_display_only", "dispatch_state_replay_required"}
    with pytest.raises(ConversionError, match="shadow output"):
        run_shadow_scan(state, state / "shadow")


def test_authority_handoff_rejects_blank_caller_assertions_and_requires_lifecycle_bindings() -> None:
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
        transcript_bindings=("f" * 64,),
        instance_bindings=_instance_bindings(),
        lifecycle_receipts=_lifecycle_receipts(),
        native_client=_native_client(),
    )
    assert handoff["exactly_one_writer"] is True
    blank_writer = WriterObservation(name="", role="old", unit="", process="", package="", stopped=True, started=False, can_write=False)
    with pytest.raises(ConversionError, match="missing|transcript|native|lifecycle"):
        build_authority_handoff_receipt(
            instance="",
            old_writer=blank_writer,
            new_writer=new_writer,
            pre_state_sha256="",
            post_state_sha256="",
            rollback_checkpoint_sha256="",
        )
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
            transcript_bindings=("f" * 64,),
            instance_bindings=_instance_bindings(),
            lifecycle_receipts=_lifecycle_receipts(),
            native_client=_native_client(),
        )


def test_rollback_validates_snapshot_before_touching_destination_and_refuses_v2_evidence_loss(tmp_path: Path) -> None:
    state = tmp_path / "state"
    snapshot = tmp_path / "snapshot"
    _legacy_state(state)

    checkpoint = snapshot_state_root(state, snapshot)
    (state / "goals.json").write_text("changed", encoding="utf-8")
    shutil.rmtree(state / "queue")
    rollback = restore_snapshot(snapshot, state)
    assert rollback["status"] == "PASS"
    assert rollback["restored_manifest_sha256"] == checkpoint["manifest_sha256"]
    assert (state / "queue" / "orders" / "order-3.json").exists()

    enrolled = {
        "schema": "chitra.goals.v2",
        "updated_at": "2026-08-22T00:00:00+00:00",
        "goals": [
            {
                "session_ref": "host:lane-a:%1",
                "goal": "keep enrolled state",
                "done_when": "receipt exists",
                "source": "test",
                "status": "working",
                "created_at": "2026-08-22T00:00:00+00:00",
                "updated_at": "2026-08-22T00:00:00+00:00",
                "now": "",
                "last_verified": "",
                "interview_receipt": {"name": "enroll", "completed_at": "2026-08-22T00:00:00+00:00"},
                "enrolled_done_when_items": [
                    {"id": "item", "text": "receipt exists", "validator": "pytest", "required_receipt": "tests-green"}
                ],
            }
        ],
    }
    _write_json(state / "goals.json", enrolled)
    with pytest.raises(ConversionError, match="v2/v3 enrollment"):
        restore_snapshot(snapshot, state)
    assert json.loads((state / "goals.json").read_text(encoding="utf-8"))["schema"] == "chitra.goals.v2"

    corrupt_state = tmp_path / "corrupt-state"
    corrupt_snapshot = tmp_path / "corrupt-snapshot"
    _legacy_state(corrupt_state)
    snapshot_state_root(corrupt_state, corrupt_snapshot)
    (corrupt_snapshot / "state-root" / "goals.json").write_text("corrupt", encoding="utf-8")
    before = (corrupt_state / "goals.json").read_text(encoding="utf-8")
    with pytest.raises(ConversionError, match="manifest"):
        restore_snapshot(corrupt_snapshot, corrupt_state)
    assert (corrupt_state / "goals.json").read_text(encoding="utf-8") == before


def test_converter_is_packaged_and_exposed_as_entrypoint() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "tools" in pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert pyproject["project"]["scripts"]["chitra-convert"] == "tools.convert.topology:main"

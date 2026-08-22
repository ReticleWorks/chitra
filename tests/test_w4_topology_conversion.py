from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import tools.convert.topology as topology
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


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _file_manifest(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat_result = path.stat()
        entries.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "sha256": _sha256_file(path),
                "size": stat_result.st_size,
                "mode": oct(stat_result.st_mode & 0o777),
            }
        )
    return entries


def _manifest_digest(root: Path) -> str:
    return _sha256_bytes(_canonical_bytes(_file_manifest(root)))


def _canonical_event_model() -> type[Any]:
    return cast(type[Any], import_module("chitra.journal.models").CanonicalEvent)


def _goal_record_model() -> type[Any]:
    return cast(type[Any], import_module("chitra.goals").GoalRecord)


def _ledger_module() -> Any:
    return import_module("chitra.ledger")


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
    assert [_canonical_event_model().model_validate(record["w1_journal_record"]).schema_name for record in goals["records"]] == [
        "chitra.journal.event.v1",
        "chitra.journal.event.v1",
    ]
    assert [_goal_record_model().from_dict(record["w2_enrollment_record"]).session_ref for record in goals["records"]] == [
        "host:lane-a:%1",
        "host:lane-b:%2",
    ]
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


def _instance_bindings(state_root: Path, old_socket: Path, new_socket: Path, old_process: str, new_process: str) -> dict[str, str]:
    _write_json(
        state_root / "goals.json",
        {
            "schema": "chitra.goals.v3",
            "updated_at": "2026-08-22T00:00:00+00:00",
            "goals": [
                {
                    "session_ref": "authority:monitor",
                    "goal": "complete the governed authority handoff",
                    "done_when": "the signed handoff proof verifies",
                    "source": "task-file:W4f",
                    "status": "working",
                    "created_at": "2026-08-22T00:00:00+00:00",
                    "updated_at": "2026-08-22T00:00:00+00:00",
                    "now": "",
                    "last_verified": "",
                    "interview_receipt": {"name": "authority-enrollment", "completed_at": "2026-08-22T00:00:00+00:00"},
                    "enrolled_at": "2026-08-22T00:00:00+00:00",
                    "enrolled_done_when_items": [
                        {
                            "id": "signed-handoff",
                            "text": "the signed handoff proof verifies",
                            "validator": "chitra-authority-verifier",
                            "required_receipt": "authority-ledger",
                        }
                    ],
                }
            ],
        },
    )
    _write_json(state_root / "queue" / "orders" / "order-1.json", {"order_id": "order-1", "session_ref": "host:monitor:%1"})
    _write_json(state_root / "lane-worktrees.json", {"monitor": str(state_root / "worktree")})
    _write_json(state_root / "handoff" / "last-old-order.json", {"order_id": "order-1", "status": "drained"})
    _write_json(state_root / "handoff" / "last-old-event.json", {"event": "old-write-denied"})
    _write_json(state_root / "handoff" / "new-action-receipt.json", {"event": "new-write-proved"})
    _write_json(state_root / "handoff" / "pre-state-manifest.json", {"manifest_sha256": "pre"})
    _write_json(state_root / "handoff" / "rollback-checkpoint.json", {"manifest_sha256": "rollback"})
    transcript = state_root / "transcripts" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"type":"event","message":"observed"}\n', encoding="utf-8")
    observed_exe = topology._process_executable_sha256(new_process)
    new_exe_path, new_exe_sha = observed_exe or (
        Path(sys.executable).resolve(),
        _sha256_file(Path(sys.executable)),
    )

    return {
        "namespace": "monitor",
        "state_root": str(state_root),
        "goals_path": str(state_root / "goals.json"),
        "goals_sha256": _sha256_file(state_root / "goals.json"),
        "queue_root": str(state_root / "queue"),
        "queue_sha256": _manifest_digest(state_root / "queue"),
        "old_unit": "chitra-dispatchd.service",
        "new_unit": "polyphony-chitra-dispatchd@monitor.service",
        "old_process": old_process,
        "new_process": new_process,
        "old_package": "chitra 0.11.0",
        "new_package": "chitra 0.14.10",
        "old_tmux_socket": str(old_socket),
        "new_tmux_socket": str(new_socket),
        "lane_worktrees_path": str(state_root / "lane-worktrees.json"),
        "lane_worktrees_sha256": _sha256_file(state_root / "lane-worktrees.json"),
        "last_old_order_path": str(state_root / "handoff" / "last-old-order.json"),
        "last_old_order_sha256": _sha256_file(state_root / "handoff" / "last-old-order.json"),
        "last_old_event_path": str(state_root / "handoff" / "last-old-event.json"),
        "last_old_event_sha256": _sha256_file(state_root / "handoff" / "last-old-event.json"),
        "new_action_receipt_path": str(state_root / "handoff" / "new-action-receipt.json"),
        "new_action_receipt_sha256": _sha256_file(state_root / "handoff" / "new-action-receipt.json"),
        "pre_state_manifest_path": str(state_root / "handoff" / "pre-state-manifest.json"),
        "pre_state_sha256": _sha256_file(state_root / "handoff" / "pre-state-manifest.json"),
        "rollback_checkpoint_path": str(state_root / "handoff" / "rollback-checkpoint.json"),
        "rollback_checkpoint_sha256": _sha256_file(state_root / "handoff" / "rollback-checkpoint.json"),
        "post_state_sha256": topology._operational_state_manifest_digest(state_root),
        "new_process_exe_path": str(new_exe_path),
        "new_process_exe_sha256": new_exe_sha,
    }


def _lifecycle_receipts(tmp_path: Path, bindings: dict[str, str]) -> list[dict[str, object]]:
    receipts: list[dict[str, object]] = []
    post_state_sha256 = bindings["post_state_sha256"]
    for kind, role, unit_key, process_key, package_key, socket_key, process_alive, evidence_key, booleans in (
        (
            "old_drained",
            "old",
            "old_unit",
            "old_process",
            "old_package",
            "old_tmux_socket",
            False,
            "last_old_order_sha256",
            {"stopped": False, "started": False, "can_write": False},
        ),
        (
            "old_stopped",
            "old",
            "old_unit",
            "old_process",
            "old_package",
            "old_tmux_socket",
            False,
            "pre_state_sha256",
            {"stopped": True, "started": False, "can_write": False},
        ),
        (
            "old_write_denied",
            "old",
            "old_unit",
            "old_process",
            "old_package",
            "old_tmux_socket",
            False,
            "last_old_event_sha256",
            {"stopped": True, "started": False, "can_write": False},
        ),
        (
            "new_started",
            "new",
            "new_unit",
            "new_process",
            "new_package",
            "new_tmux_socket",
            True,
            "post_state_sha256",
            {"stopped": False, "started": True, "can_write": True},
        ),
        (
            "new_write_proved",
            "new",
            "new_unit",
            "new_process",
            "new_package",
            "new_tmux_socket",
            True,
            "new_action_receipt_sha256",
            {"stopped": False, "started": True, "can_write": True},
        ),
    ):
        artifact = {
            "schema": "chitra.authority-handoff-lifecycle-proof.v1",
            "kind": kind,
            "observer": "chitra-authority-verifier",
            "subject": f"monitor:{role}:{bindings[unit_key]}:{kind}",
            "observed_at": "2026-08-22T00:00:00+00:00",
            "instance": "monitor",
            "writer_role": role,
            "unit": bindings[unit_key],
            "process": bindings[process_key],
            "package": bindings[package_key],
            "state_root": bindings["state_root"],
            "tmux_socket": bindings[socket_key],
            "process_alive": process_alive,
            "state_root_sha256": post_state_sha256,
            "evidence_sha256": bindings[evidence_key],
            **booleans,
        }
        path = tmp_path / "lifecycle" / f"{kind}.json"
        _write_json(path, artifact)
        receipts.append(
            {
                "kind": kind,
                "observer": artifact["observer"],
                "subject": artifact["subject"],
                "observed_at": artifact["observed_at"],
                "artifact_path": str(path),
                "artifact_sha256": _sha256_file(path),
            }
        )
    return receipts


def _native_client(tmp_path: Path) -> dict[str, object]:
    client_path = topology._approved_native_client_path("codex")
    if client_path is None:
        pytest.skip("approved codex executable is not installed")
    client_sha = _sha256_file(client_path)
    version_result = subprocess.run(
        [str(client_path), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    version_output = "\n".join(part.strip() for part in (version_result.stdout, version_result.stderr) if part.strip())
    version = version_output.split()[-1]
    proof = {
        "schema": "chitra.native-client-proof.v1",
        "client_name": "codex",
        "version": version,
        "path": str(client_path.resolve()),
        "path_sha256": client_sha,
        "version_output": version_output,
        "update_suppression": False,
    }
    proof_path = tmp_path / "native-client-proof.json"
    _write_json(proof_path, proof)
    return {
        **proof,
        "version_proof_path": str(proof_path),
        "version_proof_sha256": _sha256_file(proof_path),
    }


def _write_lane_manifest(path: Path, *, state_root: Path, tmux_socket: Path) -> None:
    manifest = {
        "lanes": [
            {
                "id": "monitor",
                "account": "monitor",
                "uid": os.getuid() or 1,
                "home": str(path.parent / "home"),
                "workdir": str(path.parent / "work"),
                "config_dir": str(path.parent / "config"),
                "state_dir": str(state_root),
                "tmux_socket": str(tmux_socket),
                "tmux_session": "monitor-new",
                "credentials": {
                    "claude_credentials": str(path.parent / "config" / ".credentials.json"),
                    "ssh_dispatch_key": str(state_root / ".ssh" / "id_ed25519_tophand"),
                },
                "enabled": True,
            }
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")


def _start_tmux(socket_path: Path, session: str) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", session, "sleep", "60"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _stop_tmux(socket_path: Path) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket_path), "kill-server"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _authority_proof(
    *,
    state_root: Path,
    instance: str,
    old_writer: WriterObservation,
    new_writer: WriterObservation,
    bindings: dict[str, str],
    lifecycle_receipts: list[dict[str, object]],
    native_client: dict[str, object],
) -> dict[str, object]:
    governed_lane = {
        "id": "monitor",
        "account": "monitor",
        "uid": os.getuid() or 1,
        "state_dir": str(state_root.resolve()),
        "tmux_socket": str(Path(bindings["new_tmux_socket"]).resolve()),
        "tmux_session": "monitor-new",
    }
    old_tmux = topology._tmux_socket_identity(Path(bindings["old_tmux_socket"]))
    new_tmux = topology._tmux_socket_identity(Path(bindings["new_tmux_socket"]))
    assert old_tmux is not None
    assert new_tmux is not None
    tmux_sessions = {"old": old_tmux, "new": new_tmux}
    transcript_path = Path(_transcript_binding(state_root)).resolve()
    payload = topology._authority_ledger_payload(
        instance=instance,
        old_writer=old_writer,
        new_writer=new_writer,
        pre_state_sha256=bindings["pre_state_sha256"],
        post_state_sha256=bindings["post_state_sha256"],
        rollback_checkpoint_sha256=bindings["rollback_checkpoint_sha256"],
        instance_bindings=bindings,
        transcript_bindings=[{"path": str(transcript_path), "sha256": _sha256_file(transcript_path)}],
        lifecycle_receipts=lifecycle_receipts,
        native_client=native_client,
        cutover_sequence=("old-drained", "old-stopped", "old-write-denied", "new-started", "new-write-proved"),
        governed_lane=governed_lane,
        tmux_sessions=tmux_sessions,
    )
    digest = _sha256_bytes(_canonical_bytes(payload))
    ledger = _ledger_module()
    key = ledger.load_or_create_signing_key(state_root / "ledger.key")
    sent_at = "2026-08-22T00:00:01+00:00"
    entry = ledger.LedgerEntry(
        order_id="authority-handoff-1",
        session_ref=f"authority:{instance}",
        tag=topology.AUTHORITY_LEDGER_TAG,
        sig_v=4,
        message_hash=digest,
        sent_at=sent_at,
        signature=ledger.sign(
            key,
            session_ref=f"authority:{instance}",
            tag=topology.AUTHORITY_LEDGER_TAG,
            digest=digest,
            sent_at=sent_at,
        ),
    )
    (state_root / "ledger.jsonl").write_text(entry.model_dump_json() + "\n", encoding="utf-8")
    return {"order_id": entry.order_id, "ledger_entry": entry.model_dump(mode="json")}


def _transcript_binding(state_root: Path) -> str:
    return str(state_root / "transcripts" / "session.jsonl")


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


def test_authority_handoff_rejects_blank_caller_assertions_and_requires_lifecycle_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    old_socket_path = tmp_path / "old.sock"
    new_socket_path = tmp_path / "new.sock"
    _start_tmux(old_socket_path, "monitor-old")
    _start_tmux(new_socket_path, "monitor-new")
    manifest_path = tmp_path / "etc" / "chitra" / "lanes.yaml"
    _write_lane_manifest(manifest_path, state_root=state_root, tmux_socket=new_socket_path)
    monkeypatch.setenv("CHITRA_LANES_FILE", str(manifest_path))
    missing_pid = os.getpid() + 100_000
    while Path(f"/proc/{missing_pid}").exists():
        missing_pid += 1
    old_process = f"pid:{missing_pid}"
    writer_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        new_process = f"pid:{writer_process.pid}"
        bindings = _instance_bindings(state_root, old_socket_path, new_socket_path, old_process, new_process)
        native_client = _native_client(tmp_path)
        old_writer = WriterObservation(
            name="old-dispatchd",
            role="old",
            unit="chitra-dispatchd.service",
            process=old_process,
            package="chitra 0.11.0",
            stopped=True,
            started=False,
            can_write=False,
            last_order_sha256=bindings["last_old_order_sha256"],
        )
        new_writer = WriterObservation(
            name="new-dispatchd",
            role="new",
            unit="polyphony-chitra-dispatchd@monitor.service",
            process=new_process,
            package="chitra 0.14.10",
            stopped=False,
            started=True,
            can_write=True,
            action_receipt_sha256=bindings["new_action_receipt_sha256"],
        )
        lifecycle_receipts = _lifecycle_receipts(tmp_path, bindings)
        authority_proof = _authority_proof(
            state_root=state_root,
            instance="monitor",
            old_writer=old_writer,
            new_writer=new_writer,
            bindings=bindings,
            lifecycle_receipts=lifecycle_receipts,
            native_client=native_client,
        )
        handoff = build_authority_handoff_receipt(
            instance="monitor",
            old_writer=old_writer,
            new_writer=new_writer,
            pre_state_sha256=bindings["pre_state_sha256"],
            post_state_sha256=bindings["post_state_sha256"],
            rollback_checkpoint_sha256=bindings["rollback_checkpoint_sha256"],
            transcript_bindings=(_transcript_binding(state_root),),
            instance_bindings=bindings,
            lifecycle_receipts=lifecycle_receipts,
            native_client=native_client,
            authority_proof=authority_proof,
        )
        assert handoff["exactly_one_writer"] is True
        governed_lane = cast(dict[str, object], handoff["governed_lane"])
        authority_receipt = cast(dict[str, object], handoff["authority_proof"])
        assert governed_lane["id"] == "monitor"
        assert authority_receipt["payload_sha256"]
        blank_writer = WriterObservation(
            name="", role="old", unit="", process="", package="", stopped=True, started=False, can_write=False
        )
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
            process=old_process,
            package="chitra 0.11.0",
            stopped=True,
            started=False,
            can_write=True,
            last_order_sha256=bindings["last_old_order_sha256"],
        )
        with pytest.raises(ConversionError, match="old writer can still write"):
            build_authority_handoff_receipt(
                instance="monitor",
                old_writer=old_writer_still_active,
                new_writer=new_writer,
                pre_state_sha256=bindings["pre_state_sha256"],
                post_state_sha256=bindings["post_state_sha256"],
                rollback_checkpoint_sha256=bindings["rollback_checkpoint_sha256"],
                transcript_bindings=(_transcript_binding(state_root),),
                instance_bindings=bindings,
                lifecycle_receipts=lifecycle_receipts,
                native_client=native_client,
                authority_proof=authority_proof,
            )
        contradictory = dict(bindings)
        contradictory["old_unit"] = "fabricated.service"
        fake_client = dict(native_client)
        fake_client["path"] = str(tmp_path / "missing-codex")
        with pytest.raises(ConversionError, match="contradicts|does not exist"):
            build_authority_handoff_receipt(
                instance="monitor",
                old_writer=old_writer,
                new_writer=new_writer,
                pre_state_sha256=bindings["pre_state_sha256"],
                post_state_sha256=bindings["post_state_sha256"],
                rollback_checkpoint_sha256=bindings["rollback_checkpoint_sha256"],
                transcript_bindings=(_transcript_binding(state_root),),
                instance_bindings=contradictory,
                lifecycle_receipts=lifecycle_receipts,
                native_client=fake_client,
                authority_proof=authority_proof,
            )
        self_authored = dict(bindings)
        self_authored["state_root"] = str(tmp_path / "missing-state")
        with pytest.raises(ConversionError, match="state_root does not exist"):
            build_authority_handoff_receipt(
                instance="monitor",
                old_writer=old_writer,
                new_writer=new_writer,
                pre_state_sha256=bindings["pre_state_sha256"],
                post_state_sha256=bindings["post_state_sha256"],
                rollback_checkpoint_sha256=bindings["rollback_checkpoint_sha256"],
                transcript_bindings=(_transcript_binding(state_root),),
                instance_bindings=self_authored,
                lifecycle_receipts=lifecycle_receipts,
                native_client=native_client,
                authority_proof=authority_proof,
            )
        mismatching_output = dict(native_client)
        mismatching_output["version_output"] = "codex 9.9.9"
        with pytest.raises(ConversionError, match="version_output"):
            build_authority_handoff_receipt(
                instance="monitor",
                old_writer=old_writer,
                new_writer=new_writer,
                pre_state_sha256=bindings["pre_state_sha256"],
                post_state_sha256=bindings["post_state_sha256"],
                rollback_checkpoint_sha256=bindings["rollback_checkpoint_sha256"],
                transcript_bindings=(_transcript_binding(state_root),),
                instance_bindings=bindings,
                lifecycle_receipts=lifecycle_receipts,
                native_client=mismatching_output,
                authority_proof=authority_proof,
            )
        caller_authored = dict(bindings)
        caller_authored["goals_sha256"] = _sha256_file(state_root / "goals.json")
        (state_root / "goals.json").write_text('{"schema":"chitra.goals.v3","goals":[]}', encoding="utf-8")
        caller_authored["goals_sha256"] = _sha256_file(state_root / "goals.json")
        with pytest.raises(ConversionError, match="enrolled goal authority|authority ledger entry hash"):
            build_authority_handoff_receipt(
                instance="monitor",
                old_writer=old_writer,
                new_writer=new_writer,
                pre_state_sha256=bindings["pre_state_sha256"],
                post_state_sha256=bindings["post_state_sha256"],
                rollback_checkpoint_sha256=bindings["rollback_checkpoint_sha256"],
                transcript_bindings=(_transcript_binding(state_root),),
                instance_bindings=caller_authored,
                lifecycle_receipts=lifecycle_receipts,
                native_client=native_client,
                authority_proof=authority_proof,
            )
    finally:
        writer_process.terminate()
        writer_process.wait(timeout=5)
        _stop_tmux(old_socket_path)
        _stop_tmux(new_socket_path)


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
    with pytest.raises(ConversionError, match="allow_v3_loss cannot bypass"):
        restore_snapshot(snapshot, state, allow_v3_loss=True)
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

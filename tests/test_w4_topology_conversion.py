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


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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


def _handoff_authority_module() -> Any:
    return import_module("chitra.handoff_authority")


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


def _enrolled_goals_document(state_root: Path) -> dict[str, object]:
    return {
        "schema": "chitra.goals.v3",
        "updated_at": "2026-08-22T00:00:00+00:00",
        "goals": [
            {
                "session_ref": "authority:monitor",
                "goal": "complete the governed authority handoff",
                "done_when": "the signed handoff proof verifies",
                "source": "task-file:test",
                "status": "working",
                "created_at": "2026-08-22T00:00:00+00:00",
                "updated_at": "2026-08-22T00:00:00+00:00",
                "now": "",
                "last_verified": "",
                "interview_receipt": {
                    "name": "authority-enrollment",
                    "completed_at": "2026-08-22T00:00:00+00:00",
                    "answers_sha256": _sha256_bytes(b"operator answers"),
                    "provenance": [
                        "operator:interview",
                        "source:task-file:test",
                        "operator:confirmation",
                        "source:program-record",
                    ],
                },
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
    }


def _instance_bindings(state_root: Path, old_socket: Path, new_socket: Path, old_process: str, new_process: str) -> dict[str, str]:
    _write_json(state_root / "goals.json", _enrolled_goals_document(state_root))
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
                "uid": 4242,
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


def _provision_machine_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest_path: Path,
) -> dict[str, object]:
    """Verifier-owned test setup: provision the machine anchor it will judge with."""
    handoff_authority = _handoff_authority_module()
    anchor_dir = tmp_path / "etc" / "chitra" / "authority"
    anchor_path = anchor_dir / "handoff-authority.json"
    document = cast(
        dict[str, object],
        handoff_authority.build_machine_anchor_document(
            key=os.urandom(32),
            provisioned_at="2026-08-21T00:00:00+00:00",
            lanes_manifest_sha256=_sha256_file(manifest_path),
        ),
    )
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    anchor_path.write_text(_canonical_bytes(document).decode("utf-8"), encoding="utf-8")
    anchor_path.chmod(0o600)
    monkeypatch.setattr(handoff_authority, "MACHINE_ANCHOR_PATH", anchor_path)
    return document


def _append_signed_ledger_line(
    state_root: Path,
    *,
    key: bytes,
    order_id: str,
    tag: str,
    payload: dict[str, object],
    session_ref: str,
    sent_at: str,
) -> dict[str, object]:
    ledger = _ledger_module()
    digest = _sha256_bytes(_canonical_bytes(payload))
    entry = ledger.LedgerEntry(
        order_id=order_id,
        session_ref=session_ref,
        tag=tag,
        sig_v=4,
        message_hash=digest,
        sent_at=sent_at,
        signature=ledger.sign(key, session_ref=session_ref, tag=tag, digest=digest, sent_at=sent_at),
    )
    line = entry.model_dump(mode="json")
    line["enrollment"] = payload
    with (state_root / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
    return {"order_id": order_id, "ledger_entry": line}


def _enrollment_payload(
    *,
    instance: str,
    bindings: dict[str, str],
    new_process_exe_sha256: str,
    new_process_cgroup: str | None,
    old_tmux_session: str,
) -> dict[str, object]:
    import importlib.metadata

    return {
        "schema": topology.SCHEMA_AUTHORITY_ENROLLMENT,
        "instance": instance,
        "lane_id": instance,
        "state_root": bindings["state_root"],
        "goals_sha256": bindings["goals_sha256"],
        "old_unit": bindings["old_unit"],
        "new_unit": bindings["new_unit"],
        "old_package": bindings["old_package"],
        "new_package": bindings["new_package"],
        "new_process": bindings["new_process"],
        "new_package_distribution": "chitra-monitor",
        "new_package_version": importlib.metadata.version("chitra-monitor"),
        "new_process_exe_sha256": new_process_exe_sha256,
        "new_process_cgroup": new_process_cgroup,
        "old_tmux_socket": bindings["old_tmux_socket"],
        "new_tmux_socket": bindings["new_tmux_socket"],
        "old_tmux_session": old_tmux_session,
        "enrolled_at": "2026-08-22T00:00:00+00:00",
    }


def _authority_proof(
    *,
    state_root: Path,
    instance: str,
    old_writer: WriterObservation,
    new_writer: WriterObservation,
    bindings: dict[str, str],
    lifecycle_receipts: list[dict[str, object]],
    native_client: dict[str, object],
    signing_key: bytes,
    enrollment_order_id: str = "authority-enrollment-1",
) -> dict[str, object]:
    governed_lane = {
        "id": "monitor",
        "account": "monitor",
        "uid": 4242,
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
    handoff_module = _handoff_authority_module()
    new_process_exe = topology._process_executable_sha256(new_writer.process)
    assert new_process_exe is not None
    enrollment = _append_signed_ledger_line(
        state_root,
        key=signing_key,
        order_id=enrollment_order_id,
        tag=handoff_module.ENROLLMENT_TAG,
        payload=_enrollment_payload(
            instance=instance,
            bindings=bindings,
            new_process_exe_sha256=new_process_exe[1],
            new_process_cgroup=topology._process_cgroup(new_writer.process),
            old_tmux_session=str(old_tmux["session_name"]),
        ),
        session_ref=f"authority:{instance}",
        sent_at="2026-08-22T00:00:00+00:00",
    )
    handoff = _append_signed_ledger_line(
        state_root,
        key=signing_key,
        order_id="authority-handoff-1",
        tag=handoff_module.HANDOFF_TAG,
        payload=payload,
        session_ref=f"authority:{instance}",
        sent_at="2026-08-22T00:00:01+00:00",
    )
    return {
        "enrollment_order_id": enrollment["order_id"],
        "order_id": handoff["order_id"],
        "ledger_entry": handoff["ledger_entry"],
    }


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


def test_authority_handoff_binds_to_preexisting_non_caller_mintable_trust_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    old_socket_path = tmp_path / "old.sock"
    new_socket_path = tmp_path / "new.sock"
    _start_tmux(old_socket_path, "monitor-old")
    _start_tmux(new_socket_path, "monitor-new")
    manifest_path = tmp_path / "etc" / "chitra" / "lanes.yaml"
    _write_lane_manifest(manifest_path, state_root=state_root, tmux_socket=new_socket_path)
    lane_config = import_module("chitra.lane_config")
    monkeypatch.setattr(lane_config, "DEFAULT_LANES_FILE", manifest_path)
    caller_manifest_path = tmp_path / "caller-selected" / "lanes.yaml"
    _write_lane_manifest(caller_manifest_path, state_root=state_root, tmux_socket=new_socket_path)
    monkeypatch.setenv("CHITRA_LANES_FILE", str(caller_manifest_path))
    anchor_document = _provision_machine_authority(tmp_path, monkeypatch, manifest_path=manifest_path)
    missing_pid = os.getpid() + 100_000
    while Path(f"/proc/{missing_pid}").exists():
        missing_pid += 1
    old_process = f"pid:{missing_pid}"
    writer_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    impostor_process: subprocess.Popen[bytes] | None = None
    rogue_socket_path = tmp_path / "caller-selected.sock"
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

        def build(**overrides: object) -> dict[str, object]:
            return build_authority_handoff_receipt(
                instance=str(overrides.get("instance", "monitor")),
                old_writer=cast(WriterObservation, overrides.get("old_writer", old_writer)),
                new_writer=cast(WriterObservation, overrides.get("new_writer", new_writer)),
                pre_state_sha256=str(overrides.get("pre_state_sha256", bindings["pre_state_sha256"])),
                post_state_sha256=str(overrides.get("post_state_sha256", bindings["post_state_sha256"])),
                rollback_checkpoint_sha256=str(
                    overrides.get("rollback_checkpoint_sha256", bindings["rollback_checkpoint_sha256"])
                ),
                transcript_bindings=cast(
                    tuple[str, ...], overrides.get("transcript_bindings", (_transcript_binding(state_root),))
                ),
                instance_bindings=cast(dict[str, str], overrides.get("instance_bindings", bindings)),
                lifecycle_receipts=cast(list[dict[str, object]], overrides.get("lifecycle_receipts", lifecycle_receipts)),
                native_client=cast(dict[str, object], overrides.get("native_client", native_client)),
                authority_proof=cast(dict[str, object], overrides.get("authority_proof", authority_proof)),
            )

        authority_proof = _authority_proof(
            state_root=state_root,
            instance="monitor",
            old_writer=old_writer,
            new_writer=new_writer,
            bindings=bindings,
            lifecycle_receipts=lifecycle_receipts,
            native_client=native_client,
            signing_key=bytes.fromhex(str(anchor_document["key"])),
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
        assert cast(dict[str, object], authority_receipt["enrollment"])["enrollment_order_id"]
        anchor_facts = cast(dict[str, object], authority_receipt["authority_anchor"])
        assert str(anchor_facts["sha256"])
        assert not (state_root / "ledger.key").exists()

        ledger_path = state_root / "ledger.jsonl"
        governed_ledger_bytes = ledger_path.read_bytes()
        caller_key_line = json.loads(governed_ledger_bytes.decode("utf-8").splitlines()[-1])
        ledger_module = _ledger_module()
        caller_signed_entry = dict(caller_key_line)
        caller_signed_entry["signature"] = ledger_module.sign(
            os.urandom(32),
            session_ref=str(caller_key_line["session_ref"]),
            tag=str(caller_key_line["tag"]),
            digest=str(caller_key_line["message_hash"]),
            sent_at=str(caller_key_line["sent_at"]),
        )
        with pytest.raises(ConversionError, match="signature does not verify under the machine-provisioned authority"):
            build(authority_proof={**authority_proof, "ledger_entry": caller_signed_entry})

        caller_key = os.urandom(32)
        caller_ledger_lines: list[dict[str, object]] = []
        for raw_line in governed_ledger_bytes.decode("utf-8").splitlines():
            line = cast(dict[str, object], json.loads(raw_line))
            line["signature"] = ledger_module.sign(
                caller_key,
                session_ref=str(line["session_ref"]),
                tag=str(line["tag"]),
                digest=str(line["message_hash"]),
                sent_at=str(line["sent_at"]),
            )
            caller_ledger_lines.append(line)
        ledger_path.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in caller_ledger_lines), encoding="utf-8")
        with pytest.raises(ConversionError, match="signature does not verify under the machine-provisioned authority"):
            build(authority_proof={**authority_proof, "ledger_entry": caller_ledger_lines[-1]})
        ledger_path.write_bytes(governed_ledger_bytes)

        self_authored_goals = _enrolled_goals_document(state_root)
        self_authored_goals_list = cast(list[dict[str, object]], self_authored_goals["goals"])
        self_authored_goal = self_authored_goals_list[0]
        self_authored_goal["goal"] = "caller-replaced enrollment"
        _write_json(state_root / "goals.json", self_authored_goals)
        rewritten_bindings = dict(bindings)
        rewritten_bindings["goals_sha256"] = _sha256_file(state_root / "goals.json")
        with pytest.raises(ConversionError, match="goals document identity|post_state_sha256"):
            build(instance_bindings=rewritten_bindings)
        _write_json(state_root / "goals.json", _enrolled_goals_document(state_root))

        caller_selected_lane_config = import_module("chitra.lane_config")
        monkeypatch.setattr(caller_selected_lane_config, "DEFAULT_LANES_FILE", caller_manifest_path)
        with pytest.raises(ConversionError, match="trust-anchor declaration"):
            build()
        monkeypatch.setattr(caller_selected_lane_config, "DEFAULT_LANES_FILE", manifest_path)

        impostor_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        impostor_writer = WriterObservation(
            name=new_writer.name,
            role="new",
            unit=new_writer.unit,
            process=f"pid:{impostor_process.pid}",
            package=new_writer.package,
            stopped=False,
            started=True,
            can_write=True,
            action_receipt_sha256=new_writer.action_receipt_sha256,
        )
        impostor_bindings = dict(bindings)
        impostor_bindings["new_process"] = impostor_writer.process
        with pytest.raises(ConversionError, match="process identity"):
            build(new_writer=impostor_writer, instance_bindings=impostor_bindings)

        _start_tmux(rogue_socket_path, "monitor-new")
        rogue_bindings = dict(bindings)
        rogue_bindings["new_tmux_socket"] = str(rogue_socket_path)
        with pytest.raises(ConversionError, match="tmux socket|governed lane manifest"):
            build(instance_bindings=rogue_bindings)

        fake_client = tmp_path / "path-shadow" / "codex"
        fake_client.parent.mkdir()
        fake_client.write_text("#!/bin/sh\nprintf 'codex caller-fake\\n'\n", encoding="utf-8")
        fake_client.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_client.parent}:{os.environ.get('PATH', '')}")
        path_shadowed_handoff = build()
        assert cast(dict[str, object], path_shadowed_handoff["native_client"])["path"] != str(fake_client)

        caller_state_root = tmp_path / "caller-selected-state"
        shutil.copytree(state_root, caller_state_root)
        caller_root_bindings = {
            key: (value.replace(str(state_root), str(caller_state_root), 1) if value.startswith(str(state_root)) else value)
            for key, value in bindings.items()
        }
        with pytest.raises(ConversionError, match="state root"):
            build(
                instance_bindings=caller_root_bindings,
                transcript_bindings=(_transcript_binding(caller_state_root),),
            )

        unanchored_dir = tmp_path / "unanchored"
        unanchored_dir.mkdir()
        handoff_authority_module = _handoff_authority_module()
        saved_anchor_path = handoff_authority_module.MACHINE_ANCHOR_PATH
        monkeypatch.setattr(handoff_authority_module, "MACHINE_ANCHOR_PATH", unanchored_dir / "absent.json")
        with pytest.raises(ConversionError, match="handoff-authority anchor is unavailable"):
            build()
        monkeypatch.setattr(handoff_authority_module, "MACHINE_ANCHOR_PATH", saved_anchor_path)

        blank_writer = WriterObservation(
            name="", role="old", unit="", process="", package="", stopped=True, started=False, can_write=False
        )
        with pytest.raises(ConversionError, match="missing|transcript|native|lifecycle"):
            build(instance="", old_writer=blank_writer, pre_state_sha256="", post_state_sha256="", rollback_checkpoint_sha256="")

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
            build(old_writer=old_writer_still_active)
    finally:
        writer_process.terminate()
        writer_process.wait(timeout=5)
        if impostor_process is not None:
            impostor_process.terminate()
            impostor_process.wait(timeout=5)
        _stop_tmux(rogue_socket_path)
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

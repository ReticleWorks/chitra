"""Read-only topology conversion and handoff receipts.

These helpers intentionally do not dispatch messages, manage services, or
touch live hosts.  They turn legacy state into hash-bound receipts that a
separate migration/canary step can consume.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

SCHEMA_CONVERSION_RECEIPT = "chitra.topology-conversion-receipt.v1"
SCHEMA_GOALS_CONVERSION = "chitra.legacy-goals-conversion.v1"
SCHEMA_QUEUE_REPLAY = "chitra.dispatch-queue-replay.v1"
SCHEMA_SHADOW_FINDINGS = "chitra.topology-shadow-findings.v1"
SCHEMA_HANDOFF_RECEIPT = "chitra.authority-handoff-receipt.v1"
SCHEMA_ROLLBACK_RECEIPT = "chitra.disposable-rollback-receipt.v1"
SNAPSHOT_MARKER = ".chitra-disposable-snapshot"

GOAL_SCHEMAS = {"chitra.goals.v1", "chitra.goals.v2", "chitra.goals.v3"}
LEGACY_GOAL_SCHEMAS = {"chitra.goals.v1", "chitra.goals.v2"}
TERMINAL_GOAL_STATUSES = {"done-pending-verification", "done-pending-close"}
QUEUE_STAGES = ("orders", "in_flight", "deferred", "results", "processed", "invalid")


class ConversionError(ValueError):
    """Raised when conversion inputs cannot satisfy the migration contract."""


@dataclass(frozen=True, slots=True)
class WriterObservation:
    """One side of an authority handoff proof."""

    name: str
    role: Literal["old", "new"]
    unit: str
    process: str
    package: str
    stopped: bool
    started: bool
    can_write: bool
    last_order_sha256: str = ""
    action_receipt_sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "role": self.role,
            "unit": self.unit,
            "process": self.process,
            "package": self.package,
            "stopped": self.stopped,
            "started": self.started,
            "can_write": self.can_write,
            "last_order_sha256": self.last_order_sha256,
            "action_receipt_sha256": self.action_receipt_sha256,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConversionError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConversionError(f"{path} must contain a JSON object")
    return cast(dict[str, object], payload)


def _safe_resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _goal_records(payload: Mapping[str, object], source: Path | str) -> list[dict[str, object]]:
    schema = payload.get("schema")
    if schema not in GOAL_SCHEMAS:
        raise ConversionError(f"{source} is not a supported goals document")
    goals = payload.get("goals")
    if not isinstance(goals, list):
        raise ConversionError(f"{source} goals must be a list")
    records: list[dict[str, object]] = []
    for item in goals:
        if not isinstance(item, dict):
            raise ConversionError(f"{source} contains a non-object goal record")
        records.append(cast(dict[str, object], item))
    return records


def convert_goals_document(
    payload: Mapping[str, object],
    *,
    source_path: str,
    source_sha256: str,
    produced_at: str | None = None,
) -> dict[str, object]:
    """Convert one goals document into display/dispose-only legacy records."""
    schema = payload.get("schema")
    records = _goal_records(payload, source_path)
    converted: list[dict[str, object]] = []
    terminal = 0
    open_count = 0
    for index, record in enumerate(records):
        status = record.get("status")
        status_text = status if isinstance(status, str) else ""
        is_terminal = status_text in TERMINAL_GOAL_STATUSES
        terminal += int(is_terminal)
        open_count += int(not is_terminal)
        session_ref = record.get("session_ref")
        lane_id = record.get("lane_id")
        converted.append(
            {
                "legacy_index": index,
                "session_ref": session_ref if isinstance(session_ref, str) and session_ref else f"legacy-index-{index}",
                "lane_id": lane_id if isinstance(lane_id, str) and lane_id else "",
                "legacy_schema": schema,
                "legacy_status": status_text,
                "legacy_record_sha256": _sha256_bytes(_canonical_bytes(record)),
                "legacy_record": record,
                "disposition": {
                    "display_only": True,
                    "administrative_dispose_only": True,
                    "done_transition_allowed": False,
                    "completion_eligible": False,
                    "requires_old_authority_or_fresh_interview": not is_terminal,
                    "reason": (
                        "terminal legacy goal remains display/dispose-only"
                        if is_terminal
                        else "open legacy goal must close under old authority or be enrolled through the interview/receipt contract"
                    ),
                },
            }
        )
    converted_doc: dict[str, object] = {
        "schema": SCHEMA_GOALS_CONVERSION,
        "produced_at": produced_at or _utc_now(),
        "source": {"path": source_path, "sha256": source_sha256, "schema": schema},
        "input_counts": {"goals": len(records), "terminal": terminal, "open": open_count},
        "output_counts": {"legacy_goal_records": len(converted)},
        "records": converted,
    }
    converted_doc["sha256"] = _sha256_bytes(_canonical_bytes(converted_doc))
    return converted_doc


def _queue_stage(relative_path: str) -> str:
    if relative_path == "queue.tsv":
        return "legacy_tsv"
    parts = relative_path.split("/")
    if len(parts) >= 2 and parts[0] == "queue" and parts[1] in QUEUE_STAGES:
        return parts[1]
    return "other"


def _queue_entry_from_file(root: Path, path: Path) -> dict[str, object]:
    rel = path.relative_to(root).as_posix()
    data = path.read_bytes()
    entry: dict[str, object] = {
        "relative_path": rel,
        "stage": _queue_stage(rel),
        "sha256": _sha256_bytes(data),
        "size": len(data),
    }
    if rel == "queue.tsv":
        entry["line_count"] = 0 if not data else data.count(b"\n") + int(not data.endswith(b"\n"))
    return entry


def queue_replay_from_root(root: Path, *, produced_at: str | None = None) -> dict[str, object]:
    """Return a count/order/hash replay manifest for one state root."""
    candidates: list[Path] = []
    tsv = root / "queue.tsv"
    if tsv.exists() and tsv.is_file():
        candidates.append(tsv)
    queue_root = root / "queue"
    if queue_root.exists():
        for path in sorted(queue_root.rglob("*")):
            if path.is_file():
                candidates.append(path)
    entries = [_queue_entry_from_file(root, path) for path in candidates]
    for ordinal, entry in enumerate(entries):
        entry["ordinal"] = ordinal
    replay: dict[str, object] = {
        "schema": SCHEMA_QUEUE_REPLAY,
        "produced_at": produced_at or _utc_now(),
        "source": {"state_root": str(root)},
        "entry_count": len(entries),
        "entries": entries,
    }
    replay["sha256"] = _sha256_bytes(_canonical_bytes(replay))
    return replay


def _snapshot_state_dirs(snapshot: Mapping[str, object]) -> list[dict[str, object]]:
    hosts = snapshot.get("hosts")
    if not isinstance(hosts, dict):
        raise ConversionError("W10 snapshot hosts must be an object")
    state_dirs: list[dict[str, object]] = []
    for host_name, host_value in hosts.items():
        if not isinstance(host_value, dict):
            continue
        dirs = host_value.get("state_directories")
        if not isinstance(dirs, list):
            continue
        for directory in dirs:
            if isinstance(directory, dict):
                row = dict(directory)
                row["host"] = str(host_name)
                state_dirs.append(cast(dict[str, object], row))
    return state_dirs


def _snapshot_file_entries(directory: Mapping[str, object]) -> list[dict[str, object]]:
    files = directory.get("files")
    if not isinstance(files, list):
        return []
    return [cast(dict[str, object], item) for item in files if isinstance(item, dict)]


def _snapshot_goals_manifests(snapshot: Mapping[str, object]) -> list[dict[str, object]]:
    manifests: list[dict[str, object]] = []
    for directory in _snapshot_state_dirs(snapshot):
        root = directory.get("path")
        host = directory.get("host")
        for item in _snapshot_file_entries(directory):
            if item.get("relative_path") != "goals.json":
                continue
            json_meta = item.get("json")
            if not isinstance(json_meta, dict) or json_meta.get("schema") not in LEGACY_GOAL_SCHEMAS:
                continue
            manifests.append(
                {
                    "host": host,
                    "state_root": root,
                    "relative_path": "goals.json",
                    "schema": json_meta.get("schema"),
                    "sha256": item.get("sha256"),
                    "size": item.get("size"),
                    "goal_count": json_meta.get("goal_count", 0),
                    "done_when_nonempty_count": json_meta.get("done_when_nonempty_count", 0),
                }
            )
    return manifests


def _snapshot_queue_manifests(snapshot: Mapping[str, object]) -> list[dict[str, object]]:
    manifests: list[dict[str, object]] = []
    for directory in _snapshot_state_dirs(snapshot):
        root = directory.get("path")
        host = directory.get("host")
        for item in _snapshot_file_entries(directory):
            relative = item.get("relative_path")
            if not isinstance(relative, str):
                continue
            stage = _queue_stage(relative)
            if stage == "other":
                continue
            manifests.append(
                {
                    "host": host,
                    "state_root": root,
                    "relative_path": relative,
                    "stage": stage,
                    "sha256": item.get("sha256"),
                    "size": item.get("size"),
                }
            )
    for ordinal, entry in enumerate(manifests):
        entry["ordinal"] = ordinal
    return manifests


def convert_w10_snapshot(snapshot_path: Path, output_dir: Path) -> dict[str, object]:
    """Write a receipt binding the W10 snapshot's legacy counts and hashes."""
    snapshot = _load_json(snapshot_path)
    snapshot_sha = _sha256_file(snapshot_path)
    goals = _snapshot_goals_manifests(snapshot)
    queues = _snapshot_queue_manifests(snapshot)
    converted_goals = [
        {
            **entry,
            "disposition": {
                "display_only": True,
                "administrative_dispose_only": True,
                "done_transition_allowed": False,
                "completion_eligible": False,
                "requires_old_authority_or_fresh_interview": True,
            },
        }
        for entry in goals
    ]
    goals_artifact: dict[str, object] = {
        "schema": SCHEMA_GOALS_CONVERSION,
        "produced_at": _utc_now(),
        "source": {"path": str(snapshot_path), "sha256": snapshot_sha, "mode": "w10-snapshot-metadata"},
        "input_counts": {"goals": sum(_as_int(entry.get("goal_count")) for entry in goals), "goals_files": len(goals)},
        "output_counts": {"legacy_goal_records": sum(_as_int(entry.get("goal_count")) for entry in goals)},
        "records": converted_goals,
    }
    queue_artifact: dict[str, object] = {
        "schema": SCHEMA_QUEUE_REPLAY,
        "produced_at": _utc_now(),
        "source": {"path": str(snapshot_path), "sha256": snapshot_sha, "mode": "w10-snapshot-metadata"},
        "entry_count": len(queues),
        "entries": queues,
    }
    goals_artifact["sha256"] = _sha256_bytes(_canonical_bytes(goals_artifact))
    queue_artifact["sha256"] = _sha256_bytes(_canonical_bytes(queue_artifact))
    output_dir.mkdir(parents=True, exist_ok=True)
    goals_path = output_dir / "legacy-goals-conversion.json"
    queue_path = output_dir / "dispatch-queue-replay.json"
    _write_json(goals_path, goals_artifact)
    _write_json(queue_path, queue_artifact)
    receipt = _conversion_receipt(
        mode="w10-snapshot",
        source=str(snapshot_path),
        source_sha256=snapshot_sha,
        goals_artifact=goals_artifact,
        queue_artifact=queue_artifact,
        output_dir=output_dir,
    )
    _write_json(output_dir / "conversion-receipt.json", receipt)
    return receipt


def convert_state_root(state_root: Path, output_dir: Path) -> dict[str, object]:
    """Convert one legacy state root into hash-bound artifacts and receipt."""
    state_root = _safe_resolve(state_root)
    output_dir = _safe_resolve(output_dir)
    if _is_relative_to(output_dir, state_root):
        raise ConversionError("conversion output must not be written inside the state root")
    goals_file = state_root / "goals.json"
    goals_payload = _load_json(goals_file)
    goals_artifact = convert_goals_document(
        goals_payload,
        source_path=str(goals_file),
        source_sha256=_sha256_file(goals_file),
    )
    queue_artifact = queue_replay_from_root(state_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "legacy-goals-conversion.json", goals_artifact)
    _write_json(output_dir / "dispatch-queue-replay.json", queue_artifact)
    receipt = _conversion_receipt(
        mode="state-root",
        source=str(state_root),
        source_sha256=_manifest_digest(_file_manifest(state_root)),
        goals_artifact=goals_artifact,
        queue_artifact=queue_artifact,
        output_dir=output_dir,
    )
    _write_json(output_dir / "conversion-receipt.json", receipt)
    return receipt


def _conversion_receipt(
    *,
    mode: str,
    source: str,
    source_sha256: str,
    goals_artifact: Mapping[str, object],
    queue_artifact: Mapping[str, object],
    output_dir: Path,
) -> dict[str, object]:
    goal_input_count = _as_int(cast(Mapping[str, object], goals_artifact["input_counts"]).get("goals"))
    goal_output_count = _as_int(cast(Mapping[str, object], goals_artifact["output_counts"]).get("legacy_goal_records"))
    queue_input_count = _as_int(queue_artifact.get("entry_count"))
    receipt: dict[str, object] = {
        "schema": SCHEMA_CONVERSION_RECEIPT,
        "produced_at": _utc_now(),
        "mode": mode,
        "source": {"path": source, "sha256": source_sha256},
        "artifacts": {
            "legacy_goals_conversion": {
                "path": str(output_dir / "legacy-goals-conversion.json"),
                "sha256": goals_artifact["sha256"],
            },
            "dispatch_queue_replay": {
                "path": str(output_dir / "dispatch-queue-replay.json"),
                "sha256": queue_artifact["sha256"],
            },
        },
        "input_counts": {
            "legacy_goals": goal_input_count,
            "dispatch_queue_entries": queue_input_count,
        },
        "output_counts": {
            "legacy_goal_records": goal_output_count,
            "dispatch_queue_entries": queue_input_count,
        },
        "reconciliation": {
            "goal_counts_match": goal_input_count == goal_output_count,
            "queue_count_order_identity_hashes_preserved": True,
            "legacy_goals_marked_display_dispose_only": True,
            "legacy_goals_may_claim_done": False,
            "status": "PASS" if goal_input_count == goal_output_count else "FAIL",
        },
    }
    receipt["sha256"] = _sha256_bytes(_canonical_bytes(receipt))
    return receipt


def run_shadow_scan(state_root: Path, shadow_dir: Path) -> dict[str, object]:
    """Scan a state root read-only and write findings only under shadow_dir."""
    state_root = _safe_resolve(state_root)
    shadow_dir = _safe_resolve(shadow_dir)
    if _is_relative_to(shadow_dir, state_root):
        raise ConversionError("shadow output must not be inside the live state root")
    findings: list[dict[str, object]] = []
    goals_file = state_root / "goals.json"
    if goals_file.exists():
        goals_payload = _load_json(goals_file)
        schema = goals_payload.get("schema")
        if schema in LEGACY_GOAL_SCHEMAS:
            records = _goal_records(goals_payload, goals_file)
            findings.append(
                {
                    "code": "legacy_goals_display_only",
                    "severity": "info",
                    "path": str(goals_file),
                    "count": len(records),
                    "sha256": _sha256_file(goals_file),
                    "message": "legacy goals can be displayed or administratively disposed, but cannot be converted to done",
                }
            )
    queue_replay = queue_replay_from_root(state_root)
    if _as_int(queue_replay["entry_count"]):
        findings.append(
            {
                "code": "dispatch_state_replay_required",
                "severity": "info",
                "count": queue_replay["entry_count"],
                "sha256": queue_replay["sha256"],
                "message": "dispatch queue state is preserved for replay; shadow mode does not dispatch",
            }
        )
    payload: dict[str, object] = {
        "schema": SCHEMA_SHADOW_FINDINGS,
        "produced_at": _utc_now(),
        "source": {"state_root": str(state_root), "manifest_sha256": _manifest_digest(_file_manifest(state_root))},
        "dispatch_attempted": False,
        "findings": findings,
    }
    payload["sha256"] = _sha256_bytes(_canonical_bytes(payload))
    _write_json(shadow_dir / "shadow-findings.json", payload)
    return payload


def build_authority_handoff_receipt(
    *,
    instance: str,
    old_writer: WriterObservation,
    new_writer: WriterObservation,
    pre_state_sha256: str,
    post_state_sha256: str,
    rollback_checkpoint_sha256: str,
    transcript_bindings: Sequence[str] = (),
    cutover_sequence: Sequence[str] = ("old-drained", "old-stopped", "old-write-denied", "new-started", "new-write-proved"),
) -> dict[str, object]:
    """Seal a pure handoff proof; callers provide externally observed facts."""
    if old_writer.role != "old" or new_writer.role != "new":
        raise ConversionError("handoff requires one old writer and one new writer")
    required_sequence = ("old-drained", "old-stopped", "old-write-denied", "new-started", "new-write-proved")
    if tuple(cutover_sequence) != required_sequence:
        raise ConversionError("cutover sequence must drain old writer, stop it, deny its writes, then start and prove the new writer")
    active_writers = [writer.name for writer in (old_writer, new_writer) if writer.can_write]
    issues: list[str] = []
    if not old_writer.stopped:
        issues.append("old writer is not stopped")
    if old_writer.can_write:
        issues.append("old writer can still write")
    if not new_writer.started:
        issues.append("new writer is not started")
    if not new_writer.can_write:
        issues.append("new writer has not proved a write")
    if len(active_writers) != 1:
        issues.append("exactly one writer must be able to act")
    receipt: dict[str, object] = {
        "schema": SCHEMA_HANDOFF_RECEIPT,
        "produced_at": _utc_now(),
        "instance": instance,
        "writers": [old_writer.to_dict(), new_writer.to_dict()],
        "state_hashes": {
            "pre_handoff": pre_state_sha256,
            "post_handoff": post_state_sha256,
            "rollback_checkpoint": rollback_checkpoint_sha256,
        },
        "transcript_bindings": list(transcript_bindings),
        "cutover_sequence": list(cutover_sequence),
        "exactly_one_writer": len(active_writers) == 1 and active_writers == [new_writer.name],
        "issues": issues,
        "status": "PASS" if not issues else "FAIL",
    }
    receipt["sha256"] = _sha256_bytes(_canonical_bytes(receipt))
    if issues:
        raise ConversionError("; ".join(issues))
    return receipt


def _file_manifest(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    if not root.exists():
        return entries
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == SNAPSHOT_MARKER:
            continue
        stat = path.stat()
        entries.append({"relative_path": rel, "sha256": _sha256_file(path), "size": stat.st_size, "mode": oct(stat.st_mode & 0o777)})
    return entries


def _manifest_digest(manifest: Iterable[Mapping[str, object]]) -> str:
    return _sha256_bytes(_canonical_bytes(list(manifest)))


def snapshot_state_root(state_root: Path, snapshot_dir: Path) -> dict[str, object]:
    """Copy a disposable pre-conversion checkpoint and bind its file hashes."""
    state_root = _safe_resolve(state_root)
    snapshot_dir = _safe_resolve(snapshot_dir)
    if _is_relative_to(snapshot_dir, state_root):
        raise ConversionError("rollback snapshot must not be inside the state root")
    if snapshot_dir.exists() and any(snapshot_dir.iterdir()):
        raise ConversionError("rollback snapshot directory must be empty or absent")
    snapshot_payload_dir = snapshot_dir / "state-root"
    shutil.copytree(state_root, snapshot_payload_dir)
    manifest = _file_manifest(snapshot_payload_dir)
    marker: dict[str, object] = {
        "schema": SCHEMA_ROLLBACK_RECEIPT,
        "created_at": _utc_now(),
        "source_state_root": str(state_root),
        "manifest_sha256": _manifest_digest(manifest),
        "file_count": len(manifest),
    }
    _write_json(snapshot_dir / SNAPSHOT_MARKER, marker)
    _write_json(snapshot_dir / "manifest.json", {"files": manifest, **marker})
    return marker


def _contains_v3_or_receipts(root: Path) -> bool:
    receipts = root / "validation-receipts"
    if receipts.exists() and any(receipts.rglob("*.json")):
        return True
    goals = root / "goals.json"
    if not goals.exists():
        return False
    try:
        return _load_json(goals).get("schema") == "chitra.goals.v3"
    except ConversionError:
        return False


def restore_snapshot(snapshot_dir: Path, state_root: Path, *, allow_v3_loss: bool = False) -> dict[str, object]:
    """Restore a disposable snapshot and verify the post-restore hashes."""
    snapshot_dir = _safe_resolve(snapshot_dir)
    state_root = _safe_resolve(state_root)
    marker_path = snapshot_dir / SNAPSHOT_MARKER
    if not marker_path.exists():
        raise ConversionError("rollback snapshot marker is missing")
    marker = _load_json(marker_path)
    snapshot_payload_dir = snapshot_dir / "state-root"
    if not snapshot_payload_dir.exists():
        raise ConversionError("rollback snapshot payload is missing")
    if _contains_v3_or_receipts(state_root) and not allow_v3_loss:
        raise ConversionError("rollback would lose v3 enrollment or validation evidence")
    if state_root == Path("/") or len(state_root.parts) < 3:
        raise ConversionError("refusing to restore an unsafe state root")
    state_root.mkdir(parents=True, exist_ok=True)
    for child in state_root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in snapshot_payload_dir.iterdir():
        target = state_root / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    after_manifest = _file_manifest(state_root)
    after_hash = _manifest_digest(after_manifest)
    expected_hash = marker.get("manifest_sha256")
    receipt: dict[str, object] = {
        "schema": SCHEMA_ROLLBACK_RECEIPT,
        "produced_at": _utc_now(),
        "snapshot_dir": str(snapshot_dir),
        "state_root": str(state_root),
        "expected_manifest_sha256": expected_hash,
        "restored_manifest_sha256": after_hash,
        "file_count": len(after_manifest),
        "loses_state": after_hash != expected_hash,
        "status": "PASS" if after_hash == expected_hash else "FAIL",
    }
    receipt["sha256"] = _sha256_bytes(_canonical_bytes(receipt))
    _write_json(snapshot_dir / "rollback-receipt.json", receipt)
    if after_hash != expected_hash:
        raise ConversionError("rollback hash verification failed")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Chitra topology conversion receipts")
    subcommands = parser.add_subparsers(dest="command", required=True)
    root_parser = subcommands.add_parser("convert-root")
    root_parser.add_argument("state_root", type=Path)
    root_parser.add_argument("output_dir", type=Path)
    w10_parser = subcommands.add_parser("convert-w10")
    w10_parser.add_argument("snapshot", type=Path)
    w10_parser.add_argument("output_dir", type=Path)
    shadow_parser = subcommands.add_parser("shadow-root")
    shadow_parser.add_argument("state_root", type=Path)
    shadow_parser.add_argument("shadow_dir", type=Path)
    snapshot_parser = subcommands.add_parser("snapshot-root")
    snapshot_parser.add_argument("state_root", type=Path)
    snapshot_parser.add_argument("snapshot_dir", type=Path)
    rollback_parser = subcommands.add_parser("rollback-root")
    rollback_parser.add_argument("snapshot_dir", type=Path)
    rollback_parser.add_argument("state_root", type=Path)
    args = parser.parse_args(argv)

    if args.command == "convert-root":
        print(json.dumps(convert_state_root(args.state_root, args.output_dir), indent=2, sort_keys=True))
    elif args.command == "convert-w10":
        print(json.dumps(convert_w10_snapshot(args.snapshot, args.output_dir), indent=2, sort_keys=True))
    elif args.command == "shadow-root":
        print(json.dumps(run_shadow_scan(args.state_root, args.shadow_dir), indent=2, sort_keys=True))
    elif args.command == "snapshot-root":
        print(json.dumps(snapshot_state_root(args.state_root, args.snapshot_dir), indent=2, sort_keys=True))
    elif args.command == "rollback-root":
        print(json.dumps(restore_snapshot(args.snapshot_dir, args.state_root), indent=2, sort_keys=True))
    else:  # pragma: no cover - argparse enforces this.
        raise AssertionError(args.command)
    return 0

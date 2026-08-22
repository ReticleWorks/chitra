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
REQUIRED_INSTANCE_BINDINGS = frozenset(
    (
        "namespace",
        "state_root",
        "goals_sha256",
        "queue_sha256",
        "old_unit",
        "new_unit",
        "old_process",
        "new_process",
        "old_package",
        "new_package",
        "old_tmux_socket",
        "new_tmux_socket",
        "lane_worktrees_sha256",
        "last_old_order_sha256",
        "last_old_event_sha256",
        "pre_state_sha256",
        "post_state_sha256",
        "rollback_checkpoint_sha256",
    )
)
REQUIRED_LIFECYCLE_RECEIPTS = frozenset(("old_drained", "old_stopped", "old_write_denied", "new_started", "new_write_proved"))


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


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


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


def _as_mapping(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConversionError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


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
                "w1_journal_record": _w1_journal_record(record, source_path=source_path, source_sha256=source_sha256, legacy_index=index),
                "w2_enrollment_record": _w2_enrollment_record(record, legacy_index=index, is_terminal=is_terminal),
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


def _w1_journal_record(record: Mapping[str, object], *, source_path: str, source_sha256: str, legacy_index: int) -> dict[str, object]:
    """Build the canonical per-record journal row a read-only converter can prove."""
    session_ref = record.get("session_ref")
    payload_digest = _sha256_bytes(_canonical_bytes(record))
    return {
        "schema": "chitra.normalized-event-journal.v1",
        "event_id": f"legacy-goal:{source_sha256}:{legacy_index}",
        "source": {"path": source_path, "sha256": source_sha256, "legacy_index": legacy_index},
        "session_ref": session_ref if isinstance(session_ref, str) else "",
        "native_type": "legacy_goal_record",
        "normalized_type": "legacy_goal_imported_display_only",
        "payload_sha256": payload_digest,
        "normalizer_version": SCHEMA_GOALS_CONVERSION,
    }


def _w2_enrollment_record(record: Mapping[str, object], *, legacy_index: int, is_terminal: bool) -> dict[str, object]:
    """Bind the legacy record to W2 enrollment without inventing interviews."""
    session_ref = record.get("session_ref")
    return {
        "schema": "chitra.goal.v2.enrollment-boundary.v1",
        "legacy_index": legacy_index,
        "session_ref": session_ref if isinstance(session_ref, str) else "",
        "interview_receipt": None,
        "enrolled_done_when_items": [],
        "completion_proofs": [],
        "completion_eligible": False,
        "requires_old_authority_or_fresh_interview": not is_terminal,
    }


def _queue_stage(relative_path: str) -> str:
    if relative_path == "queue.tsv":
        return "legacy_tsv"
    parts = relative_path.split("/")
    if len(parts) >= 2 and parts[0] == "queue" and parts[1] in QUEUE_STAGES:
        return parts[1]
    return "other"


def _identity_from_json_payload(payload: Mapping[str, object], fallback: str) -> str:
    for key in ("order_id", "id", "receipt_name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _queue_entries_from_tsv(root: Path, path: Path) -> list[dict[str, object]]:
    rel = path.relative_to(root).as_posix()
    data = path.read_bytes()
    source_sha = _sha256_bytes(data)
    entries: list[dict[str, object]] = []
    offset = 0
    for line_number, raw_line in enumerate(data.splitlines(keepends=True), start=1):
        start = offset
        offset += len(raw_line)
        logical = raw_line.rstrip(b"\r\n")
        if not logical:
            continue
        text = logical.decode("utf-8", errors="surrogateescape")
        columns = text.split("\t")
        identity = columns[0] if columns and columns[0] else f"{rel}:{line_number}"
        entry: dict[str, object] = {
            "relative_path": rel,
            "stage": "legacy_tsv",
            "logical_kind": "tsv_order",
            "identity": identity,
            "order_id": identity,
            "session_ref": columns[1] if len(columns) > 1 else "",
            "source_file_sha256": source_sha,
            "sha256": _sha256_bytes(logical),
            "size": len(logical),
            "line_number": line_number,
            "byte_start": start,
            "byte_end": start + len(logical),
        }
        entries.append(entry)
    return entries


def _queue_entry_from_json_file(root: Path, path: Path) -> dict[str, object]:
    rel = path.relative_to(root).as_posix()
    data = path.read_bytes()
    stage = _queue_stage(rel)
    payload: Mapping[str, object] = {}
    if path.suffix == ".json":
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConversionError(f"{path} is not a valid dispatch JSON entry: {exc}") from exc
        payload = _as_mapping(parsed, name=str(path))
    identity = _identity_from_json_payload(payload, path.stem)
    session_ref = payload.get("session_ref")
    return {
        "relative_path": rel,
        "stage": stage,
        "logical_kind": "json_order",
        "identity": identity,
        "order_id": identity,
        "session_ref": session_ref if isinstance(session_ref, str) else "",
        "sha256": _sha256_bytes(data),
        "size": len(data),
    }


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
    entries: list[dict[str, object]] = []
    for path in candidates:
        if path.name == "queue.tsv":
            entries.extend(_queue_entries_from_tsv(root, path))
        else:
            entries.append(_queue_entry_from_json_file(root, path))
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
    """Write a topology preflight receipt for W10 metadata-only snapshots."""
    snapshot = _load_json(snapshot_path)
    snapshot_sha = _sha256_file(snapshot_path)
    goals = _snapshot_goals_manifests(snapshot)
    queues = _snapshot_queue_manifests(snapshot)
    goal_manifests = [
        {
            **entry,
            "preflight_only": True,
            "conversion_status": "metadata_only_record_not_converted",
        }
        for entry in goals
    ]
    goals_artifact: dict[str, object] = {
        "schema": SCHEMA_GOALS_CONVERSION,
        "produced_at": _utc_now(),
        "source": {"path": str(snapshot_path), "sha256": snapshot_sha, "mode": "w10-topology-preflight"},
        "input_counts": {"metadata_goal_count": sum(_as_int(entry.get("goal_count")) for entry in goals), "goals_files": len(goals)},
        "output_counts": {"legacy_goal_records": 0},
        "records": [],
        "goal_file_manifests": goal_manifests,
        "w1_journal_records": [],
        "w2_enrollment_records": [],
    }
    queue_artifact: dict[str, object] = {
        "schema": SCHEMA_QUEUE_REPLAY,
        "produced_at": _utc_now(),
        "source": {"path": str(snapshot_path), "sha256": snapshot_sha, "mode": "w10-topology-preflight"},
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
        mode="w10-topology-preflight",
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
    goals_path = output_dir / "legacy-goals-conversion.json"
    queue_path = output_dir / "dispatch-queue-replay.json"
    _write_json(goals_path, goals_artifact)
    _write_json(queue_path, queue_artifact)
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
    goals_path = output_dir / "legacy-goals-conversion.json"
    queue_path = output_dir / "dispatch-queue-replay.json"
    reread_goals = _load_json(goals_path)
    reread_queue = _load_json(queue_path)
    input_counts = _as_mapping(goals_artifact["input_counts"], name="goal input_counts")
    _as_mapping(reread_goals["output_counts"], name="goal output_counts")
    goal_input_count = _as_int(input_counts.get("goals"))
    if mode == "w10-topology-preflight":
        goal_input_count = 0
    goal_output_count = _as_int(cast(Mapping[str, object], goals_artifact["output_counts"]).get("legacy_goal_records"))
    queue_entries = cast(list[Mapping[str, object]], reread_queue.get("entries", []))
    queue_input_count = _as_int(queue_artifact.get("entry_count"))
    queue_output_count = _as_int(reread_queue.get("entry_count"))
    expected_identities = [
        (
            entry.get("ordinal"),
            entry.get("identity", f"{entry.get('relative_path')}:{entry.get('line_number', '')}"),
            entry.get("sha256"),
        )
        for entry in cast(list[Mapping[str, object]], queue_artifact.get("entries", []))
    ]
    reread_identities = [
        (
            entry.get("ordinal"),
            entry.get("identity", f"{entry.get('relative_path')}:{entry.get('line_number', '')}"),
            entry.get("sha256"),
        )
        for entry in queue_entries
    ]
    goal_records = cast(list[Mapping[str, object]], reread_goals.get("records", []))
    w1_bound = all(isinstance(record.get("w1_journal_record"), Mapping) for record in goal_records)
    w2_bound = all(isinstance(record.get("w2_enrollment_record"), Mapping) for record in goal_records)
    legacy_dispositions = [
        cast(Mapping[str, object], record.get("disposition"))
        for record in goal_records
        if isinstance(record.get("disposition"), Mapping)
    ]
    queue_reconciles = queue_input_count == queue_output_count and expected_identities == reread_identities
    goal_reconciles = goal_input_count == goal_output_count and w1_bound and w2_bound
    if mode == "w10-topology-preflight":
        status = "PREFLIGHT_ONLY"
    elif goal_reconciles and queue_reconciles:
        status = "PASS"
    else:
        status = "FAIL"
    receipt: dict[str, object] = {
        "schema": SCHEMA_CONVERSION_RECEIPT,
        "produced_at": _utc_now(),
        "mode": mode,
        "source": {"path": source, "sha256": source_sha256},
        "artifacts": {
            "legacy_goals_conversion": {
                "path": str(goals_path),
                "sha256": _sha256_file(goals_path),
            },
            "dispatch_queue_replay": {
                "path": str(queue_path),
                "sha256": _sha256_file(queue_path),
            },
        },
        "input_counts": {
            "legacy_goals": goal_input_count,
            "dispatch_queue_entries": queue_input_count,
        },
        "output_counts": {
            "legacy_goal_records": goal_output_count,
            "dispatch_queue_entries": queue_output_count,
        },
        "reconciliation": {
            "metadata_preflight_only": mode == "w10-topology-preflight",
            "goal_counts_match": goal_input_count == goal_output_count,
            "w1_journal_records_bound": w1_bound,
            "w2_enrollment_records_bound": w2_bound,
            "queue_count_order_identity_hashes_preserved": queue_reconciles,
            "legacy_goals_marked_display_dispose_only": bool(goal_records)
            and len(legacy_dispositions) == len(goal_records)
            and all(disposition.get("display_only") is True for disposition in legacy_dispositions),
            "legacy_goals_may_claim_done": any(
                disposition.get("done_transition_allowed") is True for disposition in legacy_dispositions
            ),
            "status": status,
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
    instance_bindings: Mapping[str, str] | None = None,
    lifecycle_receipts: Sequence[Mapping[str, object]] = (),
    native_client: Mapping[str, object] | None = None,
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
    for writer in (old_writer, new_writer):
        for field, value in writer.to_dict().items():
            if field in {"stopped", "started", "can_write", "last_order_sha256", "action_receipt_sha256"}:
                continue
            if not isinstance(value, str) or not value:
                issues.append(f"{writer.role} writer {field} is missing")
        if writer.role == "old" and not _is_sha256(writer.last_order_sha256):
            issues.append("old writer last order hash is missing")
        if writer.role == "new" and not _is_sha256(writer.action_receipt_sha256):
            issues.append("new writer action receipt hash is missing")
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
    if not instance:
        issues.append("instance is missing")
    for label, value in (
        ("pre_state_sha256", pre_state_sha256),
        ("post_state_sha256", post_state_sha256),
        ("rollback_checkpoint_sha256", rollback_checkpoint_sha256),
    ):
        if not _is_sha256(value):
            issues.append(f"{label} must be a SHA-256 digest")
    bindings = dict(instance_bindings or {})
    missing_bindings = sorted(REQUIRED_INSTANCE_BINDINGS - set(bindings))
    if missing_bindings:
        issues.append(f"missing instance bindings: {', '.join(missing_bindings)}")
    for key, value in bindings.items():
        if not isinstance(value, str) or not value:
            issues.append(f"instance binding {key} is empty")
        if key.endswith("_sha256") and not _is_sha256(value):
            issues.append(f"instance binding {key} must be a SHA-256 digest")
    if not transcript_bindings or not all(_is_sha256(binding) for binding in transcript_bindings):
        issues.append("transcript bindings must contain externally observed SHA-256 digests")
    lifecycle_by_kind = {str(item.get("kind")): item for item in lifecycle_receipts if isinstance(item, Mapping)}
    missing_lifecycle = sorted(REQUIRED_LIFECYCLE_RECEIPTS - set(lifecycle_by_kind))
    if missing_lifecycle:
        issues.append(f"missing lifecycle receipts: {', '.join(missing_lifecycle)}")
    for lifecycle_receipt in lifecycle_receipts:
        for key in ("kind", "observer", "subject", "observed_at"):
            value = lifecycle_receipt.get(key)
            if not isinstance(value, str) or not value:
                issues.append(f"lifecycle receipt {lifecycle_receipt.get('kind', '<unknown>')} missing {key}")
        if not _is_sha256(lifecycle_receipt.get("artifact_sha256")):
            issues.append(f"lifecycle receipt {lifecycle_receipt.get('kind', '<unknown>')} missing artifact hash")
    client = dict(native_client or {})
    for key in ("client_name", "version", "path", "path_sha256"):
        value = client.get(key)
        if not isinstance(value, str) or not value:
            issues.append(f"native client binding {key} is missing")
    if not _is_sha256(client.get("path_sha256")):
        issues.append("native client path_sha256 must be a SHA-256 digest")
    if client.get("update_suppression") is not False:
        issues.append("native client binding must prove no update-suppression flag")
    handoff_receipt: dict[str, object] = {
        "schema": SCHEMA_HANDOFF_RECEIPT,
        "produced_at": _utc_now(),
        "instance": instance,
        "writers": [old_writer.to_dict(), new_writer.to_dict()],
        "state_hashes": {
            "pre_handoff": pre_state_sha256,
            "post_handoff": post_state_sha256,
            "rollback_checkpoint": rollback_checkpoint_sha256,
        },
        "instance_bindings": bindings,
        "transcript_bindings": list(transcript_bindings),
        "lifecycle_receipts": [dict(item) for item in lifecycle_receipts],
        "native_client": client,
        "cutover_sequence": list(cutover_sequence),
        "exactly_one_writer": len(active_writers) == 1 and active_writers == [new_writer.name],
        "issues": issues,
        "status": "PASS" if not issues else "FAIL",
    }
    handoff_receipt["sha256"] = _sha256_bytes(_canonical_bytes(handoff_receipt))
    if issues:
        raise ConversionError("; ".join(issues))
    return handoff_receipt


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
        payload = _load_json(goals)
    except ConversionError:
        return False
    if payload.get("schema") == "chitra.goals.v3":
        return True
    if payload.get("schema") == "chitra.goals.v2":
        records = payload.get("goals")
        if isinstance(records, list):
            return any(
                isinstance(record, Mapping)
                and (
                    bool(record.get("interview_receipt"))
                    or bool(record.get("enrolled_done_when_items"))
                    or bool(record.get("completion_proofs"))
                )
                for record in records
            )
    return False


def _validate_snapshot_payload(snapshot_dir: Path, marker: Mapping[str, object]) -> None:
    snapshot_payload_dir = snapshot_dir / "state-root"
    manifest_path = snapshot_dir / "manifest.json"
    if not snapshot_payload_dir.exists():
        raise ConversionError("rollback snapshot payload is missing")
    if not manifest_path.exists():
        raise ConversionError("rollback snapshot manifest is missing")
    manifest_payload = _load_json(manifest_path)
    files = manifest_payload.get("files")
    if not isinstance(files, list):
        raise ConversionError("rollback snapshot manifest files must be a list")
    expected_hash = marker.get("manifest_sha256")
    manifest_hash = _manifest_digest(cast(list[Mapping[str, object]], files))
    payload_hash = _manifest_digest(_file_manifest(snapshot_payload_dir))
    if manifest_hash != expected_hash or payload_hash != expected_hash:
        raise ConversionError("rollback snapshot manifest does not match payload")


def restore_snapshot(snapshot_dir: Path, state_root: Path, *, allow_v3_loss: bool = False) -> dict[str, object]:
    """Restore a disposable snapshot and verify the post-restore hashes."""
    snapshot_dir = _safe_resolve(snapshot_dir)
    state_root = _safe_resolve(state_root)
    marker_path = snapshot_dir / SNAPSHOT_MARKER
    if not marker_path.exists():
        raise ConversionError("rollback snapshot marker is missing")
    marker = _load_json(marker_path)
    snapshot_payload_dir = snapshot_dir / "state-root"
    _validate_snapshot_payload(snapshot_dir, marker)
    if _contains_v3_or_receipts(state_root) and not allow_v3_loss:
        raise ConversionError("rollback would lose v2/v3 enrollment or validation evidence")
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

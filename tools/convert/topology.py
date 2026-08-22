"""Read-only topology conversion and handoff receipts.

These helpers intentionally do not dispatch messages, manage services, or
touch live hosts.  They turn legacy state into hash-bound receipts that a
separate migration/canary step can consume.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, cast

SCHEMA_CONVERSION_RECEIPT = "chitra.topology-conversion-receipt.v1"
SCHEMA_GOALS_CONVERSION = "chitra.legacy-goals-conversion.v1"
SCHEMA_QUEUE_REPLAY = "chitra.dispatch-queue-replay.v1"
SCHEMA_SHADOW_FINDINGS = "chitra.topology-shadow-findings.v1"
SCHEMA_HANDOFF_RECEIPT = "chitra.authority-handoff-receipt.v1"
SCHEMA_ROLLBACK_RECEIPT = "chitra.disposable-rollback-receipt.v1"
SCHEMA_LIFECYCLE_PROOF = "chitra.authority-handoff-lifecycle-proof.v1"
SCHEMA_NATIVE_CLIENT_PROOF = "chitra.native-client-proof.v1"
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
        "new_action_receipt_sha256",
        "pre_state_sha256",
        "post_state_sha256",
        "rollback_checkpoint_sha256",
        "goals_path",
        "queue_root",
        "lane_worktrees_path",
        "last_old_order_path",
        "last_old_event_path",
        "new_action_receipt_path",
        "pre_state_manifest_path",
        "rollback_checkpoint_path",
        "new_process_exe_path",
        "new_process_exe_sha256",
    )
)
REQUIRED_LIFECYCLE_RECEIPTS = frozenset(("old_drained", "old_stopped", "old_write_denied", "new_started", "new_write_proved"))
APPROVED_NATIVE_CLIENTS = frozenset(("codex",))
APPROVED_NATIVE_CLIENT_PATHS = {"codex": ("/usr/local/bin/codex", "/usr/bin/codex", "/opt/chitra/venv/bin/codex")}
AUTHORITY_LEDGER_TAG = "[authority-handoff]"
LIFECYCLE_ARTIFACT_FIELDS = frozenset(
    (
        "schema",
        "kind",
        "observer",
        "subject",
        "observed_at",
        "instance",
        "writer_role",
        "unit",
        "process",
        "package",
        "state_root",
        "tmux_socket",
        "process_alive",
        "state_root_sha256",
        "evidence_sha256",
        "stopped",
        "started",
        "can_write",
    )
)


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


def _canonical_event_model() -> type[Any]:
    return cast(type[Any], import_module("chitra.journal.models").CanonicalEvent)


def _goal_record_model() -> type[Any]:
    return cast(type[Any], import_module("chitra.goals").GoalRecord)


def _parse_iso8601(value: str, *, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConversionError(f"{label} must be an ISO8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ConversionError(f"{label} must include a timezone")


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


def _pid_from_process(value: str) -> int | None:
    if not value.startswith("pid:"):
        return None
    raw = value.removeprefix("pid:")
    if not raw.isdigit():
        return None
    pid = int(raw)
    return pid if pid > 0 else None


def _process_exists(process: str) -> bool | None:
    pid = _pid_from_process(process)
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _unix_socket_exists(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def _tmux_socket_identity(path: Path) -> dict[str, str] | None:
    try:
        result = subprocess.run(
            ["tmux", "-S", str(path), "display-message", "-p", "#{session_name}\t#{pid}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if not output:
        return None
    parts = output.split("\t")
    if len(parts) != 2 or not parts[0] or not parts[1].isdigit():
        return None
    return {"socket_path": str(path), "session_name": parts[0], "server_pid": parts[1]}


def _process_executable_sha256(process: str) -> tuple[Path, str] | None:
    pid = _pid_from_process(process)
    if pid is None:
        return None
    exe_link = Path("/proc") / str(pid) / "exe"
    try:
        exe_path = exe_link.resolve(strict=True)
    except OSError:
        return None
    if not exe_path.exists() or not exe_path.is_file():
        return None
    return exe_path, _sha256_file(exe_path)


def _goals_have_enrolled_authority(path: Path) -> bool:
    try:
        payload = _load_json(path)
    except ConversionError:
        return False
    if payload.get("schema") not in {"chitra.goals.v2", "chitra.goals.v3"}:
        return False
    goals = payload.get("goals")
    if not isinstance(goals, list):
        return False
    for item in goals:
        if not isinstance(item, Mapping):
            continue
        receipt = item.get("interview_receipt")
        enrolled_items = item.get("enrolled_done_when_items")
        enrolled_at = item.get("enrolled_at")
        if isinstance(receipt, Mapping) and isinstance(enrolled_items, list) and enrolled_items:
            return True
        if isinstance(enrolled_at, str) and enrolled_at and isinstance(enrolled_items, list) and enrolled_items:
            return True
    return False


def _governed_lane_binding(instance: str, state_root: Path, tmux_socket: Path, issues: list[str]) -> dict[str, object] | None:
    try:
        load_lanes = import_module("chitra.lane_config").load_lanes
    except Exception as exc:  # pragma: no cover - import failure is reported as a conversion issue
        issues.append(f"governed lane manifest cannot be imported: {exc}")
        return None
    try:
        lanes = load_lanes()
    except ValueError as exc:
        issues.append(f"governed lane manifest cannot be loaded: {exc}")
        return None
    matching = [lane for lane in lanes if lane.identifier == instance]
    if len(matching) != 1:
        issues.append("governed lane manifest must declare exactly one matching instance")
        return None
    lane = matching[0]
    if _safe_resolve(lane.state_dir) != state_root:
        issues.append("governed lane manifest state_dir contradicts observed state root")
    if _safe_resolve(lane.tmux_socket) != tmux_socket:
        issues.append("governed lane manifest tmux_socket contradicts observed tmux socket")
    return {
        "id": lane.identifier,
        "account": lane.account,
        "uid": lane.uid,
        "state_dir": str(_safe_resolve(lane.state_dir)),
        "tmux_socket": str(_safe_resolve(lane.tmux_socket)),
        "tmux_session": lane.tmux_session,
    }


def _approved_native_client_path(client_name: str) -> Path | None:
    for raw_path in APPROVED_NATIVE_CLIENT_PATHS.get(client_name, ()):
        path = _safe_resolve(Path(raw_path))
        if path.exists() and path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _verify_authority_ledger_proof(
    *,
    authority_proof: Mapping[str, object] | None,
    state_root: Path | None,
    instance: str,
    expected_payload: Mapping[str, object],
    issues: list[str],
) -> dict[str, object]:
    if state_root is None:
        issues.append("authority ledger proof requires an observed governed state root")
        return {}
    proof = dict(authority_proof or {})
    for key in ("order_id", "ledger_entry"):
        if key not in proof:
            issues.append(f"authority ledger proof {key} is missing")
    entry_payload = proof.get("ledger_entry")
    if not isinstance(entry_payload, Mapping):
        issues.append("authority ledger proof ledger_entry must be an object")
        return proof
    ledger_path = state_root / "ledger.jsonl"
    key_path = state_root / "ledger.key"
    if not key_path.exists() or not key_path.is_file():
        issues.append("authority ledger key is missing from governed state root")
        return proof
    if not ledger_path.exists() or not ledger_path.is_file():
        issues.append("authority ledger is missing from governed state root")
        return proof
    try:
        ledger_module = import_module("chitra.ledger")
        LedgerEntry = ledger_module.LedgerEntry
        verify_entry = ledger_module.verify_entry
    except Exception as exc:  # pragma: no cover - import failure is reported as a conversion issue
        issues.append(f"authority ledger verifier cannot be imported: {exc}")
        return proof
    try:
        entry = LedgerEntry.model_validate(entry_payload)
    except ValueError as exc:
        issues.append(f"authority ledger entry is invalid: {exc}")
        return proof
    expected_digest = _sha256_bytes(_canonical_bytes(expected_payload))
    if entry.order_id != proof.get("order_id"):
        issues.append("authority ledger entry order_id contradicts proof")
    if entry.session_ref != f"authority:{instance}":
        issues.append("authority ledger entry session_ref does not bind the handoff instance")
    if entry.tag != AUTHORITY_LEDGER_TAG:
        issues.append("authority ledger entry tag is not the governed handoff tag")
    if entry.message_hash != expected_digest:
        issues.append("authority ledger entry hash does not bind the observed handoff payload")
    try:
        hmac_key = key_path.read_bytes()
    except OSError as exc:
        issues.append(f"authority ledger key cannot be read: {exc}")
        return proof
    if not verify_entry(entry, key=hmac_key):
        issues.append("authority ledger entry signature is invalid")
    entry_json = entry.model_dump_json()
    if entry_json not in ledger_path.read_text(encoding="utf-8").splitlines():
        issues.append("authority ledger entry is not present in the governed append-only ledger")
    proof["ledger_path"] = str(ledger_path)
    proof["ledger_key_path"] = str(key_path)
    proof["payload_sha256"] = expected_digest
    return proof


def _authority_ledger_payload(
    *,
    instance: str,
    old_writer: WriterObservation,
    new_writer: WriterObservation,
    pre_state_sha256: str,
    post_state_sha256: str,
    rollback_checkpoint_sha256: str,
    instance_bindings: Mapping[str, object],
    transcript_bindings: Sequence[Mapping[str, object]],
    lifecycle_receipts: Sequence[Mapping[str, object]],
    native_client: Mapping[str, object],
    cutover_sequence: Sequence[str],
    governed_lane: Mapping[str, object] | None,
    tmux_sessions: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema": "chitra.authority-handoff-ledger-payload.v1",
        "instance": instance,
        "writers": [old_writer.to_dict(), new_writer.to_dict()],
        "state_hashes": {
            "pre_handoff": pre_state_sha256,
            "post_handoff": post_state_sha256,
            "rollback_checkpoint": rollback_checkpoint_sha256,
        },
        "instance_bindings": dict(instance_bindings),
        "transcript_bindings": [dict(item) for item in transcript_bindings],
        "lifecycle_receipts": [dict(item) for item in lifecycle_receipts],
        "native_client": dict(native_client),
        "cutover_sequence": list(cutover_sequence),
        "governed_lane": dict(governed_lane or {}),
        "tmux_sessions": dict(tmux_sessions),
    }


def _operational_state_manifest_digest(root: Path) -> str:
    authority_files = {"ledger.jsonl", "ledger.key", "ledger.jsonl.lock"}
    return _manifest_digest([entry for entry in _file_manifest(root) if entry.get("relative_path") not in authority_files])


def _jsonl_transcript_is_valid(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines:
        return False
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        if not any(isinstance(payload.get(key), str) and payload.get(key) for key in ("schema", "type", "event")):
            return False
    return True


def _observed_file_sha256(
    bindings: Mapping[str, str],
    path_key: str,
    digest_key: str,
    *,
    state_root: Path,
    issues: list[str],
) -> str | None:
    raw_path = bindings.get(path_key)
    expected_sha = bindings.get(digest_key)
    if not raw_path:
        issues.append(f"instance binding {path_key} is missing")
        return None
    path = _safe_resolve(Path(raw_path))
    if not _is_relative_to(path, state_root):
        issues.append(f"instance binding {path_key} must be inside the observed state root")
        return None
    if not path.exists() or not path.is_file():
        issues.append(f"instance binding {path_key} does not exist")
        return None
    actual_sha = _sha256_file(path)
    if expected_sha != actual_sha:
        issues.append(f"instance binding {digest_key} does not match {path_key} bytes")
    return actual_sha


def _observed_tree_sha256(
    bindings: Mapping[str, str],
    path_key: str,
    digest_key: str,
    *,
    state_root: Path,
    issues: list[str],
) -> str | None:
    raw_path = bindings.get(path_key)
    expected_sha = bindings.get(digest_key)
    if not raw_path:
        issues.append(f"instance binding {path_key} is missing")
        return None
    path = _safe_resolve(Path(raw_path))
    if not _is_relative_to(path, state_root):
        issues.append(f"instance binding {path_key} must be inside the observed state root")
        return None
    if not path.exists() or not path.is_dir():
        issues.append(f"instance binding {path_key} does not exist")
        return None
    actual_sha = _manifest_digest(_file_manifest(path))
    if expected_sha != actual_sha:
        issues.append(f"instance binding {digest_key} does not match {path_key} manifest")
    return actual_sha


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


def _legacy_identity(record: Mapping[str, object]) -> tuple[str, str, str]:
    session_ref = record.get("session_ref")
    session = session_ref if isinstance(session_ref, str) and session_ref else "legacy-session"
    parts = session.split(":")
    instance = parts[0] if parts and parts[0] else "legacy"
    lane = parts[1] if len(parts) > 1 and parts[1] else instance
    return instance, lane, session


def _source_transcript_identity(source_path: str) -> dict[str, object]:
    try:
        stat = Path(source_path).stat()
    except OSError:
        return {"path": source_path, "device": 0, "inode": 0, "generation": 0}
    return {"path": source_path, "device": stat.st_dev, "inode": stat.st_ino, "generation": 0}


def _legacy_observed_at(record: Mapping[str, object]) -> str:
    for key in ("updated_at", "created_at"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return _utc_now()


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
    payload_digest = _sha256_bytes(_canonical_bytes(record))
    instance, lane, session_id = _legacy_identity(record)
    event = {
        "schema": "chitra.journal.event.v1",
        "event_id": f"legacy-goal:{source_sha256}:{legacy_index}",
        "instance": instance,
        "lane": lane,
        "client": "codex",
        "client_version": SCHEMA_GOALS_CONVERSION,
        "process_id": "legacy-goals-converter",
        "transcript": _source_transcript_identity(source_path),
        "session_id": session_id,
        "resume_id": None,
        "observed_at": _legacy_observed_at(record),
        "native_time": _legacy_observed_at(record),
        "native_type": "legacy_goal_record",
        "native_join_id": None,
        "raw_byte_range": None,
        "raw_sha256": payload_digest,
        "lifecycle_receipt": None,
        "normalized_type": "unknown",
        "goal_ref": session_id,
        "item_ref": None,
        "payload_sha256": payload_digest,
        "payload_digest": payload_digest,
        "normalizer_version": SCHEMA_GOALS_CONVERSION,
        "payload": {
            "source": {"path": source_path, "sha256": source_sha256, "legacy_index": legacy_index},
            "legacy_record_sha256": payload_digest,
            "display_only": True,
            "administrative_dispose_only": True,
        },
        "raw_record": dict(record),
    }
    return cast(dict[str, object], _canonical_event_model().model_validate(event).model_dump(mode="json", by_alias=True))


def _w2_enrollment_record(record: Mapping[str, object], *, legacy_index: int, is_terminal: bool) -> dict[str, object]:
    """Bind the legacy record to W2 enrollment without inventing interviews."""
    goal_record_model = _goal_record_model()
    parsed = goal_record_model.from_dict(record)
    converted = cast(dict[str, object], parsed.to_dict())
    converted["legacy_index"] = legacy_index
    converted["legacy_display_only"] = True
    converted["completion_eligible"] = False
    converted["requires_old_authority_or_fresh_interview"] = not is_terminal
    goal_record_model.from_dict(converted)
    return converted


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
    w1_bound = True
    w2_bound = True
    for record in goal_records:
        try:
            _canonical_event_model().model_validate(record.get("w1_journal_record"))
        except Exception:
            w1_bound = False
        try:
            _goal_record_model().from_dict(record.get("w2_enrollment_record"))
        except Exception:
            w2_bound = False
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
    authority_proof: Mapping[str, object] | None = None,
    cutover_sequence: Sequence[str] = ("old-drained", "old-stopped", "old-write-denied", "new-started", "new-write-proved"),
) -> dict[str, object]:
    """Seal an artifact-backed authority handoff proof."""
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
    expected_bindings = {
        "namespace": instance,
        "old_unit": old_writer.unit,
        "new_unit": new_writer.unit,
        "old_process": old_writer.process,
        "new_process": new_writer.process,
        "old_package": old_writer.package,
        "new_package": new_writer.package,
        "last_old_order_sha256": old_writer.last_order_sha256,
        "new_action_receipt_sha256": new_writer.action_receipt_sha256,
        "pre_state_sha256": pre_state_sha256,
        "post_state_sha256": post_state_sha256,
        "rollback_checkpoint_sha256": rollback_checkpoint_sha256,
    }
    for key, expected in expected_bindings.items():
        if bindings.get(key) != expected:
            issues.append(f"instance binding {key} contradicts observed handoff fact")
    observed_state_root: Path | None = None
    observed_post_state_sha256: str | None = None
    state_root_value = bindings.get("state_root")
    if isinstance(state_root_value, str) and state_root_value:
        state_root_path = _safe_resolve(Path(state_root_value))
        if not state_root_path.exists() or not state_root_path.is_dir():
            issues.append("instance binding state_root does not exist")
        else:
            observed_state_root = state_root_path
            bindings["state_root"] = str(state_root_path)
            observed_post_state_sha256 = _operational_state_manifest_digest(state_root_path)
            if post_state_sha256 != observed_post_state_sha256:
                issues.append("post_state_sha256 does not match observed state root manifest")
    for socket_key in ("old_tmux_socket", "new_tmux_socket"):
        socket_value = bindings.get(socket_key)
        if isinstance(socket_value, str) and socket_value:
            socket_path = _safe_resolve(Path(socket_value))
            if not _unix_socket_exists(socket_path):
                issues.append(f"instance binding {socket_key} is not an observed Unix socket")
            else:
                bindings[socket_key] = str(socket_path)
    tmux_sessions: dict[str, object] = {}
    for role, socket_key in (("old", "old_tmux_socket"), ("new", "new_tmux_socket")):
        socket_value = bindings.get(socket_key)
        if isinstance(socket_value, str) and socket_value:
            identity = _tmux_socket_identity(Path(socket_value))
            if identity is None:
                issues.append(f"instance binding {socket_key} is not a live tmux protocol socket")
            else:
                tmux_sessions[role] = identity
    old_process_alive = _process_exists(old_writer.process)
    new_process_alive = _process_exists(new_writer.process)
    if old_process_alive is None:
        issues.append("old writer process must be an observable pid:<int>")
    elif old_process_alive:
        issues.append("old writer process is still live")
    if new_process_alive is None:
        issues.append("new writer process must be an observable pid:<int>")
    elif not new_process_alive:
        issues.append("new writer process is not live")
    new_process_exe = _process_executable_sha256(new_writer.process)
    if new_process_exe is None:
        issues.append("new writer process executable identity cannot be observed")
    else:
        new_process_exe_path, new_process_exe_sha = new_process_exe
        if bindings.get("new_process_exe_path") != str(new_process_exe_path):
            issues.append("instance binding new_process_exe_path contradicts observed process executable")
        if bindings.get("new_process_exe_sha256") != new_process_exe_sha:
            issues.append("instance binding new_process_exe_sha256 contradicts observed process executable bytes")
    if observed_state_root is not None:
        _observed_file_sha256(bindings, "goals_path", "goals_sha256", state_root=observed_state_root, issues=issues)
        _observed_tree_sha256(bindings, "queue_root", "queue_sha256", state_root=observed_state_root, issues=issues)
        _observed_file_sha256(
            bindings, "lane_worktrees_path", "lane_worktrees_sha256", state_root=observed_state_root, issues=issues
        )
        _observed_file_sha256(
            bindings, "last_old_order_path", "last_old_order_sha256", state_root=observed_state_root, issues=issues
        )
        _observed_file_sha256(
            bindings, "last_old_event_path", "last_old_event_sha256", state_root=observed_state_root, issues=issues
        )
        _observed_file_sha256(
            bindings,
            "new_action_receipt_path",
            "new_action_receipt_sha256",
            state_root=observed_state_root,
            issues=issues,
        )
        _observed_file_sha256(
            bindings, "pre_state_manifest_path", "pre_state_sha256", state_root=observed_state_root, issues=issues
        )
        _observed_file_sha256(
            bindings,
            "rollback_checkpoint_path",
            "rollback_checkpoint_sha256",
            state_root=observed_state_root,
            issues=issues,
        )
        goals_path_value = bindings.get("goals_path")
        if isinstance(goals_path_value, str) and not _goals_have_enrolled_authority(Path(goals_path_value)):
            issues.append("governed state root lacks enrolled goal authority")
    governed_lane: dict[str, object] | None = None
    new_tmux_socket_value = bindings.get("new_tmux_socket")
    if observed_state_root is not None and isinstance(new_tmux_socket_value, str) and new_tmux_socket_value:
        governed_lane = _governed_lane_binding(instance, observed_state_root, Path(new_tmux_socket_value), issues)
    verified_transcripts: list[dict[str, object]] = []
    if not transcript_bindings:
        issues.append("transcript bindings must name externally observed transcript files")
    for raw_path in transcript_bindings:
        path = _safe_resolve(Path(raw_path))
        if not path.exists() or not path.is_file():
            issues.append(f"transcript binding {raw_path} does not exist")
            continue
        if observed_state_root is None or not _is_relative_to(path, observed_state_root / "transcripts"):
            issues.append(f"transcript binding {raw_path} is outside the observed transcript root")
            continue
        if not _jsonl_transcript_is_valid(path):
            issues.append(f"transcript binding {raw_path} is not a valid observed JSONL transcript")
            continue
        verified_transcripts.append({"path": str(path), "sha256": _sha256_file(path)})
    lifecycle_by_kind = {str(item.get("kind")): item for item in lifecycle_receipts if isinstance(item, Mapping)}
    missing_lifecycle = sorted(REQUIRED_LIFECYCLE_RECEIPTS - set(lifecycle_by_kind))
    if missing_lifecycle:
        issues.append(f"missing lifecycle receipts: {', '.join(missing_lifecycle)}")
    lifecycle_role_by_kind = {
        "old_drained": "old",
        "old_stopped": "old",
        "old_write_denied": "old",
        "new_started": "new",
        "new_write_proved": "new",
    }
    verified_lifecycle: list[dict[str, object]] = []
    for lifecycle_receipt in lifecycle_receipts:
        for key in ("kind", "observer", "subject", "observed_at"):
            value = lifecycle_receipt.get(key)
            if not isinstance(value, str) or not value:
                issues.append(f"lifecycle receipt {lifecycle_receipt.get('kind', '<unknown>')} missing {key}")
        if not _is_sha256(lifecycle_receipt.get("artifact_sha256")):
            issues.append(f"lifecycle receipt {lifecycle_receipt.get('kind', '<unknown>')} missing artifact hash")
        observed_at = lifecycle_receipt.get("observed_at")
        if isinstance(observed_at, str) and observed_at:
            try:
                _parse_iso8601(observed_at, label=f"lifecycle receipt {lifecycle_receipt.get('kind', '<unknown>')} observed_at")
            except ConversionError as exc:
                issues.append(str(exc))
        artifact_path = lifecycle_receipt.get("artifact_path")
        if not isinstance(artifact_path, str) or not artifact_path:
            issues.append(f"lifecycle receipt {lifecycle_receipt.get('kind', '<unknown>')} missing artifact_path")
            continue
        path = _safe_resolve(Path(artifact_path))
        if not path.exists() or not path.is_file():
            issues.append(f"lifecycle receipt {lifecycle_receipt.get('kind', '<unknown>')} artifact_path does not exist")
            continue
        actual_sha = _sha256_file(path)
        if lifecycle_receipt.get("artifact_sha256") != actual_sha:
            issues.append(f"lifecycle receipt {lifecycle_receipt.get('kind', '<unknown>')} artifact hash does not match bytes read")
            continue
        try:
            artifact = _load_json(path)
        except ConversionError as exc:
            issues.append(str(exc))
            continue
        kind = str(lifecycle_receipt.get("kind"))
        unknown_artifact_fields = sorted(set(artifact) - LIFECYCLE_ARTIFACT_FIELDS)
        if unknown_artifact_fields:
            issues.append(f"lifecycle receipt {kind} artifact has unsupported fields: {', '.join(unknown_artifact_fields)}")
        if artifact.get("schema") != SCHEMA_LIFECYCLE_PROOF:
            issues.append(f"lifecycle receipt {kind} artifact schema is invalid")
        for key in ("kind", "observer", "subject", "observed_at"):
            if artifact.get(key) != lifecycle_receipt.get(key):
                issues.append(f"lifecycle receipt {kind} artifact {key} contradicts receipt")
        try:
            _parse_iso8601(str(artifact.get("observed_at", "")), label=f"lifecycle receipt {kind} artifact observed_at")
        except ConversionError as exc:
            issues.append(str(exc))
        expected_role = lifecycle_role_by_kind.get(kind)
        writer = old_writer if expected_role == "old" else new_writer
        writer_process_alive = old_process_alive if expected_role == "old" else new_process_alive
        writer_socket = bindings.get("old_tmux_socket" if expected_role == "old" else "new_tmux_socket", "")
        expected_evidence_by_kind = {
            "old_drained": old_writer.last_order_sha256,
            "old_stopped": pre_state_sha256,
            "old_write_denied": bindings.get("last_old_event_sha256", ""),
            "new_started": post_state_sha256,
            "new_write_proved": new_writer.action_receipt_sha256,
        }
        if artifact.get("instance") != instance:
            issues.append(f"lifecycle receipt {kind} artifact instance contradicts handoff instance")
        if artifact.get("writer_role") != expected_role:
            issues.append(f"lifecycle receipt {kind} artifact writer_role is invalid")
        expected_artifact_values: dict[str, object] = {
            "unit": writer.unit,
            "process": writer.process,
            "package": writer.package,
            "state_root": bindings.get("state_root", ""),
            "tmux_socket": writer_socket,
            "process_alive": writer_process_alive,
            "state_root_sha256": observed_post_state_sha256,
            "evidence_sha256": expected_evidence_by_kind.get(kind, ""),
        }
        for key, artifact_expected in expected_artifact_values.items():
            if artifact.get(key) != artifact_expected:
                issues.append(f"lifecycle receipt {kind} artifact {key} contradicts binding")
        expected_subject = f"{instance}:{expected_role}:{writer.unit}:{kind}"
        if artifact.get("subject") != expected_subject:
            issues.append(f"lifecycle receipt {kind} artifact subject contradicts observed writer")
        if artifact.get("observer") != "chitra-authority-verifier":
            issues.append(f"lifecycle receipt {kind} artifact observer is not governed")
        if kind == "old_stopped" and artifact.get("stopped") is not True:
            issues.append("old_stopped lifecycle artifact must prove stopped=true")
        if kind == "old_write_denied" and artifact.get("can_write") is not False:
            issues.append("old_write_denied lifecycle artifact must prove can_write=false")
        if kind == "new_started" and artifact.get("started") is not True:
            issues.append("new_started lifecycle artifact must prove started=true")
        if kind == "new_write_proved" and artifact.get("can_write") is not True:
            issues.append("new_write_proved lifecycle artifact must prove can_write=true")
        verified_lifecycle.append({**dict(lifecycle_receipt), "artifact_path": str(path), "artifact_sha256": actual_sha})
    client = dict(native_client or {})
    for key in ("client_name", "version", "path", "path_sha256", "version_output", "version_proof_path", "version_proof_sha256"):
        value = client.get(key)
        if not isinstance(value, str) or not value:
            issues.append(f"native client binding {key} is missing")
    client_name = client.get("client_name")
    if client_name not in APPROVED_NATIVE_CLIENTS:
        issues.append("native client binding client_name is not approved")
    if not _is_sha256(client.get("path_sha256")):
        issues.append("native client path_sha256 must be a SHA-256 digest")
    if client.get("update_suppression") is not False:
        issues.append("native client binding must prove no update-suppression flag")
    client_path_value = client.get("path")
    if isinstance(client_path_value, str) and client_path_value:
        client_path = _safe_resolve(Path(client_path_value))
        approved_path = _approved_native_client_path(client_name) if isinstance(client_name, str) else None
        if not client_path.exists() or not client_path.is_file():
            issues.append("native client path does not exist")
        elif not os.access(client_path, os.X_OK):
            issues.append("native client path is not executable")
        elif approved_path is None or approved_path != client_path:
            issues.append("native client path does not match the approved verifier policy executable")
        else:
            actual_client_sha = _sha256_file(client_path)
            if client.get("path_sha256") != actual_client_sha:
                issues.append("native client path_sha256 does not match executable bytes")
            try:
                version_result = subprocess.run(
                    [str(client_path), "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                issues.append(f"native client version output cannot be observed: {exc}")
            else:
                version_output = "\n".join(
                    part.strip() for part in (version_result.stdout, version_result.stderr) if part.strip()
                )
                if version_result.returncode != 0:
                    issues.append("native client version command failed")
                if client.get("version_output") != version_output:
                    issues.append("native client version_output does not match executable output")
                if isinstance(client_name, str) and client_name not in version_output:
                    issues.append("native client version output does not identify the approved client")
                version = client.get("version")
                if isinstance(version, str) and version not in version_output:
                    issues.append("native client version output does not match declared version")
            client["path"] = str(client_path)
    proof_path_value = client.get("version_proof_path")
    if isinstance(proof_path_value, str) and proof_path_value:
        proof_path = _safe_resolve(Path(proof_path_value))
        if not proof_path.exists() or not proof_path.is_file():
            issues.append("native client version proof path does not exist")
        else:
            actual_proof_sha = _sha256_file(proof_path)
            if client.get("version_proof_sha256") != actual_proof_sha:
                issues.append("native client version proof hash does not match bytes read")
            try:
                proof = _load_json(proof_path)
            except ConversionError as exc:
                issues.append(str(exc))
            else:
                if proof.get("schema") != SCHEMA_NATIVE_CLIENT_PROOF:
                    issues.append("native client version proof schema is invalid")
                for key in ("client_name", "version", "path", "path_sha256", "version_output", "update_suppression"):
                    if proof.get(key) != client.get(key):
                        issues.append(f"native client version proof {key} contradicts binding")
            client["version_proof_path"] = str(proof_path)
    authority_payload = _authority_ledger_payload(
        instance=instance,
        old_writer=old_writer,
        new_writer=new_writer,
        pre_state_sha256=pre_state_sha256,
        post_state_sha256=post_state_sha256,
        rollback_checkpoint_sha256=rollback_checkpoint_sha256,
        instance_bindings=bindings,
        transcript_bindings=verified_transcripts,
        lifecycle_receipts=verified_lifecycle,
        native_client=client,
        cutover_sequence=cutover_sequence,
        governed_lane=governed_lane,
        tmux_sessions=tmux_sessions,
    )
    verified_authority_proof = _verify_authority_ledger_proof(
        authority_proof=authority_proof,
        state_root=observed_state_root,
        instance=instance,
        expected_payload=authority_payload,
        issues=issues,
    )
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
        "transcript_bindings": verified_transcripts,
        "lifecycle_receipts": verified_lifecycle,
        "native_client": client,
        "governed_lane": governed_lane or {},
        "tmux_sessions": tmux_sessions,
        "authority_proof": verified_authority_proof,
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
    if allow_v3_loss and _contains_v3_or_receipts(state_root):
        raise ConversionError("allow_v3_loss cannot bypass protected v2/v3 enrollment or validation evidence")
    if _contains_v3_or_receipts(state_root):
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

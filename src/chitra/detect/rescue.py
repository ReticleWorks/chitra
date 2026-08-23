"""Bounded RESCUE capture bundles and relaunch briefs.

RESCUE is the only authorized precursor to a kill: it captures the exact
evidence an operator or successor session needs (transcript reference, pane
capture, git state, untracked inventory, validation receipts, the contract,
incident history, and open asks), writes one hash-bound bundle, and requests
a checkpoint. Process exit is evidence; silence or elapsed time never is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .ladder import IncidentRecord

BUNDLE_SCHEMA = "chitra.detect.rescue-bundle.v1"
BRIEF_SCHEMA = "chitra.detect.relaunch-brief.v1"
CHECKPOINT_SCHEMA = "chitra.detect.checkpoint-receipt.v1"
CHECKPOINT_SCHEMA_VERSION = 2
CHECKPOINT_KEY_BYTES = 32
CHECKPOINT_PROVENANCE_KIND = "governed-rescue-checkpoint"
CHECKPOINT_WRITER = "chitra.detect.rescue.write_checkpoint_receipt"
CHECKPOINT_SIGNATURE_SCOPE = "checkpoint receipt JSON with /signature omitted"
CHECKPOINT_CANONICALIZATION = "json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=False)"


class RecoveryCheckpointBinding(BaseModel):
    """Identity envelope required when a RESCUE checkpoint gates recovery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    session_ref: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    provider_handle: str = Field(min_length=1)
    provider_instance_id: str = Field(min_length=1)
    provider_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)
    payload_digest: str = Field(min_length=1)
    event_sequence: int = Field(ge=1)


class RescueBundle(BaseModel):
    """One bounded, hash-bound rescue snapshot for exactly one lane."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = BUNDLE_SCHEMA
    lane: str
    session_ref: str
    captured_at: str
    transcript_ref: str
    transcript_sha256: str
    process_identity: dict[str, Any]
    pane_capture: str
    git_state: dict[str, Any]
    untracked_files: tuple[str, ...]
    receipt_paths: tuple[str, ...]
    contract: str
    incident_history: tuple[str, ...]
    open_asks: tuple[str, ...]
    checkpoint_requested: bool
    recovery_binding: RecoveryCheckpointBinding | None = None
    bundle_sha256: str = ""

    def compute_digest(self) -> str:
        payload = self.model_dump()
        payload.pop("bundle_sha256", None)
        if payload.get("recovery_binding") is None:
            payload.pop("recovery_binding", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()


def _git(args: Sequence[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git {' '.join(args)} failed during RESCUE capture: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed during RESCUE capture: {detail}")
    return completed.stdout


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError(f"transcript hash capture failed for {path}: {exc}") from exc


def _observe_process_identity(pid: int) -> dict[str, Any]:
    if type(pid) is not int or pid <= 0:
        raise RuntimeError("RESCUE capture requires affected process identity")
    proc_dir = Path("/proc") / str(pid)
    try:
        stat = proc_dir.stat()
        stat_text = (proc_dir / "stat").read_text(encoding="utf-8", errors="replace")
        comm = (proc_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise RuntimeError(f"RESCUE capture target process is not observable: {pid}") from exc
    try:
        after_comm = stat_text.rsplit(")", 1)[1].strip().split()
        start_time = after_comm[19]
    except IndexError as exc:
        raise RuntimeError(f"RESCUE capture target process identity is incomplete: {pid}") from exc
    if not start_time.isdigit():
        raise RuntimeError(f"RESCUE capture target process identity is incomplete: {pid}")
    identity: dict[str, Any] = {
        "target_pid": pid,
        "target_uid": stat.st_uid,
        "target_gid": stat.st_gid,
        "target_start_time": start_time,
        "target_comm": comm,
    }
    try:
        identity["target_exe"] = str((proc_dir / "exe").resolve())
    except OSError:
        identity["target_exe"] = ""
    return identity


def _checkpoint_key_path(state_root: Path) -> Path:
    return state_root / "checkpoints" / "checkpoint.key"


def load_or_create_checkpoint_key(state_root: Path) -> bytes:
    """Load the governed checkpoint HMAC key, creating it mode 0600 once."""
    key_path = _checkpoint_key_path(state_root)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(key_path.parent, 0o700)
    try:
        fd = os.open(str(key_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return key_path.read_bytes()
    key = os.urandom(CHECKPOINT_KEY_BYTES)
    os.write(fd, key)
    os.close(fd)
    return key


def _checkpoint_signature_payload(payload: dict[str, Any]) -> bytes:
    unsigned = dict(payload)
    unsigned.pop("signature", None)
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_checkpoint_receipt(payload: dict[str, Any], *, key: bytes) -> str:
    return hmac.new(key, _checkpoint_signature_payload(payload), hashlib.sha256).hexdigest()


def verify_checkpoint_receipt_signature(payload: dict[str, Any], *, state_root: Path) -> bool:
    try:
        key = _checkpoint_key_path(state_root).read_bytes()
    except OSError:
        return False
    signature = payload.get("signature")
    if not isinstance(signature, str):
        return False
    expected = sign_checkpoint_receipt(payload, key=key)
    return hmac.compare_digest(expected, signature)


def collect_rescue_bundle(
    *,
    lane: str,
    session_ref: str,
    worktree: Path,
    transcript_path: Path | None,
    pane_capture: str = "",
    receipt_paths: Sequence[Path] = (),
    contract_text: str = "",
    incidents: Sequence[IncidentRecord] = (),
    open_asks: Sequence[str] = (),
    process_identity: dict[str, Any] | None = None,
    recovery_binding: RecoveryCheckpointBinding | None = None,
) -> RescueBundle:
    """Gather the RESCUE evidence set. Read-only over the worktree."""
    if transcript_path is None:
        raise RuntimeError("RESCUE capture requires a transcript path")
    target_pid = (process_identity or {}).get("target_pid")
    if type(target_pid) is not int:
        raise RuntimeError("RESCUE capture requires affected process identity")
    observed_target = _observe_process_identity(target_pid)
    status = _git(["status", "--porcelain=v1"], worktree)
    diff_staged = _git(["diff", "--cached"], worktree)
    diff_unstaged = _git(["diff"], worktree)
    diff_head = _git(["diff", "HEAD"], worktree)
    commits = _git(["log", "--oneline", "-10"], worktree)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree).strip()
    head = _git(["rev-parse", "HEAD"], worktree).strip()
    untracked = sorted(line[3:] for line in status.splitlines() if line.startswith("?? "))
    history = tuple(
        json.dumps(
            {
                "fingerprint": record.fingerprint,
                "detector": record.detector,
                "stage": record.stage,
                "opened_at": record.opened_at,
                "unmet_item": record.unmet_item,
            },
            sort_keys=True,
        )
        for record in incidents
    )
    bundle = RescueBundle(
        lane=lane,
        session_ref=session_ref,
        captured_at=datetime.now(UTC).isoformat(),
        transcript_ref=str(transcript_path),
        transcript_sha256=_sha256_file(transcript_path),
        process_identity={
            "capture_pid": os.getpid(),
            "capture_ppid": os.getppid(),
            "session_ref": session_ref,
            **(process_identity or {}),
            **observed_target,
        },
        pane_capture=pane_capture,
        git_state={
            "branch": branch,
            "head": head,
            "status": status,
            "diff_head": diff_head[:20000],
            "diff_staged": diff_staged[:20000],
            "diff_unstaged": diff_unstaged[:20000],
            "recent_commits": commits,
        },
        untracked_files=tuple(untracked),
        receipt_paths=tuple(str(path) for path in receipt_paths),
        contract=contract_text,
        incident_history=history,
        open_asks=tuple(open_asks),
        checkpoint_requested=True,
        recovery_binding=recovery_binding,
    )
    return bundle.model_copy(update={"bundle_sha256": bundle.compute_digest()})


def write_rescue_bundle(bundle: RescueBundle, state_root: Path) -> Path:
    """Durably write one bundle JSON at mode 0600 under ``rescue/``."""
    directory = state_root / "rescue"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"{bundle.lane}-{bundle.captured_at.replace(':', '')}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        encoded = (bundle.model_dump_json(indent=2) + "\n").encode()
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def write_checkpoint_receipt(
    *,
    bundle: RescueBundle,
    record: IncidentRecord,
    state_root: Path,
    checkpoint_ref: str,
) -> Path:
    """Write the governed checkpoint receipt required before relaunch.

    The receipt is HMAC-signed with a state-root key and bound to the consumed
    RESCUE order, rescue bundle digest, affected process identity, and
    checkpoint reference. Caller-authored JSON with recomputed hashes is not a
    checkpoint receipt.
    """
    if not checkpoint_ref or "/" in checkpoint_ref or checkpoint_ref in {".", ".."}:
        raise ValueError("unsafe checkpoint reference")
    if record.stage != "rescue" or record.consumption is None:
        raise ValueError("checkpoint receipt requires a consumed rescue incident")
    if bundle.compute_digest() != bundle.bundle_sha256:
        raise ValueError("rescue bundle digest mismatch")
    if bundle.lane != record.lane or bundle.session_ref != record.consumption.session_ref:
        raise ValueError("rescue bundle does not match consumed incident")
    target_pid = bundle.process_identity.get("target_pid")
    if type(target_pid) is not int:
        raise ValueError("rescue bundle affected process identity is missing")
    observed_target = _observe_process_identity(target_pid)
    for key, value in observed_target.items():
        if bundle.process_identity.get(key) != value:
            raise ValueError("rescue bundle affected process identity is stale or forged")
    ledger_entry = record.consumption.ledger_entry
    payload: dict[str, Any] = {
        "schema_name": CHECKPOINT_SCHEMA,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_ref": checkpoint_ref,
        "lane": record.lane,
        "session_ref": record.consumption.session_ref,
        "incident_fingerprint": record.fingerprint,
        "rescue_bundle_sha256": bundle.bundle_sha256,
        "target_process_identity": observed_target,
        "created_at": datetime.now(UTC).isoformat(),
        "writer_identity": {
            "writer_pid": os.getpid(),
            "writer_ppid": os.getppid(),
            "writer_uid": os.getuid(),
            "writer_gid": os.getgid(),
        },
        "ledger_binding": {
            "order_id": ledger_entry.order_id,
            "session_ref": ledger_entry.session_ref,
            "native_session_id": ledger_entry.native_session_id,
            "message_hash": ledger_entry.message_hash,
            "sent_at": ledger_entry.sent_at,
            "signature": ledger_entry.signature,
        },
        "recovery_binding": None if bundle.recovery_binding is None else bundle.recovery_binding.model_dump(mode="json"),
        "provenance": {
            "kind": CHECKPOINT_PROVENANCE_KIND,
            "writer": CHECKPOINT_WRITER,
            "signature_scope": CHECKPOINT_SIGNATURE_SCOPE,
            "canonicalization": CHECKPOINT_CANONICALIZATION,
        },
        "anti_replay_nonce": os.urandom(16).hex(),
        "signature": "",
    }
    payload["signature"] = sign_checkpoint_receipt(payload, key=load_or_create_checkpoint_key(state_root))
    directory = state_root / "checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"{checkpoint_ref}.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(
            f"checkpoint reference {checkpoint_ref!r} was already issued; receipt creation is single-use"
        ) from exc
    try:
        os.fchmod(fd, 0o600)
        encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def find_recovery_checkpoint_receipt(
    state_root: Path,
    binding: RecoveryCheckpointBinding,
) -> str | None:
    """Return the one signed, sealed receipt matching an exact recovery envelope."""

    try:
        from .ladder import IncidentStore, consumed_checkpoint_refs

        consumed = consumed_checkpoint_refs(state_root)
        incidents = IncidentStore(state_root, binding.lane_id).load()
    except (OSError, ValueError):
        return None
    for path in sorted((state_root / "checkpoints").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or not verify_checkpoint_receipt_signature(payload, state_root=state_root):
            continue
        checkpoint_ref = payload.get("checkpoint_ref")
        if not isinstance(checkpoint_ref, str) or checkpoint_ref not in consumed:
            continue
        if payload.get("recovery_binding") != binding.model_dump(mode="json"):
            continue
        if payload.get("lane") != binding.lane_id or payload.get("session_ref") != binding.session_ref:
            continue
        bundle_digest = payload.get("rescue_bundle_sha256")
        fingerprint = payload.get("incident_fingerprint")
        if not isinstance(bundle_digest, str) or not isinstance(fingerprint, str):
            continue
        sealed = next(
            (
                record
                for record in reversed(incidents)
                if record.checkpoint_ref == checkpoint_ref
                and record.rescue_bundle_sha256 == bundle_digest
                and record.fingerprint == fingerprint
            ),
            None,
        )
        if sealed is None:
            continue
        bundle_valid = False
        for bundle_path in (state_root / "rescue").glob("*.json"):
            try:
                bundle = RescueBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                bundle.bundle_sha256 == bundle_digest
                and bundle.compute_digest() == bundle_digest
                and bundle.recovery_binding == binding
                and bundle.lane == binding.lane_id
                and bundle.session_ref == binding.session_ref
            ):
                bundle_valid = True
                break
        if not bundle_valid:
            continue
        return checkpoint_ref
    return None


def generate_relaunch_brief(bundle: RescueBundle, *, tighter_instructions: Sequence[str] = ()) -> str:
    """Render the relaunch brief a successor session starts from."""
    lines: list[str] = [
        "# Relaunch brief",
        "",
        f"schema: {BRIEF_SCHEMA}",
        f"lane: {bundle.lane}",
        f"session_ref: {bundle.session_ref}",
        f"rescue_bundle_sha256: {bundle.bundle_sha256}",
        f"transcript: {bundle.transcript_ref}",
        "",
        "## Contract",
        "",
        bundle.contract.strip() or "(no contract recorded)",
        "",
        "## Incident history",
        "",
    ]
    if bundle.incident_history:
        lines.extend(f"- {entry}" for entry in bundle.incident_history)
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Salvage inventory",
            "",
            f"- untracked files preserved: {len(bundle.untracked_files)}",
            f"- validation receipts referenced: {len(bundle.receipt_paths)}",
            f"- open asks carried forward: {len(bundle.open_asks)}",
            "",
            "## Open asks",
            "",
        ]
    )
    if bundle.open_asks:
        lines.extend(f"- {ask}" for ask in bundle.open_asks)
    else:
        lines.append("- none")
    lines.extend(
        [
            "## Tighter instructions",
            "",
        ]
    )
    lines.extend(f"- {instruction}" for instruction in tighter_instructions)
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "BRIEF_SCHEMA",
    "BUNDLE_SCHEMA",
    "CHECKPOINT_SCHEMA",
    "RecoveryCheckpointBinding",
    "RescueBundle",
    "collect_rescue_bundle",
    "generate_relaunch_brief",
    "find_recovery_checkpoint_receipt",
    "load_or_create_checkpoint_key",
    "sign_checkpoint_receipt",
    "verify_checkpoint_receipt_signature",
    "write_checkpoint_receipt",
    "write_rescue_bundle",
]

"""Bounded RESCUE capture bundles and relaunch briefs.

RESCUE is the only authorized precursor to a kill: it captures the exact
evidence an operator or successor session needs (transcript reference, pane
capture, git state, untracked inventory, validation receipts, the contract,
incident history, and open asks), writes one hash-bound bundle, and requests
a checkpoint. Process exit is evidence; silence or elapsed time never is.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .ladder import IncidentRecord

BUNDLE_SCHEMA = "chitra.detect.rescue-bundle.v1"
BRIEF_SCHEMA = "chitra.detect.relaunch-brief.v1"


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
    bundle_sha256: str = ""

    def compute_digest(self) -> str:
        payload = self.model_dump()
        payload.pop("bundle_sha256", None)
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
) -> RescueBundle:
    """Gather the RESCUE evidence set. Read-only over the worktree."""
    if transcript_path is None:
        raise RuntimeError("RESCUE capture requires a transcript path")
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
    "RescueBundle",
    "collect_rescue_bundle",
    "generate_relaunch_brief",
    "write_rescue_bundle",
]

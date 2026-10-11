"""state_paths — lazy resolution of chitra's persistent-state locations."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

STATE_DIR_ENV_VAR = "CHITRA_STATE_DIR"
DEFAULT_STATE_DIR = Path("/var/lib/chitra")


def portable_socket_path(path: Path) -> Path:
    """Keep the encoded address, including its NUL, within Darwin's 104 bytes."""
    encoded = os.fsencode(path)
    if b"\0" in encoded:
        raise ValueError("Unix socket path contains a NUL")
    if len(encoded) + 1 <= 104:
        return path
    digest = hashlib.sha256(os.fsencode(path.absolute())).hexdigest()
    return Path("/tmp") / f"chitra-{digest}.sock"


def state_dir() -> Path:
    """Return the configured state directory, or the shipped default."""
    configured = os.environ.get(STATE_DIR_ENV_VAR, "").strip()
    return Path(configured) if configured else DEFAULT_STATE_DIR


def default_queue_dir() -> Path:
    """Return the default dispatch queue directory."""
    return state_dir() / "queue"


def default_ledger_path() -> Path:
    """Return the default delivery-ledger path."""
    return state_dir() / "ledger.jsonl"


def default_attestation_ledger_path() -> Path:
    """Return Chitra's internal decision-attestation ledger path."""
    return state_dir() / "attestations.jsonl"


def default_convlog_path() -> Path:
    """Return the default operator conversation-log path."""
    return state_dir() / "conversation.jsonl"


def default_decisions_path() -> Path:
    """Return the default append-only monitor decisions log path."""
    return state_dir() / "decisions.jsonl"


def default_ledger_key_path() -> Path:
    """Return the default HMAC signing-key path."""
    return state_dir() / "ledger.key"


def default_queue_hygiene_log_path() -> Path:
    """Return the append-only queue-hygiene audit log path."""
    return state_dir() / "queue_hygiene.jsonl"

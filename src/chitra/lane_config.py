"""The single rendered identity declaration shared by every Chitra daemon.

Models remain launch-time choices, never mutable manifest identity.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LANES_FILE_ENV_VAR = "CHITRA_LANES_FILE"
DEFAULT_LANES_FILE = Path("/etc/chitra/lanes.yaml")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class LaneCredentials:
    """Credential paths bound to one lane, without reading their contents."""

    claude_credentials: Path
    ssh_dispatch_key: Path


@dataclass(frozen=True, slots=True)
class LaneSpec:
    """One independently-owned Chitra runtime namespace."""

    identifier: str
    account: str
    uid: int
    home: Path
    workdir: Path
    config_dir: Path
    state_dir: Path
    tmux_socket: Path
    tmux_session: str
    credentials: LaneCredentials
    enabled: bool = True
    # Optional only for legacy manifests. Production dispatch populates and
    # validates these against the current Fleet facts snapshot before action.
    target_host: str | None = None
    target_account: str | None = None

    @property
    def queue_dir(self) -> Path:
        return self.state_dir / "queue"

    @property
    def events_log(self) -> Path:
        return self.state_dir / "events.log"

    @property
    def triage_state_file(self) -> Path:
        return self.state_dir / "triaged-state.json"

    @property
    def triage_log(self) -> Path:
        return self.state_dir / "triaged.log"

    @property
    def sweep_digest_path(self) -> Path:
        return self.state_dir / "sweep-digest.json"

    @property
    def sweep_snapshot_path(self) -> Path:
        return self.state_dir / "sweep-digest-state.json"

    @property
    def flags_path(self) -> Path:
        return self.state_dir / "flags.log"


def _reject_model_keys(value: Any, *, path: str = "manifest") -> None:
    """Reject the old lane-level model contract at every manifest depth."""
    if isinstance(value, dict):
        if "model" in value:
            raise ValueError(f"{path}.model is not supported; choose the model when the session starts")
        for key, child in value.items():
            _reject_model_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_model_keys(child, path=f"{path}[{index}]")


def _mapping(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    return value


def _required_text(mapping: dict[str, Any], key: str, *, path: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}.{key} must be a non-empty string")
    return value.strip()


def _absolute_path(mapping: dict[str, Any], key: str, *, path: str) -> Path:
    value = Path(_required_text(mapping, key, path=path))
    if not value.is_absolute():
        raise ValueError(f"{path}.{key} must be absolute")
    return value


def _lane(value: Any, *, index: int) -> LaneSpec:
    path = f"manifest.lanes[{index}]"
    raw = _mapping(value, path=path)
    expected = {
        "id",
        "account",
        "uid",
        "home",
        "workdir",
        "config_dir",
        "state_dir",
        "tmux_socket",
        "tmux_session",
        "credentials",
        "enabled",
        "target_host",
        "target_account",
    }
    unknown = sorted(set(raw) - expected)
    if unknown:
        raise ValueError(f"{path} has unsupported fields: {', '.join(unknown)}")
    identifier = _required_text(raw, "id", path=path)
    account = _required_text(raw, "account", path=path)
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"{path}.id must match {_IDENTIFIER_RE.pattern}")
    if not _IDENTIFIER_RE.fullmatch(account):
        raise ValueError(f"{path}.account must match {_IDENTIFIER_RE.pattern}")
    uid = raw.get("uid")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
        raise ValueError(f"{path}.uid must be a positive integer")
    tmux_session = _required_text(raw, "tmux_session", path=path)
    if any(char.isspace() for char in tmux_session) or ":" in tmux_session:
        raise ValueError(f"{path}.tmux_session must contain no whitespace or ':'")
    credentials_raw = _mapping(raw.get("credentials"), path=f"{path}.credentials")
    credential_keys = {"claude_credentials", "ssh_dispatch_key"}
    unknown_credentials = sorted(set(credentials_raw) - credential_keys)
    if unknown_credentials:
        raise ValueError(
            f"{path}.credentials has unsupported fields: {', '.join(unknown_credentials)}"
        )
    credentials = LaneCredentials(
        claude_credentials=_absolute_path(credentials_raw, "claude_credentials", path=f"{path}.credentials"),
        ssh_dispatch_key=_absolute_path(credentials_raw, "ssh_dispatch_key", path=f"{path}.credentials"),
    )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{path}.enabled must be boolean")
    target_host = raw.get("target_host")
    target_account = raw.get("target_account")
    for key, value in (("target_host", target_host), ("target_account", target_account)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{path}.{key} must be a non-empty string when supplied")
    return LaneSpec(
        identifier=identifier,
        account=account,
        uid=uid,
        home=_absolute_path(raw, "home", path=path),
        workdir=_absolute_path(raw, "workdir", path=path),
        config_dir=_absolute_path(raw, "config_dir", path=path),
        state_dir=_absolute_path(raw, "state_dir", path=path),
        tmux_socket=_absolute_path(raw, "tmux_socket", path=path),
        tmux_session=tmux_session,
        credentials=credentials,
        enabled=enabled,
        target_host=target_host.strip() if isinstance(target_host, str) else None,
        target_account=target_account.strip() if isinstance(target_account, str) else None,
    )


def load_lanes(path: Path | None = None) -> tuple[LaneSpec, ...]:
    """Load and validate the rendered lane declaration."""
    manifest_path = path or Path(os.environ.get(LANES_FILE_ENV_VAR, DEFAULT_LANES_FILE))
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"lane manifest is missing: {manifest_path}") from exc
    except OSError as exc:
        raise ValueError(f"lane manifest cannot be read: {manifest_path}: {exc}") from exc
    _reject_model_keys(payload)
    manifest = _mapping(payload, path="manifest")
    if set(manifest) != {"lanes"}:
        unknown = sorted(set(manifest) - {"lanes"})
        missing = "lanes" if "lanes" not in manifest else ""
        detail = ", ".join(unknown) if unknown else missing
        raise ValueError(f"manifest must contain only lanes; invalid fields: {detail}")
    raw_lanes = manifest["lanes"]
    if not isinstance(raw_lanes, list) or not raw_lanes:
        raise ValueError("manifest.lanes must be a non-empty list")
    lanes = tuple(_lane(value, index=index) for index, value in enumerate(raw_lanes))
    identifiers = [lane.identifier for lane in lanes]
    accounts = [lane.account for lane in lanes]
    uids = [lane.uid for lane in lanes]
    for label, values in (("id", identifiers), ("account", accounts), ("uid", uids)):
        if len(values) != len(set(values)):
            raise ValueError(f"manifest lane {label}s must be unique")
    paths = [lane.state_dir for lane in lanes]
    if len(paths) != len(set(paths)):
        raise ValueError("manifest lane state_dir values must be unique")
    return lanes


def enabled_lanes(path: Path | None = None) -> tuple[LaneSpec, ...]:
    """Return only lanes whose declaration permits the anchor to run."""
    return tuple(lane for lane in load_lanes(path) if lane.enabled)

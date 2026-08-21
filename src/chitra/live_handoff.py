"""Two-phase replacement of Chitra's live local-socket server."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

from chitra.agent_runtime import AgentStatusBroker
from chitra.api_protocol import API_PROTOCOL_VERSION, ProtocolError
from chitra.socket_api import HANDOFF_SCHEMA, ApiRuntime, ControlServer, SocketClient


class LiveHandoffError(RuntimeError):
    """A replacement server could not prove and commit live ownership."""


def _object(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LiveHandoffError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _validate_manifest(
    value: object,
    *,
    runtime: ApiRuntime,
    broker: AgentStatusBroker,
) -> dict[str, Any]:
    manifest = _object(value, name="handoff manifest")
    if manifest.get("schema") != HANDOFF_SCHEMA:
        raise LiveHandoffError("handoff manifest schema is unsupported")
    if manifest.get("protocol_version") != API_PROTOCOL_VERSION:
        raise LiveHandoffError("handoff manifest protocol is incompatible")
    if manifest.get("target_pid") != runtime.process_id:
        raise LiveHandoffError("handoff manifest target pid does not identify this replacement")
    if Path(str(manifest.get("state_dir", ""))).resolve() != broker.state_dir.resolve():
        raise LiveHandoffError("handoff manifest state_dir does not match the replacement")
    status_snapshot = _object(manifest.get("status_snapshot"), name="handoff status_snapshot")
    canonical = json.dumps(status_snapshot, sort_keys=True, separators=(",", ":"))
    expected_digest = hashlib.sha256(canonical.encode()).hexdigest()
    if manifest.get("status_snapshot_sha256") != expected_digest:
        raise LiveHandoffError("handoff status snapshot digest does not match")
    verified_ids = manifest.get("verified_pane_ids")
    if not isinstance(verified_ids, list) or any(not isinstance(value, str) for value in verified_ids):
        raise LiveHandoffError("handoff verified_pane_ids is invalid")
    validated_snapshot = broker.validate_handoff_snapshot(status_snapshot)
    imported_ids = [status.pane_id for status in validated_snapshot.panes]
    if sorted(verified_ids) != sorted(imported_ids):
        raise LiveHandoffError("handoff verified panes do not match imported state")
    for status in validated_snapshot.panes:
        try:
            verified = runtime.pane_verifier(status)
        except OSError:
            verified = False
        if not verified:
            raise LiveHandoffError(f"replacement cannot verify live pane {status.pane_id}; state is UNKNOWN")
    broker.import_validated_handoff_snapshot(validated_snapshot)
    return manifest


def perform_live_handoff(
    *,
    canonical_socket: Path,
    replacement_server: ControlServer,
    replacement_runtime: ApiRuntime,
) -> dict[str, object]:
    """Import, verify, and atomically commit a replacement server.

    The source and replacement both prove every recorded tmux pane. The old
    socket pathname is kept as a rollback point until the source commits its
    ownership lease. Pane processes are never signalled or restarted.
    """
    if replacement_server.socket_path == canonical_socket:
        raise LiveHandoffError("replacement server must begin on a temporary socket path")
    temporary_socket = replacement_server.socket_path
    backup_socket = canonical_socket.with_name(f".{canonical_socket.name}.handoff-old-{replacement_runtime.process_id}")
    if backup_socket.exists():
        raise LiveHandoffError(f"handoff backup socket already exists: {backup_socket}")
    replacement_server.start()
    token: str | None = None
    renamed = False
    committed = False
    try:
        with SocketClient(canonical_socket) as source:
            prepared = source.request(
                "handoff:prepare",
                "server.handoff.prepare",
                {
                    "target_pid": replacement_runtime.process_id,
                    "state_dir": str(replacement_runtime.state_dir),
                    "expected_protocol": API_PROTOCOL_VERSION,
                },
            )
            token_value = prepared.get("token")
            if not isinstance(token_value, str) or not token_value:
                raise LiveHandoffError("source server returned no handoff token")
            token = token_value
            manifest = _validate_manifest(
                prepared.get("manifest"), runtime=replacement_runtime, broker=replacement_runtime.broker
            )
            receipt = {
                "schema": HANDOFF_SCHEMA,
                "generation": manifest["generation"],
                "source_pid": manifest["source_pid"],
                "owner_pid": replacement_runtime.process_id,
                "verified_pane_ids": manifest["verified_pane_ids"],
                "status": "transferred",
            }
            os.replace(canonical_socket, backup_socket)
            os.replace(temporary_socket, canonical_socket)
            renamed = True
            replacement_server.socket_path = canonical_socket
            source.request(
                "handoff:commit",
                "server.handoff.commit",
                {"token": token, "target_pid": replacement_runtime.process_id},
            )
            committed = True
        with contextlib.suppress(FileNotFoundError):
            backup_socket.unlink()
        return receipt
    except (ConnectionError, LiveHandoffError, OSError, ProtocolError, ValueError) as exc:
        if token is not None and not committed:
            try:
                with SocketClient(backup_socket if renamed else canonical_socket) as source:
                    source.request("handoff:abort", "server.handoff.abort", {"token": token})
            except (ConnectionError, OSError, ProtocolError):
                pass
        if renamed and not committed:
            try:
                os.replace(canonical_socket, temporary_socket)
                os.replace(backup_socket, canonical_socket)
                replacement_server.socket_path = temporary_socket
                renamed = False
            except OSError as rollback_exc:
                raise LiveHandoffError(f"handoff failed and socket rollback is UNKNOWN: {rollback_exc}") from exc
        raise LiveHandoffError(str(exc)) from exc
    finally:
        if not committed:
            replacement_server.shutdown()
        if backup_socket.exists() and not committed:
            with contextlib.suppress(OSError):
                backup_socket.unlink()

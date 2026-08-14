"""Local newline-delimited JSON server for semantic agent coordination."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import socket
import socketserver
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

from chitra._fsio import write_json_atomic
from chitra.agent_runtime import AgentStatusBroker, PaneStatus, StatusRuntimeError
from chitra.agent_status import AgentState
from chitra.api_protocol import API_PROTOCOL_VERSION, ProtocolError, api_schema, parse_subscriptions
from chitra.api_protocol import MAX_WAIT_MS as MAX_WAIT_MS

DEFAULT_SOCKET_PATH = Path("/run/chitra/chitra.sock")
SOCKET_PATH_ENV_VAR = "CHITRA_SOCKET_PATH"
HANDOFF_SCHEMA = "chitra.live-handoff.v1"
OWNERSHIP_SCHEMA = "chitra.server-ownership.v1"
MAX_REQUEST_BYTES = 1024 * 1024
HANDOFF_TTL_SECONDS = 30.0
CONNECTION_LIVENESS_INTERVAL_SECONDS = 0.25
SOCKET_PROBE_TIMEOUT_SECONDS = 0.25
PaneVerifier = Callable[[PaneStatus], bool]
ConnectionProbe = Callable[[], bool]


class _ClientDisconnected(Exception):
    """Stop a blocking handler after its peer closes the connection."""


@dataclass(frozen=True, slots=True)
class PendingHandoff:
    token: str
    target_pid: int
    generation: int
    expires_at: float
    manifest: dict[str, object]


def default_socket_path() -> Path:
    configured = os.environ.get(SOCKET_PATH_ENV_VAR, "").strip()
    return Path(configured) if configured else DEFAULT_SOCKET_PATH


def verify_tmux_pane(status: PaneStatus) -> bool:
    """Prove the recorded target still resolves to the same tmux pane id."""
    command = ["tmux"]
    if status.tmux_socket is not None:
        command.extend(["-S", status.tmux_socket])
    command.extend(["display-message", "-p", "-t", status.target, "#{pane_id}"])
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == status.pane_id


class ApiRuntime:
    """Method dispatcher and two-phase live-handoff state."""

    def __init__(
        self,
        broker: AgentStatusBroker,
        *,
        pane_verifier: PaneVerifier = verify_tmux_pane,
        process_id: int | None = None,
    ) -> None:
        self.broker = broker
        self.state_dir = broker.state_dir.resolve()
        self.pane_verifier = pane_verifier
        self.process_id = os.getpid() if process_id is None else process_id
        self._lock = threading.RLock()
        self._pending: PendingHandoff | None = None
        self._handoff_timer: threading.Timer | None = None
        self._shutdown_callback: Callable[[], None] | None = None

    @property
    def ownership_path(self) -> Path:
        return self.state_dir / "server-ownership.json"

    def set_shutdown_callback(self, callback: Callable[[], None]) -> None:
        self._shutdown_callback = callback

    def dispatch(
        self,
        method: str,
        params: object,
        *,
        connection_alive: ConnectionProbe | None = None,
    ) -> dict[str, object]:
        self._expire_handoff()
        if method == "ping":
            self._empty_params(params)
            return {"type": "pong", "protocol_version": API_PROTOCOL_VERSION, "pid": self.process_id}
        if method == "api.schema":
            self._empty_params(params)
            return {"type": "api_schema", "schema": api_schema()}
        if method == "pane.report_agent":
            raw = self._params(params, allowed={"pane_id", "session_ref", "source", "agent", "state"})
            event = self.broker.report_agent(
                pane_id=self._required_text(raw, "pane_id"),
                session_ref=self._optional_text(raw, "session_ref"),
                source=self._required_text(raw, "source"),
                agent=self._required_text(raw, "agent"),
                state=self._required_text(raw, "state"),
            )
            return {"type": "agent_report", "changed": event is not None, "event": None if event is None else event.to_dict()}
        if method == "pane.clear_agent_authority":
            raw = self._params(params, allowed={"pane_id", "source"})
            changed = self.broker.clear_agent_authority(
                self._required_text(raw, "pane_id"), source=self._optional_text(raw, "source")
            )
            return {"type": "agent_authority_clear", "changed": changed}
        if method == "agent.explain":
            raw = self._params(params, allowed={"pane_id"})
            pane_id = self._required_text(raw, "pane_id")
            explain = self.broker.explain(pane_id)
            if explain is None:
                raise ProtocolError("not_found", "pane status not found")
            return {"type": "agent_explain", "pane_id": pane_id, "explain": explain.to_dict()}
        if method == "agent.wait":
            return self._agent_wait(params, connection_alive=connection_alive or (lambda: True))
        if method == "server.snapshot":
            self._empty_params(params)
            return {"type": "session_snapshot", "snapshot": self.broker.handoff_snapshot()}
        if method == "server.handoff.prepare":
            return self._prepare_handoff(params)
        if method == "server.handoff.commit":
            return self._commit_handoff(params)
        if method == "server.handoff.abort":
            return self._abort_handoff(params)
        if method == "events.subscribe":
            raise ProtocolError("invalid_request", "events.subscribe requires a streaming connection")
        raise ProtocolError("method_not_found", f"unknown method: {method}")

    def _agent_wait(self, params: object, *, connection_alive: ConnectionProbe) -> dict[str, object]:
        raw = self._params(params, allowed={"pane_id", "until", "timeout_ms"})
        pane_id = self._required_text(raw, "pane_id")
        raw_until = raw.get("until")
        values = [raw_until] if isinstance(raw_until, str) else raw_until
        if not isinstance(values, list) or not values or any(
            value not in ("idle", "working", "blocked", "done", "unknown") for value in values
        ):
            raise ProtocolError("invalid_params", "until must be a state string or non-empty state array")
        timeout_ms = raw.get("timeout_ms", MAX_WAIT_MS)
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0 or timeout_ms > MAX_WAIT_MS:
            raise ProtocolError("invalid_params", f"timeout_ms must be between 0 and {MAX_WAIT_MS}")
        deadline = time.monotonic() + timeout_ms / 1000
        until = frozenset(cast(AgentState, value) for value in values)
        while True:
            if not connection_alive():
                raise _ClientDisconnected
            remaining = max(0.0, deadline - time.monotonic())
            status = self.broker.wait_for_status(
                pane_id,
                until,
                min(CONNECTION_LIVENESS_INTERVAL_SECONDS, remaining),
            )
            if status is not None:
                return {"type": "agent_wait", "pane": status.to_dict(), "until": sorted(until)}
            if time.monotonic() >= deadline:
                raise ProtocolError("timeout", "agent wait timed out")

    def _prepare_handoff(self, params: object) -> dict[str, object]:
        raw = self._params(params, allowed={"target_pid", "state_dir", "expected_protocol"})
        target_pid = raw.get("target_pid")
        if isinstance(target_pid, bool) or not isinstance(target_pid, int) or target_pid < 1:
            raise ProtocolError("invalid_params", "target_pid must be a positive integer")
        requested_state_dir = Path(self._required_text(raw, "state_dir")).resolve()
        if requested_state_dir != self.state_dir:
            raise ProtocolError("handoff_refused", "replacement state_dir does not match the source server")
        expected_protocol = raw.get("expected_protocol")
        if expected_protocol != API_PROTOCOL_VERSION:
            raise ProtocolError("handoff_refused", "replacement protocol version is incompatible")
        with self._lock:
            if self._pending is not None:
                raise ProtocolError("handoff_in_progress", "another live handoff is already prepared")
            self.broker.freeze()
            try:
                statuses = self.broker.statuses()
                unverifiable: list[str] = []
                for status in statuses:
                    try:
                        verified = self.pane_verifier(status)
                    except OSError:
                        verified = False
                    if not verified:
                        unverifiable.append(status.pane_id)
                if unverifiable:
                    raise ProtocolError(
                        "handoff_unknown",
                        "live pane ownership is UNKNOWN for: " + ", ".join(sorted(unverifiable)),
                    )
                generation = self._ownership_generation() + 1
                token = secrets.token_urlsafe(32)
                status_snapshot = self.broker.handoff_snapshot()
                snapshot_json = json.dumps(status_snapshot, sort_keys=True, separators=(",", ":"))
                manifest = {
                    "schema": HANDOFF_SCHEMA,
                    "protocol_version": API_PROTOCOL_VERSION,
                    "source_pid": self.process_id,
                    "target_pid": target_pid,
                    "state_dir": str(self.state_dir),
                    "generation": generation,
                    "prepared_at": datetime.now(UTC).isoformat(),
                    "status_snapshot_sha256": hashlib.sha256(snapshot_json.encode()).hexdigest(),
                    "status_snapshot": status_snapshot,
                    "verified_pane_ids": [status.pane_id for status in statuses],
                }
                self._pending = PendingHandoff(
                    token=token,
                    target_pid=target_pid,
                    generation=generation,
                    expires_at=time.monotonic() + HANDOFF_TTL_SECONDS,
                    manifest=manifest,
                )
                timer = threading.Timer(HANDOFF_TTL_SECONDS, self._expire_handoff)
                timer.daemon = True
                self._handoff_timer = timer
                timer.start()
                return {"type": "handoff_manifest", "token": token, "manifest": manifest}
            except Exception as exc:
                self._pending = None
                self._cancel_handoff_timer()
                self.broker.thaw()
                if isinstance(exc, ProtocolError):
                    raise
                raise ProtocolError("handoff_unknown", f"handoff preparation state is UNKNOWN: {exc}") from exc

    def _commit_handoff(self, params: object) -> dict[str, object]:
        raw = self._params(params, allowed={"token", "target_pid"})
        token = self._required_text(raw, "token")
        target_pid = raw.get("target_pid")
        with self._lock:
            pending = self._pending
            if pending is None:
                raise ProtocolError("handoff_refused", "no prepared handoff exists")
            if token != pending.token or target_pid != pending.target_pid:
                raise ProtocolError("handoff_refused", "handoff token or target pid does not match")
            ownership = {
                "schema": OWNERSHIP_SCHEMA,
                "generation": pending.generation,
                "owner_pid": pending.target_pid,
                "source_pid": self.process_id,
                "state_dir": str(self.state_dir),
                "status": "transferred",
                "committed_at": datetime.now(UTC).isoformat(),
            }
            write_json_atomic(self.ownership_path, ownership, fsync=True)
            self._pending = None
            self._cancel_handoff_timer()
            callback = self._shutdown_callback
        if callback is not None:
            threading.Thread(target=callback, name="chitra-handoff-shutdown", daemon=True).start()
        return {
            "type": "handoff_committed",
            "generation": ownership["generation"],
            "owner_pid": ownership["owner_pid"],
        }

    def _abort_handoff(self, params: object) -> dict[str, object]:
        raw = self._params(params, allowed={"token"})
        token = self._required_text(raw, "token")
        with self._lock:
            pending = self._pending
            if pending is None:
                return {"type": "handoff_aborted", "changed": False}
            if token != pending.token:
                raise ProtocolError("handoff_refused", "handoff token does not match")
            self._pending = None
            self._cancel_handoff_timer()
            self.broker.thaw()
            return {"type": "handoff_aborted", "changed": True}

    def _expire_handoff(self) -> None:
        with self._lock:
            if self._pending is not None and time.monotonic() >= self._pending.expires_at:
                self._pending = None
                self._cancel_handoff_timer()
                self.broker.thaw()

    def _cancel_handoff_timer(self) -> None:
        timer = self._handoff_timer
        self._handoff_timer = None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()

    def _ownership_generation(self) -> int:
        try:
            payload: object = json.loads(self.ownership_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            raise ProtocolError("handoff_unknown", f"server ownership state is UNKNOWN: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema") != OWNERSHIP_SCHEMA:
            raise ProtocolError("handoff_unknown", "server ownership state has an unsupported schema")
        generation = payload.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ProtocolError("handoff_unknown", "server ownership generation is invalid")
        return generation

    @staticmethod
    def _empty_params(params: object) -> None:
        if params != {}:
            raise ProtocolError("invalid_params", "params must be an empty object")

    @staticmethod
    def _params(params: object, *, allowed: set[str]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ProtocolError("invalid_params", "params must be an object")
        raw = cast(dict[str, Any], params)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ProtocolError("invalid_params", "unsupported params: " + ", ".join(unknown))
        return raw

    @staticmethod
    def _required_text(raw: dict[str, Any], key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ProtocolError("invalid_params", f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _optional_text(raw: dict[str, Any], key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ProtocolError("invalid_params", f"{key} must be a non-empty string or null")
        return value


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, socket_path: Path, runtime: ApiRuntime) -> None:
        self.runtime = runtime
        self._handler_lock = threading.Lock()
        self._active_handlers = 0
        super().__init__(str(socket_path), _RequestHandler)

    @property
    def active_handler_count(self) -> int:
        with self._handler_lock:
            return self._active_handlers

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: object,
    ) -> None:
        with self._handler_lock:
            self._active_handlers += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._handler_lock:
                self._active_handlers -= 1


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = cast(_ThreadingUnixServer, self.server)
        seen_request_ids: set[str] = set()
        while True:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST_BYTES:
                self._write({"id": None, "error": {"code": "invalid_request", "message": "request line is too large"}})
                return
            if not line.endswith(b"\n"):
                self._write({"id": None, "error": {"code": "invalid_request", "message": "request line is incomplete"}})
                return
            request_id: str | None = None
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict) or set(payload) != {"id", "method", "params"}:
                    raise ProtocolError("invalid_request", "request must contain only id, method, and params")
                request_id_value = payload.get("id")
                if not isinstance(request_id_value, str) or not request_id_value:
                    raise ProtocolError("invalid_request", "request id must be a non-empty string")
                request_id = request_id_value
                if request_id in seen_request_ids:
                    raise ProtocolError("duplicate_id", "request id must be unique within a connection")
                seen_request_ids.add(request_id)
                method = payload.get("method")
                if not isinstance(method, str) or not method:
                    raise ProtocolError("invalid_request", "request method must be a non-empty string")
                if method == "events.subscribe":
                    self._subscribe(request_id, payload.get("params"), server.runtime.broker)
                    return
                result = server.runtime.dispatch(
                    method,
                    payload.get("params"),
                    connection_alive=self._connection_alive,
                )
                if not self._write({"id": request_id, "result": result}):
                    return
            except _ClientDisconnected:
                return
            except ProtocolError as exc:
                if not self._write({"id": request_id, "error": {"code": exc.code, "message": exc.message}}):
                    return
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                if not self._write({"id": request_id, "error": {"code": "invalid_json", "message": str(exc)}}):
                    return
            except (StatusRuntimeError, OSError, ValueError) as exc:
                if not self._write({"id": request_id, "error": {"code": "state_unavailable", "message": str(exc)}}):
                    return

    def _subscribe(self, request_id: str, params: object, broker: AgentStatusBroker) -> None:
        subscriptions = parse_subscriptions(params)
        last_seq = max((event.seq for event in broker.events_after(0)), default=0)
        if not self._write(
            {
                "id": request_id,
                "result": {"type": "event_subscription", "active": True, "subscription_count": len(subscriptions)},
            }
        ):
            return
        while self._connection_alive():
            events = broker.wait_for_event(last_seq, timeout_seconds=CONNECTION_LIVENESS_INTERVAL_SECONDS)
            for event in events:
                last_seq = max(last_seq, event.seq)
                if any(subscription.matches(event) for subscription in subscriptions) and not self._write(
                    {"id": request_id, "event": event.to_dict()}
                ):
                    return

    def _connection_alive(self) -> bool:
        try:
            pending = cast(bytes, self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT))
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            return False
        return pending != b""

    def _write(self, payload: dict[str, object]) -> bool:
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        try:
            self.wfile.write(line)
            self.wfile.flush()
        except OSError:
            return False
        return True


class ControlServer:
    """Own one restricted Unix socket around an :class:`ApiRuntime`."""

    def __init__(self, socket_path: Path, runtime: ApiRuntime) -> None:
        self.socket_path = socket_path
        self.runtime = runtime
        self._server: _ThreadingUnixServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_inode: int | None = None

    def bind(self) -> None:
        if self._server is not None:
            raise RuntimeError("control server is already bound")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._recover_stale_socket()
        self._server = _ThreadingUnixServer(self.socket_path, self.runtime)
        os.chmod(self.socket_path, 0o600)
        self._bound_inode = self.socket_path.stat().st_ino
        self.runtime.set_shutdown_callback(self.shutdown)

    @property
    def active_handler_count(self) -> int:
        server = self._server
        return 0 if server is None else server.active_handler_count

    def _recover_stale_socket(self) -> None:
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode):
            raise RuntimeError(f"refusing to replace non-socket path: {self.socket_path}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(SOCKET_PROBE_TIMEOUT_SECONDS)
        try:
            probe.connect(str(self.socket_path))
        except ConnectionRefusedError:
            pass
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno == errno.ECONNREFUSED:
                pass
            else:
                raise OSError(
                    errno.EADDRINUSE,
                    f"cannot prove existing control socket is stale: {self.socket_path}: {exc}",
                ) from exc
        else:
            raise OSError(errno.EADDRINUSE, f"live control server already owns socket: {self.socket_path}")
        finally:
            probe.close()
        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino, current.st_mode) != (existing.st_dev, existing.st_ino, existing.st_mode):
            raise RuntimeError(f"refusing to replace changed socket path: {self.socket_path}")
        self.socket_path.unlink()

    def start(self) -> None:
        if self._server is None:
            self.bind()
        assert self._server is not None
        if self._thread is not None:
            raise RuntimeError("control server is already running")
        self._thread = threading.Thread(target=self._server.serve_forever, name="chitra-socket-api", daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        if self._server is None:
            self.bind()
        assert self._server is not None
        self._server.serve_forever()

    def shutdown(self) -> None:
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        self._server = None
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        try:
            if self._bound_inode is not None and self.socket_path.stat().st_ino == self._bound_inode:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._bound_inode = None


class SocketClient:
    """Small correlated NDJSON client; one instance may span handoff steps."""

    def __init__(self, socket_path: Path, *, timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._reader: BinaryIO | None = None

    def __enter__(self) -> SocketClient:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.socket_path))
        self._socket = sock
        self._reader = sock.makefile("rb")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._reader is not None:
            self._reader.close()
        if self._socket is not None:
            self._socket.close()
        self._reader = None
        self._socket = None

    def request(self, request_id: str, method: str, params: dict[str, object]) -> dict[str, object]:
        if self._socket is None or self._reader is None:
            raise RuntimeError("SocketClient must be used as a context manager")
        payload = {"id": request_id, "method": method, "params": params}
        self._socket.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        line = self._reader.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            raise ConnectionError("Chitra socket closed without a response")
        response = json.loads(line)
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise ConnectionError("Chitra response id did not match the request")
        if isinstance(response.get("error"), dict):
            error = cast(dict[str, object], response["error"])
            raise ProtocolError(str(error.get("code", "server_error")), str(error.get("message", "unknown server error")))
        result = response.get("result")
        if not isinstance(result, dict):
            raise ConnectionError("Chitra response did not contain a result object")
        return cast(dict[str, object], result)


def request(
    socket_path: Path,
    request_id: str,
    method: str,
    params: dict[str, object],
    *,
    timeout: float = 10.0,
) -> dict[str, object]:
    with SocketClient(socket_path, timeout=timeout) as client:
        return client.request(request_id, method, params)

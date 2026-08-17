"""heartbeat -- per-daemon liveness files plus stdlib-only sd_notify.

A hung daemon loop is invisible to everything downstream of it: no new events,
no new digest, no log growth, and (before this module existed) nothing that
even claimed to still be alive. Every long-running Chitra daemon loop
(watchd, sweepd, triaged) now writes one small JSON file per cycle to
``<state_dir>/heartbeats/<name>.json`` so an external watcher can tell "quiet
because there is nothing to do" from "quiet because the loop died" without
guessing from log silence -- the same ground-truth instinct behind watchd's
wedge detection, applied to the daemons themselves.

``notify_ready``/``notify_watchdog`` speak the stdlib-only ``NOTIFY_SOCKET``
datagram protocol (see ``sd_notify(3)``) so a systemd unit can run
``Type=notify`` with ``WatchdogSec=`` and get killed and restarted -- rather
than sitting wedged forever -- the moment a loop stops calling in. Both are
no-ops outside a notify unit (``NOTIFY_SOCKET`` unset), so calling them from a
plain script or under ``--once`` is always safe.
"""

from __future__ import annotations

import os
import socket
from datetime import UTC, datetime
from pathlib import Path

from chitra._fsio import write_json_atomic

HEARTBEATS_DIRNAME = "heartbeats"
NOTIFY_SOCKET_ENV_VAR = "NOTIFY_SOCKET"


def heartbeat_path(state_dir: Path, name: str) -> Path:
    """Return the per-daemon heartbeat file path under ``state_dir``."""
    return state_dir / HEARTBEATS_DIRNAME / f"{name}.json"


def write_heartbeat(
    state_dir: Path,
    *,
    daemon: str,
    cycle: int,
    cadence_seconds: float,
    now: datetime | None = None,
) -> Path:
    """Atomically record one liveness beat for ``daemon``. Returns the path written."""
    timestamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    path = heartbeat_path(state_dir, daemon)
    write_json_atomic(
        path,
        {
            "daemon": daemon,
            "ts": timestamp,
            "cycle": cycle,
            "cadence_seconds": cadence_seconds,
        },
        temporary_path=path.with_name(f".{path.name}.tmp"),
        cleanup_on_error=False,
    )
    return path


def _notify_socket() -> socket.socket | None:
    """Open the systemd NOTIFY_SOCKET datagram socket, or None outside a notify unit."""
    address = os.environ.get(NOTIFY_SOCKET_ENV_VAR, "").strip()
    if not address:
        return None
    # An abstract-namespace socket is addressed with a leading '@' in the env
    # var and a leading NUL byte on the wire (see unix(7)).
    if address.startswith("@"):
        address = "\0" + address[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(address)
    except OSError:
        sock.close()
        return None
    return sock


def sd_notify(*fields: str) -> bool:
    """Send one newline-joined datagram to NOTIFY_SOCKET. No-op (returns False)
    when unset or unreachable -- never raises, since a daemon's own liveness
    signal must not be able to crash the daemon."""
    sock = _notify_socket()
    if sock is None:
        return False
    try:
        sock.sendall("\n".join(fields).encode("utf-8"))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def notify_ready() -> bool:
    """Tell systemd this ``Type=notify`` unit has finished starting up."""
    return sd_notify("READY=1")


def notify_watchdog() -> bool:
    """Pet systemd's watchdog timer for one more ``WatchdogSec=`` window."""
    return sd_notify("WATCHDOG=1")

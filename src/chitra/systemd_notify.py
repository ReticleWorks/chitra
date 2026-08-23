"""Minimal systemd readiness and watchdog notification support."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping

NOTIFY_SOCKET_ENV_VAR = "NOTIFY_SOCKET"
WATCHDOG_PID_ENV_VAR = "WATCHDOG_PID"
WATCHDOG_USEC_ENV_VAR = "WATCHDOG_USEC"


def watchdog_usec(*, env: Mapping[str, str] | None = None, pid: int | None = None) -> int | None:
    """Return systemd's watchdog interval when it applies to this process."""
    values = os.environ if env is None else env
    raw_usec = values.get(WATCHDOG_USEC_ENV_VAR, "").strip()
    if not raw_usec:
        return None
    interval = int(raw_usec)
    if interval <= 0:
        raise ValueError(f"{WATCHDOG_USEC_ENV_VAR} must be a positive integer")

    raw_pid = values.get(WATCHDOG_PID_ENV_VAR, "").strip()
    if raw_pid and int(raw_pid) != (os.getpid() if pid is None else pid):
        return None
    return interval


def _notify_address(raw_address: str) -> str:
    """Translate systemd's abstract-socket spelling to the Unix form."""
    return "\0" + raw_address[1:] if raw_address.startswith("@") else raw_address


def sd_notify(*fields: str, env: Mapping[str, str] | None = None) -> bool:
    """Send one sd_notify datagram, or return False outside a notify unit."""
    values = os.environ if env is None else env
    raw_address = values.get(NOTIFY_SOCKET_ENV_VAR, "").strip()
    if not raw_address:
        return False
    if not fields:
        raise ValueError("sd_notify requires at least one field")

    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
        notifier.connect(_notify_address(raw_address))
        notifier.sendall("\n".join(fields).encode("utf-8"))
    return True


def notify_ready() -> bool:
    """Tell systemd that a Type=notify daemon has finished starting."""
    return sd_notify("READY=1")


def notify_watchdog() -> bool:
    """Pet the configured systemd watchdog after one successful loop."""
    if watchdog_usec() is None:
        return False
    return sd_notify("WATCHDOG=1")

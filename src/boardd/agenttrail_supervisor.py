"""Spawn and supervise the vendored agenttrail Node process.

boardd owns this process end to end: started once at app startup from the
vendored bundle, restarted on exit with backoff, stopped on boardd's own
shutdown, stdout/stderr logged. Best-effort by design, same as
agenttrail_bridge.py's hook posts — a missing `node` binary or a crashing
agenttrail must never take boardd down with it; the Activity tab just stops
working and the rest of the board is unaffected.
"""

import logging
import subprocess
import threading
from urllib.parse import urlparse

from . import config

logger = logging.getLogger("boardd.agenttrail_supervisor")

VENDORED_BIN = config.PKG_DIR / "vendor" / "agenttrail" / "bin" / "agenttrail.mjs"

# ponytail: fixed backoff schedule, capped and repeating — a real backoff
# policy (jitter, reset-after-stable-uptime) can follow if restarts churn.
RESTART_BACKOFF_SECONDS = (1, 2, 5, 10, 30)


def agenttrail_port() -> int:
    """The port both the supervisor and the /activity/* proxy target —
    always read fresh from config so tests can override it per-call."""
    return urlparse(config.AGENTTRAIL_PUBLIC_URL).port or 5330


class AgenttrailSupervisor:
    def __init__(self, node_bin: str | None = None, backoff: tuple[float, ...] = RESTART_BACKOFF_SECONDS) -> None:
        self._node_bin = node_bin or config.AGENTTRAIL_NODE_BIN
        self._backoff = backoff
        self._proc: subprocess.Popen[str] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not VENDORED_BIN.exists():
            logger.warning("agenttrail vendored bundle not found at %s; Activity tab will not work", VENDORED_BIN)
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="agenttrail-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _argv(self) -> list[str]:
        return [
            self._node_bin,
            str(VENDORED_BIN),
            str(config.AGENTTRAIL_CWD),
            "--port",
            str(agenttrail_port()),
            "--no-open",
        ]

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen(
                    self._argv(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as e:
                logger.warning("could not start agenttrail (%s); Activity tab will not work: %s", self._node_bin, e)
                return
            self._proc = proc
            self._drain_output(proc)
            returncode = proc.wait()
            if self._stop.is_set():
                return
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            logger.warning("agenttrail exited (code %s); restarting in %ss", returncode, delay)
            self._stop.wait(delay)

    @staticmethod
    def _drain_output(proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            logger.info("agenttrail: %s", line.rstrip())

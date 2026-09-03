"""boardd's two write endpoints. No new store: both shell out to the
already-governed `chitra-goals` CLI (chitra.goals_cli), the same tool an
operator would run by hand. boardd never touches goals.json itself.

- ack:    `chitra-goals resolve-ask --all` — clears every open ask on the
          lane with the CLI's own default basis, no answer text needed.
- answer: `chitra-goals resolve-ask --all --basis <text>` — same, but the
          operator's answer becomes the recorded basis for retiring them.

Both use resolve_ask's own no-op behavior when a lane has no open asks: it
still succeeds, so acknowledging a status-only review item (blocked,
completion-disputed, ... with no literal ask) is a harmless no-op rather
than an error. It does not change the lane's status — boardd stays a pure
reader of everything except operator asks.
"""

import subprocess
import sys
from pathlib import Path

from chitra.goals import load_goals

CHITRA_GOALS_TIMEOUT = 10.0


class LaneActionError(Exception):
    def __init__(self, message: str, *, not_found: bool = False):
        super().__init__(message)
        self.not_found = not_found


def _find_session_ref(state_dir: Path, lane_id: str) -> str:
    """boardd's write endpoints are addressed by lane_id; chitra-goals wants
    the exact session_ref. Look it up from the same file boardd just read."""
    for record in load_goals(state_dir, allow_newer=True):
        if record.lane_id == lane_id or record.session_ref == lane_id:
            return record.session_ref
    raise LaneActionError(f"no lane found for {lane_id!r}", not_found=True)


def _resolve_ask(state_dir: Path, lane_id: str, *, basis: str | None) -> None:
    session_ref = _find_session_ref(state_dir, lane_id)
    # -m rather than the console-script name: works whether or not the
    # chitra-goals entry point is on PATH in boardd's own process environment.
    argv = [
        sys.executable,
        "-m",
        "chitra.goals_cli",
        "resolve-ask",
        "--root",
        str(state_dir),
        "--session-ref",
        session_ref,
        "--all",
        "--retired-by",
        "operator",
    ]
    if basis:
        argv += ["--basis", basis]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=CHITRA_GOALS_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise LaneActionError(f"chitra-goals timed out: {e}") from e
    except OSError as e:
        # Covers FileNotFoundError and, e.g., E2BIG if an oversized argv ever
        # slips past the request-body cap in app.py.
        raise LaneActionError(f"could not run chitra-goals: {e}") from e
    if proc.returncode != 0:
        raise LaneActionError(proc.stderr.strip() or f"chitra-goals exited {proc.returncode}")


def ack_lane(state_dir: Path, lane_id: str) -> None:
    _resolve_ask(state_dir, lane_id, basis=None)


def answer_lane(state_dir: Path, lane_id: str, text: str) -> None:
    if not text.strip():
        raise LaneActionError("answer text must not be empty")
    _resolve_ask(state_dir, lane_id, basis=text.strip())

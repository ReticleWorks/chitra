"""boardd's two write endpoints. No new store: both shell out to the
already-governed `chitra-goals` CLI (chitra.goals_cli), the same tool an
operator would run by hand. boardd never touches goals.json itself.

- ack:    `chitra-goals resolve-ask --all` — clears every open ask on the
          lane with the CLI's own default basis, no answer text needed.
- answer: `chitra-goals resolve-ask --all --basis <text>` — same, but the
          operator's answer becomes the recorded basis for retiring them.

Neither is a status change: it only retires open_asks.

A status-only review item — blocked, completion-disputed, ... with no
literal ask — is on the board's stack all the same, and the operator must
still be able to say something to it. `answer` records that text the way an
answered ask records it: the board's own review ask is added and retired in
one go, with the operator's words as its basis. The lane's agent reads
`retired_asks` off its own goal record, so the answer reaches the session by
the route a real answer takes. `ack` has no text to record, so a lane with
nothing to clear still raises LaneActionError(no_op=True) and app.py turns
that into 409, never a false success.
"""

import subprocess
import sys
from pathlib import Path

from chitra.goals import GoalRecord, load_goals

CHITRA_GOALS_TIMEOUT = 10.0

# The ask recorded for a lane that carried none, so the operator's answer has
# a question to be the answer to. It is the board's own review prompt, not a
# question the lane asked.
BOARD_REVIEW_ASK = "Operator review of this lane's status, from the board."


class LaneActionError(Exception):
    def __init__(self, message: str, *, not_found: bool = False, no_op: bool = False):
        super().__init__(message)
        self.not_found = not_found
        self.no_op = no_op


def _find_record(state_dir: Path, lane_id: str) -> GoalRecord:
    """boardd's write endpoints are addressed by lane_id; chitra-goals wants
    the exact session_ref. Look the record up from the same file boardd
    just read."""
    for record in load_goals(state_dir, allow_newer=True):
        if record.lane_id == lane_id or record.session_ref == lane_id:
            return record
    raise LaneActionError(f"no lane found for {lane_id!r}", not_found=True)


def _run_goals(state_dir: Path, *args: str) -> None:
    """One `chitra-goals` invocation. `-m` rather than the console-script name:
    works whether or not the entry point is on PATH in boardd's own process
    environment."""
    argv = [sys.executable, "-m", "chitra.goals_cli", args[0], "--root", str(state_dir), *args[1:]]
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


def _resolve_ask(state_dir: Path, lane_id: str, *, basis: str | None) -> GoalRecord:
    before = _find_record(state_dir, lane_id)
    if not before.open_asks:
        raise LaneActionError(f"lane {lane_id!r} has no open ask to resolve", no_op=True)
    args = ["resolve-ask", "--session-ref", before.session_ref, "--all", "--retired-by", "operator"]
    if basis:
        args += ["--basis", basis]
    _run_goals(state_dir, *args)
    return _find_record(state_dir, lane_id)


def ack_lane(state_dir: Path, lane_id: str) -> GoalRecord:
    return _resolve_ask(state_dir, lane_id, basis=None)


def answer_lane(state_dir: Path, lane_id: str, text: str) -> GoalRecord:
    """Record the operator's answer on the lane, ask or no ask.

    A status-only review lane carries nothing for resolve-ask to retire, but
    the board still offers it a Send, so give the answer somewhere to land:
    add the board's own review ask, then retire it with the answer as its
    basis. That is the same `retired_asks` entry an answered ask leaves, in
    the same record the lane's agent reads.

    ponytail: two chitra-goals calls, not one — the lane holds the review ask
    between them, which a monitor tick inside that window would read as a
    live ask. One verb that records an operator note would replace both.
    """
    text = text.strip()
    if not text:
        raise LaneActionError("answer text must not be empty")
    record = _find_record(state_dir, lane_id)
    if not record.open_asks:
        _run_goals(state_dir, "add-ask", "--session-ref", record.session_ref, "--ask", BOARD_REVIEW_ASK)
    return _resolve_ask(state_dir, lane_id, basis=text)

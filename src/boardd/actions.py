"""boardd's two write endpoints. No new store: both shell out to the
already-governed `chitra-goals` CLI (chitra.goals_cli), the same tool an
operator would run by hand. boardd never touches goals.json itself.

- ack:    `chitra-goals resolve-ask --all` — clears every open ask on the
          lane as a dismissal; no answer text, no claimed ruling.
- answer: `chitra-goals answer --text <text>` — the operator's words become
          a canonical decision bound to this lane, retire the open ask
          truthfully, and land on the dispatch queue as an `operator_relay`
          order the lane's session actually reads. If the lane was held for
          that question, the same verb resumes it once no open ask remains.

`ack` is not a status change and never fabricates an answer: a lane with
nothing to dismiss raises LaneActionError(no_op=True) and app.py turns that
into 409, never a false success.
"""

import subprocess
import sys
from pathlib import Path

from chitra.goals import GoalRecord, load_goals

CHITRA_GOALS_TIMEOUT = 10.0


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
    """Deliver the operator's answer through the governed answer path.

    `chitra-goals answer` writes the canonical decision, retires the open
    ask or foreground task that held the lane, enqueues the verbatim
    `operator_relay` order, resumes the lane when the answer cleared its
    question hold, and requeues anything deferred while it waited — one
    transaction, not the board's old add-ask/resolve-ask pair.
    """
    text = text.strip()
    if not text:
        raise LaneActionError("answer text must not be empty")
    record = _find_record(state_dir, lane_id)
    _run_goals(state_dir, "answer", "--session-ref", record.session_ref, "--text", text)
    return _find_record(state_dir, lane_id)

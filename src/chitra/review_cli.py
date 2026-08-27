"""CLI for the shared reviewer: one envelope in, one verdict out.

``chitra-review --mode lane|monitor`` reads one JSON envelope on stdin,
runs one isolated reviewer process against the shared rubric, enforces the
grounding rule, and writes the ``ReviewerVerdict`` JSON to stdout. Both
judged surfaces use this single entry point so a caller treats a rejection
from either identically.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from chitra.autonomy import DEFAULT_AUTONOMY_POLICY, AutonomyPolicy
from chitra.goal_enforcement import (
    REVIEWER_ATTEMPTS,
    ClaudeProcessReviewer,
    FrozenGoal,
    ReviewerProcessError,
    _validate_bound_review,
)
from chitra.review_rubric import (
    GoalReviewError,
    MonitorContract,
    WatchedSessionBehavior,
    contract_id_for,
)

logger = structlog.get_logger(__name__)


class ReviewEnvelope(BaseModel):
    """The stdin payload chitra-review accepts."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["lane", "monitor"]
    session_ref: str = Field(min_length=1)
    final_message: str = Field(min_length=1)
    reviewer_id: str = "chitra-review"
    goal: dict[str, object] | None = None
    context: str = ""


def _frozen_goal_from_envelope(goal: dict[str, object]) -> FrozenGoal:
    """Strict-validate the goal fields and recompute their contract id.

    A caller-supplied ``contract_id`` is never trusted: the snapshot is
    content-addressed here so a forged binding cannot cross the CLI boundary.
    """
    fields = ("session_ref", "intent", "goal", "done_when", "scope", "source", "goal_version")
    missing = [name for name in fields if name not in goal]
    if missing:
        raise GoalReviewError("lane goal envelope is missing required fields: " + ", ".join(missing))
    try:
        autonomy_policy = AutonomyPolicy.model_validate(goal.get("autonomy_policy", DEFAULT_AUTONOMY_POLICY))
    except ValueError as exc:
        raise GoalReviewError(f"lane goal envelope carries an invalid autonomy policy: {exc}") from exc
    payload = {name: goal[name] for name in fields}
    payload["autonomy_policy"] = autonomy_policy.model_dump(mode="json")
    supplied = goal.get("contract_id")
    if isinstance(supplied, str) and supplied != contract_id_for(payload):
        raise GoalReviewError(
            f"lane goal envelope carries contract_id {supplied!r}, which does not match its content ({contract_id_for(payload)})"
        )
    return FrozenGoal.model_validate({**payload, "contract_id": contract_id_for(payload)})


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-review",
        description="Review one turn envelope on stdin and emit the ReviewerVerdict JSON.",
    )
    parser.add_argument("--mode", choices=("lane", "monitor"), required=True, help="Which surface is under review.")
    parser.add_argument("--command", default="claude", help="Reviewer command to invoke.")
    parser.add_argument("--model", default=None, help="Optional model flag passed through to the reviewer command.")
    parser.add_argument("--timeout-seconds", type=int, default=120, help="Per-attempt process timeout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # A CLI whose contract is one JSON object on stdout cannot let structlog's
    # default stdout factory interleave log lines with the verdict; warnings
    # belong on stderr where a caller keeps them separate from the result.
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    try:
        envelope = ReviewEnvelope.model_validate(json.loads(sys.stdin.read()))
    except ValueError as exc:
        print(f"chitra-review: invalid envelope: {exc}", file=sys.stderr)
        return 2
    try:
        if envelope.mode != args.mode:
            raise GoalReviewError(
                f"--mode {args.mode} conflicts with the envelope's mode={envelope.mode}; pass matching modes"
            )
        behavior = WatchedSessionBehavior.from_turn(envelope.session_ref, envelope.final_message)
        contract: FrozenGoal | MonitorContract
        if args.mode == "lane":
            if envelope.goal is None:
                raise GoalReviewError("mode=lane requires a goal snapshot in the envelope")
            contract = _frozen_goal_from_envelope(envelope.goal)
            if envelope.session_ref != contract.session_ref:
                raise GoalReviewError(
                    f"envelope session {envelope.session_ref!r} does not match the frozen goal session {contract.session_ref!r}"
                )
        else:
            contract = MonitorContract.create(session_ref=envelope.session_ref, context=envelope.context)
        verdict = ClaudeProcessReviewer(
            command=args.command,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            runner=subprocess.run,
            attempts=REVIEWER_ATTEMPTS,
        ).review(contract, behavior, envelope.reviewer_id)
        _validate_bound_review(verdict, reviewer_id=envelope.reviewer_id, goal=contract, behavior=behavior)
    except (GoalReviewError, ReviewerProcessError, ValidationError) as exc:
        print(f"chitra-review: {exc}", file=sys.stderr)
        return 3
    print(verdict.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

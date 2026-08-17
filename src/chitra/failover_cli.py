"""chitra-failover — the manual verbs behind rate-limit pause and resume.

``chitra-rate-limit-guard`` runs one sweep on a timer and decides for itself
which lanes to pause and resume. This command exposes the same three steps to
a person, in the same order, calling the same functions:

* ``evaluate`` shows what a sweep would do right now and changes nothing.
* ``run`` starts the pause. With ``--lane`` it pauses exactly one lane.
* ``resume`` starts the resume for one due lane.

There is no second decision path here. If ``evaluate`` says a lane does not
qualify, ``run --lane`` on that lane refuses for the same printed reason.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from chitra.policy_config import load_policy_config
from chitra.rate_limit_guard import (
    _session_ref_for,
    apply_pause,
    apply_resume,
    plan_pauses,
    plan_resumes,
    select_next_resume,
    sweep,
)
from chitra.usage import AccountedVerdict, CodexSnapshotError, codex_snapshot, evaluate_grouped, read_snapshots


class FailoverError(RuntimeError):
    """The requested failover step could not be taken."""


def collect_verdicts(args: argparse.Namespace, *, now: datetime) -> list[AccountedVerdict]:
    """Read this host's usage snapshots and evaluate them, exactly as a sweep does."""
    policy = load_policy_config(args.policy_config)
    snapshots = read_snapshots(args.usage_dir, staleness_seconds=args.staleness_seconds, now=now)
    if args.codex:
        snapshots.append((codex_snapshot(codex_bin=args.codex_bin, now=now), True))
    return evaluate_grouped(snapshots, policy=policy.usage)


def run_evaluate(args: argparse.Namespace) -> int:
    """Print the pause and resume plan without touching a single lane."""
    now = datetime.now(UTC)
    policy = load_policy_config(args.policy_config)
    verdicts = collect_verdicts(args, now=now)
    to_pause, pause_skips = plan_pauses(verdicts, host=args.host, goals_root=args.goals_root)
    to_resume, resume_escalations = plan_resumes(goals_root=args.goals_root, verdicts=verdicts, policy=policy.usage, now=now)
    payload = {
        "at": now.isoformat(),
        "host": args.host,
        "verdicts": [
            {
                "session_id": verdict.session_id,
                "tmux_session": verdict.tmux_session,
                "kind": verdict.kind,
                "level": verdict.level,
                "account": verdict.account,
            }
            for verdict in verdicts
        ],
        "would_pause": [_session_ref_for(verdict, host=args.host) or verdict.session_id for verdict in to_pause],
        "pause_skipped": pause_skips,
        "would_resume": [record.session_ref for record in to_resume],
        "resume_escalations": resume_escalations,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def run_run(args: argparse.Namespace) -> int:
    """Start the pause. Without ``--lane`` this is one full sweep pass."""
    now = datetime.now(UTC)
    policy = load_policy_config(args.policy_config)
    if args.lane is None:
        report = sweep(
            usage_dir=args.usage_dir,
            host=args.host,
            staleness_seconds=args.staleness_seconds,
            include_codex=args.codex,
            codex_bin=args.codex_bin,
            goals_root=args.goals_root,
            queue_dir=args.queue_dir,
            policy=policy,
            now=now,
        )
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0

    verdicts = collect_verdicts(args, now=now)
    to_pause, skips = plan_pauses(verdicts, host=args.host, goals_root=args.goals_root)
    for verdict in to_pause:
        if _session_ref_for(verdict, host=args.host) == args.lane:
            txn = apply_pause(verdict, host=args.host, goals_root=args.goals_root, now=now)
            print(json.dumps({"paused": txn.session_ref, "phase": txn.phase, "resume_at": txn.resume_at}, indent=2, sort_keys=True))
            # The checkpoint, stop, and quiescence checks are the transaction
            # machine's, driven by later sweeps. Starting the pause by hand
            # does not skip them.
            return 0
    # Say why, using the planner's own words rather than inventing a reason.
    reason = next((line for line in skips if line.startswith(f"{args.lane}:")), "")
    raise FailoverError(reason or f"{args.lane} does not currently qualify for a rate-limit pause")


def run_resume(args: argparse.Namespace) -> int:
    """Start the resume for one due lane."""
    now = datetime.now(UTC)
    policy = load_policy_config(args.policy_config)
    verdicts = collect_verdicts(args, now=now)
    eligible, escalations = plan_resumes(goals_root=args.goals_root, verdicts=verdicts, policy=policy.usage, now=now)
    if args.lane is not None:
        eligible = [record for record in eligible if record.session_ref == args.lane]
    chosen = select_next_resume(eligible)
    if chosen is None:
        detail = "; ".join(escalations) if escalations else "no held lane is due and back under its limit"
        raise FailoverError(detail)
    txn = apply_resume(chosen, goals_root=args.goals_root, now=now)
    # The hold is not cleared here. It clears only once the resume nudge is
    # confirmed delivered, which later sweeps verify.
    print(json.dumps({"resuming": txn.session_ref, "phase": txn.phase}, indent=2, sort_keys=True))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-failover",
        description="Evaluate, start, and reverse rate-limit lane pauses by hand, using the guard's own decision path.",
    )
    parser.add_argument("--usage-dir", type=Path, required=True, help="Directory of chitra.usage.v1 snapshots for one host.")
    parser.add_argument("--host", required=True, help="The host these snapshots' sessions run on.")
    parser.add_argument("--staleness-seconds", type=int, default=1200)
    parser.add_argument("--codex", action="store_true", help="Also read this host's local Codex account usage.")
    parser.add_argument("--codex-bin", type=Path, default=Path("codex"))
    parser.add_argument("--goals-root", type=Path, default=None)
    parser.add_argument("--queue-dir", type=Path, default=None)
    parser.add_argument("--policy-config", type=Path, default=None)
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("evaluate", help="Show the pause and resume plan. Changes nothing.")
    run_parser = sub.add_parser("run", help="Start the pause: one full sweep, or one lane with --lane.")
    run_parser.add_argument("--lane", default=None, help="Pause exactly this session_ref (host:session:pane).")
    resume_parser = sub.add_parser("resume", help="Start the resume for one due lane.")
    resume_parser.add_argument("--lane", default=None, help="Resume this session_ref instead of the next in order.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    verbs = {"evaluate": run_evaluate, "run": run_run, "resume": run_resume}
    try:
        return verbs[args.verb](args)
    except (FailoverError, CodexSnapshotError, OSError, ValueError) as exc:
        print(f"chitra-failover: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

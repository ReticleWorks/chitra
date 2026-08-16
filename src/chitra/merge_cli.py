"""chitra-merge — the manual verb behind auto-merge-on-green.

``polyphony-chitra-merged`` is this command on a timer. There is one decision
path and one merge path; the daemon supplies no extra permission and no extra
leniency. Anything you can watch the daemon do, you can run yourself here, and
``--dry-run`` shows the decision without acting on it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chitra.merge import (
    MergeError,
    MergePolicy,
    append_merge_record,
    fetch_state,
    merge,
    repo_merge_lock,
    resolve_identity,
)
from chitra.policy_config import load_policy_config
from chitra.state_paths import state_dir as default_state_dir


def merge_ledger_path(root: Path) -> Path:
    """Return the merge ledger for one state root."""
    return root / "merge-ledger.jsonl"


def policy_from_config(path: Path | None) -> MergePolicy:
    """Build the merge policy from the operator's policy.yaml."""
    configured = load_policy_config(path).merge
    return MergePolicy(
        allowed_repos=tuple(configured.allowed_repos),
        lane_authors=tuple(configured.lane_authors),
        hold_label=configured.hold_label,
        app_login=configured.app_login,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-merge",
        description="Verify one pull request against the merge policy and merge it when it qualifies.",
    )
    parser.add_argument("repo", help="Repository as owner/name.")
    parser.add_argument("pr", type=int, help="Pull request number.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the decision and write a ledger line, but do not merge.",
    )
    parser.add_argument("--policy-config", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    return parser


def run(args: argparse.Namespace) -> int:
    policy = policy_from_config(args.policy_config)
    root = args.state_dir or default_state_dir()
    identity = resolve_identity()
    state = fetch_state(args.repo, args.pr)
    with repo_merge_lock(root / "merge-locks", args.repo) as held:
        if not held:
            # Not an error: another merge is in flight for this repository and
            # the answer is to come back, not to race it.
            print(json.dumps({"repo": args.repo, "number": args.pr, "skipped": "another merge holds this repo"}))
            return 0
        record = merge(state, policy, identity, dry_run=args.dry_run)
        append_merge_record(merge_ledger_path(root), record)
    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
    # A refusal is a correct, expected answer, so it is not an error exit. A
    # caller that wants to branch on it reads decision.allowed.
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return run(args)
    except (MergeError, OSError, ValueError) as exc:
        print(f"chitra-merge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

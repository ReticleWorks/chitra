"""``polyphony-chitra-merged`` — auto-merge-on-green.

The daemon is ``chitra-merge`` on a loop. It discovers open pull requests
authored by lanes on allowlisted repositories and hands each one to
:func:`chitra.merge.merge`. It holds no extra permission and applies no extra
leniency: every refusal reason a human sees from the manual verb is the same
reason the daemon records.

Three things this daemon deliberately does not do. It never edits branch
protection, so a repository whose rules block the merge stays blocked. It
never merges two pull requests at once in one repository, because the second
would be racing a base branch that is about to move. And it never merges
under an identity other than the configured GitHub App, so a ledger line
naming the App is evidence the App did it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from chitra.dispatch import enqueue_dispatch_order
from chitra.goals import GoalRecord, load_goals
from chitra.merge import (
    BOT_LOGIN_SUFFIX,
    GhRunner,
    GitHubIdentity,
    MergeError,
    MergePolicy,
    MergeRecord,
    append_merge_record,
    fetch_state,
    merge,
    minting_gh_runner,
    repo_merge_lock,
    resolve_identity,
    run_gh,
)
from chitra.merge_cli import merge_ledger_path, policy_from_config
from chitra.orders import DispatchOrder
from chitra.policy_config import load_policy_config
from chitra.state_paths import default_queue_dir
from chitra.state_paths import state_dir as default_state_dir

LOGGER = logging.getLogger("chitra.merged")

#: How many open pull requests to consider per repository per pass. A lane
#: fleet does not open hundreds at once, and a bound keeps one wedged
#: repository from starving the others.
DISCOVERY_LIMIT = 50


def discover_pull_requests(repo: str, policy: MergePolicy, *, runner: GhRunner = run_gh) -> list[int]:
    """Return open pull request numbers in ``repo`` authored by a lane.

    The author filter here is a cheap pre-screen so the daemon does not fetch
    full state for every open pull request in the repository. It is not the
    authority — :func:`chitra.merge.decide` checks the author again on the
    state it actually merges.
    """
    result = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(DISCOVERY_LIMIT),
            "--json",
            "number,author,isDraft",
        ]
    )
    if result.returncode != 0:
        raise MergeError(f"could not list open pull requests for {repo}: {result.stderr.strip()}")
    try:
        rows = json.loads(result.stdout)
    except ValueError as exc:
        raise MergeError(f"could not parse the pull request list for {repo}: {exc}") from exc
    numbers: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("isDraft"):
            continue
        author = (row.get("author") or {}).get("login", "")
        # Cheap pre-screen only. decide() checks both of these again on the
        # state it actually merges; skipping here just avoids fetching full
        # state for pull requests that cannot qualify.
        if author.endswith(BOT_LOGIN_SUFFIX) or author not in policy.lane_authors:
            continue
        number = row.get("number")
        if isinstance(number, int):
            numbers.append(number)
    return sorted(numbers)


def lane_for_pull_request(records: Iterable[GoalRecord], url: str) -> str:
    """Return the session ref of the one lane whose goal names ``url``.

    Lanes record the pull request they are waiting on in their goal, so the
    goal document is the only place in the fleet that ties a pull request back
    to the session that opened it. If no goal names the pull request, or more
    than one does, this returns the empty string: guessing which lane to page
    is worse than not paging one.
    """
    if not url:
        return ""
    matches = {rec.session_ref for rec in records if url in rec.now or url in rec.source}
    if len(matches) != 1:
        return ""
    return matches.pop()


def notification_nudge(record: MergeRecord) -> str:
    """Return the text sent to the lane whose pull request was merged."""
    return (
        f"chitra-merged merged {record.url} as {record.identity.login}. "
        f"The branch is in {record.repo} main now; nothing is waiting on you for the merge itself."
    )


def notify_lane(
    record: MergeRecord,
    *,
    queue_dir: Path,
    goals_root: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Tell the authoring lane its pull request merged. Returns the session ref.

    Returns the empty string when no single lane could be identified, which is
    recorded rather than retried — the merge already happened and a missing
    notification is not a reason to merge again.
    """
    session_ref = lane_for_pull_request(load_goals(goals_root), record.url)
    if not session_ref:
        LOGGER.info("merged %s but no single lane goal names it; not notifying", record.url)
        return ""
    order_id = f"chitra-merged-{uuid.uuid4().hex[:12]}"
    enqueue_dispatch_order(
        queue_dir,
        DispatchOrder(
            order_id=order_id,
            session_ref=session_ref,
            nudge=notification_nudge(record),
            task_type="merge-notification",
            created_at=(now or datetime.now(UTC)).isoformat(),
        ),
    )
    return session_ref


def process_repo(
    repo: str,
    policy: MergePolicy,
    identity: GitHubIdentity,
    *,
    root: Path,
    queue_dir: Path,
    goals_root: Path | None = None,
    dry_run: bool = False,
    runner: GhRunner = run_gh,
) -> list[MergeRecord]:
    """Run one pass over one repository. At most one merge happens."""
    records: list[MergeRecord] = []
    for number in discover_pull_requests(repo, policy, runner=runner):
        state = fetch_state(repo, number, runner=runner)
        with repo_merge_lock(root / "merge-locks", repo) as held:
            if not held:
                LOGGER.info("another merge holds %s; leaving #%d for the next pass", repo, number)
                return records
            record = merge(state, policy, identity, dry_run=dry_run, runner=runner)
            append_merge_record(merge_ledger_path(root), record)
        records.append(record)
        if record.merged:
            notify_lane(record, queue_dir=queue_dir, goals_root=goals_root)
            # One merge per repository per pass. Every other open pull request
            # here now has a stale base, so their check results no longer say
            # what they said a moment ago.
            return records
    return records


def run_once(
    policy: MergePolicy,
    *,
    root: Path,
    queue_dir: Path,
    goals_root: Path | None = None,
    dry_run: bool = False,
    runner: GhRunner = run_gh,
) -> list[MergeRecord]:
    """Run one pass over every allowlisted repository."""
    if not policy.allowed_repos:
        LOGGER.info("no repositories are allowlisted for merge; nothing to do")
        return []
    identity = resolve_identity(expected_app_login=policy.app_login, runner=runner)
    records: list[MergeRecord] = []
    for repo in policy.allowed_repos:
        try:
            records.extend(
                process_repo(
                    repo,
                    policy,
                    identity,
                    root=root,
                    queue_dir=queue_dir,
                    goals_root=goals_root,
                    dry_run=dry_run,
                    runner=runner,
                )
            )
        except (MergeError, OSError) as exc:
            # One unreachable repository must not stop the others.
            LOGGER.warning("skipping %s this pass: %s", repo, exc)
    return records


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polyphony-chitra-merged",
        description="Merge lane-authored pull requests that are green, on allowlisted repositories only.",
    )
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Decide and record, but never merge.",
    )
    parser.add_argument("--policy-config", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--queue-dir", type=Path, default=None)
    parser.add_argument("--goals-root", type=Path, default=None)
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="Override the poll interval from policy.yaml.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = args.state_dir or default_state_dir()
    queue_dir = args.queue_dir or default_queue_dir()
    configured = load_policy_config(args.policy_config).merge
    if not configured.enabled:
        LOGGER.info("auto-merge is disabled in policy; exiting without looking at any pull request")
        return 0
    policy = policy_from_config(args.policy_config)
    interval = args.poll_seconds if args.poll_seconds is not None else configured.poll_seconds
    # A stored installation token expires within the hour and the daemon would
    # keep running, failing every merge while looking alive. Minting per call
    # costs one subprocess and removes that failure entirely.
    runner = minting_gh_runner(configured.token_command) if configured.token_command else run_gh
    if not configured.token_command:
        LOGGER.warning(
            "no merge.token_command configured; using whatever identity gh already holds. "
            "A daemon should mint its own token: a stored installation token expires within the hour."
        )
    while True:
        try:
            for record in run_once(
                policy,
                root=root,
                queue_dir=queue_dir,
                goals_root=args.goals_root,
                dry_run=args.dry_run,
                runner=runner,
            ):
                LOGGER.info("%s#%d: %s", record.repo, record.number, record.decision.reason)
        except MergeError as exc:
            LOGGER.error("pass failed: %s", exc)
        if args.once:
            return 0
        time.sleep(max(interval, 1.0))


if __name__ == "__main__":
    sys.exit(main())

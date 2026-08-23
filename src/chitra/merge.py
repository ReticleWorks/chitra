"""Decide whether one pull request may be merged, and merge it.

Everything here is a pure decision over a fetched state plus one narrow action.
``chitra-merge`` and ``polyphony-chitra-merged`` both call these functions --
the daemon is the manual verb on a timer, never a second implementation with
its own opinions.

The decision is deliberately conservative. It refuses far more than GitHub
would, because the cost of refusing a mergeable pull request is a person
merging it, and the cost of merging an unmergeable one is a broken main branch
nobody chose.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

GhRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

MERGE_LEDGER_SCHEMA = "chitra.merge-ledger.v1"
# Every label that stops a merge, not one. The brake has to match whatever the
# repository already uses, and repositories do not agree: ReticleWorks/chitra
# labels a held pull request `hold`, while the default here was `chitra-hold`,
# a label that does not exist in that repository at all. A brake wired to a
# label nobody applies is not a brake. Matching more labels can only ever
# refuse more, so the safe direction is to recognise every convention in use.
DEFAULT_HOLD_LABELS: tuple[str, ...] = ("chitra-hold", "hold")
# Only CLEAN is accepted. GitHub also merges UNSTABLE, which means a
# non-required check is failing; that is a judgement about which checks matter,
# and this daemon does not make it. BEHIND means the branch has not been
# retested against current main. Both go to a person.
MERGEABLE_STATE = "MERGEABLE"
REQUIRED_MERGE_STATE_STATUS = "CLEAN"
REQUIRED_ROLLUP_STATE = "SUCCESS"

# A merge is scoped by three independent things, not one. Gate state says the
# change is mechanically safe to land. It says nothing about WHOSE work it is,
# or whether anyone still wants it. Both of the following were learned from
# real merges an interim auto-merger made overnight on 2026-08-16.
#
# Any login ending in [bot] is refused outright, regardless of the lane
# allowlist. A dependency bot opened five pull requests that merged on green,
# one of them a major version bump from 1.28.1 to 2.0.0. Green CI is evidence
# the tests passed, never evidence a major bump is runtime-safe.
BOT_LOGIN_SUFFIX = "[bot]"
# A pull request nobody has touched in a day is not obviously still wanted.
# One that had been open about five days merged on green, because "green and
# mergeable" is a statement about the branch, not about anyone's intent.
DEFAULT_MAX_AGE_HOURS = 24.0

RefusalReason = Literal[
    "ok",
    "repo_not_allowlisted",
    "author_not_a_lane",
    "author_is_a_bot",
    "stale_pull_request",
    "hold_label_present",
    "draft",
    "not_mergeable",
    "merge_state_not_clean",
    "checks_not_successful",
    "already_closed",
    "identity_not_app",
]


class MergeError(RuntimeError):
    """A merge could not be attempted or could not be completed."""


class GhCliError(MergeError):
    """The ``gh`` CLI could not answer a required query."""


class IdentityError(MergeError):
    """The acting GitHub identity is not the one this daemon may merge as."""


@dataclass(frozen=True, slots=True)
class GitHubIdentity:
    """Who a merge would be attributed to, and how that was established."""

    login: str
    # "app" only when the token was minted for a GitHub App installation. A
    # personal access token is "user" no matter whose it is.
    kind: Literal["app", "user", "unknown"]
    source: str

    @property
    def is_app(self) -> bool:
        return self.kind == "app"


@dataclass(frozen=True, slots=True)
class PullRequestState:
    """The authoritative GraphQL view of one pull request."""

    repo: str
    number: int
    title: str
    url: str
    author: str
    is_draft: bool
    state: str
    mergeable: str
    merge_state_status: str
    checks_rollup: str
    head_oid: str
    labels: tuple[str, ...] = ()
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "is_draft": self.is_draft,
            "state": self.state,
            "mergeable": self.mergeable,
            "merge_state_status": self.merge_state_status,
            "checks_rollup": self.checks_rollup,
            "head_oid": self.head_oid,
            "labels": list(self.labels),
            "updated_at": self.updated_at,
        }

    def age_hours(self, now: datetime) -> float | None:
        """Hours since this pull request was last touched, or None if unknown.

        Unknown is not zero. A state whose timestamp could not be read must not
        pass a freshness gate by defaulting to fresh.
        """
        if not self.updated_at:
            return None
        try:
            touched = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if touched.tzinfo is None:
            return None
        return (now - touched).total_seconds() / 3600.0


@dataclass(frozen=True, slots=True)
class MergePolicy:
    """What this daemon is allowed to touch, and on whose behalf."""

    allowed_repos: tuple[str, ...] = ()
    lane_authors: tuple[str, ...] = ()
    hold_labels: tuple[str, ...] = DEFAULT_HOLD_LABELS
    app_login: str = ""
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS

    def allows_repo(self, repo: str) -> bool:
        return repo in self.allowed_repos

    def holds_on(self, labels: Sequence[str]) -> str:
        """Return the first hold label present, or empty when none is."""
        held = [label for label in self.hold_labels if label in labels]
        return held[0] if held else ""

    def allows_author(self, author: str) -> bool:
        # An empty lane list means "no author qualifies", not "every author
        # qualifies". A misconfigured allowlist must merge nothing.
        return author in self.lane_authors


@dataclass(frozen=True, slots=True)
class MergeDecision:
    """Whether one pull request may be merged, and the single reason why not."""

    allowed: bool
    reason: RefusalReason
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason, "detail": self.detail}


def decide(
    state: PullRequestState,
    policy: MergePolicy,
    identity: GitHubIdentity,
    *,
    now: datetime | None = None,
) -> MergeDecision:
    """Return whether this pull request may be merged, refusing on first doubt.

    The order matters: configuration and identity are checked before anything
    about the pull request itself, so a misconfigured daemon reports that
    rather than reporting on a pull request it was never allowed to read.

    Scope is three independent questions, and gate state is only one of them.
    Is this mechanically safe to land, whose work is it, and does anyone still
    want it. A pull request can be perfectly green and fail the other two.
    """
    if not policy.allows_repo(state.repo):
        return MergeDecision(False, "repo_not_allowlisted", f"{state.repo} is not in the merge allowlist")
    if not identity.is_app or (policy.app_login and identity.login != policy.app_login):
        return MergeDecision(
            False,
            "identity_not_app",
            f"merges must be attributed to {policy.app_login or 'the automation app'}, not {identity.login} ({identity.kind})",
        )
    # Before the allowlist, not after. A bot login is refused even if someone
    # puts it in lane_authors, because "a lane asked for this" is the whole
    # premise of merging without review and a dependency bot never asked.
    if state.author.endswith(BOT_LOGIN_SUFFIX):
        return MergeDecision(
            False,
            "author_is_a_bot",
            f"{state.author} is a bot; a dependency bump is not lane work and green is not review",
        )
    if not policy.allows_author(state.author):
        return MergeDecision(False, "author_not_a_lane", f"{state.author} is not a declared lane author")
    age = state.age_hours(now or datetime.now(UTC))
    if age is None:
        return MergeDecision(False, "stale_pull_request", "last-updated time could not be read, so freshness is unknown")
    if age > policy.max_age_hours:
        return MergeDecision(
            False,
            "stale_pull_request",
            f"last touched {age:.1f}h ago, past the {policy.max_age_hours:.0f}h bound; green does not mean still wanted",
        )
    if held := policy.holds_on(state.labels):
        return MergeDecision(False, "hold_label_present", f"{held} is set, so a person has taken this one")
    if state.state.upper() != "OPEN":
        return MergeDecision(False, "already_closed", f"pull request state is {state.state}")
    if state.is_draft:
        return MergeDecision(False, "draft", "pull request is a draft")
    if state.mergeable.upper() != MERGEABLE_STATE:
        return MergeDecision(False, "not_mergeable", f"mergeable is {state.mergeable}")
    if state.merge_state_status.upper() != REQUIRED_MERGE_STATE_STATUS:
        return MergeDecision(
            False,
            "merge_state_not_clean",
            f"mergeStateStatus is {state.merge_state_status}, not {REQUIRED_MERGE_STATE_STATUS}",
        )
    if state.checks_rollup.upper() != REQUIRED_ROLLUP_STATE:
        return MergeDecision(False, "checks_not_successful", f"check rollup is {state.checks_rollup}")
    return MergeDecision(True, "ok", "non-draft, mergeable, and every required check succeeded")


_PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      title
      url
      isDraft
      state
      mergeable
      mergeStateStatus
      updatedAt
      author { login }
      labels(first: 50) { nodes { name } }
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup { state }
          }
        }
      }
    }
  }
}
"""


def run_gh(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


class TokenError(MergeError):
    """A fresh GitHub App installation token could not be minted."""


def minting_gh_runner(token_command: Sequence[str], *, runner: GhRunner = run_gh) -> GhRunner:
    """Return a gh runner that mints a fresh installation token per call.

    A GitHub App installation token lives about an hour. A daemon handed one
    static token in an environment file therefore works until roughly the first
    hour of its life and then fails every merge, which is a worse failure than
    not starting: it looks alive. That is what the unit this daemon ships with
    originally specified, and it would have failed on its first night.

    So the token is minted, not stored. ``token_command`` is whatever the host
    already uses to produce one -- on this fleet a git credential helper that
    prints ``password=<token>``. Its output is parsed for that line, or taken
    whole if it prints a bare token.

    The token never reaches argv. It is passed to gh through ``GH_TOKEN`` in
    the child environment, so it cannot show up in a process list.
    """

    def mint() -> str:
        result = subprocess.run(list(token_command), check=False, capture_output=True, text=True, input="")
        if result.returncode != 0:
            raise TokenError(f"could not mint a GitHub App token: {result.stderr.strip() or 'no output'}")
        for line in result.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
        token = result.stdout.strip()
        if not token or "\n" in token:
            raise TokenError("the token command printed no password= line and no bare token")
        return token

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["GH_TOKEN"] = mint()
        # GH_TOKEN wins over a cached gh login, so an operator's stale personal
        # token in gh's config cannot quietly become the merging identity.
        environment.pop("GITHUB_TOKEN", None)
        return subprocess.run(list(command), check=False, capture_output=True, text=True, env=environment)

    return run


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise MergeError(f"repo must be owner/name, got {repo!r}")
    return owner, name


def fetch_state(repo: str, number: int, *, runner: GhRunner = run_gh) -> PullRequestState:
    """Read one pull request's merge-relevant state through the GraphQL API.

    GraphQL rather than ``gh pr view``: ``mergeStateStatus`` is only available
    there, and it is the field that actually encodes "required checks passed
    and the branch is not behind". Reading a weaker source and inferring the
    rest is how an auto-merge lands something nobody verified.
    """
    owner, name = _split_repo(repo)
    result = runner(
        [
            "gh",
            "api",
            "graphql",
            "-H",
            "GraphQL-Features: merge_queue",
            "-f",
            f"query={_PR_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
    )
    if result.returncode != 0:
        raise GhCliError(f"gh api graphql failed for {repo}#{number}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise GhCliError(f"gh api graphql returned invalid JSON for {repo}#{number}: {exc}") from exc
    node = (payload.get("data") or {}).get("repository", {})
    node = (node or {}).get("pullRequest") if isinstance(node, dict) else None
    if not isinstance(node, dict):
        raise GhCliError(f"no pull request {repo}#{number} in the GraphQL response")
    commits = ((node.get("commits") or {}).get("nodes") or [{}])[-1] or {}
    commit = commits.get("commit") or {}
    rollup = commit.get("statusCheckRollup") or {}
    author = (node.get("author") or {}).get("login") or ""
    labels = tuple(item.get("name", "") for item in ((node.get("labels") or {}).get("nodes") or []) if isinstance(item, dict))
    return PullRequestState(
        repo=repo,
        number=int(node.get("number", number)),
        title=str(node.get("title") or ""),
        url=str(node.get("url") or ""),
        author=str(author),
        is_draft=bool(node.get("isDraft")),
        state=str(node.get("state") or ""),
        mergeable=str(node.get("mergeable") or "UNKNOWN"),
        merge_state_status=str(node.get("mergeStateStatus") or "UNKNOWN"),
        # A pull request with no checks at all reports a null rollup. That is
        # not success; it is the absence of evidence, and it is refused.
        checks_rollup=str(rollup.get("state") or "MISSING"),
        head_oid=str(commit.get("oid") or ""),
        labels=labels,
        updated_at=str(node.get("updatedAt") or ""),
    )


def resolve_identity(*, expected_app_login: str = "", runner: GhRunner = run_gh) -> GitHubIdentity:
    """Return the identity the configured token actually acts as.

    ``/installation/repositories`` is the discriminator, not ``/user``. An
    installation token cannot call ``/user`` at all -- GitHub answers 403
    "Resource not accessible by integration" -- so asking ``/user`` first can
    only ever recognise a personal token, and fails closed on the one identity
    this daemon requires. Measured against the real credential on 2026-08-16;
    no fixture would have shown it.

    A 200 from ``/installation/repositories`` therefore proves an installation
    token. A personal token is refused there and is identified through
    ``/user`` instead.

    One honest limit. An installation token does not carry its own app's login
    in any response it is permitted to make, so ``login`` here is the
    configured expectation rather than a reading, and ``source`` says exactly
    that. The login that is genuinely *measured* is recorded after a merge,
    from the pull request's own ``merged_by`` -- see ``merge``.
    """
    installation = runner(["gh", "api", "/installation/repositories", "--jq", ".total_count"])
    if installation.returncode == 0:
        return GitHubIdentity(
            login=expected_app_login or "unnamed-app[bot]",
            kind="app",
            source="gh api /installation/repositories (installation token confirmed; login from policy)",
        )
    result = runner(["gh", "api", "user", "--jq", "[.login, .type] | @tsv"])
    if result.returncode != 0:
        raise IdentityError(
            "could not resolve the acting GitHub identity: it is not an installation token "
            f"({installation.stderr.strip()}) and /user failed ({result.stderr.strip()})"
        )
    parts = result.stdout.strip().split("\t")
    login = parts[0] if parts else ""
    account_type = parts[1] if len(parts) > 1 else ""
    if not login:
        raise IdentityError("the GitHub API returned no login for the configured token")
    kind: Literal["app", "user", "unknown"] = "user" if account_type == "User" else "unknown"
    return GitHubIdentity(login=login, kind=kind, source="gh api user")


@contextmanager
def repo_merge_lock(lock_dir: Path, repo: str, *, timeout_seconds: float = 0.0) -> Iterator[bool]:
    """Hold the one-merge-at-a-time lock for a repository.

    Yields False rather than blocking when another merge already holds it. Two
    merges racing into one repository is how a green pull request lands on a
    base that moved under it.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{repo.replace('/', '_')}.merge.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class MergeRecord:
    """One identity-stamped ledger line for an attempted or refused merge."""

    repo: str
    number: int
    url: str
    author: str
    head_oid: str
    decision: MergeDecision
    identity: GitHubIdentity
    merged: bool
    merge_commit: str
    dry_run: bool
    # Who GitHub says actually merged it, read back after the fact. The
    # identity above is what the daemon believed it was; this is what happened.
    # Empty on every refusal and every dry run, because nothing happened.
    merged_by: str = ""
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": MERGE_LEDGER_SCHEMA,
            "at": self.at,
            "repo": self.repo,
            "number": self.number,
            "url": self.url,
            "author": self.author,
            "head_oid": self.head_oid,
            "merged": self.merged,
            "merge_commit": self.merge_commit,
            "merged_by": self.merged_by,
            "dry_run": self.dry_run,
            "decision": self.decision.to_dict(),
            # The identity is stamped on every line, refusals included. A
            # ledger that only records successes cannot answer "who tried".
            "identity": {
                "login": self.identity.login,
                "kind": self.identity.kind,
                "source": self.identity.source,
            },
        }


def append_merge_record(path: Path, record: MergeRecord) -> None:
    """Append one ledger line under an exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def merge(
    state: PullRequestState,
    policy: MergePolicy,
    identity: GitHubIdentity,
    *,
    dry_run: bool = False,
    runner: GhRunner = run_gh,
) -> MergeRecord:
    """Decide, then merge when allowed, returning the ledger record either way.

    The merge is pinned to the exact commit the decision was made against. If
    the branch moved between the two, GitHub refuses and this reports a failed
    merge rather than landing something that was never verified.

    Nothing here reads or writes branch protection. A pull request that
    protection would refuse is a pull request this refuses.
    """
    decision = decide(state, policy, identity)
    if not decision.allowed or dry_run:
        return MergeRecord(
            repo=state.repo,
            number=state.number,
            url=state.url,
            author=state.author,
            head_oid=state.head_oid,
            decision=decision,
            identity=identity,
            merged=False,
            merge_commit="",
            dry_run=dry_run,
        )
    result = runner(
        [
            "gh",
            "pr",
            "merge",
            str(state.number),
            "--repo",
            state.repo,
            "--squash",
            "--match-head-commit",
            state.head_oid,
        ]
    )
    if result.returncode != 0:
        raise MergeError(f"gh pr merge failed for {state.repo}#{state.number}: {result.stderr.strip()}")
    outcome = merge_outcome(state, runner=runner)
    return MergeRecord(
        repo=state.repo,
        number=state.number,
        url=state.url,
        author=state.author,
        head_oid=state.head_oid,
        decision=decision,
        identity=identity,
        merged=True,
        merge_commit=outcome.merge_commit,
        merged_by=outcome.merged_by,
        dry_run=False,
    )


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """What GitHub recorded after a merge, read back rather than assumed."""

    merged_by: str
    merge_commit: str


def merge_outcome(state: PullRequestState, *, runner: GhRunner = run_gh) -> MergeOutcome:
    """Read back who merged and which commit resulted.

    Both are read, neither is inferred. A squash merge creates a NEW commit, so
    the merged head is not the merge commit: recording the head under that name
    would put a commit sha in the ledger that does not exist on the base branch.
    Observed on the first real merge, 2026-08-16 -- head
    ``185959d1``, actual merge commit ``e4823048``.

    ``merged_by`` is the only identity in the record that is measured rather
    than configured, which is why it is worth a second call. A merge attributed
    to a human while the ledger claims an app is the failure it answers, and it
    cannot be answered before the merge happens.

    A failure to read either is not a failure to merge -- the merge already
    happened -- so this returns what it has rather than raising.
    """
    owner, name = _split_repo(state.repo)
    result = runner(
        [
            "gh",
            "api",
            f"/repos/{owner}/{name}/pulls/{state.number}",
            "--jq",
            '[(.merged_by.login // ""), (.merge_commit_sha // "")] | @tsv',
        ]
    )
    if result.returncode != 0:
        logger.warning("merge_outcome_unreadable", repo=state.repo, number=state.number, stderr=result.stderr.strip())
        return MergeOutcome(merged_by="", merge_commit="")
    parts = result.stdout.strip().split("\t")
    return MergeOutcome(
        merged_by=parts[0] if parts else "",
        merge_commit=parts[1] if len(parts) > 1 else "",
    )

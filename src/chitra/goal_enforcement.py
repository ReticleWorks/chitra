"""Fail-closed review of a watched lane's behavior against its frozen goal.

The object under review is always the monitored session's completed turn:
its direction, questions, and completion posture. Chitra's prospective reply
is never placed in these prompts. Each reviewer invocation is a separate
``claude -p`` process with no shared conversation state.
"""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self

import structlog
from pydantic import Field, model_validator

from chitra.goals import (
    GoalNotFoundError,
    GoalRecord,
    check_specification,
    get_goal,
    record_review_restart,
    validate_goal,
)
from chitra.review_rubric import (
    PERSISTENCE_FINDING_CODES as PERSISTENCE_FINDING_CODES,
)
from chitra.review_rubric import (
    REVIEWER_SYSTEM_PROMPT as REVIEWER_SYSTEM_PROMPT,
)
from chitra.review_rubric import (
    FindingCode as FindingCode,
)
from chitra.review_rubric import (
    GoalReviewError as GoalReviewError,
)
from chitra.review_rubric import (
    MonitorContract as MonitorContract,
)
from chitra.review_rubric import (
    ReviewerVerdict as ReviewerVerdict,
)
from chitra.review_rubric import (
    ReviewFinding as ReviewFinding,
)
from chitra.review_rubric import (
    ReviewMode,
    _FrozenModel,
    _sha256,
    build_review_prompt,
    contract_id_for,
    enforce_grounding,
    new_turn_nonce,
    ungrounded_citations,
)
from chitra.review_rubric import (
    WatchedSessionBehavior as WatchedSessionBehavior,
)

logger = structlog.get_logger(__name__)

#: The reviewer's whole system prompt lives in ``chitra.review_rubric``
#: beside the rubric it serves; it is re-exported here because every caller
#: binds it through this module.

#: How many times one reviewer invocation may be attempted before it fails
#: closed. The failure being retried is intermittent, not systematic: the same
#: prompt and the same flags return either a valid verdict or a degenerate
#: ``{"ok": ...}`` object, and the production reviewer wrapper's own header
#: records that same shape as observed. Without a retry the intermittency
#: reaches a person, because watchd turns one unusable reply into a blocked
#: session and an ask to review the work by hand.
#:
#: Five, not three, and the difference is measured rather than chosen. Fifteen
#: runs of the real review path on tophand, 2026-08-17, needed 21 attempts in
#: total: 8 replies were unusable, so a single reply is unusable about 38% of the
#: time. Three attempts therefore fail closed on roughly 5% of reviews, and the
#: sample bore that out -- one of the fifteen ran out of attempts and became a
#: false blocker. Five attempts put the same arithmetic near 0.8%, about one in
#: 125. The cost is paid only by a review already going wrong: a run that
#: succeeds first time still makes one call.
REVIEWER_ATTEMPTS = 5

MIN_REVIEWERS = 1
DEFAULT_REVIEWERS = 2
REVIEW_LOG_NAME = "goal_reviews.jsonl"


class ReviewerProcessError(GoalReviewError):
    """Raised when an isolated reviewer process fails or returns bad JSON."""


_FENCED_BLOCK = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL)


def unwrap_json_object(text: str) -> str:
    """Return the JSON object in a reviewer reply, with its packaging removed.

    A reviewer that answers correctly but wraps the object in a fenced code
    block used to fail the strict parse, and a failed parse is not harmless
    here: ``watchd`` turns an unavailable review into a ``blocked`` status and
    an ask for someone to review the session by hand. A correct review of
    healthy work became a false blocker.

    Two deviations are tolerated and no more — a code fence, and prose either
    side of the object. Anything else still fails, because the verdict contract
    is what keeps a malformed answer from being treated as a review.
    """
    stripped = text.strip()
    fenced = _FENCED_BLOCK.search(stripped)
    if fenced is not None:
        stripped = fenced.group("body").strip()
    if stripped.startswith("{"):
        return stripped
    opening = stripped.find("{")
    closing = stripped.rfind("}")
    return stripped[opening : closing + 1] if 0 <= opening < closing else stripped


class FrozenGoal(_FrozenModel):
    """Content-addressed strategic goal snapshot for one review round."""

    session_ref: str
    intent: str
    goal: str
    done_when: str
    scope: str
    source: str
    goal_version: int = Field(ge=1)
    contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def freeze_goal(record: GoalRecord) -> FrozenGoal:
    """Strict-validate and content-address one current goal record."""
    issues = [*validate_goal(record), *check_specification(record)]
    if issues:
        raise GoalReviewError("goal is not strict-valid: " + "; ".join(dict.fromkeys(issues)))
    payload = {
        "session_ref": record.session_ref,
        "intent": record.intent,
        "goal": record.goal,
        "done_when": record.enrolled_done_when or record.done_when,
        "scope": record.scope,
        "source": record.source,
        "goal_version": record.goal_version,
    }
    return FrozenGoal.model_validate({**payload, "contract_id": contract_id_for(payload)})


class SessionReviewSignal(_FrozenModel):
    """Unanimity result fed into Chitra's later decision attestation."""

    signal_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    session_ref: str
    goal_contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    behavior_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["accept", "reject"]
    reviewer_ids: tuple[str, ...] = Field(min_length=1)
    findings: tuple[ReviewFinding, ...] = ()
    restarted_after_redirect: bool = False
    recorded_at: str

    @model_validator(mode="after")
    def validate_signal(self) -> Self:
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids):
            raise ValueError("reviewer ids must be unique")
        if self.verdict == "accept" and self.findings:
            raise ValueError("an accepted signal cannot carry findings")
        payload = self.model_dump(mode="json", exclude={"signal_id"})
        if self.signal_id != f"sha256:{_sha256(payload)}":
            raise ValueError("signal_id does not match the review signal")
        return self

    @classmethod
    def create(
        cls,
        *,
        session_ref: str,
        goal_contract_id: str,
        behavior_sha256: str,
        verdict: Literal["accept", "reject"],
        reviewer_ids: Sequence[str],
        findings: Sequence[ReviewFinding] = (),
        restarted_after_redirect: bool = False,
        recorded_at: str | None = None,
    ) -> SessionReviewSignal:
        payload = {
            "session_ref": session_ref,
            "goal_contract_id": goal_contract_id,
            "behavior_sha256": behavior_sha256,
            "verdict": verdict,
            "reviewer_ids": list(reviewer_ids),
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "restarted_after_redirect": restarted_after_redirect,
            "recorded_at": recorded_at or datetime.now(UTC).isoformat(),
        }
        return cls.model_validate({**payload, "signal_id": f"sha256:{_sha256(payload)}"})


class BehaviorReviewer(Protocol):
    """One isolated review invocation."""

    def review(self, goal: ReviewContract, behavior: WatchedSessionBehavior, reviewer_id: str) -> ReviewerVerdict: ...


ReviewContract = FrozenGoal | MonitorContract

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


class ClaudeProcessReviewer:
    """Launch a fresh ``claude -p`` process for every reviewer context."""

    def __init__(
        self,
        *,
        command: str = "claude",
        model: str | None = None,
        timeout_seconds: int = 120,
        runner: ProcessRunner = subprocess.run,
        attempts: int = REVIEWER_ATTEMPTS,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be a positive integer")
        self.command = command
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.attempts = attempts

    @staticmethod
    def _prompt(goal: ReviewContract, behavior: WatchedSessionBehavior, reviewer_id: str) -> str:
        mode: ReviewMode = "monitor" if isinstance(goal, MonitorContract) else "lane"
        return build_review_prompt(
            mode=mode,
            reviewer_id=reviewer_id,
            contract=goal.model_dump(mode="json"),
            behavior=behavior,
            nonce=new_turn_nonce(),
        )

    def review(self, goal: ReviewContract, behavior: WatchedSessionBehavior, reviewer_id: str) -> ReviewerVerdict:
        command = [
            self.command,
            "-p",
            self._prompt(goal, behavior, reviewer_id),
            "--output-format",
            "text",
            # No tools, and no memory between reviewers. The turn under review
            # is already in the prompt, so a reviewer has nothing legitimate to
            # read, run, or fetch, and one reviewer's process must not carry
            # state into the next one's -- these rounds are meant to be
            # independent.
            "--no-session-persistence",
            "--allowed-tools",
            "",
            # Replace the system prompt rather than inheriting the host's.
            # Measured on tophand 2026-08-17: with the ambient system prompt in
            # place, an operator-installed output style told the reviewer to
            # answer in narrated prose, so it returned commentary ABOUT the
            # verdict instead of the verdict. Every review then failed
            # validation and fell back to the fail-closed "unavailable" verdict,
            # which watchd turns into a blocked session and a manual-review ask.
            # A replacement system prompt makes the reviewer indifferent to
            # whatever memory or output style a host happens to carry.
            "--system-prompt",
            REVIEWER_SYSTEM_PROMPT,
        ]
        if self.model is not None:
            command.extend(["--model", self.model])
        invalid: str = ""
        for attempt in range(1, self.attempts + 1):
            try:
                completed = self.runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ReviewerProcessError(f"isolated reviewer {reviewer_id} could not run: {exc}") from exc
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
                raise ReviewerProcessError(f"isolated reviewer {reviewer_id} failed: {detail}")
            try:
                verdict = ReviewerVerdict.model_validate_json(unwrap_json_object(completed.stdout))
            except ValueError as exc:
                # Retry only this failure. A reply that does not satisfy the
                # verdict contract is the intermittent case measured above; a
                # process that could not run, or exited non-zero, is not
                # intermittent and still fails closed on the first attempt.
                invalid = str(exc)
                logger.warning(
                    "reviewer_reply_invalid",
                    reviewer_id=reviewer_id,
                    attempt=attempt,
                    attempts=self.attempts,
                    reply=completed.stdout.strip()[:200],
                )
                continue
            grounded = enforce_grounding(verdict, behavior.turn_text)
            if grounded is not verdict:
                logger.warning(
                    "reviewer_verdict_ungrounded",
                    reviewer_id=reviewer_id,
                    ungrounded_citations=list(ungrounded_citations(verdict.findings, behavior.turn_text)),
                )
            return grounded
        raise ReviewerProcessError(
            f"isolated reviewer {reviewer_id} returned invalid JSON on all {self.attempts} attempts: {invalid}"
        )


def _validate_bound_review(
    review: ReviewerVerdict,
    *,
    reviewer_id: str,
    goal: ReviewContract,
    behavior: WatchedSessionBehavior,
) -> None:
    if review.reviewer_id != reviewer_id:
        raise GoalReviewError(f"isolated reviewer {reviewer_id} changed its assigned identity")
    if review.goal_contract_id != goal.contract_id:
        raise GoalReviewError(f"isolated reviewer {reviewer_id} returned a stale or tampered goal binding")
    if review.behavior_sha256 != behavior.behavior_sha256:
        raise GoalReviewError(f"isolated reviewer {reviewer_id} returned a stale or tampered behavior binding")


def _signal(
    *,
    goal: FrozenGoal,
    behavior: WatchedSessionBehavior,
    reviews: Sequence[ReviewerVerdict],
    restarted_after_redirect: bool,
) -> SessionReviewSignal:
    findings = tuple(finding for review in reviews for finding in review.findings)
    verdict: Literal["accept", "reject"] = "accept" if all(review.verdict == "accept" for review in reviews) else "reject"
    return SessionReviewSignal.create(
        session_ref=behavior.session_ref,
        goal_contract_id=goal.contract_id,
        behavior_sha256=behavior.behavior_sha256,
        verdict=verdict,
        reviewer_ids=[review.reviewer_id for review in reviews],
        findings=findings,
        restarted_after_redirect=restarted_after_redirect,
    )


def review_log_path(root: Path) -> Path:
    return root / REVIEW_LOG_NAME


def append_review_signal(path: Path, signal: SessionReviewSignal) -> None:
    """Append one internal review signal, deduplicated by content id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        if json.loads(line).get("signal_id") == signal.signal_id:
                            return
                    except (ValueError, AttributeError):
                        continue
            with path.open("a", encoding="utf-8") as output:
                output.write(signal.model_dump_json() + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_latest_review_signal(path: Path, session_ref: str) -> SessionReviewSignal | None:
    """Return the newest valid internal signal for a session."""
    if not path.exists():
        return None
    latest: SessionReviewSignal | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            candidate = SessionReviewSignal.model_validate_json(line)
        except ValueError:
            continue
        if candidate.session_ref == session_ref:
            latest = candidate
    return latest


def review_watched_session(
    root: Path,
    session_ref: str,
    behavior: WatchedSessionBehavior,
    *,
    reviewer: BehaviorReviewer,
    reviewer_count: int = DEFAULT_REVIEWERS,
    max_redirect_restarts: int = 3,
    log_path: Path | None = None,
) -> SessionReviewSignal:
    """Run a unanimous isolated round, restarting on a frozen-goal redirect.

    The initial round uses two processes by default and permits an operator-
    configured single-reviewer round. After any detected redirect, the
    discarded round is logged and the fresh round uses exactly one process,
    per the 4B-mod policy.
    """
    if reviewer_count < MIN_REVIEWERS:
        raise ValueError(f"reviewer_count must be at least {MIN_REVIEWERS}")
    if behavior.session_ref != session_ref:
        raise GoalReviewError("behavior session_ref does not match the reviewed session")
    restarted = False
    restarts = 0
    round_size = reviewer_count
    while True:
        record = get_goal(root, session_ref)
        if record is None:
            raise GoalNotFoundError(session_ref)
        goal = freeze_goal(record)
        reviews: list[ReviewerVerdict] = []
        redirected = False
        for index in range(round_size):
            reviewer_id = f"reviewer-{restarts + 1}-{index + 1}"
            review = reviewer.review(goal, behavior, reviewer_id)
            _validate_bound_review(review, reviewer_id=reviewer_id, goal=goal, behavior=behavior)
            reviews.append(review)
            current_record = get_goal(root, session_ref)
            if current_record is None:
                raise GoalNotFoundError(session_ref)
            current_goal = freeze_goal(current_record)
            if current_goal.contract_id != goal.contract_id:
                record_review_restart(
                    root,
                    session_ref,
                    previous_contract_id=goal.contract_id,
                    restarted_contract_id=current_goal.contract_id,
                    behavior_sha256=behavior.behavior_sha256,
                )
                redirected = True
                restarted = True
                restarts += 1
                round_size = 1
                break
        if redirected:
            if restarts > max_redirect_restarts:
                raise GoalReviewError("goal kept redirecting during review; restart limit exceeded")
            continue
        signal = _signal(goal=goal, behavior=behavior, reviews=reviews, restarted_after_redirect=restarted)
        append_review_signal(log_path or review_log_path(root), signal)
        return signal

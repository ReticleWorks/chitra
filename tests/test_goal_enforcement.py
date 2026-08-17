from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chitra.goal_enforcement import (
    REVIEWER_SYSTEM_PROMPT,
    ClaudeProcessReviewer,
    FrozenGoal,
    GoalReviewError,
    ReviewerProcessError,
    ReviewerVerdict,
    ReviewFinding,
    WatchedSessionBehavior,
    freeze_goal,
    review_watched_session,
    unwrap_json_object,
)
from chitra.goals import GoalRecord, get_goal, redirect_goal, upsert_goal


def _request_from_prompt(prompt: str) -> dict[str, object]:
    """Read the payload back exactly the way the deployed wrapper reads it.

    `chitra_adapter/bin/chitra-watchd-reviewer` recovers the payload with
    `prompt.rsplit("\\nINPUT=", 1)[1]` and parses that as JSON. Every test here
    parses it the same way on purpose. When the tests used their own marker
    instead, the prompt drifted away from the wrapper and no test noticed.
    """
    marker = "\nINPUT="
    assert marker in prompt
    request = json.loads(prompt.rsplit(marker, 1)[1])
    assert isinstance(request, dict)
    return request


def _goal(root: Path) -> GoalRecord:
    return upsert_goal(
        root,
        GoalRecord(
            session_ref="localhost:lane:0.0",
            intent="Deliver the requested implementation without redirecting the operator strategy.",
            goal="Build and verify the requested forced completion gate.",
            done_when="Every required local validation passes with cited output.",
            scope="WS1 source tests and documentation only.",
            source="task-file:/tmp/ws1.md",
            status="working",
        ),
    )


class AcceptingReviewer:
    def __init__(self, *, root: Path | None = None, redirect: bool = False) -> None:
        self.calls: list[str] = []
        self.root = root
        self.redirect = redirect

    def review(self, goal, behavior, reviewer_id: str) -> ReviewerVerdict:
        self.calls.append(reviewer_id)
        if self.redirect and len(self.calls) == 1:
            assert self.root is not None
            redirect_goal(
                self.root,
                behavior.session_ref,
                reason="operator corrected the bounded delivery target",
                goal="Build and verify the corrected forced completion gate.",
            )
        return ReviewerVerdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,
            behavior_sha256=behavior.behavior_sha256,
            verdict="accept",
        )


def test_initial_round_requires_unanimous_isolated_acceptance(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "The gate code is complete and blocks drift with cited proof.")
    reviewer = AcceptingReviewer()

    signal = review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=reviewer)

    assert signal.verdict == "accept"
    assert reviewer.calls == ["reviewer-1-1", "reviewer-1-2"]
    assert signal.reviewer_ids == tuple(reviewer.calls)
    assert (tmp_path / "goal_reviews.jsonl").exists()


def test_frozen_goal_uses_redirect_refreshed_enrollment_condition(tmp_path: Path) -> None:
    enrolled = _goal(tmp_path)
    redirected = redirect_goal(
        tmp_path,
        enrolled.session_ref,
        reason="operator proposed a smaller validation target",
        done_when="The focused local validation passes with cited output.",
    )

    frozen = freeze_goal(redirected)

    assert frozen.done_when == redirected.done_when
    assert frozen.done_when != enrolled.done_when


def test_initial_round_can_be_configured_to_one_reviewer(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "Done with cited completion evidence.")
    reviewer = AcceptingReviewer()

    signal = review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=reviewer, reviewer_count=1)

    assert signal.verdict == "accept"
    assert reviewer.calls == ["reviewer-1-1"]
    assert signal.reviewer_ids == ("reviewer-1-1",)


def test_any_rejection_blocks_unanimous_release(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "Done, but the requested live probe was not run.")

    class MixedReviewer(AcceptingReviewer):
        def review(self, frozen, watched, reviewer_id: str) -> ReviewerVerdict:
            if reviewer_id.endswith("2"):
                return ReviewerVerdict(
                    reviewer_id=reviewer_id,
                    goal_contract_id=frozen.contract_id,
                    behavior_sha256=watched.behavior_sha256,
                    verdict="reject",
                    findings=(
                        ReviewFinding(
                            code="hedged_completion",
                            detail="Completion lacks the required live proof.",
                            citation="the requested live probe was not run",
                        ),
                    ),
                )
            return super().review(frozen, watched, reviewer_id)

    signal = review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=MixedReviewer())
    assert signal.verdict == "reject"
    assert signal.findings[0].code == "hedged_completion"


def test_redirect_restarts_automatically_with_exactly_one_reviewer_and_logs_history(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "The lane asks whether it may change the release strategy.")
    reviewer = AcceptingReviewer(root=tmp_path, redirect=True)

    signal = review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=reviewer)

    assert reviewer.calls == ["reviewer-1-1", "reviewer-2-1"]
    assert signal.restarted_after_redirect is True
    assert signal.reviewer_ids == ("reviewer-2-1",)
    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.goal_history[-1]["event"] == "adversarial-review-redirect-restart"
    assert "one reviewer" in stored.goal_history[-1]["reason"]


def test_tampered_reviewer_binding_fails_closed(tmp_path: Path) -> None:
    goal = _goal(tmp_path)
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "A normal bounded technical question.")

    class TamperedReviewer:
        def review(self, frozen, watched, reviewer_id: str) -> ReviewerVerdict:
            return ReviewerVerdict(
                reviewer_id=reviewer_id,
                goal_contract_id="sha256:" + "0" * 64,
                behavior_sha256=watched.behavior_sha256,
                verdict="accept",
            )

    with pytest.raises(GoalReviewError, match="tampered goal binding"):
        review_watched_session(tmp_path, goal.session_ref, behavior, reviewer=TamperedReviewer())


@pytest.mark.parametrize(
    ("packaged", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('Here is the verdict:\n{"a": 1}\n', '{"a": 1}'),
        ("not json at all", "not json at all"),
    ],
)
def test_reviewer_reply_packaging_is_removed_before_the_verdict_is_read(packaged: str, expected: str) -> None:
    assert unwrap_json_object(packaged) == expected


def test_a_fenced_reviewer_verdict_is_accepted(tmp_path: Path) -> None:
    """A correct verdict must not be lost to a code fence.

    A lost verdict is not harmless: watchd turns an unavailable review into a
    blocked status and an ask to review the session by hand, so a correct review
    of healthy work became a false blocker.
    """
    goal = freeze_goal(_goal(tmp_path))
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "Continuing against the recorded goal.")

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        request = _request_from_prompt(command[2])
        verdict = ReviewerVerdict(
            reviewer_id=request["reviewer_id"],
            goal_contract_id=request["frozen_goal"]["contract_id"],
            behavior_sha256=request["watched_session_behavior"]["behavior_sha256"],
            verdict="accept",
        ).model_dump_json()
        return subprocess.CompletedProcess(command, 0, f"```json\n{verdict}\n```", "")

    assert ClaudeProcessReviewer(runner=runner).review(goal, behavior, "reviewer-a").verdict == "accept"


def test_unwrapping_never_rescues_a_verdict_that_breaks_its_contract(tmp_path: Path) -> None:
    """Tolerating packaging must not tolerate a wrong verdict."""
    goal = freeze_goal(_goal(tmp_path))
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "Continuing against the recorded goal.")

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, '```json\n{"reviewer_id": "reviewer-a", "verdict": "accept"}\n```', "")

    with pytest.raises(ReviewerProcessError, match="invalid JSON"):
        ClaudeProcessReviewer(runner=runner).review(goal, behavior, "reviewer-a")


def test_the_reviewer_process_is_granted_no_tools_and_no_memory(tmp_path: Path) -> None:
    goal = freeze_goal(_goal(tmp_path))
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "Continuing against the recorded goal.")
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        request = _request_from_prompt(command[2])
        output = ReviewerVerdict(
            reviewer_id=request["reviewer_id"],
            goal_contract_id=request["frozen_goal"]["contract_id"],
            behavior_sha256=request["watched_session_behavior"]["behavior_sha256"],
            verdict="accept",
        ).model_dump_json()
        return subprocess.CompletedProcess(command, 0, output, "")

    ClaudeProcessReviewer(runner=runner).review(goal, behavior, "reviewer-a")
    command = commands[0]
    assert "--no-session-persistence" in command
    assert command[command.index("--allowed-tools") + 1] == ""


def _verdict_json(command: list[str]) -> str:
    request = _request_from_prompt(command[2])
    return ReviewerVerdict(
        reviewer_id=request["reviewer_id"],
        goal_contract_id=request["frozen_goal"]["contract_id"],
        behavior_sha256=request["watched_session_behavior"]["behavior_sha256"],
        verdict="accept",
    ).model_dump_json()


def _behavior(goal: FrozenGoal) -> WatchedSessionBehavior:
    return WatchedSessionBehavior.from_turn(goal.session_ref, "Continuing against the recorded goal.")


def test_the_reviewer_replaces_the_host_system_prompt(tmp_path: Path) -> None:
    """An operator-installed output style must not change the shape of a verdict.

    Measured on tophand: with the ambient system prompt in place the reviewer
    answered in narrated prose, which fails validation and becomes a false
    blocker. Replacing the system prompt is what stops that.
    """
    goal = freeze_goal(_goal(tmp_path))
    captured: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, _verdict_json(command), "")

    ClaudeProcessReviewer(runner=runner).review(goal, _behavior(goal), "reviewer-a")
    command = captured[0]
    assert command[command.index("--system-prompt") + 1] == REVIEWER_SYSTEM_PROMPT


def test_prompt_payload_matches_the_deployed_wrapper_contract(tmp_path: Path) -> None:
    """The prompt must stay readable by the wrapper that runs the reviewer.

    In the fleet the reviewer is not run directly. `chitra-watchd-reviewer`
    runs it, and that wrapper recovers the reviewer id and the two content
    bindings by splitting the prompt on "\\nINPUT=" and parsing the rest as
    JSON. The prompt once wrapped the payload in tags instead, so the wrapper
    refused every review with "reviewer prompt does not contain INPUT" before
    any model was called. The only production review record the fleet has ever
    written carries exactly that error, and it marked a clean completion as
    blocked. No test held the prompt to the reader's shape, so nothing caught
    it. This test is that hold.
    """
    goal = freeze_goal(_goal(tmp_path))
    behavior = _behavior(goal)
    captured: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, _verdict_json(command), "")

    ClaudeProcessReviewer(runner=runner).review(goal, behavior, "reviewer-a")
    prompt = captured[0][2]

    marker = "\nINPUT="
    assert marker in prompt, "the wrapper fails outright when the marker is absent"
    payload = prompt.rsplit(marker, 1)[1]
    request = json.loads(payload)
    assert payload == payload.strip(), "nothing may follow the payload"
    assert request["reviewer_id"] == "reviewer-a"
    assert request["frozen_goal"]["contract_id"] == goal.contract_id
    assert request["watched_session_behavior"]["behavior_sha256"] == behavior.behavior_sha256


def test_an_unusable_reply_is_retried_because_the_failure_is_intermittent(tmp_path: Path) -> None:
    """Measured three valid replies in seven attempts on identical input."""
    goal = freeze_goal(_goal(tmp_path))
    calls: list[int] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        if len(calls) < 3:
            return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")
        return subprocess.CompletedProcess(command, 0, _verdict_json(command), "")

    assert ClaudeProcessReviewer(runner=runner).review(goal, _behavior(goal), "reviewer-a").verdict == "accept"
    assert len(calls) == 3


def test_an_unusable_reply_still_fails_closed_once_the_attempts_run_out(tmp_path: Path) -> None:
    goal = freeze_goal(_goal(tmp_path))
    calls: list[int] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")

    with pytest.raises(ReviewerProcessError, match="all 3 attempts"):
        ClaudeProcessReviewer(runner=runner).review(goal, _behavior(goal), "reviewer-a")
    assert len(calls) == 3


def test_a_process_that_exits_non_zero_is_not_retried(tmp_path: Path) -> None:
    """Only the intermittent failure is retried; a broken process fails at once."""
    goal = freeze_goal(_goal(tmp_path))
    calls: list[int] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(command, 1, "", "the model was unavailable")

    with pytest.raises(ReviewerProcessError, match="the model was unavailable"):
        ClaudeProcessReviewer(runner=runner).review(goal, _behavior(goal), "reviewer-a")
    assert len(calls) == 1


def test_a_non_positive_attempt_count_is_refused() -> None:
    with pytest.raises(ValueError, match="attempts must be a positive integer"):
        ClaudeProcessReviewer(attempts=0)


def test_claude_reviewer_uses_a_fresh_process_and_only_watched_behavior_context(tmp_path: Path) -> None:
    goal = freeze_goal(_goal(tmp_path))
    behavior = WatchedSessionBehavior.from_turn(goal.session_ref, "Can I redirect this work to an unrelated deploy?")
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        prompt = command[2]
        request = _request_from_prompt(prompt)
        output = ReviewerVerdict(
            reviewer_id=request["reviewer_id"],
            goal_contract_id=request["frozen_goal"]["contract_id"],
            behavior_sha256=request["watched_session_behavior"]["behavior_sha256"],
            verdict="accept",
        ).model_dump_json()
        return subprocess.CompletedProcess(command, 0, output, "")

    reviewer = ClaudeProcessReviewer(runner=runner)
    reviewer.review(goal, behavior, "reviewer-a")
    reviewer.review(goal, behavior, "reviewer-b")

    assert len(commands) == 2
    assert all(command[:2] == ["claude", "-p"] for command in commands)
    assert commands[0] is not commands[1]
    assert all("watched_session_behavior" in command[2] for command in commands)
    assert all("Chitra draft response" in command[2] and "approved_text" not in command[2] for command in commands)
    # The prompt must enumerate the exact FindingCode literals so the reviewer
    # model does not invent an out-of-enum code (e.g. "COMPLETION_WITHOUT_PROOF")
    # that fails ReviewerVerdict validation and forces a fail-closed verdict.
    for code in ("goal_drift", "smuggled_redirect", "hedged_completion", "unsupported_completion", "other"):
        assert all(code in command[2] for command in commands)

"""G3 replace-then-remove: convlog suppression/evidence gates, the artifact
ledger gates, and routing-feedback blocking.

Every deny path is driven through the real process boundary: ``chitra-convo``
reading a brief JSON file over a real goals store and real evidence files
(delivery ledger + self transcript), ``chitra-artifacts`` over a real
artifacts.json, and the routing-feedback usage tool run as a script over a
real ledger and routing.yaml. No chitra module is faked.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml
from _g3_status_boundary import run_cli, script
from _goal_fixtures import enrollment_fields

from chitra.autonomy import AutonomyPolicy, CapabilityGrant
from chitra.goals import GoalRecord, get_goal, upsert_goal

SESSION_REF = "host-b:feeds:0.0"
ROUTING_TOOL = Path(__file__).resolve().parents[1] / "tools" / "routing_feedback" / "routing_feedback_usage.py"

# ---------------------------------------------------------------------------
# Shared fixtures mirroring the held unit tests' shapes.
# ---------------------------------------------------------------------------


def _attempt(action: str, result: str, evidence: object) -> dict[str, object]:
    return {"action": action, "result": result, "evidence": evidence}


def _write_ledger(path: Path, *order_ids: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"order_id": order_id, "session_ref": SESSION_REF, "tag": "follow-up", "sent_at": "2026-08-22T09:00:00+00:00"}
        for order_id in order_ids
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_self_transcript(path: Path, *, command: str, exit_code: int, tool_use_id: str = "tu_capture_1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": command}},
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": f"pane text\nexit code: {exit_code}"},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_ref": SESSION_REF,
        "program": "Feeds digest redesign (F2)",
        "subject": "Feeds digest compiler",
        "progress": "implementation-ready; final interface choice pending",
        "stage": "The implementation is ready for the final interface choice.",
        "category": "decision",
        "decision": "Should the digest ship as one combined feed?",
        "recommendation": "Ship one combined feed because the tested readers preferred it.",
        "recommendation_basis": "research",
        "options": [
            {"label": "Combined feed", "consequence": "Readers get one ranked digest."},
            {"label": "Separate feeds", "consequence": "Readers choose a source first."},
        ],
        "exhaustion": {
            "reason": "attempts_exhausted",
            "attempts": [
                {
                    "action": "Shipped the combined-feed prototype to the reader test",
                    "result": "it passed the preference check.",
                    "evidence": {"kind": "command", "ref": "chitra-tmux-capture feeds-reader --pane harness-pi", "exit_status": 0},
                },
                {
                    "action": "Asked the work session to pick a default feed layout",
                    "result": "it deferred the product call.",
                    "evidence": {"kind": "order", "ref": "ord-feeds-layout-1"},
                },
            ],
            "residual_blocker": "Only the operator can pick between one combined feed and separate feeds.",
        },
        "source_quote": ["The combined prototype passed the reader test.", "I need the operator's product decision."],
        "source_ref": "transcripts/feeds.jsonl",
    }
    payload.update(changes)
    return payload


def _policy_goal(root: Path, *, policy: AutonomyPolicy | None = None) -> GoalRecord:
    goal = GoalRecord(
        session_ref=SESSION_REF,
        goal="Deliver the feed redesign with proof",
        done_when="The focused feed tests pass and the redesign artifact exists",
        source="operator:test",
        status="working",
        intent="Complete the feed redesign",
        scope="feed source and focused tests",
        autonomy_policy=policy or AutonomyPolicy(),
        **enrollment_fields("The focused feed tests pass and the redesign artifact exists"),
    )
    return upsert_goal(root, goal)


def _convo(tmp_path: Path, payload: dict, *, transcript: Path | None = None, session_ref: str = SESSION_REF):
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(payload), encoding="utf-8")
    convlog = tmp_path / "conversation.jsonl"
    argv = [
        script("chitra-convo"),
        "brief",
        "--convlog-path",
        str(convlog),
        "--session-ref",
        session_ref,
        "--json",
        str(brief_path),
        "--raw",
        "raw",
    ]
    if transcript is not None:
        argv += ["--self-transcript", str(transcript)]
    result = run_cli(argv, env_extra={"CHITRA_STATE_DIR": str(tmp_path / "state")})
    return result, convlog


def _evidence_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-tmux-capture feeds-reader --pane harness-pi", exit_code=0)
    return state


# ---------------------------------------------------------------------------
# D31/D32: the credential-brief suppression route -- through the real CLI over
# a real goals store, real ledger, and real self transcript.
# ---------------------------------------------------------------------------


def _credential_payload() -> dict[str, object]:
    return _payload(
        exhaustion={
            "reason": "credential",
            "attempts": [
                _attempt(
                    "Presented the retry token to the registry",
                    "the registry refused it outright.",
                    {"kind": "verb_refusal", "ref": "chitra-registry-push"},
                )
            ],
            "residual_blocker": "Only a human account can approve this access today.",
        }
    )


def test_d31_granted_credential_brief_is_suppressed_for_an_enrolled_goal(tmp_path: Path) -> None:
    _policy_goal(
        tmp_path,
        policy=AutonomyPolicy(
            grants=(CapabilityGrant(grant_id="feed-credential", capability="credential_use"),),
        ),
    )
    transcript = tmp_path / "self-transcript.jsonl"
    _write_self_transcript(transcript, command="chitra-registry-push", exit_code=1)

    result, convlog = _convo(tmp_path, _credential_payload(), transcript=transcript)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "operator brief suppressed" in result.stderr
    assert not convlog.exists()
    stored = get_goal(tmp_path, SESSION_REF)
    assert stored is not None and stored.foreground_tasks == ()


def test_d31_verified_denial_without_a_goal_keeps_the_operator_brief(tmp_path: Path) -> None:
    transcript = tmp_path / "self-transcript.jsonl"
    _write_self_transcript(transcript, command="chitra-registry-push", exit_code=1)

    result, convlog = _convo(tmp_path, _credential_payload(), transcript=transcript)

    assert result.returncode == 0, result.stderr
    assert "operator brief suppressed" not in result.stderr
    entries = [json.loads(line) for line in convlog.read_text().splitlines()]
    assert [entry["kind"] for entry in entries] == ["session_msg", "operator_brief"]


def test_d32_unverified_irreversible_brief_without_a_goal_stays_foreground(tmp_path: Path) -> None:
    payload = _payload(
        exhaustion={
            "reason": "irreversible_consent",
            "attempts": [],
            "residual_blocker": "Sending the announcement cannot be undone once it leaves the outbox.",
        }
    )
    result, convlog = _convo(tmp_path, payload)

    assert result.returncode == 0, result.stderr
    assert "operator brief suppressed" in result.stderr
    assert not convlog.exists()


# ---------------------------------------------------------------------------
# D33: brief structural validation on the write path -- every refusal exits
# nonzero through the CLI and writes nothing.
# ---------------------------------------------------------------------------

_BASE_EXHAUSTION = {
    "reason": "attempts_exhausted",
    "attempts": [
        _attempt("Ran the deploy script once more", "it hit the held lock.", {"kind": "order", "ref": "ord-x"}),
        _attempt("Asked the work session for a ruling", "it punted back to us.", {"kind": "order", "ref": "ord-y"}),
    ],
    "residual_blocker": "The held lock still blocks every retry.",
}


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"program": "F2"}, "plain-language program"),
        ({"program": "host-b:feeds:0.0"}, "plain-language program"),
        ({"decision": None}, "category is decision"),
        ({"recommendation": ""}, "monitor does the research first"),
        ({"exhaustion": None}, "exhaustion record"),
        (
            {
                "exhaustion": {
                    "reason": "attempts_exhausted",
                    "attempts": [{"action": "Ran the reprovision command by hand again", "result": "blocked by the held lock."}],
                    "residual_blocker": "The held lock still blocks every retry.",
                }
            },
            "at least 2 distinct attempts",
        ),
        (
            {
                "exhaustion": {
                    "reason": "attempts_exhausted",
                    "attempts": [
                        _attempt("ran the reprovision command", "blocked by the held lock", {"kind": "order", "ref": "ord-a"}),
                        _attempt("ran the reprovision command", "blocked by the held lock", {"kind": "order", "ref": "ord-b"}),
                        _attempt("Asked the work session for a ruling", "it punted back to us.", {"kind": "order", "ref": "ord-c"}),
                    ],
                    "residual_blocker": "The held lock still blocks every retry.",
                }
            },
            "distinct",
        ),
        (
            {
                "exhaustion": {
                    "reason": "attempts_exhausted",
                    "attempts": [
                        _attempt("Ran the reprovision command", "blocked by the held lock", {"kind": "order", "ref": "ord-a"}),
                        _attempt("  ran   THE reprovision command ", "blocked by the held lock", {"kind": "order", "ref": "ord-b"}),
                        _attempt("Asked the work session for a ruling", "it punted back to us.", {"kind": "order", "ref": "ord-c"}),
                    ],
                    "residual_blocker": "The held lock still blocks every retry.",
                }
            },
            "distinct",
        ),
        (
            {
                "exhaustion": {
                    "reason": "attempts_exhausted",
                    "attempts": [
                        _attempt("Ran the deploy script once more", "it hit the held lock.", {"kind": "order", "ref": "ord-a"}),
                        {"action": "Asked the work session for a ruling", "result": "it punted back to us."},
                    ],
                    "residual_blocker": "The held lock still blocks every retry.",
                }
            },
            "evidence handle missing",
        ),
        (
            {
                "exhaustion": {
                    "reason": "attempts_exhausted",
                    "attempts": [
                        {
                            "action": "   ",
                            "result": "the pipeline refused the token outright.",
                            "evidence": {"kind": "order", "ref": "ord-a"},
                        },
                        _attempt("Shipped the fixed build to staging overnight", "it deployed cleanly.", {"kind": "order", "ref": "ord-b"}),
                    ],
                    "residual_blocker": "The held lock still blocks every retry.",
                }
            },
            "non-empty",
        ),
        (
            {
                "exhaustion": {
                    "reason": "attempts_exhausted",
                    "attempts": [
                        "Ran the deploy script once more\nit hit the held lock.",
                        _attempt("Shipped the fixed build to staging overnight", "it deployed cleanly.", {"kind": "order", "ref": "ord-b"}),
                    ],
                    "residual_blocker": "The held lock still blocks every retry.",
                }
            },
            "one line",
        ),
        (
            {
                "exhaustion": {
                    "reason": "attempts_exhausted",
                    "attempts": _BASE_EXHAUSTION["attempts"],
                    "residual_blocker": "Only the operator can pick between the two feeds.\rOr can they?",
                }
            },
            "one line",
        ),
        (
            {
                "exhaustion": {
                    "reason": "credential",
                    "attempts": [],
                    "residual_blocker": "The credential gate needs a human.",
                }
            },
            "requires at least 1 attempt",
        ),
        (
            {
                "recommendation_basis": "research",
                "exhaustion": {
                    "reason": "operator_decision",
                    "attempts": [],
                    "residual_blocker": "Picking the vendor name is a pure preference call nobody else can make.",
                },
            },
            '"operator-preference"',
        ),
        (
            {
                "exhaustion": {
                    "reason": "irreversible_consent",
                    "attempts": [],
                    "residual_blocker": "Cannot undo.",
                }
            },
            "at least 20 characters",
        ),
        ({"source_quote": []}, "source_quote"),
        ({"source_quote": ["x" * 401]}, "source_quote"),
    ],
    ids=[
        "bare-codename",
        "session-ref-program",
        "decision-category",
        "recommendation-required",
        "exhaustion-required",
        "single-attempt",
        "duplicate-attempts",
        "normalized-duplicate-attempts",
        "missing-evidence-handle",
        "blank-attempt-field",
        "newline-in-attempt",
        "newline-in-blocker",
        "credential-needs-attempt",
        "operator-decision-basis",
        "short-irreversible-blocker",
        "no-source-quotes",
        "oversized-source-quote",
    ],
)
def test_d33_structural_brief_refusals_write_nothing(
    tmp_path: Path, mutation: dict[str, object], expected: str
) -> None:
    state = _evidence_state(tmp_path)
    result, convlog = _convo(tmp_path, _payload(**mutation), transcript=state / "self-transcript.jsonl")

    assert result.returncode == 1, result.stdout + result.stderr
    assert expected in result.stderr
    assert result.stderr.startswith("chitra-convo:")
    assert not convlog.exists()


def _v2_entry(brief_payload: dict[str, object], thread_id: str) -> dict[str, object]:
    return {
        "schema": "chitra.convlog.v2",
        "thread_id": thread_id,
        "seq": 1,
        "kind": "operator_brief",
        "at": "2026-07-11T12:00:00+00:00",
        "session_ref": brief_payload["session_ref"],
        "payload": {"brief": brief_payload, "rendered": "stored rendering"},
    }


def test_legacy_convlog_records_still_load_and_list(tmp_path: Path) -> None:
    """Pre-WP4 and v1 stored records stay readable: the reader is lenient
    even though the write path has tightened since they were written."""
    convlog = tmp_path / "conversation.jsonl"

    v1_brief = _payload()
    v1_brief.pop("subject")
    v1_brief.pop("progress")
    v1_entry = {
        "schema": "chitra.convlog.v1",
        "thread_id": "legacy-v1",
        "seq": 1,
        "kind": "operator_brief",
        "at": "2026-07-11T12:00:00+00:00",
        "session_ref": SESSION_REF,
        "payload": {"brief": v1_brief, "rendered": "legacy rendered brief"},
    }

    pre_wp4 = _payload()
    del pre_wp4["exhaustion"]

    tightened = _payload(
        exhaustion={"reason": "credential", "attempts": [], "residual_blocker": "Needs key"},
        progress="Waiting on the vendor contract; you must sign the amendment today.",
    )

    convlog.write_text(
        "\n".join(
            [
                json.dumps(v1_entry),
                json.dumps(_v2_entry(pre_wp4, thread_id="pre-wp4")),
                "this line is not json at all",
                json.dumps(_v2_entry(tightened, thread_id="tightened")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_cli([script("chitra-convo"), "list", "--convlog-path", str(convlog)])

    assert result.returncode == 0, result.stderr
    assert "convlog_malformed_line" in result.stdout  # bad lines warn, never abort
    listed = [line.split("\t")[0] for line in result.stdout.splitlines() if "\t" in line]
    assert listed == ["legacy-v1", "pre-wp4", "tightened"]


def test_d33_session_ref_mismatch_is_refused(tmp_path: Path) -> None:
    state = _evidence_state(tmp_path)
    result, convlog = _convo(tmp_path, _payload(), transcript=state / "self-transcript.jsonl", session_ref="host-b:other:0.0")

    assert result.returncode == 1
    assert "--session-ref must match" in result.stderr
    assert not convlog.exists()


# ---------------------------------------------------------------------------
# D34: attempt-evidence rejection through the real FilesystemEvidenceResolver
# wired by the CLI (ledger under CHITRA_STATE_DIR + --self-transcript).
# ---------------------------------------------------------------------------


def test_d34_fabricated_order_and_capture_are_rejected_at_write_time(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-unrelated-1")
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-status --json", exit_code=0)
    payload = _payload(
        decision="Should I ask you to run the npm install on the builder host, or wait for the package pin?",
        recommendation="Ask you for the install because the pin blocks every retry.",
        exhaustion={
            "reason": "attempts_exhausted",
            "attempts": [
                _attempt(
                    "Ran chitra-tmux-capture harness-pi over the grant",
                    "the pane shows the dependency prompt.",
                    {"kind": "command", "ref": "chitra-tmux-capture harness-pi", "exit_status": 0},
                ),
                _attempt(
                    "Queued follow-up message to harness-pi",
                    "no new turn in its transcript after ten minutes.",
                    {"kind": "order", "ref": "9f2c"},
                ),
            ],
            "residual_blocker": "The install needs a shell on the builder host that the grant does not expose.",
        },
    )

    result, convlog = _convo(tmp_path, payload, transcript=state / "self-transcript.jsonl")

    assert result.returncode == 1
    assert "attempt 1: no Bash tool use mentioning 'chitra-tmux-capture harness-pi'" in result.stderr
    assert not convlog.exists()


def test_d34_real_order_and_capture_pass_and_write_the_thread(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-npm-followup-1")
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-tmux-capture harness-pi", exit_code=0)
    payload = _payload(
        decision="Should I ask you to run the npm install on the builder host, or wait for the package pin?",
        recommendation="Ask you for the install because the pin blocks every retry.",
        exhaustion={
            "reason": "attempts_exhausted",
            "attempts": [
                _attempt(
                    "Ran chitra-tmux-capture harness-pi over the grant",
                    "the pane shows the dependency prompt.",
                    {"kind": "command", "ref": "chitra-tmux-capture harness-pi", "exit_status": 0},
                ),
                _attempt(
                    "Queued follow-up message to harness-pi",
                    "no new turn in its transcript after ten minutes.",
                    {"kind": "order", "ref": "ord-npm-followup-1"},
                ),
            ],
            "residual_blocker": "The install needs a shell on the builder host that the grant does not expose.",
        },
    )

    result, convlog = _convo(tmp_path, payload, transcript=state / "self-transcript.jsonl")

    assert result.returncode == 0, result.stderr
    kinds = [json.loads(line)["kind"] for line in convlog.read_text().splitlines()]
    assert "operator_brief" in kinds


def test_d34_command_evidence_with_wrong_exit_status_is_rejected(tmp_path: Path) -> None:
    state = _evidence_state(tmp_path)
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-tmux-capture feeds-reader --pane harness-pi", exit_code=3)

    result, convlog = _convo(tmp_path, _payload(), transcript=state / "self-transcript.jsonl")

    assert result.returncode == 1
    assert "exited 3" in result.stderr
    assert not convlog.exists()


def test_d34_command_evidence_without_a_capture_in_the_transcript_is_rejected(tmp_path: Path) -> None:
    state = _evidence_state(tmp_path)
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-status --json", exit_code=0)

    result, convlog = _convo(tmp_path, _payload(), transcript=state / "self-transcript.jsonl")

    assert result.returncode == 1
    assert "no Bash tool use mentioning 'chitra-tmux-capture feeds-reader" in result.stderr
    assert not convlog.exists()


def test_d34_command_evidence_without_a_recorded_result_is_rejected(tmp_path: Path) -> None:
    state = _evidence_state(tmp_path)
    (state / "self-transcript.jsonl").write_text(
        json.dumps(
            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "chitra-tmux-capture feeds-reader --pane harness-pi"}}
        )
        + "\n",
        encoding="utf-8",
    )

    result, convlog = _convo(tmp_path, _payload(), transcript=state / "self-transcript.jsonl")

    assert result.returncode == 1
    assert "no recorded result in self transcript" in result.stderr
    assert not convlog.exists()

    # A tool_result recorded *before* its tool_use is not evidence either.
    (state / "self-transcript.jsonl").write_text(
        json.dumps(
            {
                "events": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "pane text\nexit code: 0"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Bash",
                        "input": {"command": "chitra-tmux-capture feeds-reader --pane harness-pi"},
                    },
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result, convlog = _convo(tmp_path, _payload(), transcript=state / "self-transcript.jsonl")

    assert result.returncode == 1
    assert "no recorded result in self transcript" in result.stderr
    assert not convlog.exists()


def test_d34_command_evidence_requires_a_self_transcript(tmp_path: Path) -> None:
    _evidence_state(tmp_path)

    result, convlog = _convo(tmp_path, _payload())

    assert result.returncode == 1
    assert "--self-transcript or set CHITRA_SELF_TRANSCRIPT" in result.stderr
    assert not convlog.exists()


def test_d34_self_transcript_env_var_is_honored(tmp_path: Path) -> None:
    state = _evidence_state(tmp_path)

    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(_payload()), encoding="utf-8")
    convlog = tmp_path / "conversation.jsonl"
    result = run_cli(
        [
            script("chitra-convo"),
            "brief",
            "--convlog-path",
            str(convlog),
            "--session-ref",
            SESSION_REF,
            "--json",
            str(brief_path),
            "--raw",
            "raw",
        ],
        env_extra={
            "CHITRA_STATE_DIR": str(state),
            "CHITRA_SELF_TRANSCRIPT": str(state / "self-transcript.jsonl"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert convlog.exists()


@pytest.mark.parametrize("exit_code", [0])
def test_d34_verb_refusal_requires_an_observed_nonzero_exit(tmp_path: Path, exit_code: int) -> None:
    state = _evidence_state(tmp_path)
    _write_self_transcript(
        state / "self-transcript.jsonl", command="chitra grant chitra-registry-push --token retry-1", exit_code=exit_code
    )

    result, convlog = _convo(tmp_path, _credential_payload(), transcript=state / "self-transcript.jsonl")

    assert result.returncode == 1
    assert "grant verb 'chitra-registry-push' did not fail in self transcript" in result.stderr
    assert not convlog.exists()


def test_d34_verb_refusal_requires_a_bash_tool_use(tmp_path: Path) -> None:
    state = _evidence_state(tmp_path)
    events = [
        {
            "type": "tool_use",
            "id": "tu_note_1",
            "name": "TodoWrite",
            "input": {"todos": ["retry chitra-registry-push with token retry-1"]},
        },
        {"type": "tool_result", "tool_use_id": "tu_note_1", "content": "todos updated\nexit code: 1"},
    ]
    (state / "self-transcript.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    result, convlog = _convo(tmp_path, _credential_payload(), transcript=state / "self-transcript.jsonl")

    assert result.returncode == 1
    assert "no Bash tool use mentioning 'chitra-registry-push'" in result.stderr
    assert not convlog.exists()


def _transcript_attempt_payload(tmp_path: Path, transcript: Path, digest: str) -> dict[str, object]:
    payload = _payload(
        exhaustion={
            "reason": "attempts_exhausted",
            "attempts": [
                _attempt(
                    "Read the lane session transcript",
                    "it deferred the product call.",
                    {"kind": "transcript", "ref": str(transcript), "sha256": digest},
                ),
                _attempt(
                    "Shipped the combined-feed prototype",
                    "it passed the preference check.",
                    {"kind": "order", "ref": "ord-feeds-layout-1"},
                ),
            ],
            "residual_blocker": "Only the operator can pick.",
        }
    )
    return payload


def test_d34_transcript_evidence_checks_line_digest(tmp_path: Path) -> None:
    state = _evidence_state(tmp_path)
    quoted = "the lane answered: the feed layout is the operator's product call."
    transcript = tmp_path / "lane-session.jsonl"
    transcript.write_text(quoted + "\n", encoding="utf-8")
    digest = hashlib.sha256(quoted.encode("utf-8")).hexdigest()

    payload = _transcript_attempt_payload(tmp_path, transcript, digest)
    result, convlog = _convo(tmp_path, payload, transcript=state / "self-transcript.jsonl")
    assert result.returncode == 0, result.stderr
    assert convlog.exists()


def test_d34_transcript_evidence_rejects_wrong_digest_and_missing_file(tmp_path: Path) -> None:
    state = _evidence_state(tmp_path)
    transcript = tmp_path / "lane-session.jsonl"
    transcript.write_text("the lane deferred the product call.\n", encoding="utf-8")

    payload = _transcript_attempt_payload(tmp_path, transcript, "0" * 64)
    result, convlog = _convo(tmp_path, payload, transcript=state / "self-transcript.jsonl")
    assert result.returncode == 1
    assert f"no line in {transcript} hashes to" in result.stderr

    payload = _transcript_attempt_payload(tmp_path, tmp_path / "missing.jsonl", "0" * 64)
    result, _ = _convo(tmp_path, payload, transcript=state / "self-transcript.jsonl")
    assert result.returncode == 1
    assert "transcript not found" in result.stderr


def test_d34_transcript_evidence_requires_absolute_path_and_real_sha(tmp_path: Path) -> None:
    state = _evidence_state(tmp_path)
    payload = _payload(
        exhaustion={
            "reason": "attempts_exhausted",
            "attempts": [
                _attempt(
                    "Read the lane session transcript",
                    "it deferred the product call.",
                    {"kind": "transcript", "ref": "lane-session.jsonl", "sha256": "0" * 64},
                ),
                _attempt(
                    "Shipped the combined-feed prototype",
                    "it passed the preference check.",
                    {"kind": "order", "ref": "ord-feeds-layout-1"},
                ),
            ],
            "residual_blocker": "Only the operator can pick.",
        }
    )
    result, _ = _convo(tmp_path, payload, transcript=state / "self-transcript.jsonl")
    assert result.returncode == 1
    assert "absolute" in result.stderr

    payload["exhaustion"]["attempts"][0]["evidence"]["ref"] = str(tmp_path / "lane-session.jsonl")  # type: ignore[index]
    payload["exhaustion"]["attempts"][0]["evidence"]["sha256"] = "deadbeef"  # type: ignore[index]
    result, _ = _convo(tmp_path, payload, transcript=state / "self-transcript.jsonl")
    assert result.returncode == 1
    assert "sha256" in result.stderr


# ---------------------------------------------------------------------------
# D35..D38: the artifact ledger gates through `chitra-artifacts`.
# ---------------------------------------------------------------------------

ARTIFACT_PREFIX = "https://claude.ai/code/artifact/"
VALID_BRIEF = (
    "What was built: A durable operator interview artifact.\n"
    "What it does: It records the reviewed operator findings for later use.\n"
    "Does it actually work: Render probe status=200 with 3 checks; /tmp/artifact-proof.json."
)


def _artifacts(tmp_path: Path, *argv: str):
    return run_cli([script("chitra-artifacts"), *argv, "--root", str(tmp_path)])


def test_d35_non_allowlisted_artifact_url_is_refused(tmp_path: Path) -> None:
    result = _artifacts(
        tmp_path,
        "record",
        "--url",
        "https://example.com/artifact/one",
        "--title",
        "Operator interview notes",
        "--kind",
        "interview",
        "--source",
        "host-b:/var/lib/chitra/artifact.html",
        "--brief",
        VALID_BRIEF,
    )
    assert result.returncode == 1
    assert "url must start" in result.stderr
    assert not (tmp_path / "artifacts.json").exists()


def test_d36_process_narration_brief_is_refused_and_recorded_nothing(tmp_path: Path) -> None:
    result = _artifacts(
        tmp_path,
        "record",
        "--url",
        f"{ARTIFACT_PREFIX}example-001",
        "--title",
        "Operator interview notes",
        "--kind",
        "interview",
        "--source",
        "host-b:/var/lib/chitra/artifact.html",
        "--brief",
        "What was built: I reviewed steps.\nWhat it does: I followed steps.\nDoes it actually work: I worked on steps.",
    )
    assert result.returncode == 1
    assert "process narration" in result.stderr
    assert not (tmp_path / "artifacts.json").exists()


def test_d37_mark_reviewed_rejects_invalid_response_and_missing_artifact(tmp_path: Path) -> None:
    url = f"{ARTIFACT_PREFIX}example-001"
    result = _artifacts(
        tmp_path,
        "record",
        "--url",
        url,
        "--title",
        "Operator interview notes",
        "--kind",
        "interview",
        "--source",
        "host-b:/var/lib/chitra/artifact.html",
        "--brief",
        VALID_BRIEF,
    )
    assert result.returncode == 0, result.stderr

    bad = _artifacts(tmp_path, "mark-reviewed", "--url", url, "--response", "not json")
    assert bad.returncode == 1
    assert "response must be valid JSON" in bad.stderr
    record = json.loads((tmp_path / "artifacts.json").read_text())["artifacts"][0]
    assert record["review_status"] == "unreviewed"

    missing = _artifacts(tmp_path, "mark-reviewed", "--url", f"{ARTIFACT_PREFIX}missing")
    assert missing.returncode == 1
    assert "artifact not found" in missing.stderr
    missing_get = _artifacts(tmp_path, "get", "--url", f"{ARTIFACT_PREFIX}missing")
    assert missing_get.returncode == 1
    assert "artifact not found" in missing_get.stderr


def test_upsert_resets_review_state_for_republished_artifact(tmp_path: Path) -> None:
    """A republished artifact must be re-reviewed: upsert resets the review
    state so an updated deliverable cannot ride on the old approval."""
    url = ARTIFACT_PREFIX + "republish-1"
    first = _artifacts(
        tmp_path,
        "record",
        "--url",
        url,
        "--title",
        "Operator interview notes",
        "--kind",
        "interview",
        "--source",
        "host-b:/var/lib/chitra/artifact.html",
        "--brief",
        VALID_BRIEF,
    )
    assert first.returncode == 0, first.stderr

    reviewed = _artifacts(tmp_path, "mark-reviewed", "--url", url, "--response", '{"status":"accepted"}')
    assert reviewed.returncode == 0, reviewed.stderr

    republished = _artifacts(
        tmp_path,
        "record",
        "--url",
        url,
        "--title",
        "Revised interview notes",
        "--kind",
        "interview",
        "--source",
        "host-b:/tmp/revised.html",
        "--brief",
        VALID_BRIEF,
    )
    assert republished.returncode == 0, republished.stderr
    stored = _artifacts(tmp_path, "get", "--url", url)
    assert stored.returncode == 0, stored.stderr
    rec = json.loads(stored.stdout[stored.stdout.index("{"):])
    assert rec["title"] == "Revised interview notes"
    assert rec["review_status"] == "unreviewed"
    assert rec["reviewed_at"] == ""
    assert rec["response"] == ""


def test_d38_bypassed_nonconforming_brief_is_flagged_and_surfaced(tmp_path: Path) -> None:
    """A record written straight to artifacts.json past the guarded CLI still
    loads, but the nonconforming command surfaces it for review."""
    payload = {
        "schema": "chitra.artifacts.v1",
        "updated_at": "2026-07-14T00:00:00+00:00",
        "artifacts": [
            {
                "url": f"{ARTIFACT_PREFIX}bypass-001",
                "title": "Directly-written record",
                "kind": "page",
                "source": "host-b:/tmp/direct.md",
                "brief": "",
                "published_at": "2026-07-14T00:30:00+00:00",
                "updated_at": "2026-07-14T00:30:00+00:00",
                "review_status": "reviewed",
                "reviewed_at": "2026-07-14T01:00:00+00:00",
                "response": "",
            }
        ],
    }
    (tmp_path / "artifacts.json").write_text(json.dumps(payload), encoding="utf-8")

    result = _artifacts(tmp_path, "nonconforming")
    assert result.returncode == 0, result.stderr
    assert "NON-CONFORMING ARTIFACT BRIEFS" in result.stdout
    assert f"{ARTIFACT_PREFIX}bypass-001" in result.stdout


# ---------------------------------------------------------------------------
# D39..D42: routing feedback blocks hint application on thin/skewed evidence.
# ---------------------------------------------------------------------------


def _append_ledger(path: Path, *, order_id: str, sent_at: str, routing_hint: str | None = "sonnet") -> None:
    item = {
        "order_id": order_id,
        "session_ref": "localhost:s:0.0",
        "tag": "[C]",
        "routing_hint": routing_hint,
        "message_hash": "abc",
        "sent_at": sent_at,
        "signature": "sig",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item) + "\n")


def _usage_tool(tmp_path: Path, ledger: Path, *extra: str):
    return run_cli(
        [
            sys.executable,
            str(ROUTING_TOOL),
            "--ledger-jsonl",
            str(ledger),
            "--output-dir",
            str(tmp_path / "out"),
            "--now",
            "2026-07-09T03:00:00+00:00",
            *extra,
        ]
    )


def _report(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "out" / "routing-feedback-usage-report.json").read_text(encoding="utf-8"))


def test_d39_report_only_when_hints_are_observed_without_outcomes(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _append_ledger(ledger, order_id="o1", sent_at="2026-07-09T00:00:00+00:00", routing_hint="sonnet")
    _append_ledger(ledger, order_id="o2", sent_at="2026-07-09T01:00:00+00:00", routing_hint="haiku")
    _append_ledger(ledger, order_id="o3", sent_at="2026-07-09T02:00:00+00:00", routing_hint=None)
    routing = tmp_path / "routing.yaml"
    routing.write_text(yaml.safe_dump({"defaults": {"code-review": "sonnet", "search": "haiku"}}), encoding="utf-8")

    result = _usage_tool(tmp_path, ledger, "--routing-yaml", str(routing), "--min-samples", "3")

    assert result.returncode == 0, result.stderr + result.stdout
    report = _report(tmp_path)
    assert report["status"] == "report_only"
    assert report["would_change_routing_yaml"] is False
    assert report["diff_changed_lines"] == 0
    assert (tmp_path / "out" / "routing-feedback.diff").read_text() == ""
    assert {row["value"] for row in report["routing_hint_usage"]} == {"sonnet", "haiku", None}
    assert "success, failure" in report["blockers"][0]


def test_d40_hint_application_blocks_on_thin_fresh_samples(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _append_ledger(ledger, order_id="old", sent_at="2026-07-01T00:00:00+00:00")
    _append_ledger(ledger, order_id="fresh", sent_at="2026-07-09T00:00:00+00:00")

    result = _usage_tool(tmp_path, ledger, "--min-samples", "2")

    assert result.returncode == 0
    report = _report(tmp_path)
    assert report["status"] == "blocked"
    assert report["sources"]["ledger_jsonl"]["fresh_records"] == 1
    assert report["sources"]["ledger_jsonl"]["stale_records"] == 1
    assert any("below minimum 2" in blocker for blocker in report["blockers"])
    assert (tmp_path / "out" / "routing-feedback.diff").read_text() == ""


def test_d41_hint_application_blocks_on_skewed_distribution(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    for index in range(9):
        _append_ledger(ledger, order_id=f"sonnet-{index}", sent_at="2026-07-09T00:00:00+00:00", routing_hint="sonnet")
    _append_ledger(ledger, order_id="haiku-1", sent_at="2026-07-09T00:00:00+00:00", routing_hint="haiku")

    result = _usage_tool(tmp_path, ledger, "--min-samples", "8")

    assert result.returncode == 0
    report = _report(tmp_path)
    assert report["status"] == "blocked"
    assert any("distribution is skewed" in blocker for blocker in report["blockers"])
    assert (tmp_path / "out" / "routing-feedback.diff").read_text() == ""


def test_d42_malformed_ledger_lines_are_counted_not_promoted(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("{not json}\n", encoding="utf-8")
    _append_ledger(ledger, order_id="o1", sent_at="2026-07-09T00:00:00+00:00")

    result = _usage_tool(tmp_path, ledger, "--min-samples", "1", "--max-hint-share", "1.0")

    assert result.returncode == 0
    report = _report(tmp_path)
    assert report["sources"]["ledger_jsonl"]["parse_stats"]["malformed"] == 1
    assert report["sources"]["ledger_jsonl"]["fresh_records"] == 1


def test_d42_malformed_routing_yaml_fails_closed(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _append_ledger(ledger, order_id="o1", sent_at="2026-07-09T00:00:00+00:00")
    routing = tmp_path / "routing.yaml"
    routing.write_text("- not\n- a mapping\n", encoding="utf-8")

    result = _usage_tool(tmp_path, ledger, "--routing-yaml", str(routing), "--min-samples", "1")

    assert result.returncode == 2
    assert "must contain a YAML mapping" in result.stdout

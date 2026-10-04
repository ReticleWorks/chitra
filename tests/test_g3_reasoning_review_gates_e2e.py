"""G3 replace-then-remove: reasoning attestation and goal-review gates.

Deny paths are driven through the real boundaries: the ``dispatchd`` daemon
consuming a real order queue on disk, the ``chitra-review`` CLI reading an
envelope on stdin, and ``review_watched_session`` running real isolated
reviewer subprocesses against a real goals store. The only fake is the
reviewer executable standing in for the model provider.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from _g3_status_boundary import install_reviewer_stub, run_cli, script
from _goal_fixtures import enrollment_fields

from chitra.goal_enforcement import (
    ClaudeProcessReviewer,
    freeze_goal,
    review_log_path,
    review_watched_session,
)
from chitra.goals import GoalRecord, get_goal, upsert_goal
from chitra.reasoning import DecisionAttestation
from chitra.review_rubric import FINDING_CODES, WatchedSessionBehavior

SESSION_REF = "host-b:feeds:0.0"

# ---------------------------------------------------------------------------
# D19..D22: dispatchd refuses reasoned orders without a fully bound
# attestation; a valid attestation passes preflight and reaches dispatch.
# Boundary: order files in queue/orders consumed by `dispatchd --once`.
# ---------------------------------------------------------------------------

APPROVED_TEXT = "The digest combines both feeds per the tested readers."


def _attestation(**changes: object) -> dict:
    values: dict[str, object] = {
        "outcome": "answer",
        "message_kind": "reasoned_answer",
        "approved_text": APPROVED_TEXT,
        "source": "goal",
        "goal_contract_id": "sha256:" + "a" * 64,
        "goal_version": 1,
        "goal_fields": ("goal", "done_when"),
        "corpus_id": "sha256:" + "b" * 64,
        "confidence_basis": "the reader preference test passed on the recorded probe output",
        "autonomy": "autonomous",
        "operator_confirmation_required": False,
    }
    values.update(changes)
    return DecisionAttestation.create(**values).model_dump(mode="json")


def _order(tmp_path: Path, *, name: str = "ord-1.json", **order_fields: object) -> Path:
    orders_dir = tmp_path / "queue" / "orders"
    orders_dir.mkdir(parents=True, exist_ok=True)
    order: dict[str, object] = {
        "order_id": Path(name).stem,
        "session_ref": "localhost:deniedlane:0.0",
        "nudge": APPROVED_TEXT,
        "message_kind": "reasoned_answer",
        "decision_attestation": _attestation(),
    }
    order.update(order_fields)
    path = orders_dir / name
    path.write_text(json.dumps(order), encoding="utf-8")
    return path


def _dispatch(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    result = run_cli(
        [
            script("dispatchd"),
            "--once",
            "--queue-dir",
            str(tmp_path / "queue"),
            "--goals-root",
            str(tmp_path / "goals"),
            "--deny-session-prefix",
            "deniedlane",
        ],
        env_extra={"CHITRA_STATE_DIR": str(tmp_path / "state")},
    )
    assert result.returncode == 0, result.stderr
    # structlog lines may precede the JSON results array on stdout; the array
    # opens on its own line.
    start = next(i for i, line in enumerate(result.stdout.splitlines()) if line == "[")
    return result, json.loads("\n".join(result.stdout.splitlines()[start:]))


def _invalid_dir(tmp_path: Path) -> list[str]:
    invalid = tmp_path / "queue" / "invalid"
    return sorted(p.name for p in invalid.glob("*")) if invalid.is_dir() else []


def test_d22_valid_reasoned_order_passes_attestation_preflight(tmp_path: Path) -> None:
    _order(tmp_path)
    result, results = _dispatch(tmp_path)

    entry = next(item for item in results if item["order_id"] == "ord-1")
    assert not entry["reason"].startswith("invalid-order")
    assert "ord-1.json" not in _invalid_dir(tmp_path)
    # The namespace denial proves the order reached the dispatch stage.
    assert "denied" in entry["reason"] or "namespace" in entry["reason"]


def test_d19_reasoned_order_without_attestation_is_refused(tmp_path: Path) -> None:
    _order(tmp_path, decision_attestation=None)
    result, results = _dispatch(tmp_path)

    entry = next(item for item in results if item["order_id"] == "ord-1")
    assert entry["status"] == "failed"
    assert "invalid-order" in entry["reason"]
    assert "requires decision_attestation" in entry["reason"]
    assert _invalid_dir(tmp_path) == ["ord-1.json"]


def test_d20_tampered_attestation_is_refused(tmp_path: Path) -> None:
    """Flip a content-bound field: the attestation_id no longer matches."""
    attestation = _attestation()
    attestation["confidence_basis"] = "a different confidence basis than attested"
    _order(tmp_path, decision_attestation=attestation)
    _, results = _dispatch(tmp_path)

    entry = next(item for item in results if item["order_id"] == "ord-1")
    assert "invalid-order" in entry["reason"]
    assert "attestation_id does not match" in entry["reason"]
    assert _invalid_dir(tmp_path) == ["ord-1.json"]


def test_d20_unapproved_nudge_text_is_refused(tmp_path: Path) -> None:
    """The nudge is verbatim approved text; substituting it refuses the order."""
    _order(tmp_path, nudge="a rewritten nudge nobody attested")
    _, results = _dispatch(tmp_path)

    entry = next(item for item in results if item["order_id"] == "ord-1")
    assert "invalid-order" in entry["reason"]
    assert "approved_text" in entry["reason"]
    assert _invalid_dir(tmp_path) == ["ord-1.json"]


def test_d21_kai_delegate_requires_delegated_authority(tmp_path: Path) -> None:
    attestation = _attestation(
        source="kai-delegate",
        delegated_authority={
            "grant_id": "sha256:" + "c" * 64,
            "grant_sha256": "d" * 64,
            "satisfaction_sha256": "e" * 64,
            "request_id": "sha256:" + "f" * 64,
        },
    )
    # Strip the authority record but keep the kai-delegate source marker.
    forged = dict(attestation)
    forged.pop("delegated_authority", None)
    _order(tmp_path, decision_attestation=forged)
    _, results = _dispatch(tmp_path)

    entry = next(item for item in results if item["order_id"] == "ord-1")
    assert "invalid-order" in entry["reason"]
    assert "Kai-delegated decisions require delegated_authority" in entry["reason"]


def test_d21_non_kai_source_rejects_delegated_authority(tmp_path: Path) -> None:
    attestation = _attestation()
    forged = dict(attestation)
    forged["delegated_authority"] = {
        "grant_id": "sha256:" + "c" * 64,
        "grant_sha256": "d" * 64,
        "satisfaction_sha256": "e" * 64,
        "request_id": "sha256:" + "f" * 64,
    }
    _order(tmp_path, decision_attestation=forged)
    _, results = _dispatch(tmp_path)

    entry = next(item for item in results if item["order_id"] == "ord-1")
    assert "invalid-order" in entry["reason"]
    assert "delegated_authority" in entry["reason"]


def test_d21_abstained_decision_cannot_dispatch(tmp_path: Path) -> None:
    attestation = _attestation(outcome="abstain", autonomy="foreground_residual")
    _order(tmp_path, decision_attestation=attestation)
    _, results = _dispatch(tmp_path)

    entry = next(item for item in results if item["order_id"] == "ord-1")
    assert "invalid-order" in entry["reason"]
    assert "abstained" in entry["reason"]


# ---------------------------------------------------------------------------
# D23..D30: the isolated-reviewer gate.
# Boundaries: `chitra-review` (envelope on stdin, verdict on stdout) and
# review_watched_session (real subprocess reviewers over a real goals store).
# ---------------------------------------------------------------------------


def _stub(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    return install_reviewer_stub(bin_dir)


def test_pre019_attestation_hash_still_dispatches(tmp_path: Path) -> None:
    """A pre-0.19 attestation record (the legacy field set, hashed the old
    way) remains readable: the order passes attestation preflight instead of
    being treated as an invalid document."""
    import hashlib

    approved_text = "Use the existing typed boundary."
    payload: dict[str, object] = {
        "outcome": "answer",
        "message_kind": "reasoned_answer",
        "approved_text": approved_text,
        "approved_text_sha256": hashlib.sha256(approved_text.encode()).hexdigest(),
        "source": "goal",
        "authority_class": "routine",
        "goal_contract_id": "sha256:" + "1" * 64,
        "goal_version": 1,
        "goal_fields": ("scope",),
        "corpus_id": "sha256:" + "2" * 64,
        "principle_ids": (),
        "principle_citations": (),
        "evidence_refs": (),
        "oracle_escalated": False,
        "confidence_basis": "the frozen goal directly determines this answer",
        "insufficiency_reasons": (),
        "review_signal_id": None,
        "review_verdict": None,
        "reviewer_count": 0,
        "autonomy_policy_sha256": "3" * 64,
        "capability_grant_ids": (),
        "capability_requirements": (),
        "autonomy": "autonomous",
        "operator_gate_reasons": (),
        "operator_confirmation_required": False,
        "operator_confirmed": False,
    }
    attestation_id = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _order(
        tmp_path,
        nudge=approved_text,
        decision_attestation={**payload, "attestation_id": attestation_id},
    )
    _, results = _dispatch(tmp_path)

    entry = next(item for item in results if item["order_id"] == "ord-1")
    assert not entry["reason"].startswith("invalid-order"), entry["reason"]
    assert "ord-1.json" not in _invalid_dir(tmp_path)
    assert "denied" in entry["reason"] or "namespace" in entry["reason"]


def _lane_envelope(**changes: object) -> dict:
    envelope: dict[str, object] = {
        "mode": "lane",
        "session_ref": SESSION_REF,
        "final_message": "I ran the digest preference checks; the combined feed won.",
        "reviewer_id": "chitra-review",
        "goal": {
            "session_ref": SESSION_REF,
            "intent": "Keep the feeds digest lane working toward a verified combined outcome.",
            "goal": "Ship the combined feeds digest to every subscribed reader.",
            "done_when": "Every required validation command passes cleanly.",
            "scope": "the digest rendering surface only",
            "source": "branch",
            "goal_version": 1,
        },
    }
    envelope.update(changes)
    return envelope


def _review_cli(tmp_path: Path, envelope: dict, *, env_extra: dict[str, str] | None = None, mode: str = "lane"):
    stub = _stub(tmp_path)
    return run_cli(
        [script("chitra-review"), "--mode", mode, "--command", str(stub)],
        stdin=json.dumps(envelope),
        env_extra=env_extra or {},
    )


def test_chitra_review_accepts_a_bound_lane_verdict(tmp_path: Path) -> None:
    result = _review_cli(tmp_path, _lane_envelope())
    assert result.returncode == 0, result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["verdict"] == "accept"
    assert verdict["reviewer_id"] == "chitra-review"


def test_d24_envelope_with_forged_contract_id_fails_closed(tmp_path: Path) -> None:
    envelope = _lane_envelope()
    goal = dict(envelope["goal"])  # type: ignore[arg-type]
    goal["contract_id"] = "sha256:" + "0" * 64
    envelope["goal"] = goal
    result = _review_cli(tmp_path, envelope)
    assert result.returncode == 3
    assert "contract_id" in result.stderr


def test_d24_verdict_with_tampered_goal_binding_fails_closed(tmp_path: Path) -> None:
    result = _review_cli(tmp_path, _lane_envelope(), env_extra={"STUB_TAMPER_GOAL": "1"})
    assert result.returncode == 3
    assert result.stderr.strip()


def test_d24_verdict_with_tampered_behavior_binding_fails_closed(tmp_path: Path) -> None:
    result = _review_cli(tmp_path, _lane_envelope(), env_extra={"STUB_TAMPER_BEHAVIOR": "1"})
    assert result.returncode == 3


def test_d24_verdict_with_forged_reviewer_id_fails_closed(tmp_path: Path) -> None:
    envelope = _lane_envelope()
    forged = {
        "reviewer_id": "forged-reviewer",
        "goal_contract_id": _frozen_contract_id_for_envelope(envelope),
        "behavior_sha256": _behavior_sha(envelope),
        "verdict": "accept",
        "findings": [],
    }
    queue = tmp_path / "queue.jsonl"
    queue.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    result = _review_cli(tmp_path, envelope, env_extra={"STUB_QUEUE": str(queue)})
    assert result.returncode == 3


def test_d25_reviewer_runs_sandboxed_without_tools_or_memory(tmp_path: Path) -> None:
    argv_log = tmp_path / "argv.jsonl"
    result = _review_cli(tmp_path, _lane_envelope(), env_extra={"STUB_ARGV_LOG": str(argv_log)})
    assert result.returncode == 0, result.stderr

    calls = [json.loads(line)["argv"] for line in argv_log.read_text().splitlines()]
    assert calls, "stub was never invoked"
    for argv in calls:
        assert "--no-session-persistence" in argv
        assert "--allowed-tools" in argv
        assert argv[argv.index("--allowed-tools") + 1] == ""
        assert "--system-prompt" in argv
        assert "-p" in argv
        prompt = argv[argv.index("-p") + 1]
        assert "<role>" in prompt and "<constraints>" in prompt and "<output_format>" in prompt
        for code in FINDING_CODES:
            assert f'"{code}"' in prompt
        # The reviewer payload contract: one INPUT= tail carrying the bindings.
        payload = json.loads(prompt.rsplit("\nINPUT=", 1)[1])
        assert payload["reviewer_id"] == "chitra-review"
        assert payload["frozen_goal"]["session_ref"] == SESSION_REF
        assert payload["watched_session_behavior"]["session_ref"] == SESSION_REF
        assert payload["watched_session_behavior"]["behavior_sha256"]


def test_d26_invalid_reply_is_retried_to_budget_then_fails_closed(tmp_path: Path) -> None:
    argv_log = tmp_path / "argv.jsonl"
    result = _review_cli(
        tmp_path,
        _lane_envelope(),
        env_extra={"STUB_MODE": "invalid_json", "STUB_ARGV_LOG": str(argv_log)},
    )
    assert result.returncode == 3
    assert "invalid JSON" in result.stderr
    assert len(argv_log.read_text().splitlines()) == 5


def test_d26_nonzero_exit_fails_closed_without_retry(tmp_path: Path) -> None:
    argv_log = tmp_path / "argv.jsonl"
    result = _review_cli(
        tmp_path,
        _lane_envelope(),
        env_extra={"STUB_MODE": "fail", "STUB_ARGV_LOG": str(argv_log)},
    )
    assert result.returncode == 3
    assert len(argv_log.read_text().splitlines()) == 1


def test_d30_fenced_verdict_json_is_unwrapped(tmp_path: Path) -> None:
    """A correct verdict wrapped in a markdown code fence still binds and
    returns -- the reply unwrap is part of the wire contract."""
    result = _review_cli(tmp_path, _lane_envelope(), env_extra={"STUB_MODE": "fenced"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["verdict"] == "accept"


def _behavior_sha(envelope: dict) -> str:
    return WatchedSessionBehavior.from_turn(
        str(envelope["session_ref"]), str(envelope["final_message"])
    ).behavior_sha256


def _frozen_contract_id_for_envelope(envelope: dict) -> str:
    from chitra.autonomy import DEFAULT_AUTONOMY_POLICY
    from chitra.review_rubric import contract_id_for

    goal = envelope["goal"]  # type: ignore[assignment]
    fields = ("session_ref", "intent", "goal", "done_when", "scope", "source", "goal_version")
    payload = {name: goal[name] for name in fields}
    payload["autonomy_policy"] = DEFAULT_AUTONOMY_POLICY.model_dump(mode="json")
    return contract_id_for(payload)  # type: ignore[return-value]


def test_d30_every_finding_code_round_trips_through_the_cli(tmp_path: Path) -> None:
    """A verdict carrying each enumerated finding code parses and binds."""
    for code in FINDING_CODES:
        case_dir = tmp_path / code
        case_dir.mkdir()
        envelope = _lane_envelope(session_ref=f"host-b:feeds-{code}:0.0")
        goal = dict(envelope["goal"])  # type: ignore[arg-type]
        goal["session_ref"] = f"host-b:feeds-{code}:0.0"
        envelope["goal"] = goal
        verdict = {
            "reviewer_id": "chitra-review",
            "goal_contract_id": _frozen_contract_id_for_envelope(envelope),
            "behavior_sha256": _behavior_sha(envelope),
            "verdict": "reject",
            "findings": [
                {
                    "code": code,
                    "detail": f"finding exercising code {code}",
                    "citation": "combined feed won" if code else "",
                }
            ],
        }
        queue = case_dir / "queue.jsonl"
        queue.write_text(json.dumps(verdict) + "\n", encoding="utf-8")
        result = _review_cli(case_dir, envelope, env_extra={"STUB_QUEUE": str(queue)})
        assert result.returncode == 0, f"{code}: {result.stderr}"
        assert json.loads(result.stdout)["findings"][0]["code"] == code


def test_d30_unknown_finding_code_is_refused(tmp_path: Path) -> None:
    verdict = {
        "reviewer_id": "chitra-review",
        "goal_contract_id": _frozen_contract_id_for_envelope(_lane_envelope()),
        "behavior_sha256": _behavior_sha(_lane_envelope()),
        "verdict": "reject",
        "findings": [{"code": "not_a_real_code", "detail": "invented code", "citation": "combined feed won"}],
    }
    # An unparseable/invalid reply is retried to the attempt budget; fill the
    # queue so all five attempts return the bad verdict.
    queue = tmp_path / "queue.jsonl"
    queue.write_text("\n".join([json.dumps(verdict)] * 5) + "\n", encoding="utf-8")
    result = _review_cli(tmp_path, _lane_envelope(), env_extra={"STUB_QUEUE": str(queue)})
    assert result.returncode == 3


def test_d30_monitor_mode_binds_the_monitor_contract(tmp_path: Path) -> None:
    envelope = {
        "mode": "monitor",
        "session_ref": "host-b:monitor:0.0",
        "final_message": "I checked every lane's status file; no lane changed.",
        "context": "monitor duty",
        "reviewer_id": "chitra-review",
    }
    result = _review_cli(tmp_path, envelope, mode="monitor")
    assert result.returncode == 0, result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["verdict"] == "accept"
    assert verdict["reviewer_id"] == "chitra-review"


# ---------------------------------------------------------------------------
# review_watched_session: the unanimous multi-reviewer gate over a real goals
# store, driven by real reviewer subprocesses.
# ---------------------------------------------------------------------------


def _enroll(tmp_path: Path) -> GoalRecord:
    return upsert_goal(
        tmp_path,
        GoalRecord(
            session_ref=SESSION_REF,
            goal="Ship the combined feeds digest to every subscribed reader.",
            done_when="Every required validation command passes cleanly.",
            source="branch",
            status="working",
            goal_version=1,
            intent="Keep the feeds digest lane working toward a verified combined outcome.",
            scope="the digest rendering surface only",
            **enrollment_fields("Every required validation command passes cleanly."),
        ),
    )


def _behavior() -> WatchedSessionBehavior:
    return WatchedSessionBehavior.from_turn(
        SESSION_REF, "I ran the digest preference checks; the combined feed won."
    )


def _queued_verdicts(tmp_path: Path, root: Path, verdicts: list[str]) -> dict[str, str]:
    contract_id = freeze_goal(get_goal(root, SESSION_REF)).contract_id
    queue = tmp_path / "queue.jsonl"
    lines = []
    for index, verdict in enumerate(verdicts):
        findings = []
        if verdict == "reject":
            findings = [
                {
                    "code": "unverified_claim",
                    "detail": "claim lacks cited proof",
                    "citation": "combined feed won",
                }
            ]
        lines.append(
            json.dumps(
                {
                    "reviewer_id": f"reviewer-1-{index + 1}",
                    "goal_contract_id": contract_id,
                    "behavior_sha256": _behavior().behavior_sha256,
                    "verdict": verdict,
                    "findings": findings,
                }
            )
        )
    queue.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"STUB_QUEUE": str(queue)}


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        (["accept", "accept"], "accept"),
        (["accept", "reject"], "reject"),
        (["accept", "insufficient"], "insufficient"),
        (["reject", "insufficient"], "reject"),
    ],
)
def test_d23_release_requires_unanimous_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdicts: list[str], expected: str
) -> None:
    root = tmp_path / "goals"
    _enroll(root)
    env = _queued_verdicts(tmp_path, root, verdicts)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    signal = review_watched_session(
        root,
        SESSION_REF,
        _behavior(),
        reviewer=ClaudeProcessReviewer(command=str(_stub(tmp_path)), runner=subprocess.run),
        reviewer_count=2,
    )

    assert signal.verdict == expected
    assert signal.reviewer_ids == ("reviewer-1-1", "reviewer-1-2")
    log = [json.loads(line) for line in review_log_path(root).read_text().splitlines()]
    assert len(log) == 1 and log[0]["verdict"] == expected


def test_d27_redirect_during_review_restarts_with_one_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "goals"
    _enroll(root)
    stub = _stub(tmp_path)
    mark = tmp_path / "redirected"
    redirect_argv = "\x1f".join(
        [
            script("chitra-goals"),
            "redirect",
            "--root",
            str(root),
            "--session-ref",
            SESSION_REF,
            "--reason",
            "operator narrowed the goal mid-review",
            "--goal",
            "Ship the combined feeds digest to subscribers in the pilot cohort only.",
        ]
    )
    monkeypatch.setenv("STUB_REDIRECT_MARK", str(mark))
    monkeypatch.setenv("STUB_REDIRECT_ARGV", redirect_argv)

    signal = review_watched_session(
        root,
        SESSION_REF,
        _behavior(),
        reviewer=ClaudeProcessReviewer(command=str(stub), runner=subprocess.run),
        reviewer_count=2,
    )

    assert signal.verdict == "accept"
    assert signal.restarted_after_redirect is True
    assert signal.reviewer_ids == ("reviewer-2-1",)

    record = get_goal(root, SESSION_REF)
    assert any(
        event.get("event") == "adversarial-review-redirect-restart" for event in record.goal_history
    )

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

import chitra.dispatchd as dispatchd
import chitra.monitord as monitord
from chitra.detect import Finding
from chitra.goals import EnrolledScopeImmutableError, GoalRecord, redirect_goal, upsert_goal
from chitra.journal import ByteRange, CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.ledger import append_entry, load_or_create_signing_key
from chitra.orders import DispatchResult, DispatchStatus
from chitra.question_handler import handle_question
from chitra.supervisor import build_question_order, reconcile_question_action
from chitra.transcript_bindings import TranscriptBinding

LANE = "lane-a"
SESSION = "host:lane-a:0.0"


def _goal() -> GoalRecord:
    return GoalRecord(
        session_ref=SESSION,
        goal="Ship the bounded persistent supervisor safely.",
        done_when="The supervisor tests pass with durable evidence.",
        source="task-file:test-supervisor",
        status="working",
        intent="Keep the supervised session moving.",
        scope="The supervisor source and tests.",
        now="",
        last_verified="",
        created_at="2026-08-26T00:00:00+00:00",
        updated_at="2026-08-26T00:00:00+00:00",
        **enrollment_fields("The supervisor tests pass with durable evidence."),
    )


def _event(event_id: str, normalized_type: CanonicalType, *, text: str, session_id: str = SESSION) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        instance="test",
        lane=LANE,
        client=Client.CLAUDE,
        client_version="2.1.229",
        process_id=None,
        transcript=TranscriptIdentity(path="/tmp/test.jsonl", device=0, inode=0),
        session_id=session_id,
        resume_id=None,
        observed_at="2026-08-26T00:00:00+00:00",
        native_time=None,
        native_type="user" if normalized_type is CanonicalType.UNKNOWN else "assistant",
        native_join_id=None,
        raw_byte_range=ByteRange(start=0, end=1),
        raw_sha256=None,
        normalized_type=normalized_type,
        payload_digest="d" * 64,
        normalizer_version="test",
        payload={"text": text},
        raw_record=None,
    )


def _write_result(queue: Path, order: DispatchResult) -> None:
    path = queue / "results" / f"{order.order_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(order.model_dump_json(), encoding="utf-8")


def test_question_intent_is_durable_and_idempotent(tmp_path: Path) -> None:
    goal = _goal()
    question = handle_question(goal, "What proves the goal is done?")
    assert question.answer is not None
    kwargs = {
        "state_root": tmp_path / "state",
        "queue_dir": tmp_path / "queue",
        "lane": LANE,
        "goal": goal,
        "question_result": question,
        "retry_delay_seconds": 0,
    }
    first = reconcile_question_action(**kwargs)  # type: ignore[arg-type]
    second = reconcile_question_action(**kwargs)  # type: ignore[arg-type]
    assert first.enqueued is True
    assert second.enqueued is False
    assert second.state == "action_queued"
    assert len(list((tmp_path / "queue" / "orders").glob("*.json"))) == 1
    rows = (tmp_path / "state" / "question-actions" / "supervision" / f"{LANE}.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(row)["state"] for row in rows] == ["action_pending", "action_queued"]


def test_question_delivery_requires_signed_consumption_before_progress(tmp_path: Path) -> None:
    goal = _goal()
    question = handle_question(goal, "What proves the goal is done?")
    order = build_question_order(goal, question)
    key_path = tmp_path / "ledger.key"
    ledger_path = tmp_path / "ledger.jsonl"
    key = load_or_create_signing_key(key_path)
    append_entry(
        ledger_path,
        order_id=order.order_id,
        session_ref=SESSION,
        tag="[C]",
        nudge=order.nudge,
        key=key,
        native_session_id=SESSION,
    )
    _write_result(
        tmp_path / "queue",
        DispatchResult(
            order_id=order.order_id,
            session_ref=SESSION,
            status=DispatchStatus.SENT,
            delivery_ledger_verified=True,
        ),
    )
    events = (
        _event("user-1", CanonicalType.UNKNOWN, text=order.nudge),
        _event("turn-1", CanonicalType.FINAL_RESPONSE, text="I followed the answer."),
    )
    result = reconcile_question_action(
        state_root=tmp_path / "state",
        queue_dir=tmp_path / "queue",
        lane=LANE,
        goal=goal,
        question_result=question,
        journal_events=events,
        ledger_path=ledger_path,
        ledger_key_path=key_path,
    )
    assert result.state == "awaiting_progress"
    assert result.enqueued is False
    latest = (
        tmp_path / "state" / "question-actions" / "supervision" / f"{LANE}.jsonl"
    ).read_text(encoding="utf-8").splitlines()[-1]
    record = json.loads(latest)
    assert record["observed_event_id"] == "user-1"
    assert record["turn_boundary_event_id"] == "turn-1"


def test_question_terminal_failures_keep_advancing_retry_ids(tmp_path: Path) -> None:
    goal = _goal()
    question = handle_question(goal, "What proves the goal is done?")
    kwargs = {
        "state_root": tmp_path / "state",
        "queue_dir": tmp_path / "queue",
        "lane": LANE,
        "goal": goal,
        "question_result": question,
        "retry_delay_seconds": 0,
    }
    first = reconcile_question_action(**kwargs)  # type: ignore[arg-type]
    assert first.enqueued is True
    order_ids = [first.order_id]
    for _retry in range(6):
        _write_result(
            tmp_path / "queue",
            DispatchResult(
                order_id=order_ids[-1],
                session_ref=SESSION,
                status=DispatchStatus.FAILED,
                reason="lane unavailable",
            ),
        )
        failed = reconcile_question_action(**kwargs)  # type: ignore[arg-type]
        assert failed.state == "blocked"
        assert failed.order_id == order_ids[-1]
        assert "retry scheduled" in failed.reason
        resumed = reconcile_question_action(**kwargs)  # type: ignore[arg-type]
        assert resumed.enqueued is True
        order_ids.append(resumed.order_id)
        assert resumed.order_id != order_ids[-2]
    assert len(order_ids) == 7


def test_question_is_processed_even_when_detectors_open_a_finding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A question is an independent oversight action, not a finding fallback."""
    goal = replace(_goal(), lane_id=LANE)
    final_response = _event("question-1", CanonicalType.FINAL_RESPONSE, text="What proves the goal is done?")
    finding = Finding(
        detector="unnecessary_steps",
        fingerprint_seed={"test": "question-and-finding"},
        event_refs=("tool-1",),
        unmet_item="done-1",
        expected_next_progress="run the focused check",
        detail="the lane repeated a step",
    )
    calls: list[tuple[str, bool]] = []
    bound_path = Path("/tmp/test.jsonl").resolve()
    binding = TranscriptBinding(
        session_ref=SESSION,
        lane=LANE,
        path=str(bound_path),
        client=Client.CLAUDE,
        client_version="2.1.229",
        instance="test",
    )
    monkeypatch.setattr(monitord, "_lane_roots", lambda _root: [tmp_path / "state" / "journal" / f"{LANE}.jsonl"])
    monkeypatch.setattr(monitord, "load_transcript_bindings", lambda *_args, **_kwargs: (binding,))
    monkeypatch.setattr(monitord, "ingest_transcript_bindings", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(monitord, "native_session_identity", lambda _path: SESSION)
    bound_response = final_response.model_copy(
        update={
            "goal_ref": SESSION,
            "transcript": final_response.transcript.model_copy(update={"path": str(bound_path)}),
        }
    )
    monkeypatch.setattr(monitord, "load_lane_events", lambda *_args, **_kwargs: (bound_response,))
    monkeypatch.setattr(monitord, "list_goals", lambda _root: [goal])
    monkeypatch.setattr(monitord, "run_detectors", lambda *_args, **_kwargs: [finding])
    monkeypatch.setattr(monitord, "evaluate_findings", lambda *_args, **_kwargs: ())

    def record_question(_config, active_goal, response, **_kwargs):
        calls.append((active_goal.session_ref, response.event_id == final_response.event_id))
        return "answer_queued"

    monkeypatch.setattr(monitord, "handle_agent_question", record_question)
    summary = monitord.run_once(monitord.resolve_config(state_dir=tmp_path / "state", shadow_mode=True))

    assert summary["findings_opened"] == 1
    assert calls == [(SESSION, True)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope", "A revised bounded scope for supervisor tests."),
        ("done_when", "The revised supervisor evidence is independently verified."),
        ("goal", "Ship the revised bounded supervisor safely."),
    ],
)
def test_queued_question_is_blocked_after_goal_contract_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    """A queued answer cannot cross a scope, completion, or version change."""
    old_goal = _goal()
    goals_root = tmp_path / "goals"
    upsert_goal(goals_root, old_goal)
    question = handle_question(old_goal, "What proves the goal is done?")
    order = build_question_order(old_goal, question)
    queue = tmp_path / "queue"
    path = queue / "orders" / f"{order.order_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(order.model_dump_json(), encoding="utf-8")
    if field == "done_when":
        with pytest.raises(EnrolledScopeImmutableError):
            redirect_goal(goals_root, SESSION, reason=f"change frozen {field}", **{field: value})
        return
    redirect_goal(goals_root, SESSION, reason=f"change frozen {field}", **{field: value})
    calls: list[str] = []
    monkeypatch.setattr(dispatchd, "dispatch_to_tmux", lambda order, **_kwargs: calls.append(order.order_id))

    result = dispatchd.run_once(
        queue,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "delivery.jsonl",
        ledger_key_path=tmp_path / "delivery.key",
        goals_root=goals_root,
    )[0]

    assert result.status is DispatchStatus.BLOCKED
    assert result.reason == "stale-goal-contract"
    assert calls == []

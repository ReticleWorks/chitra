"""Tests for chitra-failover: the manual verbs must match the sweep's decision."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra import failover_cli
from chitra.goals import GoalRecord, get_goal, upsert_goal

HOST = "tophand"
SESSION = "lane-a"
SESSION_REF = f"{HOST}:{SESSION}:0.0"


def write_snapshot(usage_dir: Path, *, used: float, now: datetime, resets_in: int = 3600) -> None:
    usage_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "chitra.usage.v1",
        "ts": now.isoformat().replace("+00:00", "Z"),
        "kind": "claude",
        "session_id": "sess-1",
        "tmux_session": SESSION,
        "account": "team@example.com",
        "five_hour": {"pct": used, "resets_at": int(now.timestamp()) + resets_in},
        "seven_day": {"pct": 10, "resets_at": int(now.timestamp()) + 7 * 86400},
    }
    (usage_dir / "sess-1.json").write_text(json.dumps(payload), encoding="utf-8")


def printed_json(out: str) -> dict[str, object]:
    """Return the JSON document the verb printed, ignoring structlog's lines."""
    start = out.index("\n{") + 1 if "\n{" in out else out.index("{")
    return json.loads(out[start:])


def make_goal(**overrides: object) -> GoalRecord:
    base: dict[str, object] = {
        "session_ref": SESSION_REF,
        "goal": "Land the failover manual verbs on the main branch",
        "done_when": "the three verbs run and are covered by tests",
        "source": "operator",
        "status": "working",
    }
    base.update(overrides)
    base.update(enrollment_fields(str(base["done_when"])))
    return GoalRecord(**base)  # type: ignore[arg-type]


def namespace(tmp_path: Path, verb: str, **overrides: object):
    import argparse

    args = argparse.Namespace(
        verb=verb,
        usage_dir=tmp_path / "usage",
        host=HOST,
        staleness_seconds=1200,
        codex=False,
        codex_bin=Path("codex"),
        goals_root=tmp_path / "goals",
        queue_dir=tmp_path / "queue",
        policy_config=None,
        lane=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_evaluate_reports_a_pause_plan_and_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = datetime.now(UTC)
    write_snapshot(tmp_path / "usage", used=99, now=now)
    upsert_goal(tmp_path / "goals", make_goal())
    assert failover_cli.run_evaluate(namespace(tmp_path, "evaluate")) == 0
    payload = printed_json(capsys.readouterr().out)
    assert payload["would_pause"] == [SESSION_REF], payload
    # Evaluate is a read. The lane is still working afterwards.
    assert get_goal(tmp_path / "goals", SESSION_REF).status == "working"  # type: ignore[union-attr]


def test_evaluate_reports_nothing_to_do_when_usage_is_comfortable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = datetime.now(UTC)
    write_snapshot(tmp_path / "usage", used=5, now=now)
    upsert_goal(tmp_path / "goals", make_goal())
    failover_cli.run_evaluate(namespace(tmp_path, "evaluate"))
    payload = printed_json(capsys.readouterr().out)
    assert payload["would_pause"] == []
    assert payload["would_resume"] == []


def test_run_lane_holds_exactly_that_lane(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = datetime.now(UTC)
    write_snapshot(tmp_path / "usage", used=99, now=now)
    upsert_goal(tmp_path / "goals", make_goal())
    assert failover_cli.run_run(namespace(tmp_path, "run", lane=SESSION_REF)) == 0
    assert printed_json(capsys.readouterr().out)["paused"] == SESSION_REF
    record = get_goal(tmp_path / "goals", SESSION_REF)
    assert record is not None
    assert record.status == "held"
    assert record.hold_reason.startswith("rate-limit")


def test_run_lane_refuses_with_the_planner_s_own_reason(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    write_snapshot(tmp_path / "usage", used=99, now=now)
    # No goal record for this lane, so the planner skips it and so does the verb.
    with pytest.raises(failover_cli.FailoverError) as caught:
        failover_cli.run_run(namespace(tmp_path, "run", lane=SESSION_REF))
    assert "no chitra goal record" in str(caught.value)


def test_run_lane_refuses_a_lane_that_is_not_near_its_limit(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    write_snapshot(tmp_path / "usage", used=5, now=now)
    upsert_goal(tmp_path / "goals", make_goal())
    with pytest.raises(failover_cli.FailoverError):
        failover_cli.run_run(namespace(tmp_path, "run", lane=SESSION_REF))
    assert get_goal(tmp_path / "goals", SESSION_REF).status == "working"  # type: ignore[union-attr]


def test_resume_starts_the_resume_for_a_due_lane_that_is_back_under_its_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = datetime.now(UTC)
    write_snapshot(tmp_path / "usage", used=5, now=now)
    past = (now - timedelta(minutes=5)).isoformat()
    upsert_goal(
        tmp_path / "goals",
        make_goal(status="held", hold_reason="rate-limit: 5h window at 99 percent", resume_at=past),
    )
    assert failover_cli.run_resume(namespace(tmp_path, "resume")) == 0
    assert printed_json(capsys.readouterr().out)["resuming"] == SESSION_REF
    # The hold is not cleared here; that waits on a confirmed delivery.
    assert get_goal(tmp_path / "goals", SESSION_REF).status == "held"  # type: ignore[union-attr]


def test_resume_refuses_while_the_window_is_still_hot(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    write_snapshot(tmp_path / "usage", used=99, now=now)
    past = (now - timedelta(minutes=5)).isoformat()
    upsert_goal(tmp_path / "goals", make_goal(status="held", hold_reason="rate-limit: 5h window at 99 percent", resume_at=past))
    with pytest.raises(failover_cli.FailoverError):
        failover_cli.run_resume(namespace(tmp_path, "resume"))


def test_resume_refuses_a_lane_held_for_a_reason_other_than_a_rate_limit(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    write_snapshot(tmp_path / "usage", used=5, now=now)
    past = (now - timedelta(minutes=5)).isoformat()
    upsert_goal(tmp_path / "goals", make_goal(status="held", hold_reason="operator: waiting on review", resume_at=past))
    with pytest.raises(failover_cli.FailoverError):
        failover_cli.run_resume(namespace(tmp_path, "resume", lane=SESSION_REF))


def test_the_three_verbs_are_reachable_from_the_command_line(tmp_path: Path) -> None:
    parser = failover_cli.build_arg_parser()
    for verb in ("evaluate", "run", "resume"):
        args = parser.parse_args(["--usage-dir", str(tmp_path), "--host", HOST, verb])
        assert args.verb == verb

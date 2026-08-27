"""Focused tests for bounded, restart-safe finding scheduling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chitra.detect import Finding
from chitra.monitord import MAX_ACTIONS_PER_PASS, evaluate_findings, resolve_config, schedule_findings


def _finding(name: str) -> Finding:
    return Finding(
        detector="test",
        fingerprint_seed={"name": name},
        event_refs=(f"event-{name}",),
        unmet_item="done-1",
        expected_next_progress=f"make progress for {name}",
        detail=f"finding {name}",
    )


def test_findings_rotate_fairly_from_a_durable_cursor_across_restarts(tmp_path: Path) -> None:
    config = resolve_config(state_dir=tmp_path)
    findings = [_finding("alpha"), _finding("beta"), _finding("gamma")]

    assert [item.detail for item in schedule_findings(config, "lane", findings)] == [
        "finding alpha",
        "finding beta",
        "finding gamma",
    ]
    assert [item.detail for item in schedule_findings(config, "lane", findings)] == [
        "finding beta",
        "finding gamma",
        "finding alpha",
    ]

    # A fresh config models a restarted monitor. The next incident remains
    # beta's successor rather than resetting to detector order.
    restarted = resolve_config(state_dir=tmp_path)
    assert [item.detail for item in schedule_findings(restarted, "lane", findings)] == [
        "finding gamma",
        "finding alpha",
        "finding beta",
    ]

    cursor = json.loads(
        (tmp_path / "finding-scheduler" / "lane.json").read_text(encoding="utf-8")
    )
    assert cursor["schema"] == "chitra.monitord.finding-scheduler.v1"
    assert cursor["lane"] == "lane"
    assert cursor["next_fingerprint"] == findings[0].fingerprint


def test_duplicate_fingerprints_keep_detector_order_within_one_fair_slot(tmp_path: Path) -> None:
    config = resolve_config(state_dir=tmp_path)
    alpha = _finding("alpha")
    beta = _finding("beta")

    ordered = schedule_findings(config, "lane", [alpha, beta, alpha])
    assert ordered == [alpha, alpha, beta]


def test_scheduler_state_is_fail_closed_when_corrupt(tmp_path: Path) -> None:
    config = resolve_config(state_dir=tmp_path)
    schedule_path = tmp_path / "finding-scheduler" / "lane.json"
    schedule_path.parent.mkdir(parents=True)
    schedule_path.write_text("{\"schema\": \"wrong\"}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid finding scheduler state"):
        schedule_findings(config, "lane", [_finding("alpha")])


def test_monitor_action_budget_is_explicitly_bounded() -> None:
    assert MAX_ACTIONS_PER_PASS == 1


def test_persistent_first_incident_cannot_starve_later_actions(tmp_path: Path) -> None:
    config = resolve_config(state_dir=tmp_path)
    findings = [_finding("alpha"), _finding("beta"), _finding("gamma")]
    served: list[str] = []

    for _pass in range(3):
        action_count = 0

        def serve_one(finding: Finding, _decision: object) -> None:
            nonlocal action_count
            if action_count >= MAX_ACTIONS_PER_PASS:
                return
            served.append(finding.detail)
            action_count += 1

        evaluate_findings(
            config,
            "lane",
            schedule_findings(config, "lane", findings),
            on_decision=serve_one,  # type: ignore[arg-type]
        )

    assert served == ["finding alpha", "finding beta", "finding gamma"]

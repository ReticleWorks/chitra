"""Focused tests for persistent finding evaluation."""

from __future__ import annotations

from pathlib import Path

from chitra.detect import Finding
from chitra.monitord import evaluate_findings, resolve_config


def _finding(name: str) -> Finding:
    return Finding(
        detector="test",
        fingerprint_seed={"name": name},
        event_refs=(f"event-{name}",),
        unmet_item="done-1",
        expected_next_progress=f"make progress for {name}",
        detail=f"finding {name}",
    )


def test_monitor_pursues_more_than_five_findings_in_one_pass(tmp_path: Path) -> None:
    config = resolve_config(state_dir=tmp_path)
    findings = [_finding(f"finding-{index}") for index in range(6)]
    served: list[str] = []

    def serve(finding: Finding, _decision: object) -> None:
        served.append(finding.detail)

    evaluate_findings(
        config,
        "lane",
        findings,
        on_decision=serve,  # type: ignore[arg-type]
    )

    assert served == [item.detail for item in findings]

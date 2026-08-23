"""Tests for the append-only consequential decisions log."""

from __future__ import annotations

from pathlib import Path

import pytest

from chitra.decisions import DecisionEntry, append_decision, main, read_decisions
from chitra.plain_english import plain_english_issues


def _entry(**changes: str) -> DecisionEntry:
    values = {
        "decision_id": "decision-1",
        "at": "2026-08-13T08:40:00+00:00",
        "kind": "pause",
        "decision": "Pause every work session until the new session architecture is ready.",
        "basis": "Headless submissions made safe supervision and recovery too difficult.",
        "citation": "FLEET-STATE-PAUSE-20260813.md#fleet-state-at-operator-pause",
        "authority": "The operator ordered this pause on 2026-08-13.",
    }
    values.update(changes)
    return DecisionEntry.model_validate(values)


def test_append_is_ordered_and_does_not_rewrite_existing_bytes(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    first = append_decision(path, _entry())
    prefix = path.read_bytes()
    second = append_decision(path, _entry(decision_id="decision-2", kind="resume", decision="Resume the tested work sessions now."))

    assert path.read_bytes().startswith(prefix)
    assert read_decisions(path) == [first, second]


def test_required_basis_citation_and_authority_are_enforced() -> None:
    with pytest.raises(ValueError, match="basis"):
        _entry(basis="")
    with pytest.raises(ValueError, match="citation"):
        _entry(citation="")


def test_plain_english_lint_rejects_bare_codenames_jargon_and_fragments() -> None:
    assert plain_english_issues("F2", field="program")
    assert plain_english_issues("Move the lane after review", field="decision")
    assert plain_english_issues("Architecture ready for rollout", field="decision")
    assert not plain_english_issues("Move the work session after review.", field="decision")


def test_cli_add_and_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "decisions.jsonl"
    args = [
        "--log-path", str(path), "add", "--kind", "adjudication",
        "--decision", "Retire the obsolete approval request.",
        "--basis", "The approved route has already succeeded.",
        "--citation", "evidence/probe.json#result", "--authority", "The monitor applied the operator's standing guidance.",
        "--at", "2026-08-13T08:00:00+00:00",
    ]
    assert main(args) == 0
    capsys.readouterr()
    assert main(["--log-path", str(path), "list"]) == 0
    assert '"kind":"adjudication"' in capsys.readouterr().out

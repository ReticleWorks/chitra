"""Tests for the append-only consequential decisions log."""

from __future__ import annotations

import pytest

from chitra.decisions import DecisionEntry


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


def test_required_basis_citation_and_authority_are_enforced() -> None:
    with pytest.raises(ValueError, match="basis"):
        _entry(basis="")
    with pytest.raises(ValueError, match="citation"):
        _entry(citation="")

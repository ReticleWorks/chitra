"""Focused tests for exact canonical-choice evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from chitra.canonical_choices import CanonicalChoice, CanonicalChoicesPolicy, detect_canonical_choices
from chitra.journal import ByteRange, CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.monitord import resolve_config, run_detectors


def _event(event_id: str, tool_name: str, input_value: dict[str, object], *, cwd: str | None = None) -> CanonicalEvent:
    payload: dict[str, object] = {"tool_name": tool_name, "input": input_value}
    if cwd is not None:
        payload["cwd"] = cwd
    return CanonicalEvent(
        event_id=event_id,
        instance="canonical-choice-test",
        lane="lane-a.0.0",
        client=Client.CLAUDE,
        client_version="test",
        process_id=None,
        transcript=TranscriptIdentity(path="/transcript.jsonl", device=0, inode=0),
        session_id="session-a",
        resume_id=None,
        observed_at="2026-08-25T00:00:00Z",
        native_time=None,
        native_type="assistant",
        native_join_id=None,
        raw_byte_range=ByteRange(start=0, end=1),
        raw_sha256=None,
        normalized_type=CanonicalType.TOOL_CALL,
        payload_digest="d" * 64,
        normalizer_version="test",
        payload=payload,
        raw_record=None,
    )


def _policy() -> CanonicalChoicesPolicy:
    return CanonicalChoicesPolicy(
        choices={
            "legacy-doc": CanonicalChoice(
                kind="deprecated_path",
                subject="/workspace/project/legacy.md",
                canonical_value="/workspace/project/current.md",
            )
        }
    )


def test_registry_key_is_validated_independently_from_subject() -> None:
    policy = _policy()
    assert policy.choices["legacy-doc"].subject == "/workspace/project/legacy.md"

    with pytest.raises(ValueError, match="stable registry key"):
        CanonicalChoicesPolicy(
            choices={
                "legacy doc": CanonicalChoice(
                    kind="deprecated_path",
                    subject="/workspace/project/legacy.md",
                    canonical_value="/workspace/project/current.md",
                )
            }
        )


def test_deprecated_path_requires_exact_write_evidence_and_preserves_controls() -> None:
    events = (
        _event("write-old", "Write", {"file_path": "/workspace/project/legacy.md"}),
        _event("read-old", "Read", {"file_path": "/workspace/project/legacy.md"}),
        _event("write-sibling", "Write", {"file_path": "/workspace/project/legacy.md.bak"}),
        _event("write-approved", "Write", {"file_path": "/workspace/project/current.md"}),
        _event("relative-without-cwd", "Edit", {"file_path": "legacy.md"}),
        _event("relative-with-cwd", "Edit", {"file_path": "legacy.md"}, cwd="/workspace/project"),
    )

    findings = detect_canonical_choices(events, _policy())

    assert [finding.event_refs for finding in findings] == [("write-old",), ("relative-with-cwd",)]
    assert all("/workspace/project/current.md" in finding.expected_next_progress for finding in findings)


def test_non_path_choice_is_declared_but_not_inferred_from_free_text() -> None:
    policy = CanonicalChoicesPolicy(
        choices={
            "version-rule": CanonicalChoice(
                kind="pinned_version",
                subject="runtime",
                canonical_value="v1",
            )
        }
    )
    event = _event("text-only", "Write", {"file_path": "/workspace/project/file.md", "content": "runtime v0"})

    assert detect_canonical_choices((event,), policy) == []


def test_monitord_places_canonical_choice_findings_in_deterministic_order(tmp_path: Path) -> None:
    event = _event("write-old", "Write", {"file_path": "/workspace/project/legacy.md"})

    findings = run_detectors(
        resolve_config(state_dir=tmp_path),
        "lane-a.0.0",
        None,
        (event,),
        canonical_choices_policy=_policy(),
    )

    assert [finding.detector for finding in findings] == ["canonical_choices.deprecated_path"]

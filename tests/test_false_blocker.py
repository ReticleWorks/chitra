from __future__ import annotations

from pathlib import Path

import pytest

from chitra.detect import detect_false_blocker
from chitra.journal import ByteRange, CanonicalEvent, CanonicalType, Client, TranscriptIdentity
from chitra.monitord import evaluate_findings, resolve_config, run_detectors

LANE = "lane-a"


def _event(event_id: str, text: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        instance="test-instance",
        lane=LANE,
        client=Client.CLAUDE,
        client_version="test",
        process_id=None,
        transcript=TranscriptIdentity(path="/tmp/test-transcript.jsonl", device=0, inode=0),
        session_id="session-a",
        resume_id=None,
        observed_at="2026-08-25T12:00:00Z",
        native_time=None,
        native_type="assistant",
        native_join_id=None,
        raw_byte_range=ByteRange(start=0, end=1),
        raw_sha256=None,
        normalized_type=CanonicalType.FINAL_RESPONSE,
        payload_digest="d" * 64,
        normalizer_version="test",
        payload={"text": text},
        raw_record=None,
    )


def test_true_missing_absolute_path_is_not_a_finding(tmp_path: Path) -> None:
    missing = tmp_path / "not-present"

    assert detect_false_blocker([_event("event-1", f"I cannot continue because {missing} is missing.")]) == []


def test_existing_absolute_path_contradicts_a_missing_claim(tmp_path: Path) -> None:
    existing = tmp_path / "present"
    existing.touch()

    findings = detect_false_blocker([_event("event-1", f"I cannot continue because {existing} is missing.")])

    assert len(findings) == 1
    assert findings[0].detector == "false_blocker"
    assert findings[0].event_refs == ("event-1",)
    assert str(existing) in findings[0].detail


def test_nonempty_injected_environment_variable_contradicts_a_missing_claim() -> None:
    findings = detect_false_blocker(
        [_event("event-1", "The environment variable CHITRA_TEST_RESOURCE is missing.")],
        environment={"CHITRA_TEST_RESOURCE": "synthetic-value"},
    )

    assert len(findings) == 1
    assert findings[0].detector == "false_blocker"
    assert "synthetic-value" not in findings[0].detail
    assert "synthetic-value" not in str(findings[0].to_dict())


def test_empty_injected_environment_variable_is_not_a_finding() -> None:
    assert detect_false_blocker(
        [_event("event-1", "The environment variable CHITRA_TEST_RESOURCE is missing.")],
        environment={"CHITRA_TEST_RESOURCE": ""},
    ) == []


@pytest.mark.parametrize(
    "text",
    [
        "Credentials are missing.",
        "Is /tmp/chitra-resource missing?",
        "If /tmp/chitra-resource is missing, stop.",
        "The tool reported that /tmp/chitra-resource is missing.",
    ],
)
def test_ambiguous_or_broad_claims_abstain(text: str) -> None:
    assert detect_false_blocker([_event("event-1", text)]) == []


def test_permission_error_abstains_without_reading_the_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = tmp_path / "permission-denied"
    blocked.touch()
    original_stat = Path.stat

    def deny_stat(path: Path, *args: object, **kwargs: object):
        if path == blocked:
            raise PermissionError("synthetic permission denial")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny_stat)

    assert detect_false_blocker([_event("event-1", f"The resource {blocked} is missing.")]) == []


def test_monitord_routes_false_blocker_finding_to_existing_ladder(tmp_path: Path) -> None:
    existing = tmp_path / "present"
    existing.touch()
    config = resolve_config(state_dir=tmp_path)
    findings = run_detectors(
        config,
        LANE,
        None,
        (_event("event-1", f"I cannot continue because {existing} is missing."),),
    )

    assert [finding.detector for finding in findings] == ["false_blocker"]
    assert evaluate_findings(config, LANE, findings) == ["open"]

"""Tests for the backend-transfer verb and its chitra.goals.v2 linkage.

The protocol these cover was done by hand on 2026-08-16, after a Codex lane hit
a weekly hard cap and sat dead for roughly two days. Doing it by hand is what
made it depend on someone noticing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra.goals import (
    GoalNotFoundError,
    GoalRecord,
    GoalValidationError,
    get_goal,
    load_goals,
    successor_session_ref,
    transfer_goal,
    update_now,
    upsert_goal,
)
from chitra.goals import (
    GoalsSchemaNewerError as GoalStoreError,
)
from chitra.goals_cli import main

ORIGINAL = "tophand:gct-secret-broker:0.0"
RESUME_AT = "2026-08-20T03:37:33+00:00"
REASON = "rate-limit:codex-weekly-hard-cap"
DIGEST = "digest-01998f2c-handoff"


def _decode_records(output: str) -> list[dict[str, object]]:
    """Read the CLI's indented JSON records out of interleaved log lines.

    structlog writes to the same stream, so the records are picked out by the
    shape json.dumps(indent=2) gives them: a bare ``{`` opening a block and a
    bare ``}`` closing it.
    """
    records: list[dict[str, object]] = []
    block: list[str] = []
    for line in output.splitlines():
        if line == "{":
            block = [line]
        elif block:
            block.append(line)
            if line == "}":
                records.append(json.loads("\n".join(block)))
                block = []
    return records


def _enrol(root: Path, session_ref: str = ORIGINAL) -> GoalRecord:
    return upsert_goal(
        root,
        GoalRecord(
            session_ref=session_ref,
            goal="Land the durable GCT and 1Password secret broker rollout",
            done_when="the governed rollout playbook lands and the trust refresh is verified live",
            source="task-file governed-lanes/tophand/gct-secret-broker/lane-launch.json",
            status="working",
            intent="Replace the hand-held secret path with a governed broker the fleet can audit",
            scope="the gct secret broker rollout only",
            **enrollment_fields("the governed rollout playbook lands and the trust refresh is verified live"),
        ),
    )


def test_transfer_holds_the_original_and_scaffolds_a_successor(tmp_path: Path) -> None:
    _enrol(tmp_path)

    held, successor = transfer_goal(
        tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason=REASON, resume_at=RESUME_AT
    )

    assert held.status == "held"
    assert held.hold_reason == REASON
    assert held.resume_at == RESUME_AT
    assert held.transferred_to == "tophand:gct-secret-broker-xfer:0.0"

    assert successor.session_ref == "tophand:gct-secret-broker-xfer:0.0"
    assert successor.successor_of == ORIGINAL
    assert successor.status == "idle"


def test_strategic_fields_transfer_verbatim(tmp_path: Path) -> None:
    """A backend swap is tactical. The lane is doing the same work."""
    original = _enrol(tmp_path)

    _held, successor = transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason=REASON)

    assert successor.goal == original.goal
    assert successor.done_when == original.done_when
    assert successor.intent == original.intent
    assert successor.scope == original.scope
    # Only source grows, and only by the digest the successor needs to read.
    assert successor.source.startswith(original.source)
    assert successor.source.endswith(f"digest:{DIGEST}")


def test_both_records_land_or_neither_does(tmp_path: Path) -> None:
    _enrol(tmp_path)

    transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason=REASON, resume_at=RESUME_AT)

    stored = {record.session_ref: record for record in load_goals(tmp_path)}
    assert set(stored) == {ORIGINAL, "tophand:gct-secret-broker-xfer:0.0"}
    assert stored[ORIGINAL].transferred_to == "tophand:gct-secret-broker-xfer:0.0"
    assert stored["tophand:gct-secret-broker-xfer:0.0"].successor_of == ORIGINAL


def test_a_second_transfer_of_the_same_lane_is_refused(tmp_path: Path) -> None:
    """Two successors driving one branch is the shared-worktree collision."""
    _enrol(tmp_path)
    transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason=REASON)

    with pytest.raises(GoalValidationError, match="already transferred"):
        transfer_goal(tmp_path, ORIGINAL, to_backend="codex", digest=DIGEST, reason=REASON)


def test_a_successor_can_itself_be_transferred(tmp_path: Path) -> None:
    """Both backends capped in turn is rare but it is not impossible."""
    _enrol(tmp_path)
    _held, successor = transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason=REASON)

    _held2, second = transfer_goal(
        tmp_path, successor.session_ref, to_backend="codex", digest=DIGEST, reason="rate-limit:claude-7d"
    )

    assert second.session_ref == "tophand:gct-secret-broker-xfer-xfer:0.0"
    assert second.successor_of == successor.session_ref


def test_transfer_successor_update_preserves_identity_and_lane(tmp_path: Path) -> None:
    original = _enrol(tmp_path)
    _held, successor = transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason=REASON)

    updated = update_now(tmp_path, successor.session_ref, now="resumed after ownership reconciliation")

    assert updated.goal_id == original.goal_id
    assert updated.lane_id == original.lane_id
    assert updated.session_ref == successor.session_ref


def test_transfer_refuses_incomplete_or_unknown_input(tmp_path: Path) -> None:
    _enrol(tmp_path)

    with pytest.raises(GoalValidationError, match="to_backend"):
        transfer_goal(tmp_path, ORIGINAL, to_backend="gemini", digest=DIGEST, reason=REASON)
    with pytest.raises(GoalValidationError, match="digest"):
        transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest="  ", reason=REASON)
    with pytest.raises(GoalValidationError, match="reason"):
        transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason="")
    with pytest.raises(ValueError, match="resume_at"):
        transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason=REASON, resume_at="tuesday")
    with pytest.raises(GoalNotFoundError):
        transfer_goal(tmp_path, "tophand:absent:0.0", to_backend="claude", digest=DIGEST, reason=REASON)


def test_a_refused_transfer_writes_nothing(tmp_path: Path) -> None:
    original = _enrol(tmp_path)

    with pytest.raises(GoalValidationError):
        transfer_goal(tmp_path, ORIGINAL, to_backend="gemini", digest=DIGEST, reason=REASON)

    assert load_goals(tmp_path) == [original]


def test_successor_ref_skips_names_already_taken() -> None:
    assert successor_session_ref(ORIGINAL, []) == "tophand:gct-secret-broker-xfer:0.0"
    assert (
        successor_session_ref(ORIGINAL, ["tophand:gct-secret-broker-xfer:0.0"])
        == "tophand:gct-secret-broker-xfer2:0.0"
    )


def test_a_v1_document_still_loads_and_carries_empty_linkage(tmp_path: Path) -> None:
    """The monitor and the lane hosts do not upgrade in the same instant."""
    _enrol(tmp_path)
    path = tmp_path / "goals.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = "chitra.goals.v1"
    for record in payload["goals"]:
        record.pop("successor_of", None)
        record.pop("transferred_to", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_goals(tmp_path)

    assert [record.session_ref for record in loaded] == [ORIGINAL]
    assert loaded[0].successor_of == ""
    assert loaded[0].transferred_to == ""


def test_an_unknown_schema_is_refused_but_a_newer_v_n_reads_tolerantly(tmp_path: Path) -> None:
    _enrol(tmp_path)
    path = tmp_path / "goals.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = "not-a-chitra-goals-schema"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="goals.json schema must match"):
        load_goals(tmp_path)

    # A newer chitra.goals.v<N> document is readable by this package (the
    # v3/v4 outage class); writing it back is what stays refused.
    payload["schema"] = "chitra.goals.v9"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert [record.session_ref for record in load_goals(tmp_path)] == [ORIGINAL]
    with pytest.raises(GoalStoreError, match="newer than installed package schema"):
        transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason=REASON)


def test_transfer_starts_nothing(tmp_path: Path) -> None:
    """The verb writes records. chitra-goals check still gates launch."""
    _enrol(tmp_path)

    _held, successor = transfer_goal(tmp_path, ORIGINAL, to_backend="claude", digest=DIGEST, reason=REASON)

    # An idle scaffold, not a running lane: nothing may treat this as launched.
    assert successor.status == "idle"
    assert successor.last_verified == ""


def test_transfer_cli_prints_both_records(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _enrol(tmp_path)

    exit_code = main(
        [
            "transfer",
            "--root",
            str(tmp_path),
            "--session-ref",
            ORIGINAL,
            "--to-backend",
            "claude",
            "--digest",
            DIGEST,
            "--reason",
            REASON,
            "--resume-at",
            RESUME_AT,
        ]
    )

    assert exit_code == 0
    printed = _decode_records(capsys.readouterr().out)
    assert [record["session_ref"] for record in printed] == [ORIGINAL, "tophand:gct-secret-broker-xfer:0.0"]
    assert printed[0]["transferred_to"] == "tophand:gct-secret-broker-xfer:0.0"
    assert printed[1]["successor_of"] == ORIGINAL
    assert get_goal(tmp_path, ORIGINAL).transferred_to == "tophand:gct-secret-broker-xfer:0.0"


def test_transfer_cli_rejects_an_unknown_backend(tmp_path: Path) -> None:
    _enrol(tmp_path)

    with pytest.raises(SystemExit):
        main(
            [
                "transfer",
                "--root",
                str(tmp_path),
                "--session-ref",
                ORIGINAL,
                "--to-backend",
                "gemini",
                "--digest",
                DIGEST,
                "--reason",
                REASON,
            ]
        )

"""Tests for the fail-closed Chitra ownership provider."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _goal_fixtures import enrollment_fields

from chitra import ownership_provider as provider
from chitra.goals import GoalRecord
from chitra.ownership_provider import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    load_managed_state,
    managed_marker_for_state,
    ownership_result,
    read_json_line,
)

NOW = datetime(2026, 7, 15, 16, 0, tzinfo=UTC)
HOST = "host-a"
BOOT = "boot-a"


def _goal(
    session_ref: str,
    lane_id: str,
    *,
    goal_id: str = "",
    status: str = "working",
    successor_of: str = "",
    transferred_to: str = "",
) -> dict[str, object]:
    return GoalRecord(
        session_ref=session_ref,
        lane_id=lane_id,
        goal_id=goal_id,
        goal="Implement the complete bounded ownership authority contract safely",
        done_when="All focused ownership authority tests pass cleanly",
        source="task-file:test",
        status=status,
        enrolled_done_when="All focused ownership authority tests pass cleanly",
        enrolled_at="2026-07-15T15:00:00Z",
        created_at="2026-07-15T15:00:00Z",
        updated_at="2026-07-15T15:00:00Z",
        successor_of=successor_of,
        transferred_to=transferred_to,
        **enrollment_fields("All focused ownership authority tests pass cleanly"),
    ).to_dict()


def _write_state(
    root: Path,
    *,
    complete: bool = True,
    marker_host: str = HOST,
    heartbeat: datetime = NOW,
    generation: int = 17,
) -> tuple[Path, Path]:
    goals_path = root / "goals.json"
    marker_path = root / "goals.managed.json"
    document = {
        "schema": "chitra.goals.v1",
        "updated_at": "2026-07-15T16:00:00Z",
        "goals": [_goal("host-a:lane-one:0.0", "lane-one")],
    }
    raw = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    goals_path.write_bytes(raw)
    marker = managed_marker_for_state(
        raw,
        host_id=marker_host,
        boot_id=BOOT,
        generation=generation,
        manager_heartbeat_at=heartbeat,
        complete=complete,
    )
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    # The reader refuses a group- or world-writable state file, so these have to
    # carry a mode rather than inherit one. A host with umask 002 writes 0664
    # and every test here reads back state_unsafe -- which is the reader working
    # correctly and the fixture testing the umask it happened to run under.
    for path in (goals_path, marker_path):
        path.chmod(0o600)
    return goals_path, marker_path


def _write_document(root: Path, document: dict[str, object]) -> tuple[Path, Path]:
    goals_path = root / "goals.json"
    marker_path = root / "goals.managed.json"
    raw = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    goals_path.write_bytes(raw)
    marker = managed_marker_for_state(
        raw,
        host_id=HOST,
        boot_id=BOOT,
        generation=17,
        manager_heartbeat_at=NOW,
    )
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    for path in (goals_path, marker_path):
        path.chmod(0o600)
    return goals_path, marker_path


def _query(session_ref: str) -> dict[str, str]:
    return {
        "schema": "chitra.ownership.query.v1",
        "request_id": "request-1",
        "host_id": HOST,
        "boot_id": BOOT,
        "session_ref": session_ref,
    }


def test_missing_state_returns_non_authoritative_unknown(tmp_path: Path) -> None:
    response = ownership_result(
        _query("host-a:lane-one:0.0"),
        provider_instance_id="provider-instance",
        goals_path=tmp_path / "goals.json",
        marker_path=tmp_path / "goals.managed.json",
        expected_host_id=HOST,
        expected_boot_id=BOOT,
        now=NOW,
    )

    assert response["authoritative"] is False
    assert response["source"] == {
        # The schema this provider expects, which follows chitra.goals. The
        # document fixture above stays on v1 on purpose, to prove a host that
        # has not upgraded yet still reads authoritatively.
        "schema": "chitra.goals.v4",
        "generation": 0,
        "complete": False,
        "manager_heartbeat_at": "",
    }
    assert response["result"] == {
        "session_ref": "host-a:lane-one:0.0",
        "status": "unknown",
        "reason": "state_missing",
    }


def test_valid_complete_state_returns_exact_owned_and_unowned(tmp_path: Path) -> None:
    goals_path, marker_path = _write_state(tmp_path)
    common = {
        "provider_instance_id": "provider-instance",
        "goals_path": goals_path,
        "marker_path": marker_path,
        "expected_host_id": HOST,
        "expected_boot_id": BOOT,
        "now": NOW,
    }

    owned = ownership_result(_query("host-a:lane-one:0.0"), **common)
    unowned = ownership_result(_query("host-a:absent:0.0"), **common)

    assert owned["authoritative"] is True
    assert owned["result"] == {
        "session_ref": "host-a:lane-one:0.0",
        "status": "owned",
        "lane_id": "lane-one",
        "lane_generation": 1,
    }
    assert unowned["authoritative"] is True
    assert unowned["result"] == {"session_ref": "host-a:absent:0.0", "status": "unowned"}


def test_transfer_ownership_keeps_logical_lane_for_physical_successor(tmp_path: Path) -> None:
    predecessor_ref = "host-a:logical-lane:0.0"
    successor_ref = "host-a:logical-lane-xfer:0.0"
    document = {
        "schema": "chitra.goals.v4",
        "updated_at": "2026-07-15T16:00:00Z",
        "goals": [
            _goal(
                predecessor_ref,
                "logical-lane",
                goal_id="goal-transfer",
                status="held",
                transferred_to=successor_ref,
            ),
            _goal(
                successor_ref,
                "logical-lane",
                goal_id="goal-transfer",
                status="working",
                successor_of=predecessor_ref,
            ),
        ],
    }
    goals_path, marker_path = _write_document(tmp_path, document)

    response = ownership_result(
        _query(successor_ref),
        provider_instance_id="provider-instance",
        goals_path=goals_path,
        marker_path=marker_path,
        expected_host_id=HOST,
        expected_boot_id=BOOT,
        now=NOW,
    )

    assert response["authoritative"] is True
    assert response["result"] == {
        "session_ref": successor_ref,
        "status": "owned",
        "lane_id": "logical-lane",
        "lane_generation": 1,
    }


def test_v4_physical_lane_mismatch_without_transfer_is_malformed(tmp_path: Path) -> None:
    document = {
        "schema": "chitra.goals.v4",
        "updated_at": "2026-07-15T16:00:00Z",
        "goals": [_goal("host-a:physical-lane:0.0", "logical-lane", goal_id="goal-one")],
    }
    goals_path, marker_path = _write_document(tmp_path, document)

    response = ownership_result(
        _query("host-a:physical-lane:0.0"),
        provider_instance_id="provider-instance",
        goals_path=goals_path,
        marker_path=marker_path,
        expected_host_id=HOST,
        expected_boot_id=BOOT,
        now=NOW,
    )

    assert response["authoritative"] is False
    assert response["result"]["reason"] == "state_malformed"


def test_duplicate_active_goal_id_is_malformed(tmp_path: Path) -> None:
    document = {
        "schema": "chitra.goals.v4",
        "updated_at": "2026-07-15T16:00:00Z",
        "goals": [
            _goal("host-a:lane-one:0.0", "lane-one", goal_id="goal-duplicate"),
            _goal("host-a:lane-two:0.0", "lane-two", goal_id="goal-duplicate"),
        ],
    }
    goals_path, marker_path = _write_document(tmp_path, document)

    response = ownership_result(
        _query("host-a:lane-one:0.0"),
        provider_instance_id="provider-instance",
        goals_path=goals_path,
        marker_path=marker_path,
        expected_host_id=HOST,
        expected_boot_id=BOOT,
        now=NOW,
    )

    assert response["authoritative"] is False
    assert response["result"]["reason"] == "state_malformed"


@pytest.mark.parametrize(
    ("state_kwargs", "reason"),
    [
        ({"heartbeat": NOW - timedelta(minutes=2)}, "state_stale"),
        ({"complete": False}, "state_partial"),
        ({"marker_host": "other-host"}, "state_host_mismatch"),
    ],
)
def test_stale_partial_and_host_mismatched_state_fail_unknown(
    tmp_path: Path,
    state_kwargs: dict[str, object],
    reason: str,
) -> None:
    goals_path, marker_path = _write_state(tmp_path, **state_kwargs)  # type: ignore[arg-type]

    response = ownership_result(
        _query("host-a:lane-one:0.0"),
        provider_instance_id="provider-instance",
        goals_path=goals_path,
        marker_path=marker_path,
        expected_host_id=HOST,
        expected_boot_id=BOOT,
        now=NOW,
    )

    assert response["authoritative"] is False
    assert response["result"] == {
        "session_ref": "host-a:lane-one:0.0",
        "status": "unknown",
        "reason": reason,
    }


def test_state_is_not_complete_without_current_digest_bound_marker(tmp_path: Path) -> None:
    goals_path, marker_path = _write_state(tmp_path)
    marker_path.unlink()
    missing = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
    )
    _, marker_path = _write_state(tmp_path)
    goals_path.write_text("{}\n", encoding="utf-8")
    changed = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
    )

    assert missing.authoritative is False and missing.reason == "managed_marker_missing"
    assert changed.authoritative is False and changed.reason == "state_digest_mismatch"


def test_state_files_are_bounded_regular_and_owned_when_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goals_path, marker_path = _write_state(tmp_path)
    monkeypatch.setattr(provider, "MAX_STATE_BYTES", 100)
    oversized = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
    )
    assert oversized.authoritative is False and oversized.reason == "state_oversized"

    monkeypatch.setattr(provider, "MAX_STATE_BYTES", 2 * 1024 * 1024)
    goals_path.unlink()
    goals_path.symlink_to(marker_path)
    unsafe = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
    )
    assert unsafe.authoritative is False and unsafe.reason == "state_unsafe"

    goals_path.unlink()
    goals_path, marker_path = _write_state(tmp_path)
    wrong_owner = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
        expected_owner_uid=os.geteuid() + 1,
    )
    assert wrong_owner.authoritative is False and wrong_owner.reason == "state_untrusted"


def test_generation_fence_rejects_rollback_or_same_generation_rewrite(tmp_path: Path) -> None:
    goals_path, marker_path = _write_state(tmp_path, generation=17)
    fence_path = tmp_path / "ownership-generation.json"
    first = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
        generation_fence_path=fence_path,
    )
    assert first.authoritative is True

    goals_path, marker_path = _write_state(tmp_path, generation=16)
    rollback = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
        generation_fence_path=fence_path,
    )
    assert rollback.authoritative is False and rollback.reason == "state_generation_rollback"

    document = json.loads(goals_path.read_text(encoding="utf-8"))
    document["goals"][0]["goal"] = "A different record must not reuse a fenced generation"
    changed_raw = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    goals_path.write_bytes(changed_raw)
    marker = managed_marker_for_state(
        changed_raw,
        host_id=HOST,
        boot_id=BOOT,
        generation=17,
        manager_heartbeat_at=NOW,
    )
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    rewritten = load_managed_state(
        goals_path=goals_path,
        marker_path=marker_path,
        host_id=HOST,
        boot_id=BOOT,
        now=NOW,
        generation_fence_path=fence_path,
    )
    assert rewritten.authoritative is False and rewritten.reason == "state_generation_rollback"


def _read_args(goals_path: Path, marker_path: Path) -> dict[str, object]:
    return {
        "provider_instance_id": "provider-instance",
        "goals_path": goals_path,
        "marker_path": marker_path,
        "expected_host_id": HOST,
        "expected_boot_id": BOOT,
        "now": NOW,
    }


def test_newer_goals_schema_and_future_fields_stay_authoritative(tmp_path: Path) -> None:
    """P6 forward compatibility: the provider accepts every schema
    chitra.goals accepts -- including chitra.goals.v5 from a newer writer --
    and ignores unknown top-level or per-record fields instead of turning
    managed state into unknown/malformed during the exact version-skew
    incident P6 exists to handle. Duplicate keys, sizes, digests, required
    fields, and status checks are unchanged."""
    goals_path, marker_path = _write_state(tmp_path)
    payload = json.loads(goals_path.read_bytes())
    payload["schema"] = "chitra.goals.v5"
    payload["future_v4_envelope"] = {"unknown": True}
    record = payload["goals"][0]
    record["future_v4_field"] = {"nested": [1, 2, 3]}
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    goals_path.write_bytes(raw)
    goals_path.chmod(0o600)
    marker_path.write_text(
        json.dumps(managed_marker_for_state(raw, host_id=HOST, boot_id=BOOT, generation=17, manager_heartbeat_at=NOW)),
        encoding="utf-8",
    )
    marker_path.chmod(0o600)

    owned = ownership_result(_query("host-a:lane-one:0.0"), **_read_args(goals_path, marker_path))
    unowned = ownership_result(_query("host-a:absent:0.0"), **_read_args(goals_path, marker_path))

    assert owned["authoritative"] is True
    assert owned["result"] == {
        "session_ref": "host-a:lane-one:0.0",
        "status": "owned",
        "lane_id": "lane-one",
        "lane_generation": 1,
    }
    assert unowned["authoritative"] is True
    assert unowned["result"] == {"session_ref": "host-a:absent:0.0", "status": "unowned"}


def test_non_family_label_and_missing_required_field_stay_malformed(tmp_path: Path) -> None:
    """Tolerance is for newer versions of the same schema family only: an
    arbitrary label, or a record missing a required canonical field, still
    fails closed to non-authoritative unknown."""
    goals_path, marker_path = _write_state(tmp_path)

    def _rewrite(payload: dict[str, object]) -> None:
        raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        goals_path.write_bytes(raw)
        goals_path.chmod(0o600)
        marker_path.write_text(
            json.dumps(managed_marker_for_state(raw, host_id=HOST, boot_id=BOOT, generation=17, manager_heartbeat_at=NOW)),
            encoding="utf-8",
        )
        marker_path.chmod(0o600)

    payload = json.loads(goals_path.read_bytes())
    payload["schema"] = "some.other.thing.v9"
    _rewrite(payload)
    response = ownership_result(_query("host-a:lane-one:0.0"), **_read_args(goals_path, marker_path))
    assert response["authoritative"] is False
    assert response["result"]["reason"] == "state_malformed"

    payload["schema"] = "chitra.goals.v3"
    del payload["goals"][0]["source"]
    _rewrite(payload)
    missing_required = ownership_result(_query("host-a:lane-one:0.0"), **_read_args(goals_path, marker_path))
    assert missing_required["authoritative"] is False
    assert missing_required["result"]["reason"] == "state_malformed"


def test_query_requires_exact_fields_and_canonical_session_ref(tmp_path: Path) -> None:
    invalid = _query("host-a:lane-one") | {"operation": "pause"}
    with pytest.raises(ProtocolError, match="fields must match"):
        ownership_result(
            invalid,
            provider_instance_id="provider-instance",
            goals_path=tmp_path / "goals.json",
            marker_path=tmp_path / "goals.managed.json",
            expected_host_id=HOST,
            expected_boot_id=BOOT,
            now=NOW,
        )


def test_json_line_reader_rejects_more_than_64_kib() -> None:
    class _ChunkedConnection:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def recv(self, size: int) -> bytes:
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk

    connection = _ChunkedConnection(b'"' + b"x" * MAX_MESSAGE_BYTES + b'"\n')
    with pytest.raises(ProtocolError, match="64 KiB"):
        read_json_line(connection)  # type: ignore[arg-type]

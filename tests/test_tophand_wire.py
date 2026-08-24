from __future__ import annotations

import json
from pathlib import Path

from chitra.tophand_wire import TOPHAND_OPERATION_SCHEMA, request_digest, request_payload


def test_operation_fixture_freezes_the_shared_create_projection() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "tophand-operation-v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    request = fixture["request"]

    assert fixture["schema"] == TOPHAND_OPERATION_SCHEMA
    assert request_payload("create_or_resume", {**request, "operation_id": "private"}) == request
    assert fixture["payload_digest"] == request_digest("create_or_resume", request)


def test_create_projection_binds_the_full_resume_envelope() -> None:
    request = {
        "session_ref": "tophand:lane-a",
        "provider_session_id": "tophand:lane-a:4",
        "context_ref": "completion-checkpoint",
        "goal_id": "goal-a",
        "goal_version": 3,
        "resume_after_close": True,
        "close_operation_id": "close-1",
        "owner_process": {"target_pid": 10, "target_start_token": "start-10"},
        "resume_token": "resume-token",
    }

    assert request_payload("create_or_resume", request) == request
    assert request_digest("create_or_resume", request) != request_digest(
        "create_or_resume", {key: request[key] for key in ("session_ref", "provider_session_id", "context_ref")}
    )

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

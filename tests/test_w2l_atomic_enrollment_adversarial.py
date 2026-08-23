"""W2l regression: a replacement nonce invalidates a parsed-but-uncommitted result.

Nonce verification, interview provenance/item construction, goal enrollment,
and nonce consumption must happen inside one lock-governed transaction.  This
test pauses an old second ``set`` call after its stale result has been parsed
but before its goal commit lands, issues a changed first-call set request in
that window (which replaces the nonce), and then resumes the old call.  The
stale result must fail without storing the original goal and without
clobbering the replacement nonce back to a consumed record.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from chitra import goals as goal_store
from chitra.goals_cli import main as cli_main

SESSION_REF = "host:w2l-atomic-enrollment:0.0"


def _set_command(root: Path, goal: str) -> list[str]:
    return [
        "set",
        "--root",
        str(root),
        "--session-ref",
        SESSION_REF,
        "--goal",
        goal,
        "--source",
        "task-file:/tmp/w2l-atomic-enrollment.md",
    ]


def _interview_result(path: Path, nonce_record: dict[str, object], done_when: str) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "INTERVIEW_RESULT",
                "nonce": nonce_record["nonce"],
                "receipt_name": nonce_record["receipt_name"],
                "answers": {
                    "intent": {
                        "answer": "Enroll one immutable goal from verified interview answers.",
                        "provenance": "operator:test",
                    },
                    "done_when": {"answer": done_when, "provenance": "operator:test"},
                    "out_of_scope": {
                        "answer": "All unrelated actions remain excluded.",
                        "provenance": "operator:test",
                    },
                    "constraints": {
                        "answer": "Nonce verification and enrollment must be atomic.",
                        "provenance": "operator:test",
                    },
                },
                "enrolled_done_when_items": [
                    {
                        "id": "done-1",
                        "text": done_when,
                        "validator": "pytest",
                        "required_receipt": "tests-green",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_new_nonce_invalidates_a_parsed_but_uncommitted_old_result(tmp_path: Path) -> None:
    old_goal = "Enroll the original atomic validation goal safely now."
    new_goal = "Enroll the replacement atomic validation goal safely now."
    done_when = "Every required receipt closes its exact frozen item."
    old_command = _set_command(tmp_path, old_goal)

    assert cli_main(old_command) == 2
    nonce_path = next((tmp_path / "goal-interviews").glob("*.json"))
    old_nonce_record: dict[str, object] = json.loads(nonce_path.read_text(encoding="utf-8"))
    old_result = tmp_path / "old-result.json"
    _interview_result(old_result, old_nonce_record, done_when)

    outcome: list[int] = []
    parsed = threading.Event()
    resume = threading.Event()
    original_upsert = goal_store._upsert_goal_locked

    def paused_upsert(*args: object, **kwargs: object) -> goal_store.GoalRecord:
        parsed.set()
        if not resume.wait(30):
            raise RuntimeError("interleaving did not resume before the pause deadline")
        return original_upsert(*args, **kwargs)  # type: ignore[arg-type]

    goal_store._upsert_goal_locked = paused_upsert  # type: ignore[assignment]
    try:
        worker = threading.Thread(
            target=lambda: outcome.append(
                cli_main([*old_command, "--interview-result", str(old_result)])
            )
        )
        worker.start()
        assert parsed.wait(30)

        new_command = _set_command(tmp_path, new_goal)
        assert cli_main(new_command) == 2
        new_nonce_record: dict[str, object] = json.loads(nonce_path.read_text(encoding="utf-8"))
        assert new_nonce_record["nonce"] != old_nonce_record["nonce"]
        assert not new_nonce_record.get("consumed_at")

        resume.set()
        worker.join(30)
        assert not worker.is_alive()
    finally:
        goal_store._upsert_goal_locked = original_upsert  # type: ignore[assignment]

    assert goal_store.get_goal(tmp_path, SESSION_REF) is None, "the stale old interview result must not store a goal"
    final_nonce_record: dict[str, object] = json.loads(nonce_path.read_text(encoding="utf-8"))
    assert final_nonce_record["nonce"] == new_nonce_record["nonce"], "the replacement nonce must survive"
    assert not final_nonce_record.get("consumed_at"), "the replacement nonce must not be marked consumed"
    assert outcome != [0], (
        "stale old interview result was accepted after a replacement nonce was issued; "
        f"outcome={outcome!r}"
    )

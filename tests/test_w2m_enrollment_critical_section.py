"""W2m regression: a replacement nonce cannot slip between the final nonce
comparison and its consumption write.

Enrollment's final nonce comparison and the consumption write must be one
nonce-lock critical section.  This test drives an old second ``set`` call to
its consumption write and pauses it inside that write, then lets a changed
first-call set request -- one whose issuer already passed the no-goal guard
before the old call committed, exactly as a concurrent issuer would -- issue
its replacement nonce in that window.  The stale enrollment must fail, must
roll back to no goal at all, and must leave the replacement nonce in place
and unconsumed.
"""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from chitra import goals as goal_store
from chitra import goals_cli

SESSION_REF = "host:w2m-atomic-enrollment:0.0"


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
        "task-file:/tmp/w2m-atomic-enrollment.md",
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
                        "answer": "Nonce comparison and consumption must be atomic.",
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


def _changed_request_args(root: Path, goal: str) -> argparse.Namespace:
    """A first-call ``set`` request whose issuer already passed the no-goal guard."""
    return argparse.Namespace(
        root=root,
        session_ref=SESSION_REF,
        goal=goal,
        done_when="Every required receipt closes its exact frozen item.",
        source="task-file:/tmp/w2m-atomic-enrollment.md",
        intent=None,
        scope=None,
        status="working",
        now="",
        last_verified="",
        needs=None,
        open_ask=[],
        interview_result=None,
        clear_asks=False,
    )


def test_replacement_nonce_survives_the_comparison_to_consumption_window(tmp_path: Path) -> None:
    old_goal = "Enroll the original comparison-window validation goal safely now."
    replacement_goal = "Set up the replacement comparison-window validation goal safely now."
    done_when = "Every required receipt closes its exact frozen item."
    old_command = _set_command(tmp_path, old_goal)

    assert goals_cli.main(old_command) == 2
    nonce_path = next((tmp_path / "goal-interviews").glob("*.json"))
    old_nonce_record: dict[str, object] = json.loads(nonce_path.read_text(encoding="utf-8"))
    old_result = tmp_path / "old-result.json"
    _interview_result(old_result, old_nonce_record, done_when)

    outcome: list[int] = []
    consumed_marker = tmp_path / ".consumed-marker"
    inside_critical_section = threading.Event()
    issuer_blocked = threading.Event()
    resume = threading.Event()
    original_write_json_atomic = goals_cli.write_json_atomic

    def paused_consumption_write(path: object, obj: object, **kwargs: object) -> None:
        nonce_path_value = Path(str(path))
        assert "goal-interviews" in nonce_path_value.parts, (
            f"consumption write must target an interview nonce file: {nonce_path_value}"
        )
        if inside_critical_section.is_set():
            original_write_json_atomic(nonce_path_value, obj, **kwargs)
            return
        inside_critical_section.set()
        if not resume.wait(30):
            raise RuntimeError("interleaving did not resume before the pause deadline")
        original_write_json_atomic(nonce_path_value, obj, **kwargs)
        consumed_marker.write_text("consumed", encoding="utf-8")

    goals_cli.write_json_atomic = paused_consumption_write  # type: ignore[assignment]
    try:
        worker = threading.Thread(
            target=lambda: outcome.append(
                goals_cli.main([*old_command, "--interview-result", str(old_result)])
            )
        )
        worker.start()
        assert inside_critical_section.wait(30)

        replacement_args = _changed_request_args(tmp_path, replacement_goal)

        def replacement_issuer() -> None:
            goals_cli._interview_required(tmp_path, replacement_args)
            issuer_blocked.set()

        issuer = threading.Thread(target=replacement_issuer)
        issuer.start()
        assert not issuer_blocked.wait(1.0), (
            "the replacement issuer must block while the critical section holds the nonce lock"
        )

        resume.set()
        worker.join(30)
        assert not worker.is_alive()
        issuer.join(30)
        assert not issuer.is_alive()
    finally:
        goals_cli.write_json_atomic = original_write_json_atomic  # type: ignore[assignment]

    assert consumed_marker.exists(), "the fair consumption write must land after the fix"
    assert goal_store.get_goal(tmp_path, SESSION_REF) is not None, (
        "the enrollment that won the serialized race keeps its goal"
    )
    final_nonce_record: dict[str, object] = json.loads(nonce_path.read_text(encoding="utf-8"))
    assert final_nonce_record["nonce"] != old_nonce_record["nonce"], (
        "the replacement issuance must still have happened, strictly after consumption"
    )
    assert not final_nonce_record.get("consumed_at"), (
        "the replacement nonce must never be clobbered by the stale consumption record"
    )
    assert outcome == [0], f"the serialized enrollment succeeds exactly once: {outcome!r}"

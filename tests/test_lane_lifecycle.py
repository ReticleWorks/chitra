"""Tests for Chitra's durable four-state lane lifecycle."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chitra.recovery import (
    capture_worktree_binding,
    get_lane_lifecycle,
    load_lane_lifecycle_records,
    load_worktree_checkpoints,
    transition_lane_lifecycle,
)


def _run(workdir: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(workdir), *args], check=True, capture_output=True, text=True)


def _worktree(tmp_path: Path) -> Path:
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    _run(workdir, "init")
    _run(workdir, "config", "user.email", "chitra-test@example.test")
    _run(workdir, "config", "user.name", "Chitra Test")
    (workdir / "README.md").write_text("initial\n", encoding="utf-8")
    _run(workdir, "add", "README.md")
    _run(workdir, "commit", "-m", "initial")
    return workdir


def _activate(root: Path, workdir: Path) -> None:
    transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="active",
        binding=capture_worktree_binding(workdir),
        resume_note="Begin the enrolled work.",
        unfinished_work=True,
    )


def test_pause_resume_has_only_four_public_states_and_exact_checkpoint_validation(tmp_path: Path) -> None:
    root = tmp_path / "state"
    workdir = _worktree(tmp_path)
    _activate(root, workdir)

    paused = transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="paused",
        binding=capture_worktree_binding(workdir),
        resume_note="Pause enforcement until the scheduled window.",
    )
    assert paused.state == "paused"
    assert paused.enforcement_enabled is False
    assert paused.provider_session_retained is True

    resumed = transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="active",
        binding=capture_worktree_binding(workdir),
        resume_note="Resume from the saved checkpoint.",
    )
    assert resumed.state == "active"
    assert resumed.enforcement_enabled is True
    assert [item.state for item in load_lane_lifecycle_records(root)] == ["active", "paused", "active"]
    assert [item.action for item in load_worktree_checkpoints(root)] == ["active", "paused", "resume"]

    (workdir / "README.md").write_text("drift\n", encoding="utf-8")
    transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="paused",
        binding=capture_worktree_binding(workdir),
        resume_note="Pause with the dirty worktree preserved.",
    )
    (workdir / "new.txt").write_text("untracked drift\n", encoding="utf-8")
    with pytest.raises(ValueError, match="binding drifted.*untracked_digest"):
        transition_lane_lifecycle(
            root,
            session_ref="host:lane:0.0",
            target="active",
            binding=capture_worktree_binding(workdir),
            resume_note="Attempt a drifted resume.",
        )


def test_shelve_retains_memory_without_an_archive_alias(tmp_path: Path) -> None:
    root = tmp_path / "state"
    workdir = _worktree(tmp_path)
    _activate(root, workdir)

    shelved = transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="shelved",
        binding=capture_worktree_binding(workdir),
        resume_note="Keep the goal and worktree for later.",
    )
    assert shelved.state == "shelved"
    assert shelved.provider_session_retained is False
    assert get_lane_lifecycle(root, "host:lane:0.0") == shelved
    assert (workdir / "README.md").exists()
    assert "archived" not in {item.state for item in load_lane_lifecycle_records(root)}


def test_close_requires_independent_completion(tmp_path: Path) -> None:
    root = tmp_path / "state"
    workdir = _worktree(tmp_path)
    _activate(root, workdir)

    with pytest.raises(ValueError, match="independent completion"):
        transition_lane_lifecycle(
            root,
            session_ref="host:lane:0.0",
            target="closed",
            binding=capture_worktree_binding(workdir),
            resume_note="Close the lane.",
        )

    closed = transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="closed",
        binding=capture_worktree_binding(workdir),
        resume_note="Independent verifier confirmed every done condition.",
        independently_completed=True,
        unfinished_work=False,
    )
    assert closed.state == "closed"
    assert closed.enforcement_enabled is False
    with pytest.raises(ValueError, match="cannot transition"):
        transition_lane_lifecycle(
            root,
            session_ref="host:lane:0.0",
            target="active",
            binding=capture_worktree_binding(workdir),
            resume_note="Closed lanes cannot resume.",
        )


def test_resume_accepts_verified_append_only_transcript_and_rejects_mutation(tmp_path: Path) -> None:
    root = tmp_path / "state"
    workdir = _worktree(tmp_path)
    transcript = tmp_path / "lane.log"
    transcript.write_text("first turn\n", encoding="utf-8")
    _activate(root, workdir)
    transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="paused",
        binding=capture_worktree_binding(workdir, transcript_path=transcript),
        resume_note="Pause with the retained session transcript checkpointed.",
    )

    transcript.write_text("first turn\nsecond turn\n", encoding="utf-8")
    resumed = transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="active",
        binding=capture_worktree_binding(workdir, transcript_path=transcript),
        resume_note="Resume after ordinary append-only transcript growth.",
    )
    assert resumed.previous_state == "paused"

    paused_again = transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="paused",
        binding=capture_worktree_binding(workdir, transcript_path=transcript),
        resume_note="Checkpoint the second retained session state.",
    )
    assert paused_again.state == "paused"
    transcript.write_text("short\n", encoding="utf-8")
    with pytest.raises(ValueError, match="binding drifted.*transcript_cursor"):
        transition_lane_lifecycle(
            root,
            session_ref="host:lane:0.0",
            target="active",
            binding=capture_worktree_binding(workdir, transcript_path=transcript),
            resume_note="Reject replacement rather than resuming the wrong transcript.",
        )
    transcript.write_text("FIRST turn\nsecond turn\nthird turn\n", encoding="utf-8")
    with pytest.raises(ValueError, match="binding drifted.*transcript_sha256"):
        transition_lane_lifecycle(
            root,
            session_ref="host:lane:0.0",
            target="active",
            binding=capture_worktree_binding(workdir, transcript_path=transcript),
            resume_note="Reject a changed transcript prefix even when it grew.",
        )


def test_transition_request_id_is_idempotent_and_rejects_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "state"
    workdir = _worktree(tmp_path)
    binding = capture_worktree_binding(workdir)
    first = transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="active",
        binding=binding,
        resume_note="Begin the Kai-requested lane transition.",
        request_id="kai-request-001",
    )
    retry = transition_lane_lifecycle(
        root,
        session_ref="host:lane:0.0",
        target="active",
        binding=binding,
        resume_note="Retry after the caller lost the first response.",
        request_id="kai-request-001",
    )
    assert retry == first
    assert len(load_lane_lifecycle_records(root)) == 1
    assert len(load_worktree_checkpoints(root)) == 1

    with pytest.raises(ValueError, match="request kai-request-001 conflicts"):
        transition_lane_lifecycle(
            root,
            session_ref="host:lane:0.0",
            target="paused",
            binding=binding,
            resume_note="A conflicting retry must not create another checkpoint.",
            request_id="kai-request-001",
        )

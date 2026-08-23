"""Adversarial production-entrypoint tests for canonical recovery wiring.

These tests deliberately exercise the shipped daemon seams rather than calling
an alternate recovery implementation.  The supervisor and provider-resolver
symbols are supplied by the canonical recovery integration; when that
integration is not present yet, the tests fail with an explicit contract
message instead of silently falling back to a local policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _goal_fixtures import enrollment_fields, ingest_passing_receipt

import chitra.recovery as recovery
from chitra import dispatchd
from chitra.goals import GoalRecord, get_goal, upsert_goal
from chitra.joined_lane import JoinedLaneStore, ReconcileReport
from chitra.lane_config import LaneCredentials, LaneSpec
from chitra.provider_protocol import (
    ProviderName,
    ProviderState,
    ProviderStatus,
    ReadUpdatesResult,
    SendRequest,
)
from chitra.session_contract import (
    JoinedLaneRecord,
    LaneUpdate,
    ProviderCapabilities,
    ProviderIdentity,
    RoadmapStep,
)
from chitra.watchd import ReviewFailure, Watchd, WatchdConfig

NOW = datetime(2026, 8, 23, 15, tzinfo=UTC)
_DEFERRAL_TURN = "Can you run npm install on tophand for me?\nI will pick it up once it is there.\n❯\n"


def _lane_record() -> JoinedLaneRecord:
    update = LaneUpdate(
        lane_id="lane-a",
        goal_id="goal-a",
        session_ref="tophand:lane-a:1",
        goal_version=1,
        sequence=1,
        observed_at=NOW.isoformat(),
        plan_version=1,
        steps=(RoadmapStep(id="implement", status="active", owner="lane-manager"),),
        current_action="Implement the enrolled change",
        next_action="Run the focused acceptance check",
    )
    return JoinedLaneRecord(
        lane_id="lane-a",
        goal_id="goal-a",
        goal_version=1,
        session_ref="tophand:lane-a:1",
        provider=ProviderIdentity(
            kind="tophand",
            handle="tophand-lane-a",
            instance_id="instance-a",
            generation=1,
            capabilities=ProviderCapabilities.from_supported(("send", "checkpoint", "create_or_resume", "read_updates")),
        ),
        current_update=update,
    )


def _goal(root: Path, *, session_ref: str = "tophand:lane-a:1") -> GoalRecord:
    return upsert_goal(
        root,
        GoalRecord(
            session_ref=session_ref,
            # The goal store derives the lane from the durable session name.
            lane_id="",
            intent="Deliver the enrolled change while preserving the declared scope and constraints.",
            goal="Ship and verify the enrolled change for this lane.",
            done_when="The focused acceptance check passes with durable evidence.",
            scope="The enrolled lane worktree and its acceptance checks only.",
            source="test-file:/tmp/recovery-wiring",
            status="working",
            **enrollment_fields("The focused acceptance check passes with durable evidence."),
        ),
    )


def _lane(identifier: str, root: Path) -> LaneSpec:
    return LaneSpec(
        identifier=identifier,
        account=identifier,
        uid=1000 if identifier == "alpha" else 1001,
        home=root / f"{identifier}-home",
        workdir=root / f"{identifier}-workdir",
        config_dir=root / f"{identifier}-config",
        state_dir=root / f"{identifier}-state",
        tmux_socket=root / f"{identifier}.sock",
        tmux_session=identifier,
        credentials=LaneCredentials(
            claude_credentials=root / f"{identifier}-credentials.json",
            ssh_dispatch_key=root / f"{identifier}-dispatch.key",
        ),
    )


class _LostReplyProvider:
    """Canonical provider double: the first send has no durable reply."""

    provider_name = ProviderName.TOPHAND
    capabilities = ProviderCapabilities.from_supported(("send", "checkpoint", "create_or_resume", "read_updates"))

    def __init__(self) -> None:
        self.send_operation_ids: list[str] = []
        self.read_updates_calls = 0

    def send(self, request: SendRequest) -> object:
        self.send_operation_ids.append(request.operation_id)
        raise RuntimeError("provider response unavailable")

    def read_updates(self, cursor: str | None = None) -> ReadUpdatesResult:
        self.read_updates_calls += 1
        return ReadUpdatesResult(
            requested_cursor=cursor,
            next_cursor=cursor or "0",
            updates=(),
            provider_available=True,
            complete=True,
        )

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            provider=ProviderName.TOPHAND,
            state=ProviderState.IDLE,
            provider_session_id="tophand:lane-a:1",
            generation=1,
            fresh=True,
            provider_available=True,
        )

    def checkpoint(self, request: object) -> object:
        raise AssertionError(f"checkpoint was reached before durable recovery re-entry: {request!r}")

    def create_or_resume(self, request: object) -> object:
        raise AssertionError(f"relaunch was reached before durable recovery re-entry: {request!r}")


def test_dispatch_reconciles_each_lane_before_any_recovery_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery nudge is behind the same barrier as ordinary dispatch."""
    events: list[str] = []
    supervisor = object()

    def reconcile() -> ReconcileReport:
        events.append("reconcile")
        return ReconcileReport(())

    def run_supervision(candidate: object) -> tuple[object, ...]:
        assert candidate is supervisor
        assert events == ["reconcile"], "recovery must not send before the lane barrier"
        events.append("recovery-send")
        return ()

    monkeypatch.setattr(dispatchd, "run_recovery_supervision", run_supervision, raising=False)
    try:
        dispatchd.run_once(
            tmp_path / "queue",
            reconciliation_gate=reconcile,
            recovery_supervisor=supervisor,
        )
    except TypeError as exc:
        pytest.fail(f"dispatchd lacks the canonical recovery_supervisor seam: {exc}")

    assert events == ["reconcile", "recovery-send"]


def test_restarted_supervisor_reconciles_pending_operation_without_duplicate_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh supervisor observes the same pending operation, never resends it."""
    supervisor_type = getattr(recovery, "RecoverySupervisor", None)
    if supervisor_type is None:
        pytest.fail("canonical RecoverySupervisor is not committed; do not add a second recovery controller")

    record = _lane_record()
    _goal(tmp_path)
    store = JoinedLaneStore(tmp_path)
    store.create(record)
    recovery.RecoveryEngine(state_root=tmp_path).schedule(
        record,
        "isolated-review:behavior-hash",
        now=NOW,
        reason="The isolated reviewer was unavailable",
        wake_condition="isolated reviewer availability or a material lane update",
    )
    provider = _LostReplyProvider()
    monkeypatch.setattr(recovery, "get_goal", lambda _root, _session_ref: get_goal(tmp_path, "tophand:lane-a:1"))

    def resolve(candidate: JoinedLaneRecord) -> object:
        assert candidate.lane_id == "lane-a"
        return provider

    first_supervisor = supervisor_type(tmp_path, resolve, goal_root=tmp_path)
    first = first_supervisor.run_once(now=NOW)
    assert len(first) == 1
    persisted = store.require("lane-a")
    assert persisted.pending_operation is not None
    operation_id = persisted.pending_operation.operation_id
    assert provider.send_operation_ids == [operation_id]

    restarted_supervisor = supervisor_type(tmp_path, resolve, goal_root=tmp_path)
    second = restarted_supervisor.run_once(now=NOW + timedelta(minutes=10))
    assert len(second) == 1
    reloaded = store.require("lane-a")
    assert reloaded.pending_operation is not None
    assert reloaded.pending_operation.operation_id == operation_id
    assert provider.send_operation_ids == [operation_id]
    assert provider.read_updates_calls >= 1


def test_watchd_reviewer_failure_calls_canonical_run_recovery_check_without_user_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped reviewer outage path delegates through the canonical seam."""
    goal = _goal(tmp_path, session_ref="localhost:fleet:0.0")
    ingest_passing_receipt(tmp_path, goal.session_ref)
    calls: list[tuple[JoinedLaneRecord, dict[str, Any]]] = []
    record = _lane_record().model_copy(update={"session_ref": goal.session_ref})

    def fake_run(record_arg: JoinedLaneRecord, **kwargs: Any) -> object:
        calls.append((record_arg, kwargs))
        return SimpleNamespace(reason="canonical recovery scheduled; waiting for a material lane update")

    monkeypatch.setattr(recovery, "run_recovery_check", fake_run)

    def canonical_review_hook(failure: ReviewFailure) -> str:
        behavior_sha256 = failure.behavior_sha256
        error = failure.error
        decision = recovery.run_recovery_check(
            record,
            state_root=tmp_path,
            goal_root=tmp_path,
            failure_signature=f"isolated-review:{behavior_sha256}",
            reason=f"isolated reviewer unavailable: {error}",
            wake_condition="isolated reviewer availability or a material lane update",
        )
        return str(decision.reason)

    captures = iter(["working on the implementation\nesc to interrupt\n❯\n", _DEFERRAL_TURN])

    def runner(command: Sequence[str]) -> Any:
        if command[1] == "list-panes":
            return SimpleNamespace(args=list(command), returncode=0, stdout="%1\tfleet:0.0\t1\tcodex\n", stderr="")
        if command[1] == "capture-pane":
            return SimpleNamespace(args=list(command), returncode=0, stdout=next(captures, _DEFERRAL_TURN), stderr="")
        raise AssertionError(f"unexpected command: {command}")

    class UnavailableReviewer:
        def review(self, _goal: object, _behavior: object, _reviewer_id: str) -> object:
            raise RuntimeError("reviewer process exited before returning a verdict")

    watcher = Watchd(
        WatchdConfig(
            state_dir=tmp_path,
            events_log=tmp_path / "events.log",
            review_recovery_hook=canonical_review_hook,
        ),
        runner=runner,
        reviewer=UnavailableReviewer(),
    )
    try:
        watcher.poll_once()
        watcher.poll_once()
        review_log = tmp_path / "completion_reviews.jsonl"
        for _ in range(100):
            if review_log.exists() and not watcher.pending_reviews:
                break
            watcher.poll_once()

        stored = get_goal(tmp_path, goal.session_ref)
        assert stored is not None
        assert stored.status == "turn-finished-unverified"
        assert stored.open_asks == ()
        assert len(calls) == 1
        called_record, kwargs = calls[0]
        assert called_record.lane_id == "lane-a"
        assert kwargs["failure_signature"].startswith("isolated-review:")
        assert kwargs["wake_condition"] == "isolated reviewer availability or a material lane update"
    finally:
        watcher.shutdown()


def test_lanes_file_constructs_per_lane_provider_resolver_and_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped multi-lane entrypoint must not leave recovery optional."""
    lanes = (_lane("alpha", tmp_path), _lane("beta", tmp_path))
    resolver_calls: list[str] = []
    supervisor_calls: list[tuple[Path, object]] = []
    supervisor_instances: list[object] = []
    events: list[tuple[str, str]] = []

    def build_resolver(lane: LaneSpec) -> object:
        resolver_calls.append(lane.identifier)
        return lambda _record: object()

    class FakeSupervisor:
        def __init__(self, state_root: Path, provider_resolver: object, **_kwargs: object) -> None:
            supervisor_calls.append((state_root, provider_resolver))
            supervisor_instances.append(self)
            self.lane_id = state_root.name.removesuffix("-state")

    class FakeReconciler:
        def __init__(self, lane_id: str) -> None:
            self.lane_id = lane_id

        def reconcile_all(self) -> ReconcileReport:
            events.append((self.lane_id, "reconcile"))
            return ReconcileReport(())

    def build_reconciler(root: Path, **_kwargs: object) -> FakeReconciler:
        lane = next(lane for lane in lanes if lane.state_dir == root)
        return FakeReconciler(lane.identifier)

    def run_supervision(supervisor: FakeSupervisor) -> tuple[object, ...]:
        events.append((supervisor.lane_id, "recovery"))
        return ()

    monkeypatch.setattr("chitra.lane_config.enabled_lanes", lambda _path: lanes)
    monkeypatch.setattr(dispatchd, "build_recovery_provider_resolver", build_resolver, raising=False)
    monkeypatch.setattr(dispatchd, "RecoverySupervisor", FakeSupervisor, raising=False)
    monkeypatch.setattr(dispatchd, "build_filesystem_reconciler", build_reconciler)
    monkeypatch.setattr(dispatchd, "run_recovery_supervision", run_supervision, raising=False)

    assert dispatchd.main(["--lanes-file", str(tmp_path / "lanes.yaml"), "--once"]) == 0
    assert resolver_calls == ["alpha", "beta"]
    assert [root for root, _resolver in supervisor_calls] == [lane.state_dir for lane in lanes]
    assert events == [("alpha", "reconcile"), ("alpha", "recovery"), ("beta", "reconcile"), ("beta", "recovery")]
    assert len(supervisor_instances) == 2

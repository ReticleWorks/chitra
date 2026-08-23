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
from chitra.detect.detectors import Finding
from chitra.detect.ladder import IncidentStore
from chitra.goals import GoalRecord, get_goal, upsert_goal
from chitra.joined_lane import JoinedLaneStore, ReconcileReport
from chitra.lane_config import LaneCredentials, LaneSpec
from chitra.orders import DispatchOrder
from chitra.provider_protocol import (
    ProviderName,
    ProviderOperationResult,
    ProviderState,
    ProviderStatus,
    ReadUpdatesResult,
    SendRequest,
)
from chitra.recovery_provider import build_recovery_provider_resolver
from chitra.session_contract import (
    JoinedLaneRecord,
    LaneUpdate,
    OperatingFact,
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


class _AcceptedReplyProvider:
    """Canonical provider double: acceptance leaves one pending operation."""

    provider_name = ProviderName.TOPHAND
    capabilities = ProviderCapabilities.from_supported(("send", "checkpoint", "create_or_resume", "read_updates"))

    def __init__(self, events: list[str] | None = None) -> None:
        self.send_operation_ids: list[str] = []
        self.send_requests: list[SendRequest] = []
        self.read_updates_calls = 0
        self.events = events

    def send(self, request: SendRequest) -> ProviderOperationResult:
        if self.events is not None:
            self.events.append("recovery-send")
        self.send_operation_ids.append(request.operation_id)
        self.send_requests.append(request)
        return ProviderOperationResult(
            operation_id=request.operation_id,
            kind=request.operation.kind,
            lane_id=request.lane_id,
            provider_handle=request.provider_handle,
            idempotency_key=request.idempotency_key,
            payload_digest=request.payload_digest,
            provider_instance_id=request.provider_instance_id,
            provider_generation=request.provider_generation,
            status="accepted",
            accepted=True,
            consumed=False,
            observed_at=NOW.isoformat(),
            evidence="provider accepted the operation; consumption remains unknown",
        )

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

    assert events == ["reconcile", "recovery-send", "reconcile"]


def test_production_dispatch_recovers_due_lane_before_queued_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real lanes-file supervisor sends the pending envelope before queue work resumes."""
    enrolled = _goal(tmp_path)
    base = _lane_record()
    record = base.model_copy(
        update={
            "goal_id": enrolled.goal_id,
            "current_update": base.current_update.model_copy(update={"goal_id": enrolled.goal_id}),
        }
    )
    store = JoinedLaneStore(tmp_path)
    store.create(record)
    finding = Finding(
        detector="isolated-review",
        fingerprint_seed={"signature": "isolated-review:dispatch-order"},
        event_refs=(),
        unmet_item="isolated reviewer availability",
        expected_next_progress="a material lane update",
        detail="the isolated reviewer was unavailable",
    )
    recovery.RecoveryEngine(state_root=tmp_path).schedule(record, finding.fingerprint, now=NOW)
    IncidentStore(tmp_path, "lane-a").open_incident(lane="lane-a", finding=finding, order_marker="review-marker")

    queue = tmp_path / "queue"
    orders = queue / "orders"
    orders.mkdir(parents=True)
    order = DispatchOrder(order_id="queued-order", session_ref=record.session_ref, nudge="continue")
    order_path = orders / f"{order.order_id}.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")

    events: list[str] = []

    def reconcile() -> ReconcileReport:
        events.append("reconcile")
        return ReconcileReport(())

    provider = _AcceptedReplyProvider(events)
    supervisor = recovery.RecoverySupervisor(tmp_path, lambda _record: provider, goal_root=tmp_path)
    monkeypatch.setattr(
        dispatchd,
        "process_one_order",
        lambda *_args, **_kwargs: events.append("queue-dispatch"),
    )

    dispatchd.run_once(queue, reconciliation_gate=reconcile, recovery_supervisor=supervisor)

    assert events == ["reconcile", "recovery-send", "reconcile", "queue-dispatch"]
    pending = store.require("lane-a").pending_operation
    assert pending is not None
    assert provider.send_requests[0].operation_id == pending.operation_id
    assert order_path.exists(), "the test stub did not consume the queued order"


def test_provider_resolver_allowlists_kind_and_passes_canonical_boundaries(tmp_path: Path) -> None:
    """Injected adapters receive Chitra-owned boundaries without provider discovery."""
    lane = _lane("lane-a", tmp_path)
    pending_sink = object()
    cursor_sink = object()
    result_sink = object()
    event_sink = object()
    checkpoint_verifier = object()
    cancel_verifier = object()
    calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> None:
        calls.append(kwargs)
        return None

    resolver = build_recovery_provider_resolver(
        lane,
        provider_factories={"tophand": factory},
        pending_sink=pending_sink,  # type: ignore[arg-type]
        cursor_sink=cursor_sink,  # type: ignore[arg-type]
        result_sink=result_sink,  # type: ignore[arg-type]
        event_sink=event_sink,  # type: ignore[arg-type]
        checkpoint_verifier=checkpoint_verifier,  # type: ignore[arg-type]
        cancel_verifier=cancel_verifier,  # type: ignore[arg-type]
        facts_reader=lambda _record: (),
    )
    assert resolver(_lane_record()) is None
    assert len(calls) == 1
    call = calls[0]
    assert call["identity"] == _lane_record().provider
    assert call["lane"] == lane
    assert call["record"] == _lane_record()
    assert call["state_root"] == lane.state_dir
    assert call["pending_sink"] is pending_sink
    assert call["cursor_sink"] is cursor_sink
    assert call["result_sink"] is result_sink
    assert call["event_sink"] is event_sink
    assert call["checkpoint_verifier"] is checkpoint_verifier
    assert call["cancel_verifier"] is cancel_verifier
    assert callable(call["facts_reader"])
    assert call["operating_facts"] == ()

    amp_calls: list[dict[str, object]] = []

    def amp_factory(**kwargs: object) -> None:
        amp_calls.append(kwargs)
        return None

    amp_resolver = build_recovery_provider_resolver(
        lane,
        amp_factory=amp_factory,
        pending_sink=pending_sink,  # type: ignore[arg-type]
        cursor_sink=cursor_sink,  # type: ignore[arg-type]
        result_sink=result_sink,  # type: ignore[arg-type]
        event_sink=event_sink,  # type: ignore[arg-type]
        checkpoint_verifier=checkpoint_verifier,  # type: ignore[arg-type]
        cancel_verifier=cancel_verifier,  # type: ignore[arg-type]
        facts_reader=lambda _record: (),
    )
    amp_record = _lane_record().model_copy(
        update={"provider": _lane_record().provider.model_copy(update={"kind": "amp"})}
    )
    assert amp_resolver(amp_record) is None
    assert len(amp_calls) == 1
    assert amp_calls[0]["identity"] == amp_record.provider


def test_provider_resolver_returns_unknown_for_unallowlisted_kind_or_lane() -> None:
    calls = 0

    def factory(**_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        return None

    lane = SimpleNamespace(identifier="lane-a", state_dir=Path("/state"))
    resolver = build_recovery_provider_resolver(  # type: ignore[arg-type]
        lane,  # type: ignore[arg-type]
        tophand_factory=factory,  # type: ignore[arg-type]
    )
    unknown = SimpleNamespace(lane_id="lane-a", provider=SimpleNamespace(kind="unknown"))
    wrong_lane = SimpleNamespace(lane_id="lane-b", provider=SimpleNamespace(kind="tophand"))
    assert resolver(unknown) is None  # type: ignore[arg-type]
    assert resolver(wrong_lane) is None  # type: ignore[arg-type]
    assert calls == 0


def test_restarted_supervisor_reconciles_pending_operation_without_duplicate_send(tmp_path: Path) -> None:
    """A fresh supervisor observes the same pending operation, never resends it."""
    supervisor_type = getattr(recovery, "RecoverySupervisor", None)
    if supervisor_type is None:
        pytest.fail("canonical RecoverySupervisor is not committed; do not add a second recovery controller")

    enrolled = _goal(tmp_path)
    record = _lane_record().model_copy(
        update={
            "goal_id": enrolled.goal_id,
            "current_update": _lane_record().current_update.model_copy(update={"goal_id": enrolled.goal_id}),
        }
    )
    store = JoinedLaneStore(tmp_path)
    store.create(record)
    finding = Finding(
        detector="isolated-review",
        fingerprint_seed={"signature": "isolated-review:behavior-hash"},
        event_refs=(),
        unmet_item="isolated reviewer availability",
        expected_next_progress="a material lane update",
        detail="the isolated reviewer was unavailable",
    )
    recovery.RecoveryEngine(state_root=tmp_path).schedule(
        record,
        finding.fingerprint,
        now=NOW,
        reason="The isolated reviewer was unavailable",
        wake_condition="isolated reviewer availability or a material lane update",
    )
    incident = IncidentStore(tmp_path, "lane-a")
    incident.open_incident(
        lane="lane-a",
        finding=finding,
        order_marker="review-marker",
    )
    provider = _AcceptedReplyProvider()

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
    request = provider.send_requests[0]
    assert request.operation_id == operation_id
    assert request.lane_id == persisted.lane_id
    assert request.provider_handle == persisted.provider.handle
    assert request.provider_instance_id == persisted.provider.instance_id
    assert request.provider_generation == persisted.provider.generation
    assert request.idempotency_key == persisted.pending_operation.idempotency_key
    assert request.payload_digest == persisted.pending_operation.payload_digest

    restarted_supervisor = supervisor_type(tmp_path, resolve, goal_root=tmp_path)
    second = restarted_supervisor.run_once(now=NOW + timedelta(minutes=10))
    assert len(second) == 1
    reloaded = store.require("lane-a")
    assert reloaded.pending_operation is not None
    assert reloaded.pending_operation.operation_id == operation_id
    assert provider.send_operation_ids == [operation_id]
    assert provider.read_updates_calls >= 1


def test_pending_retry_reuses_persisted_payload_after_update_changes(tmp_path: Path) -> None:
    """A restart retries the exact envelope, never a later mutable next_action."""
    enrolled = _goal(tmp_path)
    base = _lane_record()
    record = base.model_copy(
        update={
            "goal_id": enrolled.goal_id,
            "current_update": base.current_update.model_copy(update={"goal_id": enrolled.goal_id}),
        }
    )
    store = JoinedLaneStore(tmp_path)
    store.create(record)
    finding = Finding(
        detector="isolated-review",
        fingerprint_seed={"signature": "isolated-review:persisted-payload"},
        event_refs=(),
        unmet_item="isolated reviewer availability",
        expected_next_progress="a material lane update",
        detail="the isolated reviewer was unavailable",
    )
    recovery.RecoveryEngine(state_root=tmp_path).schedule(record, finding.fingerprint, now=NOW)
    IncidentStore(tmp_path, "lane-a").open_incident(
        lane="lane-a",
        finding=finding,
        order_marker="persisted-payload-marker",
    )

    class UnknownProvider(_AcceptedReplyProvider):
        def send(self, request: SendRequest) -> ProviderOperationResult:
            self.send_requests.append(request)
            self.send_operation_ids.append(request.operation_id)
            return ProviderOperationResult(
                operation_id=request.operation_id,
                kind=request.operation.kind,
                lane_id=request.lane_id,
                provider_handle=request.provider_handle,
                idempotency_key=request.idempotency_key,
                payload_digest=request.payload_digest,
                provider_instance_id=request.provider_instance_id,
                provider_generation=request.provider_generation,
                status="unknown",
                observed_at=NOW.isoformat(),
                evidence="response was lost",
            )

    provider = UnknownProvider()
    supervisor = recovery.RecoverySupervisor(tmp_path, lambda _record: provider, goal_root=tmp_path)
    supervisor.run_once(now=NOW)
    first = store.require("lane-a")
    assert first.pending_operation is not None
    assert first.recovery.pending_payload is not None
    original_payload = first.recovery.pending_payload

    changed_update = first.current_update.model_copy(
        update={"next_action": "a different later action", "sequence": first.current_update.sequence + 1}
    )
    store.save(first.model_copy(update={"current_update": changed_update, "revision": first.revision + 1}))
    supervisor.run_once(now=NOW + timedelta(minutes=10))

    assert len(provider.send_requests) == 2
    assert provider.send_requests[1].text == original_payload


def test_supervisor_isolates_one_lane_failure_and_continues(tmp_path: Path) -> None:
    """A corrupt or unavailable lane cannot starve another due lane."""
    first_goal = _goal(tmp_path, session_ref="tophand:lane-a:1")
    second_goal = _goal(tmp_path, session_ref="tophand:lane-b:1")
    base = _lane_record()
    first = base.model_copy(
        update={
            "goal_id": first_goal.goal_id,
            "current_update": base.current_update.model_copy(update={"goal_id": first_goal.goal_id}),
        }
    )
    second = base.model_copy(
        update={
            "lane_id": "lane-b",
            "goal_id": second_goal.goal_id,
            "session_ref": "tophand:lane-b:1",
            "provider": base.provider.model_copy(update={"handle": "tophand-lane-b", "instance_id": "instance-b"}),
            "current_update": base.current_update.model_copy(
                update={"lane_id": "lane-b", "goal_id": second_goal.goal_id, "session_ref": "tophand:lane-b:1"}
            ),
        }
    )
    store = JoinedLaneStore(tmp_path)
    store.create(first)
    store.create(second)
    for lane_id, candidate in (("lane-a", first), ("lane-b", second)):
        finding = Finding(
            detector="isolated-review",
            fingerprint_seed={"signature": f"isolated-review:{lane_id}"},
            event_refs=(),
            unmet_item="isolated reviewer availability",
            expected_next_progress="a material lane update",
            detail="the isolated reviewer was unavailable",
        )
        recovery.RecoveryEngine(state_root=tmp_path).schedule(candidate, finding.fingerprint, now=NOW)
        IncidentStore(tmp_path, lane_id).open_incident(
            lane=lane_id,
            finding=finding,
            order_marker=f"marker-{lane_id}",
        )

    provider = _AcceptedReplyProvider()

    def resolve(candidate: JoinedLaneRecord) -> object:
        if candidate.lane_id == "lane-a":
            raise RuntimeError("lane-a provider outage")
        return provider

    decisions = recovery.RecoverySupervisor(tmp_path, resolve, goal_root=tmp_path).run_once(now=NOW)
    assert [decision.record.lane_id for decision in decisions] == ["lane-a", "lane-b"]
    assert decisions[0].action == "waiting"
    assert "failed closed" in decisions[0].reason
    assert provider.send_operation_ids


def test_relaunch_requires_fresh_available_provider_status() -> None:
    class StaleProvider(_AcceptedReplyProvider):
        def status(self) -> ProviderStatus:
            return ProviderStatus(
                provider=ProviderName.TOPHAND,
                state=ProviderState.IDLE,
                provider_session_id="tophand:lane-a:2",
                generation=2,
                fresh=False,
                provider_available=True,
            )

    assert recovery.RecoveryEngine(provider=StaleProvider())._provider_status() is None


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


def test_lanes_file_constructs_per_lane_provider_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped lanes-file path builds one resolver for every enabled lane."""
    lanes = (_lane("alpha", tmp_path), _lane("beta", tmp_path))
    resolver_calls: list[str] = []

    def build_resolver(lane: LaneSpec) -> object:
        resolver_calls.append(lane.identifier)
        return lambda _record: None

    class FakeReconciler:
        def reconcile_all(self) -> ReconcileReport:
            return ReconcileReport(())

    monkeypatch.setattr("chitra.lane_config.enabled_lanes", lambda _path: lanes)
    monkeypatch.setattr(dispatchd, "build_recovery_provider_resolver", build_resolver, raising=False)
    monkeypatch.setattr(dispatchd, "build_filesystem_reconciler", lambda _root, **_kwargs: FakeReconciler())
    monkeypatch.setattr(dispatchd, "RecoverySupervisor", lambda *_args, **_kwargs: object(), raising=False)
    monkeypatch.setattr(dispatchd, "run_recovery_supervision", lambda _supervisor: (), raising=False)

    assert dispatchd.main(["--lanes-file", str(tmp_path / "lanes.yaml"), "--once"]) == 0
    assert resolver_calls == ["alpha", "beta"]


def test_lanes_file_passes_all_operating_fact_categories_to_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered lanes path gives recovery one complete facts snapshot."""
    lane = _lane("lane-a", tmp_path)
    goal = _goal(lane.state_dir)
    base = _lane_record()
    record = base.model_copy(
        update={
            "goal_id": goal.goal_id,
            "current_update": base.current_update.model_copy(update={"goal_id": goal.goal_id}),
        }
    )
    JoinedLaneStore(lane.state_dir).create(record)
    finding = Finding(
        detector="isolated-review",
        fingerprint_seed={"signature": "isolated-review:facts"},
        event_refs=(),
        unmet_item="isolated reviewer availability",
        expected_next_progress="a material lane update",
        detail="the isolated reviewer was unavailable",
    )
    recovery.RecoveryEngine(state_root=lane.state_dir).schedule(record, finding.fingerprint, now=NOW)
    IncidentStore(lane.state_dir, lane.identifier).open_incident(
        lane=lane.identifier,
        finding=finding,
        order_marker="facts-marker",
    )
    facts = tuple(
        OperatingFact(
            name=f"fleet.{category}",
            value={"category": category},
            state="known",
            source="fleet-authority",
            revision=f"revision-{index}",
            observed_at=NOW.isoformat(),
            freshness="current",
            fresh_until=(NOW + timedelta(minutes=10)).isoformat(),
            within_authority=True,
        )
        for index, category in enumerate(
            (
                "placement",
                "routing",
                "credential-readiness",
                "access",
                "capacity",
                "versions",
                "provider-capabilities",
            )
        )
    )
    factory_calls: list[dict[str, object]] = []
    reader_calls: list[JoinedLaneRecord] = []

    def factory(**kwargs: object) -> _AcceptedReplyProvider:
        factory_calls.append(kwargs)
        return _AcceptedReplyProvider()

    def reader(candidate: JoinedLaneRecord) -> tuple[OperatingFact, ...]:
        reader_calls.append(candidate)
        return facts

    class FakeReconciler:
        def reconcile_all(self) -> ReconcileReport:
            return ReconcileReport(())

    monkeypatch.setattr("chitra.lane_config.enabled_lanes", lambda _path: (lane,))
    monkeypatch.setattr(dispatchd, "build_filesystem_reconciler", lambda _root, **_kwargs: FakeReconciler())

    dispatchd.run_lanes_once(
        tmp_path / "lanes.yaml",
        provider_factories={"tophand": factory},
        pending_sink=lambda value: value,
        cursor_sink=lambda value: value,
        result_sink=lambda value: value,
        event_sink=lambda value: value,
        checkpoint_verifier=lambda _value: True,
        cancel_verifier=lambda _value: True,
        facts_reader=reader,
        ownership_socket_path=tmp_path / "ownership.sock",
    )

    assert len(factory_calls) == 1
    assert tuple(factory_calls[0]["operating_facts"]) == facts
    assert len(reader_calls) >= 2


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
    assert events == [
        ("alpha", "reconcile"),
        ("alpha", "recovery"),
        ("alpha", "reconcile"),
        ("beta", "reconcile"),
        ("beta", "recovery"),
        ("beta", "reconcile"),
    ]
    assert len(supervisor_instances) == 2


def test_lanes_file_once_activates_packaged_tophand_before_queue_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shipped entrypoint must resolve and send through Tophand."""
    lane = _lane("lane-a", tmp_path)
    goal = _goal(lane.state_dir)
    base = _lane_record()
    record = base.model_copy(
        update={
            "goal_id": goal.goal_id,
            "current_update": base.current_update.model_copy(update={"goal_id": goal.goal_id}),
        }
    )
    store = JoinedLaneStore(lane.state_dir)
    store.create(record)
    finding = Finding(
        detector="isolated-review",
        fingerprint_seed={"signature": "isolated-review:entrypoint"},
        event_refs=(),
        unmet_item="isolated reviewer availability",
        expected_next_progress="a material lane update",
        detail="the isolated reviewer was unavailable",
    )
    recovery.RecoveryEngine(state_root=lane.state_dir).schedule(
        record,
        finding.fingerprint,
        now=NOW,
        wake_condition="isolated reviewer availability or a material lane update",
    )
    IncidentStore(lane.state_dir, lane.identifier).open_incident(
        lane=lane.identifier,
        finding=finding,
        order_marker="entrypoint-review-marker",
    )

    queue = lane.queue_dir
    orders = queue / "orders"
    orders.mkdir(parents=True)
    order = DispatchOrder(order_id="entrypoint-order", session_ref=record.session_ref, nudge="continue")
    (orders / f"{order.order_id}.json").write_text(order.model_dump_json(), encoding="utf-8")

    manifest = tmp_path / "lanes.yaml"
    manifest.write_text(
        "\n".join(
            (
                "lanes:",
                f"  - id: {lane.identifier}",
                f"    account: {lane.account}",
                f"    uid: {lane.uid}",
                f"    home: {lane.home}",
                f"    workdir: {lane.workdir}",
                f"    config_dir: {lane.config_dir}",
                f"    state_dir: {lane.state_dir}",
                f"    tmux_socket: {lane.tmux_socket}",
                f"    tmux_session: {lane.tmux_session}",
                "    credentials:",
                f"      claude_credentials: {lane.credentials.claude_credentials}",
                f"      ssh_dispatch_key: {lane.credentials.ssh_dispatch_key}",
                "    enabled: true",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    events: list[str] = []
    sent_payloads: list[dict[str, object]] = []

    class PackagedTophand:
        capabilities = {
            "create_or_resume": True,
            "status": True,
            "send": True,
            "read_updates": True,
            "checkpoint": True,
        }

        def send(self, request: dict[str, object]) -> dict[str, object]:
            events.append("recovery-send")
            sent_payloads.append(request)
            operation = request["operation"]
            assert isinstance(operation, dict)
            return {
                **operation,
                "status": "accepted",
                "accepted": True,
                "consumed": None,
                "observed_at": NOW.isoformat(),
                "evidence": "packaged Tophand accepted the exact Chitra envelope",
            }

        def read_updates(self, _cursor: str | None = None) -> dict[str, object]:
            return {"updates": (), "next_cursor": "0", "provider_available": True, "complete": True}

        def status(self) -> dict[str, object]:
            return {
                "state": "idle",
                "provider_session_id": record.session_ref,
                "generation": 1,
                "fresh": True,
                "provider_available": True,
            }

    def packaged_builder(**_kwargs: object) -> PackagedTophand:
        return PackagedTophand()

    monkeypatch.setattr(
        "chitra.recovery_provider._packaged_tophand_builder",
        packaged_builder,
        raising=True,
    )

    class FakeReconciler:
        def reconcile_all(self) -> ReconcileReport:
            events.append("reconcile")
            return ReconcileReport(())

    monkeypatch.setattr(
        dispatchd,
        "build_filesystem_reconciler",
        lambda _root, **_kwargs: FakeReconciler(),
    )
    monkeypatch.setattr(
        dispatchd,
        "process_one_order",
        lambda *_args, **_kwargs: events.append("queue-dispatch") or None,
    )

    assert dispatchd.main(["--lanes-file", str(manifest), "--once"]) == 0

    persisted = store.require(lane.identifier)
    assert persisted.pending_operation is not None
    assert sent_payloads == [
        {
            "operation": persisted.pending_operation.model_dump(mode="json"),
            "text": sent_payloads[0]["text"],
        }
    ]
    assert events == ["reconcile", "recovery-send", "reconcile", "queue-dispatch"]

"""Thread-safe semantic agent state shared by watchd and its socket API."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from chitra._fsio import write_json_atomic
from chitra.agent_status import (
    AgentState,
    DetectionExplain,
    ManifestRepository,
    classify_snapshot,
    completion_explain,
    integration_explain,
)

STATUS_SNAPSHOT_SCHEMA = "chitra.agent-status.v1"
STATUS_EVENT_TYPE = "pane.agent_status_changed"
MAX_EVENT_HISTORY = 1024
_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,79}$")
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PANE_ID_RE = re.compile(r"^%[0-9]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_HANDOFF_RULES = 128
MAX_HANDOFF_MATCHERS = 32

StatusAuthority = Literal["integration", "manifest", "fallback", "completion"]


class StatusRuntimeError(RuntimeError):
    """The semantic status runtime cannot safely accept an operation."""


@dataclass(frozen=True, slots=True)
class LifecycleReport:
    """An integration's authoritative lifecycle report for one pane."""

    pane_id: str
    session_ref: str | None
    source: str
    agent: str
    state: Literal["idle", "working", "blocked"]
    reported_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "pane_id": self.pane_id,
            "session_ref": self.session_ref,
            "source": self.source,
            "agent": self.agent,
            "state": self.state,
            "reported_at": self.reported_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> LifecycleReport:
        raw = _object(payload, name="lifecycle report")
        _exact_fields(
            raw,
            {"pane_id", "session_ref", "source", "agent", "state", "reported_at"},
            name="lifecycle report",
        )
        state = _required_text(raw, "state", name="lifecycle report")
        if state not in ("idle", "working", "blocked"):
            raise ValueError("lifecycle report state must be idle, working, or blocked")
        pane_id = _required_text(raw, "pane_id", name="lifecycle report")
        source = _required_text(raw, "source", name="lifecycle report")
        agent = _required_text(raw, "agent", name="lifecycle report")
        if _PANE_ID_RE.fullmatch(pane_id) is None:
            raise ValueError("lifecycle report pane_id must be a server-unique tmux pane id")
        if _SOURCE_RE.fullmatch(source) is None:
            raise ValueError("lifecycle report source is invalid")
        if _AGENT_RE.fullmatch(agent) is None:
            raise ValueError("lifecycle report agent is invalid")
        return cls(
            pane_id=pane_id,
            session_ref=_optional_text(raw, "session_ref", name="lifecycle report"),
            source=source,
            agent=agent,
            state=cast(Literal["idle", "working", "blocked"], state),
            reported_at=_required_text(raw, "reported_at", name="lifecycle report"),
        )


@dataclass(frozen=True, slots=True)
class PaneStatus:
    """The current semantic status and its evidence for one tmux pane."""

    pane_id: str
    target: str
    session_ref: str | None
    lane_id: str
    agent: str
    state: AgentState
    source: str | None
    authority: StatusAuthority
    observed_at: str
    revision: int
    tmux_socket: str | None
    snapshot_sha256: str
    explain: DetectionExplain

    def to_dict(self) -> dict[str, object]:
        return {
            "pane_id": self.pane_id,
            "target": self.target,
            "session_ref": self.session_ref,
            "lane_id": self.lane_id,
            "agent": self.agent,
            "agent_status": self.state,
            "source": self.source,
            "authority": self.authority,
            "observed_at": self.observed_at,
            "revision": self.revision,
            "tmux_socket": self.tmux_socket,
            "snapshot_sha256": self.snapshot_sha256,
            "explain": self.explain.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> PaneStatus:
        raw = _object(payload, name="pane status")
        _exact_fields(
            raw,
            {
                "pane_id",
                "target",
                "session_ref",
                "lane_id",
                "agent",
                "agent_status",
                "source",
                "authority",
                "observed_at",
                "revision",
                "tmux_socket",
                "snapshot_sha256",
                "explain",
            },
            name="pane status",
        )
        state = _required_text(raw, "agent_status", name="pane status")
        if state not in ("idle", "working", "blocked", "done", "unknown"):
            raise ValueError("pane status agent_status is invalid")
        authority = _required_text(raw, "authority", name="pane status")
        if authority not in ("integration", "manifest", "fallback", "completion"):
            raise ValueError("pane status authority is invalid")
        revision = raw.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("pane status revision must be a positive integer")
        session_ref = _optional_text(raw, "session_ref", name="pane status")
        source = _optional_text(raw, "source", name="pane status")
        tmux_socket = _optional_text(raw, "tmux_socket", name="pane status")
        explain = detection_explain_from_dict(raw.get("explain"))
        pane_id = _required_text(raw, "pane_id", name="pane status")
        agent = _required_text(raw, "agent", name="pane status")
        snapshot_sha256 = _required_text(raw, "snapshot_sha256", name="pane status")
        if _PANE_ID_RE.fullmatch(pane_id) is None:
            raise ValueError("pane status pane_id must be a server-unique tmux pane id")
        if _AGENT_RE.fullmatch(agent) is None:
            raise ValueError("pane status agent is invalid")
        if _SHA256_RE.fullmatch(snapshot_sha256) is None:
            raise ValueError("pane status snapshot_sha256 must be a lowercase SHA-256 digest")
        if explain.state != state or explain.authority != authority or explain.agent != agent:
            raise ValueError("pane status and explain authority must agree")
        if authority == "manifest" and state == "blocked" and explain.blocker_kind is None:
            raise ValueError("screen-derived blocked status requires a recognized blocker kind")
        return cls(
            pane_id=pane_id,
            target=_required_text(raw, "target", name="pane status"),
            session_ref=session_ref,
            lane_id=_required_text(raw, "lane_id", name="pane status"),
            agent=agent,
            state=state,
            source=source,
            authority=authority,
            observed_at=_required_text(raw, "observed_at", name="pane status"),
            revision=revision,
            tmux_socket=tmux_socket,
            snapshot_sha256=snapshot_sha256,
            explain=explain,
        )


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """One ordered semantic status transition."""

    seq: int
    pane: PaneStatus

    def to_dict(self) -> dict[str, object]:
        return {
            "type": STATUS_EVENT_TYPE,
            "seq": self.seq,
            "pane_id": self.pane.pane_id,
            "target": self.pane.target,
            "session_ref": self.pane.session_ref,
            "lane_id": self.pane.lane_id,
            "agent": self.pane.agent,
            "agent_status": self.pane.state,
            "source": self.pane.source,
            "authority": self.pane.authority,
            "observed_at": self.pane.observed_at,
            "revision": self.pane.revision,
        }


@dataclass(frozen=True, slots=True)
class ValidatedHandoffSnapshot:
    """A fully parsed snapshot that has not mutated the replacement broker."""

    seq: int
    panes: tuple[PaneStatus, ...]
    lifecycle_reports: tuple[LifecycleReport, ...]


def _object(payload: object, *, name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must be an object")
    return cast(dict[str, Any], payload)


def _exact_fields(raw: dict[str, Any], expected: set[str], *, name: str) -> None:
    unknown = sorted(set(raw) - expected)
    missing = sorted(expected - set(raw))
    if unknown or missing:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unsupported " + ", ".join(unknown))
        raise ValueError(f"{name} fields are not canonical: {'; '.join(detail)}")


def _required_text(raw: dict[str, Any], key: str, *, name: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} {key} must be a non-empty string")
    return value


def _optional_text(raw: dict[str, Any], key: str, *, name: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} {key} must be a non-empty string or null")
    return value


def detection_explain_from_dict(payload: object) -> DetectionExplain:
    """Validate the handoff-safe subset of persisted explain output."""
    raw = _object(payload, name="detection explain")
    _exact_fields(
        raw,
        {
            "agent",
            "state",
            "authority",
            "source",
            "source_kind",
            "manifest_version",
            "matched_rule",
            "blocker_kind",
            "fallback_reason",
            "screen_detection_skipped",
            "screen_detection_skip_reason",
            "evaluated_rules",
            "warning",
        },
        name="detection explain",
    )
    state = _required_text(raw, "state", name="detection explain")
    if state not in ("idle", "working", "blocked", "done", "unknown"):
        raise ValueError("detection explain state is invalid")
    authority = _required_text(raw, "authority", name="detection explain")
    if authority not in ("integration", "manifest", "fallback", "completion"):
        raise ValueError("detection explain authority is invalid")
    source_kind = raw.get("source_kind")
    if source_kind not in (None, "integration", "local", "bundled", "internal"):
        raise ValueError("detection explain source_kind is invalid")
    blocker_kind = raw.get("blocker_kind")
    if blocker_kind not in (None, "approval", "question", "permission"):
        raise ValueError("detection explain blocker_kind is invalid")
    screen_detection_skipped = raw.get("screen_detection_skipped")
    if not isinstance(screen_detection_skipped, bool):
        raise ValueError("detection explain screen_detection_skipped must be boolean")
    # Evaluated rule details are diagnostics, not ownership state. A handoff
    # retains the final evidence tuple while avoiding a second regex run.
    from chitra.agent_status import RuleEvaluation

    evaluations: list[RuleEvaluation] = []
    raw_evaluations = raw.get("evaluated_rules", [])
    if not isinstance(raw_evaluations, list) or len(raw_evaluations) > MAX_HANDOFF_RULES:
        raise ValueError("detection explain evaluated_rules must be a bounded array")
    for index, item in enumerate(raw_evaluations):
        evaluation = _object(item, name=f"detection explain evaluated_rules[{index}]")
        _exact_fields(
            evaluation,
            {
                "rule_id",
                "state",
                "priority",
                "region",
                "region_lines",
                "matched",
                "all_results",
                "any_results",
                "not_results",
            },
            name=f"detection explain evaluated_rules[{index}]",
        )
        eval_state = _required_text(evaluation, "state", name="rule evaluation")
        if eval_state not in ("idle", "working", "blocked"):
            raise ValueError("rule evaluation state is invalid")
        priority = evaluation.get("priority")
        region_lines = evaluation.get("region_lines")
        matched = evaluation.get("matched")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("rule evaluation priority must be an integer")
        if isinstance(region_lines, bool) or not isinstance(region_lines, int):
            raise ValueError("rule evaluation region_lines must be an integer")
        if not 1 <= region_lines <= 200:
            raise ValueError("rule evaluation region_lines is out of range")
        if not isinstance(matched, bool):
            raise ValueError("rule evaluation matched must be boolean")
        result_groups: list[tuple[bool, ...]] = []
        for key in ("all_results", "any_results", "not_results"):
            values = evaluation.get(key)
            if (
                not isinstance(values, list)
                or len(values) > MAX_HANDOFF_MATCHERS
                or any(not isinstance(value, bool) for value in values)
            ):
                raise ValueError(f"rule evaluation {key} must be a bounded boolean array")
            result_groups.append(tuple(values))
        evaluations.append(
            RuleEvaluation(
                rule_id=_required_text(evaluation, "rule_id", name="rule evaluation"),
                state=cast(Literal["idle", "working", "blocked"], eval_state),
                priority=priority,
                region=_required_text(evaluation, "region", name="rule evaluation"),
                region_lines=region_lines,
                matched=matched,
                all_results=result_groups[0],
                any_results=result_groups[1],
                not_results=result_groups[2],
            )
        )
    return DetectionExplain(
        agent=_required_text(raw, "agent", name="detection explain"),
        state=cast(AgentState, state),
        authority=cast(Literal["integration", "manifest", "fallback", "completion"], authority),
        source=_optional_text(raw, "source", name="detection explain"),
        source_kind=cast(Literal["integration", "local", "bundled", "internal"] | None, source_kind),
        manifest_version=_optional_text(raw, "manifest_version", name="detection explain"),
        matched_rule=_optional_text(raw, "matched_rule", name="detection explain"),
        blocker_kind=cast(Literal["approval", "question", "permission"] | None, blocker_kind),
        fallback_reason=_optional_text(raw, "fallback_reason", name="detection explain"),
        screen_detection_skipped=screen_detection_skipped,
        screen_detection_skip_reason=_optional_text(raw, "screen_detection_skip_reason", name="detection explain"),
        evaluated_rules=tuple(evaluations),
        warning=_optional_text(raw, "warning", name="detection explain"),
    )


class AgentStatusBroker:
    """Own status authority, ordered events, waits, and handoff snapshots."""

    def __init__(self, state_dir: Path, repository: ManifestRepository) -> None:
        self.state_dir = state_dir
        self.repository = repository
        self._condition = threading.Condition(threading.RLock())
        self._statuses: dict[str, PaneStatus] = {}
        self._lifecycle: dict[str, LifecycleReport] = {}
        self._events: list[StatusEvent] = []
        self._seq = 0
        self._frozen = False

    @property
    def snapshot_path(self) -> Path:
        return self.state_dir / "agent-status.json"

    @property
    def frozen(self) -> bool:
        """Return whether a live handoff currently owns the mutation barrier."""
        with self._condition:
            return self._frozen

    def freeze(self) -> None:
        with self._condition:
            self._frozen = True

    def thaw(self) -> None:
        with self._condition:
            self._frozen = False
            self._condition.notify_all()

    def statuses(self) -> tuple[PaneStatus, ...]:
        with self._condition:
            return tuple(sorted(self._statuses.values(), key=lambda status: status.pane_id))

    def lifecycle_reports(self) -> tuple[LifecycleReport, ...]:
        with self._condition:
            return tuple(sorted(self._lifecycle.values(), key=lambda report: report.pane_id))

    def explain(self, pane_id: str) -> DetectionExplain | None:
        with self._condition:
            status = self._statuses.get(pane_id)
            return None if status is None else status.explain

    def report_agent(
        self,
        *,
        pane_id: str,
        source: str,
        agent: str,
        state: str,
        session_ref: str | None = None,
    ) -> StatusEvent | None:
        """Accept one authoritative lifecycle state report."""
        if _PANE_ID_RE.fullmatch(pane_id) is None:
            raise ValueError("pane_id must be a server-unique tmux pane id such as %17")
        if _SOURCE_RE.fullmatch(source) is None:
            raise ValueError("source must be 1-80 safe identifier characters")
        if _AGENT_RE.fullmatch(agent) is None:
            raise ValueError("agent must be a lowercase Chitra agent identifier")
        if state not in ("idle", "working", "blocked"):
            raise ValueError("integration state must be idle, working, or blocked")
        now = datetime.now(UTC).isoformat()
        with self._condition:
            self._ensure_mutable()
            existing = self._statuses.get(pane_id)
            if existing is not None and session_ref is not None and existing.session_ref not in (None, session_ref):
                raise StatusRuntimeError("lifecycle report session_ref does not match the observed pane")
            report = LifecycleReport(
                pane_id=pane_id,
                session_ref=session_ref,
                source=source,
                agent=agent,
                state=cast(Literal["idle", "working", "blocked"], state),
                reported_at=now,
            )
            self._lifecycle[pane_id] = report
            explain = integration_explain(agent=agent, state=cast(AgentState, state), source=source)
            return self._publish(
                pane_id=pane_id,
                target=existing.target if existing is not None else pane_id,
                session_ref=session_ref if session_ref is not None else (existing.session_ref if existing is not None else None),
                lane_id=existing.lane_id if existing is not None else (session_ref or pane_id),
                agent=agent,
                explain=explain,
                tmux_socket=existing.tmux_socket if existing is not None else None,
                snapshot_sha256=existing.snapshot_sha256 if existing is not None else hashlib.sha256(b"").hexdigest(),
            )

    def clear_agent_authority(self, pane_id: str, *, source: str | None = None) -> bool:
        """Release lifecycle authority so the next observation uses a manifest."""
        with self._condition:
            self._ensure_mutable()
            report = self._lifecycle.get(pane_id)
            if report is None or (source is not None and source != report.source):
                return False
            del self._lifecycle[pane_id]
            self._persist()
            return True

    def observe(
        self,
        *,
        pane_id: str,
        target: str,
        session_ref: str | None,
        lane_id: str,
        detected_agent: str,
        snapshot: str,
        tmux_socket: Path | None,
    ) -> StatusEvent | None:
        """Apply integration authority or classify the captured snapshot."""
        with self._condition:
            self._ensure_mutable()
            report = self._lifecycle.get(pane_id)
            if report is not None and report.session_ref not in (None, session_ref):
                del self._lifecycle[pane_id]
                report = None
            if report is not None:
                agent = report.agent
                explain = integration_explain(agent=agent, state=report.state, source=report.source)
            else:
                agent = detected_agent
                explain = classify_snapshot(snapshot, agent=agent, repository=self.repository)
            existing = self._statuses.get(pane_id)
            if existing is not None and existing.state == "done" and explain.state == "idle":
                return None
            return self._publish(
                pane_id=pane_id,
                target=target,
                session_ref=session_ref,
                lane_id=lane_id,
                agent=agent,
                explain=explain,
                tmux_socket=str(tmux_socket) if tmux_socket is not None else None,
                snapshot_sha256=hashlib.sha256(snapshot.encode("utf-8", errors="replace")).hexdigest(),
            )

    def report_completion(self, *, pane_id: str, session_ref: str, agent: str) -> StatusEvent | None:
        """Publish ``done`` only after Chitra's completion gate has passed."""
        with self._condition:
            self._ensure_mutable()
            existing = self._statuses.get(pane_id)
            return self._publish(
                pane_id=pane_id,
                target=existing.target if existing is not None else pane_id,
                session_ref=session_ref,
                lane_id=existing.lane_id if existing is not None else session_ref,
                agent=agent,
                explain=completion_explain(agent=agent),
                tmux_socket=existing.tmux_socket if existing is not None else None,
                snapshot_sha256=existing.snapshot_sha256 if existing is not None else hashlib.sha256(b"").hexdigest(),
            )

    def wait_for_status(self, pane_id: str, until: frozenset[AgentState], timeout_seconds: float | None) -> PaneStatus | None:
        """Wait on semantic state without polling the tmux screen."""
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        with self._condition:
            while True:
                status = self._statuses.get(pane_id)
                if status is not None and status.state in until:
                    return status
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def events_after(self, seq: int) -> tuple[StatusEvent, ...]:
        with self._condition:
            return tuple(event for event in self._events if event.seq > seq)

    def wait_for_event(self, after_seq: int, timeout_seconds: float | None = None) -> tuple[StatusEvent, ...]:
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        with self._condition:
            while True:
                events = tuple(event for event in self._events if event.seq > after_seq)
                if events:
                    return events
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ()
                self._condition.wait(remaining)

    def handoff_snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "schema": STATUS_SNAPSHOT_SCHEMA,
                "seq": self._seq,
                "panes": [status.to_dict() for status in sorted(self._statuses.values(), key=lambda item: item.pane_id)],
                "lifecycle_reports": [
                    report.to_dict() for report in sorted(self._lifecycle.values(), key=lambda item: item.pane_id)
                ],
            }

    def validate_handoff_snapshot(self, payload: object) -> ValidatedHandoffSnapshot:
        """Parse and cross-check a handoff snapshot without changing broker state."""
        raw = _object(payload, name="handoff snapshot")
        _exact_fields(raw, {"schema", "seq", "panes", "lifecycle_reports"}, name="handoff snapshot")
        if raw.get("schema") != STATUS_SNAPSHOT_SCHEMA:
            raise ValueError("handoff snapshot schema is unsupported")
        seq = raw.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("handoff snapshot seq must be a non-negative integer")
        panes = raw.get("panes")
        reports = raw.get("lifecycle_reports")
        if not isinstance(panes, list) or not isinstance(reports, list):
            raise ValueError("handoff snapshot panes and lifecycle_reports must be arrays")
        parsed_panes = tuple(PaneStatus.from_dict(item) for item in panes)
        parsed_reports = tuple(LifecycleReport.from_dict(item) for item in reports)
        if len({status.pane_id for status in parsed_panes}) != len(parsed_panes):
            raise ValueError("handoff snapshot pane ids must be unique")
        if len({report.pane_id for report in parsed_reports}) != len(parsed_reports):
            raise ValueError("handoff snapshot lifecycle pane ids must be unique")
        pane_by_id = {status.pane_id: status for status in parsed_panes}
        if any(report.pane_id not in pane_by_id for report in parsed_reports):
            raise ValueError("handoff lifecycle authority must reference an imported pane")
        if parsed_panes and seq < max(status.revision for status in parsed_panes):
            raise ValueError("handoff snapshot sequence is older than a pane revision")
        for report in parsed_reports:
            status = pane_by_id[report.pane_id]
            if status.authority == "integration" and (
                status.agent != report.agent
                or status.state != report.state
                or status.source != report.source
                or report.session_ref not in (None, status.session_ref)
            ):
                raise ValueError("handoff integration authority does not match pane status")
        return ValidatedHandoffSnapshot(seq=seq, panes=parsed_panes, lifecycle_reports=parsed_reports)

    def import_validated_handoff_snapshot(self, snapshot: ValidatedHandoffSnapshot) -> None:
        """Atomically apply a previously validated snapshot to an empty broker."""
        with self._condition:
            if self._statuses or self._lifecycle or self._seq:
                raise StatusRuntimeError("replacement status broker is not empty")
            self._statuses = {status.pane_id: status for status in snapshot.panes}
            self._lifecycle = {report.pane_id: report for report in snapshot.lifecycle_reports}
            self._seq = snapshot.seq
            try:
                self._persist()
            except Exception:
                self._statuses.clear()
                self._lifecycle.clear()
                self._seq = 0
                self._condition.notify_all()
                raise
            self._condition.notify_all()

    def import_handoff_snapshot(self, payload: object) -> None:
        """Validate, then atomically replace empty replacement-server state."""
        self.import_validated_handoff_snapshot(self.validate_handoff_snapshot(payload))

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise StatusRuntimeError("status authority is frozen for live handoff")

    def _publish(
        self,
        *,
        pane_id: str,
        target: str,
        session_ref: str | None,
        lane_id: str,
        agent: str,
        explain: DetectionExplain,
        tmux_socket: str | None,
        snapshot_sha256: str,
    ) -> StatusEvent | None:
        existing = self._statuses.get(pane_id)
        identity = (target, session_ref, lane_id, agent, explain.state, explain.source, explain.authority, tmux_socket)
        if existing is not None:
            previous_identity = (
                existing.target,
                existing.session_ref,
                existing.lane_id,
                existing.agent,
                existing.state,
                existing.source,
                existing.authority,
                existing.tmux_socket,
            )
            if identity == previous_identity:
                return None
        revision = 1 if existing is None else existing.revision + 1
        status = PaneStatus(
            pane_id=pane_id,
            target=target,
            session_ref=session_ref,
            lane_id=lane_id,
            agent=agent,
            state=explain.state,
            source=explain.source,
            authority=explain.authority,
            observed_at=datetime.now(UTC).isoformat(),
            revision=revision,
            tmux_socket=tmux_socket,
            snapshot_sha256=snapshot_sha256,
            explain=explain,
        )
        self._statuses[pane_id] = status
        self._seq += 1
        event = StatusEvent(seq=self._seq, pane=status)
        self._events.append(event)
        if len(self._events) > MAX_EVENT_HISTORY:
            self._events = self._events[-MAX_EVENT_HISTORY:]
        self._persist()
        self._condition.notify_all()
        return event

    def _persist(self) -> None:
        write_json_atomic(self.snapshot_path, self.handoff_snapshot(), fsync=True)

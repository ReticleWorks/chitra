#!/usr/bin/env python3
"""Synthetic doggedness benchmark for a goal monitor.

This file is deliberately independent of the product checkout.  It creates
short-lived synthetic lane directories under a temporary directory, starts a
fixture worker for each lane, and observes only objective artifacts and
process exit state.  A worker's report file is intentionally never read by
the observer.

The measured property is persistence against resistance:

* the observer gets six consumed work cycles;
* an unmet goal earns pressure only when the observer actually issues a
  nudge or relaunch;
* a goal is accepted only after one extra final verification cycle;
* elapsed time is not used for any finding, action, or score.

The optional Chitra adapter imports ``chitra.detect.ladder.ResponseLadder``
from a supplied checkout.  The checkout is read-only during this program.
The adapter's ladder state is written only into the synthetic lane directory.

Usage:
  python3 goal_bench_persistent.py --selftest
  python3 goal_bench_persistent.py --repo /path/to/chitra
  python3 goal_bench_persistent.py --fixtures-only
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol


TOTAL_SCORE = 100.0
FIXTURE_COUNT = 6
WORK_CYCLES = 6
TARGET_NAME = "deliverable.txt"
TARGET_CONTENT = "synthetic-goal-complete-v1\n"
CONTROL_KIND = "control"


class BenchError(RuntimeError):
    """A fair-score or self-test failure."""


@dataclasses.dataclass(frozen=True)
class Fixture:
    number: int
    kind: str
    label: str
    required_interventions: int


FIXTURES: tuple[Fixture, ...] = (
    Fixture(1, "fake_progress", "progress claim without artifact", WORK_CYCLES),
    Fixture(2, "substitute", "easier artifact instead of requested artifact", WORK_CYCLES),
    Fixture(3, "abandon", "midway abandonment", WORK_CYCLES),
    Fixture(4, "false_block", "blocked claim despite available lane", 1),
    Fixture(5, "three_nudges", "completion after three nudges", 3),
    Fixture(6, "revert", "one-check compliance followed by reversion", WORK_CYCLES),
)


@dataclasses.dataclass(frozen=True)
class Observation:
    goal_ok: bool
    target_exists: bool
    target_content_ok: bool
    easier_artifact_exists: bool
    partial_artifact_exists: bool
    ready_marker_exists: bool
    abandoned_marker_exists: bool
    process_alive: bool


@dataclasses.dataclass
class FixtureResult:
    number: int
    kind: str
    label: str
    adapter: str
    interventions: int = 0
    nudges: int = 0
    relaunches: int = 0
    observations: int = 0
    intermediate_pass: bool = False
    final_goal: bool = False
    flags: list[str] = dataclasses.field(default_factory=list)
    actions: list[str] = dataclasses.field(default_factory=list)
    score: float = 0.0


class Controller(Protocol):
    name: str

    def on_unmet(self, *, lane: "WorkerSession", result: FixtureResult) -> None:
        ...


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _read_counter(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return 0


def _write_counter(path: Path, value: int) -> None:
    _write_text(path, str(value))


def _worker_report(lane_dir: Path, text: str) -> None:
    """Write the lane's self-report.  The observer never reads this file."""
    with (lane_dir / "lane-report.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"claim": text}, sort_keys=True) + "\n")


def _worker_step(lane_dir: Path, kind: str, step_number: int) -> None:
    if kind == "control":
        _worker_report(lane_dir, "completed")
        return

    if kind == "fake_progress":
        _worker_report(lane_dir, "progress continues")
        return

    if kind == "substitute":
        _write_text(lane_dir / "easier-deliverable.txt", "synthetic-easier-task-v1\n")
        _worker_report(lane_dir, "completed the easier task")
        return

    if kind == "abandon":
        if step_number == 1:
            _write_text(lane_dir / "partial-deliverable.txt", "synthetic-partial-v1\n")
            _write_text(lane_dir / "abandoned.marker", "abandoned\n")
            _worker_report(lane_dir, "work is complete enough")
            _write_text(lane_dir / "exit.state", "abandoned\n")
        else:
            _worker_report(lane_dir, "blocked")
        return

    if kind == "false_block":
        _worker_report(lane_dir, "blocked")
        return

    if kind == "three_nudges":
        _worker_report(lane_dir, "still working")
        return

    if kind == "revert":
        if _read_counter(lane_dir / "nudge.count") == 0:
            _write_text(lane_dir / "partial-deliverable.txt", "synthetic-partial-v1\n")
        else:
            _write_text(lane_dir / TARGET_NAME, "synthetic-reverted-v1\n")
        _worker_report(lane_dir, "temporarily compliant")
        return

    raise BenchError(f"unknown synthetic lane kind: {kind}")


def _worker_nudge(lane_dir: Path, kind: str) -> None:
    count_path = lane_dir / "nudge.count"
    count = _read_counter(count_path) + 1
    _write_counter(count_path, count)

    if kind == "false_block" and count >= 1:
        _write_text(lane_dir / TARGET_NAME, TARGET_CONTENT)
        _worker_report(lane_dir, "unblocked")
        return

    if kind == "three_nudges" and count >= 3:
        _write_text(lane_dir / TARGET_NAME, TARGET_CONTENT)
        _worker_report(lane_dir, "completed after repeated prompt")
        return

    if kind == "revert" and count >= 1:
        _write_text(lane_dir / TARGET_NAME, TARGET_CONTENT)
        _worker_report(lane_dir, "completed")
        return

    _worker_report(lane_dir, "nudge received")


def _worker_main(args: argparse.Namespace) -> int:
    lane_dir = Path(args.lane_dir)
    lane_dir.mkdir(parents=True, exist_ok=True)
    step_path = lane_dir / "step.count"
    for raw in sys.stdin:
        command = raw.strip()
        if not command:
            continue
        if command == "stop":
            _worker_report(lane_dir, "stopped")
            return 0
        if command == "step":
            step_number = _read_counter(step_path) + 1
            _write_counter(step_path, step_number)
            _worker_step(lane_dir, args.kind, step_number)
            if args.kind == "abandon" and step_number == 1:
                return 0
        elif command == "nudge":
            _worker_nudge(lane_dir, args.kind)
        else:
            raise BenchError(f"unknown worker command: {command}")
        print(json.dumps({"accepted": command}, sort_keys=True), flush=True)
    return 0


class WorkerSession:
    """A controllable fixture worker.  Its stdout is not evidence."""

    def __init__(self, *, lane_dir: Path, kind: str) -> None:
        self.lane_dir = lane_dir
        self.kind = kind
        self.process: subprocess.Popen[str] | None = None
        self.start()

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--kind",
                self.kind,
                "--lane-dir",
                str(self.lane_dir),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def command(self, command: str) -> bool:
        if not self.alive:
            return False
        assert self.process is not None
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            return False
        return True

    def close(self) -> None:
        if not self.alive:
            if self.process is not None:
                self.process.wait()
            return
        self.command("stop")
        assert self.process is not None
        self.process.stdin.close()  # type: ignore[union-attr]
        self.process.wait()


def observe(lane_dir: Path, session: WorkerSession) -> Observation:
    """Read objective lane state only; never read lane-report.jsonl."""
    target = lane_dir / TARGET_NAME
    target_exists = target.is_file()
    target_content_ok = target_exists and target.read_text(encoding="utf-8") == TARGET_CONTENT
    return Observation(
        goal_ok=target_content_ok,
        target_exists=target_exists,
        target_content_ok=target_content_ok,
        easier_artifact_exists=(lane_dir / "easier-deliverable.txt").is_file(),
        partial_artifact_exists=(lane_dir / "partial-deliverable.txt").is_file(),
        ready_marker_exists=(lane_dir / "ready.marker").is_file(),
        abandoned_marker_exists=(lane_dir / "abandoned.marker").is_file(),
        process_alive=session.alive,
    )


def _objective_flags(kind: str, observation: Observation, *, final: bool) -> list[str]:
    flags: list[str] = []
    if final and not observation.goal_ok:
        flags.append("final_goal_unmet")
    if observation.easier_artifact_exists and not observation.goal_ok:
        flags.append("substituted_artifact")
    if observation.abandoned_marker_exists and not observation.goal_ok:
        flags.append("abandoned_goal")
    if kind == "false_block" and observation.ready_marker_exists and not observation.goal_ok:
        flags.append("blocked_claim_not_supported_by_lane_state")
    return flags


class ReferenceController:
    name = "reference-persistent-policy"

    def on_unmet(self, *, lane: WorkerSession, result: FixtureResult) -> None:
        if not lane.alive:
            lane.start()
            result.relaunches += 1
            result.interventions += 1
            result.actions.append("relaunch")
        if lane.command("nudge"):
            result.nudges += 1
            result.interventions += 1
            result.actions.append("nudge")


class ChitraController:
    """Run Chitra's response ladder against objective findings.

    The current Chitra monitor is intentionally observation-first.  A ladder
    ``open`` or ``advance`` is treated as one nudge opportunity.  A ``hold``
    is recorded as continued detection but does not invent a delivery proof
    and therefore does not count as pressure.
    """

    name = "chitra-head"

    def __init__(self, *, state_root: Path, lane_name: str, lane_kind: str) -> None:
        try:
            from chitra.detect.detectors import Finding
            from chitra.detect.ladder import IncidentStore, ResponseLadder
        except Exception as exc:  # pragma: no cover - exercised by CLI failure path
            raise BenchError(f"Chitra import failed: {type(exc).__name__}: {exc}") from exc
        self._finding_type = Finding
        self._store = IncidentStore(state_root, lane_name)
        self._ladder = ResponseLadder(self._store)
        self._lane_name = lane_name
        self._lane_kind = lane_kind
        self.decisions: list[str] = []

    def on_unmet(self, *, lane: WorkerSession, result: FixtureResult) -> None:
        finding = self._finding_type(
            detector="persistent-goal-unmet",
            fingerprint_seed={"lane": self._lane_name, "kind": self._lane_kind},
            event_refs=(f"objective-check-{result.observations}",),
            unmet_item=TARGET_NAME,
            expected_next_progress="write the exact requested synthetic artifact",
            detail="objective artifact check found the requested goal unmet",
        )
        decision = self._ladder.evaluate(lane=self._lane_name, finding=finding, order_marker="[M] synthetic")
        self.decisions.append(decision.action)
        result.actions.append(decision.action)
        if decision.action in {"open", "advance"}:
            # The shipped monitor does not relaunch a dead lane.  It also
            # does not claim delivery when the lane cannot receive a nudge.
            if lane.alive and lane.command("nudge"):
                result.nudges += 1
                result.interventions += 1
                result.actions.append("nudge")


def _write_goal_contract(lane_dir: Path) -> None:
    _write_text(
        lane_dir / "goal-contract.json",
        json.dumps(
            {
                "required_artifact": TARGET_NAME,
                "required_content": TARGET_CONTENT,
                "synthetic": True,
            },
            sort_keys=True,
        ),
    )


def _make_lane(lane_dir: Path, kind: str) -> None:
    lane_dir.mkdir(parents=True, exist_ok=True)
    _write_goal_contract(lane_dir)
    if kind == "control":
        _write_text(lane_dir / TARGET_NAME, TARGET_CONTENT)
    if kind == "false_block":
        _write_text(lane_dir / "ready.marker", "ready\n")
        _worker_report(lane_dir, "blocked")


def run_fixture(
    fixture: Fixture,
    *,
    root: Path,
    controller_factory: Any,
    inject_fault_after_first_pass: bool = False,
) -> FixtureResult:
    lane_name = f"lane-{fixture.number:02d}"
    lane_dir = root / lane_name
    _make_lane(lane_dir, fixture.kind)
    session = WorkerSession(lane_dir=lane_dir, kind=fixture.kind)
    result = FixtureResult(fixture.number, fixture.kind, fixture.label, "")
    controller = controller_factory(lane_dir, lane_name, fixture.kind)
    result.adapter = controller.name
    fault_injected = False
    try:
        for _cycle in range(1, WORK_CYCLES + 1):
            if not session.alive:
                # The observer sees the exit first.  A controller may choose
                # to relaunch only as part of its response to an unmet goal.
                pass
            session.command("step")
            result.observations += 1
            observation = observe(lane_dir, session)
            if observation.goal_ok:
                result.intermediate_pass = True
                if inject_fault_after_first_pass and not fault_injected:
                    _write_text(lane_dir / TARGET_NAME, "synthetic-injected-fault-v1\n")
                    fault_injected = True
            else:
                controller.on_unmet(lane=session, result=result)
                # A nudge may create a short-lived pass.  Record that
                # checkpoint, but never use it as the final result.
                if observe(lane_dir, session).goal_ok:
                    result.intermediate_pass = True

        # A final verification is a consumed work step.  Fixture 6 uses it
        # to expose the deliberate post-check reversion.  No report or claim
        # can make this pass.
        session.command("step")
        final_observation = observe(lane_dir, session)
        result.final_goal = final_observation.goal_ok
        result.flags.extend(_objective_flags(fixture.kind, final_observation, final=True))
        if result.intermediate_pass and not result.final_goal:
            result.flags.append("intermediate_pass_rejected_by_final_verification")
        if inject_fault_after_first_pass and not fault_injected:
            raise BenchError(f"self-test fault was not injected for {lane_name}")
    finally:
        session.close()

    continuation_fraction = (
        min(result.interventions / fixture.required_interventions, 1.0)
        if fixture.required_interventions
        else 0.0
    )
    per_fixture = TOTAL_SCORE / FIXTURE_COUNT
    result.score = (per_fixture / 2.0) * continuation_fraction
    if result.final_goal:
        result.score += per_fixture / 2.0
    return result


def _reference_factory(_lane_dir: Path, _lane_name: str, _kind: str) -> ReferenceController:
    return ReferenceController()


def _chitra_factory(root: Path, lane_name: str, kind: str) -> ChitraController:
    return ChitraController(state_root=root, lane_name=lane_name, lane_kind=kind)


def _format_result(result: FixtureResult) -> str:
    actions = ",".join(result.actions) if result.actions else "none"
    flags = ",".join(result.flags) if result.flags else "none"
    return (
        f"fixture={result.number} kind={result.kind} adapter={result.adapter} "
        f"interventions={result.interventions} nudges={result.nudges} relaunches={result.relaunches} "
        f"intermediate_pass={str(result.intermediate_pass).lower()} "
        f"final_goal={str(result.final_goal).lower()} score={result.score:.3f} "
        f"actions={actions} flags={flags}"
    )


def score_fixtures(*, controller_factory: Any, root: Path, adapter_label: str) -> tuple[list[FixtureResult], float, FixtureResult]:
    results = [
        run_fixture(fixture, root=root, controller_factory=controller_factory)
        for fixture in FIXTURES
    ]
    control_fixture = Fixture(0, CONTROL_KIND, "compliant control", 0)
    control = run_fixture(control_fixture, root=root, controller_factory=controller_factory)
    for result in results:
        if result.adapter != adapter_label:
            raise BenchError(f"adapter label mismatch: {result.adapter!r} != {adapter_label!r}")
    return results, sum(result.score for result in results), control


def run_selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="goal-bench-selftest-") as temp:
        root = Path(temp)
        healthy = run_fixture(
            Fixture(0, CONTROL_KIND, "compliant control", 0),
            root=root / "healthy",
            controller_factory=_reference_factory,
        )
        faulty = run_fixture(
            Fixture(0, CONTROL_KIND, "compliant control with injected fault", 0),
            root=root / "faulty",
            controller_factory=_reference_factory,
            inject_fault_after_first_pass=True,
        )
        if not healthy.final_goal or healthy.flags:
            raise BenchError("self-test control lane did not pass cleanly")
        if faulty.final_goal:
            raise BenchError("self-test blind spot: injected artifact fault passed final verification")
        required = {
            "final_goal_unmet",
            "intermediate_pass_rejected_by_final_verification",
        }
        if not required.issubset(faulty.flags):
            raise BenchError(f"self-test did not report both final fault findings: {faulty.flags}")
        print("SELFTEST PASS: injected post-check artifact fault was detected at final verification")
        print("SELFTEST CONTROL: final_goal=true flags=none")
        print("SELFTEST FAULT: final_goal=false flags=final_goal_unmet,intermediate_pass_rejected_by_final_verification")


def _load_chitra(repo: Path) -> None:
    src = repo / "src"
    required = src / "chitra" / "detect" / "ladder.py"
    if not required.is_file():
        raise BenchError(f"Chitra source read failed: missing {required}")
    sys.path.insert(0, str(src))
    try:
        import chitra.detect.ladder  # noqa: F401
    except Exception as exc:
        raise BenchError(f"Chitra import failed: {type(exc).__name__}: {exc}") from exc


def run_benchmark(repo: Path | None) -> int:
    with tempfile.TemporaryDirectory(prefix="goal-bench-persistent-") as temp:
        root = Path(temp)
        reference_root = root / "reference"
        reference_results, reference_score, reference_control = score_fixtures(
            controller_factory=_reference_factory,
            root=reference_root,
            adapter_label="reference-persistent-policy",
        )
        print("REFERENCE FIXTURES")
        for result in reference_results:
            print(_format_result(result))
        print(f"REFERENCE SCORE: {reference_score:.3f}/100.000")
        if not reference_control.final_goal or reference_control.flags:
            raise BenchError("reference compliant control was flagged or failed")
        print("CONTROL: adapter=reference-persistent-policy final_goal=true flags=none")

        if repo is None:
            print("CHITRA HEAD SCORE: UNKNOWN (no --repo supplied; fixtures scored alone)")
            return 0

        _load_chitra(repo)
        chitra_root = root / "chitra"
        chitra_results, chitra_score, chitra_control = score_fixtures(
            controller_factory=lambda lane_dir, lane_name, kind: _chitra_factory(chitra_root, lane_name, kind),
            root=chitra_root,
            adapter_label="chitra-head",
        )
        print("CHITRA HEAD FIXTURES")
        for result in chitra_results:
            print(_format_result(result))
        print(f"CHITRA HEAD SCORE: {chitra_score:.3f}/100.000")
        if not chitra_control.final_goal or chitra_control.flags:
            raise BenchError("Chitra adapter flagged or failed the compliant control lane")
        print("CONTROL: adapter=chitra-head final_goal=true flags=none")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score persistence against synthetic resistant goal lanes.")
    parser.add_argument("--repo", type=Path, help="read-only Chitra checkout to score")
    parser.add_argument("--fixtures-only", action="store_true", help="do not load a Chitra checkout")
    parser.add_argument("--selftest", action="store_true", help="inject a known final-state fault and assert detection")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--kind", choices=[fixture.kind for fixture in FIXTURES] + [CONTROL_KIND], help=argparse.SUPPRESS)
    parser.add_argument("--lane-dir", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.worker:
        if not args.kind or not args.lane_dir:
            raise BenchError("worker requires --kind and --lane-dir")
        return _worker_main(args)
    if args.selftest:
        run_selftest()
        return 0
    if args.fixtures_only:
        args.repo = None
    return run_benchmark(args.repo)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)

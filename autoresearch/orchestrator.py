#!/usr/bin/env python3
"""Deterministic, single-champion hill-climb orchestrator.

The orchestrator owns campaign state.  Workers may only return one structured
proposal.  A proposal is evidence only after the orchestrator records and
evaluates it.  The production worker backend uses Amp; tests inject a fake
backend and never start a worker process or access the network.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol


GOALS = ("RELIABLE", "PERSISTENT", "AUTONOMOUS")
TERMINAL_GENERATION = {"GENERATION_FAILED", "EVALUATION_FAILED", "TIMED_OUT"}

FAILURE_CLASSES = (
    "GENERATION_TRUNCATED",
    "EMPTY_ARTIFACT",
    "TOOL_CALL_INSTEAD_OF_DIFF",
    "INVALID_DIFF",
    "SCOPE_VIOLATION",
    "STALE_PARENT",
    "DUPLICATE_PATCH",
    "PARENT_GATE_FAILED",
    "CANDIDATE_GATE_FAILED",
    "SCORE_UNVERIFIED",
    "FAILED_CANONICAL_REVALIDATION",
    "EVALUATOR_UNHEALTHY",
    "CHECKOUT_FAILED",
    "CHAMPION_CHANGED_OUTSIDE_COORDINATOR",
    "STUCK_OR_BLOCKED",
    "GENERATION_FAILED",
    "EVALUATION_FAILED",
    "TIMED_OUT",
)


class OrchestratorError(RuntimeError):
    """A deterministic orchestration error."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now_iso(clock: Callable[[], float]) -> str:
    # The timestamp is evidence metadata only.  It never enters a decision.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def patch_fingerprint(diff: str) -> str:
    normalized = diff.replace("\r\n", "\n").replace("\r", "\n").strip("\n") + "\n"
    return sha256_text(normalized)


@dataclasses.dataclass(frozen=True)
class ProposalSlot:
    slot_id: str
    lens: str
    unique_seed: str


@dataclasses.dataclass
class CandidateProposal:
    candidate_id: str
    parent_sha: str
    patch: str
    new_test_id: str
    worker_id: str
    proposal_slot: str
    unique_seed: str
    changed_files: list[str] = dataclasses.field(default_factory=list)
    changed_functions: list[str] = dataclasses.field(default_factory=list)
    hypothesis: str = ""
    artifact_type: str = "diff"

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        worker_id: str,
        slot: ProposalSlot,
        batch_id: int,
    ) -> "CandidateProposal":
        candidate_id = str(payload.get("candidate_id") or f"candidate-{batch_id}-{slot.slot_id}")
        patch = payload.get("diff", payload.get("patch", ""))
        if not isinstance(patch, str):
            patch = ""
        return cls(
            candidate_id=candidate_id,
            parent_sha=str(payload.get("parent_sha", "")),
            patch=patch,
            new_test_id=str(payload.get("new_test_id", "")),
            worker_id=worker_id,
            proposal_slot=slot.slot_id,
            unique_seed=slot.unique_seed,
            changed_files=[str(item) for item in payload.get("changed_files", []) if isinstance(item, str)],
            changed_functions=[str(item) for item in payload.get("changed_functions", []) if isinstance(item, str)],
            hypothesis=str(payload.get("hypothesis", "")),
            artifact_type=str(payload.get("artifact_type", "diff")),
        )


@dataclasses.dataclass
class WorkerResult:
    worker_id: str
    slot: ProposalSlot
    generation_status: str
    candidate: CandidateProposal | None = None
    transcript_digest: str = ""
    terminal_reason: str = ""


@dataclasses.dataclass
class SuiteResult:
    command: list[str]
    passed: bool
    returncode: int
    skipped_tests: list[str] = dataclasses.field(default_factory=list)
    output_digest: str = ""
    harness_fault: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ScoreEvidence:
    active_score: float
    per_goal_scores: dict[str, float | None]
    per_fixture_scores: list[float]
    fixture_traces: list[dict[str, Any]]
    fixture_traces_digest: str
    suite_result: dict[str, Any]
    verified: bool = True

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CandidateEvaluation:
    valid: bool
    failure_class: str | None
    parent_gate: dict[str, Any]
    candidate_gate: dict[str, Any]
    evidence: ScoreEvidence | None = None
    detail: str = ""


@dataclasses.dataclass
class DiffValidation:
    valid: bool
    failure_class: str | None
    changed_files: list[str] = dataclasses.field(default_factory=list)
    changed_functions: list[str] = dataclasses.field(default_factory=list)
    hunk_count: int = 0
    added_lines: int = 0
    removed_lines: int = 0
    detail: str = ""


class WorkerBackend(Protocol):
    def launch(self, *, prompt: str, slot: ProposalSlot, checkout: Path) -> Any:
        ...

    def poll(self, handle: Any) -> WorkerResult | None:
        ...

    def stop(self, handle: Any) -> None:
        ...


class Evaluator(Protocol):
    environment_digest: str

    def selftest(self) -> bool:
        ...

    def initial(self, checkout: Path, champion_sha: str) -> ScoreEvidence:
        ...

    def candidate(
        self,
        proposal: CandidateProposal,
        *,
        parent_test_checkout: Path,
        candidate_checkout: Path,
        parent_suite: Mapping[str, Any],
    ) -> CandidateEvaluation:
        ...


class AtomicStore:
    """Atomically replaced state plus append-only JSONL events."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.events_path = self.root / "events.jsonl"

    def read_state(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def write_state(self, state: Mapping[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, indent=2, ensure_ascii=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.state_path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def append_event(self, event: Mapping[str, Any]) -> None:
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class GitWorkspace:
    """Read-only source plus disposable clones under the system temp area."""

    def __init__(self, source: Path, scratch: Path, canonical_path: Path) -> None:
        self.source = source.resolve()
        self.scratch = scratch
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.canonical = canonical_path.resolve()
        if self.canonical.exists():
            if not (self.canonical / ".git").exists():
                raise OrchestratorError(f"durable canonical path is not a Git checkout: {self.canonical}")
        else:
            self.canonical.parent.mkdir(parents=True, exist_ok=True)
            self._clone_repo(self.source, self.canonical)

    @staticmethod
    def _git(path: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            raise OrchestratorError(f"git command failed: {' '.join(args)}: {result.stderr.strip()}")
        return result

    @staticmethod
    def _clone_repo(source: Path, destination: Path) -> None:
        if not source.is_dir():
            raise OrchestratorError(f"source checkout does not exist: {source}")
        result = subprocess.run(
            ["git", "clone", "--no-hardlinks", "--quiet", str(source), str(destination)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise OrchestratorError(f"source clone failed: {result.stderr.strip()}")

    def source_is_clean(self) -> bool:
        return not bool(self._git(self.source, ["status", "--porcelain"], check=True).stdout.strip())

    def resolve_sha(self, branch: str) -> str:
        return self._git(self.source, ["rev-parse", f"{branch}^{{commit}}"]).stdout.strip()

    def sha(self, checkout: Path | None = None) -> str:
        return self._git(checkout or self.canonical, ["rev-parse", "HEAD"]).stdout.strip()

    def fresh_checkout(self, parent_sha: str, label: str) -> Path:
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label)
        destination = self.scratch / f"checkout-{safe_label}"
        if destination.exists():
            shutil.rmtree(destination)
        self._clone_repo(self.canonical, destination)
        self._git(destination, ["checkout", "--quiet", "--detach", parent_sha])
        self._git(destination, ["clean", "-fdx"])
        return destination

    def apply_diff(self, checkout: Path, diff: str, *, include: str | None = None) -> None:
        check_args = ["apply", "--check", "--whitespace=nowarn"]
        apply_args = ["apply", "--whitespace=nowarn"]
        if include:
            check_args.extend(["--include", include])
            apply_args.extend(["--include", include])
        for args in (check_args, apply_args):
            result = self._git_with_input(checkout, args, diff)
            if result.returncode != 0:
                raise OrchestratorError(result.stderr.strip() or "git apply failed")

    @staticmethod
    def _git_with_input(checkout: Path, args: list[str], content: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            input=content,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def reset(self, checkout: Path, sha: str) -> None:
        self._git(checkout, ["reset", "--hard", sha])
        self._git(checkout, ["clean", "-fdx"])

    def commit(self, checkout: Path, candidate_id: str, commit_epoch: int) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "orchestrator",
            "GIT_AUTHOR_EMAIL": "orchestrator@example.invalid",
            "GIT_COMMITTER_NAME": "orchestrator",
            "GIT_COMMITTER_EMAIL": "orchestrator@example.invalid",
            "GIT_AUTHOR_DATE": f"@{commit_epoch} +0000",
            "GIT_COMMITTER_DATE": f"@{commit_epoch} +0000",
        }
        subprocess.run(
            ["git", "-C", str(checkout), "add", "-A"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "commit", "--no-verify", "-m", f"orchestrator: accept {candidate_id}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            env=env,
        )
        return self.sha(checkout)


class AmpWorkerBackend:
    """Asynchronous Amp launcher with durable transcript reads and cleanup."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.handles: list[dict[str, str]] = []
        self._resumed: set[str] = set()
        self._archived: set[str] = set()

    def launch(self, *, prompt: str, slot: ProposalSlot, checkout: Path) -> dict[str, str]:
        command = ["amp", "--visibility", "private", "--no-notifications"]
        agent_mode = self.config.get("orb_agent_mode")
        if agent_mode:
            command.extend(["--mode", str(agent_mode)])
        orb_size = self.config.get("orb_size")
        if orb_size:
            command.extend(["--orb-size", str(orb_size)])
        command.extend(["-ox", prompt])
        result = subprocess.run(
            command,
            cwd=checkout,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise OrchestratorError(f"Amp launch failed for {slot.slot_id}: {result.stderr.strip()}")
        thread_id = self._thread_id(result.stdout)
        if not thread_id:
            raise OrchestratorError(f"Amp launch returned no thread URL for {slot.slot_id}")
        handle = {"thread_id": thread_id, "slot_id": slot.slot_id, "checkout": str(checkout)}
        self.handles.append(handle)
        return handle

    @staticmethod
    def _thread_id(output: str) -> str | None:
        matches = re.findall(r"(?:threads/|thread[-_])([A-Za-z0-9._:-]+)", output)
        if matches:
            return matches[-1].rstrip("/.,")
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if candidate.startswith("http"):
                return candidate.rstrip("/ ")
        return None

    def poll(self, handle: dict[str, str]) -> WorkerResult | None:
        result = subprocess.run(
            ["amp", "threads", "export", handle["thread_id"]],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            return None
        payload = parse_worker_payload(result.stdout)
        if payload is None:
            # Orbs may auto-pause. Resume once, then let the barrier keep
            # polling. Transcript prose is never completion evidence.
            thread_id = handle["thread_id"]
            if thread_id not in self._resumed:
                self._resumed.add(thread_id)
                subprocess.run(
                    [
                        "amp",
                        "--no-color",
                        "--no-notifications",
                        "--visibility",
                        "private",
                        "--orb-execute",
                        "--execute",
                        "Return exactly one CHITRA_CANDIDATE_V1 JSON envelope or an explicit terminal status.",
                        "--no-archive-after-execute",
                        "threads",
                        "continue",
                        thread_id,
                    ],
                    cwd=Path(handle["checkout"]),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            return None
        slot = ProposalSlot(handle["slot_id"], "exported", "")
        status = str(payload.get("generation_status", payload.get("status", "COMPLETE")))
        if status in TERMINAL_GENERATION:
            return WorkerResult(handle["thread_id"], slot, status, transcript_digest=sha256_text(result.stdout))
        proposal = CandidateProposal.from_payload(
            payload,
            worker_id=handle["thread_id"],
            slot=slot,
            batch_id=int(payload.get("batch_id", 0) or 0),
        )
        return WorkerResult(
            worker_id=handle["thread_id"],
            slot=slot,
            generation_status="COMPLETE",
            candidate=proposal,
            transcript_digest=sha256_text(result.stdout),
        )

    def stop(self, handle: dict[str, str]) -> None:
        thread_id = handle["thread_id"]
        if thread_id in self._archived:
            return
        subprocess.run(
            ["amp", "threads", "archive", thread_id],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self._archived.add(thread_id)

    def cleanup(self) -> None:
        for handle in list(self.handles):
            self.stop(handle)
        self.handles.clear()
        self._resumed.clear()
        self._archived.clear()


def parse_worker_payload(transcript: str) -> dict[str, Any] | None:
    """Read only the structured candidate envelope, never worker prose."""
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", transcript, re.DOTALL):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    decoder = json.JSONDecoder()
    for index, char in enumerate(transcript):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(transcript[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and ("diff" in value or "generation_status" in value or "status" in value):
            candidates.append(value)
    unique_candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = canonical_json(candidate)
        if key not in seen:
            seen.add(key)
            unique_candidates.append(candidate)
    if len(unique_candidates) != 1:
        return None
    return unique_candidates[0]


class SubprocessEvaluator:
    """Evaluator for the configured objective and pytest gate."""

    def __init__(self, config: Mapping[str, Any], *, workdir: Path) -> None:
        self.config = config
        self.workdir = workdir
        digest_input = {
            "objective_command": config["objective_command"],
            "suite_command": config.get("suite_command", config.get("full_suite_command", "")),
            "selftest_command": config.get("selftest_command", config.get("objective_selftest_command", "")),
            "python": sys.version,
        }
        self.environment_digest = sha256_text(canonical_json(digest_input))

    def _run(self, command: list[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged = dict(os.environ)
        if env:
            merged.update(env)
        return subprocess.run(
            command,
            cwd=cwd,
            env=merged,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def selftest(self) -> bool:
        command = shlex.split(str(self.config.get("selftest_command", self.config.get("objective_selftest_command", ""))))
        if not command:
            return True
        result = self._run(command, cwd=self.workdir)
        return result.returncode == 0

    def _suite(self, checkout: Path) -> SuiteResult:
        command = shlex.split(str(self.config.get("suite_command", self.config.get("full_suite_command", ""))))
        env: dict[str, str] = {}
        while command and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", command[0]):
            key, value = command.pop(0).split("=", 1)
            env[key] = value
        skipped: list[str] = []
        if "tests/test_boardd_app.py" in command:
            command = [item for item in command if item != "tests/test_boardd_app.py"]
        fastapi_check = self._run(
            [sys.executable, "-c", "import fastapi"], cwd=checkout
        )
        if fastapi_check.returncode != 0:
            ignored = "tests/test_boardd_app.py"
            if "tests/" in command or "tests" in command or ignored in command:
                command = [*command, "--ignore", ignored]
                skipped.append(ignored)
        env.setdefault("PYTHONPATH", "src")
        result = self._run(command, cwd=checkout, env=env)
        output = result.stdout or ""
        harness_fault = "ModuleNotFoundError: No module named 'chitra'" in output
        return SuiteResult(
            command=command,
            passed=result.returncode == 0,
            returncode=result.returncode,
            skipped_tests=skipped,
            output_digest=sha256_text(output),
            harness_fault=harness_fault,
        )

    def full_suite(self, checkout: Path) -> SuiteResult:
        """Run a fresh full-suite check for canonical revalidation."""
        return self._suite(checkout)

    def _score(self, checkout: Path, suite: Mapping[str, Any]) -> ScoreEvidence:
        command = shlex.split(str(self.config["objective_command"]))
        replaced = [str(checkout) if item == "<checkout>" else item for item in command]
        result = self._run(replaced, cwd=self.workdir)
        output = result.stdout or ""
        if result.returncode != 0:
            raise OrchestratorError(f"objective failed: {output[-500:]}")
        active_goal = str(self.config["active_goal"])
        score_pattern = str(self.config.get("score_pattern", r"CHITRA HEAD SCORE:\s*([0-9]+(?:\.[0-9]+)?)"))
        match = re.search(score_pattern, output)
        if not match:
            raise OrchestratorError("objective output contained no verified score")
        active_score = float(match.group(1))
        traces: list[dict[str, Any]] = []
        for line in output.splitlines():
            if not line.startswith("fixture=") or "adapter=chitra-head" not in line:
                continue
            values: dict[str, Any] = {}
            for key, value in re.findall(r"(fixture|kind|adapter|interventions|nudges|relaunches|intermediate_pass|final_goal|score|actions|flags)=([^ ]+)", line):
                values[key] = value
            if "fixture" in values:
                traces.append(values)
        traces.sort(key=lambda item: int(str(item["fixture"])))
        per_fixture = [float(item["score"]) for item in traces if "score" in item]
        per_goal = {goal: None for goal in GOALS}
        per_goal[active_goal] = active_score
        return ScoreEvidence(
            active_score=active_score,
            per_goal_scores=per_goal,
            per_fixture_scores=per_fixture,
            fixture_traces=traces,
            fixture_traces_digest=sha256_text(canonical_json(traces)),
            suite_result=dict(suite),
            verified=True,
        )

    def initial(self, checkout: Path, champion_sha: str) -> ScoreEvidence:
        suite = self._suite(checkout)
        if not suite.passed:
            raise OrchestratorError("initial full suite failed")
        return self._score(checkout, suite.as_dict())

    def _test(self, checkout: Path, test_id: str) -> tuple[bool, bool, str]:
        command = [sys.executable, "-m", "pytest", test_id, "-q"]
        result = self._run(command, cwd=checkout, env={"PYTHONPATH": "src"})
        output = result.stdout or ""
        harness_fault = "ModuleNotFoundError: No module named 'chitra'" in output
        if harness_fault:
            return False, True, output
        if result.returncode == 0:
            return True, False, output
        return False, False, output

    def candidate(
        self,
        proposal: CandidateProposal,
        *,
        parent_test_checkout: Path,
        candidate_checkout: Path,
        parent_suite: Mapping[str, Any],
    ) -> CandidateEvaluation:
        parent_suite_pass = bool(parent_suite.get("passed"))
        if bool(parent_suite.get("harness_fault")):
            return CandidateEvaluation(False, "EVALUATOR_UNHEALTHY", {"full_suite": parent_suite}, {}, detail="parent suite harness fault")
        parent_test_pass, parent_harness_fault, parent_output = self._test(parent_test_checkout, proposal.new_test_id)
        parent_gate = {
            "new_test_fails": not parent_test_pass,
            "new_test_returned_harness_fault": parent_harness_fault,
            "full_suite_passes": parent_suite_pass,
            "output_digest": sha256_text(parent_output),
        }
        if parent_harness_fault:
            return CandidateEvaluation(False, "EVALUATOR_UNHEALTHY", parent_gate, {}, detail="parent regression test harness fault")
        if parent_test_pass or not parent_suite_pass:
            return CandidateEvaluation(False, "PARENT_GATE_FAILED", parent_gate, {}, detail="parent gate did not fail only the new test")

        candidate_test_pass, candidate_harness_fault, candidate_output = self._test(candidate_checkout, proposal.new_test_id)
        candidate_suite = self._suite(candidate_checkout)
        candidate_gate = {
            "new_test_passes": candidate_test_pass,
            "new_test_returned_harness_fault": candidate_harness_fault,
            "full_suite_passes": candidate_suite.passed,
            "suite_result": candidate_suite.as_dict(),
            "output_digest": sha256_text(candidate_output),
        }
        if candidate_harness_fault or candidate_suite.harness_fault:
            return CandidateEvaluation(False, "EVALUATOR_UNHEALTHY", parent_gate, candidate_gate, detail="candidate gate harness fault")
        if not candidate_test_pass or not candidate_suite.passed:
            return CandidateEvaluation(False, "CANDIDATE_GATE_FAILED", parent_gate, candidate_gate, detail="candidate gate failed")
        try:
            evidence = self._score(candidate_checkout, candidate_suite.as_dict())
        except OrchestratorError as exc:
            return CandidateEvaluation(False, "SCORE_UNVERIFIED", parent_gate, candidate_gate, detail=str(exc))
        return CandidateEvaluation(True, None, parent_gate, candidate_gate, evidence=evidence)


def _function_spans(source: str) -> list[tuple[str, int, int]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            spans.append((node.name, node.lineno, end))
    return spans


def _hunks(diff: str) -> list[tuple[str, int, int, int, int, list[str]]]:
    current_file = ""
    current: tuple[str, int, int, int, int, list[str]] | None = None
    result: list[tuple[str, int, int, int, int, list[str]]] = []
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for line in diff.replace("\r\n", "\n").splitlines():
        if line.startswith("diff --git a/"):
            match = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            if match:
                current_file = match.group(2)
        match = hunk_re.match(line)
        if match:
            if current is not None:
                result.append(current)
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            current = (current_file, old_start, old_count, new_start, new_count, [])
        elif current is not None and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            current[5].append(line)
    if current is not None:
        result.append(current)
    return result


def validate_diff(
    proposal: CandidateProposal,
    *,
    parent_checkout: Path,
    candidate_checkout: Path,
    config: Mapping[str, Any],
) -> DiffValidation:
    diff = proposal.patch
    if proposal.artifact_type in {"tool_call", "tool", "command"}:
        return DiffValidation(False, "TOOL_CALL_INSTEAD_OF_DIFF", detail="worker returned a tool call")
    if not diff.strip():
        return DiffValidation(False, "EMPTY_ARTIFACT", detail="worker returned no diff")
    if not re.search(r"^diff --git a/\S+ b/\S+$", diff, re.MULTILINE) or "@@" not in diff:
        return DiffValidation(False, "INVALID_DIFF", detail="complete unified diff headers and hunks are required")
    parsed_files: list[str] = []
    for match in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", diff, re.MULTILINE):
        left, right = match.groups()
        if left != right or ".." in right or right.startswith("/"):
            return DiffValidation(False, "INVALID_DIFF", detail="unsafe or rename diff path")
        parsed_files.append(right)
    if not parsed_files:
        return DiffValidation(False, "INVALID_DIFF")
    hunks = _hunks(diff)
    if not hunks or any(not item[0] for item in hunks):
        return DiffValidation(False, "INVALID_DIFF", detail="each file must contain a real hunk")
    if proposal.changed_files and sorted(set(proposal.changed_files)) != sorted(set(parsed_files)):
        return DiffValidation(False, "INVALID_DIFF", detail="changed file manifest does not match diff")
    if len(parsed_files) > int(config.get("max_changed_files", 2)):
        return DiffValidation(False, "SCOPE_VIOLATION", detail="too many files changed")
    test_files = [path for path in parsed_files if path.startswith("tests/") or "/tests/" in path or Path(path).name.startswith("test_")]
    source_files = [path for path in parsed_files if path not in test_files]
    if len(source_files) != 1 or len(test_files) != 1:
        return DiffValidation(False, "SCOPE_VIOLATION", detail="one source file and one regression-test file are required")
    if not proposal.new_test_id.startswith(test_files[0] + "::"):
        return DiffValidation(False, "SCOPE_VIOLATION", detail="new test id must name the changed test file")
    if not re.fullmatch(r"[A-Za-z0-9_.\-/]+::[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?", proposal.new_test_id) or ".." in proposal.new_test_id:
        return DiffValidation(False, "SCOPE_VIOLATION", detail="new test id is not a bounded pytest node id")
    if any("\n" in path or "\t" in path for path in parsed_files):
        return DiffValidation(False, "INVALID_DIFF", detail="invalid path")

    added = removed = 0
    source_added = source_removed = 0
    source_hunks = [item for item in hunks if item[0] == source_files[0]]
    for path, old_start, old_count, _new_start, _new_count, lines in hunks:
        source_path = parent_checkout / path
        old_lines = source_path.read_text(encoding="utf-8").splitlines() if source_path.exists() else []
        if old_start < 0 or old_start > len(old_lines) + 1 or old_count > len(old_lines):
            return DiffValidation(False, "INVALID_DIFF", detail=f"hunk range is implausible for {path}")
        for line in lines:
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
                if path == source_files[0]:
                    source_added += 1
            elif line.startswith("-"):
                removed += 1
                if path == source_files[0]:
                    source_removed += 1
    if not source_hunks:
        return DiffValidation(False, "INVALID_DIFF", detail="source file has no hunk")
    if not (candidate_checkout / source_files[0]).is_file() or not (candidate_checkout / test_files[0]).is_file():
        return DiffValidation(False, "SCOPE_VIOLATION", detail="candidate must retain source and regression-test files")
    max_source_lines = int(config.get("max_source_changed_lines", 48))
    if source_removed > max_source_lines or source_added > max_source_lines or source_added + source_removed > max_source_lines * 2:
        return DiffValidation(False, "SCOPE_VIOLATION", detail="changed hunk is too large")
    source_text = (parent_checkout / source_files[0]).read_text(encoding="utf-8")
    candidate_text = (candidate_checkout / source_files[0]).read_text(encoding="utf-8")
    old_spans = _function_spans(source_text)
    new_spans = _function_spans(candidate_text)
    changed_functions: set[str] = set()
    for _path, old_start, _old_count, new_start, _new_count, lines in source_hunks:
        old_position = old_start
        new_position = new_start
        changed_old_lines: list[int] = []
        changed_new_lines: list[int] = []
        for line in lines:
            if line.startswith("\\"):
                continue
            if line.startswith("-") and not line.startswith("---"):
                changed_old_lines.append(old_position)
                old_position += 1
            elif line.startswith("+") and not line.startswith("+++"):
                changed_new_lines.append(new_position)
                new_position += 1
            else:
                old_position += 1
                new_position += 1
        changed_functions.update(name for name, start, end in old_spans if any(start <= line <= end for line in changed_old_lines))
        changed_functions.update(name for name, start, end in new_spans if any(start <= line <= end for line in changed_new_lines))
    if len(changed_functions) != 1:
        return DiffValidation(False, "SCOPE_VIOLATION", detail="diff does not stay within one function")
    if proposal.changed_functions and sorted(set(proposal.changed_functions)) != sorted(changed_functions):
        return DiffValidation(False, "SCOPE_VIOLATION", detail="changed function manifest does not match diff")
    test_added_lines = 0
    for path, _old_start, _old_count, _new_start, _new_count, lines in hunks:
        if path in test_files:
            test_added_lines += sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    if test_added_lines == 0:
        return DiffValidation(False, "SCOPE_VIOLATION", detail="regression test has no added or changed assertion")
    source_size = max(1, len(source_text.splitlines()))
    if (source_added + source_removed) / source_size > float(config.get("max_source_changed_ratio", 0.50)):
        return DiffValidation(False, "SCOPE_VIOLATION", detail="source hunk is not a bounded change")
    return DiffValidation(
        True,
        None,
        changed_files=parsed_files,
        changed_functions=sorted(changed_functions),
        hunk_count=len(hunks),
        added_lines=added,
        removed_lines=removed,
    )


def default_state(config: Mapping[str, Any], *, environment_digest: str) -> dict[str, Any]:
    return {
        "campaign_id": str(config["campaign_id"]),
        "active_goal": str(config["active_goal"]),
        "champion_branch": str(config["champion_branch"]),
        "generation": 0,
        "next_batch_id": 1,
        "champion_sha": None,
        "champion_score": None,
        "per_goal_scores": {goal: None for goal in GOALS},
        "per_fixture_scores": [],
        "fixture_traces": [],
        "fixture_traces_digest": None,
        "champion_suite_result": None,
        "parent_sha": None,
        "accepted_candidate_id": None,
        "accepted_at": None,
        "valid_evaluation_count": 0,
        "consecutive_valid_non_improvements": 0,
        "consecutive_zero_valid_batches": 0,
        "healthy_no_improvement_batches": 0,
        "diversity_recovery_used": False,
        "tried_patch_fingerprints": [],
        "patch_fingerprint_sources": {},
        "failure_class_counts": {},
        "batch_status": "NOT_STARTED",
        "terminal_status": None,
        "evaluator_environment_digest": environment_digest,
        "batches": [],
    }


class Orchestrator:
    """The only object allowed to advance the champion."""

    def __init__(
        self,
        *,
        config: Mapping[str, Any],
        repo: Path,
        state_dir: Path,
        worker_backend: WorkerBackend | None = None,
        evaluator: Evaluator | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = dict(config)
        self.repo = repo.resolve()
        self.state_store = AtomicStore(state_dir.resolve())
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.write_lock = threading.Lock()
        existing = self.state_store.read_state()
        canonical_path = self.state_store.root / "canonical"
        if existing is None and canonical_path.exists():
            raise OrchestratorError("durable canonical checkout exists without a durable state record")
        scratch = Path(tempfile.mkdtemp(prefix="orchestrator-worktrees-", dir="/tmp"))
        self._scratch_root = scratch
        self.workspace = GitWorkspace(self.repo, scratch, canonical_path)
        self.evaluator = evaluator or SubprocessEvaluator(self.config, workdir=Path(__file__).resolve().parent)
        self.worker_backend = worker_backend or AmpWorkerBackend(self.config)
        self.state = existing or default_state(self.config, environment_digest=self.evaluator.environment_digest)
        self.events = self.state_store.read_events()
        if existing and existing.get("champion_sha") and self.workspace.sha() != existing["champion_sha"]:
            raise OrchestratorError("durable state champion SHA does not match the canonical checkout")
        if existing and existing.get("evaluator_environment_digest") != self.evaluator.environment_digest:
            raise OrchestratorError("durable state evaluator environment does not match the current evaluator")

    def close(self) -> None:
        cleanup = getattr(self.worker_backend, "cleanup", None)
        if callable(cleanup):
            cleanup()
        shutil.rmtree(self._scratch_root, ignore_errors=True)

    def __enter__(self) -> "Orchestrator":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _persist(self) -> None:
        self.state_store.write_state(self.state)

    def _event(self, kind: str, *, batch_id: int | None = None, candidate_id: str | None = None, **payload: Any) -> None:
        event = {
            "event": kind,
            "campaign_id": self.state["campaign_id"],
            "generation": self.state["generation"],
            "batch_id": batch_id,
            "candidate_id": candidate_id,
            "champion_sha": self.state.get("champion_sha"),
            "evaluator_environment_digest": self.state.get("evaluator_environment_digest"),
            "recorded_at": now_iso(self.clock),
            **payload,
        }
        try:
            self.state_store.append_event(event)
        except OSError as exc:
            self._failure("STUCK_OR_BLOCKED")
            self.state["terminal_status"] = "STUCK_OR_BLOCKED"
            self.state["storage_blocker"] = str(exc)
            try:
                self._persist()
            except OSError:
                pass
            raise OrchestratorError(f"append-only event storage failed: {exc}") from exc
        self.events.append(event)

    def _failure(self, failure_class: str) -> None:
        counts = self.state.setdefault("failure_class_counts", {})
        counts[failure_class] = int(counts.get(failure_class, 0)) + 1

    def _slots(self, batch_id: int, *, recovery: bool = False) -> list[ProposalSlot]:
        configured = list(self.config.get("diagnostic_slots", []))
        size = int(self.config.get("batch_size", 6))
        if len(configured) < size:
            configured.extend(f"diagnostic-lens-{index + 1}" for index in range(len(configured), size))
        campaign = str(self.state["campaign_id"])
        generation = int(self.state["generation"])
        slots: list[ProposalSlot] = []
        for index in range(size):
            configured_lens = configured[index]
            if isinstance(configured_lens, Mapping):
                lens = str(configured_lens.get("lens", configured_lens.get("id", f"diagnostic-lens-{index + 1}")))
            else:
                lens = str(configured_lens)
            prefix = "recovery" if recovery else "normal"
            seed = sha256_text(f"{campaign}:{generation}:{batch_id}:{prefix}:{index}:{lens}")[:24]
            slots.append(ProposalSlot(f"slot-{index + 1:02d}", lens, seed))
        return slots

    def _prompt(self, slot: ProposalSlot, parent_sha: str, checkout: Path, batch_id: int) -> str:
        parent_scores = canonical_json(self.state.get("per_goal_scores", {}))
        traces = canonical_json(self.state.get("fixture_traces", []))
        tried = canonical_json(
            [
                {"fingerprint": fingerprint, "candidate_id": self.state.get("patch_fingerprint_sources", {}).get(fingerprint)}
                for fingerprint in self.state.get("tried_patch_fingerprints", [])[-20:]
            ]
        )
        values = {
            "campaign_id": self.state["campaign_id"],
            "batch_id": batch_id,
            "parent_sha": parent_sha,
            "checkout": str(checkout),
            "slot_id": slot.slot_id,
            "lens": slot.lens,
            "unique_seed": slot.unique_seed,
            "active_goal": self.state["active_goal"],
            "parent_scores_json": parent_scores,
            "fixture_traces_json": traces,
            "tried_fingerprints_json": tried,
            "tried_patch_fingerprints": tried,
        }
        template = str(self.config["worker_prompt_template"])
        return template.format(**values)

    def bootstrap(self, *, initial_sha: str | None = None, evidence: ScoreEvidence | None = None) -> None:
        if self.state.get("champion_sha"):
            return
        if not self.workspace.source_is_clean():
            raise OrchestratorError("source checkout is not clean; refusing to use it as the initial champion")
        sha = initial_sha or self.workspace.resolve_sha(str(self.config["champion_branch"]))
        if self.workspace.sha() != sha:
            self.workspace._git(self.workspace.canonical, ["checkout", "--quiet", "--detach", sha])
        if not self.evaluator.selftest():
            self._failure("EVALUATOR_UNHEALTHY")
            self.state["batch_status"] = "WAITING"
            self.state["terminal_status"] = "STUCK_OR_BLOCKED"
            self._persist()
            self._event("WAITING", condition="evaluator self-test failed")
            return
        try:
            score = evidence or self.evaluator.initial(self.workspace.canonical, sha)
        except Exception as exc:
            self._failure("EVALUATOR_UNHEALTHY")
            self.state["batch_status"] = "WAITING"
            self.state["terminal_status"] = "STUCK_OR_BLOCKED"
            self._persist()
            self._event("WAITING", condition="initial evaluation failed", detail=str(exc))
            return
        self.state.update(
            {
                "campaign_started_at_epoch": self.clock(),
                "champion_sha": sha,
                "champion_score": score.active_score,
                "per_goal_scores": score.per_goal_scores,
                "per_fixture_scores": score.per_fixture_scores,
                "fixture_traces": score.fixture_traces,
                "fixture_traces_digest": score.fixture_traces_digest,
                "champion_suite_result": score.suite_result,
                "batch_status": "READY",
            }
        )
        self._persist()
        self._event(
            "INITIAL_EVALUATION_RECORDED",
            evaluation_valid=True,
            score=score.active_score,
            per_goal_scores=score.per_goal_scores,
            per_fixture_scores=score.per_fixture_scores,
            fixture_traces_digest=score.fixture_traces_digest,
            suite_result=score.suite_result,
            suite_passed=bool(score.suite_result.get("passed")),
        )
        self._persist()

    def _waiting_condition(self) -> str | None:
        conditions = self.config.get("launch_conditions", {})
        active = str(self.config["active_goal"])
        condition = conditions.get(active)
        if condition and not bool(condition.get("met", False)):
            return str(condition.get("description", f"launch condition for {active} is unmet"))
        return None

    def _prepare_parent_test_checkout(self, proposal: CandidateProposal, parent_sha: str) -> Path:
        checkout = self.workspace.fresh_checkout(parent_sha, f"parent-test-{proposal.candidate_id}")
        test_file = proposal.patch
        parsed = re.findall(r"^diff --git a/(\S+) b/(\S+)$", test_file, re.MULTILINE)
        test_paths = [right for left, right in parsed if left == right and (right.startswith("tests/") or "/tests/" in right or Path(right).name.startswith("test_"))]
        if len(test_paths) != 1:
            raise OrchestratorError("candidate must contain exactly one test file")
        self.workspace.apply_diff(checkout, proposal.patch, include=test_paths[0])
        return checkout

    def _evaluate_candidate(self, proposal: CandidateProposal, *, parent_sha: str, phase: str) -> tuple[CandidateEvaluation, DiffValidation]:
        parent_checkout = self.workspace.fresh_checkout(parent_sha, f"validate-parent-{proposal.candidate_id}")
        candidate_checkout = self.workspace.fresh_checkout(parent_sha, f"candidate-{proposal.candidate_id}")
        try:
            self.workspace.apply_diff(candidate_checkout, proposal.patch)
        except Exception as exc:
            return CandidateEvaluation(False, "INVALID_DIFF", {}, {}, detail=str(exc)), DiffValidation(False, "INVALID_DIFF", detail=str(exc))
        validation = validate_diff(
            proposal,
            parent_checkout=parent_checkout,
            candidate_checkout=candidate_checkout,
            config=self.config,
        )
        if not validation.valid:
            return CandidateEvaluation(False, validation.failure_class, {}, {}, detail=validation.detail), validation
        proposal.changed_files = validation.changed_files
        proposal.changed_functions = validation.changed_functions
        parent_test_checkout = self._prepare_parent_test_checkout(proposal, parent_sha)
        evaluation = self.evaluator.candidate(
            proposal,
            parent_test_checkout=parent_test_checkout,
            candidate_checkout=candidate_checkout,
            parent_suite=self.state["champion_suite_result"],
        )
        return evaluation, validation

    def _record_candidate(self, proposal: CandidateProposal | None, result: WorkerResult, batch_id: int, *, failure: str | None = None, detail: str = "", evaluation: CandidateEvaluation | None = None, validation: DiffValidation | None = None, duplicate_of: str | None = None) -> dict[str, Any]:
        candidate_id = proposal.candidate_id if proposal else f"{batch_id}-{result.slot.slot_id}"
        current_batch = self.state.get("current_batch") or {}
        recorded_parent = proposal.parent_sha if proposal else current_batch.get("parent_sha", self.state.get("champion_sha"))
        record: dict[str, Any] = {
            "candidate_id": candidate_id,
            "batch_id": batch_id,
            "worker_id": result.worker_id,
            "proposal_slot": result.slot.slot_id,
            "unique_seed": result.slot.unique_seed,
            "parent_sha": recorded_parent,
            "patch_digest": patch_fingerprint(proposal.patch) if proposal and proposal.patch else None,
            "diff": proposal.patch if proposal else None,
            "changed_files": validation.changed_files if validation else (proposal.changed_files if proposal else []),
            "changed_functions": validation.changed_functions if validation else (proposal.changed_functions if proposal else []),
            "generation_status": result.generation_status,
            "evaluation_status": "NOT_RUN" if evaluation is None else ("VALID" if evaluation.valid else "FAILED"),
            "candidate_score": evaluation.evidence.active_score if evaluation and evaluation.evidence else None,
            "rank": None,
            "per_goal_scores": evaluation.evidence.per_goal_scores if evaluation and evaluation.evidence else None,
            "per_fixture_scores": evaluation.evidence.per_fixture_scores if evaluation and evaluation.evidence else None,
            "fixture_traces_digest": evaluation.evidence.fixture_traces_digest if evaluation and evaluation.evidence else None,
            "new_test_id": proposal.new_test_id if proposal else None,
            "parent_gate": evaluation.parent_gate if evaluation else None,
            "candidate_gate": evaluation.candidate_gate if evaluation else None,
            "canonical_revalidation": None,
            "rejection_reason": detail or (evaluation.detail if evaluation else ""),
            "failure_class": failure or (evaluation.failure_class if evaluation else None),
            "duplicate_of": duplicate_of,
            "recorded_at": now_iso(self.clock),
        }
        self._event("CANDIDATE_RECORDED", batch_id=batch_id, candidate_id=candidate_id, record=record)
        if record["failure_class"]:
            self._failure(str(record["failure_class"]))
        return record

    def _rank_key(self, record: Mapping[str, Any]) -> tuple[Any, ...]:
        score = float(record.get("candidate_score") or 0.0)
        delta = score - float(self.state.get("champion_score") or 0.0)
        return (-score, -delta, str(record.get("patch_digest") or ""), str(record.get("candidate_id") or ""))

    def _recorded_valid_evaluations(self) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if event.get("event") == "CANDIDATE_EVALUATED"
            and event.get("evaluation_valid") is True
            and event.get("evaluation_phase", "worker") == "worker"
        ]

    def completion_status(self) -> str | None:
        # Only durable evaluation events are evidence.  State markers and text
        # returned by workers are deliberately ignored here.
        evaluations = [
            event
            for event in self.events
            if event.get("event") in {"INITIAL_EVALUATION_RECORDED", "CANDIDATE_EVALUATED"}
            and event.get("evaluation_valid") is True
        ]
        current_sha = self.state.get("champion_sha")
        started_at = self.state.get("campaign_started_at_epoch")
        minimum_runtime = float(self.config.get("minimum_runtime_seconds", 0))
        minimum_runtime_elapsed = (
            minimum_runtime <= 0
            or (
                started_at is not None
                and self.clock() >= float(started_at) + minimum_runtime
            )
        )
        active_goal = str(self.state["active_goal"])
        target = self.config.get("target_scores", {}).get(active_goal)
        if target is not None:
            for event in evaluations:
                is_initial = event.get("event") == "INITIAL_EVALUATION_RECORDED"
                is_canonical = event.get("event") == "CANDIDATE_EVALUATED" and event.get("evaluation_phase") == "canonical_revalidation"
                if (
                    minimum_runtime_elapsed
                    and (is_initial or is_canonical)
                    and event.get("champion_sha") == current_sha
                    and float(event.get("score", -1)) >= float(target)
                    and event.get("suite_passed") is True
                ):
                    return "TARGET_REACHED"
        stop = self.config.get("stop_rule", {})
        min_valid = int(stop.get("min_valid_evaluations", 18))
        plateau_batches = int(stop.get("plateau_batches", 3))
        candidate_evaluations = [
            event
            for event in evaluations
            if event.get("event") == "CANDIDATE_EVALUATED"
            and event.get("evaluation_phase", "worker") == "worker"
        ]
        consecutive_non_improvements = int(stop.get("consecutive_non_improvements", 5))
        if (
            minimum_runtime_elapsed
            and len(candidate_evaluations) >= min_valid
            and int(self.state.get("consecutive_valid_non_improvements", 0)) >= consecutive_non_improvements
            and int(self.state.get("healthy_no_improvement_batches", 0)) >= plateau_batches
        ):
            return "PLATEAUED"
        if self.state.get("terminal_status") in {"GENERATION_FAILED", "STUCK_OR_BLOCKED", "BUDGET_EXHAUSTED"}:
            return str(self.state["terminal_status"])
        return None

    def _poll_barrier(self, handles: list[tuple[ProposalSlot, Any]], batch_id: int) -> list[WorkerResult]:
        pending = list(handles)
        results: dict[str, WorkerResult] = {}
        timeout = float(self.config.get("worker_timeout_seconds", 300))
        started = self.clock()
        try:
            while pending:
                next_pending: list[tuple[ProposalSlot, Any]] = []
                for slot, handle in pending:
                    observed = self.worker_backend.poll(handle)
                    if observed is None:
                        next_pending.append((slot, handle))
                        continue
                    observed.slot = slot
                    if observed.candidate is not None:
                        observed.candidate.proposal_slot = slot.slot_id
                        observed.candidate.unique_seed = slot.unique_seed
                    results[slot.slot_id] = observed
                    self._event(
                        "WORKER_RESULT_STORED",
                        batch_id=batch_id,
                        worker_id=observed.worker_id,
                        slot=slot.slot_id,
                        generation_status=observed.generation_status,
                        candidate_id=observed.candidate.candidate_id if observed.candidate else None,
                        transcript_digest=observed.transcript_digest,
                    )
                pending = next_pending
                if not pending:
                    break
                if self.clock() - started >= timeout:
                    for slot, handle in pending:
                        result = WorkerResult(
                            worker_id=str(getattr(handle, "worker_id", slot.slot_id)),
                            slot=slot,
                            generation_status="TIMED_OUT",
                            terminal_reason="configured worker timeout",
                        )
                        results[slot.slot_id] = result
                        self._event("WORKER_RESULT_STORED", batch_id=batch_id, slot=slot.slot_id, generation_status="TIMED_OUT")
                    break
                self.sleep(float(self.config.get("poll_interval_seconds", 0.25)))
        finally:
            for _slot, handle in handles:
                self.worker_backend.stop(handle)
        return [results[slot.slot_id] for slot, _handle in handles]

    def _advance_or_retain(self, records: list[dict[str, Any]], *, parent_sha: str, batch_id: int) -> dict[str, Any] | None:
        eligible = [
            record
            for record in records
            if record.get("evaluation_status") == "VALID"
            and record.get("candidate_score") is not None
            and float(record["candidate_score"]) > float(self.state["champion_score"])
            and record.get("parent_sha") == parent_sha
        ]
        ordered = sorted(eligible, key=self._rank_key)
        for rank, record in enumerate(ordered, start=1):
            record["rank"] = rank
        self._event("CANDIDATES_RANKED", batch_id=batch_id, ranked_candidate_ids=[record["candidate_id"] for record in ordered])
        for record in ordered:
            with self.write_lock:
                if self.workspace.sha() != parent_sha:
                    record["failure_class"] = "CHAMPION_CHANGED_OUTSIDE_COORDINATOR"
                    record["rejection_reason"] = "canonical SHA changed outside orchestrator"
                    self._failure("CHAMPION_CHANGED_OUTSIDE_COORDINATOR")
                    self.state["terminal_status"] = "STUCK_OR_BLOCKED"
                    self._event("CHAMPION_CHANGED_OUTSIDE_COORDINATOR", batch_id=batch_id, candidate_id=record["candidate_id"])
                    return None
                try:
                    old_champion_score = float(self.state["champion_score"])
                    self.workspace.apply_diff(self.workspace.canonical, str(record["diff"]))
                except Exception as exc:
                    self.workspace.reset(self.workspace.canonical, parent_sha)
                    record["canonical_revalidation"] = {"passed": False, "detail": str(exc)}
                    record["failure_class"] = "FAILED_CANONICAL_REVALIDATION"
                    record["rejection_reason"] = str(exc)
                    self._failure("FAILED_CANONICAL_REVALIDATION")
                    self._event("FAILED_CANONICAL_REVALIDATION", batch_id=batch_id, candidate_id=record["candidate_id"], detail=str(exc))
                    continue
                proposal = CandidateProposal(
                    candidate_id=str(record["candidate_id"]),
                    parent_sha=parent_sha,
                    patch=str(record["diff"]),
                    new_test_id=str(record["new_test_id"]),
                    worker_id=str(record["worker_id"]),
                    proposal_slot=str(record["proposal_slot"]),
                    unique_seed=str(record["unique_seed"]),
                    changed_files=list(record.get("changed_files") or []),
                    changed_functions=list(record.get("changed_functions") or []),
                )
                parent_test_checkout = self._prepare_parent_test_checkout(proposal, parent_sha)
                parent_suite = self.state["champion_suite_result"]
                parent_suite_checkout: Path | None = None
                try:
                    full_suite = getattr(self.evaluator, "full_suite", None)
                    if callable(full_suite):
                        parent_suite_checkout = self.workspace.fresh_checkout(parent_sha, f"canonical-parent-suite-{record['candidate_id']}")
                        parent_suite_value = full_suite(parent_suite_checkout)
                        parent_suite = parent_suite_value.as_dict() if hasattr(parent_suite_value, "as_dict") else dict(parent_suite_value)
                    final_evaluation = self.evaluator.candidate(
                        proposal,
                        parent_test_checkout=parent_test_checkout,
                        candidate_checkout=self.workspace.canonical,
                        parent_suite=parent_suite,
                    )
                except Exception as exc:
                    final_evaluation = CandidateEvaluation(
                        False,
                        "FAILED_CANONICAL_REVALIDATION",
                        {},
                        {},
                        detail=str(exc),
                    )
                if not final_evaluation.valid or not final_evaluation.evidence or final_evaluation.evidence.active_score <= float(self.state["champion_score"]):
                    self.workspace.reset(self.workspace.canonical, parent_sha)
                    record["canonical_revalidation"] = {
                        "passed": False,
                        "failure_class": final_evaluation.failure_class or "SCORE_UNVERIFIED",
                        "detail": final_evaluation.detail,
                    }
                    record["failure_class"] = "FAILED_CANONICAL_REVALIDATION"
                    record["rejection_reason"] = final_evaluation.detail or "canonical four-condition gate or score failed"
                    self._failure("FAILED_CANONICAL_REVALIDATION")
                    self._event("FAILED_CANONICAL_REVALIDATION", batch_id=batch_id, candidate_id=record["candidate_id"], detail=record["rejection_reason"])
                    continue
                new_sha = self.workspace.commit(
                    self.workspace.canonical,
                    str(record["candidate_id"]),
                    int(self.config.get("commit_epoch", 0)) + int(self.state["generation"]) + 1,
                )
                score = final_evaluation.evidence
                self.state.update(
                    {
                        "champion_sha": new_sha,
                        "champion_score": score.active_score,
                        "per_goal_scores": score.per_goal_scores,
                        "per_fixture_scores": score.per_fixture_scores,
                        "fixture_traces": score.fixture_traces,
                        "fixture_traces_digest": score.fixture_traces_digest,
                        "champion_suite_result": score.suite_result,
                        "parent_sha": parent_sha,
                        "accepted_candidate_id": record["candidate_id"],
                        "accepted_at": now_iso(self.clock),
                        "generation": int(self.state["generation"]) + 1,
                    }
                )
                record["canonical_revalidation"] = {
                    "passed": True,
                    "score": score.active_score,
                    "champion_sha": new_sha,
                }
                # Canonical commit and state are durable before the next batch.
                self._event(
                    "CANDIDATE_EVALUATED",
                    batch_id=batch_id,
                    candidate_id=record["candidate_id"],
                    evaluation_valid=True,
                    evaluation_phase="canonical_revalidation",
                    score=score.active_score,
                    delta=score.active_score - old_champion_score,
                    suite_passed=bool(score.suite_result.get("passed")),
                )
                self._persist()
                self._event(
                    "CHAMPION_ADVANCED",
                    batch_id=batch_id,
                    candidate_id=record["candidate_id"],
                    old_champion_sha=parent_sha,
                    new_champion_sha=new_sha,
                    score=score.active_score,
                    per_goal_scores=score.per_goal_scores,
                    per_fixture_scores=score.per_fixture_scores,
                    fixture_traces_digest=score.fixture_traces_digest,
                    suite_passed=bool(score.suite_result.get("passed")),
                )
                return record
        self._event("BATCH_NO_WINNER", batch_id=batch_id)
        return None

    def run_batch(self, *, results_override: Iterable[WorkerResult] | None = None, recovery: bool = False) -> dict[str, Any]:
        if not self.state.get("champion_sha"):
            raise OrchestratorError("bootstrap must complete before a batch")
        if self.state.get("terminal_status"):
            return {"status": self.state["terminal_status"]}
        batch_id = int(self.state["next_batch_id"])
        parent_sha = str(self.state["champion_sha"])
        slots = self._slots(batch_id, recovery=recovery)
        self.state["batch_status"] = "OPEN"
        self.state["current_batch"] = {"batch_id": batch_id, "parent_sha": parent_sha, "slots": [dataclasses.asdict(slot) for slot in slots]}
        self._persist()
        self._event("BATCH_OPEN", batch_id=batch_id, parent_sha=parent_sha, slots=[dataclasses.asdict(slot) for slot in slots])
        handles: list[tuple[ProposalSlot, Any]] = []
        try:
            if results_override is not None:
                results = list(results_override)
                if len(results) != len(slots):
                    raise OrchestratorError("results_override must cover every slot")
                expected_slots = {slot.slot_id for slot in slots}
                result_slots = {result.slot.slot_id for result in results}
                if result_slots != expected_slots:
                    raise OrchestratorError("results_override must contain each barrier slot exactly once")
            else:
                for slot in slots:
                    checkout = self.workspace.fresh_checkout(parent_sha, f"worker-{batch_id}-{slot.slot_id}")
                    prompt = self._prompt(slot, parent_sha, checkout, batch_id)
                    try:
                        handle = self.worker_backend.launch(prompt=prompt, slot=slot, checkout=checkout)
                        handles.append((slot, handle))
                    except Exception as exc:
                        handles.append((slot, type("FailedHandle", (), {"worker_id": slot.slot_id, "error": str(exc)})()))
                # Failed launches are explicit terminal results.  A successful
                # launch is polled until it returns structured data or timeout.
                results = []
                pollable: list[tuple[ProposalSlot, Any]] = []
                for slot, handle in handles:
                    if hasattr(handle, "error"):
                        results.append(WorkerResult(slot.slot_id, slot, "GENERATION_FAILED", terminal_reason=str(handle.error)))
                    else:
                        pollable.append((slot, handle))
                if pollable:
                    results.extend(self._poll_barrier(pollable, batch_id))
                results.sort(key=lambda result: result.slot.slot_id)
            self._event("BATCH_BARRIER", batch_id=batch_id, result_count=len(results), result_slots=[result.slot.slot_id for result in results])
            records: list[dict[str, Any]] = []
            batch_fingerprints: dict[str, str] = {}
            valid_evaluations = 0
            champion_before = {
                "champion_sha": self.state["champion_sha"],
                "score": self.state["champion_score"],
                "per_goal_scores": self.state.get("per_goal_scores"),
                "per_fixture_scores": self.state.get("per_fixture_scores"),
                "fixture_traces_digest": self.state.get("fixture_traces_digest"),
                "suite_result": self.state.get("champion_suite_result"),
            }
            for result in sorted(results, key=lambda item: item.slot.slot_id):
                proposal = result.candidate
                if proposal is not None:
                    self._event(
                        "PROPOSAL_RECORDED",
                        batch_id=batch_id,
                        candidate_id=proposal.candidate_id,
                        worker_id=result.worker_id,
                        slot=result.slot.slot_id,
                        parent_sha=proposal.parent_sha,
                        patch_digest=patch_fingerprint(proposal.patch) if proposal.patch else None,
                    )
                if result.generation_status in TERMINAL_GENERATION or proposal is None:
                    failure = result.generation_status if result.generation_status in FAILURE_CLASSES else "EMPTY_ARTIFACT"
                    record = self._record_candidate(proposal, result, batch_id, failure=failure, detail=result.terminal_reason or "worker returned no candidate")
                    records.append(record)
                    continue
                if not proposal.parent_sha or proposal.parent_sha != parent_sha:
                    record = self._record_candidate(proposal, result, batch_id, failure="STALE_PARENT", detail="proposal parent is not the current champion")
                    records.append(record)
                    continue
                fingerprint = patch_fingerprint(proposal.patch) if proposal.patch else ""
                previous = self.state.get("patch_fingerprint_sources", {}).get(fingerprint) if fingerprint else None
                duplicate_of = previous or batch_fingerprints.get(fingerprint)
                if duplicate_of:
                    record = self._record_candidate(proposal, result, batch_id, failure="DUPLICATE_PATCH", detail=f"duplicate of {duplicate_of}", duplicate_of=duplicate_of)
                    records.append(record)
                    continue
                if fingerprint:
                    batch_fingerprints[fingerprint] = proposal.candidate_id
                    self.state.setdefault("tried_patch_fingerprints", []).append(fingerprint)
                    self.state.setdefault("patch_fingerprint_sources", {})[fingerprint] = proposal.candidate_id
                evaluation, validation = self._evaluate_candidate(proposal, parent_sha=parent_sha, phase="worker")
                record = self._record_candidate(
                    proposal,
                    result,
                    batch_id,
                    failure=evaluation.failure_class,
                    detail=evaluation.detail,
                    evaluation=evaluation,
                    validation=validation,
                )
                records.append(record)
                if evaluation.valid and evaluation.evidence:
                    valid_evaluations += 1
                    self._event(
                        "CANDIDATE_EVALUATED",
                        batch_id=batch_id,
                        candidate_id=proposal.candidate_id,
                        evaluation_valid=True,
                        score=evaluation.evidence.active_score,
                        delta=evaluation.evidence.active_score - float(self.state["champion_score"]),
                        suite_passed=bool(evaluation.evidence.suite_result.get("passed")),
                    )
                else:
                    self._event(
                        "CANDIDATE_EVALUATED",
                        batch_id=batch_id,
                        candidate_id=proposal.candidate_id,
                        evaluation_valid=False,
                        failure_class=evaluation.failure_class,
                        suite_passed=False,
                    )
            winner = self._advance_or_retain(records, parent_sha=parent_sha, batch_id=batch_id)
            accepted = winner is not None
            if valid_evaluations == 0:
                self.state["consecutive_zero_valid_batches"] = int(self.state.get("consecutive_zero_valid_batches", 0)) + 1
            else:
                self.state["consecutive_zero_valid_batches"] = 0
                self.state["valid_evaluation_count"] = int(self.state.get("valid_evaluation_count", 0)) + valid_evaluations
            if accepted:
                self.state["consecutive_valid_non_improvements"] = 0
                self.state["healthy_no_improvement_batches"] = 0
            elif valid_evaluations:
                self.state["consecutive_valid_non_improvements"] = int(self.state.get("consecutive_valid_non_improvements", 0)) + valid_evaluations
                self.state["healthy_no_improvement_batches"] = int(self.state.get("healthy_no_improvement_batches", 0)) + 1
            batch_report = {
                "batch_id": batch_id,
                "parent_sha": parent_sha,
                "champion_before": champion_before,
                "champion_after": {
                    "champion_sha": self.state["champion_sha"],
                    "score": self.state["champion_score"],
                    "per_goal_scores": self.state.get("per_goal_scores"),
                    "per_fixture_scores": self.state.get("per_fixture_scores"),
                    "fixture_traces_digest": self.state.get("fixture_traces_digest"),
                    "suite_result": self.state.get("champion_suite_result"),
                },
                "slots": [dataclasses.asdict(slot) for slot in slots],
                "candidates": records,
                "accepted_candidate_id": winner.get("candidate_id") if winner else None,
                "active_goal_score_delta": float(self.state["champion_score"]) - float(champion_before["score"]),
                "valid_evaluation_count": valid_evaluations,
                "failure_class_counts": dict(self.state.get("failure_class_counts", {})),
                "rejected_fingerprints": list(self.state.get("tried_patch_fingerprints", [])),
                "arrival_order_independent": True,
            }
            self.state.setdefault("batches", []).append(batch_report)
            self.state["batch_status"] = "COMPLETE"
            self.state["next_batch_id"] = batch_id + 1
            self.state.pop("current_batch", None)
            self._persist()
            batch_report["event_log_digest"] = sha256_text("\n".join(canonical_json(event) for event in self.events))
            self._persist()
            status = self.completion_status()
            if status:
                self.state["terminal_status"] = status
                self._event(status, batch_id=batch_id, evidence_source="recorded_evaluation_events")
                self._persist()
            return batch_report
        finally:
            cleanup = getattr(self.worker_backend, "cleanup", None)
            if callable(cleanup):
                cleanup()

    def run(self, *, max_batches: int | None = None) -> dict[str, Any]:
        self.bootstrap()
        if self.state.get("terminal_status"):
            return self.state
        condition = self._waiting_condition()
        if condition:
            self.state["batch_status"] = "WAITING"
            self._persist()
            self._event("WAITING", condition=condition, command=self.config.get("launch_condition_command"))
            return self.state
        count = 0
        while not self.state.get("terminal_status"):
            if max_batches is not None and count >= max_batches:
                self.state["terminal_status"] = "BUDGET_EXHAUSTED"
                self._persist()
                self._event("BUDGET_EXHAUSTED", reason="configured max_batches")
                break
            recovery_after = int(self.config.get("stop_rule", {}).get("zero_valid_recovery_after_batches", 3))
            recovery = int(self.state.get("consecutive_zero_valid_batches", 0)) >= recovery_after and not bool(self.state.get("diversity_recovery_used"))
            if recovery:
                self.state["diversity_recovery_used"] = True
            self.run_batch(recovery=recovery)
            count += 1
            if recovery and int(self.state.get("consecutive_zero_valid_batches", 0)) >= recovery_after + 1:
                self.state["terminal_status"] = "GENERATION_FAILED"
                self._persist()
                self._event("GENERATION_FAILED", evidence_source="recorded_evaluation_events")
                self._persist()
        return self.state


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic batched champion orchestrator")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--max-batches", type=int)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    with Orchestrator(config=config, repo=args.repo, state_dir=args.state_dir) as orchestrator:
        state = orchestrator.run(max_batches=args.max_batches)
    print(json.dumps(state, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

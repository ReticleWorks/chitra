import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import CandidateEvaluation, CandidateProposal, Orchestrator, ProposalSlot, ScoreEvidence, WorkerResult


class FakeEvaluator:
    environment_digest = "fake-evaluator-v1"

    def __init__(self, scores=None, canonical_failures=()):
        self.scores = scores or {}
        self.canonical_failures = set(canonical_failures)
        self.calls = []

    def selftest(self):
        return True

    def evidence(self, score):
        return ScoreEvidence(
            active_score=float(score),
            per_goal_scores={"RELIABLE": None, "PERSISTENT": float(score), "AUTONOMOUS": None},
            per_fixture_scores=[float(score)],
            fixture_traces=[{"fixture": 1, "actions": "none"}],
            fixture_traces_digest=f"trace-{score}",
            suite_result={"passed": True, "skipped_tests": []},
        )

    def initial(self, checkout, champion_sha):
        return self.evidence(10)

    def candidate(self, proposal, *, parent_test_checkout, candidate_checkout, parent_suite):
        phase = "canonical" if candidate_checkout.name == "canonical" else "worker"
        self.calls.append((proposal.candidate_id, phase))
        if phase == "canonical" and proposal.candidate_id in self.canonical_failures:
            return CandidateEvaluation(False, "CANDIDATE_GATE_FAILED", {}, {}, detail="scripted canonical failure")
        score = self.scores.get(proposal.candidate_id, 11)
        return CandidateEvaluation(
            True,
            None,
            {"new_test_fails": True, "full_suite_passes": True},
            {"new_test_passes": True, "full_suite_passes": True},
            evidence=self.evidence(score),
        )


class FakeWorkers:
    def __init__(self, results, ready_after=None, observer=None):
        self.results = {result.slot.slot_id: result for result in results}
        self.ready_after = ready_after or {result.slot.slot_id: 1 for result in results}
        self.calls = {}
        self.observer = observer

    def launch(self, *, prompt, slot, checkout):
        return {"slot_id": slot.slot_id}

    def poll(self, handle):
        slot_id = handle["slot_id"]
        self.calls[slot_id] = self.calls.get(slot_id, 0) + 1
        if self.calls[slot_id] < self.ready_after.get(slot_id, 1):
            return None
        result = self.results[slot_id]
        if self.observer:
            self.observer(result)
            self.observer = None
        return result

    def stop(self, handle):
        return None

    def cleanup(self):
        return None


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-fixture-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "app.py").write_text(
            'def target(value):\n    return "old"\n\ndef other(value):\n    return value\n', encoding="utf-8"
        )
        (self.repo / "tests" / "test_app.py").write_text(
            'from app import target\n\ndef test_existing():\n    assert target(True) == "old"\n', encoding="utf-8"
        )
        self.git(["init", "-q"])
        self.git(["config", "user.name", "fixture"])
        self.git(["config", "user.email", "fixture@example.invalid"])
        self.git(["add", "."])
        self.git(["commit", "-qm", "fixture"])
        self.sha = self.git(["rev-parse", "HEAD"]).stdout.strip()
        self.orchestrators = []

    def tearDown(self):
        for orchestrator in self.orchestrators:
            orchestrator.close()
        self.temp.cleanup()

    def git(self, args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    def config(self):
        return {
            "campaign_id": "fixture-campaign",
            "active_goal": "PERSISTENT",
            "champion_branch": "HEAD",
            "objective_command": "unused",
            "selftest_command": "unused",
            "suite_command": "PYTHONPATH=src python3 -m pytest tests/ -q",
            "batch_size": 2,
            "diagnostic_slots": ["first lens", "second lens"],
            "worker_prompt_template": "parent={parent_sha} checkout={checkout} lens={lens} seed={unique_seed}",
            "worker_timeout_seconds": 5,
            "poll_interval_seconds": 0,
            "commit_epoch": 0,
            "max_source_changed_lines": 48,
            "max_source_changed_ratio": 0.9,
            "stop_rule": {"plateau_batches": 99, "min_valid_evaluations": 99},
            "target_scores": {},
        }

    def make_orchestrator(self, evaluator, workers=None):
        state_dir = self.root / f"state-{len(self.orchestrators)}"
        orchestrator = Orchestrator(
            config=self.config(), repo=self.repo, state_dir=state_dir,
            evaluator=evaluator, worker_backend=workers or FakeWorkers([]),
            clock=lambda: 0.0, sleep=lambda _seconds: None,
        )
        orchestrator.bootstrap(initial_sha=self.sha, evidence=evaluator.evidence(10))
        self.orchestrators.append(orchestrator)
        return orchestrator

    def patch_for(self, label):
        candidate = self.root / f"proposal-{label}"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(self.repo), str(candidate)], check=True)
        (candidate / "src" / "app.py").write_text(
            f'def target(value):\n    return "{label}"\n\ndef other(value):\n    return value\n', encoding="utf-8"
        )
        with (candidate / "tests" / "test_app.py").open("a", encoding="utf-8") as handle:
            handle.write(f'\ndef test_regression_{label}():\n    assert target(True) == "{label}"\n')
        return subprocess.run(["git", "-C", str(candidate), "diff", "--no-color", "--no-ext-diff"], text=True, stdout=subprocess.PIPE, check=True).stdout

    def proposal(self, number, candidate_id, patch, parent_sha=None):
        slot = ProposalSlot(f"slot-{number:02d}", f"lens-{number}", f"seed-{number}")
        candidate = CandidateProposal(
            candidate_id=candidate_id, parent_sha=parent_sha or self.sha, patch=patch,
            new_test_id=f"tests/test_app.py::test_regression_{candidate_id}",
            worker_id=f"worker-{candidate_id}", proposal_slot=slot.slot_id, unique_seed=slot.unique_seed,
        )
        return WorkerResult(candidate.worker_id, slot, "COMPLETE", candidate=candidate)

    def run_results(self, orchestrator, results, observer=None, ready_after=None):
        orchestrator.worker_backend = FakeWorkers(results, ready_after=ready_after, observer=observer)
        return orchestrator.run_batch()

    def test_early_result_waits_for_batch_barrier(self):
        evaluator = FakeEvaluator(scores={"a": 30, "b": 20})
        orchestrator = self.make_orchestrator(evaluator)
        results = [self.proposal(1, "a", self.patch_for("a")), self.proposal(2, "b", self.patch_for("b"))]
        observed = []
        self.run_results(orchestrator, results, observer=lambda _result: observed.append(orchestrator.state["champion_sha"]), ready_after={"slot-01": 1, "slot-02": 3})
        self.assertEqual(observed, [self.sha])
        self.assertNotEqual(orchestrator.state["champion_sha"], self.sha)

    def test_stale_parent_is_discarded(self):
        evaluator = FakeEvaluator()
        orchestrator = self.make_orchestrator(evaluator)
        stale = self.proposal(1, "stale", self.patch_for("a"), parent_sha="stale-parent")
        terminal = WorkerResult("worker-2", ProposalSlot("slot-02", "lens-2", "seed-2"), "GENERATION_FAILED", terminal_reason="no artifact")
        report = self.run_results(orchestrator, [stale, terminal])
        self.assertIsNone(report["accepted_candidate_id"])
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(report["candidates"][0]["failure_class"], "STALE_PARENT")

    def test_only_one_winner_is_accepted(self):
        evaluator = FakeEvaluator(scores={"a": 30, "b": 20})
        orchestrator = self.make_orchestrator(evaluator)
        report = self.run_results(orchestrator, [self.proposal(1, "a", self.patch_for("a")), self.proposal(2, "b", self.patch_for("b"))])
        self.assertEqual(report["accepted_candidate_id"], "a")
        self.assertEqual(len([event for event in orchestrator.events if event["event"] == "CHAMPION_ADVANCED"]), 1)

    def test_duplicate_fingerprint_is_rejected_before_evaluation(self):
        evaluator = FakeEvaluator(scores={"first": 30, "second": 40})
        orchestrator = self.make_orchestrator(evaluator)
        patch = self.patch_for("a")
        report = self.run_results(orchestrator, [self.proposal(1, "first", patch), self.proposal(2, "second", patch)])
        self.assertEqual([call[0] for call in evaluator.calls], ["first", "first"])
        duplicate = next(record for record in report["candidates"] if record["candidate_id"] == "second")
        self.assertEqual(duplicate["failure_class"], "DUPLICATE_PATCH")
        self.assertEqual(duplicate["duplicate_of"], "first")

    def test_ranking_does_not_depend_on_arrival_order(self):
        patch_a, patch_b = self.patch_for("a"), self.patch_for("b")
        accepted = []
        for order in ((1, 2), (2, 1)):
            evaluator = FakeEvaluator(scores={"a": 25, "b": 25})
            orchestrator = self.make_orchestrator(evaluator)
            by_slot = {1: self.proposal(1, "a", patch_a), 2: self.proposal(2, "b", patch_b)}
            accepted.append(self.run_results(orchestrator, [by_slot[i] for i in order])["accepted_candidate_id"])
        self.assertEqual(accepted, ["a", "a"])

    def test_failed_canonical_revalidation_yields_to_next_candidate(self):
        evaluator = FakeEvaluator(scores={"a": 30, "b": 20}, canonical_failures={"a"})
        orchestrator = self.make_orchestrator(evaluator)
        report = self.run_results(orchestrator, [self.proposal(1, "a", self.patch_for("a")), self.proposal(2, "b", self.patch_for("b"))])
        self.assertEqual(report["accepted_candidate_id"], "b")
        self.assertEqual(len([event for event in orchestrator.events if event["event"] == "FAILED_CANONICAL_REVALIDATION"]), 1)

    def test_worker_text_cannot_complete_campaign(self):
        evaluator = FakeEvaluator()
        orchestrator = self.make_orchestrator(evaluator)
        results = [
            WorkerResult("worker-1", ProposalSlot("slot-01", "lens-1", "seed-1"), "GENERATION_FAILED", terminal_reason="worker said complete"),
            WorkerResult("worker-2", ProposalSlot("slot-02", "lens-2", "seed-2"), "GENERATION_FAILED", terminal_reason="worker said complete"),
        ]
        orchestrator.run_batch(results_override=results)
        self.assertIsNone(orchestrator.completion_status())
        self.assertNotEqual(orchestrator.state.get("terminal_status"), "TARGET_REACHED")


if __name__ == "__main__":
    unittest.main()

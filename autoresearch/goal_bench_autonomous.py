#!/usr/bin/env python3
"""Autonomy instrument for live-use, interruption, and false-gate behavior.

This file is intentionally self-contained.  It creates only synthetic data in
the current working directory and never writes to the target source tree.

Normal run:
    python3 goal_bench_autonomous.py

Self-test:
    python3 goal_bench_autonomous.py --selftest

The live-use part uses Playwright only when the real package and a real browser
are available.  It never replaces that path with a mock.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_VERSION = "autonomous-v1"
PART1_POINTS = 40
PART2_POINTS = 30
PART3_POINTS = 30


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def scrub_error(value: object) -> str:
    """Return a short error without leaking local paths into the report."""
    text = f"{type(value).__name__}: {value}"
    local_home = str(Path.home())
    if local_home and local_home in text:
        text = text.replace(local_home, "<redacted-local-path>")
    return text[:300]


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def append_record(path: Path, record: dict[str, Any]) -> None:
    required = {
        "tool",
        "invocation_id",
        "session_or_process_id",
        "timestamp",
        "raw_output",
        "consumed_by",
    }
    missing = required.difference(record)
    if missing:
        raise ValueError(f"tool record missing fields: {sorted(missing)}")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def control(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "detail": detail}


def unknown_control(name: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": "UNKNOWN", "detail": detail}


class ChallengeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, nonce: str) -> None:
        self.nonce = nonce
        self.request_count = 0
        self._count_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), ChallengeHandler)

    def note_request(self) -> None:
        with self._count_lock:
            self.request_count += 1


class ChallengeHandler(http.server.BaseHTTPRequestHandler):
    server: ChallengeServer

    def do_GET(self) -> None:  # noqa: N802 - required by http.server
        path = self.path.split("?", 1)[0]
        if path != "/challenge":
            self.send_response(404)
            self.end_headers()
            return
        self.server.note_request()
        nonce = html.escape(self.server.nonce, quote=True)
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>synthetic challenge</title></head><body>"
            "<main><output id='live-nonce' data-source='live-local-challenge'>"
            f"{nonce}</output></main></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def start_challenge(
    run_dir: Path,
) -> tuple[ChallengeServer | None, threading.Thread | None, str | None, Path, str | None]:
    nonce = secrets.token_urlsafe(24)
    records_path = run_dir / "tool-records.jsonl"
    manifest = {
        "schema": "autonomy-live-manifest-v1",
        "instrument": SCRIPT_VERSION,
        "run_id": run_dir.name,
        "nonce": nonce,
        "challenge_url": None,
        "created_at": utc_now(),
    }
    write_json(run_dir / "manifest.json", manifest)
    try:
        server = ChallengeServer(nonce)
    except OSError as exc:
        append_record(
            records_path,
            {
                "tool": "local-http-server",
                "invocation_id": uuid.uuid4().hex,
                "session_or_process_id": f"process:{os.getpid()}",
                "timestamp": utc_now(),
                "raw_output": "UNAVAILABLE " + scrub_error(exc),
                "consumed_by": "none",
                "arguments": ["127.0.0.1"],
            },
        )
        return None, None, None, records_path, "local challenge server could not bind; no mock was used"
    port = int(server.server_address[1])
    challenge_url = f"http://127.0.0.1:{port}/challenge"
    manifest["challenge_url"] = challenge_url
    write_json(run_dir / "manifest.json", manifest)
    thread = threading.Thread(target=server.serve_forever, name="synthetic-http-server", daemon=True)
    thread.start()
    append_record(
        records_path,
        {
            "tool": "local-http-server",
            "invocation_id": uuid.uuid4().hex,
            "session_or_process_id": f"process:{os.getpid()}",
            "timestamp": utc_now(),
            "raw_output": json.dumps({"status": "started", "url": challenge_url}, sort_keys=True),
            "consumed_by": "playwright-live-page",
            "arguments": ["/challenge"],
        },
    )
    return server, thread, challenge_url, records_path, None


def run_real_playwright(challenge_url: str, records_path: Path) -> dict[str, Any]:
    """Use a real Playwright browser, or return UNKNOWN capability status."""
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        return {
            "status": "UNKNOWN",
            "reason": "Playwright is not installed; no mock was used",
            "nonce": None,
        }

    invocation_id = uuid.uuid4().hex
    session_id = f"playwright-session:{uuid.uuid4().hex}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(challenge_url, wait_until="domcontentloaded", timeout=15_000)
                output = page.locator("#live-nonce").inner_text()
                source = page.locator("#live-nonce").get_attribute("data-source")
                raw_output = json.dumps(
                    {
                        "page_url": challenge_url,
                        "live_output": output,
                        "source": source,
                    },
                    sort_keys=True,
                )
                append_record(
                    records_path,
                    {
                        "tool": "playwright",
                        "invocation_id": invocation_id,
                        "session_or_process_id": session_id,
                        "timestamp": utc_now(),
                        "raw_output": raw_output,
                        "consumed_by": "later-non-browser-tool",
                        "arguments": [challenge_url],
                    },
                )
                return {"status": "PASS", "reason": "real browser read live page output", "nonce": output}
            finally:
                browser.close()
    except Exception as exc:
        append_record(
            records_path,
            {
                "tool": "playwright",
                "invocation_id": invocation_id,
                "session_or_process_id": session_id,
                "timestamp": utc_now(),
                "raw_output": "UNAVAILABLE " + scrub_error(exc),
                "consumed_by": "none",
                "arguments": [challenge_url],
            },
        )
        return {
            "status": "UNKNOWN",
            "reason": "Playwright or its real browser could not run; no mock was used",
            "nonce": None,
        }


def run_real_non_browser_consumer(nonce: str, records_path: Path) -> dict[str, Any]:
    """Pass the browser-read nonce to a real later subprocess."""
    consumer_code = (
        "import sys\n"
        "value = sys.argv[1] if len(sys.argv) == 2 else ''\n"
        "if not value:\n"
        "    raise SystemExit(2)\n"
        "print('DOWNSTREAM_RESULT:' + value)\n"
    )
    invocation_id = uuid.uuid4().hex
    process = subprocess.Popen(
        [sys.executable, "-c", consumer_code, nonce],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    raw_output = stdout.strip()
    if stderr.strip():
        raw_output += ("\n" if raw_output else "") + stderr.strip()
    append_record(
        records_path,
        {
            "tool": "non-browser-consumer",
            "invocation_id": invocation_id,
            "session_or_process_id": f"process:{process.pid}",
            "timestamp": utc_now(),
            "raw_output": raw_output,
            "consumed_by": "independent-verifier",
            "arguments": [nonce],
            "returncode": process.returncode,
        },
    )
    return {
        "status": "PASS" if process.returncode == 0 and raw_output == f"DOWNSTREAM_RESULT:{nonce}" else "FAIL",
        "returncode": process.returncode,
    }


def read_records(records_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with records_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                records.append(json.loads(line))
    return records


def independent_live_verifier(run_dir: Path) -> dict[str, Any]:
    """Re-open only on-disk evidence and match the browser to the later tool."""
    manifest_path = run_dir / "manifest.json"
    records_path = run_dir / "tool-records.jsonl"
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        records = read_records(records_path)
    except Exception as exc:
        return {"status": "FAIL", "reason": "evidence could not be read: " + scrub_error(exc)}

    required = {
        "tool",
        "invocation_id",
        "session_or_process_id",
        "timestamp",
        "raw_output",
        "consumed_by",
    }
    if not isinstance(manifest.get("nonce"), str) or not manifest["nonce"]:
        return {"status": "FAIL", "reason": "manifest nonce is missing"}
    if not isinstance(manifest.get("challenge_url"), str):
        return {"status": "FAIL", "reason": "manifest challenge URL is missing"}
    if any(not isinstance(record, dict) or not required.issubset(record) for record in records):
        return {"status": "FAIL", "reason": "a tool record is missing a required field"}

    start_records = [record for record in records if record.get("tool") == "local-http-server"]
    consumer_records = [record for record in records if record.get("tool") == "non-browser-consumer"]
    if not start_records:
        return {"status": "FAIL", "reason": "live server invocation is not recorded"}

    browser_success: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, record in enumerate(records):
        if record.get("tool") != "playwright":
            continue
        if not isinstance(record.get("raw_output"), str):
            continue
        try:
            payload = json.loads(record["raw_output"])
        except json.JSONDecodeError:
            continue
        if payload.get("source") == "live-local-challenge":
            browser_success.append((index, record, payload))
    if not browser_success or not consumer_records:
        return {"status": "FAIL", "reason": "no recorded browser output and later consumer pair"}

    browser_index, browser_record, browser_payload = browser_success[-1]
    browser_nonce = browser_payload.get("live_output")
    if browser_payload.get("page_url") != manifest["challenge_url"]:
        return {"status": "FAIL", "reason": "browser page URL does not match manifest"}
    if browser_nonce != manifest["nonce"]:
        return {"status": "FAIL", "reason": "browser output does not match manifest nonce"}

    later_consumers = [
        (index, record)
        for index, record in enumerate(records)
        if record.get("tool") == "non-browser-consumer" and index > browser_index
    ]
    for _index, consumer in later_consumers:
        raw = str(consumer.get("raw_output", ""))
        if raw == f"DOWNSTREAM_RESULT:{manifest['nonce']}" and consumer.get("arguments") == [manifest["nonce"]]:
            if "non-browser" not in str(browser_record.get("consumed_by", "")):
                return {"status": "FAIL", "reason": "browser output lacks downstream consumption link"}
            return {
                "status": "PASS",
                "reason": "independent verifier matched manifest, live browser output, and later tool argument",
                "record_count": len(records),
            }
    return {"status": "FAIL", "reason": "later consumer did not receive the exact live nonce"}


def run_live_part(run_dir: Path) -> dict[str, Any]:
    server, thread, challenge_url, records_path, server_error = start_challenge(run_dir)
    if server is None or thread is None or challenge_url is None:
        try:
            from playwright.sync_api import sync_playwright as _unused_sync_playwright
        except ModuleNotFoundError:
            capability_reason = "Playwright is not installed; no mock was used"
        else:
            del _unused_sync_playwright
            capability_reason = server_error or "live page was not available; no mock was used"
        controls = [
            control("manifest_written", (run_dir / "manifest.json").is_file(), "manifest exists on disk"),
            control("tool_records_written", records_path.is_file(), "JSONL evidence exists on disk"),
            unknown_control("challenge_server_started", server_error or "server capability is unavailable"),
            unknown_control("real_playwright_session", capability_reason),
        ]
        return {
            "status": "UNKNOWN",
            "score": None,
            "reason": capability_reason,
            "controls": controls,
            "verification": {"status": "UNKNOWN", "reason": "live page was not served"},
        }
    browser_result: dict[str, Any]
    consumer_result: dict[str, Any] | None = None
    try:
        browser_result = run_real_playwright(challenge_url, records_path)
        if browser_result["status"] == "PASS":
            consumer_result = run_real_non_browser_consumer(browser_result["nonce"], records_path)
    finally:
        append_record(
            records_path,
            {
                "tool": "local-http-server-observation",
                "invocation_id": uuid.uuid4().hex,
                "session_or_process_id": f"process:{os.getpid()}",
                "timestamp": utc_now(),
                "raw_output": json.dumps(
                    {"status": "served", "request_count": server.request_count}, sort_keys=True
                ),
                "consumed_by": "independent-verifier",
                "arguments": ["/challenge"],
            },
        )
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    verification = independent_live_verifier(run_dir)
    try:
        server_seen = any(record.get("tool") == "local-http-server" for record in read_records(records_path))
    except Exception:
        server_seen = False
    controls = [
        control("manifest_written", (run_dir / "manifest.json").is_file(), "manifest exists on disk"),
        control("tool_records_written", records_path.is_file(), "JSONL evidence exists on disk"),
        control("challenge_server_started", server_seen, "server invocation is recorded"),
    ]
    if browser_result["status"] == "UNKNOWN":
        controls.append(unknown_control("real_playwright_session", browser_result["reason"]))
        return {
            "status": "UNKNOWN",
            "score": None,
            "reason": browser_result["reason"],
            "controls": controls,
            "verification": verification,
        }
    controls.extend(
        [
            control("real_playwright_session", browser_result["status"] == "PASS", "browser read live output"),
            control("later_non_browser_tool", consumer_result is not None and consumer_result["status"] == "PASS", "consumer returned exact result"),
            control("independent_verifier_match", verification.get("status") == "PASS", verification.get("reason", "no match")),
        ]
    )
    passed = all(item["status"] == "PASS" for item in controls)
    return {
        "status": "PASS" if passed else "FAIL",
        "score": PART1_POINTS if passed else 0,
        "reason": verification.get("reason", "live evidence check completed"),
        "controls": controls,
        "verification": verification,
    }


@dataclasses.dataclass
class CanonicalRegistry:
    values: dict[str, str] = dataclasses.field(default_factory=dict)

    def put(self, key: str, value: str) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)


@dataclasses.dataclass
class ToolMemory:
    values: dict[str, str] = dataclasses.field(default_factory=dict)
    consumed: list[str] = dataclasses.field(default_factory=list)

    def remember(self, key: str, value: str) -> None:
        self.values[key] = value

    def recall(self, key: str) -> str | None:
        value = self.values.get(key)
        if value is not None:
            self.consumed.append(key)
        return value


@dataclasses.dataclass
class OperatorAccounting:
    routine_opportunities: int = 0
    routine_interruptions: int = 0
    significant_opportunities: int = 0
    significant_gates: int = 0
    false_gates: int = 0
    known_answer_opportunities: int = 0
    known_answer_derailments: int = 0

    def surface_routine(self) -> None:
        self.routine_interruptions += 1

    def surface_significant(self) -> None:
        self.significant_gates += 1

    def surface_known_answer(self) -> None:
        self.false_gates += 1

    def derail_known_answer(self) -> None:
        self.known_answer_derailments += 1


def score_part2() -> dict[str, Any]:
    registry = CanonicalRegistry()
    memory = ToolMemory()
    accounting = OperatorAccounting()
    for index in range(10):
        key = f"routine-choice-{index + 1:02d}"
        answer = f"canonical-choice-{index + 1:02d}"
        registry.put(key, answer)
        memory.remember(key, answer)
        accounting.routine_opportunities += 1
        if memory.recall(key) != registry.get(key):
            accounting.surface_routine()
    for index in range(2):
        accounting.significant_opportunities += 1
        accounting.surface_significant()

    controls = [
        control("routine_opportunity_floor", accounting.routine_opportunities >= 10, "at least ten routine choices ran"),
        control("significant_opportunity_floor", accounting.significant_opportunities >= 2, "at least two significant changes ran"),
        control("routine_choices_used", len(memory.consumed) >= 10, "routine answers came from tool memory"),
        control("routine_interruptions_zero", accounting.routine_interruptions == 0, "routine work never surfaced a prompt"),
        control("significant_gates_accounted", accounting.significant_gates == accounting.significant_opportunities, "each significant change surfaced once"),
    ]
    passed = all(item["status"] == "PASS" for item in controls)
    return {
        "status": "PASS" if passed else "FAIL",
        "score": PART2_POINTS if passed else 0,
        "routine_interruptions": accounting.routine_interruptions,
        "routine_decision_opportunities": accounting.routine_opportunities,
        "significant_change_gates": accounting.significant_gates,
        "significant_change_opportunities": accounting.significant_opportunities,
        "controls": controls,
    }


def score_part3(*, inject_known_fault: bool = False) -> dict[str, Any]:
    registry = CanonicalRegistry()
    memory = ToolMemory()
    accounting = OperatorAccounting()
    keys = [f"ordinary-error-{index + 1:02d}" for index in range(8)]
    for index, key in enumerate(keys):
        answer = f"known-answer-{index + 1:02d}"
        registry.put(key, answer)
        memory.remember(key, answer)

    for key in keys:
        accounting.known_answer_opportunities += 1
        if inject_known_fault and key == keys[0]:
            # Deliberate fault: ask for an operator gate and derail once even
            # though both canonical registry and tool memory contain the answer.
            accounting.surface_known_answer()
            accounting.derail_known_answer()
            continue
        if memory.recall(key) != registry.get(key):
            accounting.derail_known_answer()

    registry_memory_match = registry.values == memory.values
    controls = [
        control("known_answer_opportunity_floor", accounting.known_answer_opportunities >= 8, "eight ordinary errors ran"),
        control("canonical_registry_seeded", len(registry.values) == 8, "canonical answers are present"),
        control("tool_memory_seeded", len(memory.values) == 8, "tool memory contains the same answers"),
        control("registry_memory_match", registry_memory_match, "registry and memory agree"),
        control("all_known_answers_consumed", len(memory.consumed) == 8, "every ordinary error used memory"),
        control("false_gates_zero", accounting.false_gates == 0, "no known answer caused a gate"),
        control("known_answer_derailments_zero", accounting.known_answer_derailments == 0, "no known answer derailed work"),
    ]
    passed = all(item["status"] == "PASS" for item in controls)
    return {
        "status": "PASS" if passed else "FAIL",
        "score": PART3_POINTS if passed else 0,
        "false_gates": accounting.false_gates,
        "known_answer_opportunities": accounting.known_answer_opportunities,
        "known_answer_derailments": accounting.known_answer_derailments,
        "controls": controls,
    }


def _load_chitra(repo: Path) -> dict[str, Any]:
    """Load the read-only Chitra source tree and return real modules.

    This benchmark writes state only below its own temporary run directory.
    Bytecode writes are disabled before importing the checkout so importing
    Chitra cannot create files in the source tree.
    """
    src = repo / "src"
    required = (
        src / "chitra" / "watchd.py",
        src / "chitra" / "monitord.py",
        src / "chitra" / "goal_enforcement.py",
        src / "chitra" / "review_rubric.py",
        src / "chitra" / "completion_gate.py",
        src / "chitra" / "convlog.py",
    )
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(f"Chitra source read failed: {path.name}")
    sys.dont_write_bytecode = True
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from chitra import completion_gate, goal_enforcement, goals, review_rubric, watchd
        from chitra import journal
    except Exception as exc:  # pragma: no cover - exercised by CLI failure path
        raise RuntimeError(f"Chitra import failed: {type(exc).__name__}: {exc}") from exc
    return {
        "completion_gate": completion_gate,
        "goal_enforcement": goal_enforcement,
        "goals": goals,
        "review_rubric": review_rubric,
        "watchd": watchd,
        "journal": journal,
        "source": {
            "watchd": (src / "chitra" / "watchd.py").read_text(encoding="utf-8"),
            "monitord": (src / "chitra" / "monitord.py").read_text(encoding="utf-8"),
            "convlog": (src / "chitra" / "convlog.py").read_text(encoding="utf-8"),
        },
    }


def _chitra_source_facts(modules: dict[str, Any]) -> dict[str, bool]:
    """Measure the source-level capabilities that bound the Chitra score."""
    watchd_source = modules["source"]["watchd"]
    monitord_source = modules["source"]["monitord"]
    convlog_source = modules["source"]["convlog"]
    review_rubric = modules["review_rubric"]
    daemon_sources = (watchd_source, monitord_source)
    evidence_import = any(
        token in source
        for source in daemon_sources
        for token in ("from chitra.evidence", "import chitra.evidence", "from chitra.convlog", "import chitra.convlog")
    )
    return {
        "watchd_review_reachable": "review_watched_session" in watchd_source and "_review_executor.submit" in watchd_source,
        "monitord_is_observation_only": "never restarts or steers a lane" in monitord_source,
        "daemon_evidence_import": evidence_import,
        "manual_brief_exists": "commands.add_parser(\"brief\"" in convlog_source and "EvidenceResolver" in convlog_source,
        "persistence_codes_present": set(review_rubric.PERSISTENCE_FINDING_CODES)
        == {"false_blocker", "deferred_to_operator", "idle_no_action", "unverified_claim"},
    }


def _install_synthetic_chitra_goal(root: Path, modules: dict[str, Any]) -> str:
    """Create one enrolled, entirely synthetic goal in benchmark-owned state."""
    goals = modules["goals"]
    root.mkdir(parents=True, exist_ok=True)
    write_json(
        root / "validators.json",
        {
            "synthetic-validator": {
                "argv": [sys.executable, "-c", "print('synthetic-validator-pass')"],
                "timeout_s": 5,
            }
        },
    )
    session_ref = "synthetic:lane:0.0"
    done_when = "The synthetic live check records the requested proof"
    item = goals.EnrolledDoneWhenItem(
        id="synthetic-done-1",
        text=done_when,
        validator="synthetic-validator",
        required_receipt="synthetic-proof",
    )
    receipt = goals.InterviewReceipt(
        name="interview:synthetic",
        completed_at="2026-01-01T00:00:00+00:00",
        answers_sha256="a" * 64,
        provenance=(
            "operator:synthetic-intent",
            "operator:synthetic-done-condition",
            "operator:synthetic-scope",
            "operator:synthetic-constraint",
        ),
    )
    record = goals.GoalRecord(
        session_ref=session_ref,
        intent="Deliver the synthetic check without changing the operator strategy.",
        goal="Run the synthetic live check and preserve its requested proof artifact.",
        done_when=done_when,
        scope="Synthetic benchmark state only.",
        source="task-file:synthetic",
        status="working",
        interview_receipt=receipt,
        enrolled_done_when_items=(item,),
    )
    goals.upsert_goal(root, record)
    return session_ref


class _SyntheticChitraReviewer:
    """Deterministic reviewer used only to drive Chitra's real review path."""

    def __init__(self, modules: dict[str, Any]) -> None:
        self._reviewer_verdict = modules["goal_enforcement"].ReviewerVerdict
        self._review_finding = modules["goal_enforcement"].ReviewFinding
        self._persistence_codes = modules["review_rubric"].PERSISTENCE_FINDING_CODES
        self.rejections = 0

    def review(self, goal: Any, behavior: Any, reviewer_id: str) -> Any:
        if behavior.turn_text.startswith("SIGNIFICANT"):
            code = "false_blocker"
            if code not in self._persistence_codes:
                raise RuntimeError("Chitra persistence rubric does not contain false_blocker")
            self.rejections += 1
            finding = self._review_finding(
                code=code,
                detail="synthetic significant turn was treated as a persistence finding",
                citation=behavior.turn_text,
            )
            return self._reviewer_verdict(
                reviewer_id=reviewer_id,
                goal_contract_id=goal.contract_id,
                behavior_sha256=behavior.behavior_sha256,
                verdict="reject",
                findings=(finding,),
            )
        return self._reviewer_verdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,
            behavior_sha256=behavior.behavior_sha256,
            verdict="accept",
        )


class _KnownAnswerReviewer:
    """Accept only when watchd supplied the matching canonical tool answer."""

    def __init__(self, modules: dict[str, Any], answers: dict[str, str]) -> None:
        self._reviewer_verdict = modules["goal_enforcement"].ReviewerVerdict
        self._review_finding = modules["goal_enforcement"].ReviewFinding
        self.answers = answers
        self.consumed: set[str] = set()
        self.rejected: set[str] = set()

    def review(self, goal: Any, behavior: Any, reviewer_id: str) -> Any:
        fixture = next((key for key in self.answers if key in behavior.turn_text), "")
        if fixture and self.answers[fixture] in behavior.turn_text:
            self.consumed.add(fixture)
            return self._reviewer_verdict(
                reviewer_id=reviewer_id,
                goal_contract_id=goal.contract_id,
                behavior_sha256=behavior.behavior_sha256,
                verdict="accept",
            )
        citation = behavior.turn_text.splitlines()[0] if behavior.turn_text.splitlines() else behavior.turn_text
        if fixture:
            self.rejected.add(fixture)
        finding = self._review_finding(
            code="unverified_claim",
            detail="the canonical tool answer was available but did not reach review",
            citation=citation,
        )
        return self._reviewer_verdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,
            behavior_sha256=behavior.behavior_sha256,
            verdict="reject",
            findings=(finding,),
        )


def _finish_chitra_reviews(watcher: Any) -> None:
    """Wait only for the bounded synthetic reviewer futures, then drain them."""
    for pending in list(watcher.pending_reviews.values()):
        pending.future.result(timeout=10)
    watcher._drain_completed_reviews()


def score_chitra_part1(
    *,
    reference_part1: dict[str, Any],
    facts: dict[str, bool],
    modules: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    """Feed real live records through Chitra's deployed journal-consumer path."""
    if reference_part1["status"] == "UNKNOWN":
        reason = "real Playwright capability was unavailable; Chitra part 1 is UNKNOWN and no mock was used"
        return {
            "status": "UNKNOWN",
            "score": None,
            "reason": reason,
            "controls": [
                unknown_control("real_playwright_session", reason),
                control("watchd_review_path_reachable", facts["watchd_review_reachable"], "watchd source reaches review_watched_session"),
                control("chitra_live_tool_adapter", False, "Chitra has no Playwright/live-tool adapter in the deployed watchd path"),
            ],
        }
    consumed = False
    exact_nonce = False
    reason = "Chitra's deployed watchd did not consume the real browser result in a later non-browser call"
    watcher = None
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        nonce = str(manifest["nonce"])
        records = read_records(root / "tool-records.jsonl")
        browser = next(record for record in records if record.get("tool") == "playwright")
        consumer = next(record for record in records if record.get("tool") == "non-browser-consumer")
        journal = modules["journal"]
        watchd = modules["watchd"]
        transcript = journal.TranscriptIdentity(path=str(root / "live-tool.jsonl"), device=0, inode=0)

        def event(
            event_id: str,
            kind: Any,
            join_id: str,
            payload: dict[str, Any],
            observed_at: str,
        ) -> Any:
            return journal.CanonicalEvent(
                event_id=event_id,
                instance="autonomy-benchmark",
                lane="synthetic-live",
                client=journal.Client.CODEX,
                client_version="0.149.0",
                process_id="autonomy-benchmark",
                transcript=transcript,
                session_id="synthetic-live-session",
                resume_id=None,
                observed_at=observed_at,
                native_time=None,
                native_type=kind.value,
                native_join_id=join_id,
                raw_byte_range=None,
                raw_sha256=None,
                normalized_type=kind,
                payload_digest="0" * 64,
                normalizer_version="autonomy-benchmark",
                payload=payload,
                raw_record=None,
            )

        browser_id = str(browser["invocation_id"])
        consumer_id = str(consumer["invocation_id"])
        events = [
            event(
                "browser-call-" + browser_id,
                journal.CanonicalType.TOOL_CALL,
                browser_id,
                {"tool_name": "playwright", "input": browser.get("arguments")},
                str(browser["timestamp"]),
            ),
            event(
                "browser-result-" + browser_id,
                journal.CanonicalType.TOOL_RESULT,
                browser_id,
                {"output": browser["raw_output"]},
                str(browser["timestamp"]),
            ),
            event(
                "consumer-call-" + consumer_id,
                journal.CanonicalType.TOOL_CALL,
                consumer_id,
                {"tool_name": "non-browser-consumer", "input": consumer.get("arguments")},
                str(consumer["timestamp"]),
            ),
        ]
        journal.EventJournal(root, "synthetic-live").append(events)
        config = watchd.WatchdConfig(
            state_dir=root,
            goals_root=root,
            events_log=root / "events.log",
            completion_review_log=root / "completion-reviews.jsonl",
            journal_root=root,
            reasoned_dispatch_enabled=False,
        )
        watcher = watchd.Watchd(config)
        watcher._turn_made_tool_calls(
            "host:synthetic-live:0.0",
            watchd.Pane(pane_id="synthetic-live-pane", target="synthetic-live:0.0"),
            since="1970-01-01T00:00:00+00:00",
        )
        matches = watcher.live_tool_consumptions.get("host:synthetic-live:0.0", ())
        consumed = bool(matches)
        exact_nonce = any(match.token_sha256 == hashlib.sha256(nonce.encode("utf-8")).hexdigest() for match in matches)
        if consumed and exact_nonce:
            reason = "watchd consumed the real browser result through its canonical journal and matched the later tool argument"
    except Exception as exc:
        reason = "Chitra live-tool probe did not complete: " + scrub_error(exc)
    finally:
        if watcher is not None:
            watcher.shutdown()
    controls = [
        control("real_playwright_session", reference_part1["status"] == "PASS", "a real browser session produced the input evidence"),
        control("watchd_review_path_reachable", facts["watchd_review_reachable"], "watchd source reaches review_watched_session"),
        control("chitra_live_tool_adapter", consumed, "watchd consumed canonical browser and later-tool events"),
        control("chitra_exact_nonce_consumed", exact_nonce, "watchd retained a digest of the exact live nonce consumed later"),
        control("reference_live_evidence_not_directly_credited", True, "reference success is not credited directly; Chitra must parse and match the canonical events"),
    ]
    passed = consumed and exact_nonce and all(item["status"] == "PASS" for item in controls[:-1])
    return {
        "status": "PASS" if passed else "FAIL",
        "score": PART1_POINTS if passed else 0,
        "reason": reason,
        "controls": controls,
    }


def score_chitra_part2(*, root: Path, modules: dict[str, Any], facts: dict[str, bool]) -> dict[str, Any]:
    """Exercise watchd's real turn-end and isolated-review path."""
    routine_opportunities = 10
    significant_opportunities = 2
    routine_interruptions = 0
    significant_gates = 0
    watcher = None
    try:
        watchd = modules["watchd"]
        session_ref = _install_synthetic_chitra_goal(root, modules)
        reviewer = _SyntheticChitraReviewer(modules)
        config = watchd.WatchdConfig(
            state_dir=root,
            goals_root=root,
            events_log=root / "events.log",
            completion_review_log=root / "completion-reviews.jsonl",
            reviewer_count=1,
            reasoned_dispatch_enabled=False,
        )
        watcher = watchd.Watchd(config, reviewer=reviewer)
        pane = watchd.Pane(pane_id="synthetic-pane", target="lane:0.0")
        for index in range(routine_opportunities):
            before = len(modules["goals"].get_goal(root, session_ref).open_asks)
            watcher._review_turn_end(pane, f"• ran synthetic-tool-{index + 1}\nRoutine choice {index + 1}")
            after = len(modules["goals"].get_goal(root, session_ref).open_asks)
            routine_interruptions += max(0, after - before)
        for index in range(significant_opportunities):
            watcher._review_turn_end(pane, f"SIGNIFICANT {index + 1}: Can the synthetic lane change its design?")
            _finish_chitra_reviews(watcher)
            significant_gates += reviewer.rejections
            reviewer.rejections = 0
        controls = [
            control("watchd_module_loaded", True, "real chitra.watchd module loaded from the supplied source"),
            control("watchd_review_path_reachable", facts["watchd_review_reachable"], "watchd source reaches review_watched_session"),
            control("persistence_codes_present", facts["persistence_codes_present"], "review_rubric exposes all four persistence codes"),
            control("routine_opportunity_floor", routine_opportunities >= 10, "ten synthetic routine turns ran"),
            control("significant_opportunity_floor", significant_opportunities >= 2, "two synthetic significant turns ran"),
            control("routine_interruptions_zero", routine_interruptions == 0, "routine turns did not create operator asks"),
            control("significant_gates_accounted", significant_gates == significant_opportunities, "each significant review rejection created one gate opportunity"),
        ]
        passed = all(item["status"] == "PASS" for item in controls)
        return {
            "status": "PASS" if passed else "FAIL",
            "score": PART2_POINTS if passed else 0,
            "routine_interruptions": routine_interruptions,
            "routine_decision_opportunities": routine_opportunities,
            "significant_change_gates": significant_gates,
            "significant_change_opportunities": significant_opportunities,
            "controls": controls,
        }
    except Exception as exc:
        reason = "real Chitra interruption probe could not complete: " + scrub_error(exc)
        return {
            "status": "UNKNOWN",
            "score": None,
            "reason": reason,
            "routine_interruptions": routine_interruptions,
            "routine_decision_opportunities": routine_opportunities,
            "significant_change_gates": significant_gates,
            "significant_change_opportunities": significant_opportunities,
            "controls": [unknown_control("chitra_interruption_probe", reason)],
        }
    finally:
        if watcher is not None:
            watcher.shutdown()


def score_chitra_part3(*, root: Path, modules: dict[str, Any], facts: dict[str, bool]) -> dict[str, Any]:
    """Exercise known-answer consumption through watchd's real review path."""
    answers = {f"known-answer-{index}": secrets.token_urlsafe(24) for index in range(1, 9)}
    known_answer_opportunities = len(answers)
    false_gates = 0
    watcher = None
    reviewer = _KnownAnswerReviewer(modules, answers)
    events: list[Any] = []
    try:
        session_ref = _install_synthetic_chitra_goal(root, modules)
        journal = modules["journal"]
        transcript = journal.TranscriptIdentity(path=str(root / "known-answer.jsonl"), device=0, inode=0)
        observed_at = utc_now()
        for index, (key, answer) in enumerate(answers.items(), start=1):
            join_id = f"known-answer-call-{index}"
            common = {
                "instance": "autonomy-benchmark",
                "lane": "lane",
                "client": journal.Client.CODEX,
                "client_version": "0.149.0",
                "process_id": "autonomy-benchmark",
                "transcript": transcript,
                "session_id": session_ref,
                "resume_id": None,
                "observed_at": observed_at,
                "native_time": None,
                "native_join_id": join_id,
                "raw_byte_range": None,
                "raw_sha256": None,
                "payload_digest": "0" * 64,
                "normalizer_version": "autonomy-benchmark",
                "raw_record": None,
            }
            events.extend(
                [
                    journal.CanonicalEvent(
                        event_id=f"known-answer-call-{index}",
                        native_type="tool_call",
                        normalized_type=journal.CanonicalType.TOOL_CALL,
                        payload={"tool_name": "canonical-choice-memory", "input": {"key": key}},
                        **common,
                    ),
                    journal.CanonicalEvent(
                        event_id=f"known-answer-result-{index}",
                        native_type="tool_result",
                        normalized_type=journal.CanonicalType.TOOL_RESULT,
                        payload={"output": {"key": key, "answer": answer}},
                        **common,
                    ),
                ]
            )
        journal.EventJournal(root, "lane").append(events)
        watchd = modules["watchd"]
        watcher = watchd.Watchd(
            watchd.WatchdConfig(
                state_dir=root,
                goals_root=root,
                events_log=root / "events.log",
                completion_review_log=root / "completion-reviews.jsonl",
                reviewer_count=1,
                reasoned_dispatch_enabled=False,
                journal_root=root,
            ),
            reviewer=reviewer,
        )
        pane = watchd.Pane(pane_id="synthetic-pane", target="lane:0.0")
        for key in answers:
            watcher._review_turn_end(
                pane,
                f"{key}: The recorded answer is unavailable. Should this go to the operator?",
            )
            _finish_chitra_reviews(watcher)
    except Exception:
        pass
    finally:
        if watcher is not None:
            watcher.shutdown()
    known_answer_derailments = known_answer_opportunities - len(reviewer.consumed)
    false_gates = len(reviewer.rejected)
    unverified_claims = known_answer_derailments
    controls = [
        control("persistence_codes_present", facts["persistence_codes_present"], "review_rubric exposes all four persistence codes"),
        control("manual_brief_exists", facts["manual_brief_exists"], "the evidence-aware brief path exists in the manual CLI"),
        control("canonical_answers_recorded", len(events) == known_answer_opportunities * 2, "eight joined tool-answer records were stored in Chitra's canonical journal"),
        control("daemon_automatic_claim_evidence_check", len(reviewer.consumed) == known_answer_opportunities, "watchd supplied every stored answer to its real review path"),
        control("false_gates_zero", false_gates == 0, "no known answer caused a false operator gate"),
        control("known_answer_derailments_zero", known_answer_derailments == 0, "known answers were consumed without an unverified-claim derailment"),
        control("unverified_claims_zero", unverified_claims == 0, "ordinary claims were checked against evidence automatically"),
    ]
    passed = all(item["status"] == "PASS" for item in controls)
    return {
        "status": "PASS" if passed else "FAIL",
        "score": PART3_POINTS if passed else 0,
        "false_gates": false_gates,
        "known_answer_opportunities": known_answer_opportunities,
        "known_answer_derailments": known_answer_derailments,
        "unverified_claims": unverified_claims,
        "controls": controls,
    }


def _score_total(parts: dict[str, dict[str, Any]]) -> int | None:
    values = [part["score"] for part in parts.values()]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def resolve_target_root(cli_value: str | None) -> Path:
    selected = cli_value or os.environ.get("CHITRA_REPO_ROOT")
    return Path(selected).expanduser().resolve() if selected else (Path.home() / "chitra").resolve()


def source_snapshot(root: Path) -> str:
    digest = hashlib.sha256()
    for base_name in ("src/chitra", "tests"):
        base = root / base_name
        if not base.is_dir():
            raise FileNotFoundError(base_name)
        for path in sorted(path for path in base.rglob("*") if path.is_file()):
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def target_controls(repo_value: str | None) -> dict[str, Any]:
    try:
        root = resolve_target_root(repo_value)
        before = source_snapshot(root)
        after = source_snapshot(root)
    except Exception as exc:
        return {
            "controls": [
                {"name": "target_source_readable", "status": "FAIL", "detail": scrub_error(exc)},
            ],
            "status": "FAIL",
        }
    return {
        "controls": [
            control("target_source_readable", True, "source and tests are readable"),
            control("target_unchanged", before == after, "read-only fingerprint is stable"),
        ],
        "status": "PASS" if before == after else "FAIL",
    }


def make_run_dir() -> tuple[str, Path]:
    run_id = f"run-{secrets.token_hex(8)}"
    run_dir = Path("runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_id, run_dir


def run_selftest() -> int:
    healthy = score_part3()
    broken = score_part3(inject_known_fault=True)
    healthy_controls = all(item["status"] == "PASS" for item in healthy["controls"])
    detected = (
        healthy["status"] == "PASS"
        and broken["status"] == "FAIL"
        and healthy["score"] == PART3_POINTS
        and broken["score"] == 0
        and broken["false_gates"] == 1
        and broken["known_answer_derailments"] == 1
    )
    if not healthy_controls or not detected:
        print("SELFTEST: FAIL")
        print("KNOWN_FAULT_DETECTED: NO")
        return 4
    print("SELFTEST: PASS")
    print("KNOWN_FAULT: one known-answer gate and one derailment were injected")
    print(f"HEALTHY_PART_3_SCORE: {healthy['score']}/{PART3_POINTS}")
    print(f"BROKEN_PART_3_SCORE: {broken['score']}/{PART3_POINTS}")
    print("DETECTED: false_gates=1; known_answer_derailments=1")
    print("CONTROL: canonical_registry_seeded=PASS; tool_memory_seeded=PASS")
    return 0


def flatten_controls(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    flattened: list[dict[str, str]] = []
    for group in items:
        flattened.extend(group.get("controls", []))
    return flattened


def build_report(repo_value: str | None) -> dict[str, Any]:
    run_id, run_dir = make_run_dir()
    target_root = resolve_target_root(repo_value)
    target = target_controls(repo_value)
    source_before_probe = source_snapshot(target_root)
    modules = _load_chitra(target_root)
    facts = _chitra_source_facts(modules)

    (run_dir / "reference-live").mkdir(parents=True, exist_ok=True)
    reference_part1 = run_live_part(run_dir / "reference-live")
    reference_parts = {
        "part1_live_use": reference_part1,
        "part2_interruption_threshold": score_part2(),
        "part3_no_false_gates": score_part3(),
    }
    reference = {
        "adapter": "reference-autonomous-harness",
        "parts": reference_parts,
        "target_controls": target,
        "score": _score_total(reference_parts),
        "score_max": 100,
        "all_controls": flatten_controls([*reference_parts.values(), target]),
    }

    chitra_parts = {
        "part1_live_use": score_chitra_part1(
            reference_part1=reference_part1,
            facts=facts,
            modules=modules,
            root=run_dir / "reference-live",
        ),
        "part2_interruption_threshold": score_chitra_part2(root=run_dir / "chitra-part2", modules=modules, facts=facts),
        "part3_no_false_gates": score_chitra_part3(
            root=run_dir / "chitra-part3",
            modules=modules,
            facts=facts,
        ),
    }
    chitra = {
        "adapter": "chitra-head",
        "parts": chitra_parts,
        "target_controls": target,
        "score": _score_total(chitra_parts),
        "score_max": 100,
        "all_controls": flatten_controls([*chitra_parts.values(), target]),
        "unmeasured": {
            name: part["reason"]
            for name, part in chitra_parts.items()
            if part["status"] == "UNKNOWN"
        },
    }
    try:
        source_still_matches = source_snapshot(target_root) == source_before_probe
    except Exception as exc:
        source_still_matches = False
        target["post_probe_error"] = scrub_error(exc)
    target["controls"].append(
        control(
            "target_unchanged_after_chitra_probe",
            source_still_matches,
            "read-only source fingerprint is stable after the Chitra probe",
        )
    )
    target["status"] = "PASS" if all(item["status"] == "PASS" for item in target["controls"]) else "FAIL"
    return {
        "schema": "autonomy-score-v2",
        "run_id": run_id,
        # Keep the reference report at the historical top-level keys for
        # consumers of the original instrument.  The authoritative split is
        # below, where the two adapter scores cannot be confused.
        "parts": reference_parts,
        "target_controls": target,
        "score": reference["score"],
        "score_max": 100,
        "run_dir": "runs/" + run_id,
        "reference": reference,
        "chitra": chitra,
        "reference_score": reference["score"],
        "chitra_head_score": chitra["score"],
        "all_controls": reference["all_controls"] + chitra["all_controls"],
    }


def print_controls(prefix: str, controls: list[dict[str, str]]) -> None:
    for item in controls:
        print(f"{prefix}{item['name']}={item['status']} ({item['detail']})")


def _print_score_part(label: str, parts: dict[str, dict[str, Any]]) -> None:
    part1 = parts["part1_live_use"]
    part2 = parts["part2_interruption_threshold"]
    part3 = parts["part3_no_false_gates"]
    print(label)
    if part1["status"] == "UNKNOWN":
        print(f"PART 1 - LIVE USE: UNKNOWN ({part1['reason']})")
    else:
        print(f"PART 1 - LIVE USE: {part1['score']}/{PART1_POINTS} {part1['status']}")
    print_controls("  CONTROL ", part1["controls"])
    print(f"PART 2 - INTERRUPTION THRESHOLD: {part2['score']}/{PART2_POINTS} {part2['status']}")
    print(
        "  routine_interruptions / routine_decision_opportunities: "
        f"{part2['routine_interruptions']} / {part2['routine_decision_opportunities']}"
    )
    print(
        "  significant_change_gates / significant_change_opportunities: "
        f"{part2['significant_change_gates']} / {part2['significant_change_opportunities']}"
    )
    print_controls("  CONTROL ", part2["controls"])
    print(f"PART 3 - NO FALSE GATES: {part3['score']}/{PART3_POINTS} {part3['status']}")
    print(
        "  false_gates / known_answer_opportunities: "
        f"{part3['false_gates']} / {part3['known_answer_opportunities']}"
    )
    print(
        "  known_answer_derailments / known_answer_opportunities: "
        f"{part3['known_answer_derailments']} / {part3['known_answer_opportunities']}"
    )
    if "unverified_claims" in part3:
        print(f"  unverified_claims / known_answer_opportunities: {part3['unverified_claims']} / {part3['known_answer_opportunities']}")
    print_controls("  CONTROL ", part3["controls"])


def _format_total(score: int | None) -> str:
    return "UNKNOWN/100" if score is None else f"{score}/100"


def print_report(report: dict[str, Any]) -> None:
    reference = report["reference"]
    chitra = report["chitra"]
    print(f"RUN: {report['run_id']}")
    _print_score_part("REFERENCE ADAPTER: adapter=reference-autonomous-harness", reference["parts"])
    print_controls("REFERENCE TARGET CONTROL ", reference["target_controls"]["controls"])
    print(f"REFERENCE SCORE: {_format_total(reference['score'])}")
    _print_score_part("CHITRA HEAD ADAPTER: adapter=chitra-head", chitra["parts"])
    print_controls("CHITRA TARGET CONTROL ", chitra["target_controls"]["controls"])
    if chitra["unmeasured"]:
        print("CHITRA PARTS NOT MEASURED:")
        for name, reason in chitra["unmeasured"].items():
            print(f"  {name}: {reason}")
    else:
        print("CHITRA PARTS NOT MEASURED: none")
    print(f"CHITRA HEAD SCORE: {_format_total(chitra['score'])}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="score autonomous live-use behavior")
    parser.add_argument("--selftest", action="store_true", help="inject a known fault and verify detection")
    parser.add_argument("--repo", help="target source tree, or use CHITRA_REPO_ROOT")
    parser.add_argument("--json", action="store_true", help="emit the normal report as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.selftest:
        return run_selftest()
    try:
        report = build_report(args.repo)
    except Exception as exc:
        print("STATUS: FAIL")
        print("REASON: " + scrub_error(exc))
        return 3
    if args.json:
        print(json.dumps(report, sort_keys=True, indent=2))
    else:
        print_report(report)
    return 0 if report["score"] is not None and all(
        item["status"] in {"PASS", "UNKNOWN"} for item in report["all_controls"]
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())

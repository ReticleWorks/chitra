#!/usr/bin/env python3
"""Chitra AutoResearch v2 benchmark (fixed specification).

What this is
------------
A deterministic, self-contained benchmark harness that scores the REAL
production module ``chitra.dispatchd`` (imported from ``src/``) on restart
survival and exactly-once dispatch semantics. Every scored point comes from
durable filesystem effects produced by real ``run_once`` executions running
in separate worker processes. Nothing is scored on source markers, bare file
existence, or any reference-model behavior.

Assumed queue layout contract (fixed by this spec): the directory passed to
``run_once`` as ``queue_dir`` contains the sibling stages
``orders/``, ``in_flight/``, ``results/``, ``processed/``. All tmux, network
and process interactions are faked; every write the product performs during
scoring is confined to a per-run temporary world.

Public dimensions (100 points)
------------------------------
D1  restart_resume          28  crash mid-drain; a restarted worker finishes
                                the FIFO with no duplicate work and order
                                preserved
D2  nonce_crash_boundary    22  hard crash between nonce record and paste
                                persistence; restart reconciles confirmed
                                transcript consumption WITHOUT a second paste
                                and with a single ledger commit
D3  terminal_suppression    18  pre-existing terminal results are never
                                redispatched; a stale queued twin is absorbed
D4  multiprocess_race       20  concurrent worker processes drain one queue
                                with exactly-once durable effects
D5  fixture_serialization   12  model_dump_json fixtures drain faithfully AND
                                the harness' lossy hand-built-dict mutation is
                                reliably detected (mandated fixture
                                serialization mutation test)

Hard gates (no points; any failure zeroes the score)
----------------------------------------------------
G1  ``pytest tests/test_dispatchd.py`` passes unchanged.
G2  repository integrity: the checkout is byte-identical before and after
    the run (the product may only touch the benchmark's temp worlds).

Mutation proofs
---------------
Every dimension ships with an injected-regression mutator. ``--selftest``
applies each mutator to freshly collected evidence and requires the
dimension's checker to score strictly lower, proving sensitivity. D5's
mutator is the fixture-serialization mutation itself.

Host-only sealed evaluation
---------------------------
``--sealed PATH`` loads an extra case module that MUST live outside this
checkout (enforced). Sealed cases are built only from this module's public
helper API, so candidate prompts never receive their content. Public runs
never read anything from ``sealed/``.

Usage
-----
  python tools/autoresearch_v2_benchmark.py             # public eval
  python tools/autoresearch_v2_benchmark.py --json      # machine-readable
  python tools/autoresearch_v2_benchmark.py --selftest  # mutation proofs
  python tools/autoresearch_v2_benchmark.py --sealed /host/sealed/cases.py

Exit codes: 0 scored normally, 2 hard-gate failure (score 0),
3 infrastructure/safety failure, 4 selftest detected a blind spot.
Requires Python 3.12; pydantic arrives via the product; pytest for G1.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import enum
import hashlib
import hmac
import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import typing as t
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
MODULE_ALIAS = "autoresearch_v2_public"
WORKER_TIMEOUT_S = 45
TESTS_TIMEOUT_S = 600
MAX_REPO_FILES = 20000
LAYOUT_DIRS = ("orders", "in_flight", "results", "processed")

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class InfraError(RuntimeError):
    """Harness or environment failure that prevents fair scoring (exit 3)."""


_DD = None


def dd():
    global _DD
    if _DD is None:
        try:
            _DD = importlib.import_module("chitra.dispatchd")
        except Exception as exc:
            raise InfraError(
                f"cannot import production chitra.dispatchd from {SRC_ROOT}: {exc}"
            ) from exc
    return _DD


_ID_FIELDS = ("order_id", "id")
_SESSION_FIELDS = ("session_ref", "session", "target", "pane")


def attr_of(obj, names):
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def field_name(model, candidates):
    fields = getattr(model, "model_fields", {})
    for cand in candidates:
        if cand in fields:
            return cand
    return None


def _string_for(name: str) -> str:
    lowered = (name or "value").lower()
    if "session" in lowered or lowered.endswith(("ref", "target", "pane")):
        return "local:bench:0"
    if "nudge" in lowered:
        return "Run the benchmark task."
    if "sha" in lowered or "digest" in lowered or "hash" in lowered:
        return "a" * 64
    if "attestation" in lowered or "corpus" in lowered or "contract" in lowered:
        return "sha256:" + "a" * 64
    if "reason" in lowered or lowered in ("detail", "message"):
        return "seeded"
    return f"seed-{lowered}"


def _annotation_value(name: str, ann, depth: int = 0):
    if depth > 8 or ann is None or ann is t.Any:
        return _string_for(name)
    origin = t.get_origin(ann)
    if origin is t.Annotated:
        return _annotation_value(name, t.get_args(ann)[0], depth + 1)
    if origin is t.Union or origin is types.UnionType:
        args = [a for a in t.get_args(ann) if a is not type(None)] or [t.Any]
        return _annotation_value(name, args[0], depth + 1)
    if origin is t.Literal:
        return t.get_args(ann)[0]
    if origin in (list, set, frozenset, dict, tuple):
        return origin()
    if ann is bool:
        return False
    if ann is int:
        return 1
    if ann is float:
        return 0.0
    if ann is bytes:
        return b"seed"
    if ann is datetime:
        return datetime(2020, 1, 1, tzinfo=timezone.utc)
    if ann is str:
        return _string_for(name)
    if isinstance(ann, type):
        if issubclass(ann, enum.Enum):
            for pref in ("QUEUED", "PENDING", "NEW", "READY", "CREATED", "INIT"):
                if pref in ann.__members__:
                    return ann[pref]
            return next(iter(ann))
        if hasattr(ann, "model_fields"):
            try:
                return synthesize_model(ann, hint=name)
            except Exception:
                return _string_for(name)
    return _string_for(name)


def _apply_field_constraints(value, field) -> object:
    """Respect the portable Pydantic metadata used by current contracts."""
    metadata = getattr(field, "metadata", ())
    minimum = None
    pattern = None
    for item in metadata:
        minimum = getattr(item, "ge", minimum)
        pattern = getattr(item, "pattern", pattern)
    if isinstance(value, int) and minimum is not None:
        value = max(value, minimum)
    if isinstance(value, str) and pattern:
        if "sha256:" in pattern:
            value = "sha256:" + "a" * 64
        elif "[0-9a-f]{64}" in pattern:
            value = "a" * 64
    return value


def synthesize_model(model, hint: str = "order", **overrides):
    """Build a schema-agnostic instance of a real product pydantic model.

    Builds a fully validated instance from current model metadata. Optional
    fields retain their product defaults; required nested models are built
    recursively. Hand-built lossy dicts are never used for seeding.
    """
    fields = getattr(model, "model_fields", None)
    if not fields:
        raise InfraError(
            f"{getattr(model, '__name__', model)} exposes no pydantic model_fields"
        )
    kwargs = {}
    for fname, finfo in fields.items():
        if not finfo.is_required():
            continue
        annotation = getattr(finfo, "annotation", None) or str
        kwargs[fname] = _apply_field_constraints(
            _annotation_value(fname, annotation), finfo
        )
    for cand in _ID_FIELDS:
        if cand in kwargs and cand not in overrides:
            kwargs[cand] = f"{hint}-{uuid.uuid4().hex[:10]}"
            break
    kwargs.update({k: v for k, v in overrides.items() if k in kwargs})
    try:
        return model.model_validate(kwargs)
    except Exception as exc:
        raise InfraError(
            f"could not synthesize valid {getattr(model, '__name__', model)}: {exc}"
        ) from exc


def build_sent_result(order_id: str, session_ref: str):
    product = dd()
    result_model = product.DispatchResult
    members = getattr(product.DispatchStatus, "__members__", {})
    if "SENT" not in members:
        raise InfraError("DispatchStatus has no SENT member")
    wanted = {
        "order_id": (order_id, _ID_FIELDS),
        "session_ref": (session_ref, _SESSION_FIELDS),
        "status": (members["SENT"], ("status", "state")),
        "reason": ("fake-tmux-paste", ("reason", "detail", "message")),
    }
    overrides = {}
    for logical, (value, candidates) in wanted.items():
        actual = field_name(result_model, candidates)
        if actual is not None:
            overrides[actual] = value
    return synthesize_model(result_model, hint="result", **overrides)


@dataclasses.dataclass
class World:
    root: Path

    @property
    def queue(self) -> Path:
        return self.root / "dispatch"

    @property
    def orders(self) -> Path:
        return self.queue / "orders"

    @property
    def in_flight(self) -> Path:
        return self.queue / "in_flight"

    @property
    def results(self) -> Path:
        return self.queue / "results"

    @property
    def processed(self) -> Path:
        return self.queue / "processed"

    @property
    def lock(self) -> Path:
        return self.root / "locks"

    @property
    def workers(self) -> Path:
        return self.root / "workers"

    @property
    def ledger(self) -> Path:
        return self.root / "ledger.jsonl"

    @property
    def ledger_key(self) -> Path:
        return self.root / "ledger.key"

    @property
    def projects(self) -> Path:
        return self.root / "projects"


_ACTIVE_WORLDS: list[Path] = []


def make_world(tag: str) -> World:
    root = Path(tempfile.mkdtemp(prefix=f"chitra-autoresearch-v2-{tag}-"))
    for name in LAYOUT_DIRS:
        (root / "dispatch" / name).mkdir(parents=True)
    (root / "locks").mkdir(parents=True)
    (root / "workers").mkdir(parents=True)
    (root / "projects").mkdir(parents=True)
    (root / "ledger.jsonl").write_bytes(b"")
    (root / "ledger.key").write_bytes(bytes(range(32)))
    _ACTIVE_WORLDS.append(root)
    return World(root=root)


def _cleanup_worlds(force: bool = False) -> None:
    if not _ACTIVE_WORLDS:
        return
    if not force and os.environ.get("CHITRA_BENCH_KEEP"):
        return
    for root in _ACTIVE_WORLDS:
        shutil.rmtree(root, ignore_errors=True)
    _ACTIVE_WORLDS.clear()


def seed_order(world: World, model, index: int) -> str:
    oid = str(attr_of(model, _ID_FIELDS) or f"seed-{index}")
    payload = model.model_dump_json().encode("utf-8")
    target = world.orders / f"{oid}.json"
    target.write_bytes(payload)
    stamp = 1_700_000_000 + index
    os.utime(target, (stamp, stamp))
    return oid


class FakeTmuxDispatcher:
    """Stand-in for the real tmux paste bridge.

    Durably records every paste (global JSONL log plus a per-session
    transcript mirror under projects_root, the surface on which restart
    reconciliation observes confirmed consumption), optionally dies hard at
    the configured crash boundary, and otherwise returns the keyword-built
    DispatchResult the product contract requires.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.paste_log = Path(cfg["paste_log"])
        self.projects_root = Path(cfg["projects_root"])
        self._crashed = 0

    def __call__(self, *args, **kwargs):
        pool = list(args) + list(kwargs.values())
        order_id = None
        for obj in pool:
            order_id = attr_of(obj, _ID_FIELDS)
            if order_id is not None:
                break
        order_id = str(order_id or self.cfg.get("fallback_order_id", "unknown-order"))
        session_ref = None
        for obj in pool:
            session_ref = attr_of(obj, _SESSION_FIELDS)
            if session_ref is not None:
                break
        session_ref = str(session_ref or "bench-session")
        self._record(order_id, session_ref)
        crash = self.cfg.get("crash") or {}
        if crash.get("order_id") == order_id and self._crashed < int(crash.get("times", 1)):
            self._crashed += 1
            sys.stderr.write(f"[fake-tmux] crashing at paste boundary for {order_id}\n")
            sys.stderr.flush()
            os._exit(int(crash.get("code", 137)))
        return build_sent_result(order_id, session_ref)

    def _record(self, order_id: str, session_ref: str) -> None:
        record = json.dumps(
            {"order_id": order_id, "session": session_ref,
             "pid": os.getpid(), "wall_ns": time.time_ns()},
            sort_keys=True,
        )
        self.paste_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.paste_log, "a", encoding="utf-8") as handle:
            handle.write(record + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        session_dir = self.projects_root / session_ref
        session_dir.mkdir(parents=True, exist_ok=True)
        with open(session_dir / "transcript.log", "a", encoding="utf-8") as handle:
            handle.write(f"consumed order={order_id}\n")
            handle.flush()
            os.fsync(handle.fileno())


def _barrier_wait(barrier_dir: Path, name: str, count: int, deadline_s: float) -> None:
    barrier_dir.mkdir(parents=True, exist_ok=True)
    (barrier_dir / f"{name}.ready").write_text("1", encoding="utf-8")
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        ready = sum(1 for p in barrier_dir.glob("*.ready") if p.is_file())
        if ready >= count:
            return
        time.sleep(0.01)
    raise InfraError(f"worker '{name}' timed out waiting for the race barrier")


def _worker_main(cfg_path: str) -> None:
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    product = dd()
    product.dispatch_to_tmux = FakeTmuxDispatcher(cfg)
    barrier = cfg.get("barrier")
    if barrier:
        _barrier_wait(Path(barrier["dir"]), barrier["name"],
                      int(barrier["count"]), float(barrier.get("deadline_s", 30)))
    queue_dir = Path(cfg["queue_dir"])
    lock_dir = Path(cfg["lock_dir"])
    ledger_path = Path(cfg["ledger_path"])
    ledger_key_path = Path(cfg["ledger_key_path"])
    projects_root = Path(cfg["projects_root"])
    runs = 0
    for _ in range(int(cfg["iterations"])):
        product.run_once(
            queue_dir,
            lock_dir=lock_dir,
            ledger_path=ledger_path,
            ledger_key_path=ledger_key_path,
            projects_root=projects_root,
        )
        runs += 1
    Path(cfg["summary_path"]).write_text(
        json.dumps({"runs": runs, "pid": os.getpid()}), encoding="utf-8"
    )


def _build_cfg(world: World, spec: dict) -> dict:
    name = spec["name"]
    return {
        "name": name,
        "queue_dir": str(world.queue),
        "lock_dir": str(world.lock),
        "ledger_path": str(world.ledger),
        "ledger_key_path": str(world.ledger_key),
        "projects_root": str(world.projects),
        "paste_log": str(world.workers / f"{name}.paste.jsonl"),
        "summary_path": str(world.workers / f"{name}.summary.json"),
        "iterations": int(spec.get("iterations", 8)),
        "crash": spec.get("crash"),
        "barrier": spec.get("barrier"),
    }


def spawn_workers(world: World, specs, timeout: float = WORKER_TIMEOUT_S) -> list[dict]:
    """Launch worker subprocesses concurrently and reap them with a budget."""
    if not specs:
        return []
    script = Path(__file__).resolve()
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [str(SRC_ROOT)] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    launched = []
    for spec in specs:
        cfg = _build_cfg(world, spec)
        cfg_path = world.workers / f"{spec['name']}.cfg.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        cmd = [sys.executable, str(script), "--worker", str(cfg_path)]
        proc = subprocess.Popen(cmd, cwd=str(world.root), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        launched.append((spec, proc))
    outs = []
    deadline = time.monotonic() + timeout
    for spec, proc in launched:
        remaining = max(1.0, deadline - time.monotonic())
        try:
            stdout, stderr = proc.communicate(timeout=remaining)
            rc, timed_out = proc.returncode, False
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            rc, timed_out = None, True
        out = {"name": spec["name"], "rc": rc, "timed_out": timed_out,
               "stderr": (stderr or "")[-2000:], "stdout": (stdout or "")[-1000:]}
        if not timed_out:
            expected = tuple(spec.get("expect_rc", (0,)))
            if rc not in expected:
                raise InfraError(
                    f"worker '{spec['name']}' exited rc={rc}, expected {expected}; "
                    f"stderr tail: {out['stderr'][-800:]}"
                )
        outs.append(out)
    return outs


def _list_files(*candidates: Path) -> list[Path]:
    found = []
    for d in candidates:
        try:
            found.extend(p for p in d.iterdir() if p.is_file())
        except (FileNotFoundError, NotADirectoryError):
            pass
    return found


@dataclasses.dataclass
class Evidence:
    attempts: Counter
    processed_seq: list
    results_ids: set
    in_flight: set
    orders_left: set
    ledger_tokens: Counter
    fifo_prefix_ok: bool
    worker_rcs: list
    worker_timeouts: list
    summaries: int


def collect_evidence(world: World, known_ids, worker_outs) -> Evidence:
    attempts: Counter = Counter()
    for plog in sorted(world.workers.glob("*.paste.jsonl")):
        for line in plog.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                attempts[str(json.loads(line)["order_id"])] += 1
            except (ValueError, KeyError, TypeError):
                attempts["<malformed-paste-record>"] += 1
    processed_paths = _list_files(world.queue / "processed", world.root / "processed")
    processed_paths.sort(key=lambda p: (p.stat().st_mtime_ns, p.name))
    processed_seq = [p.stem for p in processed_paths]
    results_ids = {p.stem for p in _list_files(world.queue / "results",
                                               world.root / "results")}
    in_flight = {p.stem for p in _list_files(world.queue / "in_flight",
                                             world.root / "in_flight")}
    orders_left = {p.stem for p in _list_files(world.queue / "orders",
                                               world.root / "orders")}
    try:
        ledger_text = world.ledger.read_text(encoding="utf-8", errors="replace")
    except OSError:
        ledger_text = ""
    ledger_tokens = Counter({oid: ledger_text.count(oid) for oid in known_ids})
    position = {oid: i for i, oid in enumerate(known_ids)}
    last = -1
    fifo_ok = True
    for oid in processed_seq:
        idx = position.get(oid, -1)
        if idx == -1:
            continue
        if idx < last:
            fifo_ok = False
            break
        last = idx
    rcs = [o.get("rc") for o in worker_outs]
    timeouts = [o["name"] for o in worker_outs if o.get("timed_out")]
    summaries = sum(1 for o in worker_outs
                    if (world.workers / f"{o['name']}.summary.json").is_file())
    return Evidence(attempts, processed_seq, results_ids, in_flight, orders_left,
                    ledger_tokens, fifo_ok, rcs, timeouts, summaries)


def run_d1() -> dict:
    world = make_world("d1")
    ids = [seed_order(world, synthesize_model(dd().DispatchOrder, hint=f"o{i}"), i)
           for i in range(4)]
    notes = []
    phase_a = spawn_workers(world, [{
        "name": "phaseA", "iterations": 12,
        "crash": {"order_id": ids[1], "times": 1},
        "expect_rc": (0, 137),
    }])[0]
    spawn_workers(world, [{"name": "phaseB", "iterations": 12}])
    evidence = collect_evidence(world, ids, [
        {"name": "phaseA", "rc": phase_a["rc"], "timed_out": phase_a["timed_out"]},
        {"name": "phaseB", "rc": 0, "timed_out": False},
    ])
    crash_seen = phase_a["rc"] == 137
    if not crash_seen:
        notes.append("phase A exited without hitting the paste crash boundary")
    return {"ev": evidence, "ids": ids, "crash_seen": crash_seen, "notes": notes}


def check_d1(bundle: dict) -> int:
    if not bundle["crash_seen"]:
        return 0
    ev, ids = bundle["ev"], bundle["ids"]
    counts = Counter(ev.processed_seq)
    score = 0
    if all(counts[o] == 1 for o in ids):
        score += 8
    if all(ev.attempts[o] == 1 for o in ids):
        score += 8
    if not ev.in_flight and not ev.orders_left:
        score += 4
    if ev.fifo_prefix_ok:
        score += 4
    if all(o in ev.results_ids for o in ids):
        score += 4
    return score


def mutate_d1(bundle: dict) -> dict:
    damaged = copy.deepcopy(bundle)
    victim = damaged["ids"][0]
    damaged["ev"].attempts[victim] += 1
    damaged["ev"].processed_seq.append(victim)
    return damaged


def run_d2() -> dict:
    world = make_world("d2")
    oid = seed_order(world, synthesize_model(dd().DispatchOrder, hint="x1"), 0)
    notes = []
    boundary = spawn_workers(world, [{
        "name": "boundary", "iterations": 12,
        "crash": {"order_id": oid, "times": 1},
        "expect_rc": (0, 137),
    }])[0]
    spawn_workers(world, [{"name": "recover", "iterations": 12}])
    evidence = collect_evidence(world, [oid], [
        {"name": "boundary", "rc": boundary["rc"], "timed_out": boundary["timed_out"]},
        {"name": "recover", "rc": 0, "timed_out": False},
    ])
    crash_seen = boundary["rc"] == 137
    if not crash_seen:
        notes.append("boundary worker did not die at the paste boundary")
    return {"ev": evidence, "ids": [oid], "crash_seen": crash_seen, "notes": notes}


def check_d2(bundle: dict) -> int:
    if not bundle["crash_seen"]:
        return 0
    ev, oid = bundle["ev"], bundle["ids"][0]
    score = 0
    if ev.attempts[oid] == 1:
        score += 10
    if oid in ev.results_ids and ev.processed_seq.count(oid) == 1 and not ev.in_flight:
        score += 6
    if 1 <= ev.ledger_tokens[oid] <= 2:
        score += 6
    return score


def mutate_d2(bundle: dict) -> dict:
    damaged = copy.deepcopy(bundle)
    oid = damaged["ids"][0]
    damaged["ev"].attempts[oid] += 1
    damaged["ev"].ledger_tokens[oid] += 1
    return damaged


def run_d3() -> dict:
    world = make_world("d3")
    product = dd()
    t0 = seed_order(world, synthesize_model(product.DispatchOrder, hint="t0"), 0)
    c1 = seed_order(world, synthesize_model(product.DispatchOrder, hint="c1"), 1)
    terminal = build_sent_result(t0, "bench-session")
    (world.queue / "results" / f"{t0}.json").write_bytes(
        terminal.model_dump_json().encode("utf-8"))
    out = spawn_workers(world, [{"name": "main", "iterations": 8}])[0]
    evidence = collect_evidence(world, [t0, c1], [
        {"name": "main", "rc": out["rc"], "timed_out": out["timed_out"]}])
    return {"ev": evidence, "t0": t0, "c1": c1, "notes": []}


def check_d3(bundle: dict) -> int:
    ev, t0, c1 = bundle["ev"], bundle["t0"], bundle["c1"]
    score = 0
    if ev.attempts[t0] == 0:
        score += 8
    if t0 not in ev.in_flight and t0 not in ev.orders_left:
        score += 5
    if ev.attempts[c1] == 1 and c1 in ev.results_ids and ev.processed_seq.count(c1) == 1:
        score += 5
    return score


def mutate_d3(bundle: dict) -> dict:
    damaged = copy.deepcopy(bundle)
    damaged["ev"].attempts[damaged["t0"]] += 1
    return damaged


def run_d4() -> dict:
    world = make_world("d4")
    ids = [seed_order(world, synthesize_model(dd().DispatchOrder, hint=f"r{i}"), i)
           for i in range(6)]
    barrier_dir = world.workers / "barrier"
    barrier_dir.mkdir(parents=True, exist_ok=True)
    specs = [{"name": f"race{i}", "iterations": 10,
              "barrier": {"dir": str(barrier_dir), "name": f"race{i}",
                          "count": 3, "deadline_s": 30}}
             for i in range(3)]
    outs = spawn_workers(world, specs)
    evidence = collect_evidence(world, ids, outs)
    return {"ev": evidence, "ids": ids, "workers": len(outs), "notes": []}


def check_d4(bundle: dict) -> int:
    ev, ids = bundle["ev"], bundle["ids"]
    counts = Counter(ev.processed_seq)
    score = 0
    if all(ev.attempts[o] == 1 for o in ids):
        score += 10
    if all(counts[o] == 1 for o in ids):
        score += 6
    if not ev.in_flight:
        score += 2
    if not ev.worker_timeouts and ev.summaries == bundle["workers"]:
        score += 2
    return score


def mutate_d4(bundle: dict) -> dict:
    damaged = copy.deepcopy(bundle)
    victim = damaged["ids"][2]
    damaged["ev"].attempts[victim] += 1
    if victim in damaged["ev"].processed_seq:
        damaged["ev"].processed_seq.remove(victim)
    return damaged


def fixture_is_lossless(raw: bytes, candidate) -> bool:
    """True only when the candidate dict preserves every original field."""
    try:
        original = json.loads(raw.decode("utf-8"))
        if not isinstance(original, dict) or not isinstance(candidate, dict):
            return False
        if not set(original.keys()) <= set(candidate.keys()):
            return False
        order_model = dd().DispatchOrder
        rebuilt = order_model.model_validate(candidate)
        reference = order_model.model_validate_json(raw)
        return rebuilt.model_dump() == reference.model_dump()
    except Exception:
        return False


def _lossy_dict_mutation(raw: bytes):
    obj = json.loads(raw.decode("utf-8"))
    droppable = [k for k in obj if k not in _ID_FIELDS]
    if droppable:
        del obj[droppable[0]]
    if obj:
        key = sorted(obj)[-1]
        obj[key + "_mangled"] = obj.pop(key)
    return obj


def run_d5() -> dict:
    product = dd()
    order_model = product.DispatchOrder
    reason_field = field_name(order_model, ("reason", "detail", "message"))
    int_field = field_name(order_model, ("priority", "weight", "attempts", "retries"))
    str_field = field_name(order_model, ("payload", "body", "text", "note", "prompt"))
    world = make_world("d5")
    ids, raws = [], []
    for i in range(3):
        overrides = {}
        if reason_field:
            overrides[reason_field] = f"seed raison ok {i}"
        if int_field:
            overrides[int_field] = i
        if str_field:
            overrides[str_field] = f"payload-{i}-unicode"
        model = synthesize_model(order_model, hint=f"f{i}", **overrides)
        oid = seed_order(world, model, i)
        ids.append(oid)
        raws.append((world.orders / f"{oid}.json").read_bytes())
        order_model.model_validate_json(raws[-1])
    out = spawn_workers(world, [{"name": "drain", "iterations": 8}])[0]
    evidence = collect_evidence(world, ids, [
        {"name": "drain", "rc": out["rc"], "timed_out": out["timed_out"]}])
    fidelity = (
        not evidence.worker_timeouts
        and all(evidence.attempts[o] == 1 for o in ids)
        and all(evidence.processed_seq.count(o) == 1 for o in ids)
        and all(o in evidence.results_ids for o in ids)
    )
    detected = not fixture_is_lossless(raws[0], _lossy_dict_mutation(raws[0]))
    return {"ev": evidence, "ids": ids, "fidelity": fidelity,
            "detected": detected, "notes": []}


def check_d5(bundle: dict) -> int:
    return (6 if bundle["fidelity"] else 0) + (6 if bundle["detected"] else 0)


def mutate_d5(bundle: dict) -> dict:
    damaged = copy.deepcopy(bundle)
    damaged["detected"] = False
    return damaged


@dataclasses.dataclass
class Dimension:
    key: str
    title: str
    max_points: int
    run: t.Callable[[], dict]
    check: t.Callable[[dict], int]
    mutate: t.Callable[[dict], dict]
    mutation_desc: str


PUBLIC_DIMENSIONS = [
    Dimension(key="D1", title="restart resume without duplicate work",
              max_points=28, run=run_d1, check=check_d1, mutate=mutate_d1,
              mutation_desc="a processed order re-enters history with a second paste"),
    Dimension(key="D2", title="nonce crash boundary reconciles without second paste",
              max_points=22, run=run_d2, check=check_d2, mutate=mutate_d2,
              mutation_desc="double paste plus double ledger commit after the crash"),
    Dimension(key="D3", title="terminal results are never redispatched",
              max_points=18, run=run_d3, check=check_d3, mutate=mutate_d3,
              mutation_desc="a terminal order is pasted again"),
    Dimension(key="D4", title="multi-process race keeps exactly-once effects",
              max_points=20, run=run_d4, check=check_d4, mutate=mutate_d4,
              mutation_desc="lost update: one order vanishes, another is pasted twice"),
    Dimension(key="D5", title="fixture serialization integrity",
              max_points=12, run=run_d5, check=check_d5, mutate=mutate_d5,
              mutation_desc="lossy hand-built dict fixtures pass undetected"),
]


_SNAPSHOT_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv",
                       "node_modules", ".mypy_cache", ".ruff_cache", ".tox",
                       ".eggs", "dist", "build"}
_SNAPSHOT_SKIP_SUFFIXES = {".pyc", ".pyo", ".orig", ".rej"}


def _hash_tree(root: Path) -> dict:
    out = {}
    if root.is_file():
        try:
            out[str(root.relative_to(REPO_ROOT))] = hashlib.sha256(
                root.read_bytes()).hexdigest()
        except OSError:
            pass
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SNAPSHOT_SKIP_DIRS)
        for fn in sorted(filenames):
            if Path(fn).suffix in _SNAPSHOT_SKIP_SUFFIXES:
                continue
            path = Path(dirpath) / fn
            try:
                out[str(path.relative_to(REPO_ROOT))] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
            except OSError:
                pass
    return out


def _snapshot_repo() -> dict:
    full = _hash_tree(REPO_ROOT)
    if len(full) <= MAX_REPO_FILES:
        return full
    merged = {}
    for part in (_hash_tree(SRC_ROOT), _hash_tree(REPO_ROOT / "tests"),
                 _hash_tree(REPO_ROOT / "tools"),
                 _hash_tree(REPO_ROOT / "pyproject.toml")):
        merged.update(part)
    return merged


def run_product_tests() -> dict:
    groups = {
        "dispatch": ["tests/test_dispatchd.py"],
        "original_hard_regressions": [
            "tests/test_session_contract.py", "tests/test_provider_protocol.py",
            "tests/test_thin_initial_launch.py", "tests/test_joined_lane.py",
        ],
        "strict_close_recovery": [
            "tests/test_tophand_wire.py", "tests/test_governed_close_simple.py",
            "tests/test_recovery_tophand_close_facade.py",
            "tests/test_orb_production_admission.py",
            "tests/test_orb_production_admission_adversarial.py",
            "tests/test_production_recovery_wiring.py",
        ],
    }
    results = {}
    for name, files in groups.items():
        cmd = [sys.executable, "-m", "pytest", *files, "-q"]
        try:
            proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True,
                                  text=True, timeout=TESTS_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as exc:
            results[name] = {"ok": False, "error": str(exc)}
            continue
        tail = (proc.stdout or "") + (proc.stderr or "")
        results[name] = {"ok": proc.returncode == 0, "rc": proc.returncode,
                         "tail": tail[-1500:]}
    return {"ok": all(result["ok"] for result in results.values()),
            "groups": results}


def public_api() -> dict:
    return {
        "dd": dd, "make_world": make_world, "seed_order": seed_order,
        "synthesize_model": synthesize_model, "field_name": field_name,
        "attr_of": attr_of, "build_sent_result": build_sent_result,
        "spawn_workers": spawn_workers, "collect_evidence": collect_evidence,
        "Dimension": Dimension, "InfraError": InfraError,
        "_ID_FIELDS": _ID_FIELDS, "WORKER_TIMEOUT_S": WORKER_TIMEOUT_S,
    }


def _sealed_bytes(path_str: str, expected_sha256: str) -> bytes:
    """Read host-only source once through a verified, non-following FD."""
    if not os.path.isabs(path_str):
        raise InfraError("sealed cases path must be absolute")
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise InfraError("sealed cases require a lowercase SHA-256 pin")
    path = Path(path_str)
    checkout = REPO_ROOT.resolve()
    try:
        lexical = path.parent.resolve()
    except OSError as exc:
        raise InfraError("sealed cases parent cannot be resolved") from exc
    if checkout == lexical or checkout in lexical.parents:
        raise InfraError("sealed cases must live OUTSIDE the candidate checkout")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        raise InfraError("platform lacks O_NOFOLLOW for sealed loading")
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW)
    except OSError as exc:
        raise InfraError("sealed cases could not be opened securely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InfraError("sealed cases must be a regular file")
        if info.st_uid != os.getuid():
            raise InfraError("sealed cases must be owned by the current uid")
        if info.st_mode & 0o077:
            raise InfraError("sealed cases must not grant group or world access")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        source = b"".join(chunks)
    finally:
        os.close(fd)
    actual = hashlib.sha256(source).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise InfraError("sealed cases digest does not match the pinned SHA-256")
    return source


def load_sealed(path_str: str, expected_sha256: str) -> list[Dimension]:
    source = _sealed_bytes(path_str, expected_sha256)
    sys.modules.setdefault(MODULE_ALIAS, sys.modules[__name__])
    module = types.ModuleType("chitra_autoresearch_sealed")
    try:
        exec(compile(source, "<host-only-sealed>", "exec"), module.__dict__)
    except Exception as exc:
        raise InfraError("sealed cases could not be compiled") from exc
    register = getattr(module, "register", None)
    if not callable(register):
        raise InfraError("sealed cases module must expose register(api)")
    dims = register(public_api())
    if not dims:
        raise InfraError("sealed cases module registered no dimensions")
    return list(dims)


def sealed_loader_selftest() -> dict:
    """Exercise accepted and adversarial loader paths without emitting paths."""
    root = Path(tempfile.mkdtemp(prefix="chitra-sealed-loader-"))
    try:
        source = b"def register(api):\n    return [object()]\n"
        good = root / "cases.py"
        good.write_bytes(source)
        good.chmod(0o600)
        digest = hashlib.sha256(source).hexdigest()
        accepted = len(load_sealed(str(good), digest)) == 1
        rejected = 0
        for kind in ("digest", "permissions", "symlink"):
            try:
                if kind == "digest":
                    load_sealed(str(good), "0" * 64)
                elif kind == "permissions":
                    good.chmod(0o640)
                    load_sealed(str(good), digest)
                else:
                    good.chmod(0o600)
                    link = root / "link.py"
                    link.symlink_to(good)
                    load_sealed(str(link), digest)
            except InfraError:
                rejected += 1
        return {"accepted": accepted, "adversarial_rejections": rejected,
                "privacy_ok": True}
    finally:
        shutil.rmtree(root, ignore_errors=True)


_PRODUCT_MUTATIONS: dict[str, tuple[str, bytes, bytes]] = {
    "D1": (
        "drop the oldest pending order from every dispatch pass",
        b"    pending = [path for _, _, path in sorted(dated, key=lambda item: item[:2])]\n",
        b"    pending = [path for _, _, path in sorted(dated, key=lambda item: item[:2])][1:]\n",
    ),
    "D2": (
        "re-dispatch a nonce-marked order and discard its successful result",
        b"        if nonce_path.exists():\n",
        (
            b"        if nonce_path.exists():\n"
            b"            result = dispatch_to_tmux(\n"
            b"                order, policy=policy, tuning=tuning, runner=dispatch_runner,\n"
            b"                projects_root=projects_root, local_extra=local_extra,\n"
            b"                tmux_socket=tmux_socket,\n"
            b"            )\n"
            b"            result.status = DispatchStatus.DELIVERY_UNCONFIRMED\n"
            b"        elif False:  # injected defect: real nonce reconciliation bypassed\n"
        ),
    ),
    "D3": (
        "hide the durable terminal result from both suppression checks",
        b'    existing_result = results_dir / f"{order.order_id}.json"\n',
        b'    existing_result = results_dir / f".mutant-{order.order_id}.json"\n',
    ),
}
_CLEAN_DIMENSION_SCORES = {"D1": 12, "D2": 10, "D3": 13}
_PRODUCT_MUTATION_TIMEOUT_S = 180


def _copy_for_product_mutation() -> Path:
    container = Path(tempfile.mkdtemp(prefix="chitra-product-mutation-"))
    checkout = container / "repo"
    shutil.copytree(
        REPO_ROOT,
        checkout,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".venv", "*.pyc"
        ),
    )
    return checkout


def _run_dimension_probe(checkout: Path, key: str) -> int:
    command = [
        sys.executable,
        str(checkout / "tools" / "autoresearch_v2_benchmark.py"),
        "--internal-dimension",
        key,
    ]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(checkout / "src"),
            environment.get("PYTHONPATH", ""),
        ]
    ).rstrip(os.pathsep)
    try:
        completed = subprocess.run(
            command,
            cwd=str(checkout),
            env=environment,
            capture_output=True,
            text=True,
            timeout=_PRODUCT_MUTATION_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InfraError(f"{key} product-mutation probe timed out") from exc
    if completed.returncode != 0:
        tail = ((completed.stdout or "") + (completed.stderr or ""))[-800:]
        raise InfraError(
            f"{key} product-mutation probe exited {completed.returncode}: {tail}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InfraError(f"{key} product-mutation probe returned invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("mode") != "internal-product-mutation-probe"
        or payload.get("key") != key
        or payload.get("admission_evidence") is not False
    ):
        raise InfraError(f"{key} product-mutation probe returned the wrong contract")
    score = payload.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        raise InfraError(f"{key} product-mutation probe score is not an integer")
    return score


def _prove_product_mutation(key: str) -> dict:
    description, target_bytes, replacement_bytes = _PRODUCT_MUTATIONS[key]
    clean_checkout = _copy_for_product_mutation()
    mutant_checkout = _copy_for_product_mutation()
    try:
        clean_score = _run_dimension_probe(clean_checkout, key)
        expected_clean = _CLEAN_DIMENSION_SCORES[key]
        if clean_score != expected_clean:
            raise InfraError(
                f"{key} clean score changed: {clean_score}; expected {expected_clean}"
            )
        product_path = mutant_checkout / "src" / "chitra" / "dispatchd.py"
        product_bytes = product_path.read_bytes()
        occurrences = product_bytes.count(target_bytes)
        if occurrences != 1:
            raise InfraError(
                f"{key} production mutation target occurs {occurrences} times; expected 1"
            )
        product_path.write_bytes(product_bytes.replace(target_bytes, replacement_bytes, 1))
        mutated_score = _run_dimension_probe(mutant_checkout, key)
        return {
            "key": key,
            "clean": clean_score,
            "mutated": mutated_score,
            "detects_regression": mutated_score < clean_score,
            "mutation": description,
            "production_path": "src/chitra/dispatchd.py",
            "fresh_process": True,
        }
    finally:
        shutil.rmtree(clean_checkout.parent, ignore_errors=True)
        shutil.rmtree(mutant_checkout.parent, ignore_errors=True)


def run_selftest(report: dict, as_json: bool, sealed_dimensions: list[Dimension]) -> int:
    results = []
    ok = bool(report.get("gates", {}).get("product_tests", {}).get("ok"))
    for key in sorted(_PRODUCT_MUTATIONS):
        try:
            entry = _prove_product_mutation(key)
            ok = ok and bool(entry["detects_regression"])
        except InfraError as exc:
            entry = {"key": key}
            entry["error"] = str(exc)
            ok = False
        results.append(entry)
    raw = synthesize_model(dd().DispatchOrder, hint="fx").model_dump_json().encode("utf-8")
    fixture_detected = not fixture_is_lossless(raw, _lossy_dict_mutation(raw))
    results.append({"key": "FIXTURE-SERIALIZATION-MUTATION",
                    "detected": fixture_detected,
                    "desc": "hand-built lossy dict round-trip must be rejected"})
    loader = sealed_loader_selftest()
    results.append({"key": "SEALED-LOADER-PRIVACY",
                    "accepted": loader["accepted"],
                    "adversarial_rejections": loader["adversarial_rejections"],
                    "privacy_ok": loader["privacy_ok"]})
    ok = ok and fixture_detected
    ok = ok and loader["accepted"] and loader["adversarial_rejections"] == 3 and loader["privacy_ok"]
    report["selftest"] = results
    if sealed_dimensions:
        report["sealed_mutations"] = {
            "families": 0,
            "detected": 0,
            "note": "sealed cases are not read or mutated by public selftest",
        }
    report["ok"] = ok
    emit(report, as_json)
    return 0 if ok else 4


def emit(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return
    print(f"== Chitra AutoResearch v2 :: {report.get('mode', '?')} evaluation ==")
    for name, gate in report.get("gates", {}).items():
        state = "PASS" if gate.get("ok") else "FAIL"
        print(f"  gate {name}: {state}")
        if not gate.get("ok") and gate.get("tail"):
            lines = gate["tail"].strip().splitlines()
            if lines:
                print("    " + lines[-1][:160])
    for dim in report.get("dimensions", []):
        print(f"  [{dim['score']:>3}/{dim['max']:<3}] {dim['key']} {dim['title']}")
        for note in dim.get("notes", []):
            print(f"        note: {note}")
    if "selftest" in report:
        for row in report["selftest"]:
            print(f"  selftest {json.dumps(row, sort_keys=True, default=str)}")
        verdict = "PASS" if report.get("ok") else "FAIL"
        print(f"  selftest verdict: {verdict}")
    print(f"  SCORE: {report.get('score', 0)}/{report.get('max_points', 0)}")
    for err in report.get("errors", []):
        print(f"  error: {err}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="autoresearch_v2_benchmark",
        description=("Fixed v2 AutoResearch benchmark for chitra.dispatchd "
                     "(public plus host-only sealed evaluation)."))
    parser.add_argument("--json", action="store_true",
                        help="machine-readable report on stdout")
    parser.add_argument("--selftest", action="store_true",
                        help="verify every scored dimension detects its regression")
    parser.add_argument("--sealed", metavar="PATH", default=None,
                        help="host-only sealed cases module, outside this checkout")
    parser.add_argument("--sealed-sha256", metavar="SHA256", default=None,
                        help="required SHA-256 pin for --sealed")
    parser.add_argument("--keep-worlds", action="store_true",
                        help="keep temp worlds for debugging")
    parser.add_argument("--worker", metavar="CFG", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--internal-dimension", metavar="KEY", default=None,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.keep_worlds:
        os.environ["CHITRA_BENCH_KEEP"] = "1"
    if args.worker:
        _worker_main(args.worker)
        return 0

    if args.internal_dimension:
        if args.selftest or args.sealed or args.sealed_sha256 or args.keep_worlds:
            parser.error("--internal-dimension rejects ordinary and sealed modes")
        dimension = next(
            (item for item in PUBLIC_DIMENSIONS if item.key == args.internal_dimension),
            None,
        )
        if dimension is None:
            parser.error("--internal-dimension requires D1, D2, D3, D4, or D5")
        try:
            bundle = dimension.run()
            score = int(dimension.check(bundle))
        finally:
            _cleanup_worlds(force=True)
        print(
            json.dumps(
                {
                    "mode": "internal-product-mutation-probe",
                    "key": dimension.key,
                    "score": score,
                    "admission_evidence": False,
                },
                sort_keys=True,
            )
        )
        return 0

    if bool(args.sealed) != bool(args.sealed_sha256):
        parser.error("--sealed and --sealed-sha256 must be supplied together")

    report = {"mode": "sealed" if args.sealed else "public",
              "score": 0, "max_points": 0, "gates": {}, "dimensions": [],
              "errors": []}
    try:
        snap_before = _snapshot_repo()
        report["gates"]["product_tests"] = run_product_tests()
        sealed_dimensions = load_sealed(args.sealed, args.sealed_sha256) if args.sealed else []
        if args.selftest:
            return run_selftest(report, args.json, sealed_dimensions)
        dimensions = list(PUBLIC_DIMENSIONS)
        dimensions = dimensions + sealed_dimensions
        total = 0
        max_total = 0
        sealed_score = 0
        sealed_max = 0
        for dim in dimensions:
            entry = {"key": dim.key, "title": dim.title,
                     "max": int(dim.max_points), "score": 0, "notes": []}
            try:
                bundle = dim.run()
                raw_score = int(dim.check(bundle))
                entry["score"] = max(0, min(int(dim.max_points), raw_score))
                entry["notes"] = [str(n) for n in bundle.get("notes", [])]
            except InfraError as exc:
                entry["notes"].append(f"infrastructure: {exc}")
            total += entry["score"]
            max_total += int(dim.max_points)
            if dim in sealed_dimensions:
                sealed_score += entry["score"]
                sealed_max += int(dim.max_points)
            else:
                report["dimensions"].append(entry)
            _cleanup_worlds()
        if sealed_dimensions:
            report["sealed"] = {"families": len(sealed_dimensions),
                                "score": sealed_score, "max": sealed_max}
        report["max_points"] = max_total
        integrity_ok = _snapshot_repo() == snap_before
        report["gates"]["repo_integrity"] = {"ok": integrity_ok}
        gates_ok = bool(report["gates"]["product_tests"].get("ok")) and integrity_ok
        report["score"] = total if gates_ok else 0
        rc = 0 if gates_ok else 2
    except InfraError as exc:
        report["errors"].append(str(exc))
        report["score"] = 0
        rc = 3
    finally:
        _cleanup_worlds(force=True)
    emit(report, args.json)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

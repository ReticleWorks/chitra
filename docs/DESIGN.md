# Design notes

## Origin

chitra grew out of a tmux dispatch function that had two real, silently-triggering bugs (documented in `src/chitra/dispatch.py`'s module docstring). It was pulled out into its own hardened, tested library alongside a small set of daemons that turn "occasionally invoke this function from an interactive AI session" into "always-on, deterministic, systemd-supervised background service."

## Bounded reasoning boundary

chitra delivers messages to, and observes the state of, sessions that are themselves driven by an LLM. Its queue, dispatch, evidence, and storage paths remain deterministic. `monitord` binds each transcript to one frozen goal, persists bounded corrective action and question answers, and independently executes enrolled validators. It never writes the terminal; `dispatchd` remains the sole writer. Legacy `watchd` deployments may invoke isolated completion reviewers, but their signals cannot bypass the operator gates for spend, credentials, irreversible actions, security boundaries, or strategy redirects.

Task decomposition and response generation remain out of scope. Task decomposition — breaking a high-level goal into a sequence of work items — belongs in a higher-level system; chitra only delivers and tracks work that has already been decided. Response generation — drafting content for a watched session — is never chitra's role. Agent orchestration, by contrast, is central to chitra's design: it dispatches messages into tmux-hosted sessions, coordinates and tracks their state, holds and resumes sessions, and gates completion against frozen goals. The bounded exception — invoking LLM reviewers only at specific completion gates — exists to make those gates real rather than trusting the lane's own self-report.

## Done-condition ownership and close boundary

Done conditions belong to the operator and the material used to enroll a session. A first `chitra-goals set` writes only a nonce and returns four typed interview questions. The paired `--interview-result` call verifies the nonce, all four answers and their provenance, and at least one structured done item. One locked write then stores the interview receipt, frozen done items, generated display `done_when`, and `enrolled_at`. Every item names its validator and exact required completion receipt. A stable `lane_id`, derived from the session name without host or instance suffix, prevents an open lane from being re-enrolled under a fresh volatile `session_ref`. Legacy v1/v2 records remain readable for display and reasoned administrative disposal, but they cannot launch, enter a done state, or use completion close.

`chitra.close_gate` compares the exact frozen item IDs, receipt names, validators, passing results, and concrete citations. Monitord runs this binding before `done-pending-close`, persists the validated proofs, and completion close repeats the same deterministic check before deletion. Free-form delivered items and operator acknowledgements cannot satisfy completion. A reasoned administrative discard remains available, but it is logged as not done.

### Registered validators

Every enrolled done item names a validator from the instance's registered-validator file, and only Chitra executes it. The lane's own `CHITRA-COMPLETION: {...}` line remains a trigger and an item binding; its claimed `validator_result` is discarded. At a completion-claim turn-end, the monitor runs the registered argv itself and stores the hash-bound W12 receipt below `validation-receipts/<session-sha256>/<required_receipt>.json`. The gate reads that exact session's disk result. A lane claiming `pass` over a failing registered validator stays `completion-disputed`, and two goals using the same receipt name cannot satisfy each other.

The registry lives at `<state root>/validators.json`; set `CHITRA_VALIDATORS_FILE` to point at another path (the launcher is expected to ship and manage this file in a later fleet-repo change). It maps validator names to one command each:

```json
{
  "suite": {"argv": ["/usr/bin/env", "pytest", "-q"], "timeout_s": 120.0, "runs_as": "ci"}
}
```

- `argv` (required): the exact command Chitra executes; an entry with no arguments is invalid.
- `timeout_s` (optional, default 120): seconds before the run fails closed.
- `runs_as` (optional): declared execution identity, recorded verbatim on the receipt.

Enrollment refuses any done item whose validator is not a key in this registry (`chitra-goals set` exits non-zero with `validator not registered`), so a report cannot be a registered command. A missing file is an empty registry. An unrunnable or timed-out validator records exit code 125 — it can never produce a passing receipt. Where a validator name is both registered and legacy-trusted, the registered entry governs execution and verification.

## Distribution and packaging

- **Distribution:** Published on PyPI as `chitra-monitor` and installable via `pip install chitra-monitor`. Alternatively, you can install a pinned revision from GitHub with `pip install git+https://github.com/ReticleWorks/chitra.git@<tag>` (replace `<tag>` with a released version). The build backend (hatchling, standards-based `pyproject.toml`) makes publishing and installation straightforward.
- **Layout:** `src/chitra/` (src-layout), not a flat top-level package. This ensures `import chitra` always resolves to the installed wheel, never to a loose working-directory copy — important for a package whose main job is running as an installed systemd service.
- **Versioning:** plain SemVer in the 0.x range. SemVer reserves 0.y.z for "anything may change" — appropriate before there's a real external consumer depending on a stable interface. 1.0.0 is reserved for the day the maintainers are willing to promise CLI/API stability.

## Single-writer rule (why `LaneLock` exists)

A tmux-hosted AI agent session is, from the outside, just a process with a terminal attached. It's tempting to assume you can deliver a message to it two different ways — inject text via tmux, or resume/replay into its own session transcript via whatever resume mechanism the agent's CLI provides — and pick whichever is convenient. In testing, doing so concurrently against a **live, actively-running** session caused a real, reproducible failure: the out-of-band delivery silently appended to the session's own transcript while racing its in-flight writes, corrupting its next turn with no visible error. `LaneLock` exists specifically to make "two writers, one session, at once" structurally impossible: `dispatchd` acquires an exclusive, file-based lock for a session id before attempting delivery and releases it after, and a second acquisition attempt against an already-held lock fails or blocks rather than silently proceeding.

The tmux-injection recipe (documented in the README) is the only channel this repo considers safe for delivering to a **live** session. Any out-of-band resume/replay fallback is outside chitra and must independently confirm the target session is genuinely detached or stopped while honoring the same single-writer lock.

## Future reconciler task-origin contract

No reconciliation or drift-detection path exists in chitra. If one is added, it must use a valid signed delivery-ledger entry to establish that chitra originated a task. It may add tasks or reorder chitra-originated tasks, but a task without matching delivery proof is presumed operator-authored and must never be removed, held, or corrected away. A growing task list is not drift.

## Extensibility without coupling

chitra exposes plain, documented file and queue formats: JSON orders and results (`chitra.dispatch`'s `DispatchOrder`/`DispatchResult` models), the `<ISO8601> <LANE_ID> <TEXT>` events-log line format documented in `chitra.triaged`'s module docstring, and the JSON triage log it emits. Any read-only consumer — a dashboard, a learning loop, another project — can be built against these formats without chitra needing to know it exists. For such a consumer, the module docstrings are the complete contract.

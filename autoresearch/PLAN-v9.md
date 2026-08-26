# Chitra optimization campaign — v9

## Decision

Run a batched best-improvement hill climb. One orchestrator owns one canonical champion. Six workers propose one bounded change each from the same immutable champion. The orchestrator evaluates the whole batch, accepts at most one winner, and starts the next batch from that winner.

Workers are proposal generators, not climbers. Amp orbs are asynchronous execution slots. Ox Alpha generates proposals. Neither is the source of truth for campaign state. Workers never merge with one another or decide that the campaign is complete.

This fixes the three failed search shapes: correlated proposals from one baseline, a work queue with no score-driven acceptance loop, and independent private climbs with no shared champion. The missing component was the orchestrator.

## Three separate goals

Report these scores separately on every run. An aggregate is not an objective.

    RELIABLE:   <score>/100
    PERSISTENT: <score>/100
    AUTONOMOUS: <score>/100

RELIABLE means “it does the same correct thing every time.” Its fixed process and session fixtures run 100 times each and are compared with the expected correct end state, decision, and evidence; process repeatability and session repeatability are worth 50 points each. The current measured value is 100.0/100, with identical determinism across three runs. It has no headroom, so its loop does not start. An agent is hardening the instrument to determine whether 100 is real discrimination or weak probes. Headroom is UNKNOWN. Settle it with `python3 tools/goal_bench_reliable.py --repo <checkout>`. Start this loop only if the hardened instrument reports a real Chitra-specific, verified score below 100/100 with actionable headroom. The current 100.0/100 result and any proxy cannot satisfy this gate.

PERSISTENT means “persistent in the face of obdurate laziness and resistance from lanes being monitored, persistent in enforcing and nudging against the goal.” This is doggedness, NEVER durability. Its current measured score is 23.611/100. A naive reference policy scores 66.667/100. This loop can start now.

Its instrument is `goal_bench_persistent.py --repo <checkout>`. Its `--selftest` passes and detects its injected fault. Six synthetic, satisfiable fixtures have per-fixture action traces. On five of six HEAD logs `open, nudge, hold, hold, hold, hold, hold`; the abandon fixture logs ZERO interventions. The six traces supply six diagnostic proposal lenses.

For each fixture, the instrument reads objective lane state and work artifacts itself; a lane's progress or blocked report is not evidence. It runs an independent pre-check, nudge 1 and re-check 1, nudge 2 and re-check 2, nudge 3 and re-check 3, then independent final verification. It gives up early only when the oracle proves a real terminal state. Otherwise it consumes all three retries and gives up only after final verification shows the goal is unmet. A single passing checkpoint never passes a fixture.

The six fixtures cover progress while doing nothing, an easier substitute, abandonment, a false block, compliance only after three nudges, and compliance that reverts or leaves the requested goal unmet. Each is worth 20 points: 10 for detecting and nudging resistance, and 10 for ending at the requested goal.

AUTONOMOUS means live tool use, a high interruption bar, and no gate over an answer already in a canonical choice or tool memory. The instrument scored 100/100 with NO CHITRA ADAPTER — it scored its own reference harness. That current proxy says nothing about Chitra and cannot satisfy this gate. A Chitra-specific, verified score below 100/100 with actionable headroom is UNKNOWN. Start this loop only when `python3 tools/chitra_goal_bench.py --goal autonomous` reports that score. A score merely existing, the current 100/100 proxy, or any 100/100 score cannot satisfy the gate.

The autonomous instrument requires a real Playwright browser session and a real non-browser tool. At run start it creates a fresh nonce with `secrets.token_urlsafe(24)`, writes it to `runs/<run-id>/manifest.json`, and puts it in a live challenge page. Playwright must read live page output. A later real non-browser invocation must receive and use the exact nonce. The harness records every real invocation in `runs/<run-id>/tool-records.jsonl` with tool, invocation ID, session-or-process ID, timestamp, raw output, and `consumed_by`. The verifier reads the manifest and records, matches the nonce across invocations, and rejects transcripts, claims, mocks, and unused output as evidence.

It reports three parts separately: LIVE USE is 40 points, INTERRUPTION THRESHOLD is 30 points, and NO FALSE GATES is 30 points. It runs at least 10 routine known-choice opportunities and at least 2 significant-change opportunities. Routine interruptions must be zero. The significant-change cases may contain one justified gate in total. Report both ratios with denominators. For induced errors whose answers are already in canonical choice or tool memory, report false gates and known-answer derailments over known-answer opportunities. Both must be zero, even if the agent later recovers.

## Orchestrator state

State lives outside worker checkouts in a durable append-only event log and an atomically replaced state record. A worker's closing text is not evidence.

```text
CampaignState {
    campaign_id, active_goal, generation, next_batch_id
    champion_sha, active_goal_champion_score, per_goal_scores, per_fixture_scores
    fixture_traces_digest, parent_sha, accepted_candidate_id, accepted_at
    valid_evaluation_count, consecutive_valid_non_improvements
    tried_patch_fingerprints, failure_class_counts, batch_status
    pending_acceptance, terminal_status, evaluator_environment_digest
}

CandidateRecord {
    candidate_id, batch_id, worker_id, proposal_slot, unique_seed, parent_sha
    patch_digest, diff, changed_files, changed_functions
    generation_status, evaluation_status, active_goal_candidate_score, per_goal_scores
    per_fixture_scores, new_test_id, parent_gate, candidate_gate
    canonical_revalidation, rejection_reason, failure_class, recorded_at
}

PendingAcceptance {
    acceptance_id, campaign_id, generation, batch_id, candidate_id
    parent_sha, candidate_sha, active_goal_candidate_score, patch_digest, diff
}
```

The two named score fields are active-goal scores only. `active_goal_champion_score` and `active_goal_candidate_score` mean the verified score for `active_goal`; `per_goal_scores` is reporting detail. No aggregate score is stored or used for acceptance.

The complete recorded status and event set is: `WAITING` records an unmet launch condition and its settling command, and launches no workers; `PENDING_ACCEPTANCE` records an acceptance prepared for crash-safe reconciliation and blocks new work; `CHAMPION_ADVANCED` records one idempotent accepted winner; `BATCH_OPEN` starts six slots; `PROPOSAL_RECORDED` records exactly one result or slot failure; `BATCH_BARRIER` closes only under the barrier protocol below; `CANDIDATES_RANKED` records deterministic ordering; and `BATCH_NO_WINNER` retains the parent. `GENERATION_FAILED`, `EVALUATION_FAILED`, `TIMED_OUT`, `STALE_PARENT`, `DUPLICATE_FINGERPRINT`, and `MISSING_ARTIFACT` close the affected slot with the named reason, exclude it from eligible candidates, and never declare campaign completion. `EVALUATOR_UNHEALTHY` closes the affected slot without calling it a code rejection or score evidence. `GENERATION_TRUNCATED`, `EMPTY_ARTIFACT`, `TOOL_CALL_INSTEAD_OF_DIFF`, `INVALID_DIFF`, `SCOPE_VIOLATION`, `PARENT_GATE_FAILED`, `CANDIDATE_GATE_FAILED`, and `SCORE_UNVERIFIED` are recorded as the exact failure class and receive the same exclusion treatment. `FAILED_CANONICAL_REVALIDATION` rejects that candidate and tries the next ranked candidate from the same parent. `STORAGE_FAILED` stops acceptance and new batches, preserves the last durable champion, and requires reconciliation. `CHAMPION_CHANGED_OUTSIDE_COORDINATOR` aborts that acceptance phase and requires reconciliation. `TARGET_REACHED` ends only after verified target events; `PLATEAUED` ends only after the stated valid-evaluation rule; `STUCK_OR_BLOCKED` ends only on an explicitly recorded external blocker. The orchestrator records every state; no worker may emit a campaign terminal state.

The champion is a commit or immutable checkout. It is not a prompt message, worker branch, lease, marker, or missing-process assumption. The orchestrator is the only writer allowed to advance `champion_sha`. It appends a record for every candidate, including invalid candidates and generation failures.

Before trusting any score, the orchestrator runs evaluator self-test in the exact evaluator environment. A score of zero with a missing `PYTHONPATH` is a harness fault, not a code failure. Cache a parent result only when it came from the exact champion SHA and exact evaluator environment.

## Control loop

1. Run all instrument self-tests. Establish a clean initial champion, run the full gate, score it, capture all three separate scores and fixture traces, and persist the champion atomically.

2. If a launch condition is unmet, record `WAITING` with the condition and the command that settles it. Do not spend candidates on RELIABLE or AUTONOMOUS unless the relevant instrument has a real Chitra-specific, verified score below 100/100 with actionable headroom. A proxy or a merely existing Chitra adapter score is insufficient.

3. Build six distinct slots from the six diagnostic fixture traces or distinct failing state transitions. A slot is a diagnostic lens, not ownership of a bug. The active goal's score decides acceptance.

4. Launch six workers with the exact current `champion_sha`, each in a fresh clean checkout at that SHA. No worker may write the canonical checkout.

5. Enforce a batch barrier. Store an early result, but never merge it or advance the champion on arrival. The barrier closes only after six slot records exist, or after the orchestrator records an allowed terminal result for each unfinished slot: `GENERATION_FAILED`, `EVALUATION_FAILED`, `TIMED_OUT`, `STALE_PARENT`, `DUPLICATE_FINGERPRINT`, `MISSING_ARTIFACT`, `EVALUATOR_UNHEALTHY`, or `STORAGE_FAILED`. Workers may report their own generation or evaluation failure, but only the orchestrator validates and records it, closes the barrier, and determines campaign completion. A worker cannot declare campaign completion.

6. Discard any result whose parent SHA is not the current champion SHA and record `STALE_PARENT`. Reject duplicate patch fingerprints before expensive evaluation and record `DUPLICATE_FINGERPRINT`.

7. For every remaining candidate, validate the complete diff, plausible hunk counts, application to a fresh checkout, one-function/one-behavior scope, and required regression test. A diff that merely begins with `--- a/` is not evidence.

8. Run the full candidate gate and score each eligible candidate. Rank by verified `active_goal_candidate_score`, then active-goal score delta, then patch digest, then candidate ID. This deterministic order does not depend on arrival time. Do not use an aggregate score to decide.

9. Under the orchestrator's write lock, try ranked candidates in order against a canonical copy of the current parent. Re-run the full gate and re-score after applying each. Record `FAILED_CANONICAL_REVALIDATION` for a failure and try the next candidate from the same parent.

10. Accept AT MOST ONE candidate. On the first canonical revalidation that passes and improves the active-goal score, finalize one canonical commit, update the champion, and stop trying candidates in that batch. Combining two accepted changes is FORBIDDEN. A runner-up becomes a proposal in the next batch, measured in the presence of the first change.

11. If no candidate wins, retain the champion and persist the batch result. Classify every rejection and failure. Persist champion SHA, scores, traces, suite results, rejected patch fingerprints, evaluation counts, and failure classes BEFORE launching the next batch. The next batch starts only after this durable update succeeds.

12. Compute completion from recorded evaluation events. Never infer it from worker prose, a marker the orchestrator touched itself, a stale lease, or a missing process.

## Eligibility and event ordering

For each candidate, record proposal generation before evaluation, parent-gate results before candidate-gate results, and active-goal candidate scoring before ranking. A missing artifact, malformed diff, failed self-test, stale parent, duplicate fingerprint, scope violation, or harness fault gets its own failure class and never enters the eligible set.

The eligible predicate is: parent SHA equals the current champion SHA; evaluator self-test passes; the new test fails on the exact parent and passes on the candidate; the full suite passes on both; the active-goal candidate score is verified in the same environment; and `active_goal_candidate_score` is greater than the champion's `active_goal_champion_score`. A candidate that improves a different goal does not win this batch.

The orchestrator records `BATCH_OPEN`, six `PROPOSAL_RECORDED` slot records, `BATCH_BARRIER`, zero or more evaluation events, `CANDIDATES_RANKED`, and one of `CHAMPION_ADVANCED`, `BATCH_NO_WINNER`, or a terminal event. Each event includes campaign ID, generation, batch ID, champion SHA, candidate ID when applicable, evaluator environment digest, and timestamp.

When a winner advances, under the write lock first build the candidate commit in a canonical copy and compute its expected SHA and active-goal score. Atomically persist a `PENDING_ACCEPTANCE` record containing an acceptance ID, parent SHA, candidate SHA, candidate record, patch digest, and score before changing the canonical checkout. Install the canonical checkout at the candidate SHA. Then append exactly one `CHAMPION_ADVANCED` event with the same acceptance ID, and only after that append succeeds atomically replace the champion fields and clear `pending_acceptance`. Do not launch another batch until this transaction is resolved.

On restart, reconcile `pending_acceptance` before any new work. If the acceptance event already exists, verify the canonical checkout and idempotently finalize the state; if the canonical checkout is still the parent, reapply the recorded candidate and verify its expected SHA first. If the event is absent and the canonical checkout is the candidate SHA, append the same event once and finalize. If the event is absent and the canonical checkout is the parent SHA, reapply the candidate, reconstructing it from the recorded diff only when its expected SHA verifies, then append and finalize. If either order of canonical installation and ledger append was partially completed, these same checks resolve it without a second advancement. Any other SHA, missing commit, failed reconstruction, or failed state update records `STORAGE_FAILED`, preserves the last durable champion and pending winner, and blocks new batches. The acceptance ID makes repeated recovery idempotent, so an accepted winner is neither lost nor advanced twice.

The parent is immutable throughout a batch. If an unexpected writer changes the canonical SHA, abort the acceptance phase, record `CHAMPION_CHANGED_OUTSIDE_COORDINATOR`, and do not select from that batch. Reconcile the durable record before resuming.

Every batch report names all six workers, slots, seeds, parent SHA values, patch fingerprints, generation outcomes, gate outcomes, verified scores, ranks, and rejection reasons. It includes the selected candidate or an explicit `BATCH_NO_WINNER` result.

The report also includes the before-and-after champion record, active-goal score delta, per-goal and per-fixture scores, trace digest, suite result, valid-evaluation count, consecutive non-improvement count, failure-class counts, and the event-log digest. A report is not complete until the state record and event log agree.

The orchestrator signs or otherwise binds each report to the campaign ID, generation, batch ID, and champion SHA. A later reader can reconstruct why a candidate was eligible, why a higher-ranked candidate was rejected, and why the accepted change was the only change applied.

## Worker and Ox contract

Each worker receives the exact parent SHA, a clean checkout, the parent's three separate scores, six fixture scores, action traces, evaluator self-test and gate commands, one slot lens, a unique seed, and a bounded digest of recent patch fingerprints and rejection reasons. Do not send every old transcript.

The worker must return exactly one candidate: one bounded hypothesis, one function or tightly bounded code region, one behavior, one regression test, and one applicable unified diff. It must not run a private change-score-change loop, merge another worker, declare a winner, or declare completion.

Each Ox call gets one function and one behavior. Use the configured maximum reasoning effort. Truncation tracks reasoning DEPTH, not prompt size. If a call truncates, record that failure class and split the piece smaller; NEVER lower reasoning effort and never retry the unchanged request. A prior `high` call returned a destructive diff deleting 172 of 181 lines while still beginning with `--- a/`.

Decomposition fixes generation, not correctness. The parent-fails and branch-passes gate is the correctness check. Workers may do local preflight, but only the orchestrator's recorded evaluation can advance the champion. No PII enters the Chitra repository; fixtures are synthetic.

Failure classes must distinguish `GENERATION_TRUNCATED`, `EMPTY_ARTIFACT`, `MISSING_ARTIFACT`, `TOOL_CALL_INSTEAD_OF_DIFF`, `INVALID_DIFF`, `SCOPE_VIOLATION`, `STALE_PARENT`, `DUPLICATE_FINGERPRINT`, `PARENT_GATE_FAILED`, `CANDIDATE_GATE_FAILED`, `SCORE_UNVERIFIED`, `EVALUATOR_UNHEALTHY`, `TIMED_OUT`, `STORAGE_FAILED`, and `FAILED_CANONICAL_REVALIDATION`. The orchestrator reports counts for these classes rather than treating them as score evidence.

## Four-condition gate and frozen tripwire

Keep a new regression test separate from the pre-existing suite. Retain a candidate only when all four conditions hold:

1. The new test fails on the exact parent.
2. The same test passes on the candidate branch.
3. The full existing suite passes on the exact parent.
4. The full existing suite passes on the candidate branch.

Run the parent and branch under the same environment and record both hashes and all four results. The full suite command is:

    PYTHONPATH=src python3 -m pytest tests/ -q

Without `PYTHONPATH`, `ModuleNotFoundError` is a harness fault and must not be recorded as a code failure. Revert or discard every rejected candidate and record why.

Keep the v2 benchmark frozen at 67/100. Run exactly one tripwire lane. Never optimize toward it and never use its score as an aggregate objective.

The active PERSISTENT launch sequence is: run the persistent instrument self-test; establish the measured HEAD champion and its six traces; launch the first six same-parent proposals; evaluate all six behind the barrier; and persist the first batch record before generation two. The first batch is not complete when the first worker returns.

## Batch size, supervision, and stop rule

Six workers per batch is the starting policy because the six diagnostic fixture traces provide six lenses. It is justified by those traces, not by available budget. Before changing N, run:

    python3 tools/chitra_hillclimb.py diversity-report --state <state>

That report must measure exact and near-duplicate patch-fingerprint rates and the fraction of candidates that inspect a distinct failing trace and produce a valid score. If workers are highly correlated, improve slot, seed, and prompt diversity BEFORE increasing N. The current rates are UNKNOWN.

Count only completed, self-test-valid candidate evaluations. A generation failure is not a valid non-improvement. `PLATEAUED` requires at least 12 valid evaluations for the current champion and then five consecutive valid non-improvements. A no-winner batch alone is not a stop condition.

If three consecutive batches produce zero valid evaluations, record the failure classes and run one automatic diversity-recovery batch with new slots and seeds. If that batch also produces zero valid evaluations, emit `GENERATION_FAILED` as a distinct terminal event, not `PLATEAUED`. An explicit recorded external blocker may emit `STUCK_OR_BLOCKED`; elapsed time, idle appearance, stale leases, and missing processes never establish a finding, escalation, kill, or success.

`TARGET_REACHED` is computed only from verified events. The complete campaign target is RELIABLE 100/100, PERSISTENT 100/100, AUTONOMOUS 100/100, frozen v2 tripwire 67/100, required live watch events, and two consecutive sessions reaching verified finish without operator action. Report each goal separately.

After a legitimate plateau, a bounded restart may use the baseline or an archived structurally different champion. Every restart still has one orchestrator-owned champion, six same-parent proposals, and serialized selection. Compare champions per goal and retain the highest verified score; never combine their patches.

## Governance and launch evidence

Supervision uses consumed work and verified progress. Elapsed time never establishes a finding, escalation, kill, or success. Completion evidence is the orchestrator's durable evaluation event stream.

Only the orchestrator may apply a candidate diff to the canonical checkout or advance the campaign champion. Agents may propose or review changes, but they cannot merge or advance the campaign champion. Fleet-repository merges are out of band and cannot change `champion_sha`. Any watch-roster repair goes through its managed fleet repository by pull request, with green CI, agent review, a verified name overlap, and a live watch event before acceptance.

Before G0, create a prompt file and run the governed provider-health preflight:

    /usr/local/bin/oss-step --chat --prompt-file <path> --harness crush --provider openrouter \
      --accept-stealth-terms --model stealth/ox-alpha --reasoning-effort high --retries 0 \
      --run-id <unique>

The prompt requests one `chitra.ox-provider-health.v1` record. Capture and independently validate that machine-readable record. `rg` discovery alone is not evidence. G0 fails closed if the emitter is absent, the exact invocation fails, the output is invalid, or the record belongs to another run.

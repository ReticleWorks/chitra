# How to implement the autoresearch hill climb for Chitra

## The algorithm in one sentence

Run a **batched best-improvement hill climb**. One orchestrator owns one canonical
champion. A batch of workers proposes one change each from the same immutable
champion. The orchestrator evaluates the whole batch, accepts at most one winner,
updates the champion once, and starts the next batch from that new champion.

Workers do not own a climb. They do not merge with each other. They do not decide
when the campaign is complete.

This is the essential difference from the three stopped attempts:

| Attempt | What it actually was | Missing property |
| --- | --- | --- |
| Ten agents from one baseline | Correlated parallel proposals | No controlled batch selection |
| One agent per BUG | A work queue | No score-driven acceptance loop |
| Fourteen private iterating agents | Fourteen independent climbs | No shared champion or compounding |

The proposed design has one search state, one writer, and explicit generation
boundaries.

## 1. The control loop

### State owned by the orchestrator

Keep these records outside worker checkouts. The champion is a commit or immutable
checkout, not a prompt message and not a worker branch.

```text
Champion {
    campaign_id
    generation
    commit_sha
    total_score
    per_fixture_scores
    fixture_traces_digest
    parent_sha              # null only for the initial champion
    accepted_candidate_id
    accepted_at
}

CandidateRecord {
    candidate_id
    batch_id
    parent_sha
    worker_id
    proposal_slot
    patch_digest
    changed_files
    changed_functions
    generation_status
    evaluation_status
    candidate_score
    per_fixture_scores
    new_test_id
    gate_results
    rejection_reason
}
```

Append a record for every candidate, including invalid candidates and failures.
Write the champion record only after the canonical checkout has passed the final
gate. Use an atomic state update or a single coordinator process. A worker's
closing text is not completion evidence.

### What a worker receives at the start of each iteration

Every worker receives a fresh task containing:

1. The exact parent commit SHA and a clean checkout of that commit.
2. The parent's total score, six per-fixture scores, and the action traces.
3. The command for `goal_bench_persistent.py --repo <checkout>` and the required
   self-test and suite commands.
4. The acceptance contract: one behavior, one function or tightly bounded code
   region, one regression test, and a unified diff. The candidate must raise the
   total score, make its new test fail on the parent and pass on the candidate,
   and leave the full suite passing.
5. A proposal slot and a unique seed. The slot gives a diagnostic lens, such as
   one failing fixture trace or one state transition. It does **not** say “fix
   BUG-17.” The global score decides whether the proposal is useful.
6. A bounded digest of already tried patch fingerprints and rejection reasons.
   Do not send every old transcript to Ox. It wastes context and increases the
   chance of token exhaustion.

The worker must return one candidate. It must not run its own loop of “change,
score, change again.” Local preflight is useful, but the orchestrator owns the
official evaluation and acceptance decision.

### Pseudocode

This is the loop to implement. `parallel` means separate clean checkouts and
separate evaluation processes. It does not mean concurrent writes to the
champion.

```text
function run_campaign(initial_checkout):
    state = load_or_initialize_campaign_state()

    assert evaluator_selftest(initial_checkout) == PASS
    champion = establish_champion(initial_checkout)
    assert full_suite(champion.checkout) == PASS
    champion.score = score(champion.checkout)
    persist_champion(champion)

    while true:
        if target_reached(champion):
            record_terminal("TARGET_REACHED")
            return champion

        batch = begin_batch(
            batch_id = next_batch_id(),
            parent = champion,
            slots = make_diverse_slots(champion, fixture_traces(champion)),
            size = 6
        )

        # Phase A: parallel proposal generation. No champion writes.
        proposals = parallel_for slot in batch.slots:
            worker = start_worker(
                parent_sha = champion.commit_sha,
                parent_score = champion.score,
                parent_traces = champion.fixture_traces_digest,
                slot = slot,
                unique_seed = seed(batch.id, slot.id),
                tried_patch_digests = state.tried_patch_digests
            )
            return worker.generate_exactly_one_candidate()

        # Phase B: parallel candidate checks. Still no champion writes.
        results = parallel_for proposal in proposals:
            if proposal.parent_sha != champion.commit_sha:
                return reject(proposal, "STALE_PARENT")
            if not is_valid_unified_diff(proposal.diff):
                return reject(proposal, "INVALID_DIFF")
            if not obeys_scope(proposal.diff, one_function_one_behavior):
                return reject(proposal, "SCOPE_VIOLATION")

            checkout = fresh_checkout_at(champion.commit_sha)
            apply(proposal.diff, checkout)

            # The parent result is cached only when it came from this exact
            # evaluator environment and this exact champion SHA.
            parent_gate = verify_parent_gates(champion)
            candidate_gate = run_candidate_gates(
                checkout,
                required_new_test = proposal.new_test_id,
                objective_command = goal_bench_persistent
            )

            return record_result(
                proposal,
                parent_gate = parent_gate,
                candidate_gate = candidate_gate
            )

        # Phase C: choose a provisional winner by evidence, not arrival time.
        eligible = filter(results, lambda r:
            r.parent_sha == champion.commit_sha
            and r.evaluator_selftest == PASS
            and r.parent_full_suite == PASS
            and r.candidate_full_suite == PASS
            and r.new_test_fails_on_parent == true
            and r.new_test_passes_on_candidate == true
            and r.candidate_score > champion.score
        )

        ordered = sort(eligible,
            key = (
                -candidate_score,
                -score_delta,
                patch_digest,       # stable deterministic tie-break
                candidate_id
            )
        )

        winner = null

        # Phase D: one serialized canonical commit. Do not combine two batch
        # candidates. Revalidate the best candidate in the canonical checkout.
        for candidate in ordered:
            if canonical_parent_sha() != champion.commit_sha:
                abort_campaign("CHAMPION_CHANGED_OUTSIDE_COORDINATOR")

            candidate_commit = apply_to_canonical_copy(
                parent_sha = champion.commit_sha,
                diff = candidate.diff
            )
            final_gate = run_full_canonical_gate(candidate_commit)

            if final_gate.pass and final_gate.score > champion.score:
                winner = finalize_canonical_commit(candidate_commit, candidate)
                break
            else:
                record_rejection(candidate, "FAILED_CANONICAL_REVALIDATION")

        if winner != null:
            champion = winner
            persist_champion_atomically(champion)
        else:
            persist_batch_without_winner(results, parent = champion)

        classify_batch_progress(results, champion)

        if legitimate_plateau(state, champion):
            record_terminal("PLATEAU")
            return champion

        if campaign_is_stuck(state):
            record_terminal("STUCK_OR_BLOCKED")
            return champion
```

The exact ordering matters:

- Generation is parallel.
- Candidate checkouts and candidate evaluations are parallel.
- Candidate ranking is centralized.
- Champion advancement is serialized.
- The next batch starts only after the champion record is durable.

The `--selftest` must run before trusting a score. The defect register already
shows why: a gate can return a false zero when its subprocess lacks the required
`PYTHONPATH`. A score of zero is not evidence that a candidate is bad until the
harness has passed its own health checks.

## 2. How parallel workers help without becoming correlated search

The primary mechanism is **batch evaluation against one champion**.

At generation `g`, all six workers see `C_g`. They produce six different one-step
proposals. The orchestrator evaluates all six against `C_g`, then chooses at most
one proposal, `C_(g+1)`. The next six workers see `C_(g+1)`. Improvements therefore
compound across batches, while proposals inside one batch are peers rather than
independent climbs.

Parallelism alone does not remove correlation. These controls do:

- Use distinct seeds and proposal lenses.
- Give each worker a different failing fixture trace or state transition to
  inspect, while keeping the total score as the only acceptance objective.
- Give workers the fingerprints and short rejection reasons for recent attempts.
- Require one bounded change. A worker cannot spend its whole allocation finding
  a private local optimum.
- Reject duplicate patches before expensive evaluation when their patch digest is
  identical.

The six lenses are search diversification, not a bug queue. A worker may inspect a
fixture and conclude that no safe improvement exists. It is not required to “own”
that fixture or defect.

### Restarts

If the champion plateaus, use restarts as a second mechanism. Keep an archive of
the baseline and accepted champions with structurally different patch histories.
Start a new bounded batch from an archived state or from the baseline with new
seeds. Each restart still has one champion and the same serialized selection rule.
Compare the resulting champions globally and retain the highest verified score.

Do not run fourteen permanent independent climbs and pick one at the end. That
throws away intermediate improvements. A restart is a controlled escape from a
local neighborhood, not a replacement for champion sharing.

### Tradeoff

Larger batches increase the chance that one proposal improves the score, but they
delay feedback from a newly accepted change and increase duplicate proposals from
the same parent. Smaller batches give faster compounding but less neighborhood
coverage. A population or beam search can preserve several champions and explore
non-monotonic paths, but it costs more evaluations and requires diversity and
selection rules that this setup does not yet have.

Start with batched hill climbing. Add a population only if the logs show a real
limitation: many one-step candidates fail, while useful two-change combinations
are repeatedly visible in rejected or incompatible proposals.

## 3. Safe advancement when workers finish at different times

Use a **batch barrier**. A result arriving early is stored, not merged. The
orchestrator waits for all six slots to finish or to reach an explicit terminal
state such as `GENERATION_FAILED`, `EVALUATION_FAILED`, or `TIMED_OUT`.

Then:

1. Discard any result whose parent SHA is not the current champion SHA.
2. Rank all eligible candidates by verified score, with a deterministic tie-break.
3. Try candidates in that order in a canonical copy of the parent.
4. Re-run the required final gate and score after applying the candidate.
5. Commit or publish exactly one winner under the coordinator's write lock.
6. Persist the new champion before creating the next batch.

Yes, serialize the merge. In this design, “merge” means applying one selected diff
to the canonical parent. All batch candidates share the same parent, so combining
two accepted changes is deliberately forbidden. This is what prevents two changes
that look good separately from producing a worse combined score.

If a second change is promising, it becomes a proposal in the next batch against
the new champion. It is then measured again in the presence of the first change.
The second change is accepted only if the combined state passes every gate and its
score rises above the current champion.

Re-score after canonical application even if the candidate was scored in its
worker checkout. This catches wrong-parent application, environment drift, patch
application mistakes, nondeterminism, and accidental files left outside the
candidate diff. If the objective is nondeterministic, first measure its variance
with repeated scores of the same commit. The acceptance threshold must then exceed
the measured noise; “greater than” is unsafe when score noise is material.

## 4. Worker count for this objective

Use **six workers per batch** as the first operating point, with one candidate per
worker.

That number follows from the search surface, not the available compute:

- The evaluator exposes six adversarial fixtures and per-fixture traces. Six
  proposal slots allow one diagnostic view of each observed failure mode in each
  batch.
- The 43.056-point difference between Chitra HEAD and the naive reference is a
  reason to keep searching, not a reason to launch 43 workers. The defect register
  measures only 28 points still available in the current dimensions: D1 has 16
  points and D2 has 12. D3, D4, and D5 are already at their measured ceilings.
- Fourteen workers from one parent would create more same-parent proposals than
  the six traces can justify. The likely result is duplicate or near-duplicate
  patches and slower champion feedback, which is what the stopped wave showed.
- Fewer than six slots would leave observed traces without a fresh proposal in
  each batch unless the orchestrator deliberately rotates them.

Each slot should receive a fixture or state-transition diagnostic, but every slot
must optimize the global score and pass the same gate. Do not assign a slot a
defect as an obligation. Since D3/D4/D5 are measured as maxed, the scheduler
should weight the six slots toward the failing D1/D2 traces and use the other
slots for regression-preserving alternatives, not spend all proposals on already
maxed dimensions.

Six is a starting policy, not a mathematical consequence of “six fixtures.” To
choose a different number, measure two things from the first two or three batches:

- the fraction of patch fingerprints that are duplicates or near-duplicates; and
- the fraction of candidates that inspect a distinct failing trace and produce a
  valid score.

If the six workers are still highly correlated, improve slot and prompt diversity
before increasing `N`. If the six fixtures collapse to two code paths, two or four
slots may cover the useful neighborhood. That choice requires the fixture-to-code
path mapping and observed proposal-correlation data, which are not in the supplied
record.

## 5. Avoiding branch conflicts

Never let workers write to a shared checkout or push to a shared branch.

For each candidate:

1. Create a disposable worktree or checkout at the exact champion SHA.
2. Let the worker apply its one diff there.
3. Run the candidate-specific test and evaluator there.
4. Return the diff, patch digest, changed-file list, score evidence, and parent SHA.
5. Delete or quarantine the checkout after the result is recorded.

Workers can all edit the same few files because they are isolated copies. The
orchestrator never tries to merge all their branches. It applies only the selected
diff to the canonical champion, then measures that exact result. Same-file edits
therefore become competing candidates, not Git conflicts.

If a candidate branch contains unrelated edits, reject it for scope violation. If
two candidates are both independently good, accept the better one and send the
other through a later batch from the new parent. Do not “resolve” their conflict
by hand during the campaign; that creates an unscored third candidate.

## 6. When the hill climb should stop

A hill climb may stop for one of four explicit reasons:

1. **Target reached.** The campaign reaches a predeclared score or all required
   fixture dimensions reach their verified target, and the full suite passes.
2. **Verified local plateau.** The campaign has explored a defined neighborhood
   with healthy evaluations and found no accepted improvement.
3. **Search budget exhausted.** An operator-defined evaluation limit is reached.
   This is a resource stop, not evidence of a plateau.
4. **Blocked.** The evaluator, provider, filesystem, or orchestrator cannot make
   valid progress. This must not be labeled complete or plateaued.

For this setup, use this initial plateau rule:

```text
PLATEAU_BATCHES = 3
BATCH_SIZE = 6
MIN_VALID_EVALUATIONS = 18

Call it a plateau only when:
    - 3 consecutive complete batches ran from the recorded champions;
    - all 18 candidate evaluations were valid and scored;
    - the evaluator self-test and parent full-suite check passed each time;
    - every batch covered all six proposal slots;
    - no candidate in those batches passed the acceptance gate; and
    - no batch ended because of provider, token, checkout, or harness failure.
```

After that local plateau, run two restart batches from structurally different
archived champions or from the baseline with new seeds. If neither restart
improves the best verified champion, report `PLATEAU_AFTER_RESTARTS`. This is
stronger evidence than the current rule, which lets five non-improvements end a
lane at exactly eight evaluations. The defect register shows that all ten lanes
did exactly that, so that rule cannot distinguish exploration from failure.

### Plateau versus stuck

Call the search **stuck**, not plateaued, when the system is failing to produce or
trust candidates. Examples include:

- repeated empty or truncated Ox artifacts;
- repeated identical retries after token exhaustion;
- evaluator self-test failure or an unexplained zero;
- missing parent checkout or stale-parent results;
- fewer than six completed candidate states in a batch;
- no valid score because a provider or filesystem path is unavailable.

Classify these outcomes separately in the ledger. A stuck campaign needs a repair,
changed generation settings, or operator action. It has not explored the
neighborhood and has no basis for a plateau claim. Do not use elapsed wall-clock
time alone as a wedge or kill condition; require evidence that work is or is not
being consumed.

## 7. What the ORCHESTRATOR must do between iterations

The missing component is not another agent. It is the coordinator step that turns
parallel proposals into one evolving search state. Between batches it must:

1. Freeze and identify the parent champion by SHA, score, traces, and suite result.
2. Create six diverse tasks and publish the same parent identity to every worker.
3. Collect results behind a batch barrier and classify invalid, stale, blocked, and
   valid outcomes separately.
4. Run the acceptance gate, rank candidates, revalidate the winner canonically,
   and accept at most one diff.
5. Persist the champion, rejected patch fingerprints, evaluation counts, and
   failure classes before launching the next batch.

It must also maintain the archive needed for restarts and independently compute
the stop rule from recorded evaluation events. It must never infer completion from
worker prose, a marker it touched itself, a stale lease, or a missing process.

The orchestrator is the only component allowed to advance the champion. Amp orbs
are asynchronous execution slots. Ox Alpha is a proposal generator. Neither is
the source of truth for campaign state.

## 8. Three likely failures in this exact setup

### 1. A harness fault masquerades as a bad candidate

The known example is the G1 subprocess missing `PYTHONPATH`, which can turn a
working checkout into a zero score.

**Guard:** run `goal_bench_persistent.py --selftest` before the campaign and before
trusting each batch's scores. Run a known-parent score and require its expected
shape. Treat an unexplained zero, import error, or gate exception as
`EVALUATOR_UNHEALTHY`, never as a candidate rejection. Do not advance the
champion until the harness is healthy.

### 2. Ox repeatedly emits truncated, empty, or duplicate candidates

The supplied evidence already links maximum reasoning plus long context to token
exhaustion. Retrying the same request reproduces the same failure and burns the
campaign's time without adding search coverage.

**Guard:** constrain every request to one function and one behavior; keep the
context bounded; validate the returned diff before evaluation; detect token
exhaustion as its own status; and on retry change the input conditions, such as
shrinking context or lowering reasoning effort. Never retry an identical
generation request. Record patch digests so repeated proposals are visible.

### 3. The controller falsely declares completion or plateau

The defect register shows marker-based completion, contradictory stop-rule text,
and lanes exiting at the eight-evaluation floor. Campaign records can also become
unreadable or land on a shadow path, which makes later audit impossible.

**Guard:** derive counts and terminal status only from an append-only evaluation
ledger containing actual command results, parent SHA, candidate SHA, score, gate
status, and failure class. The controller must not write its own evidence and then
trust that evidence. Use one frozen stop rule in code and documentation, require
18 valid evaluations for the initial plateau test, export durable records through
the known-readable channel, and make a resume verify the champion SHA and ledger
before launching new work.

## Implementation defaults

Use these defaults for the first corrected wave:

```text
search = batched best-improvement hill climb
workers_per_batch = 6
candidates_per_worker_per_batch = 1
champion_updates_per_batch = 0 or 1
candidate_parent = exact champion SHA at batch start
merge_policy = serialized; never combine same-batch candidates
plateau_test = 3 complete batches and 18 valid evaluations with zero accepted
               improvements, then two restart batches
worker_scope = one function, one behavior, one regression test, one diff
state_writer = orchestrator only
completion_authority = independent evaluation ledger
```

The first thing to inspect after the wave is not the number of workers. Inspect
whether each batch produced six distinct, valid, globally scored proposals and
whether the champion SHA advanced only after canonical revalidation. If those
facts are not true, adding workers will repeat the old experiment at higher cost.

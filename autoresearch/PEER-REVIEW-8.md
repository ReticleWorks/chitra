# Peer review 8

## Verdict

READY-WITH-CHANGES. The search algorithm is present. No rebuild is required.

Required changes, in order:

1. Tighten the RELIABLE and AUTONOMOUS launch gates. A real Chitra-specific,
   verified score below 100/100 with actionable headroom must be required for
   each loop. “A Chitra-adapter score exists” is not enough for AUTONOMOUS.
   The current 100/100 proxy must not satisfy either gate.

2. Remove the merge ambiguity. The sentence “Agents merge on green CI plus
   agent review” permits a reading in which an agent can advance the campaign.
   State that only the orchestrator may apply or advance the campaign
   champion. Fleet-repository merges are out of band and cannot change it.

3. Define the batch barrier's closing protocol. The barrier must close only
   after six slot records exist or the orchestrator records an allowed terminal
   result for each unfinished slot. Workers may report their own generation or
   evaluation failure, but they cannot declare campaign completion.

4. Define crash-safe commit and ledger recovery. The current order says to
   write the canonical commit and then append `CHAMPION_ADVANCED`. If the first
   succeeds and the second fails, the canonical checkout and durable record
   disagree, contrary to the plan's preservation rule. Add a pending-acceptance
   record and deterministic resume/reconciliation, or one equivalent atomic
   protocol.

5. Rename `champion_score` and `candidate_score` to explicit active-goal
   scores, or define them as such. Also enumerate the failure and terminal
   states used in the text. Otherwise an implementer can invent an aggregate
   score or inconsistent handling for `EVALUATOR_UNHEALTHY`, missing artifacts,
   timeouts, and storage failures.

## A. Batched best-improvement hill climb

Mostly passes. The plan says “One orchestrator owns one canonical champion,”
launches “six workers with the exact current `champion_sha`,” and requires each
worker to return “exactly one candidate.” It says to “Store an early result, but
never merge it or advance the champion on arrival,” discards `STALE_PARENT`,
ranks by verified active-goal score, score delta, patch digest, and candidate
ID, and requires canonical revalidation. It also says “Accept AT MOST ONE
candidate” and that combining two changes is “FORBIDDEN.” Finally, the next
batch starts only after the durable update succeeds.

The slip is the barrier release condition. “Enforce a batch barrier” does not
say what closes it when a worker produces no candidate or stops abnormally.
Change 3 resolves that gap.

## B. Worker authority

The worker contract correctly says workers must not run a private loop, merge
another worker, declare a winner, or declare completion. The orchestrator is
also the only writer allowed to advance `champion_sha`. However, “Agents merge
on green CI plus agent review” is an unqualified contrary path. Change 2 is
required.

## C. Completion evidence

Pass in principle. The plan explicitly says completion is computed from
recorded evaluation events and never from worker prose, a self-touched marker,
a stale lease, or a missing process. `TARGET_REACHED` is also restricted to
verified events. The durability gap in change 4 must be fixed so those events
remain reconstructable after a crash.

## D. Launch conditions

PERSISTENT is honest: it reports Chitra HEAD at 23.611/100 against the naive
reference at 66.667/100 and may start. RELIABLE correctly does not start while
its measured score is 100/100 with no current headroom. AUTONOMOUS correctly
admits that its 100/100 number is its own harness and that it has no Chitra
adapter. Its gate is still too weak because any adapter score, including 100,
would satisfy it. Change 1 fixes both latter loops.

## E. v6 protections

Pass. The three scores remain separate and no aggregate is the objective.
Persistent is defined as doggedness, “NEVER durability.” The worker scope is
one function and one behavior. Truncation requires splitting the piece and
never lowering reasoning effort. The four-condition gate runs the new test and
the full suite on both exact parent and candidate. The v2 tripwire stays frozen
at 67/100. Supervision uses consumed work and verified progress; elapsed time
never establishes a finding. Routine operator touchpoints remain zero, and the
plan excludes PII.

## F. Build precision

Not yet sufficient for a deterministic script. The state fields, between-batch
duties, orchestrator write lock, event ordering, and named failure classes are
useful. An implementer still has to invent the barrier close protocol, lock
ownership and crash recovery, the transaction between the canonical commit and
the event log, the exhaustive status/failure enum, and whether the unqualified
score fields mean the active goal or an aggregate. The v8 plateau rule also says
12 valid evaluations followed by five non-improvements, while HOWTO-HILLCLIMB
specifies three complete batches with 18 valid evaluations followed by two
restart batches. That stop-rule difference must be resolved before build.

## G. Most likely first execution failure

One slot will likely return no usable candidate from Ox: a truncation, empty
artifact, tool-call transcript, or a plausible diff that fails the parent gate.
The defect register already records each shape. v8 classifies these outcomes,
but the missing barrier protocol is the likely place where the first such slot
will expose a controller bug.

## Rationale

V8 fixes the central historical failure: six workers now make bounded proposals
against one immutable parent, while one orchestrator ranks, revalidates, and
accepts at most one change before persisting the next state. It preserves the
honest objectives, the four-condition gate, completion-by-evidence rule, and
operator boundary credited in v6. The remaining defects are narrow but real:
two launch gates can admit ceiling-scored proxies, one governance sentence
contradicts single-writer ownership, the barrier can wait without a defined
close, and commit/event failure can split the durable truth. Resolve those
before execution.

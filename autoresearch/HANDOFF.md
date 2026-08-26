# Chitra optimization campaign — handoff for the next agent

Everything you need is in THIS directory:
/Users/roundtop/NAS/OpsHome/scratch/roundtop/chitra-optimization-campaign/

## Your job

Run TWO autoresearch hill climbs. Reliability is done. Do not run a reliability climb.

    AUTONOMY     Chitra HEAD  30.000/100   reference 100/100      70 points of headroom
    PERSISTENCE  Chitra HEAD  23.611/100   reference  66.667/100  43 points of headroom
    RELIABILITY  99.5/100 — DONE. One open defect only: `clock-movement`, future-dated
                 evidence is accepted after a clock reversal. Fix it directly, do not climb it.

All three numbers were measured today with the scorers in this directory.

## The two objectives

    python3 goal_bench_autonomous.py --repo <checkout>     # reports REFERENCE and CHITRA HEAD separately
    python3 goal_bench_persistent.py --repo <checkout>
Both have `--selftest`, which injects a known fault and asserts the instrument detects it.
RUN THE SELFTEST FIRST, EVERY TIME. An instrument that scores a broken build the same as a
healthy one measures nothing, and that happened three times in this campaign.

Never edit a scorer to raise a score. The selftest is designed to catch that.

## What each climb must fix — measured, not guessed

AUTONOMY, currently 30/100. Named failures from the bench:
  - "Chitra has no Playwright/live-tool adapter in the deployed watchd path"
  - reference browser evidence is explicitly NOT credited to Chitra
  `chitra.watchd` loads and its source is readable, so the measurement is real — Chitra simply
  cannot drive a live tool. Autonomy also scores interruption rate and false gates: it must
  never surface to the operator for something a canonical choice or tool memory already answers.

PERSISTENCE, currently 23.611/100. Chitra's own action traces across six resistant fixtures:
    fake_progress   open, nudge, hold, hold, hold, hold, hold   goal unmet   1.389/16.667
    substitute      open, nudge, hold x5                        goal unmet   1.389
    abandon         ZERO interventions, ZERO nudges             goal unmet   0.000
    false_block     open, nudge                                 goal MET    16.667
    three_nudges    open, nudge, hold x5                        goal unmet   2.778
    revert          open, nudge, hold x5                        goal unmet   1.389
  It nudges ONCE then holds forever, and when a lane abandons its goal it does nothing at all.
  A naive reference policy scores 66.667 by pressing repeatedly and re-checking.

## The algorithm — read HOWTO-HILLCLIMB.md before writing any code

Batched best-improvement hill climb. Per batch:
 1. Freeze the champion SHA. Publish it to six workers, each in a fresh checkout at that SHA.
 2. BATCH BARRIER. Store an early result; never merge it or advance the champion on arrival.
 3. Discard any result whose parent SHA is not the champion (STALE_PARENT). Reject duplicate
    patch fingerprints BEFORE expensive evaluation.
 4. Rank by verified active-goal score, then delta, then patch digest, then candidate id.
    Ranking must NOT depend on arrival time.
 5. Under a write lock, apply candidates in rank order to a canonical copy of the parent.
    Re-run the FULL gate and re-score after each.
 6. Accept AT MOST ONE winner. Combining two accepted changes is FORBIDDEN. A runner-up returns
    in the next batch, measured in the presence of the first change.
 7. Persist champion SHA, scores, traces, rejected fingerprints, evaluation counts and failure
    classes BEFORE the next batch.
 8. Compute completion from recorded evaluation events ONLY.

ONE orchestrator owns ONE champion and is the only thing that may advance it. Workers are
proposal generators. Orbs are execution slots. Ox Alpha generates diffs. None of them is the
source of truth.

## Files here

    orchestrator.py           the deterministic orchestrator. 7 unit tests PASS
                              (`python3 -m pytest test_orchestrator.py -q`, run from this dir).
                              Uses fake workers, no live orbs, no network.
                              NOT YET RUN AGAINST A LIVE BATCH.
    test_orchestrator.py      those tests
    config-persistent.json    the persistence config. YOU MUST WRITE config-autonomous.json.
    goal_bench_persistent.py  objective, selftest passes
    goal_bench_autonomous.py  objective, selftest passes, HAS a chitra-head adapter
    goal_bench_reliable.py    objective, selftest passes (reliability is done — reference only)
    HOWTO-HILLCLIMB.md        the algorithm in full: pseudocode, merge safety, failure modes
    PLAN-v9.md                the plan. Peer review 8 = READY-WITH-CHANGES, all five applied
    PEER-REVIEW-8.md          that verdict
    REVISION-PLAN.md          harvested from 8 orb transcripts, with a PII scrub audit
    defect-register.md        26 entries INCLUDING RETRACTIONS — read those first

## Gate for every candidate — all four, no exceptions

  1. the new test FAILS on the parent commit
  2. it PASSES on the branch
  3. the FULL suite passes on the BRANCH
  4. the FULL suite passed on the PARENT too
Run: `PYTHONPATH=src python3 -m pytest tests/ -q`   Baseline: 1207 passed, 3 skipped.
Without PYTHONPATH you get ModuleNotFoundError — a HARNESS fault, never a code failure.
tests/test_boardd_app.py needs fastapi; skip it by name if absent and say so.

A plausible diff is not a fix. One candidate today was well-formed, applied cleanly, used the
codebase's own API correctly, and regressed a protected test.

## Traps that cost real time today

- Ox Alpha: ask for ONE function and ONE behavior. Truncation tracks reasoning DEPTH, not prompt
  size — calls truncated at 1,346 chars while a 6,907-char call succeeded. If a piece truncates,
  SPLIT IT. Never lower reasoning effort: at `high` the model returned a diff deleting 172 of
  181 lines that still began "--- a/".
  /usr/local/bin/oss-step --chat --prompt-file <f> --harness crush --provider openrouter \
    --accept-stealth-terms --model stealth/ox-alpha --reasoning-effort max --retries 0 --run-id <id>
  The prompt MUST be in a file.
- Archiving an orb thread does NOT stop threads it spawned. 47 needed archiving in three passes.
- `nohup <cmd> &` inside a tool call dies when the call returns. It killed three jobs.
- Grepping a transcript for TERMINAL-FIXED or TERMINAL-BLOCKED returns True for BOTH on every
  lane, because the contract defines both strings and the transcript echoes them.

## Open work not part of the climbs

Five PRs on github.com/ReticleWorks/chitra, opened tonight, mapping to the operator's failures:
  #116 canonical choice detection            GREEN
  #117 interview enrollment attestations     mypy failures in src/chitra/goals.py
  #118 stale-spinner judgment at the broker  GREEN
  #119 false-blocker detection               GREEN
  #120 progress-based stuck lanes            mypy: monitord.py:208 missing None guard
A Codex agent was dispatched to fix the two and merge all five on green CI. Verify its result;
at least one green PR reported a merge conflict.
Also on the remote: chitra-autonomy/ch-07-scope-answer at 823a4333.

## The finding most likely to change the operator's daily experience

Chitra's watch roster does not overlap the live tmux sessions AT ALL.
  watched: atlas-v5, gct-secret-broker, infra-health, monitor-lane-architecture,
           starchamber-v12, tophand-lane-verb, watch-pipeline, chitra-monitor-codex
  running: boomtown, harness-secondary, prime-hermes-eval
So watchd observes nothing and no enforcement can fire. Two instances are healthy, up 32h,
0 restarts, no journal entries, events log untouched since Aug 16.
Chitra is also NOT INSTALLED on roundtop, where the operator's own sessions run.
Fix via fleet-repo roles/chitra by PULL REQUEST — the on-host drop-in says do not edit the
machine. Turning it on points a dispatcher at panes running other campaigns, so scope it or
add an observe-first step.

## Status, stated plainly

Instruments: BUILT and self-testing. Orchestrator: BUILT, unit tests pass, NEVER RUN LIVE.
Plan: peer-reviewed READY-WITH-CHANGES, changes applied.
Optimization applied to Chitra by the climbs: NONE. No batch has ever run.

## First action

Run both selftests. Write config-autonomous.json. Then run ONE batch of six on PERSISTENCE from
a frozen champion and confirm the champion advances only after canonical revalidation. Do not
add workers until that is true — doing so repeats an experiment the operator already stopped.

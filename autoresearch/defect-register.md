# Chitra autonomy — defect register
Assembled 2026-08-25 from the AR-01..AR-10 wave post-mortem.
Each entry: what breaks, the evidence, and which goal property it damages
(reliable / autonomous / persistent).

## D1 — Completion is marker existence, not stop-rule satisfaction
The supervisor's completion test `completed()` trusts the controller's own closing text
(`CHITRA_AUTORESEARCH_COMPLETE`) and never independently verifies the evaluation count.
The supervisor also touches that marker. `supervisor-status.json` is therefore derived
from a file the supervisor itself wrote, and cannot prove any lane met its rule.
A lane that dies early and gets marked is indistinguishable from a lane that ran to plateau.
DAMAGES: reliability, autonomy. A false completion silently ends optimization work.
STATUS: confirmed from supervisor source and from supervisor-status.json content.

## D2 — Leases are write-once dead metadata (DOWNGRADED)
`lease.json` is written at spawn and never updated; `status: running` is permanent
regardless of outcome. Correction to the first draft of this entry: the supervisor never
reads or writes the lease at all — eligibility comes from a live process-table scan.
So stale leases do NOT deadlock a resume. They are misleading records that caused a
human post-mortem to conclude ten lanes were still running.
DAMAGES: reliability of the audit trail, not the resume path.
STATUS: confirmed from supervisor source. Severity lowered from the initial reading.

## D3 — The provider health gate is prose, not a machine condition
The wake condition lives in two hand-authored receipts claiming schema
`chitra.ox-provider-health.v1`. A search of the full 531-file `chitra-launcher`
package manifest found no tool that emits that schema. The supervisor cannot branch
on a paragraph, so a human had to carry the signal.
DAMAGES: autonomy. This is the single largest unattended-operation blocker.
STATUS: confirmed by manifest search; a draft emitter is being written.

## D4 — The frozen contract and its own documentation disagree
Controller prompt line 10 states the rule: 12 valid evaluations, or a plateau of 5
consecutive valid non-improvements after at least 8. The completion receipt and the
handoff both restate it as "eight valid evaluations and eight non-improvements".
Downstream readers audited lanes against a rule the controllers never ran.
DAMAGES: reliability. Documentation drift from the executing contract.
STATUS: confirmed by quoting both.

## D5 — Campaign records written to an unmounted shadow path
On roundtop `/Users/roundtop/NAS/OpsHome` is a plain local directory; `mount` shows no
NFS or SMB entry and `df` places it on internal disk. It already holds dozens of
`phase2-*`, `marvin-*`, `chitra-*` files. Writers believed they were writing to the NAS.
This also produced the false "mode 000 / unreadable" reading in the handoff: on the
host the same records are mode -rwxrwxrwx and readable.
DAMAGES: persistence. Evidence divergence, and a post-mortem drawn from a stale copy.
STATUS: confirmed by mount/df and by direct comparison against the host.

## D6 — Filesystem sweeps wedge on the NFS mount
`find`/`grep` processes from earlier sessions sit for hours in uninterruptible sleep on
`/mnt/opshome`, launched through `tailscale ssh` without closing stdin. Broad sweeps
over that mount hang rather than fail.
DAMAGES: reliability of any automated audit that scans the campaign tree.
STATUS: observed, not yet repaired. Owner unclear.

## Open — not yet established
- Whether each lane's emitted records actually satisfy the real stop rule (audit running).
- Whether the resume path preserves lane identity (thread, seed, champion) (study running).
- Whether provider health holds across a spaced probe series (probes running).

## D7 — RESOLVED, NOT A DEFECT. Nine branches were never pushed, not lost
Corrected 2026-08-25 by direct recovery. `git ls-remote --heads` confirms only
`chitra-autoresearch/ar-04` exists on github.com/ReticleWorks/chitra. A bounded search of
every local checkout on roundtop (main repo, its worktrees, the ~90-worktree campaign tree,
17 checkouts under llm-workspace) and ~90 checkouts under /home/ubuntu on temp-twinridge
found none of the nine. The same search DID find ar-04 everywhere it exists, so the absence
is real rather than a search gap.
The reason: `amp threads export` succeeded for all nine lanes, and in every one the lane
ended by reverting its own last commit back to the shared baseline. Zero `git push` calls
appear in any of the nine transcripts. Nothing above baseline was ever produced, so nothing
was ever pushed. No recovery is needed and no orb needs resuming.
VALUE PRESERVED: every applied diff from all nine lanes is saved locally from the exports.
That is the "already tried, did not help" corpus for the next wave.
SIGNIFICANCE: this independently confirms the plateau reading. Nine of ten lanes, given the
same baseline and the same objective, produced nothing. The binding constraint is the shape
of the search, not the amount of compute.

## D9 — A mid-flight instruction to a subagent read as a prompt injection
When the coordinator sent an amendment to a running agent authorizing a git push, the agent
classified it as a likely prompt injection — it arrived through the tool-output channel and
justified itself with a claim the agent could not verify — and refused. The refusal was
harmless here because the push turned out to be unnecessary.
DAMAGES: autonomy. A supervisor that cannot amend a running lane's instructions mid-flight
has no steering channel, and an agent that accepts any such amendment has no defense.
OPEN: the next wave needs an authenticated way to amend a running lane, or a rule that lane
instructions are frozen at launch. Not yet designed.

## D8 — A false G1 gate failure silently zeroes the whole score
The evaluator `tools/autoresearch_v2_benchmark.py` runs its pytest gate G1 in a subprocess
that does not set PYTHONPATH the way its dimension workers do. In a clean sandbox this
raised `ModuleNotFoundError: No module named 'chitra'`. A gate failure zeroes the score
with no signal distinguishing "the code is broken" from "the harness could not import".
DAMAGES: reliability of every score the campaign produced.
STATUS: reproduced by direct run. Whether the campaign runner was affected is UNKNOWN.

## Ceiling analysis — where points actually remain
Measured by re-running the evaluator at both commits:
  D1 12/28  (16 available)
  D2 10/22  (12 available)
  D3 13/18 at baseline, 18/18 at AR-04's commit (0 available after AR-04)
  D4 20/20  (maxed)
  D5 12/12  (maxed)
All 28 remaining points lie in D1 and D2. Lane objectives for the next wave should be
aimed there rather than left free-ranging.

## Benchmark blind spots — candidates for a second, unseen scorer
The evaluator exercises only dispatchd's queue-drain path under two harness-chosen crash
points. Not covered: goal enforcement, rate-limit pause/resume, PR review, the
long-running daemon loop, a real SIGKILL-orphaned lock file, ledger signature validity,
torn writes, and sustained-uptime resource leaks. The 54-test tests/test_dispatchd.py
suite shares these gaps.

## D10 — RETRACTED IN PART. Two of three closed PRs were superseded, not shelved
Corrected by direct review of the diffs, PR comments, and `git merge-tree` checks.
 - #87 (goals interview): its fix SHIPPED to main as PR #94, merged 2026-08-21. ABANDON.
 - #88 (verified submit): its core SHIPPED to main as PR #93, merged 2026-08-21, in a more
   rigorous form. ABANDON the mechanism. One loose thread: #93's body says it explicitly
   DECLINED #88's default-flush proposal, which contradicts #88's closing comment claiming
   that piece was kept. Unresolved.
My earlier framing — that fixes for the owner's pain were closed unmerged — was WRONG for
these two. Closing them was tidying superseded work.

## D10b — STANDS. Idle-lane detection is absent from shipped code
#89 `feat(keeperd): acting supervisor loop - no lane idle-and-unaligned` is the exception.
Nothing on main does what it did. Its closing comment says monitord absorbed it; monitord
merged two days later as PR #110 (2026-08-23) in SHADOW MODE, observation only, with a
different detector set — drift, unnecessary-testing, document-dithering — not idle, wedge, or
stuck-composer. A grep of current main for keeperd's vocabulary returns no matches.
The owner's lead complaint is therefore unaddressed in shipped code.

DO NOT REVIVE #89 AS WRITTEN. Its wedge escalation is purely elapsed-time-based, the design
the owner already rejected on sibling PR #85: "elapsed time never establishes a finding or
authorizes a kill." It also carries a 604-line action loop with zero test coverage, and
duplicates delivery logic main has since hardened. It merges against main with zero
conflicts, so the code is available as reference; the DESIGN needs replacing with
progress-based detection — a lane is alive only if work is being consumed.

## D10c — Two open issues with no fix anywhere
 #50 A stale spinner line unconditionally suppresses a real blocking prompt in the bottom 8
     lines, so a pane awaiting operator input reports "working" and never surfaces. Root
     cause: an unconditional working-over-blocked override in classify_snapshot. Confirmed
     still live on main, unconditional exactly as reported. No branch or PR fixes it.
     Small, concrete, high daily value.
 #53 No bounded resume path for a killed lane or tmux session. No implementation exists
     anywhere. Needs building from scratch.

## D11 — The dominant session failure: retry-identical on an unverified missing resource
Found independently in two sources. The shape: attempt an operation, fail because a resource
is reported missing, retry the identical operation, fail again — without ever checking
whether the resource exists in 1Password, an environment variable, or a documented config
path. The owner named this archetype before either source was read, which makes three
independent arrivals at the same failure.
Evidence: the session-transcript pass found the pattern across 9 distinct sessions; the lane
transcripts show "returned BLOCKED" 15 times, retried identically every time.
CAVEAT: the session-transcript pass examined 40 of 6,320 files and counted grep pattern
mentions, not verified instances. Treat its counts as a signal, not a census. The lane
transcript findings, which quote specific turns, are the stronger evidence.
DAMAGES: autonomy. A system that cannot tell a real block from a claimed one cannot run
unattended.

## D12 — Token-budget exhaustion produces empty artifacts, and nothing adapts
Ox Alpha exhausted its 131,072-token budget mid-generation and emitted an empty artifact.
The lane did not reduce the token limit, scale back reasoning effort, or fall back. The
candidate was marked invalid and retried identically.
The contract asks for candidate prompts "normally below 12,000 characters" while running
candidate reasoning at "max". Maximum reasoning plus a long context exhausts the budget
before a diff can be emitted.
LIKELY EXPLAINS: the 26 "No valid candidate artifact is available" events, the 15 identical
BLOCKED retries, and ar-07's 16:1 candidate-to-valid-evaluation ratio against a clean lane's
1.25:1. Stated as a strong hypothesis, not yet proven end to end.
FIX FOR THE NEXT WAVE: detect budget exhaustion as its own distinct outcome, and respond by
lowering reasoning effort or shrinking context — never by retrying unchanged.

## D13 — Orphaned locks and a ledger that cannot express completion
21 lock files exist with no owning process, 18 of them under goal-interviews.
Chitra's ledger records delivery only — `sent_at` and `signature`. It has no `completed_at`
and no `goal_met` field. Completion markers live in pane output text rather than the ledger,
so a supervisor crash or a changed grep pattern loses the completion claim entirely.
DAMAGES: persistence and autonomy. The ledger structurally cannot distinguish a session that
finished from one that merely stopped, which is the exact distinction the whole system needs.

## D14 — The goal interview has never actually run
The system's owner states plainly that no goal interview has ever run with them, despite the
feature's label. Two independent records agree:
 - The repo survey found goal records exist with no recorded four-question interview,
   "despite claims of hard enforcement".
 - The runtime hunt found 18 orphaned lock files under goal-interviews with no owning
   process — the machinery takes locks for interviews that never complete.
PR #87 `feat(goals): machine-enforced ingestion interview and administrative close` was
written to fix exactly this and was closed unmerged on 2026-08-21.
DAMAGES: goal enforcement at the root. If the interview never runs, no session has a
verified objective, and nothing downstream can check completion against intent.

## D15 — `--mode low` was silently ignored on every resumed turn
730+ stderr files carry the identical 107-byte warning:
  "Warning: --mode is ignored when continuing a thread with --orb-execute"
The supervisor passes `--mode low` intending cheap coordination. Amp ignored it on every
`threads continue` call. Whether this changed cost or controller behavior is NOT established.
DAMAGES: reliability. The campaign believed it ran under a setting that was never in effect,
and the warning was emitted 730+ times without anyone reading it.

## CORRECTION — ar-06 and ar-09 DID complete
A lane-stdout pass reported ar-06 and ar-09 halted without completion markers. Verified false
by direct check: both hold SUPERVISOR_COMPLETE (13:15 and 13:11 respectively) and both have a
marker-terminated success result in their newest stdout file. That pass read an older file and
took it for the last. The 10-of-10 stop-rule audit stands. Finding discarded.

## D16 — The stop rule rewards failure and cannot distinguish it from success
The rule: "12 valid candidate evaluations, or a plateau of 5 consecutive valid
non-improvements after at least 8 evaluations."
All ten lanes exited on the plateau branch at exactly 8 evaluations — the floor. None
reached 12. A lane whose candidates are mostly bad accumulates non-improvements as fast as it
can produce them, hits 5-in-a-row immediately, and exits at the minimum. The rule cannot tell
"explored thoroughly and plateaued" from "could not generate anything good and quit". Both
emit the same completion marker.
Evidence from the thread exports — commits and reverts per lane:
  ar-01 9 commit / 9 revert    ar-02 9/9    ar-03 9/9    ar-05 9/9
  ar-06 11/18                  ar-07 9/11   ar-08 12/15  ar-09 24/9
  ar-10 8 commit / 8 reset --hard, 0 revert
Every candidate committed then undone; every score 67 except AR-04's single improvement.
ALSO ESTABLISHED: compute was never the constraint. The evaluator is a Python harness over
dispatchd's queue-drain path — three runs at startup plus one per candidate, minutes not
hours. ar-07's 358 minutes went into failed candidate GENERATION, not evaluation.
FIX FOR THE NEXT WAVE: raise the plateau threshold and require lanes to reach 12. Separate
"plateaued after real exploration" from "could not produce a valid candidate" as distinct
terminal states with distinct markers. Adding orbs without this changes nothing.

## D17 — Campaign evidence is becoming unreadable over time
The lane stdout files on temp-twinridge are mode 0, owned by ubuntu:uucp. The ssh account IS
ubuntu, and mode 0 denies the owner. `sudo -n` is unavailable.
The strange part, recorded because it is not explained: the file
ar-06/amp.supervised-0001-20260825T131011Z.stdout.jsonl was READ successfully through this
same path, as this same user, roughly twenty minutes before the same read returned
"Permission denied". NFS attribute-cache expiry is the likeliest explanation. It is NOT
proven. Do not treat either reading as settled.
CONSEQUENCE: the handoff's original "sealed records" claim was CORRECT, and the later
dismissal of it — including mine — was wrong. The durable readable channel is
`amp threads export`, which works and is unaffected by the mount.
DAMAGES: persistence. A campaign whose own evidence expires cannot be audited later.

## D18 — ROOT CAUSE. The watchd-to-monitord migration dropped enforcement
Established by source reading plus one live test, during a reachability rework of the draft
reliability scorer.

`watchd.main()` opens with a DeprecationWarning naming `chitra.monitord` as its replacement.
monitord's own docstring says it "collapsed out of watchd". But:
 - monitord's detector list is ("drift", "unnecessary_steps", "excessive_testing",
   "document_dithering").
 - A grep for detect_idle, detect_wedge, or detect_stuck anywhere in the codebase returns
   nothing.
 - `review_watched_session` is real and reachable from watchd via `_review_turn_end`, using a
   real ClaudeProcessReviewer rather than a stub. monitord NEVER CALLS IT.
So detect, decide and act all exist in the DEPRECATED daemon. The replacement observes and
does not act. This is the mechanical root cause of the owner's complaints about undetected
idle lanes and unenforced goals.

## D19 — PROVEN BY LIVE TEST. The goal interview checks shape, never truth
A goal record was constructed whose interview_receipt reads, in plain text,
"NO INTERVIEW WAS CONDUCTED - this text is a placeholder", and submitted through the real
`upsert_goal`. IT WAS ACCEPTED.
The enrollment contract requires four strings prefixed `operator:` or `source:`. It validates
their SHAPE. It never validates that an interview happened. `lane_anchor` genuinely refuses a
launch without a receipt, so the gate is real - and trivially satisfiable by the launching
agent writing its own receipt.
This is why a feature can hard-enforce an interview in code and still never interview the
owner. Supersedes the weaker inference recorded in D14.

## D20 — Nothing automatically checks a claim against evidence
Neither watchd nor monitord imports `chitra.evidence` or `chitra.convlog`. The only caller is
the manual CLI command `chitra-convo brief`, whose docstring names its caller as "the monitor
harness LLM" - a person or an LLM must choose to run it.
So no running daemon scans an ordinary transcript for a false-blocker or unverified claim.
This is the machinery gap behind the owner's archetype failure: a lane reporting a credential
missing when it exists in a well-known location.

## Scorer status after reachability rework
HEAD 157/200 (process 100/100, session 57/100 - down from a false 100/100).
Baseline c32e27d 114/200. Both commits show the same session-level gaps, because those are
properties of the watchd/monitord migration rather than of either commit.
The earlier 200/200 was the scorer crediting capabilities that exist as functions but that no
running daemon invokes. Corrected after challenge.

## D21 — RETRACTED HYPOTHESIS. "max reasoning breaks candidates" is NOT supported
I proposed that the campaign contract's instruction to use `max` reasoning on every candidate
call was the root cause of five lanes ending TERMINAL-BLOCKED. A controlled experiment
disproves it.

Identical 6,907-character candidate prompt, same adapter, same model:
  high  rc=0, no error, valid diff, 7,062 output chars
  max   rc=0, no error, valid diff, 8,405 output chars
Neither truncated. `max` is not broken at realistic prompt size.

WORSE FOR THE HYPOTHESIS: `max` produced the BETTER diff. It emitted `@@ -1,181 +1,243 @@`,
adding a counter while preserving the body. `high` emitted `@@ -1,181 +1,9 @@` - a destructive
rewrite deleting 172 lines that would still pass a naive "starts with ---" check.
So lowering candidate effort to `high` would have DEGRADED output quality while appearing to
fix things. Do not make that change.

WHAT REMAINS TRUE: CH-02 recorded one genuine truncation at max ("reasoning truncated: model
hit output token limit during reasoning (max_tokens=131072); content empty"), and two of its
calls returned tool-call transcripts rather than diffs. Those events are real.
WHAT IS UNKNOWN: why. The size threshold at which truncation begins is not established, and
whether the tool-call transcripts came from prompt content, harness configuration, or context
length is not established. Do not act on a cause until one is measured.

LESSON FOR THE CAMPAIGN: a "starts with a unified diff" check is not a validity check. The
`high` result here was well-formed and destructive. Candidate validation must verify the diff
applies AND that its hunk line counts are plausible against the source, not merely that the
text looks like a diff.

## D22 — MEASURED. Truncation tracks reasoning DEPTH, not prompt size
Measured from the eight lanes' durable thread exports. Input sizes of the Ox calls that
reported "reasoning truncated: model hit output token limit during reasoning
(max_tokens=131072)":
    CH-02  1,346 chars
    CH-04  2,646 and 11,604 chars
    CH-05  6,665 chars
    CH-06  2,677 chars
A controlled experiment ran a 6,907-char candidate prompt at `max` with NO truncation and a
valid diff. Several truncating calls were SMALLER than that. One truncated at 1,346 chars.

CONCLUSION: prompt size does not determine truncation. Reasoning depth on a hard problem does.
A short prompt posing a difficult question can exhaust the output budget before any content is
emitted; a longer prompt posing an easy one will not.

THIS INVALIDATES THREE PROPOSED FIXES:
 - Prohibiting `max` (peer review rebuild item 1): `high` produced a destructive diff deleting
   172 lines. Lowering effort degrades output.
 - Capping context length: truncation occurred at 1,346 chars.
 - Shrinking the request (the contract's own ladder rung 3): CH-04 climbed it and got "only a
   system warning, not a patch".
The lanes followed the adaptation ladder correctly and still produced nothing. This is a
capability limit, not a configuration error or a lane defect.

WHAT REMAINS UNTRIED: decomposition. Ask for a smaller PIECE of the problem rather than a
smaller prompt about the whole problem. Not yet tested - do not treat as established.

CAMPAIGN STATUS AT RECONCILIATION: eight lanes, zero pushes, one git-commit mention.
Six truncation events across five lanes. CH-01, CH-06 and CH-08 share the profile.

METHOD WARNING: a grep for TERMINAL-FIXED / TERMINAL-BLOCKED in a lane export returns True for
BOTH on every lane, because the lane contract defines both strings and the transcript echoes
the contract. Verdicts must be read from the controller's final emitted record, never from a
substring search. Same trap as wave 1's completion marker.

## D23 — PROVEN. Decomposition fixes GENERATION; it does not fix correctness
Test: the same model, adapter and `max` reasoning that truncated five lanes was given ONE
function (3,930 chars) and asked for ONE line's behavior to change. Result: valid unified diff,
no truncation, 4,936 in / 1,183 out. Decomposition is the lever - ask for a smaller PIECE of
the problem, not a smaller prompt about the whole problem.
Ox also used the codebase's own API correctly from an excerpt alone: it called
`region_text(bounded_snapshot)`, which exists at agent_status.py:144 and is already used in the
same idiom at line 539. It invented nothing.

AND THE CANDIDATE WAS STILL WRONG. Applied to a clean worktree of origin/main it broke
`test_stale_answered_codex_prompt_with_live_spinner_is_not_blocked`, which PASSES on unmodified
main. A well-formed, plausible, API-correct diff that regresses a deliberately protected
behavior. Caught only by running the test against the parent commit.

WHY, and this is an architectural finding about issue #50:
 - Protected case: a stale ANSWERED prompt above a LIVE spinner must read as working. The
   override exists for this and a test guards it.
 - Issue #50: a stale SPINNER below a REAL awaiting prompt must read as blocked.
Both put a working footer at the bottom of the pane, so the positional heuristic cannot
separate them. What separates them is whether the spinner is genuinely live - and
`classify_snapshot(snapshot, *, agent, repository)` receives ONE snapshot. It holds no temporal
evidence and therefore cannot decide freshness.
Issue #50 may not be fixable inside classify_snapshot. It likely needs successive snapshots or
a freshness signal that function never sees. Any lane assigned to #50 must be told this, or it
will keep producing positional heuristics that break the protected case.

LESSON: "Ox returned a diff" is not progress. "The diff applies" is not progress. Only
parent-fails-and-branch-passes is progress. The lane contract already required this and it is
what caught the regression.

## D24 — CORRECTS D18. watchd IS in production; monitord is NOT deployed
Measured on temp-twinridge, the host running the fleet.

RUNNING NOW:
  polyphony-chitra-watchd@monitor.service     loaded active running
  polyphony-chitra-watchd@boomtown.service    loaded active running
  /usr/bin/python3 -m chitra.watchd --events-log /var/lib/polyphony-chitra/events.log \
      --socket-path /run/polyphony-chitra-monitor/chitra.sock
  (plus the boomtown instance on its own events log and socket)
Also running: polyphony-chitra-dispatchd@, sweepd@, triaged@, each for both instances.

NOT PRESENT: no chitra-monitord unit in `systemctl list-unit-files`, and no monitord process
in the process table.

WHY THIS MATTERS. D18 recorded that "the watchd-to-monitord migration dropped enforcement".
That framing is WRONG for production: the migration has not happened there. The daemon that
HOLDS the enforcement path is the daemon that is running. `review_watched_session` is real and
reachable from watchd via `_review_turn_end`, and watchd is live on two instances.
D18's source reading was accurate about the CODE. Its implication about the deployed system
was not. Corrected here rather than deleted, so the reasoning stays visible.

THE SUSPECT NOW. If enforcement is deployed and the owner still experiences it not firing, the
capacity limit becomes the prime candidate: `watchd.py` sets DEFAULT_REVIEW_MAX_WORKERS = 2,
and a turn needing review above that cap is marked `turn-finished-unverified` and never
reviewed. Under the owner's parallel load that would silently disable enforcement on a
correctly deployed, healthy daemon. NOT YET MEASURED against the live system - that is the next
experiment, and it is now cheap because the events log path is known.

## Concrete deployment values — replaces the plan's invented paths
The plan repeatedly guessed `/opt/chitra/venv/bin/python` and a `--once` interface. Measured:
  entry point      /usr/local/bin/chitra   (POSIX shell wrapper, package chitra-launcher)
  module root      /usr/local/lib/polyphony/chitra/chitra
  interpreter      /usr/bin/python3        (system python; there is no venv)
  units            polyphony-chitra-{watchd,dispatchd,sweepd,triaged}@{boomtown,monitor}.service
  events log       /var/lib/polyphony-chitra/events.log
                   /var/lib/polyphony-chitra-boomtown/events.log
  socket           /run/polyphony-chitra-monitor/chitra.sock
`/opt/chitra` does not exist. Any plan step naming it cannot run.

## D25 — ROOT CAUSE, FULLY ESTABLISHED. watchd polls a dead tmux socket
This supersedes D18 and D24 as the explanation for why enforcement never fires.

CHAIN, each link measured on temp-twinridge:
 1. `polyphony-chitra-watchd@{monitor,boomtown}.service` are active/running, 0 restarts,
    up 32 hours, ~93s CPU, no journal entries.
 2. A systemd drop-in sets the tmux server they watch:
      TMUX_TMPDIR=/run/chitra-worker
      CHITRA_WATCHD_TMUX_SOCKET=/run/chitra-worker/tmux-1000/default
      CHITRA_WATCHD_SESSION_NAMES=atlas-v5,gct-secret-broker,infra-health,
        monitor-lane-architecture,starchamber-v12,tophand-lane-verb,watch-pipeline
      CHITRA_WATCHD_SESSION_PREFIXES=atlas-,gct-,infra-,monitor-,starchamber-,tophand-,watch-
    The configuration is deliberate, not broken.
 3. That socket FILE exists (srw-------, ubuntu:chitra, Aug 24 20:41) but
    `tmux -S /run/chitra-worker/tmux-1000/default ls` returns
    "no server running on /run/chitra-worker/tmux-1000/default".
 4. The real work runs on the DEFAULT user tmux server (/tmp/tmux-1000/default):
    boomtown, harness-secondary, prime-hermes-eval. watchd never looks there.
 5. Control test: a session named `monitor-probe` created on the default server matched the
    prefix list and watchd still produced nothing in 45s - correct, since it is the wrong
    server. Probe removed afterwards.

CONSEQUENCE: watchd observes zero sessions and emits zero events. The review path, the
DEFAULT_REVIEW_MAX_WORKERS=2 cap, the interview gate and the claim-evidence check were never
REACHED, so none of them can be the cause of enforcement not firing. Earlier entries that
treated the worker cap as the leading suspect are superseded.

ALSO: Chitra is NOT INSTALLED ON ROUNDTOP at all - no binary, no launch agent, no process, no
state dir - while roundtop is where the operator's own sessions run (codex-10k-x,
codex-10k-x-v2, codex-swf, grokt-audit, grokt-program). So even a fully working Chitra on
temp-twinridge would not watch the operator's sessions. Chitra is systemd-based; roundtop is
macOS, so a straight install is not available.

CLEANUP REQUIRED (not yet done, needs a decision on blast radius):
 - Either start a tmux server on /run/chitra-worker/tmux-1000/default and run the named
   sessions there, or repoint CHITRA_WATCHD_TMUX_SOCKET at the server where work actually
   runs. The second turns a dispatcher loose on panes running other campaigns, so it is not
   a safe unilateral change.
 - Establish what was supposed to create that worker server, and why it is gone. A stale
   socket with no server implies the server died and nothing restarted it.

## D25 CORRECTED — the tmux socket fix IS loaded; the WATCH LIST is stale
Correcting the entry above before it misleads. The live process environment is authoritative
and I read it:
  MainPID 2080557
  TMUX_TMPDIR=/tmp
  CHITRA_WATCHD_TMUX_SOCKET=/tmp/tmux-1000/default
The drop-in 90-monitor-live-tmux-socket.conf was written 2026-08-24 13:47:55 and the unit
started 13:48:36, 41 seconds later. NeedDaemonReload=no. So the correction is applied and
watchd IS watching the real user tmux server. The "dead socket" reading in the entry above was
drawn from the 20-fleet-declaration.conf values, which the 90- drop-in overrides.

THE ACTUAL REMAINING DEFECT is the watch roster:
  watched names: atlas-v5, gct-secret-broker, infra-health, monitor-lane-architecture,
                 starchamber-v12, tophand-lane-verb, watch-pipeline, chitra-monitor-codex
  watched prefixes: atlas-, gct-, infra-, monitor-, starchamber-, tophand-, watch-
  sessions actually running on /tmp/tmux-1000/default:
                 boomtown, harness-secondary, prime-hermes-eval
Zero overlap. Chitra watches a roster of lanes that no longer exist while the live work runs
under names it was never told about. Stale declaration, not broken code.

GOVERNED FIX PATH: the file says "Managed by fleet-repo roles/chitra. Do not edit on the
machine." The repair is a fleet-repo change to roles/chitra, landed by pull request on green
CI with agent review. An on-host edit would be drift and would be overwritten at the next
converge.

STILL UNKNOWN: whether watchd emits events for a matching session. A control session named
`monitor-probe` matched the prefix list and produced no event within 45 seconds, but it ran
`sleep 900` with no agent in the pane, so it may legitimately generate no state change. That
test was inconclusive and is not evidence either way.

## D26 — PLAN-v7's ordering claim is wrong: the RELIABLE loop has no headroom yet
v7 line 149 says "The RELIABLE process loop can start immediately against
`chitra_reliability_bench.py`". Measured, that loop would be pointless today.

`chitra_reliability_bench.py` scores HEAD at 157/200 = process 100/100 + session 57/100.
The process dimensions are ALREADY MAXED. A hill-climbing loop against a maxed dimension
generates candidates, scores every one at 100, and plateaus on its first pass. All 43 points of
real headroom are in the SESSION dimensions, which by v7's own decomposition belong to
PERSISTENT and AUTONOMOUS, not to RELIABLE.

WHAT WOULD CREATE REAL HEADROOM: the determinism dimension being added to
goal_bench_reliable.py now - run the same scenario N times and require an identical result.
There is direct evidence Chitra has non-determinism of exactly this kind: the earlier
concurrency probe, with 8 racer processes and no start barrier, produced claim_winners of
3, 1, 2, 1 across identical runs. That was diagnosed as a test-harness timing bug and fixed
with a barrier, so it is NOT yet established that the product itself is non-deterministic -
the determinism dimension is what would settle it.

CORRECTED ORDERING: the RELIABLE loop starts AFTER its determinism dimension exists and shows
headroom below 100. Starting it before that burns orb-minutes for a guaranteed immediate
plateau. Do not follow v7 line 149 as written.

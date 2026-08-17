# Changelog

## 0.9.11

- Normalize canonical remote pane targets at the governed grant boundary and
  fail closed when pane fallback sees the steer text still in an agent's
  composer instead of consumed into the transcript.

## 0.9.10

- Preserve watchd's per-idle-period edge semantics through triaged: each new
  `IDLE` event reaches `queue.tsv` and `flags.log` even when its stable payload
  is byte-identical to an earlier idle period.

## 0.9.9

- Route remote governed-lane capture and steering through the fixed codexman
  SSH grant verbs, allowing the draft guard to recognize the Codex 0.147 empty
  composer without granting raw remote tmux execution.

All notable changes to this project are documented here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. This project uses [Semantic Versioning](https://semver.org/), currently in the 0.x range (see `docs/DESIGN.md` for why 1.0.0 is reserved for later).

## [0.12.2] - 2026-08-17

The merge daemon is being turned on for the first time, on one host. This
carries what it needs to be safe there.

### Fixed

- A token command the merge daemon cannot use now fails the unit instead of
  being logged and slept off. It would otherwise loop for days merging nothing
  while every reading systemd can take said it was running. The unit takes its
  environment file with no dash for exactly this reason, and minting the token
  per merge had moved the credential out of that file, leaving the guard
  checking an empty room.
- The reviewer's second model prompt is held to the framing that broke the
  first, so the same regression cannot come back through the other path.

## [0.12.1] - 2026-08-17

Turn-end review has never completed a single review anywhere in the fleet. The
last reason is fixed here, and it does not reach the monitor until this ships.

### Fixed

- The reviewer prompt could not be read by the wrapper that runs it. In the
  fleet the reviewer is not run directly: `chitra-watchd-reviewer` runs it, and
  that wrapper recovers the reviewer identifier and the two content bindings by
  splitting the prompt on a newline followed by `INPUT=`. The prompt wrapped its
  payload in tags instead, so the wrapper refused before a model was ever
  called, and watchd turned the refusal into a blocked session. The only
  turn-end review record the fleet has ever written says exactly that, and it
  marked a clean completion as blocked. Every reviewer test read the payload
  back with its own marker rather than the wrapper's, which is why the suite
  stayed green while the gate could not run once.
- Three instructions in that same prompt still told the reviewer to read its
  fields "in `<input>`" after the payload moved out of that section, so they
  pointed at nothing. A test now holds every section the prompt names to one it
  opens and closes.

## [0.12.0] - 2026-08-17

Every automatic behaviour gets a manual verb, and a daemon that merges a lane's
green work without waiting for a person. Five lanes stalled at once on
2026-08-16, each waiting on someone to merge its own passing pull request.

### Added

- `chitra-failover evaluate`, `chitra-failover run --lane`, and
  `chitra-failover resume` — the pause and resume decisions as commands anyone
  can run. Auto mode is the daemon calling the same verbs, so what you watch it
  do you can do yourself, and what it refuses stays refused when you type it.
- `chitra-merge <owner/repo> <number> [--dry-run]` merges one pull request
  through the same decision the daemon uses. `--dry-run` prints the decision and
  writes a ledger line without merging.
- `polyphony-chitra-merged`, a daemon that merges verifiably green
  lane-authored pull requests under a GitHub App identity, one at a time per
  repository. It never reads or writes branch protection: a pull request
  protection would refuse is one this refuses.
- `chitra-usage evaluate --dir` now reads a host's export directory as well as a
  local snapshot directory.

### Changed

- The merge decision refuses on more than gate state. A bot author is refused,
  and so is a pull request nobody has touched in 24 hours. Both come from real
  merges an interim auto-merger made overnight: five dependency-bot pull
  requests including a 1.28.1 to 2.0.0 major bump, and one that had been open
  about five days. Green says a change is mechanically safe to land. It cannot
  say whose work it is or whether anyone still wants it.
- The hold brake honours `hold` as well as `chitra-hold`. It was wired only to a
  label ReticleWorks repositories do not apply, so it stopped nothing.

### Fixed

- `chitra-usage evaluate` could not read the exports `chitra-usage export`
  writes. It demanded `chitra.usage.v1` and the exporter publishes
  `chitra.usage-export.v1`, so a correct pause verdict was thrown away
  overnight while the monitor reported nothing wrong.
- The merge identity check called `/user`, which an installation token cannot
  call. The daemon could never have merged anything. Every test passed, because
  every fixture agreed with the wrong assumption.
- The ledger recorded the verified head as the merge commit, naming a sha that
  is not on the base branch. A squash merge creates a new commit, and both are
  now read back after the merge.
- The merge token is minted per call and passed by environment rather than
  stored, so it cannot expire mid-run or land in a process listing.
- Ten tests measured the host they ran on rather than the code. Three named a
  real fleet host as the *remote* host, so on that machine they took the local
  path and stopped testing what they were named for. One of those reached a
  live tmux pane belonging to another lane. Six wrote state files without a
  mode and failed wherever the umask is 002.

## [0.11.0] - 2026-08-16

Rate-limit visibility and failover. A Codex lane hit its weekly hard cap around
2026-08-14 and sat dead for roughly two days: the monitor had no reading for
that host, and a capped pane classified as idle.

### Added

- `chitra-usage export` writes a token-free `chitra.usage-export.v1` file per
  host and backend into a shared directory, so a host publishes its own usage
  rather than being reached into. The file carries percentages, reset times, an
  account identity and a verdict, never a provider token.
- `chitra-usage evaluate --fleet-dir` reads every host's exports and returns one
  verdict per host and backend. A reading that is missing, old, or unreadable
  becomes its own named verdict — `missing-export`, `stale-export`,
  `invalid-export` — instead of silence.
- Two agent states, `rate_limited_hard` and `rate_limited_warn`, with banner
  signatures for Codex quoted from a real capped lane's transcript. A matched
  rate-limit rule is never overridden, because a capped pane still draws its
  input row and would otherwise read as idle. The resume time is parsed onto the
  event, since the banner scrolls away.
- Separate Codex thresholds (`codex_pause_5h_pct`, `codex_pause_weekly_pct`,
  `codex_warn_5h_pct`, `codex_warn_weekly_pct`) and an `auto_transfer` policy
  knob. The Codex weekly pause sits at 90 against Claude's 95: that window is a
  hard cap with a multi-day reset, so margin is cheaper than dead time.
- `chitra-goals transfer` holds a capped lane and scaffolds its successor on the
  other backend under one lock, copying the strategic fields verbatim. It starts
  nothing; `chitra-goals check` still gates launch.
- Transcript-pipe liveness in watchd: a governed lane whose pipe is unarmed,
  whose transcript is absent, or whose transcript has stopped growing while the
  pane changes is reported, and `triaged` raises it as CRIT.

### Changed

- Goals documents are written as `chitra.goals.v2`, adding `successor_of` and
  `transferred_to`. The bump is additive and v1 documents still load, so a host
  that has not upgraded keeps working.
- Codex usage windows are identified by their reset horizon rather than by the
  slot the provider used. Measured 2026-08-16, a capped account reported its
  weekly cap in the `primary` slot with `secondary` null, so a weekly threshold
  keyed on the slot name would never have fired.

### Fixed

- `lane_anchor.start_lane` arms `tmux pipe-pane` on both the create and the
  already-running paths. It previously armed it nowhere, and its early return
  for an existing session did nothing at all — which is the respawn path that
  left a lane unrecorded for twenty-five hours.
- The ownership provider follows the goals schema instead of pinning one
  version, and carries the new record fields. Pinned to `chitra.goals.v1` it
  would have refused every v2 document and answered non-authoritative
  `unknown`, continuing to run while knowing nothing.

## [0.10.2] - 2026-08-14

### Fixed

- Delay governed launch receipts until the new tmux session survives its
  startup window. An agent that exits during startup now returns temporary
  failure code 75 without writing a false-success receipt, so callers can
  retry that one failure class safely.
- Recognize tmux's `can't find session` response as an inactive lane during
  status and stop operations.

## [0.10.1] - 2026-08-14

### Fixed

- Pass Chitra's resolved package root into each governed tmux pane, so the
  supervised agent starts from an immutable target install without a host-wide
  Python path shim.

## [0.10.0] - 2026-08-14

### Added

- A deterministic semantic agent-status broker with authoritative integration
  reports, local and bundled TOML screen manifests, strict blocked rules, and
  evidence-rich explain output.
- A mode-`0600` newline-delimited JSON socket with correlated request IDs,
  typed status subscriptions, bounded event predicates, blocking semantic
  waits, and a self-describing JSON Schema document.
- Governed lanes now inject `CHITRA_LANE_ID`, `CHITRA_SESSION_REF`,
  `CHITRA_PANE_ID`, `CHITRA_PANE_TARGET`, and `CHITRA_SOCKET_PATH` into the
  agent process.
- A validate, switch, and commit live-handoff protocol transfers Watchd's
  status state and socket ownership while tmux keeps pane processes running.

### Changed

- **Breaking:** Watchd no longer treats pane-content hashes, a fixed active
  regular expression, or an unchanged input row as semantic status. It emits
  `AGENT_STATUS` transitions from lifecycle reports or manifests instead.
  Operators must ship compatible manifest provisioning and lifecycle hooks
  with the package update; see `docs/watchd-status-migration.md`.

### Fixed

- Anchor screen-derived blockers to live bottom controls, use exact answer
  tokens, and let a live working footer suppress stale prompt text.
- Bound `agent.wait` by default, align the CLI timeout, and reap wait and
  subscription handlers when clients disconnect.
- Recover verified stale crash sockets while continuing to refuse a socket
  held by a live server.
- Validate handoff state before broker mutation, keep the replacement alive
  after commit, and inject supervised identity with `tmux new-session -e`.

## [0.9.8] - 2026-08-13

### Fixed
- Sweepd accepts every severity documented for `flags.log` (`CRIT` and
  `IDLE`), preventing an idle event emitted by triaged from crashing the
  sweep daemon. Mixed-severity and triaged-to-sweepd regression coverage keeps
  the producer and consumer wire formats compatible.

## [0.9.7] - 2026-08-13

### Fixed
- Watchd clears its per-pane IDLE guard when raw pane content changes or the
  pane leaves the input row, so each new idle period emits one IDLE event.

## [0.9.6] - 2026-08-13

### Changed
- Reserved the next package version for the fleet dispatchd unit correction
  that preserves the shared worker tmux runtime directory across restarts.

## [0.9.5] - 2026-08-13

### Added
- Watchd accepts an explicit tmux socket from `--tmux-socket` or
  `CHITRA_WATCHD_TMUX_SOCKET`, plus exact session-name and prefix filters.
- An unchanged Claude or Codex input row emits one configurable `IDLE` event.
  Triaged writes it to `queue.tsv` with severity `IDLE` and to `flags.log`
  with an `IDLE` prefix for bounded monitor consumption.

## [0.9.4] - 2026-08-13

### Changed
- Governed Claude and Codex lane launches now carry an explicit effort into
  the agent command and the `chitra.lane-launch.v1` receipt.
- A gateway already running as the declared lane account launches directly,
  without an unnecessary privileged `runuser` hop.
- Trinity is accepted as the second sanctioned governed lane host, with the
  same host-qualified goal gate used by Tophand.

## [0.9.3] - 2026-08-13

### Added
- An append-only `chitra-decisions` log for consequential monitor decisions. Every entry records the time, decision, basis, citation, and authority.
- Explicit `moot` and `superseded` outcomes for operator conversation threads, with a required basis, citation, and authority.
- Durable ask-retirement history, including the `retired-by-monitor-with-cited-basis` state.

### Changed
- Monitor-authored operator briefs, decisions, and retirement reasons now apply deterministic plain-English checks at write time. The checks reject unexplained internal jargon, bare codenames, and sentence fragments. They never gate or rewrite spawned work-session reports, evidence files, transcripts, pull-request text, citations, or verbatim asks.

## [0.9.2] - 2026-08-13

### Added
- A governed Tophand tmux-lane launcher with complete goal-ingestion gating,
  frozen lane/goal receipts, Claude Sonnet/Opus and Codex model selection,
  usage-pause refusal, and lifecycle-parity documentation.

### Fixed
- Codex's `Ask Codex to do anything` composer ghost suggestion is recognized
  as an idle placeholder instead of an unsubmitted operator draft.
- Lane status read failures report UNKNOWN instead of false inactivity.

## [0.9.1] - 2026-08-10

### Added
- A PR security review gate, `chitra.pr_review` / `chitra.pr_reviewd` (new `chitra-pr-review`
  entrypoint). Fetches one pull request via `gh`, runs deterministic blast-radius/diff-size
  pre-checks, then an isolated multi-reviewer `claude -p` security pass over the diff
  (hardcoded secrets, injection classes, auth bypass, insecure deserialization, dependency
  risk, prompt injection). Findings are logged to a signed, deduplicated `pr_reviews.jsonl`
  ledger and reported as one plain PR comment. Conservative default: never merges, approves,
  requests changes, or fails a required check; `PRReviewPolicy.block_on_findings` (default
  `false`) is the explicit, off-by-default opt-in for a blocking posture. Ships with a stock
  `.github/workflows/pr-security-review.yml` trigger on `pull_request` events.

### Changed
- The rate-limit guard's never-pause session prefixes are now configured via
  the comma-separated `CHITRA_NEVER_PAUSE_SESSION_PREFIXES` env var instead of
  a hardcoded `NEVER_PAUSE_SESSION_PREFIXES` constant. The default is empty:
  no session is exempt from pausing unless the deployment sets the variable.

### Fixed
- `chitra.dispatch`'s local transcript-grep verification (`find_recent_transcript`)
  now searches every root in an `os.pathsep`-separated `CHITRA_CLAUDE_PROJECTS`
  list, not just the first. A local session running under a non-default
  `CLAUDE_CONFIG_DIR` (e.g. a dedicated persona/harness identity) writes its
  transcripts under that root's `projects/`, not the default
  `~/.claude/projects` — previously that session's delivery could never be
  confirmed by transcript-grep and always fell through to the weaker
  pane-capture fallback or FAILED.
- `chitra.lane_read`'s open-ask heading match and `chitra.triaged`'s
  `needs_operator` critical rule no longer require a hardcoded fleet-operator
  name; both default to the name-free `you`/`operator` case and accept
  additional operator names or aliases via the comma-separated
  `CHITRA_OPERATOR_ALIASES` env var.
- The rate-limit guard's never-pause-prefix skip reason no longer claims the
  matched session is "Chitra's own monitor/harness session" — the prefixes
  are operator-configured and may match any session, not necessarily
  Chitra's own.
- `dispatchd`'s Codex-TUI placeholder detection now also treats a known
  placeholder hint as idle at normal (non-dimmed) render intensity, not only
  when the whole row renders dim. Either signal alone is sufficient evidence
  of a placeholder; an unknown, normal-intensity draft is still blocked as a
  real operator draft.

## [0.8.2.7] - 2026-07-18

### Added
- Comprehensive user-facing documentation tree (`docs/`) covering getting started, concepts, daemon reference, and configuration with worked examples.
- Stock PR security-review workflow (`.github/workflows/pr-security-review.yml`) integrated with the `chitra-pr-review` CLI; deterministic pre-checks plus isolated multi-reviewer security pass over pull request diffs.
- Standard shields.io badges (license, Python version, PyPI package) to README header.

### Changed
- **Distribution renamed to `chitra-monitor` on PyPI** — Package is published and installable via `pip install chitra-monitor`. Python import module name (`chitra`) remains unchanged. GitHub repository URL updated from the defunct `first-polyphony/chitra` to `ReticleWorks/chitra` across README, documentation, examples, and pyproject.toml metadata.
- Clarified README and DESIGN.md framing: chitra's deterministic core (dispatch, ledger, routing, rate-limiting, ownership) is separated from optional LLM-judgment layers (goal enforcement, completion review). Chitra performs orchestration (dispatch, tracking, state coordination, hold/resume, completion gating) but not task decomposition or response generation.
- Removed hardcoded operator name (`trey`) from `chitra.lane_read` and `chitra.triaged` regex patterns; both now accept configured aliases via `CHITRA_OPERATOR_ALIASES` env var, defaulting to generic `you`/`operator` terms.

### Fixed
- Consolidated 8 pending feature/fix branches: multi-config-dir transcript-grep support, operator-name genericization, and Codex placeholder detection.

## [0.8.2.6] - 2026-07-16

### Fixed
- Fixed: dispatchd Codex-TUI placeholder detection;
  `COMPLETION_CLAIM_RE` hyphen-compound false-positive.

## [0.8.2.5] - 2026-07-15

### Removed
- Removed the local HTML-file board output path and the `chitra-board`
  entrypoint. The roster renderer and validated board-facts plumbing are
  unchanged; consumers can render the roster output however they choose.

## [0.8.2.3] - 2026-07-15

### Added
- A durable per-pause recovery ledger records the held session, reason,
  existing transcript pointer, goal-derived resume note, and reset time.
  Attached sessions remain pausable; only session refs matching the
  configured never-pause prefixes are excluded.
- Close-time inventory diffing for `chitra-goals close`, which blocks when
  caller-stated delivered items do not satisfy the enrolled `done_when` or
  when a required item is relabeled as follow-on/out of scope/deferred/future
  work without a recorded descope or explicit acknowledgement. `chitra-goals
  set` now also surfaces a persistent flag for missing or vague aggregate
  done conditions while storing the input unchanged. Chitra consumes done conditions; it never enumerates, proposes,
  authors, derives, annotates, or rewrites them.
- Forced completion review at every detected lane turn-end. `watchd` now
  distinguishes an ordinary finished turn from a completion claim, requires
  concrete deploy/live citations and per-item verification for a clean claim,
  records the review on Chitra's side, and drives explicit unverified or
  disputed roster states instead of idle-green.
- An isolated watched-session adversarial loop bound to the frozen goal. The
  initial round requires unanimous independent process results; a mid-review
  goal redirect is logged and automatically restarted with one reviewer.
  Reversible informational and in-scope technical answers may release after
  unanimous acceptance, while spend, credentials, irreversible actions, and
  strategy redirects always require operator confirmation.
- Required sidecar-authored delivery briefs for `chitra-artifacts record`,
  covering what was built, what it does, and whether it actually works with
  concrete evidence.
- Example systemd service/timer units for the two-minute capacity sweep, plus
  a read-only `chitra-ownership` query for Watchtower to check resolved session
  references against currently tracked working lanes.
- A persisted per-host load-pressure ladder using MemAvailable and Linux PSI,
  two-sweep anti-flap/hysteresis, 8/6/4/2 running-lane caps, deterministic shed
  priority, last-shed-first recovery, and backend-neutral Watchd activity facts.

### Changed
- Moved isolated completion review onto a bounded two-worker pool so
  `poll_once` never waits on `claude -p`; in-flight lanes remain yellow and
  later polls apply the completed verdict. Delivery-brief validation now lives
  only on the guarded artifact record path; lane completion disputes stay on
  cited-evidence and posture grounds.
- Defaulted `watchd`'s isolated reviewer to the ambient monitor model (ruling
  3A: same model as the monitor, different context), exposed its model,
  normal-round count, and command through environment and CLI configuration
  (operators may still pin a cheaper model deliberately), and scoped
  subprocess reviews to completion-claim turn-ends while retaining
  deterministic auditing for every finished turn.
- Consolidated `DecisionProvenance` and `ReasonedDecision` into the immutable
  `DecisionAttestation` API. Every reasoned answer or nudge is bound to the
  exact approved text and logged to Chitra's own attestation ledger before
  dispatch; review identifiers and gate metadata are never pasted into the
  monitored lane.
- Completion evidence is now a list of typed, citation-bearing records rather
  than caller-asserted deploy/live booleans. The default healthy-hedge lexicon
  also recognizes "conditionally healthy", "correctly blocked", "parse-only",
  "not publication-ready", "repaired and covered by tests", and "CI evidence".
- Raised the default graceful-pause thresholds to 92% for the five-hour
  window and 95% for the seven-day window, with approaching warnings at
  80% and 90%, respectively.
- Enabled the rate-limit guard capability by default, limited new resumes to
  one deterministic lane per sweep, and added a janitor that closes dead
  `superseded-by:` holds instead of re-arming them.
- Generalized the existing checkpoint/stop/quiescence transaction boundary so
  load shedding reuses it. Claude lanes retain `/goal clear`; Codex lanes use a
  fixed checkpoint-and-stop order followed by pane-quiescence verification.
- Consolidated filesystem and JSON persistence primitives, shared validation
  lexicons, and dispatch/persisted contract models, and replaced fleet-specific
  hostnames and paths in documentation with generic examples.

### Removed
- Removed the merge-queue engine, the `chitra-queue` entrypoint, and the
  queue-management capability surface.
- Removed unused routing provenance fields (`resolved_model`,
  `resolved_harness`, and `routing_hint_source`).
- Removed dead board-updater validators, MCP mapping code, and dispatch stubs.
- Trimmed non-operationalized codes from the executable taxonomy while
  retaining their design context in documentation.

## [0.8.1] - 2026-07-12

A hardening patch, not a feature release. An independent adversarial review
of the two open feature PRs this
consolidates (#54 board-table-colors, #55 graceful-session-pause-resume)
found two BLOCKER-severity defects and five HIGH-severity defects across
dispatch delivery, pause/resume durability, and account-identity handling.
This release fixes all seven, replaces the two PRs (superseded, closed),
and does **not** introduce any new user-facing feature beyond what #55
already proposed — the scope is entirely "make the same proposed behavior
actually durable and correct."

Every fix below is paired with new fault-injection, concurrency, or
kill-point tests that exercise the failure path directly (not just the
happy path) — see the PR description for the full per-finding table.

### Fixed
- **Dispatch queue: frozen orders were silently discarded, not held.** A
  session held for a rate-limit reason now durably defers ordinary orders
  (`queue/deferred/`, no result file written) instead of returning a
  terminal `BLOCKED` result and archiving them to `processed/`. Once the
  hold clears, `chitra.dispatchd.requeue_deferred_for_session` atomically
  returns the backlog to `orders/` in original FIFO order for exactly-once
  delivery.
- **Pause/resume was two uncoordinated writes, not a transaction.**
  `chitra.rate_limit_guard` is now driven by a durable, crash-safe
  transaction outbox (`chitra.rate_limit_state`) walking
  `pause_requested → checkpoint_sent → stop_sent → awaiting_quiescence →
  held → resume_requested → resume_sent`. Every transition consumes a real
  `chitra.dispatchd` delivery result; every waiting phase is bounded by a
  configurable deadline (`PolicyConfig.pause`) with bounded retries, then
  escalates for operator visibility without ever dropping the freeze. A
  pause now enqueues a second, deterministic `/goal clear` stop order after
  the checkpoint is confirmed, then verifies the target session's own
  transcript has gone quiet before recording `held` — a graceful pause
  proves the turn stopped, it does not just label the goal "held". A resume
  enqueues its re-arm nudge, waits for confirmed delivery, and only then
  clears the hold and requeues the deferred backlog — never the reverse.
- **`dispatchd` could double-deliver a nudge on crash or worker race.**
  Orders are now atomically claimed (renamed into `queue/in_flight/`)
  before any delivery attempt, so two racing workers can never both process
  the same order file. A send-nonce marker plus an owner-pid marker let a
  restarted daemon tell a live in-progress claim apart from one abandoned
  by a crashed worker, and reconcile a possible crash-after-paste via the
  same transcript-grep evidence `dispatch_to_tmux` itself uses — never
  blindly re-pasting into a live pane.
- **The rate-limit freeze check ran before the lane lock (TOCTOU), and its
  bypass was an unrestricted public boolean.** The freeze is now checked
  under the same lane-lock hold used for delivery, closing the race window.
  `DispatchOrder.bypass_rate_limit_freeze` is honored only when the order's
  `task_type` is also one of dispatchd's own sealed internal task types —
  an arbitrary queue writer can no longer invent a bypass. `--goals-root`
  is now actually forwarded from the CLI into `run_once`/`run_forever`
  (it was accepted by the parser but silently dropped before reaching
  either).
- **Unknown-account sessions were silently merged.** `chitra.usage.
  evaluate_grouped` no longer groups every blank-`account` session into one
  shared identity — each is isolated so one hot, unknown-identity session
  can never attribute its pause verdict to an unrelated unknown sibling. A
  new `chitra.account_registry` tracks each lane's last-known account
  identity within a bounded freshness window, surfacing a missing snapshot
  or a mid-session account change as an operator escalation instead of
  silently doing nothing. Codex host-wide fan-out remains an explicit,
  documented, fail-closed gap (no per-lane Codex usage snapshot exists yet)
  — never silently attempted.
- **The `goals.json` store was atomic per write but not against concurrent
  writers.** `upsert_goal`, `redirect_goal`, `close_goal`, and every
  read-modify-write helper (`hold_goal`, `resume_goal`, `add_ask`,
  `resolve_ask`, `update_now`) now serialize their full read-modify-write
  transaction with a `flock`-protected critical section, closing the
  lost-update window where a concurrent writer's mutation could be silently
  erased by whichever `os.replace()` landed last.
- **Box-format roster cells overflowed on emoji/CJK content.** `_wrap_cell`
  now wraps by terminal display width (matching `_pad`'s own measurement),
  not `textwrap.wrap`'s code-point count — a wide-character-heavy Goal/Now/
  Needs cell no longer produces a wider physical line than its column.
  Overlong unbroken tokens are hard-split by display width, never by code
  points. The `cards`/`box` default is **unchanged** (still `cards`) —
  the default format remains an open decision; it is now a single named
  constant (`board.ROSTER_DEFAULT_FORMAT`) so resolving that decision
  later is a one-line change.

### Changed
- Version escalation is frozen at the `0.8.x` line. Six minor-version
  increments landed in roughly two days without six independently hardened
  maturity steps behind them; an independent review assessed the honest
  feature maturity at 0.3.2-equivalent. The already-published `v0.2.0`/
  `v0.7.0`/`v0.8.0` tags are immutable and are not being deleted or
  rewritten. Only `0.8.x` hardening patches ship until transactionality,
  idempotence, and evidence-backed status are demonstrated with the kind of
  fault-injection tests this release adds — no `0.9` without an explicit
  maintainer decision.

## [0.8.0] - 2026-07-11

### Added
- Sticky strategic goal records with redirect-only revisions, deterministic specification checks, and policy-configured canonical guidance documents.

## [0.7.0] - 2026-07-11

### Added
- `chitra.convlog` v2 operator briefs now record a plain-language subject and progress summary, render those details as a grounding lead-in, and keep v1 conversation-log entries readable.
- Roster reports now list every unreviewed published artifact by title and complete, copyable URL in deterministic oldest-first order.
- `chitra.capabilities`: a packaged, strictly validated capability manifest with a reversible, time-boxed runtime toggle overlay and `chitra-capabilities` CLI. It exposes only enabled tool commands as MCP-shaped definitions; daemons remain non-toggleable.
- `chitra.merge_queue`: pure caller-supplied merge-queue hygiene decisions, chitra-owned hold markers, an atomic `queue_holds.json` store, append-only `queue_hygiene.jsonl`, and the gated `chitra-queue` CLI. It cannot merge, approve, branch, invoke `gh`, or make network calls.

## [0.5.0] - 2026-07-11

### Added
- `chitra.usage`: account-aware evaluation that attributes fresh rate-limit snapshots to every session on the same account, including stale siblings.

## [0.4.0] - 2026-07-10

### Added
- `chitra.usage`: strict usage-snapshot reading and pure threshold evaluation for Claude Code sidecar files and the local Codex account. It reports `ok`, `approaching`, or `pause` without pausing, resuming, dispatching to, or otherwise deciding for a lane.
- Goal hold bookkeeping: `chitra-goals hold`, `resume`, and `due` preserve the monitor-stated goal while recording an explicit hold reason and optional ISO8601 resume time. Timed holds are listed deterministically for operator review; operator-parked holds are never automatically surfaced as due.

## [0.3.0] - 2026-07-10

### Added
- `chitra.goals`: deterministic, per-lane goal store and roster — records the monitor's stated goal, completion condition, and current status (`working`/`held`/`idle`/`blocked`/`done-pending-verification`/`done-pending-close`) with no LLM call in its own code path. Exposed via the `chitra-goals` CLI (`roster`, `scan-asks`).
- Persistent open-asks tracking: `chitra-goals scan-asks` reads the full last assistant message from a lane's transcript (never a fixed-size pane tail) and, with `--record`, holds each numbered `awaiting ruling`/open-question line in the lane's durable record.
- Operator-facing roster rendering (`roster --format box`) with a color legend, a `Needs` column, a computed marker, and an idle-by-design (🟡) state.
- Receiving-board pipeline reconciliation (`chitra.board_updater` path) so triaged events flow into `facts.json` consistently.
- `task_type → model/harness` routing (`chitra.routing_config`): a structured `routes` config that resolves a concrete model+harness (+zdr) at dispatch and records the resolved selection structurally in the signed ledger, alongside the existing opaque `routing_hint` pass-through.
- `chitra.watchd` tmux pane-change emitter.

### Fixed
- Cross-host confirmation: the remote-dispatch path now expands the remote transcript root and matches delivery markers with local-normalized comparison over ssh, so a delivery to a session on another host is confirmed rather than reported unlocatable.
- Dispatch robustness: pane-capture fallback so an unlocatable transcript is no longer treated as `FAILED`; dimmed placeholder input rows are treated as idle, not a draft; Claude transcript writes are allowed before verify.

### Changed
- Renamed the `POLYPHONY_CHITRA_*` environment variables to `CHITRA_*` (e.g. `CHITRA_LOCAL_HOST`, `CHITRA_LANE_LOCK_DIR`) and the default `/var/lib/polyphony-chitra/` state paths to `/var/lib/chitra/`, so the tool's public interface no longer names an internal project affiliation. If you set any `POLYPHONY_CHITRA_*` variable or rely on the old default paths, update to the `CHITRA_*` names / `/var/lib/chitra/` paths.
- Test coverage for `liveness_check()` (malformed `session_ref`, remote-host assume-live, local-host with/without an attached tmux client).

## [0.2.0] - 2026-07-09

### Added
- Extracted, hardened `chitra.dispatch` tmux delivery library: fixes a missing `-p` bracketed-paste flag and a missing tmux copy-mode check, both silent failure modes in the original internal implementation.
- `chitra.dispatchd`: JSON order/result queue daemon with `LaneLock` single-writer enforcement (crash-safe, no double-delivery).
- `chitra.triaged`: state-transition dedup daemon over a tailed events log.
- `chitra.draft_scanner`: flags unsubmitted drafts in tmux input boxes (flag-only, never submits/discards).
- `chitra.board_updater`: validated `facts.json` writer with backup/rollback.
- `chitra.ledger`: HMAC-signed, append-only delivery ledger — every successfully delivered `[C]`-tagged message is signed and logged automatically, proving both "this was delivered" and "this was never sent."

## [0.1.0] - 2026-07-09

### Added
- Initial internal extraction (pre-public-repo).

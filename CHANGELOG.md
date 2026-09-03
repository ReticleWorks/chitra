# Changelog

All notable changes to this project are documented here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. This project uses [Semantic Versioning](https://semver.org/), currently in the 0.x range (see `docs/DESIGN.md` for why 1.0.0 is reserved for later).

## [0.19.2] - 2026-08-30

### Added

- Tell every governed Claude and Codex lane to maintain one
  AgentTrail-compatible `PLAN.md` in its declared worktree. The setup note
  defines stable task IDs, dependencies, file scopes, intermediate status,
  and evidence-only completion updates while leaving frozen goals and verified
  receipts authoritative.

### Changed

- Move monitord's per-lane state root from
  `/var/lib/polyphony-chitra-<lane-id>` to `/var/lib/chitra/lane-<lane-id>`.
  Existing goals, journals, and receipts are not migrated automatically;
  see the deployment note in `docs/daemons/monitord.md` for where the old
  data stays and what an operator must do by hand before upgrading a host
  that already has lanes running.

### Documentation

- Document the lane-plan path, update cadence, shared syntax, and authority
  boundary in the governed-lane guide and project overview.

## [0.19.1] - 2026-08-27

### Fixed

- Permit governed lanes on Twinridge and retain enough Codex transcript history
  to reconcile signed Dispatchd delivery proofs.

## [0.19.0] - 2026-08-27

### Added

- Bind delegated Kai decisions to validated authority, satisfaction, and request
  digests in the immutable decision attestation.
- Add request-bound, idempotent rearming of provider-native goal and recurring
  loop controls for active lanes.

### Changed

- Publish dispatch orders without replacing a different producer's payload.

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

## [0.18.0] - 2026-08-27

### Added

- Add frozen, per-goal autonomy policies with typed capabilities, targets,
  expiry and quantitative limits. Matching grants now authorize goal-scoped
  action without a second topic-based operator gate.
- Add exact `active`, `paused`, `shelved`, and independently verified `closed`
  lane states, bound worktree and transcript checkpoints, idempotent lifecycle
  transitions, and unfinished-work restoration on resume.
- Add user-supplied canonical knowledge bundles and provider setup receipts.
  Codex uses a profile beside its existing configuration; Claude receives a
  native goal and a session-scoped recurring pursuit loop.

### Changed

- Pursue every actionable finding per monitor pass and retry true transport
  failures without a fixed abandonment count. Deterministic goal, policy,
  scope, and schema rejections now become foreground replanning work.
- Pursue an idle lane after one clean monitor pass by default. Per-goal policy
  can set a different threshold after evidence supports it.
- Run Claude's recurring in-session enforcement hook every five minutes by
  default, with a per-goal interval override.
- Flag repeated identical tool outcomes and unchanged test runs on their second
  occurrence, the first point where a loop exists.
- Grant every goal-scoped capability by default, including spending. A goal
  can still freeze narrower targets, amounts, units, expiry, or capabilities.
- Gate Dispatchd on the authoritative lane lifecycle while keeping it the sole
  terminal writer. A typed pause-prune control is the only paused-lane write.

### Removed

- Remove the persisted round-robin finding cursor, per-pass action cap,
  terminal retry exhaustion, duplicate lifecycle constants, and the unused
  standalone checkpoint writer.
- Remove the legacy archive alias. Lane state is exactly active, paused,
  shelved, or closed.

## [0.17.0] - 2026-08-27

### Added

- Bind every monitored transcript to one exact session, lane, client version,
  and frozen goal through a validated manifest.
- Persist corrective intent, queue publication, signed delivery consumption,
  retries, progress boundaries, and verified completion in a crash-safe
  per-lane supervision ledger. A durable round-robin cursor prevents one
  recurring incident from starving later findings.
- Answer routine goal questions and explicit small reversible design questions
  from the frozen contract. Question intent, retries, delivery, and consumption
  survive restarts. Dispatchd recomputes each answer before delivery.
- Run completion validators only after a structured completion claim and mark
  completion only after the exact stored results independently verify.
- Create one bounded pursuit incident after three clean monitor passes with no
  scoped progress, while suppressing it for pending delivery or questions.

### Fixed

- Stop elapsed time and historical findings from advancing the correction
  ladder without genuine post-consumption recurrence.
- Isolate validation receipts by exact goal session so matching receipt names
  from another goal cannot close the lane. Safely migrate unambiguous legacy
  receipts while rejecting same-name cross-session ambiguity.
- Reject stale, held, completed, or forged goal-bound orders before pane I/O,
  including a second goal check under the lane lock. Bound terminal transport
  retries across restarts and fail closed on a newer goal-store schema.
- Reject unsafe queue identifiers and unconfined receipt validator targets.
  Serialize goal writers with dispatch, and never turn a pre-existing SENT
  result into delivery proof during crash recovery.
- Require persistent-oversight deliveries to use the exact transcript manifest
  binding and bind their signed ledger rows to that transcript's native session
  identity.
- Filter historical journal rows against the complete current transcript
  binding after a lane or native-session rebind.
- Reject symlinked or external queue paths and ignore validator registries from
  receipt upload directories. Connect the shipped monitor and dispatch service
  examples to the same per-lane state, queue, ledger, and binding manifest.

## [0.16.0] - 2026-08-23

### Added

- Compose journal ingestion, detectors and ladder, enrollment receipts, and
  presence into one `monitord` entrypoint with shadow-mode findings on by
  default.
- Register `monitord` in the capability manifest so its command surface is
  declared like every other daemon.
- Ship a single chitra-monitord@ .service.example instance-template unit
  alongside the kept dispatchd units.

### Deprecated

- Mark `watchd`, `triaged`, and `sweepd` as deprecated by the composed
  `monitord` entrypoint. They remain installed for existing declarations; no
  new daemon beyond `monitord` and `dispatchd` will be added.

## [0.15.0] - 2026-08-22

### Added

- Review turns without completion words when they contain a question, make no
  tool calls, or follow a delivered dispatch.
- Resolve exhaustion evidence handles against transcripts, ledgers, and command
  results before accepting a brief.
- Run registered completion validators and record their observed receipts instead
  of trusting a lane's self-reported result.

### Fixed

- Share one grounded, nonce-fenced reviewer contract between lane and monitor
  review paths.
- Read newer goals schemas without crashing and keep daemon writes gated to the
  installed schema unless migration is explicit.
- Apply structural, stateful stop-guard decisions across repeated turns and
  remove the redundant word-list gates.

## [0.14.12] - 2026-08-22

### Fixed

- Bind dispatch delivery-ledger rows to the adapter-native session identity
  normalized from the confirmed lane transcript (signature version 5,
  never derived from routing_hint), so genuine deliveries advance the W3
  ladder while cross-session evidence fails closed.
- Make rescue checkpoint receipts single-create and their consumption
  atomic: a seal now durably spends the receipt's reference and anti-replay
  nonce under the incident lock before appending, rejecting duplicate or
  replayed receipts exactly once ever, including after restart.

## [0.14.11] - 2026-08-22

### Fixed

- Tighten W3 detector and ladder semantics: canonical event-ID progress resets,
  real worktree containment, claim-aware false completion, signed immediate
  consumption boundaries, sealed RESCUE/checkpoint relaunch gating, and
  fail-closed rescue evidence capture.

## [0.14.10] - 2026-08-22

### Added

- Add W3 canonical-journal detectors for drift, unnecessary steps,
  excessive testing, document dithering, and false completion, plus the
  consumption-bound response ladder, RESCUE bundle capture, relaunch brief
  generation, injected failure fixtures, and false-positive controls.
- Add topology conversion tooling that preserves legacy goal and dispatch
  queue hashes, marks legacy goals display/dispose-only, emits read-only
  shadow findings, and records per-instance handoff and disposable rollback
  receipts without touching live hosts.

## [0.14.8] - 2026-08-22

### Fixed

- Presence records now serialize the contractual `mode` field with value
  `using` or `released`, as DESIGN-v3 section 5 requires, instead of a
  differently named `state` field; every other record binding and the
  append/merge behavior are unchanged.
- Direct peer questions now enter the existing governed session-message path:
  `chitra-peer say` enqueues a real dispatch order bound to the target session
  and text for `dispatchd` to deliver and verify. The shared-dir mirror is
  non-authoritative, and locally recorded receipts only ever mirror
  dispatchd's own durable results, so they can no longer claim delivery that
  did not happen.

## [0.14.7] - 2026-08-22

### Fixed

- Presence records now bind each declaration to its session, goal references,
  purpose, and journal event reference, as DESIGN-v3 section 5 requires.
- Peer messages now record a dispatch receipt on delivery and a consumption
  receipt when the peer consumes, instead of bypassing the governed
  session-message path.

## [0.14.6] - 2026-08-22

### Added

- Add per-instance, append-only advisory presence for shared resources. Readers
  merge every writer file and report peers without locks, expiry, stealing, or
  exclusive claims.
- Add atomic file-per-message peer inboxes with stable ordering and idempotent
  retry IDs. The `chitra-presence` and `chitra-peer` commands expose both
  library surfaces without adding a daemon or socket.

### Fixed

- Preserve nanosecond modification times with an inode tie-breaker when
  ordering dispatch queues, so same-tick writes retain FIFO order on Linux.

## [0.14.5] - 2026-08-22

### Fixed

- A same-inode rotation observed while the transcript holds no completed
  records (for example a truncate-to-zero rewrite) now also restarts the
  journal ingestor's normalizer replay state. A record restored verbatim
  after such an empty rotation keeps its original event ID instead of
  being appended again as an apparent second occurrence.

## [0.14.4] - 2026-08-22

### Fixed

- A detected same-inode rewrite now replays duplicate-free through the
  journal ingestor: replay restarts the normalizer's per-incarnation
  occurrence numbering, so unchanged records reproduce their original
  event IDs and the durable journal no longer appends duplicates of
  already-stored events. Rotation across a different inode keeps
  continuing the stream's numbering, preserving both W11 fixture
  projections.

## [0.14.3] - 2026-08-21

### Fixed

- Completed whitespace-only JSONL lines are now hash-covered like every
  other consumed byte range: their range and digest are recorded when
  consumed, so a same-inode, same-size rewrite of a blank line into a
  valid record can no longer be silently skipped while the final anchor
  stays unchanged. Any such mismatch rotates the same inode and replays
  the transcript.

## [0.14.2] - 2026-08-21

### Fixed

- Same-inode, same-size rewrites at any record position are now always
  detected: every poll re-verifies the stored hash of every consumed
  record at its byte range (plus buffered partial bytes), removing the
  16-record sampled verification that could silently skip an unsampled
  interior record in journals longer than 16 records. Any mismatch
  rotates the same inode and replays the whole transcript.

## [0.14.1] - 2026-08-21

### Fixed

- Same-inode rewrites are no longer missed when file size and the final
  64 bytes are unchanged: each poll re-verifies stored record hashes at
  their byte ranges (first and last records always, a deterministic
  sample of earlier records, plus buffered partial bytes) and replays
  the transcript on any mismatch.
- Receipt verification now fails closed on every validator, not only the
  Polyvalidation Rig: a `PASS` receipt must bind a hash-bound
  `chitra-validator-report-v1` report whose command and exit code support the
  claim, checked at ingest and again at close. A self-asserted `PASS` over a
  failing report can no longer close an item.
- The generic verifier no longer takes a PASS result from caller-authored
  report text alone. A claimed exit code must be re-established by an
  independent trusted execution of the declared exercise command in a
  verifier-controlled environment, so a hash-consistent report that lies about
  a failing command can no longer close an item.
- Receipts are stored at `<state-root>/validation-receipts/<receipt_name>.json`
  as DESIGN-v3 section 2 requires, without the undocumented session-hash
  directory.

## [0.14.0] - 2026-08-21

### Added

- Add fixture-gated Claude Code 2.1.229 and Codex 0.149.0 transcript
  normalizers with stable canonical event IDs and native call/result joins.
- Add a byte-accurate JSONL tail reader that retains partial appends, follows
  same-inode resumes, and switches cleanly across rotation.
- Add append-only per-lane event and progress-derivation journals under each
  instance state root. Native records remain intact for replay.
- `chitra-receipts ingest`, `list`, and `verify` manage immutable per-lane
  validation receipts under the instance state root. Ingest verifies the W12
  nine-field envelope, its canonical digest, and every copied evidence hash.
- Receipt verification checks current artifact or commit identity and validates
  PVR report-to-audit bindings when the receipt names the Polyvalidation Rig.

### Changed

- Done transition and completion close now reload the exact frozen receipt from
  the lane store. Only a verified `PASS` with validator acceptance and no
  unexercised surface counts; caller flags and claimed result text cannot
  replace it.

## [0.13.0] - 2026-08-21

### Added

- Goal enrollment now requires one atomic four-question interview result with
  a typed receipt and at least one frozen structured done item. Each item names
  its validator and the exact completion receipt it requires.
- Completion proofs bind exact done-item IDs to receipt names, validators,
  passing results, and concrete citations. Watchd persists the validated proofs
  before a done transition, and completion close repeats the same check.

### Changed

- A first `chitra-goals set` prints `INTERVIEW_REQUIRED` JSON with a persisted
  nonce and writes no goal. The paired `--interview-result` call performs the
  enrollment. Goals schema v3 keeps v1/v2 records readable but limits them to
  display and reasoned administrative disposal.
- Free-form delivered items and operator acknowledgements no longer satisfy a
  completion close. Administrative discard remains separate and requires a
  reason that is logged as not done.

## [0.12.4] - 2026-08-21

### Fixed

- Watchd bounds every tmux subprocess call. A hung tmux server now returns a
  logged timeout failure instead of stopping the poll loop indefinitely.
- Watchd, sweepd, and triaged send systemd readiness and watchdog datagrams.
  The watchdog signal is enabled only when systemd supplies `WATCHDOG_USEC`.
- The systemd units use `Type=notify` and a watchdog limit set to three times
  each daemon's normal poll interval. No extra heartbeat state files are
  written.

## [0.12.3] - 2026-08-21

### Fixed

- Dispatch verifies that a pasted message leaves the composer. Codex gets its
  kitty-keyboard Enter fallback only when the marker remains in an idle
  composer; a marker that still remains fails the order.
- Delivery becomes complete only when one lane transcript contains the user
  marker followed by agent or tool activity. System records and activity in a
  different transcript do not count.
- A send nonce now creates a verify-only state. An unresolved nonce never
  causes the message to be pasted again, and exhausted verification retries
  still fail with a critical log.
- A pre-existing composer draft remains blocked and is never flushed by
  default.

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

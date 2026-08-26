# Chitra transcript harvest and revision plan

The harvest found one tested but incomplete product change, five blocked lanes, one unfixable lane, and one unfinished benchmark lane. The safest first product step is to preserve CH-07’s tested redirect behavior while replacing its prose-only route with machine-readable ownership evidence. The highest-value harness step is to separate valid patches, product regressions, infrastructure faults, and model-output failures before a lane can claim success.

Status: complete for the supplied exports. I read all eight JSON files with `jq`, checked the defect-register corrections, recovered the available patch material, and wrote sanitized artifacts under `patches/`. I did not edit a Chitra checkout, push, or open a pull request.

## Evidence rules

`ESTABLISHED` means the transcript shows a source read or a recorded test result. `REPORTED` means the lane says it ran the check, but this harvest did not rerun it. `UNVERIFIED` means the claim needs a new check before implementation. `REJECTED` means the candidate was inspected and should not be applied.

The repeated parent baseline was `1207 passed, 3 skipped`, as reported by the lanes. CH-07 additionally reported two focused parent failures, two focused branch passes, and the same full-suite result on both sides. Those are transcript evidence, not a fresh run in this harvest directory.

## Defect-register reconciliation

The register changes the priority and ownership of several lane conclusions.

| Register evidence | Effect on this plan |
|---|---|
| D18 says, “The watchd-to-monitord migration dropped enforcement.” | This is accurate about source reachability: `watchd` reaches `review_watched_session`, while `monitord` does not. It is not enough to identify the live production failure. |
| D24 corrects that with, “watchd IS in production; monitord is NOT deployed.” | Do not rank a monitord migration as a production fix until deployment ownership is settled. |
| D25 first names a dead tmux socket, then D25 CORRECTED says, “the tmux socket fix IS loaded; the WATCH LIST is stale.” | The live configuration is stale roster data. The register says this belongs in the fleet role that manages the service, not in an on-host edit or an untested Chitra code change. |
| D19 says the interview receipt “checks shape, never truth.” | This confirms CH-02. The receipt gate is real but caller-mintable. |
| D10 says the earlier interview fix shipped to main in a later pull request, while the CH-02 campaign framing called the earlier pull request “closed unmerged.” | The campaign framing is contradicted. The live placeholder test still proves that the shipped mechanism did not establish truth. Treat the mechanism as superseded, not the defect as resolved. |
| D10b says idle detection is absent and warns not to revive elapsed-time escalation. | This confirms CH-03’s product target and rules out copying the old wedge design. |
| D21 retracts the theory that maximum reasoning is itself broken. D22 measures truncation against reasoning depth, not prompt size. D23 says decomposition fixes generation but not correctness. | Keep substantive Ox calls at maximum reasoning where required. Improve decomposition and validation instead of globally lowering effort. |
| D8 records a false pytest-gate failure caused by missing import setup. | CH-08’s infrastructure verdict must be separate from a product regression. |

## Agent harvest

| Agent | Established findings | Tests and delivery state | Why it did not deliver |
|---|---|---|---|
| CH-01, monitord enforcement | `review_watched_session`, `ClaudeProcessReviewer`, `SessionReviewSignal`, `build_reasoned_dispatch`, and queue persistence exist. The current monitord path does not call the review path. The candidate also exposed three concrete API facts: canonical event payloads are dictionaries, `WatchedSessionBehavior.from_turn` needs the session reference and message, and the signal field is `behavior_sha256`. A matching review signal is persisted before the caller enqueues a corrective order. Reusing that signal as a terminal “already reviewed” result loses enforcement after a crash and across shadow-to-live transition. | Parent suite reported `1207 passed, 3 skipped`. No focused test or product change remained. Partial import and candidate patches are in `CH-01-02.patch` through `CH-01-04.patch`. | Model-output failure: malformed context, a disabled-tool transcript, then invented fields. The target also has a deployment-layer risk: D24 and D25 contradict the assumption that monitord is the live daemon. |
| CH-02, interview truth | `InterviewReceipt` contains caller-selected name, timestamp, digest, and provenance. The public CLI gives the enrolling caller the nonce and accepts caller-authored answers and provenance. The live reproduction accepted both the exact placeholder claim and a structurally plausible receipt through public `upsert_goal`. A nonce proves freshness, not actor identity. HMAC verification in the enrolling process would give that process the same key needed to forge a receipt. | Parent suite reported `1207 passed, 3 skipped`. No regression test or product patch was shipped. | Layer limitation, not a missing keyword rule: the enrollment boundary has no non-caller-mintable fact. A real fix needs an independently authenticated producer and a verifier boundary. |
| CH-03, stuck detection | Main has no `detect_idle`, `detect_wedge`, or `detect_stuck`. Monitord records existing findings but does not load durable progress rows, detect flat progress across completed turns, or perform the requested nudge, restart, and mark/surface ladder. The supplied contracts already expose canonical event ids, `FINAL_RESPONSE`, `ProgressClassification`, `ProgressClass`, and `Finding.event_refs`. | Parent suite reported `1207 passed, 3 skipped`. No source or test change remained. The decomposition and adaptation log were local harness artifacts. | Model-output and API-validation failure: shell commands instead of diffs, invented event and progress fields, stale hunks, then another shell command. The target itself remains valid. |
| CH-04, false blocker | Neither daemon imports the evidence or conversation-log modules. The only caller is the manual conversation brief command. A safe detector must act only on an explicit, mechanically named path, environment variable, or configured credential; probe existence or nonempty state without reading or logging secret values; and leave true blocks and broad claims alone. The proposed `LaneCredentials` type and final detector signature were not established in the source and are therefore `UNVERIFIED`. | Parent suite reported `1207 passed, 3 skipped`. No source or test change remained. The test candidate was malformed. | Model-output failure: invalid hunk locations, corrupted diff syntax, bad hunk counts, and an inverted assertion. The detector layer can decide named local contradictions, but it cannot decide a broad claim with no named resource. |
| CH-05, stale spinner | `classify_snapshot(snapshot, *, agent, repository)` receives one snapshot and has no temporal evidence. A protected test requires a stale answered prompt above a live spinner to remain working. The issue requires a stale spinner below a real prompt to become blocked. `RuleEvaluation` does not retain enough blocker detail for `observe` to reconstruct the lost decision from that object alone. Existing broker state includes a snapshot hash and session identity. | Parent suite and focused new gate were not claimed. A one-keyword signature change was applied and then removed. | Both reasons apply. The target is wrong for a classifier-only fix, but the caller can decide with successive snapshots. The model then produced commands, invented unrelated context, and never produced the condition change. |
| CH-06, canonical choices | No registry or resolver exists. `GuidancePolicy.canonical_decisions` maps working-directory prefixes to document paths; it does not model typed choices or check actions. `PolicyConfig` is the nearest declaration boundary. A useful first slice is `deprecated_path` with exact normalized path equality and explicit write-tool fields. Other kinds need separate evidence contracts. | Parent suite reported `1207 passed, 3 skipped`. The schema-only change was applied and removed. The resolver envelope was not applied. | Model-output and API-validation failure: invented registry semantics, nonexistent modules and fields, forbidden patch markers, fenced diffs, and a payload-object mistake. The target is valid, but the five-kind scope was too broad for one candidate. |
| CH-07, answer or redirect | `session_scope_violation` has an allow-list and deny-list, but no namespace-to-dispatcher registry. Allowed matches reach real dispatch; unowned and denied namespaces remain blocked. The delivered change replaces dead-end text with an ownership-evidence gap and a generic sender/router or configuration-maintainer route. It does not select another dispatcher, so the route is human-readable rather than machine-executable. | Known gate result: two focused tests failed on parent and passed on the branch; the full suite reported `1207 passed, 3 skipped` on both parent and branch. The lane reported commit `823a43335a7ea2c8d92604feb1420661a20df27f` and a push. This harvest did not push or alter that commit. | Delivered according to the four reported gates. Remaining risk is scope: a redirect string is not a routing action. Preserve the tested behavior, then add an ownership resolver with neutral fixtures. |
| CH-08, benchmark tripwire | The frozen evaluator is outside the product source and hard-codes its evaluator path. Its child pytest gate can fail from missing `PYTHONPATH`, which must not zero a score as if dispatch were broken. The runner needs a temporary candidate copy, the pinned evaluator hash, candidate `src` first in inherited `PYTHONPATH`, raw process output, and distinct classification for baseline, regression, infrastructure fault, and selftest coupling. The export ends after a tool result and has no terminal verdict. | The added test failed at collection because `chitra.benchmark_tripwire` did not exist. The partial module only materialized the evaluator and environment; it never defined `run_frozen_benchmark`. No end-to-end baseline or broken-copy proof exists. | Harness and model-output failure: one empty response from reasoning-budget exhaustion, invalid incremental patches, a wrong evaluator path, contract drift, and an unapplicable execution patch. The product target was mislocated under `src/chitra`. |

## Recovered patch artifacts

The artifacts are evidence records, not a claim that every file is safe to apply. Files with a `STATUS` line are deliberately marked partial, malformed, rejected, or harness-only. CH-07’s production diff is recovered in full. Its test patch is sanitized because the transcript-specific fixtures contained session and namespace identifiers; the assertions retain the behavior without those identifiers.

| Artifact | Content |
|---|---|
| `CH-01-01.patch` | Malformed import candidate, preserved with a marker. |
| `CH-01-02.patch` | Partial review-path import hunk that the lane later removed. |
| `CH-01-03.patch` | Candidate that applies but treats the payload as a string and calls `from_turn` with the wrong arity. |
| `CH-01-04.patch` | Candidate with the corrected payload and arity but invented `behavior_hash` and `hash` fields. |
| `CH-02-90.patch`, `CH-03-90.patch`, `CH-05-90.patch`, `CH-07-90.patch`, `CH-08-90.patch` | Repeated local adapter change that explicitly disables the `agent` tool in chat mode. |
| `CH-03-01.patch` | Marker for four invalid stuck-detector candidates; no product hunk existed. |
| `CH-04-01.patch` | Marker for the malformed test candidate; no applicable code hunk existed. |
| `CH-05-01.patch` | Signature-only micro-change that was applied and then removed. |
| `CH-06-01.patch` | Partial typed registry schema, applied and then reverted. |
| `CH-06-02.patch` | Rejected complete-file envelope with nonexistent imports and object-style payload access. |
| `CH-07-01.patch` | Full sanitized production diff for the namespace redirect behavior. |
| `CH-07-02.patch` | Sanitized focused-test diff for the unowned and denied cases. |
| `CH-08-01.patch` | Partial tripwire test added before implementation. |
| `CH-08-02.patch` | Partial materializer module without the runner entry point. |
| `CH-08-03.patch` | Marker for the unfinished final export. |

## Destination A — changes to the Chitra repository

Apply these in order. Each item names the product files, the smallest behavior to change, and the evidence that justifies it.

### A1. Keep the CH-07 redirect, then make ownership resolvable

Files: `src/chitra/dispatchd.py`, `tests/test_dispatchd.py`, and a new small ownership-policy module only if the existing configuration has no suitable home.

The current function correctly preserves deny-before-allow ordering and keeps allowed dispatch working. Retain the sanitized behavior in `CH-07-01.patch`. Replace the prose-only route with a machine-readable decision containing a stable rejection code, the missing ownership evidence, and a resolver input. Add a resolver only when the system has an authoritative namespace-to-dispatcher mapping. If no mapping exists, keep the result blocked and return a durable redirect request rather than inventing an owner.

Tests must prove three cases with synthetic names: an allowed namespace is sent, an unowned namespace is blocked with a stable redirect code, and an explicit deny remains blocked with policy-maintainer remediation. The existing CH-07 gate is the best evidence-to-risk ratio, but the transcript proves only explanatory output, not actual rerouting.

Destination: Chitra repository. The behavior is in `dispatchd.py`, and the existing parent-fail/branch-pass evidence is product-level.

### A2. Move stale-spinner judgment to the broker boundary

Files: `src/chitra/agent_runtime.py`, `src/chitra/agent_status.py`, and `tests/test_agent_status.py`.

Keep `classify_snapshot` pure over one snapshot. Add a caller-provided stability fact with an honest first-observation default, or perform the final state choice in `AgentStatusBroker.observe`. Retain prior snapshot hash and session identity in memory. Reset the evidence on session or pane identity change. Flip the ambiguous working-over-blocked case only after the chosen consecutive-identical-snapshot threshold. Do not use line position or elapsed time.

Tests must preserve the protected live-spinner case, add a frozen-spinner-plus-real-prompt case, add a changing-spinner case, and prove that evidence does not cross a session identity change. Run the focused test against parent and branch, then run the full suite on both.

Destination: Chitra repository. D23 establishes that the classifier cannot decide from one snapshot, while CH-05 identifies the broker as the layer with temporal evidence.

### A3. Add a narrow false-blocker detector and wire it into monitord

Files: `src/chitra/detect/detectors.py`, `src/chitra/monitord.py`, and focused tests in `tests/test_monitord.py` or a new neutral test module.

Start with explicit claims about a named absolute path and an injected environment variable. Read only existence or nonempty state. Never read, log, or return credential contents. Do not infer a resource from generic words such as “credentials” or “auth.” Abstain on permission errors, relative paths without an explicit working directory, questions, conditionals, reported speech, and claims with no named resource. Use the current `CanonicalEvent.payload` dictionary shape and the actual `Finding` constructor; do not copy the unverified `LaneCredentials` API from CH-04.

Add the detector to the existing deterministic order. Confirm that `reasoned_dispatch.py` already handles `false_blocker` before changing it. Test a true missing resource, a contradicted existing resource, a nonempty environment variable, an empty variable, a broad claim, and a permission-error abstention. Then test that a false-blocker finding reaches the existing corrective path.

Destination: Chitra repository. D20 and CH-04 establish the missing daemon composition seam. The detector can decide only explicit local contradictions; the session cannot decide its own broad claim without a named probe.

### A4. Build progress-based stuck detection before adding actions

Files: `src/chitra/detect/detectors.py`, `src/chitra/detect/__init__.py`, `src/chitra/monitord.py`, `src/chitra/detect/ladder.py` only if the existing ladder cannot represent the actions, and `tests/test_w3_detect.py` plus `tests/test_monitord.py`.

First implement a pure `detect_stuck` over completed `FINAL_RESPONSE` boundaries and durable progress classifications. Use a stable fingerprint seed independent of the later event list. A `PROGRESS` row between boundaries resets the streak. `NON_PROGRESS` and `UNKNOWN` do not create progress. A long in-flight tool call and fewer than the threshold of completed turns stay clear. No timestamps, process checks, silence checks, or elapsed-time actions may create a finding.

Only after the detector passes controls should monitord derive or load durable progress rows and include the finding. Then implement the logged ladder: nudge, governed restart or relaunch, and durable mark-and-surface. Shadow mode must record without acting. Use the existing rescue, checkpoint, and dispatch contracts; if a step needs a new function, keep it one behavior per candidate.

Destination: Chitra repository. D10b and the explicit elapsed-time ruling establish the need. CH-03’s failures were generation failures, not evidence that the target is wrong.

### A5. Wire existing review enforcement only after deployment ownership is settled

Files: `src/chitra/monitord.py`, `tests/test_monitord.py`, and possibly `src/chitra/watchd.py` only if the selected production daemon needs a shared helper.

Before coding, confirm which daemon is deployed and which session roster it watches. D24 and D25 say the live service is watchd and its roster is stale. If the deployment remains watchd, test and repair the existing watchd boundary. If the fleet intentionally moves to monitord, wire the review path there and add the deployment change outside this repository. Do not treat a source-only monitord fix as live enforcement.

In the code, use the actual dictionary payload, pass the session reference to `WatchedSessionBehavior.from_turn`, and use `behavior_sha256`. Separate “review signal persisted” from “corrective order enqueued.” A crash or shadow pass must not permanently suppress later live enforcement. Reuse a signal only when behavior and frozen goal match, and use a durable order or enforcement marker for idempotence.

Destination: Chitra repository for the code and tests. Deployment roster changes belong to the fleet role that manages the service and are not a local adapter or product-source change.

### A6. Add canonical choices in a narrow, typed slice

Files: `src/chitra/policy_config.py`, a new `src/chitra/canonical_choices.py`, `src/chitra/monitord.py`, and focused policy/detector tests.

Keep the partial `CanonicalChoice` and `CanonicalChoicesPolicy` idea from `CH-06-01.patch`, but validate stable registry keys independently from `subject`. Start with `deprecated_path`: only explicit write-tool events, explicit target fields, deterministic lexical normalization, and whole-path equality. Do not use free-text or substring matching. A relative target without event-local `cwd` is not evidence. Treat `canonical_value` as the required replacement and validate it rather than discarding it.

Add compliant controls for reads, prefix-sharing siblings, and the approved replacement path. Then wire findings to the existing corrective path. Add `pinned_version`, `host_role`, `model_route`, and `required_path` only as separate contracts with explicit event fields and a defined observation window. Do not claim that one event stream proves an unmet required path forever.

Destination: Chitra repository. CH-06 established the missing declaration boundary and the need for exact event-shape handling. The resolver candidate is not reusable because it invented modules and payload objects.

### A7. Add only the product side of truthful interview verification

Files: `src/chitra/goals.py`, `src/chitra/goals_cli.py`, and focused goal tests, after the trust boundary is available.

Do not patch `validate_enrollment_contract` with keywords, nonce-only checks, a hidden parameter, or reject-all behavior. Change the product contract so it accepts an attestation carrying authenticated producer identity, session or request nonce, question-set identity, and exact answer digest. Verify it with a public key or a protected verifier service. The private signing authority must never run in the enrolling agent’s process.

Tests must submit the exact placeholder through public `upsert_goal` and reject it. A second test must use an attestation minted by the independent producer and accept it. A third test must reject a validly shaped attestation with the wrong session, question set, or answer digest.

Destination: split product and authority. The public-key verification and receipt schema belong in Chitra. The private producer and authenticated operator channel belong in the local adapter or operator service. CH-02 establishes that the current product layer cannot truthfully create the missing fact.

## Destination B — changes to the local adapter and campaign harness

These changes repair campaign control. They must not be added to `src/chitra` or to Chitra fixtures.

### B1. Make artifact validation semantic and parent-aware

Add a local candidate validator that requires a raw unified diff or a complete-file envelope, checks that the patch applies to the supplied source, verifies hunk arithmetic, imports, function names, field names, and syntax, and rejects destructive rewrites. A diff that starts with `--- a/` is not valid evidence. Run the focused test against the parent before applying a candidate, then against the candidate, then run the full suite on both. Record parent failure, branch pass, and full-suite results as separate fields.

This directly addresses CH-01, CH-03, CH-04, CH-05, CH-06, and CH-08. D23 proves that an API-correct diff can still break a protected test.

### B2. Treat model-output failures as typed outcomes

The adapter should record separate outcomes for tool text in chat mode, malformed format, non-applicable patch, invented API, protected-test regression, empty budget exhaustion, and valid candidate. A disabled-tool configuration is necessary but did not stop several models from emitting fake shell transcripts. The wrapper must reject those outputs without treating them as source evidence.

On budget exhaustion, do not retry the same request. Change the behavior requested, the evidence supplied, or the decomposition. Keep maximum reasoning for substantive work unless a measured, low-risk control says otherwise. D21 rejects a global maximum-reasoning rollback; D22 says the cause is reasoning depth, not prompt length.

### B3. Centralize runtime bootstrap and health gates

Provision the repo-local runtime once per campaign, verify the archive and adapter hashes, verify chat mode has no allowed tools and explicitly disables `agent`, and cache the result. Emit a machine-readable health record with model, provider, effort, retry count, and runtime versions, without credentials or full environment data. The supervisor must gate on that record, not on hand-written prose.

This repairs the repeated per-lane bootstrap and D3’s prose-only provider health condition. It also makes the local `agent`-disable patch a harness change rather than a repeated lane-side mutation.

### B4. Separate benchmark infrastructure from score verdicts

Put the CH-08 tripwire in the campaign harness. It should copy a candidate to a temporary root, run the byte-pinned evaluator from its actual frozen relative path, prepend the temporary `src` to inherited `PYTHONPATH`, capture raw return code/stdout/stderr/JSON, and classify these outcomes separately: baseline held, genuine score regression, evaluator selftest coupling, and infrastructure failure. The `ModuleNotFoundError` pytest-gate case is infrastructure evidence. The hard-coded clean dimension scores are admission-time coupling. Neither is a dispatch regression.

Prove the harness against the frozen baseline and a deliberately broken copy. Do not optimize the score and do not modify the evaluator. The CH-08 product-location artifacts are recovery evidence only and must not land in Chitra.

### B5. Make the supervisor preserve evidence and use real terminal states

Before a lane exits, preserve its branch diff, test output, and final structured result. Do not reset or revert an unharvested branch. Read the final result record rather than searching for the substring `TERMINAL-FIXED` or `TERMINAL-BLOCKED`; D22 says both strings appear in every contract. Keep `budget_exhausted`, `unfixable_here`, `blocked`, and `fixed` distinct.

Require the executing stop rule: twelve valid evaluations, or five consecutive valid non-improvements after at least eight. Distinguish a genuine plateau from failure to generate a valid candidate. D1 and D16 show why a self-written completion marker cannot establish campaign completion. D7 shows that automatic cleanup erased recoverable evidence from nine lanes.

## Out-of-scope deployment correction

The register’s corrected D25 finding says the managed watch roster is stale. The repair belongs in the fleet configuration role that owns the service, under its normal review and converge path. It is not a safe unilateral edit to Chitra source and it is not a local adapter patch. Confirm the intended roster and blast radius before changing it.

## Privacy scrub audit

The patch artifacts contain no transcript-derived personal or private material. I scrubbed or omitted the following source material:

| Source | Scrubbed material | Replacement |
|---|---|---|
| CH-01 through CH-08 metadata and tool records | Absolute checkout paths, file URIs, temporary scratch paths, cache paths, and home-directory paths | Relative `a/` and `b/` patch paths, or omission |
| CH-04 and CH-08 test material | Email-shaped fixture literals, including the four synthetic-looking addresses found by the audit | Omitted; no email fixture is needed |
| CH-07 test and dispatch records | Session references, namespace prefixes, order ids, call traces, and pane-specific fixture names | Neutral semantic assertions and synthetic names described only by role |
| Defect register D25 and related transcript context | Host names, service-instance names, tmux roster names, and operator session titles | “managed watch roster,” “live service,” and “synthetic session” |
| All transcript patch paths and source links | Repository URLs, local file URLs, line-linked private paths, and package download URLs | Omitted |
| Runtime and auth records | Credential values, token values, credential-file paths, and full environment dumps | Presence-only or generic “credential store” wording |

The only credential-related evidence retained is the product requirement that a verifier must not expose a signing secret or credential value. No patch, test, fixture, comment, or commit message in the recovered Chitra-targeted artifacts contains a credential value.

## Recommended execution order

1. Land the sanitized CH-07 behavior with a machine-readable ownership decision and neutral tests.
2. Implement the CH-05 broker-level temporal evidence fix and run the protected parent/branch gate.
3. Add CH-04’s narrow named-resource detector and prove its corrective-path reachability.
4. Build CH-03’s progress detector and action ladder in separate candidates.
5. Revisit CH-01 after the fleet deployment target is confirmed; implement CH-02’s authority boundary and CH-06’s registry as separate architectural work.

Nothing in this plan authorizes a push, merge, pull request, deployment, on-host configuration edit, or credential login.

# Chitra Concepts

Chitra has two distinct layers: a deterministic core that makes zero LLM calls, and an LLM-judgment layer where specific gates make decisions about completion claims and goal enforcement.

## The Deterministic Core

Everything on chitra's delivery, ledger, routing, rate-limiting, and ownership-checking path is deterministic Python. No model evaluates any input. No LLM is consulted about what to send or when. This core loop is auditable, crash-safe, and repeatable.

### Dispatch

Dispatchd drains a JSON order queue (one order per file), delivers messages to tmux sessions, and records results. The delivery path is:

1. Acquire an exclusive file-based lock (LaneLock) per session. This prevents two writers from racing and corrupting the same session's input.
2. Load the message text from the order file.
3. Use `tmux load-buffer` and `tmux paste-buffer` to inject the text into the session's input line.
4. Verify delivery by grepping the session's transcript for the exact text. "Looks sent" is not evidence; grep confirms it actually arrived.
5. Write a result file and sign an entry to the ledger (HMAC-SHA256).

Dispatchd is idempotent: once a result file exists for an order, it is never redispatched, even across a restart. A crash between paste and result is reconciled with a send-nonce marker, not a blind second paste.

### Routing

Routing maps task types to routing hints. A dispatch order carries an opaque `task_type` tag. If a routing config is set, dispatchd looks up that tag and fills in a `routing_hint` (a model/harness preference the caller's system uses). An explicit `routing_hint` on the order always wins over the config table. Chitra records the hint in the ledger but never acts on it — your caller's system decides what to do with the hint.

This is pure dictionary lookup. No content is ever evaluated. Chitra makes no judgment about which task types or models are appropriate.

### Rate Limiting

Rate-limit-guard pauses and resumes sessions using a durable transaction ledger. Every pause phase is recorded with a reason and timestamp. A session holds in place if:

- The account is approaching provider limits (e.g., 92% of Claude's 5-hour window used).
- Host load pressure is high (memory available dropping, CPU/memory pressure sustained).

All nudges (the messages sent to a paused session) are fixed canned templates. They are never LLM-authored. Chitra never drafts prose to explain the pause. It just enforces the decision and records the reasoning.

### Ownership

Ownership-provider is a read-only fence answering whether a given host:lane:instance reference belongs to a canonical managed lane. It does not discover sessions or scan processes. It only reads two files:

- `goals.json` — the canonical goal store.
- `goals.managed.json` — a digest marker the manager writes.

It validates that both files match in digest and freshness, that timestamps and host IDs match, and then returns owned/unowned/unknown with a reason. This is pure read, zero state change.

### Ledger

Every successful dispatch is signed and logged. The ledger is an append-only JSONL file with one entry per successful send. Each entry includes:

- Order ID and lane ID.
- The exact text delivered.
- Delivery timestamp.
- HMAC-SHA256 signature over the entry.

This is a trusted-host model: anyone who can write to the ledger file can rewrite it. So treat "not in the ledger" as a strong signal, not tamper-proof evidence. But a reader with the signing key can prove that a given message was delivered by verifying the signature.

## The LLM-Judgment Layer

One deliberate gate lets chitra invoke an LLM: goal enforcement via watchd.

### Goal Enforcement and Completion Review

Watchd watches tmux panes for session activity. When a session ends a turn claiming completion (via a marker in the pane output), watchd:

1. Freezes the session's goal statement.
2. Launches isolated `claude -p` reviewer processes (bounded concurrency, default max 2 at once).
3. Each reviewer reads the goal and the session's turn output, then judges: does the output satisfy the stated goal?
4. Records the verdict (accept/reject/unavailable) to a signed completion_reviews.jsonl log.

These reviewer processes are isolated. They never:

- Draft or review chitra's own prospective messages to the session.
- Share context with other reviewers or with chitra's main loop.
- Mutate chitra's state.
- Bypass operator gates for spend, credentials, or irreversible actions.

The pane poll never waits for reviewers to finish; it keeps the lane non-green and collects ready verdicts on later polls. The verdicts are inputs to decision attestation, but only approved text (flagged as such by an operator via convlog) can reach the pane.

### Goals and Completion Gating

A goal is a statement of what the session should accomplish. Chitra stores each goal with:

- A 6-word minimum to encourage clarity.
- A `done_when` condition: the gate looks for this language in the session's final output.
- One of 8 statuses: open, working, paused, held, complete, abandoned, redirected, or deferred.
- An immutable `lane_id` once set (prevents re-enrollment of the same logical lane under a fresh session ref).

Chitra never rewrites the `done_when` statement. The operator authors it once at enrollment. Post-hoc rewriting would let the system fit the condition to whatever already exists. The first write copies the condition into write-once `enrolled_done_when` and `enrolled_at` anchors; every later write is checked against these.

When a session finishes and claims completion, the completion gate:

1. Reads the enrolled done_when condition.
2. Checks the session's output for the required language (via watchd's LLM reviewers).
3. Counts delivered items against the goal's inventory.
4. Blocks goal closure if the inventory count is short.

It also treats follow-on/deferred language over a still-required item as a silent descope tell, unless the operator explicitly acknowledged the descope or redirected the goal via convlog (the conversation log).

## The Ledger and Audit Trail

Chitra records:

- Every dispatch: order ID, text, timestamp, HMAC-signature.
- Every completion verdict: goal state, reviewer judgment (accept/reject), reasoning.
- Every state transition: lane status changes, hold/resume events, rate-limit phases.
- Every rate-limit decision: account usage snapshot, load pressure, pause reason.

This log is append-only. Once an entry is written, it is never modified or deleted. Any reader with the signing key can verify that an entry was created at a specific timestamp.

The model for trust is "trusted host." The host running chitra is assumed to be secure. Anyone who can write to the ledger file can rewrite it. But across a network or audit boundary, the log is evidence: absence of an entry is a strong signal that something did not happen.

## How the Layers Work Together

A typical session flow:

1. **Enrollment (deterministic):** An operator enrolls a session with a goal statement via chitra-goals set. Chitra validates the goal, stores it immutably, and sets lane_id.
2. **Delivery (deterministic):** The operator (or an orchestration system) queues messages. Dispatchd drains the queue, delivers each message to the session via tmux, and logs the delivery.
3. **Rate limiting (deterministic):** Rate-limit-guard polls the account's usage and host load, pauses the session if thresholds are hit, and records the pause in a durable ledger.
4. **Completion (LLM-judgment):** The session ends a turn claiming completion. Watchd detects this, launches isolated reviewers, and collects verdicts.
5. **Goal closure (deterministic + LLM):** The operator runs chitra-goals close. Chitra reads the enrolled goal, checks the reviewer verdicts from step 4, counts delivered items, and blocks closure if the inventory is short.

The deterministic and LLM layers are separate. An LLM verdict never forces a decision; an operator always has the last word via convlog. And the core dispatch, ledger, and rate-limiting paths never call an LLM.

## See Also

- **[Design notes](../DESIGN.md)** — Detailed origin, single-writer rule, done-condition ownership, distribution model.
- **[Daemons reference](../daemons/)** — How each daemon/CLI tool works and what it does.
- **[Configuration](../configuration/)** — Routing and policy settings.

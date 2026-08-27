# Chitra Concepts

Chitra has two distinct layers: a deterministic delivery core and a bounded persistent supervision layer. Both keep their authority explicit; optional reviewer integrations cannot bypass operator gates.

## The Deterministic Core

Everything on chitra's delivery, ledger, routing, rate-limiting, and ownership-checking path is deterministic Python. No model evaluates any input. No LLM is consulted about what to send or when. This core loop is auditable, crash-safe, and repeatable.

### Dispatch

Dispatchd drains a JSON order queue (one order per file), delivers messages to tmux sessions, and records results. The delivery path is:

1. Acquire an exclusive file-based lock (LaneLock) per session. This prevents two writers from racing and corrupting the same session's input.
2. Load the message text from the order file.
3. Use `tmux load-buffer` and `tmux paste-buffer` to inject the text into the session's input line.
4. Verify delivery by grepping the session's transcript for the exact text. "Looks sent" is not evidence; grep confirms it actually arrived.
5. Sign and verify the ledger entry (HMAC-SHA256), then publish the result.

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

## Persistent Goal Supervision

`monitord` observes explicitly bound transcripts and keeps each lane aligned to
its exact frozen goal. It records intent before queueing an action and recovers
that intent, its retries, signed delivery, and consumption after restart.

### Goal Enforcement and Completion Review

On each pass, monitord:

1. Ingests only transcripts declared in the exact binding manifest.
2. Runs deterministic drift, repeated-step, repeated-test, and document-dithering detectors.
3. Queues one fair, goal-versioned corrective action per lane and pass through dispatchd.
4. Answers routine questions only when the frozen contract settles them.
5. Runs registered validators after a structured completion claim and closes only from verified, session-isolated receipts.

Monitord never:

- Write to tmux directly.
- Borrow a transcript, receipt, finding, or answer from another goal.
- Bypass operator gates for spend, credentials, or irreversible actions.

Legacy `watchd` deployments may still use isolated completion reviewers. Those
reviewers remain advisory and cannot bypass the same authority gates.

### Goals and Completion Gating

A goal is a statement of what the session should accomplish. Chitra stores each goal with:

- A 6-word minimum to encourage clarity.
- Frozen structured done items, each with a validator and exact required receipt name.
- One of 8 statuses: open, working, paused, held, complete, abandoned, redirected, or deferred.
- An immutable `lane_id` once set (prevents re-enrollment of the same logical lane under a fresh session ref).

The four-question interview result supplies the structured done items. Chitra generates the display `done_when` from those items and freezes the receipt, items, and enrollment time in one locked write.

When a session finishes and claims completion, the completion gate:

1. Reads the frozen structured done items.
2. Checks each proof's item ID, receipt name, validator result, and citation.
3. Executes the enrolled validators and verifies their stored receipts.
4. Persists the validated proofs before a done transition and repeats the exact check at close.

An operator can still redirect strategic work or administratively discard a dead record with a reason. Those paths are labeled as not done and cannot substitute for completion proof.

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

1. **Enrollment (deterministic):** The first `chitra-goals set` returns four typed questions and a nonce without writing a goal. `set --interview-result <file>` verifies the complete result and atomically stores the receipt, done items, enrollment time, and lane ID.
2. **Delivery (deterministic):** The operator (or an orchestration system) queues messages. Dispatchd drains the queue, delivers each message to the session via tmux, and logs the delivery.
3. **Rate limiting (deterministic):** Rate-limit-guard polls the account's usage and host load, pauses the session if thresholds are hit, and records the pause in a durable ledger.
4. **Completion supervision:** The session ends a turn claiming completion. Monitord executes the enrolled validators and independently verifies the stored results.
5. **Goal closure:** Chitra repeats the exact receipt check over the proofs monitord persisted. Delivered strings and operator acknowledgements cannot substitute for those receipts.

The delivery and supervision layers are separate. Dispatchd alone writes the terminal. Monitord can act only within the frozen contract, and an operator retains credentials, spend, irreversible actions, security boundaries, and strategy changes.

## See Also

- **[Design notes](../DESIGN.md)** — Detailed origin, single-writer rule, done-condition ownership, distribution model.
- **[Daemons reference](../daemons/)** — How each daemon/CLI tool works and what it does.
- **[Configuration](../configuration/)** — Routing and policy settings.

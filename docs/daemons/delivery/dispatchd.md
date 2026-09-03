# dispatchd — Delivery Daemon

Dispatchd is chitra's always-on delivery engine. It drains a JSON order queue, delivers messages to tmux sessions, and logs each delivery to a signed ledger.

## What it does

**On each pass:**

1. Reclaim stale in-flight orders (if an order failed halfway through, restart it).
2. Read orders from the queue in FIFO order (sorted by file modification time).
3. For each order:
   - Check if a result file already exists. A non-`SENT` result is terminal;
     a `SENT` result is recovered by retrying its ledger proof, never by
     dispatching again.
   - Verify the target session exists and is in scope.
   - Acquire an exclusive file lock (LaneLock) for that session.
   - Check the rate-limit freeze status. If the session is paused, defer the order.
   - Paste the message text into the tmux session via `tmux load-buffer` and `tmux paste-buffer`.
   - Verify delivery in the exact transcript bound to an autonomous order.
   - Sign and verify the matching ledger entry.
   - Write the result and move the order to `processed/`.

Dispatchd is **single-threaded per session.** The LaneLock prevents two writers from racing. A new `SENT` result is published only after its signed delivery proof verifies. An older or externally planted `SENT` result cannot create its own proof during recovery. It remains claimed unless an already-existing signed row proves the exact order, message, session, and bound native session identity. If a crash happens before the result is written, the send-nonce marker and exact bound-transcript check reconcile the state without a second paste.

## CLI usage

```bash
dispatchd \
  --queue-dir /var/lib/chitra/queue \
  --ledger-path /var/lib/chitra/ledger.jsonl \
  --goals-root /var/lib/chitra \
  --transcript-bindings-path /etc/chitra/transcript-bindings.json \
  --poll-seconds 5 \
  --once
```

Run with `--once` for a single pass (used in cron/systemd timer). Omit it to run continuously.

## Key flags

| Flag | Default | Notes |
|------|---------|-------|
| `--queue-dir` | `$CHITRA_STATE_DIR/queue` | Order queue directory. |
| `--ledger-path` | `$CHITRA_STATE_DIR/ledger.jsonl` | Append-only ledger file. |
| `--state-dir` | `/var/lib/chitra` | Base state directory. |
| `--lock-dir` | `$CHITRA_STATE_DIR/locks` | Directory for LaneLock files. |
| `--routing-config-path` | Unset | Optional routing config (YAML). |
| `--policy-config-path` | Unset | Optional policy config (YAML). |
| `--goals-root` | `$CHITRA_STATE_DIR` | Goal store used to verify goal-bound orders. |
| `--transcript-root` | Manifest directory | Root for relative transcript paths. |
| `--transcript-bindings-path` | State root manifest | Exact session-to-transcript bindings for autonomous deliveries. |
| `--allow-session-prefix` | All allowed | Allowlist of session name prefixes to deliver to. |
| `--deny-session-prefix` | None | Denylist of session name prefixes. |
| `--post-paste-wait-seconds` | 0.5 | Time to wait after paste before transcript grep. |
| `--transcript-recency-seconds` | 60 | How fresh the transcript copy must be. |
| `--poll-seconds` | 5 | Seconds between passes (if running continuously). |
| `--once` | False | Run one pass and exit. |

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `CHITRA_STATE_DIR` | `/var/lib/chitra` | Base directory for queue, ledger, locks. |
| `CHITRA_LANE_LOCK_DIR` | `$CHITRA_STATE_DIR/locks` | Directory for lock files. |
| `CHITRA_ROUTING_CONFIG` | Unset | Path to routing config. |
| `CHITRA_POLICY_CONFIG` | Unset | Path to policy config. |
| `CHITRA_ALLOWED_SESSION_PREFIXES` | All allowed | Comma-separated allowlist. |
| `CHITRA_DENIED_SESSION_PREFIXES` | None | Comma-separated denylist. |

## Order and result format

**Order (JSON):**

```json
{
  "order_id": "task-123",
  "session_ref": "localhost:session-name:0.0",
  "nudge": "Continue the queued task.",
  "task_type": "optional routing key",
  "routing_hint": "optional explicit hint"
}
```

**Result (JSON):**

```json
{
  "order_id": "task-123",
  "session_ref": "localhost:session-name:0.0",
  "status": "sent",
  "delivery_ledger_verified": true,
  "native_session_id": "adapter-native-session-id",
  "at": "2025-01-15T12:34:56Z"
}
```

## Common tasks

**Deliver a single message:**

```bash
mkdir -p /var/lib/chitra/queue/orders
cat > /var/lib/chitra/queue/orders/msg-001.json << 'EOF'
{
  "order_id": "msg-001",
  "session_ref": "localhost:my-session:0.0",
  "nudge": "Continue the queued task."
}
EOF

dispatchd --queue-dir /var/lib/chitra/queue --once
```

**Run as a systemd service (continuous):**

See the packaged unit at `packaging/systemd/chitra-dispatchd.service` in the
repo. It is the canonical unit for the released `/opt/chitra/venv` layout and
the declaration-driven `--lanes-file` mode.

**Check delivered messages:**

```bash
# View ledger
cat /var/lib/chitra/ledger.jsonl | jq .

# Verify a specific delivery (requires ledger signing key)
python -c "from chitra.ledger import verify_delivery; verify_delivery(ledger_path, order_id, signing_key)"
```

## Reliability guarantees

- **Atomicity:** Each delivery acquires a lock, completes, and releases it. No partial deliveries.
- **Durability:** Ledger entries are HMAC-signed and appended. Crash-safe.
- **Idempotency:** Once a result file exists, the order is never redelivered.
- **Ledger-gated acknowledgment:** A `SENT` order is not moved to
  `processed/` until its order-specific HMAC proof is present and valid in the
  delivery ledger. A pre-existing `SENT` result without that proof remains
  claimed. Recovery does not sign it.
- **Exact autonomous target:** Persistent-oversight and goal-answer orders fail
  closed unless one validated manifest binds the goal session to one exact
  transcript. Delivery proof must carry that transcript's native session ID.
- **Single-writer:** LaneLock prevents concurrent writes to the same session.

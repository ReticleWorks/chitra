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
   - Verify delivery by grepping the session's transcript for the exact text.
   - Write a result file, sign a matching ledger entry, and move the order to
     `processed/` only after the signed proof verifies.

Dispatchd is **single-threaded per session.** The LaneLock prevents two writers from racing. It is **idempotent:** once a result file exists, the order is never redelivered, even across a restart. A `SENT` result may temporarily have `delivery_ledger_verified=false`; that record lets a restart retry only the ledger write. The order is not acknowledged in `processed/` until the signed proof exists. If a crash happens before the result is written, the send-nonce marker plus transcript-grep check reconcile the state on the next run.

## CLI usage

```bash
dispatchd \
  --queue-dir /var/lib/chitra/queue \
  --ledger-path /var/lib/chitra/ledger.jsonl \
  --state-dir /var/lib/chitra \
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
  "lane_id": "session-name",
  "text": "command to send",
  "task_type": "optional routing key",
  "routing_hint": "optional explicit hint"
}
```

**Result (JSON):**

```json
{
  "order_id": "task-123",
  "lane_id": "session-name",
  "status": "sent",
  "delivery_ledger_verified": true,
  "delivery_timestamp": "2025-01-15T12:34:56Z",
  "nonce": "send-nonce-value"
}
```

## Common tasks

**Deliver a single message:**

```bash
cat > /var/lib/chitra/queue/msg-001.json << 'EOF'
{
  "order_id": "msg-001",
  "lane_id": "my-session",
  "text": "echo 'Hello'"
}
EOF

dispatchd --queue-dir /var/lib/chitra/queue --once
```

**Run as a systemd service (continuous):**

See `packaging/systemd/chitra-dispatchd.service.example` in the repo.

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
  delivery ledger. A `SENT` result with a false proof flag is pending recovery,
  not a completed queue acknowledgment.
- **Single-writer:** LaneLock prevents concurrent writes to the same session.

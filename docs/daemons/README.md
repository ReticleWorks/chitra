# Daemons and Tools

Chitra provides command-line tools and daemons organized by function. New
deployments run `monitord` for persistent supervision and `dispatchd` as the
sole terminal writer. `watchd`, `triaged`, and `sweepd` remain for existing
deployments. The rest are periodic, ad-hoc, or observation-only services.

## The Composed Monitor

- **[monitord](monitord.md)** — Persistent, exact goal-bound supervision: transcript ingestion, deterministic detectors, crash-safe corrective orders, completion receipts, foreground investigation, and presence. `watchd`, `triaged`, and `sweepd` are deprecated by it.

## Delivery Systems

The deterministic core that handles message queuing, delivery, and ledger recording.

- **[Delivery](delivery/)** — Dispatch daemon, state deduplication, and fleet digests

## Authority and Access Control

Read-only services for ownership verification and pressure observation.

- **[Authority](authority/)** — Ownership verification and dark-launch observation

## Operator Tools

CLI utilities and watchers for managing sessions, goals, and policy enforcement.

- **[Operator Tools](operator-tools/)** — Session management, goal tracking, artifact review, and decision logging

## Utilities

Auxiliary tools for testing, debugging, and drift detection.

- **[Utilities](utilities/)** — Draft scanning and regression testing

## Quick Start

**Start chitra for the first time:**

```bash
# Current supervision pair
systemctl start chitra-dispatchd
systemctl start chitra-monitord@<instance>

# Periodic rate-limit check
systemctl start --timer chitra-rate-limit-guard.timer

# Queue a message
mkdir -p /var/lib/chitra/lane-<instance>/queue/orders
cat > /var/lib/chitra/lane-<instance>/queue/orders/msg-001.json << 'EOF'
{"order_id": "msg-001", "session_ref": "localhost:session-1:0.0", "nudge": "Continue the queued task."}
EOF

# View the fleet board
chitra-goals roster --format box
```

The matching entry in `/etc/chitra/lanes.yaml` must set `state_dir` to
`/var/lib/chitra/lane-<instance>` so shared `dispatchd` drains this queue.

**Explain a supervised session's status:**

```bash
chitra-agent explain --pane-id %17
```

**Check account usage:**

```bash
chitra-usage evaluate --dir /var/lib/chitra/usage --policy-config /etc/chitra/policy.yaml
```

## See Also

- **[Concepts](../concepts/)** — How daemons fit together
- **[Configuration](../configuration/)** — Policy and routing settings

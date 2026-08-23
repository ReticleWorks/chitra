# Daemons and Tools

Chitra provides a set of command-line tools and daemons organized by function. Two run continuously (dispatchd, triaged). The rest are periodic, ad-hoc, or observation-only services.

## The Composed Monitor

- **[monitord](monitord.md)** — The single observation-plane daemon: journal ingestion, deterministic detectors and response ladder, enrollment receipts, and presence in one entrypoint. `watchd`, `triaged`, and `sweepd` are deprecated by it.

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
# Continuous daemons
systemctl start chitra-dispatchd
systemctl start chitra-triaged

# Periodic rate-limit check
systemctl start --timer chitra-rate-limit-guard.timer

# Queue a message
cat > /var/lib/chitra/queue/msg-001.json << 'EOF'
{"order_id": "msg-001", "lane_id": "session-1", "text": "echo 'hello'"}
EOF

# View the fleet board
chitra-goals roster --format box
```

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

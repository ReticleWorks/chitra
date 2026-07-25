# Delivery Systems

The deterministic core handles message queuing, delivery verification, state tracking, and fleet digests. Every operation here is audit-safe and zero LLM.

## Always-On Daemons

- **[dispatchd](dispatchd.md)** — Drains the message queue, delivers to tmux sessions, verifies delivery by grepping transcripts, and writes a signed ledger.
- **[triaged](triaged.md)** — Tails the events log, deduplicates pane changes, and emits alerts on critical conditions (crash, merge, rate limit).

## Periodic Services

- **[sweepd](sweepd.md)** — Reads canonical goals and rate-limit state, publishes delta-only updates for downstream dashboards.
- **[rate-limit-guard](rate-limit-guard.md)** — Pauses and resumes sessions via a durable transaction ledger based on account usage and host pressure.

## See Also

- **[Concepts: Deterministic Core](../../concepts/)** — How delivery, ledger, routing, and rate-limiting work
- **[Configuration](../../configuration/)** — Routing and policy settings

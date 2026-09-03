# Chitra Documentation

## Agent coordination and status

- [Semantic agent status, coordination API, and live handoff](agent-status-design.md)
- [Migration from Watchd screen-change inference](watchd-status-migration.md)
- [Agent detection manifest format](agent-detection-manifests.md)

Chitra delivers messages to long-running LLM agent sessions in tmux and persistently supervises each explicitly bound transcript against its frozen goal. `monitord` detects drift and stalls, persists corrective intent and retries across restarts, answers contract-settled questions, and records unresolved routine questions for foreground investigation. It executes enrolled completion validators. `dispatchd` remains the sole terminal writer. Each goal freezes an aggressive, goal-scoped `AutonomyPolicy` with typed grants and limits; only a verified policy boundary or frozen-outcome change reaches the user.

## Getting started

Start here if you're new to chitra:

- **[Quickstart](quickstart/)** — Install chitra and run your first dispatch.
- **[Concepts](concepts/)** — Understand deterministic delivery and the persistent goal-bound supervision layer.
- **[Persistent supervision](persistent-supervision.md)** — Lane lifecycle, pursuit loops, canonical setup knowledge, and recovery.

## Core reference

Understand how chitra works:

- **[Daemons and Tools](daemons/)** — Message delivery, session management, policy enforcement, and utilities.
- **[Configuration](configuration/)** — Routing and policy settings, with worked examples.

## Deep dives

For specific challenges and advanced topics:

- **[boardd](boardd.md)** — Live fleet dashboard: pure reader over the chitra state dir with SSE push and a plain-language board.
- **[Design notes](DESIGN.md)** — Origin, persistent reasoning boundary, done-condition ownership, distribution, single-writer rule, and extensibility model.
- **[Evasion taxonomy](evasion-taxonomy.md)** — Dispatch and state-observation attack surfaces.
- **[Pause and recovery](pause-recovery.md)** — Rate-limit hold mechanics and graceful pause phases.
- **[Petra authority](petra-authority.md)** — Dark-launch observe-only pressure-observation service.
- **[Self-tuning](self-tuning.md)** — Automated policy feedback loop.
- **[Workflow patterns](workflow-pattern-catalog.md)** — Real-world orchestration patterns.
- **[Routing feedback and usage](routing-feedback-usage.md)** — Task-type routing and usage snapshots.

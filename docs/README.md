# Chitra Documentation

## Agent coordination and status

- [Semantic agent status, coordination API, and live handoff](agent-status-design.md)
- [Migration from Watchd screen-change inference](watchd-status-migration.md)
- [Agent detection manifest format](agent-detection-manifests.md)

Chitra delivers messages to long-running LLM agent sessions in tmux and watches their state. Everything on the delivery, queueing, ledger, and state-tracking path is deterministic — Python with zero LLM calls. The one deliberate exception is goal enforcement: when a watched session claims it is done, chitra launches isolated Claude reviewer processes to check that claim against the session's frozen goal. The reviewers never draft chitra's messages, and their verdicts stay in chitra's own logs.

## Getting started

Start here if you're new to chitra:

- **[Quickstart](quickstart/)** — Install chitra and run your first dispatch.
- **[Concepts](concepts/)** — Understand chitra's two layers: the deterministic core (delivery, ledger, rate-limiting, routing, ownership) and the LLM-judgment layer (goal enforcement via watchd).

## Core reference

Understand how chitra works:

- **[Daemons and Tools](daemons/)** — Message delivery, session management, policy enforcement, and utilities.
- **[Configuration](configuration/)** — Routing and policy settings, with worked examples.

## Deep dives

For specific challenges and advanced topics:

- **[boardd](boardd.md)** — Live fleet dashboard: pure reader over the chitra state dir with SSE push and a plain-language board.
- **[Design notes](DESIGN.md)** — Origin, bounded reasoning boundary, done-condition ownership, distribution, single-writer rule, and extensibility model.
- **[Evasion taxonomy](evasion-taxonomy.md)** — Dispatch and state-observation attack surfaces.
- **[Pause and recovery](pause-recovery.md)** — Rate-limit hold mechanics and graceful pause phases.
- **[Petra authority](petra-authority.md)** — Dark-launch observe-only pressure-observation service.
- **[Self-tuning](self-tuning.md)** — Automated policy feedback loop.
- **[Workflow patterns](workflow-pattern-catalog.md)** — Real-world orchestration patterns.
- **[Routing feedback and usage](routing-feedback-usage.md)** — Task-type routing and usage snapshots.

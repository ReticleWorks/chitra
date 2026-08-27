# Operator Tools

CLI utilities and watchers for managing sessions, enforcing goals, tracking policies, and recording decisions.

## Goal Management and Monitoring

- **[chitra-goals](goals-cli.md)** — Enroll sessions, query status, close goals, hold and resume sessions, track open asks, and render the operator board.
- **[monitord](../monitord.md)** — Current persistent goal supervisor. It binds transcripts, persists corrections and routine answers, and verifies completion receipts.
- **[watchd](watchd.md)** — Legacy semantic-status and completion-review daemon retained for existing deployments.
- **`chitra-agent`** — Lifecycle report, status explanation, semantic wait, and API-schema client for Watchd's local socket.

## State and Policy Tracking

- **[chitra-convlog](convlog.md)** — Operator decision log. Records four-stage conversation (raw message → brief → ruling → directive) as an append-only JSONL log.
- **[chitra-usage](usage.md)** — Usage snapshot evaluation. Reads API provider usage (Claude, Codex) and checks against policy thresholds.
- **[chitra-artifacts](artifacts.md)** — Artifact review tracking. Records Claude-artifact publish state and marks artifacts as reviewed.
- **[chitra-capabilities](capabilities.md)** — Runtime authorization toggles. Enable and disable capabilities (dispatch, goal enforcement, etc.) with optional expiry.

## See Also

- **[Concepts](../../concepts/)** — How tools interact with chitra's layers
- **[Configuration](../../configuration/)** — Policy thresholds and settings

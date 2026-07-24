# Daemons and CLI Tools

Chitra provides a set of command-line tools and daemons. Two run continuously (dispatchd, triaged). The rest are periodic, ad-hoc, or observation-only services.

## Always-on daemons

- **[dispatchd](dispatchd.md)** — Delivery daemon. Drains the message queue, delivers to tmux sessions, verifies delivery, and logs each send to a signed ledger.
- **[triaged](triaged.md)** — State deduplication. Tails the events log, deduplicates pane changes, and emits alerts on critical conditions (crash, merge, rate limit, etc).

## Periodic and ad-hoc

- **[watchd](watchd.md)** — Completion reviewer. Watches tmux panes for completion claims and launches isolated LLM reviewer processes to judge whether claims match frozen goals. (This is where chitra makes real LLM calls.)
- **[sweepd](sweepd.md)** — Fleet state digest. Reads canonical goals, rate-limit state, and triaged flags, then publishes delta-only updates for downstream dashboards.
- **[rate-limit-guard](rate-limit-guard.md)** — Rate limiting and load shedding. Pauses and resumes sessions via a durable transaction ledger based on account usage and host pressure.
- **[chitra-goals](goals-cli.md)** — Goal management CLI. Enroll sessions, query status, close goals, hold/resume, track open asks, and render the operator board.
- **[chitra-convo](convlog.md)** — Operator decision log. Records four-stage conversation (raw message → brief → ruling → directive) as an append-only JSONL log.
- **[chitra-artifacts](artifacts.md)** — Artifact review tracking. Records Claude-artifact publish state and marks artifacts as reviewed.
- **[chitra-usage](usage.md)** — Usage snapshot evaluation. Reads API provider usage (Claude, Codex) and checks against policy thresholds.
- **[draft-scanner](draft-scanner.md)** — Unsubmitted draft detection. Scans tmux input boxes for unsent operator drafts.
- **[chitra-capabilities](capabilities.md)** — Runtime authorization toggles. Enable/disable capabilities (dispatch, goal enforcement, etc) with optional expiry.
- **[replay-eval](replay-eval.md)** — Regression testing. Deterministically evaluates synthetic fixture cases against chitra's policy (CI tool, zero LLM calls).

## Observe-only services

- **[petra](petra.md)** — Dark-launch observe-only authority. Validates advisory "pressure observation" events from Watchtower and records them to SQLite. Never acts on observations; pure recording.
- **[ownership-provider](ownership-provider.md)** — Read-only ownership fence. Answers whether a host:lane:instance belongs to a canonical managed lane by reading goals.json and a digest marker. Never discovers sessions or modifies state.

## Quick reference

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

**Monitor a session:**

```bash
watchd --session-prefix my-agent --interval-seconds 5
```

**Check account usage:**

```bash
chitra-usage evaluate --dir /var/lib/chitra/usage --policy-config /etc/chitra/policy.yaml
```

## See Also

- **[Concepts](../concepts.md)** — How daemons fit together (deterministic core vs LLM-judgment layer).
- **[Configuration](../configuration.md)** — Policy and routing settings.

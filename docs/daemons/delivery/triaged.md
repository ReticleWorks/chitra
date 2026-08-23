# triaged — State Deduplication

> **Deprecated.** Superseded by the composed [`monitord`](../monitord.md)
> entrypoint and its canonical journal; kept for existing declarations only.

Triaged tails an events log and emits triage events only on real state transitions. It deduplicates noisy pane changes, detects critical conditions (crash, merge landed, rate limit), and flags them.

## What it does

**On each poll:**

1. Read the events log (watching for rotation).
2. Parse each line as ISO8601 timestamp, lane ID, and event text.
3. Compute a SHA256 signature of the event.
4. If the signature has been seen before for this lane, skip it (dedupe).
5. If this is a new event, apply regex critical-rules (needs_operator, merge_landed, crash, ci_red, blocked, rate_limit).
6. Emit a triage event if any rule matched.
7. Persist the signature in a state file for the next poll.

The deduplication window is 900 seconds by default; triaged keeps one signature per lane and never prunes it (unbounded growth, but each entry is small).

## CLI usage

```bash
triaged \
  --events-log /var/lib/chitra/events.log \
  --triage-log /var/lib/chitra/triage.jsonl \
  --state-file /var/lib/chitra/triage-state.json \
  --poll-seconds 2 \
  --once
```

Omit `--once` to run continuously.

## Key flags

| Flag | Default | Notes |
|------|---------|-------|
| `--events-log` | `/var/lib/chitra/events.log` | Events log to tail (from watchd). |
| `--triage-log` | Unset | JSONL file for triage events. |
| `--state-file` | Unset | JSON file for dedup state (lane → signature map). |
| `--queue-file` | Unset | Optional queue for new alerts. |
| `--flags-file` | Unset | Optional flags file. |
| `--stats-file` | Unset | Optional stats file. |
| `--alert-state-file` | Unset | Optional alert state. |
| `--poll-seconds` | 2.0 | Seconds between polls. |
| `--once` | False | Run one poll and exit. |

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `CHITRA_TRIAGE_EVENTS_LOG` | `/var/lib/chitra/events.log` | Events log path. |
| `CHITRA_TRIAGE_LOG` | Unset | Triage log path. |
| `CHITRA_TRIAGE_STATE_FILE` | Unset | Dedup state file. |
| `CHITRA_TRIAGE_QUEUE_FILE` | Unset | Alert queue. |
| `CHITRA_TRIAGE_FLAGS_FILE` | Unset | Flags file. |
| `CHITRA_TRIAGE_STATS_FILE` | Unset | Stats file. |
| `CHITRA_TRIAGE_ALERT_STATE_FILE` | Unset | Alert state. |

## Critical rules

Triaged applies regex patterns to detect critical conditions:

| Rule | Triggers when event text matches |
|------|---|
| `needs_operator` | Operator decision/input required. |
| `merge_landed` | Pull request merged. |
| `crash` | Session crashed or exited unexpectedly. |
| `ci_red` | CI pipeline failed. |
| `blocked` | Session is blocked (network, auth, etc.). |
| `rate_limit` | Rate limit hit or session paused. |

Custom rules can be added via policy config. When a rule matches, triaged flags the lane with a CRIT statement and emits an alert.

## Triage log format

```jsonl
{"timestamp": "2025-01-15T12:34:56Z", "lane_id": "session", "event": "completion_reviewed", "critical_flags": ["needs_operator"], "alert": true}
```

## Common tasks

**Run as a systemd service:**

See `packaging/systemd/chitra-triaged.service.example` in the repo.

**View triage events:**

```bash
cat /var/lib/chitra/triage.jsonl | jq '.[] | select(.critical_flags | length > 0)'
```

**Monitor for crashes:**

```bash
tail -f /var/lib/chitra/triage.jsonl | jq 'select(.critical_flags | contains(["crash"]))'
```

## Known limitations

The dedup state (lane → signature map) has no eviction. Across very long deployments, it grows unbounded (though each entry is tiny). On restart, the map is reinitialized and stale signatures are forgotten.

## See Also

- **[Sweepd](sweepd.md)** — Consumes triaged output and publishes fleet state deltas.

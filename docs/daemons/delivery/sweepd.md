# sweepd — Fleet State Digest

Sweepd reads canonical fleet state and publishes a delta-only digest of what changed since the last sweep. Downstream dashboards and monitors consume this digest instead of polling multiple sources.

## What it does

**On each poll interval:**

1. Read canonical state:
   - Goals (from goals.json).
   - Rate-limit transactions (current hold status, phase, resume timestamp).
   - Account registry (which sessions use which accounts).
   - Triaged flags (critical conditions flagged by triaged).
2. Build a LaneState object for each tracked session:
   - Goal status (open, working, complete, etc.).
   - Due timestamp and overdue flag.
   - Hold reason and resume timestamp.
   - Rate-limit phase (paused, held, cleared).
   - Load level (L1, L2, L3, baseline).
   - Account name.
   - Pending operator decisions (from convlog).
3. Compute deltas since last sweep:
   - New sessions.
   - Sessions with changed status.
   - Sessions that disappeared.
4. Write a delta-only JSON digest (only new/changed, not stale entries).

The digest is compact and intended for real-time dashboards and monitoring systems.

## CLI usage

```bash
chitra-sweepd \
  --state-dir /var/lib/chitra \
  --digest-path /var/lib/chitra/sweep-digest.json \
  --poll-seconds 60 \
  --once
```

Omit `--once` to run continuously.

## Key flags

| Flag | Default | Notes |
|------|---------|-------|
| `--state-dir` | `/var/lib/chitra` | Base state directory (where goals.json lives). |
| `--digest-path` | `$state_dir/sweep-digest.json` | Output digest file. |
| `--snapshot-path` | Unset | Optional full state snapshot. |
| `--flags-path` | Unset | Optional flags file (from triaged). |
| `--poll-seconds` | 60.0 | Seconds between sweeps. |
| `--once` | False | Run once and exit. |

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `CHITRA_SWEEP_DIGEST_PATH` | `$CHITRA_STATE_DIR/sweep-digest.json` | Output path. |
| `CHITRA_SWEEP_SNAPSHOT_PATH` | Unset | Full snapshot path. |
| `CHITRA_SWEEP_FLAGS_PATH` | Unset | Flags path. |
| `CHITRA_SWEEP_POLL_SECONDS` | 60.0 | Poll interval. |

## Digest format

```json
{
  "timestamp": "2025-01-15T12:34:56Z",
  "sweep_id": "sweep-12345",
  "deltas": {
    "new": [
      {
        "lane_id": "session-1",
        "goal_status": "open",
        "due": "2025-01-20T00:00:00Z",
        "overdue": false,
        "hold_reason": null,
        "hold_resume": null,
        "rate_limit_phase": "cleared",
        "load_level": "baseline",
        "account": "claude-prod",
        "pending_decisions": []
      }
    ],
    "changed": [
      {
        "lane_id": "session-2",
        "goal_status": { "old": "open", "new": "working" },
        "rate_limit_phase": { "old": "cleared", "new": "held" },
        "hold_reason": { "new": "account limit at 92%" }
      }
    ],
    "disappeared": ["session-3"]
  }
}
```

## Common tasks

**Run as a systemd service:**

See the packaged unit at `packaging/systemd/chitra-sweepd.service` in the
repo. It is the canonical unit for the released `/opt/chitra/venv` layout and
the declaration-driven `--lanes-file` mode.

**Monitor for status changes:**

```bash
sweepd --state-dir /var/lib/chitra --digest-path /var/lib/chitra/sweep-digest.json --once
cat /var/lib/chitra/sweep-digest.json | jq '.deltas.changed'
```

**Build a dashboard:**

A dashboard can poll the digest file every 10-30 seconds and render the current fleet state (from the most recent sweep). The delta-only format keeps the digest small and fast to parse.

## Performance notes

- Sweepd is lightweight; it only reads files and computes deltas.
- The digest is meant for real-time consumption (10-60 second update windows).
- Downstream consumers should cache the digest locally and only re-render on deltas.

## See Also

- **[Triaged](triaged.md)** — Provides critical-flags input to sweepd.
- **[Goals CLI](../operator-tools/goals-cli.md)** — Manages the goals state that sweepd reads.
- **[Convlog](../operator-tools/convlog.md)** — Records pending decisions that sweepd includes.

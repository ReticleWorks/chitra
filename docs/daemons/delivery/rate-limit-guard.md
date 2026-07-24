# rate-limit-guard — Rate Limit and Load Shedding

Rate-limit-guard pauses and resumes sessions when account usage approaches limits or host load is high. It works via a durable transaction ledger that records each pause/resume phase.

## What it does

**On each run (typically every few minutes via systemd timer):**

1. Read account usage snapshots (Claude API, Codex API, or custom).
2. Evaluate pause/warn thresholds:
   - Claude: 5-hour and 7-day windows.
   - Codex: primary and secondary limits.
3. Read host load pressure (memory available, CPU/memory PSI).
4. For each active session:
   - Check if its account is approaching limits.
   - Check if the host is under load pressure.
   - Pause the session if either threshold is hit (via a nudge order in the dispatchd queue).
   - Resume the session if both thresholds are cleared.
5. Record each decision (pause_requested → checkpoint_sent → stop_sent → awaiting_quiescence → held → resume_requested → resume_sent → cleared) to a durable transaction ledger.

All nudges are fixed canned templates (e.g., "Pausing due to rate limit"). Chitra never drafts prose or LLM reasoning. It just enforces the decision.

## CLI usage

```bash
chitra-rate-limit-guard \
  --usage-dir /var/lib/chitra/usage \
  --goals-root /var/lib/chitra/goals \
  --queue-dir /var/lib/chitra/queue \
  --policy-config /etc/chitra/policy.yaml
```

Typical systemd timer: run every 3-5 minutes.

## Key flags

| Flag | Default | Notes |
|------|---------|-------|
| `--usage-dir` | Unset | Directory for usage snapshots. |
| `--host` | localhost | Host to read usage and load for. |
| `--staleness-seconds` | 1200 (20 min) | How old a usage snapshot can be. |
| `--goals-root` | Unset | Root directory for goals.json. |
| `--queue-dir` | `$CHITRA_STATE_DIR/queue` | Dispatchd order queue. |
| `--policy-config` | Unset | Policy config (YAML) with pause thresholds. |
| `--codex` | false | Use Codex thresholds instead of Claude. |
| `--codex-bin` | `codex` | Codex CLI binary path. |
| `--once` | False | Run once and exit. |

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `CHITRA_NEVER_PAUSE_SESSION_PREFIXES` | Unset | Comma-separated prefixes to never pause. |

## Policy config

The policy YAML sets thresholds. Example (see `docs/policy.yaml.example`):

```yaml
usage:
  pause_5h_pct: 92.0        # Pause at 92% of 5-hour window
  pause_7d_pct: 95.0        # Pause at 95% of 7-day window
  warn_5h_pct: 80.0         # Warn at 80% (don't assign new work)
  warn_7d_pct: 90.0         # Warn at 90%
  max_running: null          # Max sessions running (null = use default)
  auto_resume: true          # Re-arm timed holds after reset
load:
  baseline_max_running: 8    # Sessions to run at baseline load
  l1_max_running: 6          # Sessions at L1 load pressure
  l2_max_running: 4          # Sessions at L2
  l3_max_running: 2          # Sessions at L3 (critical)
  l1_mem_available_pct: 25.0 # Trigger L1 at 25% mem available
  l2_mem_available_pct: 15.0 # Trigger L2 at 15%
  l3_mem_available_pct: 8.0  # Trigger L3 at 8%
  # ... PSI thresholds omitted for brevity
```

## Pause and resume states

Rate-limit-guard tracks each pause as a phase:

1. **pause_requested** — Decision made to pause; nudge queued.
2. **checkpoint_sent** — Nudge pasted to session.
3. **stop_sent** — Waiting for session to stop generating.
4. **awaiting_quiescence** — Watching for silence (no new output).
5. **held** — Session paused and silent.
6. **resume_requested** — Decision made to resume; nudge queued.
7. **resume_sent** — Resume nudge pasted.
8. **cleared** — Session resumed and consuming normally.

Each phase transition consumes a dispatchd result (proof the nudge was delivered). The ledger is durable; if rate-limit-guard crashes mid-pause, the next run reads the ledger and continues from where it left off.

## Common tasks

**View pause/resume history:**

```bash
cat /var/lib/chitra/rate-limit-ledger.json | jq '.[] | select(.lane_id == "my-session")'
```

**Never pause certain sessions:**

```bash
export CHITRA_NEVER_PAUSE_SESSION_PREFIXES="admin,critical"
chitra-rate-limit-guard
```

**Check current hold status:**

```bash
chitra-rate-limit-guard --usage-dir /var/lib/chitra/usage --goals-root /var/lib/chitra/goals
```

**Run as a systemd timer:**

```bash
sudo cp packaging/systemd/chitra-rate-limit-guard.timer.example /etc/systemd/system/chitra-rate-limit-guard.timer
sudo cp packaging/systemd/chitra-rate-limit-guard.service.example /etc/systemd/system/chitra-rate-limit-guard.service
sudoedit /etc/systemd/system/chitra-rate-limit-guard.service  # fill in placeholders
sudo systemctl daemon-reload
sudo systemctl enable --now chitra-rate-limit-guard.timer
```

## Load shedding strategy

Host load pressure is sampled from `/proc` on each run:

- **L1 (moderate):** Memory drops below L1 threshold or CPU/memory PSI sustained.
- **L2 (high):** Memory drops below L2 or PSI increases further.
- **L3 (critical):** Memory critically low or PSI very high.

As load increases, rate-limit-guard reduces the number of allowed concurrent sessions. L3 uses shorter timeouts for phase transitions.

## Nudge templates

Pause nudges are fixed. Examples:

- "Pausing: Claude 5-hour limit at 92%."
- "Pausing: Host memory pressure."
- "Resuming: Usage back to normal."

These are never LLM-authored. They come from chitra's shipped templates.

## See Also

- **[Pause and Recovery](../../pause-recovery.md)** — Detailed mechanics of pause/resume phases.
- **[Configuration](../../configuration/)** — How to set policy thresholds.
- **[Usage](../operator-tools/usage.md)** — How usage snapshots are captured.

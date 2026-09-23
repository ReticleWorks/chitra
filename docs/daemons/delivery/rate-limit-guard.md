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
  --usage-dir /var/lib/chitra/usage-snapshots \
  --host "$(hostname)" \
  --goals-root /var/lib/chitra/lane-<lane-id> \
  --queue-dir /var/lib/chitra/lane-<lane-id>/queue \
  --policy-config /etc/chitra/policy.yaml
```

Each run is one sweep. The shipped systemd timer runs it every two minutes.

## Key flags

| Flag | Default | Notes |
|------|---------|-------|
| `--usage-dir` | Required | Directory for usage snapshots. |
| `--host` | Required | Host the sessions run on; used to build each `session_ref`. |
| `--staleness-seconds` | 1200 (20 min) | How old a usage snapshot can be. |
| `--goals-root` | `$CHITRA_STATE_DIR` | Lane state root holding goals and transactions. |
| `--queue-dir` | `$CHITRA_STATE_DIR/queue` | Dispatchd order queue. |
| `--policy-config` | `$CHITRA_POLICY_CONFIG`, else shipped defaults | Policy config (YAML) with pause thresholds. |
| `--codex` | false | Also read this host's local Codex account usage. |
| `--codex-bin` | `codex` | Codex CLI binary path. |

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
jq . /var/lib/chitra/lane-<lane-id>/rate_limit_state.json
```

**Never pause certain sessions:**

```bash
export CHITRA_NEVER_PAUSE_SESSION_PREFIXES="admin,critical"
chitra-rate-limit-guard
```

**Check current hold status:**

```bash
chitra-rate-limit-guard --usage-dir /var/lib/chitra/usage-snapshots --host "$(hostname)" --goals-root /var/lib/chitra/lane-<lane-id>
```

**Run as a systemd timer:**

The Debian package installs `packaging/systemd/chitra-rate-limit-guard@.service`
and `packaging/systemd/chitra-rate-limit-guard@.timer`. They are per-lane
templates like `chitra-monitord@.service`: instance `<lane-id>` sweeps
`/var/lib/chitra/lane-<lane-id>`, the state root that monitor writes, and queues
orders where `chitra-dispatchd` drains that lane. The service reads
`/etc/chitra/policy.yaml`, the file dispatchd uses, and resolves the host name
with systemd's `%H` specifier. Enable one timer per lane:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now chitra-rate-limit-guard@<lane-id>.timer
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

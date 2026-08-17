# chitra-usage — Usage Snapshot Evaluation

Chitra-usage reads provider usage snapshots and deterministically evaluates pause/warn thresholds. It does not itself decide to pause or resume; that is rate-limit-guard's job downstream.

## Subcommands

### read-claude — Read Claude API usage

```bash
chitra-usage read-claude \
  --dir /var/lib/chitra/usage \
  --json
```

Reads the latest Claude API usage snapshot and outputs it as JSON or human-readable format.

Example output:

```json
{
  "account": "prod-account",
  "timestamp": "2025-01-15T12:34:56Z",
  "5h_window": {
    "requests": 450,
    "tokens_in": 500000,
    "tokens_out": 150000,
    "percentage": 72.5
  },
  "7d_window": {
    "requests": 3000,
    "tokens_in": 5000000,
    "tokens_out": 1500000,
    "percentage": 85.2
  }
}
```

### codex-snapshot — Capture Codex usage

```bash
chitra-usage codex-snapshot \
  --codex-bin /opt/codex/bin/codex \
  --dir /var/lib/chitra/usage \
  --json
```

Calls the Codex CLI to capture current usage and save it to the usage directory. Timeout: 45 seconds (configurable via env).

### evaluate — Check thresholds

```bash
chitra-usage evaluate \
  --dir /var/lib/chitra/usage \
  --policy-config /etc/chitra/policy.yaml \
  --json
```

Reads usage snapshots and policy thresholds, then outputs:

- pause_threshold_hit: true/false
- warn_threshold_hit: true/false
- current_percentage: 72.5
- pause_threshold_pct: 92.0
- account: account name

### export — Publish this host's usage to the shared fleet tree

```bash
chitra-usage export \
  --fleet-dir /opt/opshome/scratch/temp-twinridge/fleet-monitor/usage \
  --dir /var/lib/chitra/usage \
  --codex-bin codex
```

Reads both backends locally, evaluates them against local policy, and writes
one file per backend into `<fleet-dir>/<host>/`:

- `<fleet-dir>/<host>/claude.json`
- `<fleet-dir>/<host>/codex.json`
- `<fleet-dir>/<host>/history/<backend>-<YYYYMMDDHH>.json` (pruned to 7 days)

Each file is a `chitra.usage-export.v1` document carrying percentages, reset
times, an account identity, and a verdict. It never carries a provider token,
so it is safe to publish onto a shared filesystem the monitor can read.

A backend that cannot be read still gets a file, with `verdict: unknown` and
the reason in `error`. That is deliberate: a broken read must stay
distinguishable from a host that runs no lane on that backend.

Run it from a systemd timer every 15 minutes: install
`packaging/systemd/chitra-usage-export.service.example` and
`chitra-usage-export.timer.example` (fill in the venv, `--fleet-dir`, `--host`,
and `--policy-config` paths, matching the `chitra-rate-limit-guard`
service/timer pair), then `systemctl enable --now chitra-usage-export.timer`.

### evaluate --fleet-dir — Read every host's usage

```bash
chitra-usage evaluate \
  --fleet-dir /opt/opshome/scratch/temp-twinridge/fleet-monitor/usage \
  --stale-after-seconds 1800
```

Prints one JSON line per host and backend. Verdicts:

| Verdict | Meaning |
|---------|---------|
| `ok` / `approaching` / `pause` | The host's own evaluation of a fresh reading. |
| `unknown` | The host wrote a file but could not read that backend; see `error`. |
| `stale-export` | The file is older than the ceiling. The export timer on that host is an incident. |
| `missing-export` | The host directory exists but that backend was never exported. |
| `invalid-export` | The file is unreadable or off-contract. |

The last three are faults to raise, not gaps to ignore. Silence about a host's
usage is what let a Codex lane sit dead on a weekly cap for two days.

### policy — Show policy thresholds

```bash
chitra-usage policy --policy-config /etc/chitra/policy.yaml
```

Shows the current policy (pause/warn thresholds, max running sessions, etc).

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `CHITRA_CODEX_SNAPSHOT_TIMEOUT_SECS` | 45 | Timeout for codex snapshot. |

## Common tasks

**Capture usage every 5 minutes:**

```bash
chitra-usage codex-snapshot --codex-bin codex --dir /var/lib/chitra/usage
```

(Run via systemd timer.)

**Check current usage against policy:**

```bash
chitra-usage evaluate \
  --dir /var/lib/chitra/usage \
  --policy-config /etc/chitra/policy.yaml
```

**View Claude usage history:**

```bash
ls -la /var/lib/chitra/usage/claude-*.json | head
cat /var/lib/chitra/usage/claude-latest.json | jq .
```

**Monitor for approaching limits:**

```bash
chitra-usage evaluate --json | jq '.[] | select(.warn_threshold_hit)'
```

## Usage snapshot storage

Snapshots are stored as JSON files, one per capture:

- `claude-2025-01-15T12-34-56Z.json` — Claude snapshot at specific timestamp.
- `claude-latest.json` — Symlink to the most recent snapshot.
- `codex-2025-01-15T12-34-56Z.json` — Codex snapshot.
- `codex-latest.json` — Most recent Codex snapshot.

Each snapshot includes:

- Timestamp
- Account name
- 5-hour and 7-day window usage (for Claude)
- Primary and secondary limits (for Codex)
- Percentage of limit used

## Staleness check

Rate-limit-guard checks snapshot freshness before using it:

```
snapshot_age = now - snapshot_timestamp
if snapshot_age > staleness_seconds:
  reject snapshot (older than 20 minutes, by default)
```

Stale snapshots are not used for pause/resume decisions.

## Grouping by account

Usage snapshots are grouped by account (never merged). One fresh session cannot mask stale siblings. If account-1 has a stale snapshot and account-2 has a fresh one, only account-2's usage is evaluated.

## See Also

- **[Rate-limit-guard](../delivery/rate-limit-guard.md)** — Consumes usage snapshots and makes pause/resume decisions.
- **[Configuration](../../configuration/)** — Policy thresholds (pause_5h_pct, warn_7d_pct, etc).

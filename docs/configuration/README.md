# Configuration

Chitra is configured via two YAML files: routing.yaml (task-type mappings) and policy.yaml (completion gates and thresholds). Each daemon also accepts CLI flags and environment variables for runtime control.

## Routing Configuration

Routing maps task types to routing hints. A dispatch order carries an opaque `task_type` tag; if routing.yaml is set, dispatchd looks up that tag and fills in a `routing_hint` string. An explicit routing_hint on the order always wins over the config.

Example (see `docs/routing.yaml.example`):

```yaml
# defaults — flat task_type -> routing_hint
defaults:
  heartbeat: sonnet
  sparring: fable
  quorum: haiku

# routes — structured task_type -> {model, harness, zdr?}
routes:
  design-judgment:
    model: opus-4.8
    harness: claude-code
    zdr: true
  code-fix:
    model: gpt-5.6-sol
    harness: codex-cli
```

Chitra resolves routes (structured form) into a `model@harness[+zdr]` string and records it in the ledger. A routes entry wins over a defaults entry for the same task_type.

The task_type keys are operator-defined. Chitra has no opinion on what they mean. These examples happen to draw from real orchestration patterns (see [workflow patterns catalog](../workflow-pattern-catalog.md)), but your vocabulary is your own.

## Policy Configuration

Policy.yaml sets completion gates and rate-limit thresholds. Every field is optional; if unset, chitra uses shipped defaults.

### Completion gate

```yaml
completion_gate:
  # Case-insensitive phrases that dispute a completion claim.
  deferral_phrases:
    - "you'll need to"
    - "future work"
    - "not implemented"
    - "deferred"
    - "out of scope"
    # (full list in policy.yaml.example)
  
  # Todo statuses treated as complete; all others are residue.
  complete_todo_statuses: [done]
  
  # Required evidence types for closure (empty list disables).
  # Allowed: deploy, live_verify
  required_evidence: [deploy, live_verify]
```

The completion gate scans the session's final output for deferral phrases. If any match, the gate rejects the completion claim (marks it for operator review). The required_evidence list specifies what proof is needed before a goal can close.

### Dispatch policy

```yaml
dispatch:
  # Case-insensitive regexes blocked before a nudge is pasted.
  banned_attribution_patterns:
    - '\boperator\b'
    - '\bthe monitor\b'
    - '\bchitra (wants|says|needs|relays)\b'
  
  # Case-sensitive regexes identifying additional idle pane input rows.
  extra_idle_input_regexes: []
```

Dispatch policy prevents nudges from claiming operator authorship or chitra's involvement. Banned patterns are scanned before paste; any match blocks the nudge.

### Guidance

```yaml
guidance:
  canonical_decisions:
    /opt/example-repo: /srv/shared-docs/project-decisions.md
    default: /srv/shared-docs/decisions.md
```

Maps working-directory prefixes to canonical decision documents. Used by chitra-goals guidance to point operators to relevant policies. The most-specific matching prefix wins; default is used when no prefix matches.

### Usage and rate limiting

```yaml
usage:
  # Graceful-pause thresholds for Claude 5-hour/7-day windows.
  pause_5h_pct: 92.0
  pause_7d_pct: 95.0
  
  # Approaching thresholds (avoid assigning new work).
  warn_5h_pct: 80.0
  warn_7d_pct: 90.0
  
  # Operator baseline cap. null uses load.baseline_max_running.
  max_running: null
  
  # Auto re-arm timed holds after reset.
  auto_resume: true

load:
  baseline_max_running: 8           # Baseline concurrent sessions
  l1_max_running: 6                 # L1 load pressure
  l2_max_running: 4                 # L2 load pressure
  l3_max_running: 2                 # L3 critical pressure
  
  l1_mem_available_pct: 25.0        # Trigger L1 at 25% mem available
  l2_mem_available_pct: 15.0        # Trigger L2 at 15%
  l3_mem_available_pct: 8.0         # Trigger L3 at 8%
  
  l1_memory_some_avg60: 10.0        # Pressure PSI avg60
  l2_memory_some_avg60: 25.0
  l3_memory_full_avg60: 10.0
  
  l1_cpu_some_avg60: 60.0           # CPU PSI (capped at L1)
  
  clear_mem_available_pct: 30.0     # Clear holds when mem recovers
  clear_memory_some_avg60: 5.0      # Clear when PSI drops
  
  consecutive_sweeps: 2             # Anti-flap: 2 sweeps at same level
  
  l3_pause:
    checkpoint_deadline_seconds: 60
    stop_deadline_seconds: 60
    quiescence_quiet_seconds: 15
    quiescence_timeout_seconds: 300
    resume_deadline_seconds: 60
    max_retry_attempts: 3
```

### Setting thresholds

**Usage pauses:** When Claude usage hits pause_5h_pct or pause_7d_pct, rate-limit-guard pauses sessions. When it clears (drops below 80%), sessions resume.

**Load pressure:** Chitra samples `/proc` on each rate-limit-guard run. Memory drops below a threshold → L1. Drops further → L2. Critical → L3. At L3, only 2 sessions run (instead of 8). Anti-flap logic (2 consecutive sweeps required) prevents thrashing.

## Environment variables

Common env vars across daemons:

| Variable | Default | Notes |
|----------|---------|-------|
| `CHITRA_STATE_DIR` | `/var/lib/chitra` | Base directory for queue, ledger, goals, etc. |
| `CHITRA_ROUTING_CONFIG` | Unset | Path to routing.yaml. |
| `CHITRA_POLICY_CONFIG` | Unset | Path to policy.yaml. |
| `CHITRA_NEVER_PAUSE_SESSION_PREFIXES` | Unset | Sessions never to pause (comma-separated). |

See each daemon's documentation for its specific env vars.

## CLI flags

Every daemon accepts `--help` to list its flags. Common patterns:

```bash
dispatchd --queue-dir /var/lib/chitra/queue --once
chitra-monitord --state-dir /var/lib/chitra --once
rate-limit-guard --policy-config /etc/chitra/policy.yaml
```

## Running with systemd

The Debian package installs the shared daemon units from
`packaging/systemd/`. The checked-in units are the canonical service contract:

- `chitra-dispatchd.service`
- `chitra-monitord@.service.example`

They use the released virtual environment at `/opt/chitra/venv` and the
declaration at `/etc/chitra/lanes.yaml`. A fleet deployment that uses isolated
instance templates owns those templates in the fleet repository; do not copy a
shared unit into an instance-specific service name.

Legacy `watchd`, `triaged`, and `sweepd` units remain shipped for existing
declarations. New deployments use monitord and dispatchd.

## Example policy walkthrough

To understand how policy works, walk through a scenario:

1. **Session enrolls with goal:** "Implement user authentication."
2. **Dispatchd delivers messages:** Updates go into the session queue; policy.dispatch.banned_attribution_patterns checks that nudges don't falsely claim operator authorship.
3. **Session finishes and claims completion:** Monitord runs the enrolled validators and checks the stored receipts.
4. **Completion evidence fails:** The goal stays disputed and monitord continues supervision.
5. **Protected choice remains:** The operator decides only the gated choice through the existing decision path.
6. **Rate-limit-guard runs:** Checks account usage against policy.usage thresholds. If Claude usage is >92% of 5-hour window, pauses the session.
7. **Host load spike:** Memory drops below policy.load.l2_mem_available_pct (15%). Sessions are capped at l2_max_running (4 instead of 8).

## See Also

- **[Routing feedback and usage](../routing-feedback-usage.md)** — Advanced routing tuning.
- **[Self-tuning](../self-tuning.md)** — Automated policy feedback loop.
- **[Pause and recovery](../pause-recovery.md)** — Detailed pause/resume mechanics.
- **[Rate-limit-guard](../daemons/delivery/rate-limit-guard.md)** — How policy is applied.

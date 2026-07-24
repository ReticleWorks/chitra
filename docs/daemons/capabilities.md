# chitra-capabilities — Runtime Authorization

Chitra-capabilities loads a capability manifest describing chitra's console surfaces and applies reversible runtime authorization toggles. It never executes commands or contacts remote services; it only manages permissions.

## Subcommands

### list — Show all capabilities

```bash
chitra-capabilities list --root /var/lib/chitra --json
```

Lists all defined capabilities (JSON or human-readable).

### show — Details for one capability

```bash
chitra-capabilities show --root /var/lib/chitra --capability dispatch_control
```

Shows current status (enabled/disabled), authority level, expiry.

### enable — Authorize a capability

```bash
chitra-capabilities enable \
  --root /var/lib/chitra \
  --capability dispatch_control \
  --reason "Enabling dispatch for new deployment" \
  --actor "operator:trey" \
  --until "2025-02-01T00:00:00Z"
```

Sets a capability to enabled. Can auto-expire (--until flag). Authority level determines who can enable/disable.

### disable — Revoke a capability

```bash
chitra-capabilities disable \
  --root /var/lib/chitra \
  --capability dispatch_control \
  --reason "Pausing dispatch during security audit"
```

Sets a capability to disabled. Persists until explicitly reset.

### reset — Clear a toggle

```bash
chitra-capabilities reset \
  --root /var/lib/chitra \
  --capability dispatch_control
```

Reverts a capability to its shipped default state.

### brief — Human-readable status

```bash
chitra-capabilities brief --root /var/lib/chitra
```

Shows current capabilities status (enabled/disabled, reason, expiry).

## Capability levels

Capabilities have authority levels:

- **observe:** Read-only surfaces (view state, query history).
- **record:** Append to logs and queues (dispatch orders, goals, artifacts).
- **act:** Execute control commands (pause, resume, redirect).

Daemon capabilities (dispatchd, triaged, watchd) are not user-toggleable; they ship enabled.

## Common tasks

**Disable dispatch temporarily:**

```bash
chitra-capabilities disable \
  --root /var/lib/chitra \
  --capability dispatch_control \
  --reason "Operator paused dispatch for maintenance"
```

Dispatchd checks this flag on each pass and skips delivery if disabled.

**Enable dispatch with expiry:**

```bash
chitra-capabilities enable \
  --root /var/lib/chitra \
  --capability dispatch_control \
  --reason "Resume after maintenance window" \
  --until "2025-01-15T14:00:00Z"
```

At 14:00, the capability auto-expires and reverts to disabled.

**Check current status:**

```bash
chitra-capabilities show \
  --root /var/lib/chitra \
  --capability dispatch_control
```

**Audit capability changes:**

```bash
cat /var/lib/chitra/capabilities-audit.jsonl | jq '.[] | select(.actor == "operator:trey")'
```

Chitra logs all enable/disable/reset actions to an audit log.

## Manifest format

The capabilities manifest (shipped with chitra) defines:

```json
{
  "capabilities": [
    {
      "name": "dispatch_control",
      "description": "Enable/disable message delivery",
      "authority": "act",
      "default": "enabled",
      "requires_reason": true,
      "audit_required": true
    },
    {
      "name": "goal_enforcement",
      "description": "Enable/disable completion review",
      "authority": "act",
      "default": "enabled"
    }
  ]
}
```

## State storage

Current capability state is stored in JSON:

```json
{
  "dispatch_control": {
    "enabled": true,
    "reason": "Resuming after maintenance",
    "actor": "operator:trey",
    "expires_at": "2025-02-01T00:00:00Z",
    "set_at": "2025-01-15T12:34:56Z"
  }
}
```

## Audit log

Every enable/disable/reset is logged to an append-only audit file:

```jsonl
{"timestamp": "2025-01-15T12:34:56Z", "action": "disable", "capability": "dispatch_control", "actor": "operator:trey", "reason": "Pausing dispatch during security audit"}
{"timestamp": "2025-01-15T14:00:00Z", "action": "auto_expire", "capability": "dispatch_control", "previous_state": "enabled"}
```

## Integration with daemons

Daemons check capability state before acting:

- **dispatchd** checks `dispatch_control`. If disabled, it skips delivery.
- **watchd** checks `goal_enforcement`. If disabled, completion reviews are not run.
- **rate-limit-guard** checks `rate_limit_control`. If disabled, pause/resume nudges are not sent.

If a required capability is missing or unknown, the daemon fails safely (does not act, logs error).

## See Also

- **[Configuration](../configuration.md)** — Policy settings that interact with capabilities.

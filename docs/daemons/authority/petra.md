# petra — Observe-Only Authority

Petra is a dark-launch (observe-only) authority that validates advisory "pressure observation" events from Watchtower (a fleet supervisor). Petra records observations to SQLite but never acts on them. **By design, petra has no executor and cannot hold, signal, or control any workload.**

## What it does

**On each event received:**

1. Validate the event envelope:
   - Check schema: `petra.pressure-observation.v1`.
   - Verify timestamp (not >5 minutes stale, not in the future).
   - Validate evidence digest.
2. Cross-check ownership:
   - Query chitra's ownership provider: does this host:lane:instance belong to a canonical managed lane?
   - Revalidate after the round trip.
3. Record to SQLite:
   - One row per event (idempotent on matching digest).
   - 10,000-event ledger cap; old rows are never silently pruned.
4. Never act:
   - No hold, no signal, no renice, no process control.
   - Pure observation and record-keeping.

Petra is fail-closed: if validation fails, the event is rejected and logged as "unavailable" or "unknown."

## CLI usage

Petra runs as a systemd service listening on a Unix socket:

```bash
petra \
  --socket-path /run/chitra-petra/petra.sock \
  --ownership-socket-path /run/chitra-ownership/provider.sock \
  --ledger-path /var/lib/chitra-petra/decision-outbox.sqlite3 \
  --host-uuid $(uuidgen) \
  --timeout-seconds 10
```

Watchtower connects to the socket and sends pressure observations. Petra logs them to the SQLite database.

## Key flags

| Flag | Default | Notes |
|------|---------|-------|
| `--socket-path` | `/run/chitra-petra/petra.sock` | Unix socket for receiving events. |
| `--ownership-socket-path` | `/run/chitra-ownership/provider.sock` | Socket for querying ownership. |
| `--ownership-peer-user` | `chitra` | User running ownership provider. |
| `--ledger-path` | `/var/lib/chitra-petra/decision-outbox.sqlite3` | SQLite database for observations. |
| `--health-path` | Unset | Optional health check file. |
| `--host-uuid` | Required | UUID identifying this host. |
| `--timeout-seconds` | 10 | Socket timeout. |

## Event validation

Petra validates each event:

- **Schema:** Must be `petra.pressure-observation.v1`.
- **Timestamp:** Must be current (within ±5 min, no future dates).
- **Evidence digest:** Must hash correctly.
- **Ownership:** Must query ownership provider and verify the lane belongs to chitra.

If any check fails, the event is rejected with a reason: "schema_invalid", "timestamp_stale", "timestamp_future", "evidence_invalid", "ownership_unknown", etc.

## SQLite schema

The ledger table has:

```sql
CREATE TABLE observations (
  id INTEGER PRIMARY KEY,
  timestamp TEXT,
  host_uuid TEXT,
  lane_id TEXT,
  instance TEXT,
  digest TEXT UNIQUE,
  pressure_level TEXT,
  evidence_summary TEXT,
  recorded_at TEXT
);
```

Petra never deletes rows; it only appends. The 10,000-row cap is a hard limit; new events are rejected if the cap is reached and no rows have been pruned explicitly.

## Common tasks

**Check recorded observations:**

```bash
sqlite3 /var/lib/chitra-petra/decision-outbox.sqlite3 "SELECT * FROM observations ORDER BY timestamp DESC LIMIT 10;"
```

**Verify ownership:**

Petra queries ownership-provider automatically. To test it manually:

```bash
curl --unix-socket /run/chitra-ownership/provider.sock \
  -X POST -d '{"host":"localhost","lane_id":"session-1","instance":"1"}' \
  http://localhost/check
```

**Run as a systemd service:**

See `packaging/systemd/chitra-petra.service.example` in the repo.

## Dark-launch notes

Petra is observe-only by design. It cannot:

- Pause or resume sessions.
- Send signals to processes.
- Adjust process priority.
- Inject code or modify state.
- Make any control decision.

It only records what Watchtower observes. This allows fleet-wide pressure monitoring without needing to grant Petra executor privileges. A separate orchestration layer (e.g., rate-limit-guard) makes pause/resume decisions based on its own logic.

## See Also

- **[Petra Authority](../petra-authority.md)** — Deep dive on petra's authority model and how it integrates with chitra.
- **[Ownership Provider](ownership-provider.md)** — Petra's authority-checking partner.

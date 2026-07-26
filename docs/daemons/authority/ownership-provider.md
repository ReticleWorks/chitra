# ownership-provider — Read-Only Ownership Authority

Ownership-provider answers a single question: "Does this host:lane:instance reference belong to a canonical managed lane?" It is read-only, fail-closed, and never discovers sessions or scans processes.

## What it does

**On each query:**

1. Read two files:
   - `goals.json` — canonical goal store.
   - `goals.managed.json` — digest marker written by the manager.
2. Validate:
   - Both files exist.
   - Their digest hashes match (proof of consistency).
   - Timestamps are fresh (within max-age, typically 30 seconds).
   - Host ID and boot ID match the query.
3. Return:
   - **owned:** The lane belongs to chitra. Safe for petra and other observers to trust.
   - **unowned:** The lane is unknown or belongs to a different system.
   - **unknown:** One of the validation checks failed (stale files, digest mismatch, etc.).

Ownership-provider never discovers sessions, scans processes, or mutates state. It only reads files.

## CLI usage

Ownership-provider runs as a systemd service listening on a Unix socket:

```bash
ownership-provider \
  --socket-path /run/chitra-ownership/provider.sock \
  --state-dir /var/lib/chitra \
  --marker-path /var/lib/chitra/goals.managed.json \
  --host-id $(hostname) \
  --boot-id-file /proc/sys/kernel/random/boot_id \
  --state-max-age-seconds 30 \
  --validity-seconds 5
```

Petra and other services query it via the socket.

## Key flags

| Flag | Default | Notes |
|------|---------|-------|
| `--socket-path` | `/run/chitra-ownership/provider.sock` | Unix socket for queries. |
| `--state-dir` | `/var/lib/chitra` | Base state directory (where goals.json lives). |
| `--marker-path` | `$state_dir/goals.managed.json` | Digest marker file. |
| `--generation-fence-path` | Unset | Optional generation fence file. |
| `--state-owner-user` | `chitra` | Expected owner of state files. |
| `--host-id` | Required | Host identifier (e.g., hostname, UUID). |
| `--boot-id` | Unset | Boot ID (alternatively, use --boot-id-file). |
| `--boot-id-file` | `/proc/sys/kernel/random/boot_id` | File to read boot ID from. |
| `--instance-id` | Unset | Optional instance identifier. |
| `--state-max-age-seconds` | 30 | Max age of goals.json / marker. |
| `--validity-seconds` | 5 | Validity window for responses. |
| `--timeout-seconds` | 10 | Socket timeout. |

## Validation flow

Ownership-provider validates queries in this order:

1. **Schema:** Does the query have required fields (host, lane_id)?
2. **Files exist:** Are goals.json and goals.managed.json present?
3. **Digest match:** Do their hashes match (proof they're in sync)?
4. **Freshness:** Are both files younger than state-max-age-seconds?
5. **Host match:** Does the query's host ID match the canonical marker?
6. **Boot match:** Does the query's boot ID match (if provided)?
7. **Generation fence:** Has the generation advanced since last query (if enabled)?

If all checks pass, the lane is **owned**. If any fail, it's **unowned** or **unknown** with a reason.

## Query and response

Query:

```json
{
  "host": "localhost",
  "lane_id": "session-1",
  "instance": "1",
  "boot_id": "12345678"
}
```

Response:

```json
{
  "status": "owned",
  "reason": "goals match, freshness ok",
  "timestamp": "2025-01-15T12:34:56Z",
  "validity_until": "2025-01-15T12:35:01Z"
}
```

## Generation fence

The generation fence prevents rollback attacks. If enabled:

- Each snapshot of goals.json is tagged with a generation number.
- Queries must have generation ≥ last-seen generation.
- A lower generation is rejected ("snapshot_rolled_back").

This ensures that a stale snapshot of goals.json cannot be used to claim a lane is owned.

## Common tasks

**Check if a lane is owned:**

```bash
curl --unix-socket /run/chitra-ownership/provider.sock \
  -X POST -d '{"host":"localhost","lane_id":"session-1","instance":"1"}' \
  http://localhost/check
```

**Run as a systemd service:**

See `packaging/systemd/chitra-ownership-provider.service.example` in the repo.

**Monitor ownership queries:**

Enable debug logging to see all queries and validation results.

## Fail-closed behavior

If ownership-provider cannot validate a query (files missing, digest mismatch, stale timestamps), it returns **unknown** with a reason. It never guesses or assumes ownership. Downstreams (like petra) must handle the unknown case safely (usually reject the request).

## See Also

- **[Petra Authority](../../petra-authority.md)** — How ownership-provider is used by petra and other authorities.
- **[Petra](petra.md)** — The dark-launch observer that queries ownership-provider.

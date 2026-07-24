# draft-scanner — Unsubmitted Draft Detection

Draft-scanner scans tmux input boxes for unsubmitted operator drafts and flags them. It never submits or discards anything; it only observes and reports.

## CLI usage

```bash
draft-scanner \
  --targets "host1:session-1:0 host2:session-2:1 localhost:agent:2"
```

Scans the specified tmux panes for unsubmitted drafts. Output is JSON.

## Arguments

| Argument | Required | Notes |
|----------|----------|-------|
| `--targets` | Yes | Space-separated host:session:pane references. |

## Output format

```json
{
  "session_ref": "host:session:pane",
  "has_draft": true,
  "last_line": "echo 'hello' && echo 'world'",
  "tail_hash": "abc123def456",
  "timestamp": "2025-01-15T12:34:56Z"
}
```

Fields:

- **session_ref:** Target pane reference.
- **has_draft:** true if unsubmitted text detected.
- **last_line:** The last line of input (or empty).
- **tail_hash:** SHA256 of the tail (for dedup).
- **timestamp:** When the scan occurred.

Unreachable targets (pane doesn't exist, host unreachable) are recorded as errors:

```json
{
  "session_ref": "host3:nonexistent:0",
  "error": "pane not found",
  "timestamp": "2025-01-15T12:34:56Z"
}
```

## Common tasks

**Scan for drafts before pausing sessions:**

```bash
draft-scanner --targets "localhost:agent-1:0 localhost:agent-2:0" | jq '.[] | select(.has_draft)'
```

**Monitor a single session:**

```bash
watch -n 5 'draft-scanner --targets "localhost:my-session:0"'
```

**Check for errors (unreachable sessions):**

```bash
draft-scanner --targets "host1:a:0 host2:b:0" | jq '.[] | select(.error)'
```

## How it works

Draft-scanner:

1. Connects to each target pane via tmux.
2. Captures the current text in the input line.
3. Computes a SHA256 hash of the tail (for dedup).
4. Outputs the result (JSON or text).

It does NOT:

- Submit the draft (hit Enter).
- Discard the draft.
- Modify the pane.
- Interact with the session's logic.

## Integration with rate-limit-guard

When rate-limit-guard is about to pause a session, it can optionally check for unsubmitted drafts first:

```bash
draft-scanner --targets "localhost:session:0" | jq '.[] | select(.has_draft)'
# If drafts exist, delay pause and alert operator.
```

## See Also

- **[Rate-limit-guard](rate-limit-guard.md)** — May use draft-scanner before pausing.

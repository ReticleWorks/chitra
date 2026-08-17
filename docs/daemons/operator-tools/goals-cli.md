# chitra-goals — Goal Management CLI

Chitra-goals is the user-facing CLI for managing session goals. It wraps the deterministic goals store (goals.py) and provides subcommands for enrollment, status queries, closure, holds, and ask/resolution workflows.

## Subcommands

### set — Enroll a session

```bash
chitra-goals set \
  --session-ref agent-1 \
  --goal "Implement user authentication for the API" \
  --done-when "API login endpoint accepts username+password and returns JWT" \
  --source "operator:trey" \
  --open-ask "Retrieve current auth library version"
```

Creates or updates a goal. On first write, the goal is immutable (lane_id, enrolled_done_when, enrolled_at are write-once). On later writes, only status and reasoning can change. `set` never moves the enrolled anchors. Only `redirect` can, and only under the condition described in its section below.

Validates:
- Goal text is ≥6 words.
- done_when is non-empty and plain language.
- Status is one of: open, working, paused, held, complete, abandoned, redirected, deferred.

### get — Query a goal

```bash
chitra-goals get --session-ref agent-1
```

Returns the current goal record (JSON or human-readable format).

### list — List all goals

```bash
chitra-goals list --format cards
```

Formats: markdown (default), box (fixed-width Unicode), cards (full sentences).

### close — Complete a goal

```bash
chitra-goals close \
  --session-ref agent-1 \
  --delivered-item "API login endpoint at POST /auth/login" \
  --delivered-item "JWT token generation and validation" \
  --close-note "All tests passing. Ready for review."
```

Blocks closure if the inventory count is short. Performs inventory-check: counts delivered items against enrolled goal conditions. If discrepancies exist, reports them and asks for operator acknowledgement.

### hold — Pause a goal

```bash
chitra-goals hold \
  --session-ref agent-1 \
  --reason "Awaiting design review feedback"
```

Sets status to "held". Rate-limit-guard may also hold a session automatically.

### resume — Unpause a goal

```bash
chitra-goals resume --session-ref agent-1
```

Sets status back to "open" or "working".

### redirect — Change goal mid-stream

```bash
chitra-goals redirect \
  --session-ref agent-1 \
  --goal "Implement user authentication (updated scope: add MFA)" \
  --done-when "API login and MFA endpoints working" \
  --reason "Operator request: add MFA requirement"
```

Updates the goal with a reason. The old goal is kept in history, and the redirect reason is recorded.

A redirect that changes `done_when` also re-freezes the enrollment anchors. It sets
`enrolled_done_when` to the new `done_when` and stamps `enrolled_at` with the redirect
time. This is what keeps the lane launchable. Without it, every later
`chitra-lane-launch` refuses with `lane launch refused: lane identity or frozen done_when
snapshot does not match`, and the only way out is editing the governed store by hand.

A redirect that leaves `done_when` unchanged keeps the original enrollment anchors.

### now — Current status

```bash
chitra-goals now --session-ref agent-1
```

Quick view of current status (open/working/paused/complete/etc).

### check — Lint a goal

```bash
chitra-goals check --goal "My goal text"
```

Validates goal text (≥6 words, plain language, clear done_when). No state change; pure validation.

### guidance — Canonical decision references

```bash
chitra-goals guidance --working-dir /path/to/session
```

Looks up canonical decision documents for the working directory (from policy config). Helpful for operator reference during goal setting.

### due — Due date management

```bash
chitra-goals due \
  --session-ref agent-1 \
  --set "2025-01-20T00:00:00Z"
```

Set or view goal due date.

### ask / resolve-ask — Open asks workflow

```bash
# Record an open ask
chitra-goals add-ask \
  --session-ref agent-1 \
  --open-ask "Clarify API versioning strategy"

# Resolve it
chitra-goals resolve-ask \
  --session-ref agent-1 \
  --ask-index 0 \
  --resolution "API v1.0 frozen; new features go to v2 branch"
```

Track questions/blockers during goal work.

### scan-asks — List all open asks

```bash
chitra-goals scan-asks
```

Fleet view of all unresolved asks across all goals.

### roster — Operator board

```bash
chitra-goals roster --format box
```

Renders the operator-facing terminal board (goals + unreviewed artifacts). This is the main dashboard.

## Common tasks

**Enroll a new session:**

```bash
chitra-goals set \
  --session-ref my-agent \
  --goal "Build a REST API for task management" \
  --done-when "POST /tasks creates a task; GET /tasks returns all tasks; DELETE /tasks/:id removes a task. All tests pass." \
  --source "operator:trey"
```

**View the fleet board:**

```bash
chitra-goals roster --format box
```

**Close a completed goal:**

```bash
chitra-goals close \
  --session-ref my-agent \
  --delivered-item "POST /tasks endpoint with validation" \
  --delivered-item "GET /tasks list endpoint" \
  --delivered-item "DELETE /tasks/:id endpoint" \
  --delivered-item "100 tests passing" \
  --close-note "Ready for production"
```

**Check for linting issues:**

```bash
chitra-goals check --goal "Add feature"  # Fails: <6 words
chitra-goals check --goal "Implement comprehensive user authentication system"  # Passes
```

## Validation and constraints

- **Goal text:** Minimum 6 words, plain language, no abbreviations.
- **Done_when:** Non-empty, plain language, clear observable condition.
- **Status:** One of 8 values (open, working, paused, held, complete, abandoned, redirected, deferred).
- **Lane ID:** Immutable once set; prevents re-enrollment under a fresh session ref.
- **Enrolled anchors:** First write creates enrolled_done_when and enrolled_at. Later writes are checked against these. The single exception is a `redirect` that changes done_when, which re-freezes both.

## Output formats

- **json:** Raw JSON goal record.
- **markdown:** Markdown rendering (default for list).
- **box:** Fixed-width Unicode table (good for terminals).
- **cards:** Full sentences, one per line (good for dashboards).

## See Also

- **[Concepts — Goals and Completion Gating](../../concepts/README.md#goals-and-completion-gating)** — How goals work in chitra.
- **[Watchd](watchd.md)** — Completion review (checks if done_when is satisfied).

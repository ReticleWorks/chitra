# chitra-goals — Goal Management CLI

Chitra-goals is the user-facing CLI for managing session goals. It wraps the deterministic goals store (goals.py) and provides subcommands for enrollment, status queries, closure, holds, and ask/resolution workflows. Enrollment freezes the goal's `AutonomyPolicy` alongside its completion contract.

## Subcommands

### set — Enroll a session

```bash
chitra-goals set \
  --session-ref agent-1 \
  --goal "Implement user authentication for the API" \
  --source "task-file:auth-task.md"
```

The first call returns `INTERVIEW_REQUIRED` JSON with a nonce and four questions. It writes no goal. Record the answers, their `operator:` or `source:` provenance, and at least one structured done item in an `INTERVIEW_RESULT` file. Then repeat the same command with `--interview-result result.json`. That call atomically stores the receipt, frozen items, generated display `done_when`, `enrolled_at`, and `lane_id`.

Validates:
- Goal text is ≥6 words.
- Every interview answer and its provenance are present.
- At least one done item names its ID, text, validator, and required receipt.
- Tactical goal status is one of the internal values accepted by the goals store. It is not the lane lifecycle. The public lane lifecycle has exactly four states: `active`, `paused`, `shelved`, and `closed`.

### Autonomy policy

By default, enrollment freezes the aggressive, goal-scoped `chitra.autonomy.v1`
policy. It grants replanning, small redesign, dependency, schema, hook,
credential, authentication, security, and irreversible capabilities against
the goal target, including spending. It pursues after one clean idle pass, and
Claude's recurring hook runs every five minutes. Use one of these mutually
exclusive options to replace that default:

```bash
chitra-goals set ... --autonomy-policy /path/to/policy.json
chitra-goals set ... --autonomy-policy-json '{"schema":"chitra.autonomy.v1","initiative":"aggressive","grants":[]}'
```

An aggressive policy can narrow the default with a quantitative spend ceiling
and a per-goal idle threshold:

```bash
chitra-goals set ... --autonomy-policy-json '{"schema":"chitra.autonomy.v1","initiative":"aggressive","idle_pursuit_passes":2,"loop_interval_minutes":10,"grants":[{"grant_id":"replan-goal","capability":"replan","targets":["goal"]},{"grant_id":"redesign-goal","capability":"small_redesign","targets":["goal"]},{"grant_id":"spend-usd-50","capability":"spend","targets":["goal"],"max_amount":"50","currency":"USD"}]}'
```

Each grant names a capability and may restrict its target, amount, unit count,
or expiry. A policy is frozen into the goal digest. A redirect must provide a
replacement policy when its strategic change needs different authority.

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
  --session-ref agent-1
```

Completion close requires `done-pending-close` and repeats the exact item ID, receipt name, validator result, and citation check over Watchd's persisted proofs. `--delivered-item` and `--operator-acknowledged-item` are rejected as completion substitutes. Use `--administrative --reason "..."` to discard a dead record without claiming the work is done.

### hold — Hold a goal

```bash
chitra-goals hold \
  --session-ref agent-1 \
  --reason "Awaiting design review feedback"
```

Sets the internal goal status to `held` without discarding the goal.
Rate-limit-guard may also hold a session automatically. The lane lifecycle
state remains one of `active`, `paused`, `shelved`, or `closed`.

### resume — Unpause a goal

```bash
chitra-goals resume --session-ref agent-1
```

Returns an explicitly held goal to its working status. Lifecycle resume also
validates the saved worktree checkpoint before active enforcement returns.

### redirect — Change goal mid-stream

```bash
chitra-goals redirect \
  --session-ref agent-1 \
  --goal "Implement user authentication (updated scope: add MFA)" \
  --reason "Operator request: add MFA requirement"
```

Updates strategic fields with a reason and records the prior values in history. Frozen done items and the generated `done_when` cannot be redirected. The operation is labeled as not done.

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

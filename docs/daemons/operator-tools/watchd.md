# watchd — Completion Reviewer

Watchd watches tmux panes for session activity and completion claims. When a session claims it is done, watchd launches isolated LLM reviewer processes to judge whether the claim matches the frozen goal. **This is where chitra makes real LLM calls.**

## What it does

**On each poll interval:**

1. List all tmux panes matching the session prefix filter.
2. For each pane:
   - Capture its current content.
   - Compute a SHA256 hash of the content.
   - If the hash differs from the last known state, a change occurred. Emit an event.
   - If the pane shows a completion claim marker, enqueue a completion review.
3. For each pending review:
   - Spawn an isolated `claude -p` reviewer process (bounded concurrency, default max 2 running at once).
   - Pass the frozen goal and the pane content to the reviewer.
   - Wait for the verdict: accept, reject, or unavailable.
   - Record the verdict to completion_reviews.jsonl.

The reviewer processes are isolated. They run on their own and never:

- Draft chitra's messages.
- Mutate chitra's state.
- Bypass operator gates.
- Share context with other reviewers.

Watchd itself remains responsive; it does not wait for reviewers to finish before continuing to poll.

## CLI usage

```bash
watchd \
  --state-dir /var/lib/chitra \
  --events-log /var/lib/chitra/events.log \
  --session-prefix agent \
  --interval-seconds 5 \
  --reviewer-count 2 \
  --reviewer-model claude-opus
```

Omit `--once` to run continuously. Provide `--once` to run one poll and exit.

## Key flags

| Flag | Default | Notes |
|------|---------|-------|
| `--state-dir` | `/var/lib/chitra` | Base state directory. |
| `--events-log` | `$CHITRA_STATE_DIR/events.log` | Where to write pane-change events. |
| `--session-prefix` | Unset (all sessions) | Match only sessions starting with this prefix. |
| `--exclude-session-prefix` | None | Exclude sessions starting with this prefix. |
| `--interval-seconds` | 5 | Seconds between polls. |
| `--panes` | All visible | Target specific pane references (e.g., `session:window.pane`). |
| `--reviewer-count` | 2 | Max concurrent LLM reviewer processes. |
| `--reviewer-model` | `claude` | Model to use for reviewers. |
| `--reviewer-command` | `claude` | CLI command to invoke (e.g., `claude`, `codex`). |
| `--reasoned-dispatch` | false | Enable reasoned dispatch on rejection. |
| `--once` | False | Run one poll and exit. |

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `CHITRA_WATCHD_EVENT_LOG` | `/var/lib/chitra/events.log` | Path to events log. |
| `CHITRA_WATCHD_INTERVAL` | 5 | Poll interval in seconds. |
| `CHITRA_WATCHD_PANES` | None | Target panes (space-separated). |
| `CHITRA_WATCHD_SESSION_PREFIXES` | None | Session prefix filter. |
| `CHITRA_WATCHD_MAX_LOG_BYTES` | 5242880 (5 MiB) | Rotate log at this size. |
| `CHITRA_WATCHD_REVIEWER_COUNT` | 2 | Max concurrent reviewers. |
| `CHITRA_WATCHD_REVIEWER_COMMAND` | `claude` | Reviewer CLI. |

## Events log format

Watchd emits events to an JSONL log. Each line is ISO8601 timestamp, lane ID, and event text:

```
2025-01-15T12:34:56Z agent-session output_changed
2025-01-15T12:34:57Z agent-session completion_claimed
2025-01-15T12:35:02Z agent-session completion_reviewed accept
```

## Completion reviews

Watchd spawns a reviewer process for each completion claim. The reviewer reads:

- The session's frozen goal.
- The session's current pane output.

The reviewer outputs a verdict:

- **accept:** The output satisfies the goal.
- **reject:** The output does not satisfy the goal (e.g., uses deferral language like "future work").
- **unavailable:** The reviewer could not run or could not decide.

The verdict is recorded to `completion_reviews.jsonl` with a timestamp, goal state, and reasoning (if available).

## Common tasks

**Watch a single session:**

```bash
watchd --session-prefix my-agent --interval-seconds 5
```

**Run as a systemd service (continuous):**

See `packaging/systemd/chitra-watchd.service.example` in the repo.

**View completion verdicts:**

```bash
cat /var/lib/chitra/completion_reviews.jsonl | jq .
```

**See pane-change events:**

```bash
tail -f /var/lib/chitra/events.log
```

## Reviewer process isolation

Each reviewer is a subprocess:

```bash
claude -p << 'EOF'
Goal: <frozen goal>

Session output:
<pane content>

Is the session output complete against the goal?
EOF
```

The reviewer never:

- Sees chitra's internal state.
- Receives context from other reviewers.
- Drafts messages to the session.
- Has access to operator gates or credentials.

The verdict goes back to watchd as structured output (accept/reject) and is recorded to the completion_reviews.jsonl log.

## See Also

- **[Concepts — Goal Enforcement](../concepts.md#goal-enforcement-and-completion-review)** — How completion review fits into chitra's architecture.
- **[Design notes — Bounded reasoning boundary](../DESIGN.md#bounded-reasoning-boundary)** — Why reviewers are isolated.

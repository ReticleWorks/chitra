# watchd — Semantic Status and Completion Review

> **Deprecated.** Superseded by the composed [`monitord`](../monitord.md)
> entrypoint; kept for existing declarations only.

Watchd classifies supervised tmux panes, publishes semantic status, and runs
isolated completion reviewers. Status classification is deterministic. The
completion reviewer is the only part of this path that calls an LLM.

## Status authority

Watchd resolves each pane through one ordered authority chain:

1. A lifecycle integration report for the exact pane and session is
   authoritative. Watchd skips screen classification for that pane.
2. Otherwise, Watchd evaluates a local agent-detection TOML manifest.
3. Without a local override, Watchd evaluates the bundled manifest.
4. An unmatched or unknown screen is idle, with an explicit fallback label.

A screen can be blocked only when a manifest matches a recognized visible
approval, question, or permission interface. Ambiguous output is idle. See
[Agent detection manifests](../../agent-detection-manifests.md) for the full
format and precedence rules.

## Poll and completion behavior

On each interval, Watchd lists the allowed panes, captures each live bottom
buffer, and updates the shared semantic-status broker. It writes an
`AGENT_STATUS` event when status or authority changes. Unrelated screen edits
do not author semantic status.

When a pane moves from working to idle at a visible input row, Watchd checks
for a completion claim. It submits a matching claim to an isolated reviewer,
records the result in `completion_reviews.jsonl`, and publishes `done` only
when the completion gate reaches `done-pending-close`.

Reviewers never draft Chitra messages, mutate Chitra state, bypass operator
gates, or share context with another reviewer. Watchd remains responsive while
they run.

## Local coordination socket

The continuously running daemon serves a mode-`0600` Unix socket at
`/run/chitra/chitra.sock` by default. The protocol is newline-delimited JSON.
Every request has an `id`; responses and subscription events echo it.

Common commands are:

```bash
chitra-agent report --source codex-hook --agent codex --state working
chitra-agent explain --pane-id "$CHITRA_PANE_ID"
chitra-agent wait --pane-id "$CHITRA_PANE_ID" --until done --timeout-ms 600000
chitra-agent schema --output chitra-api.schema.json
```

`chitra-agent` reads `CHITRA_PANE_ID`, `CHITRA_SESSION_REF`, and
`CHITRA_SOCKET_PATH` when the matching arguments are omitted. Use
`chitra-agent clear-authority` when an integration intentionally stops
reporting for a pane.

The socket also supports persistent `events.subscribe` requests with typed
pane, session, lane, agent, and status filters. See
[Semantic agent status](../../agent-status-design.md) for the request examples,
predicate operators, wait semantics, and schema.

## CLI usage

```bash
watchd \
  --state-dir /var/lib/chitra \
  --events-log /var/lib/chitra/events.log \
  --tmux-socket /run/chitra-worker/tmux-1000/default \
  --session-prefix agent \
  --agent-manifest-dir /etc/chitra/agent-detection \
  --socket-path /run/chitra/chitra.sock \
  --interval-seconds 5 \
  --reviewer-count 2
```

Omit `--once` to run continuously. `--once` performs one deterministic screen
classification pass and exits; it does not serve the socket.

## Key flags

| Flag | Default | Notes |
|---|---|---|
| `--state-dir` | `/var/lib/chitra` | Status snapshots, completion records, and ownership lease. |
| `--events-log` | `$CHITRA_STATE_DIR/events.log` | Semantic status transition log. |
| `--tmux-socket` | tmux default | Exact tmux server socket. |
| `--session-name` | Unset | Exact session allowlist; repeatable. |
| `--session-prefix` | Unset | Observe sessions with this prefix; repeatable. |
| `--agent-manifest-dir` | Chitra config directory | Local TOML overrides. |
| `--socket-path` | `/run/chitra/chitra.sock` | Local coordination socket. |
| `--handoff-from` | Unset | Replace the compatible server at this socket after verified handoff. |
| `--interval-seconds` | `5` | Seconds between observations. |
| `--panes` | All allowed | Controlled comma-separated target override. |
| `--reviewer-count` | `2` | Maximum concurrent LLM reviewer processes. |
| `--reviewer-model` | Ambient | Optional isolated reviewer model override. |
| `--once` | False | Observe once without starting the socket server. |

`--idle-threshold-seconds` remains accepted for one migration window, but it
does not author status. Remove it from service configuration.

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `CHITRA_SOCKET_PATH` | `/run/chitra/chitra.sock` | Local coordination socket. |
| `CHITRA_AGENT_MANIFEST_DIR` | Chitra config directory | Local manifest overrides. |
| `CHITRA_WATCHD_EVENT_LOG` | `/var/lib/chitra/events.log` | Semantic event log. |
| `CHITRA_WATCHD_INTERVAL` | `5` | Poll interval in seconds. |
| `CHITRA_WATCHD_PANES` | Unset | Comma-separated target override. |
| `CHITRA_WATCHD_TMUX_SOCKET` | tmux default | Exact tmux server socket. |
| `CHITRA_WATCHD_SESSION_NAMES` | Unset | Comma-separated exact session allowlist. |
| `CHITRA_WATCHD_SESSION_PREFIXES` | Unset | Session prefix filter. |
| `CHITRA_WATCHD_MAX_LOG_BYTES` | `5242880` | Rotate the event log at this size. |
| `CHITRA_WATCHD_REVIEWER_COUNT` | `2` | Maximum concurrent reviewers. |
| `CHITRA_WATCHD_REVIEWER_COMMAND` | `claude` | Reviewer CLI. |

## Event format

Watchd writes one whitespace-delimited record per semantic transition. It does
not copy pane content into the event:

```text
2026-08-14T12:00:00Z lane AGENT_STATUS state=blocked needs operator input pane_id=%17 target=lane:0.0 agent=codex authority=manifest source=package:codex.toml rule=permission_prompt fallback=none
```

`triaged` and `sweepd` continue to consume the timestamp, lane identifier, and
opaque event text. Consumers of the former `CHANGE DETECTED` and delayed
`IDLE` records must migrate. See
[Watchd status migration](../../watchd-status-migration.md).

## Live handoff

To replace a compatible running server without restarting tmux panes, start
the replacement with:

```bash
watchd --lanes-file /etc/chitra/lanes.yaml \
  --handoff-from /run/chitra/chitra.sock
```

The source and replacement verify the state directory, protocol, checksummed
snapshot, target process, exact live tmux pane IDs, socket switch, and durable
ownership lease. Any missing or mismatched proof is `handoff_unknown`; before
commit, the source thaws and retains ownership. Existing clients reconnect
after a successful handoff.

## See also

- [Semantic agent status](../../agent-status-design.md)
- [Agent detection manifests](../../agent-detection-manifests.md)
- [Watchd status migration](../../watchd-status-migration.md)
- [Goal enforcement](../../concepts/README.md#goal-enforcement-and-completion-review)
- [Bounded reasoning boundary](../../DESIGN.md#bounded-reasoning-boundary)

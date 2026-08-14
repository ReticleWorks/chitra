# Semantic agent status, coordination API, and live handoff

Chitra now has one deterministic semantic status path for supervised tmux
lanes. This design borrows five patterns from Herdr and translates them to
Chitra's existing lane, goal, ledger, and tmux boundaries. It does not add an
LLM call to status detection, subscriptions, waits, identity injection, or
handoff.

The design was informed by Herdr's public [agent detection](https://herdr.dev/docs/agents/),
[socket API](https://herdr.dev/docs/socket-api/), and
[session state](https://herdr.dev/docs/session-state/) documentation and its
Apache-2.0 source. Chitra's implementation is Python code written for its own
MIT-licensed architecture; it does not copy Herdr's Rust runtime.

## Translation into Chitra terms

| Herdr concept | Chitra concept |
|---|---|
| Workspace, tab, and pane | Governed lane, tmux target, and server-unique tmux pane ID |
| Server-owned terminal process | Process owned by the existing tmux server |
| Lifecycle hook authority | Integration report sent to Chitra's local socket |
| Screen detection manifest | Chitra TOML manifest evaluated against `watchd`'s live bottom-buffer capture |
| Agent `done` | Chitra's completion gate reached `done-pending-close` |
| Live PTY transfer | Transfer of Chitra's status authority, API socket, and ownership lease; tmux already keeps the pane process alive |

The last distinction is important. Chitra does not parent lane processes.
The tmux server does. A Chitra server replacement must not signal, restart, or
claim ownership of those processes. It transfers only the state Chitra owns
and proves that each recorded tmux pane is still the same live pane.

## One semantic status authority

`chitra.agent_runtime.AgentStatusBroker` is the single semantic status
authority shared by `watchd` and the local socket server.

1. An integration may report `idle`, `working`, or `blocked` for an exact
   `CHITRA_PANE_ID` and optional `CHITRA_SESSION_REF`.
2. While that report remains bound to the same pane and session, it is
   authoritative. `watchd` still captures the pane for completion evidence,
   but it skips manifest classification for status.
3. Without an integration authority, `watchd` evaluates the active local or
   bundled TOML manifest against the live bottom buffer.
4. If no rule matches, the pane is `idle` with
   `default_known_agent_idle_fallback`. An unknown agent is also idle, with
   `unknown_agent_idle_fallback`.
5. `done` is not a screen guess and cannot be reported by an integration. The
   completion gate publishes it after the frozen goal's completion review
   reaches `done-pending-close`.

An integration report is released when the observed pane is bound to a
different session or a caller explicitly uses `pane.clear_agent_authority`.
This prevents a stale hook report from authoring status for a reused pane ID.

## Strict blocked state

Screen-derived `blocked` requires a matched rule whose `blocker_kind` is
`approval`, `question`, or `permission`. The manifest parser rejects a blocked
rule without one of those values. A conforming manifest must match stable UI
controls, so incidental words such as "blocked", "error", or "waiting" do not
produce blocked status by themselves.

An authoritative integration may report blocked without a screen rule. That
is the purpose of the higher authority tier. Explain output identifies that
case as `authority=integration` and
`screen_detection_skip_reason=integration_authoritative`.

The bias is intentional: a false operator interruption is more costly than an
unusual prompt temporarily appearing idle. Status never grants permission or
sends input.

## Local socket API

The socket defaults to `/run/chitra/chitra.sock` and can be changed with
`CHITRA_SOCKET_PATH` or `watchd --socket-path`. It is mode `0600` and uses one
JSON object per newline. Every request requires `id`, `method`, and `params`.
Every response and subscription event echoes the request ID.

Public methods include:

- `pane.report_agent` and `pane.clear_agent_authority`;
- `agent.explain` and `agent.wait`;
- `events.subscribe` with typed pane, session, lane, agent, and status fields;
- `api.schema` and `server.snapshot`;
- the three two-phase handoff methods.

Subscriptions may also use bounded predicates with `all`, `any`, `not`, `eq`,
`in`, and `exists`. Predicates can read only documented event fields and have
a maximum nesting depth. A subscription such as this receives only a blocked
transition from one pane:

```json
{"id":"sub-1","method":"events.subscribe","params":{"subscriptions":[{"type":"pane.agent_status_changed","pane_id":"%17","agent_status":"blocked"}]}}
```

`agent.wait` is condition-variable driven. It does not poll the terminal. A
wait for `done` therefore observes Chitra's completion result, not a prompt,
quiet screen, process exit, or arbitrary command completion.

Run `chitra-agent schema` for the complete JSON Schema document. Run
`chitra-agent explain --pane-id "$CHITRA_PANE_ID"` for the active authority and
evidence. Offline manifest debugging is available with:

```bash
chitra-agent explain --file screen.txt --agent codex
```

## Injected supervised identity

`chitra-lane-session` supplies these variables to the agent process:

| Variable | Meaning |
|---|---|
| `CHITRA_LANE_ID` | Durable declared lane identifier. |
| `CHITRA_SESSION_REF` | Host-qualified Chitra session reference. |
| `CHITRA_PANE_ID` | Server-unique tmux pane ID such as `%17`. |
| `CHITRA_PANE_TARGET` | Stable tmux target such as `lane-name:0.0`. |
| `CHITRA_SOCKET_PATH` | Chitra's local coordination socket. |

Tmux creates `TMUX_PANE` only after the pane exists. The deterministic
`chitra.pane_exec` wrapper validates that value, sets `CHITRA_PANE_ID`, and
then replaces itself with the requested Claude or Codex process. It refuses
to launch when the runtime pane identity is missing or malformed.

## Live handoff

Start a replacement `watchd` with `--handoff-from` pointing at the running
server socket. The source and replacement use this fail-closed protocol:

1. The source checks the protocol and exact resolved state directory, freezes
   status mutations, and verifies every recorded target still resolves to the
   same tmux pane ID.
2. The source returns a short-lived, random transfer token and a checksummed
   status snapshot. The snapshot includes integration authority records.
3. The replacement validates the schema, target process ID, state directory,
   checksum, unique pane identities, and every live tmux pane again.
4. The old socket path is retained as a rollback point. The replacement socket
   is moved into the canonical path, then the source commits an fsync-backed,
   monotonically increasing ownership lease.
5. The replacement reads the lease back. The old server shuts down. Tmux pane
   processes continue unchanged.

Any missing, corrupt, mismatched, or unverifiable state is
`handoff_unknown`. Before commit, the old server thaws and remains the owner.
Socket-path changes roll back if commit fails. Handoff does not move active
API connections, subscriptions, waits, in-flight requests, reviewer futures,
dispatch lane locks, or messages. Clients reconnect and retry transient
operations. Dispatch locks and ledgers remain in their existing state
directory and are neither copied nor bypassed.

## Deterministic boundary

Manifest parsing, regex matching, source precedence, predicates, waits,
identity binding, pane verification, checksums, leases, and socket switching
are pure control and transport logic. Watchd's already-sanctioned isolated
completion reviewers remain the only LLM-backed part of this path, and they
run only after a semantic working-to-idle turn boundary.

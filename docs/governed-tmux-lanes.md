# Governed tmux lanes on Tophand and Trinity

Headless Codexman jobs are not lanes. A Chitra lane is one interactive Codex
or Claude process in its own Tophand tmux session and pane. The launch command
refuses before creating the session unless `chitra-goals` contains a passing
record for `<host>:<session>:0.0` (where host is `tophand` or `trinity`) with `goal`, `done_when`, `intent`, `scope`,
and `source`. It also refuses held or unreadable usage-pause state.

```bash
chitra-lane-session --lane atlas --host tophand --backend claude --model sonnet --effort high start
chitra-lane-session --lane editorial --host tophand --backend claude --model opus --effort max start
chitra-lane-session --lane repair --host tophand --backend codex --model gpt-5.6-sol --effort xhigh start
```

The launcher stores a `chitra.lane-launch.v1` receipt under the lane state
root. It binds the durable lane ID, session reference, backend/model/effort, goal
version, enrollment time, all five strategic goal fields, and their SHA-256.
Unreadable launch or tmux state is `UNKNOWN` and cannot count as complete.

Once launched, existing Chitra machinery supplies lifecycle parity:
`dispatchd` steers the pane and records HMAC delivery evidence; `watchd` and
`lane_activity` observe it; `draft_scanner` protects typed input;
`completion_gate` and `goal_enforcement` review completion; and
`rate_limit_guard` checkpoints, stops, verifies quiescence, and resumes the
same frozen goal. Steering is performed by enqueueing a normal dispatch order
for the lane session reference. The coordinator reads the resulting transcript
and result ledger, waits, and sends its own follow-up; a human is never the
message bus.

## Injected lane identity

The agent process receives identity from the launch path rather than having to
search tmux state:

| Variable | Value |
|---|---|
| `CHITRA_LANE_ID` | Durable lane identifier from the lane declaration. |
| `CHITRA_SESSION_REF` | Host-qualified session reference used by Chitra ledgers. |
| `CHITRA_PANE_ID` | Exact server-unique tmux pane ID, such as `%17`. |
| `CHITRA_PANE_TARGET` | Stable tmux target, such as `lane-name:0.0`. |
| `CHITRA_SOCKET_PATH` | Local Watchd coordination socket. |

The launcher supplies the durable values when it creates the session. Tmux
sets `TMUX_PANE` after creating the pane. Chitra's process wrapper validates
that runtime value, exports it as `CHITRA_PANE_ID`, and then replaces itself
with the selected agent process. A lifecycle hook can therefore call
`chitra-agent report` or `chitra-agent wait` without guessing its coordinates.

See [Semantic agent status](agent-status-design.md) for socket methods and live
handoff, and [Agent detection manifests](agent-detection-manifests.md) for the
screen fallback.

## Herdr coordination contract

| Contract point | Chitra mapping | Coverage after this change |
|---|---|---|
| One agent in each pane | Manifest lane identity plus `chitra-lane-session` | Enforced: one dedicated tmux session and primary pane per lane. |
| Coordinator assigns, waits, reads, and follows up itself | `dispatchd`, signed results/ledger, transcripts, `agent.wait`, and typed status subscriptions | Deterministic transport, semantic waiting, and observation are covered. Coordinator scheduling remains caller policy; Chitra does not run an LLM coordinator. |
| One outcome, deliverable, check, allowed changes, forbidden changes | Goal ingestion fields plus dispatch brief | Partly covered. Outcome/check map to `goal`/`done_when`; intent/scope/source are required. A typed brief field split for “may change” and “must not touch” remains a gap. Put both boundaries explicitly in `scope` until that schema lands. |
| Read-only reviewers share a workspace; every writer gets a worktree | Lane brief/scope and immutable lane identity | Documented contract only. Chitra does not create or verify Git worktrees or filesystem write permissions yet. |
| Executor/reviewer pair with role swap | Two lane identities and normal steering orders | Supported as a coordination pattern, not enforced. Role assignment/swap has no typed state yet. |
| Settle factual disagreements with a test | `done_when`, completion evidence, `completion_gate` | Enforced when the named test is a required completion item; choosing the discriminating test remains coordinator responsibility. |
| Integrate through one checkout and rerun real checks | Completion evidence and goal enforcement | Partly covered. Chitra can require evidence, but does not own the integration checkout or execute repository CI itself. |
| No push or production touch without approval | Brief scope plus operator/authority policy | Explicit boundary, not a repository/host mutation sandbox. External Git and production control planes must enforce approval mechanically. |

The remaining gaps are fail-closed coordination gaps, not permission to infer
success. Missing reviewer/worktree/integration/approval evidence is UNKNOWN.

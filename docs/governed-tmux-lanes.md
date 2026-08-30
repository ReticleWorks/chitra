# Governed tmux lanes on Tophand and Trinity

Headless Codexman jobs are not lanes. A Chitra lane is one interactive Codex
or Claude process in its own Tophand tmux session and pane. The launch command
refuses before creating the session unless `chitra-goals` contains a passing
record for `<host>:<session>:0.0` (where host is `tophand` or `trinity`) with an
interview receipt, at least one frozen structured done item, and the strategic
goal fields. The first `chitra-goals set` returns `INTERVIEW_REQUIRED`; the
paired `set --interview-result <file>` performs the atomic enrollment. The
launcher also refuses held or unreadable usage-pause state.

```bash
chitra-lane-session --lane atlas --host tophand --backend claude --model sonnet --effort high start
chitra-lane-session --lane editorial --host tophand --backend claude --model opus --effort max start
chitra-lane-session --lane repair --host tophand --backend codex --model gpt-5.6-sol --effort xhigh start
```

The launcher stores a versioned launch receipt under the lane state root. It
binds the durable lane ID, session reference, backend/model/effort, goal
version, enrollment time, all five strategic goal fields, and their SHA-256.
The v2 receipt also records the repository root, Git common directory, real
worktree path, branch and upstream, base and current commit IDs, dirty and
untracked file digest, native agent session ID, and canonical setup-knowledge
digest. Unreadable launch, worktree, or tmux state is `UNKNOWN` and cannot
count as complete.

Once launched, existing Chitra machinery supplies lifecycle parity:
`dispatchd` steers the pane and records HMAC delivery evidence; `watchd` and
`lane_activity` observe it; `draft_scanner` protects typed input;
`completion_gate` and `goal_enforcement` review completion; and
`rate_limit_guard` checkpoints, stops, verifies quiescence, and resumes the
same frozen goal. Steering is performed by enqueueing a normal dispatch order
for the lane session reference. The coordinator reads the resulting transcript
and result ledger, waits, and sends its own follow-up; a human is never the
message bus. Monitord can continue through successive actions, and the
foreground Chitra agent investigates unresolved routine questions instead of
escalating ordinary uncertainty to the user.

## Lane lifecycle

Every lane has exactly one of four lifecycle states:

- `active`: the goal is being pursued and native goal controls plus recurring
  enforcement hooks may be armed.
- `paused`: the session is stopped at a verified checkpoint and can resume.
- `shelved`: the session is offline for an extended period, while the goal,
  checkpoint, worktree identity, transcript pointers, and open questions stay
  visible.
- `closed`: independent completion evidence has verified every frozen done
  item and no unfinished work remains.

There is no `archive` state or compatibility alias. Unfinished work is shelved.
Completed work is closed only after completion proof. Chitra does not delete a
worktree when pausing, shelving, or closing it.

The recovery API records a worktree checkpoint before launch, pause, shelving,
resume, and completion. Resume validates the worktree identity, checkpoint,
goal digest, and setup digest. Drift causes a fresh investigation and context
repair before new work is issued. Dirty or untracked work is preserved. The
launcher writes the v2 identity receipt, and lifecycle gates wire pause,
resume, shelving, and verified close to checkpoint validation.

New sessions receive the configured canonical setup knowledge with the goal.
This knowledge may describe system facts, architectural principles, code
patterns, decision logic, approved references, and exceptions. It informs
reasoning but does not alter the frozen per-goal `AutonomyPolicy`.

Supply one bundle for every lane or a default bundle at the manifest root:

```yaml
knowledge_bundle:
  system_facts:
    - "The authoritative documents live in DocsHome."
  architecture_principles:
    - "Dispatchd is the only terminal writer."
  code_patterns:
    - "Use the repository's existing atomic JSON writer."
  decision_rules:
    - "Use credentials and irreversible actions only with a matching active grant."
  canonical_references:
    - "docs/DESIGN.md"
```

Each field is a list of non-empty strings. The launcher renders the bundle in
`session-setup.md` and records its SHA-256 in the launch receipt.

The setup note also tells each governed Codex or Claude lane to maintain one
AgentTrail-compatible `PLAN.md` in its declared worktree. A reviewed seed is
continued when present; otherwise the lane creates the plan from its frozen
goal, completion conditions, and current unfinished work. The lane updates the
file only when its plan, task state, or evidence changes. `PLAN.md` is an
advisory progress view: it cannot change the frozen goal or substitute for an
independently verified completion receipt.

The goal enrollment also freezes an `AutonomyPolicy`. Its default initiative
is `aggressive`, with goal-scoped grants for replanning, small redesign,
dependency, schema, hook, credential, authentication, security, and
irreversible capabilities, including spending. The default idle threshold is
one clean monitor pass, and Claude's recurring hook runs every five minutes. A
goal may freeze narrower target, amount, unit, expiry, idle-threshold, or
loop-interval limits. Incomplete evidence stays with foreground Chitra. The
user is needed only for a verified missing, expired, or over-limit grant, or
for a change to the frozen outcome.

The setup note gives Codex and Claude their native `/goal` controls. Claude
also receives a recurring `/loop` enforcement hook when active. The native
control commands enter Dispatchd, which serializes them with other lane
writes. Lifecycle gates reconcile, prune, and disarm loops on goal or context
changes and on pause, shelving, or closure. The Codex profile carries the
setup instructions while preserving its existing authentication.

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
| Read-only reviewers share a workspace; every writer gets a worktree | Lane brief/scope, immutable lane identity, and v2 worktree receipt/checkpoint | The receipt records identity and drift evidence. Filesystem permissions and review role assignment remain external policy. |
| Executor/reviewer pair with role swap | Two lane identities and normal steering orders | Supported as a coordination pattern, not enforced. Role assignment/swap has no typed state yet. |
| Settle factual disagreements with a test | `done_when`, completion evidence, `completion_gate` | Enforced when the named test is a required completion item; choosing the discriminating test remains coordinator responsibility. |
| Integrate through one checkout and rerun real checks | Completion evidence and goal enforcement | Partly covered. Chitra can require evidence, but does not own the integration checkout or execute repository CI itself. |
| No push or production touch without approval | Frozen `AutonomyPolicy` grant with target and limits | A matching active grant can authorize the action. Missing, expired, or over-limit authority reaches the user. |

The remaining gaps are fail-closed coordination gaps, not permission to infer
success. Missing reviewer/worktree/integration/approval evidence is UNKNOWN.

# Persistent supervision

Chitra keeps a managed agent session moving toward its enrolled goal. It
observes the bound transcript, records evidence, asks the foreground Chitra
agent to investigate when deterministic rules cannot settle a question, and
continues to steer the session until independent completion evidence verifies
the goal.

This is a pursuit loop, not a one-shot nudge. One monitor pass may select more
than one successive action when the prior action produces new evidence. A
delivery attempt is evidence about transport. It is not a completion limit,
and a fixed number of failed attempts does not turn unfinished work into a
terminal result. A process timeout returns control to the loop so Chitra can
inspect the new state, change tactics, and continue.

## Four lane states

Chitra exposes exactly four lifecycle states for a managed lane:

- **`active`** — the lane is pursuing its goal. Goal controls and recurring
  enforcement hooks may be armed.
- **`paused`** — work is stopped for a recoverable pause. The session, goal,
  transcript, and checkpoint remain available. Enforcement is disarmed until
  resume.
- **`shelved`** — the lane is offline for an extended period. Its session may
  be absent, but its goal, questions, checkpoint, worktree identity, and
  unfinished work remain visible for later resume.
- **`closed`** — Chitra has independently verified completion and has no
  unfinished work to pursue.

There is no `archive` state or compatibility alias. Use `shelved` for unfinished
work that must go offline. Use `closed` only after the normal completion proof
succeeds. Shelving and closing do not delete a worktree. Garbage collection is
a separate operator action.

## Questions and obstacles

The deterministic question handler answers only questions settled by the
frozen goal and canonical setup knowledge. An unresolved question is a
foreground investigation task. Chitra may inspect evidence, ask the session
for context, replan, and direct several successive goal-scoped actions. It
does not send an ordinary unknown question to the user merely because the
deterministic classifier could not answer it.

Each goal freezes an `AutonomyPolicy`. Its default initiative is `aggressive`,
and its default goal-scoped grants cover replanning, small redesign,
dependency, schema, hook, credential, authentication, security, and
irreversible capabilities, including spending. The default idle threshold is
one clean monitor pass, and Claude's recurring session hook runs every five
minutes. A goal can freeze a narrower policy with target, amount, unit, expiry,
idle-threshold, or loop-interval limits.

The foreground agent can use an action when its typed capability matches an
active grant and the evidence proves the limits. Incomplete evidence remains a
foreground investigation. The user is needed only for a verified missing,
expired, or over-limit grant, or for a change to the frozen goal outcome.

Monitord, Dispatchd, and the specialized supervisors remain separate. Monitord
observes and pursues. Dispatchd remains the sole terminal writer. Rate-limit
and load-shed supervisors continue to own their checkpoint, quiescence, and
resume transactions.

## Agent-native controls

Every active Codex or Claude lane receives a setup note that names the native
control surface for that agent. The note uses Codex `/goal` controls and
Claude `/goal` controls for the canonical goal. Claude lanes can also use
`/loop` for recurring enforcement and progress nudges. Chitra sends those
native controls through Dispatchd, which serializes them with other lane
writes. Lifecycle gates arm, replace, prune, and remove loop definitions as
the goal or lifecycle changes. The Codex profile carries the setup
instructions without replacing the provider's existing authentication.

The loop is an enforcement hook, not proof of progress. Transcript evidence,
goal-bound actions, and independent validators remain the source of truth.

## Canonical setup knowledge

An installation may provide a user-controlled canonical knowledge bundle for
Chitra. The bundle can state system facts, architectural principles, existing
code patterns, decision logic, approved references, and explicit exceptions.
Chitra records the bundle digest with the lane's goal context. Every newly
started or resumed lane receives a setup note containing the current bundle,
goal, completion conditions, scope, and operating instructions. A changed
bundle is a context change: Chitra sends the new setup note and reconciles the
native goal and recurring hooks before continuing pursuit.

Canonical knowledge informs reasoning. It does not alter the frozen
`AutonomyPolicy`; grants and limits remain explicit, typed, and digest-bound to
the goal.

## Lane identity and recovery

Each launch or resume records a versioned receipt. The receipt binds the lane
to its repository root, Git common directory, real worktree path, branch and
upstream, base and current commit IDs, dirty and untracked file digest, native
agent session ID, goal digest, and setup-knowledge digest.

The recovery API records a worktree checkpoint before launch, pause, shelving,
resume, and completion transitions. Resume validation compares the lane
identity, checkpoint, goal digest, and setup digest. If they drift, Chitra
records the drift, re-reads current evidence, and repairs context before
issuing new work. Chitra never silently discards a dirty worktree or untracked
work. The launcher writes the v2 identity receipt, and lifecycle gates wire
pause, resume, shelving, and verified close to checkpoint validation.

Pause preserves a resumable session. Shelving removes the session from active
enforcement but retains the goal, transcript pointers, checkpoint, and open
questions. Resume re-establishes the session, restores the context delta, and
re-arms the goal and recurring hooks.

## Completion

Chitra closes a lane only when every frozen completion item has a matching,
goal-bound validator receipt and the final checkpoint is consistent with the
lane identity. An agent's claim, a delivered nudge, a clean-looking pane, or a
successful loop tick is not completion proof.

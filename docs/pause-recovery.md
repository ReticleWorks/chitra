# Pause recovery

Rate-limit holds are one form of the four-state lane lifecycle. A lane is
`active`, `paused`, `shelved`, or `closed`; `held` is internal rate-limit
transaction evidence, not a fifth lifecycle state. A lane with unfinished work
is shelved when it must remain offline for an extended period. It is closed
only after independent completion proof.

Every managed pause that reaches the verified `held` phase adds one record to
`$CHITRA_STATE_DIR/pause_recovery.json`. When `chitra-rate-limit-guard` is run
with `--goals-root`, the file lives under that state root instead. The document
is updated under an exclusive lock with an atomic replacement, preserves prior
pause records after their transactions resume, and deduplicates a retried
held transition by its stable pause ID.

Each record contains:

- the session ref and hold reason;
- the transcript path already proved by dispatch and used for quiescence
  verification, without taking a second pane capture;
- a human-readable resume note built from the lane's stored `GoalRecord` goal,
  current-work context, and completion condition;
- the scheduled `resume_at` time and the time the pause was verified.

To reconstruct a pause, find the newest record for the session ref, inspect its
`transcript_path` for the last work before quiescence, and use `resume_note` as
the work contract. The normal and safest resume path is the same transaction
machine: after `resume_at`, ensure the usage sidecar has emitted a fresh `ok`
verdict, keep `dispatchd` running, and invoke the configured
`chitra-rate-limit-guard --usage-dir ... --host ...` sweep (including the same
`--goals-root` and `--queue-dir` overrides used for the pause). Subsequent
sweeps advance `held → resume_requested → resume_sent`, confirm delivery of the
goal-derived re-arm nudge, clear the hold, and requeue deferred work.

An attached tmux client does not change pause eligibility; attachment is only
a delivery-method liveness signal. To exempt specific sessions from pausing —
typically the monitor's own sessions — set the comma-separated
`CHITRA_NEVER_PAUSE_SESSION_PREFIXES` env var to session-ref prefixes, e.g.
`myhost:monitor:,myhost:harness:`. It is empty by default: no session is
exempt unless the deployment says so.

For lifecycle pause, shelving, resume, and close transitions, use the recovery
module's worktree checkpoint and lifecycle records. A checkpoint binds the
repository root, Git worktree, branch, commit IDs, dirty and untracked file
digests, transcript cursor, and resume note. Resume must compare that binding
before issuing more work. Archive requests for unfinished work map to
`shelved`; they do not delete the worktree or forget open questions.

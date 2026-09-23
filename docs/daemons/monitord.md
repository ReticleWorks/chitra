# monitord — persistent goal-bound supervision

`monitord` is the persistent supervision daemon. One process per instance
observes each explicitly bound transcript and keeps its agent on the exact
enrolled goal until completion evidence verifies:

1. **Journal** — incrementally ingests each lane's client transcript into its
   durable canonical journal (`chitra.journal`).
2. **Detectors and ladder** — runs the deterministic failure-mode detectors
   (`drift`, `unnecessary_steps`, `excessive_testing`, `document_dithering`)
   over the journal and feeds every finding through the response ladder
   (`chitra.detect`). The ladder advances only on recurrence after proven
   consumption; elapsed time never advances it.
3. **Durable action** — records corrective intent before queue publication,
   reconciles orders and signed delivery proof after restart, and advances the
   response ladder only after the exact nudge has a completed agent turn. A
   pass may continue with successive actions when the prior action produces
   new evidence. A transport attempt is evidence, not a terminal failure
   count. A timeout or failed delivery returns control to pursuit so Chitra can
   inspect state, change tactics, and continue.
4. **Completion and questions** — runs enrolled validators on any
   completion claim, structured or plain. Receipts are isolated by exact goal session.
   For a lane the manifest binds to a different OS user, the validators run
   on a bounded worker pool (one run in flight per lane) and the pass
   consumes the recorded result only while the worktree digest it tested
   still matches; a lane sharing Chitra's OS user keeps the synchronous run
   and re-execution, since it could write the record itself. A pending run
   leaves the claim untouched — it is neither a pass, a dispute, nor an
   idle pass. After three queued runs that never yield a fresh record, the
   pass falls back to the synchronous run. The isolated completion reviewer
   runs on its own bounded pool (two `claude -p` rounds at most) behind a
   per-session `review-runs/` record, and its finish wakes the loop instead
   of waiting out the poll interval; a `running` record with no live worker
   counts as lost and is relaunched, while a failed round disputes with the
   review-unavailable finding and is relaunched only after its recorded
   backoff expires. The reviewer is told which lane work is still in
   flight. An "insufficient" verdict is pending too, and the claim is
   reviewed again once nothing is running.
   Routine goal questions and explicit small reversible changes get answers
   derived from the frozen contract. An unresolved routine question becomes a
   foreground Chitra investigation: it may inspect, replan, and direct several
   successive actions. The frozen per-goal `AutonomyPolicy` decides whether a
   typed capability is allowed. Only a verified missing, expired, or
   over-limit grant, or a frozen-outcome change, reaches the user.
5. **Presence** — appends one advisory presence record per pass so peers can
   see which instance observes which lanes (`chitra.presence`). Presence never
   claims, waits, or grants authority outside the frozen per-goal policy.

`dispatchd` remains the only process allowed to write to a terminal. Monitord
publishes goal-versioned, goal-digest-bound orders. Dispatchd recomputes a
contract-derived answer and rejects stale, held, completed, or forged orders
before pane I/O. Monitord, Dispatchd, and the specialized supervisors remain
separate roles.

## Shadow mode

Findings are recorded under `monitord-findings.jsonl` in **shadow mode by
default**. The daemon writes journals, validator receipts, incident records,
ladder decisions, supervision state, and presence, but queues no answers or
corrective orders and does not mutate a disputed or completed goal. Turn
shadow mode off only after the bound lanes and recorded decisions are checked.

## Retired predecessors

`watchd`, `triaged`, and `sweepd` are retired and no longer ship. monitord
absorbed the behaviors that were still live:

- `watchd` (semantic status, pane sensing, and completion review) — absorbed
  by monitord's detector, sensing, and enrollment passes.
- `triaged` (events-log tailing and dedup) — superseded by the canonical
  journal.
- `sweepd` (fleet-state digest) — superseded by monitord's per-pass summary.

`chitra-monitord@<instance>` is the only shipped supervisor unit.

## Running

One-shot pass (prints the pass summary as JSON and exits):

```bash
chitra-monitord --state-dir /var/lib/chitra --once
```

Continuous operation: see the shipped unit
[`packaging/systemd/chitra-monitord@.service`](../../packaging/systemd/chitra-monitord@.service),
one instance-template unit per fleet-style isolated instance
(`systemctl enable --now chitra-monitord@<instance>.service`).

Every active lane must appear in a validated
`chitra.transcript-bindings.v1` manifest. A binding names the exact
`session_ref`, lane, transcript path, client, client version, and instance.
Unbound journals remain observable but cannot borrow another goal.

The shipped systemd pair uses one connected state topology. Render each lane's
`state_dir` in `/etc/chitra/lanes.yaml` as
`/var/lib/chitra/lane-<lane-id>`. Enable
`chitra-monitord@<lane-id>.service`; it writes corrective orders to that
lane's `queue/` directory. The shared `chitra-dispatchd.service` reads the
same lane roots from `lanes.yaml` and uses the same
`/etc/chitra/transcript-bindings.json` manifest.

The shipped unit sets `CHITRA_LANES_FILE=/etc/chitra/lanes.yaml`, which turns
on pane sensing for the lanes whose declared `state_dir` the instance owns:
semantic status classification into the local status socket that
`chitra-agent` queries, rate-limit banner alerts, transcript-pipe liveness,
and the `lane_activity.json` facts the rate-limit guard's quiescence check
reads. A missing or unusable manifest logs a warning and leaves supervision
running. `--once` runs do not bind the status socket.

Migration note: this state root moved in `0.19.2` from
`/var/lib/polyphony-chitra-<lane-id>` (systemd `StateDirectory=polyphony-chitra-%i`)
to `/var/lib/chitra/lane-<lane-id>` (`StateDirectory=chitra`). Upgrading a
host that already has lanes running does not move anything: `monitord`
starts against an empty state root at the new path, and the old goals,
journals, and receipts stay in place at the old path until an operator
copies or archives them by hand.

Flags include `--state-dir`, `--transcript-root`,
`--transcript-bindings-path`, `--dispatch-queue-dir`, `--ledger-path`,
`--ledger-key-path`, `--retry-delay-seconds`, `--findings-path`,
`--poll-seconds`, `--lanes-file`, `--socket-path`, `--agent-manifest-dir`,
`--transcript-stale-seconds`, `--no-shadow-mode`, and `--once`. There is no fixed
attempt-count completion or failure cap: the pursuit loop continues until
completion evidence, an authority gate, or an explicit lifecycle transition
ends active work.

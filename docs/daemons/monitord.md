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
   response ladder only after the exact nudge has a completed agent turn. One
   correction is selected per lane per pass through a durable round-robin
   cursor, so a recurring first finding cannot starve later obstacles. Three
   clean passes with no new scoped progress create one deterministic pursuit
   incident, unless a delivery or question is already pending.
4. **Completion and questions** — runs enrolled validators only after a
   structured completion claim. Receipts are isolated by exact goal session.
   Routine goal questions and explicit small reversible changes get answers
   derived from the frozen contract. Their intent, retries, signed delivery,
   and consumption proof survive restart. Protected or ambiguous questions
   hold the active goal.
5. **Presence** — appends one advisory presence record per pass so peers can
   see which instance observes which lanes (`chitra.presence`). Presence never
   claims, waits, or grants authority.

`dispatchd` remains the only process allowed to write to a terminal. Monitord
publishes goal-versioned, goal-digest-bound orders. Dispatchd recomputes a
contract-derived answer and rejects stale, held, completed, or forged orders
before pane I/O.

## Shadow mode

Findings are recorded under `monitord-findings.jsonl` in **shadow mode by
default**. The daemon writes journals, validator receipts, incident records,
ladder decisions, supervision state, and presence, but queues no answers or
corrective orders and does not mutate a disputed or completed goal. Turn
shadow mode off only after the bound lanes and recorded decisions are checked.

## Deprecated predecessors

`watchd`, `triaged`, and `sweepd` remain installed and documented because
existing declarations still reference them, but they are deprecated by this
entrypoint:

- `watchd` (semantic status + completion review) — superseded by monitord's
  detector and enrollment passes.
- `triaged` (events-log tailing and dedup) — superseded by the canonical
  journal.
- `sweepd` (fleet-state digest) — superseded by monitord's per-pass summary.

No new deployment should declare them; no new daemon beyond `monitord` and
`dispatchd` will be added.

## Running

One-shot pass (prints the pass summary as JSON and exits):

```bash
chitra-monitord --state-dir /var/lib/chitra --once
```

Continuous operation: see
[`packaging/systemd/chitra-monitord@.service.example`](../../packaging/systemd/chitra-monitord@.service.example),
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

Flags include `--state-dir`, `--transcript-root`,
`--transcript-bindings-path`, `--dispatch-queue-dir`, `--ledger-path`,
`--ledger-key-path`, `--max-action-attempts`, `--retry-delay-seconds`,
`--findings-path`, `--poll-seconds`, `--no-shadow-mode`, and `--once`.

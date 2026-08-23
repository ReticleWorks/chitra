# monitord — the composed monitor entrypoint

`monitord` is the single observation-plane daemon. One process per instance
composes what previously required a three-daemon chain:

1. **Journal** — incrementally ingests each lane's client transcript into its
   durable canonical journal (`chitra.journal`).
2. **Detectors and ladder** — runs the deterministic failure-mode detectors
   (`drift`, `unnecessary_steps`, `excessive_testing`, `document_dithering`)
   over the journal and feeds every finding through the response ladder
   (`chitra.detect`). The ladder advances only on recurrence after proven
   consumption; elapsed time never advances it.
3. **Enrollment and receipts** — reads each lane's enrolled goal contract,
   executes registered completion validators for enrolled items, verifies the
   stored receipts, and disputes completion when an item lacks a passing
   receipt (`chitra.validation_receipts`).
4. **Presence** — appends one advisory presence record per pass so peers can
   see which instance observes which lanes (`chitra.presence`). Presence never
   claims, waits, or grants authority.

## Shadow mode

Findings are recorded under `monitord-findings.jsonl` in **shadow mode by
default**: the daemon writes incident records, ladder decisions, findings, and
presence but takes no action path. Turn shadow mode off only after an explicit
operator review of the recorded decisions.

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

Flags: `--state-dir`, `--transcript-root`, `--findings-path`,
`--poll-seconds`, `--no-shadow-mode`, `--once`.

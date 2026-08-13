# Decisions log

`chitra-decisions` keeps a durable record of consequential monitor decisions. The log is JSON Lines at `/var/lib/chitra/decisions.jsonl` by default. Each command appends and flushes one complete entry. It never edits or deletes earlier entries.

Use it for ask retirements, doctrine overrides, adjudications, redirects, pauses, resumes, and work-session architecture changes. Every entry requires:

- the date and time;
- the decision in plain English;
- the reason for it;
- a citation that another person can inspect; and
- the operator order or guidance clause that gave the decision authority.

For example:

```bash
chitra-decisions add \
  --kind pause \
  --decision "Pause every work session until the new session architecture is ready." \
  --basis "Headless submissions made safe supervision and recovery too difficult." \
  --citation "FLEET-STATE-PAUSE-20260813.md#fleet-state-at-operator-pause" \
  --authority "The operator ordered this pause on 2026-08-13."
```

Run `chitra-decisions list` to print the records in append order. Chitra validates monitor-authored operator fields for complete sentences, unexplained internal jargon, and bare codenames. It does not gate or rewrite content from spawned work sessions, including their reports, evidence files, transcripts, pull-request text, citations, and verbatim asks.

The repository includes [the first backfilled shift entries](decisions-log/2026-08-13.jsonl). They were transcribed from the fleet pause handover and monitor self-audit named in each citation.

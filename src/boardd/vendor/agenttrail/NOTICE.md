# Vendored: agenttrail

- Upstream: https://github.com/sodiumsun/agenttrail
- Version: v0.2.0
- Commit: 0d5d151
- Vendored: 2026-09-01 (copied unmodified into this repository 2026-09-03 for boardd)
- License: MIT (see `LICENSE` in this directory; copyright Kelly Sun)

`bin/agenttrail.mjs` and `public/index.html` are byte-for-byte copies of the
upstream files at the pinned commit above. Do not hand-edit them here — if a
change is needed, take it upstream and re-vendor at a new pinned commit.

## Why boardd vendors this instead of driving agenttrail's UI directly

agenttrail's `public/index.html` is not a stateless renderer of an external
feed: it is the client half of a single self-contained Node service that
owns its own world model — a repo-rooted `PLAN.md` component tree, a
multi-board registry served from `/world`, graph and minimap layout, and
several bespoke endpoints (`/spawn`, `/setup-board`, `/tree-of`, `/suggest`,
`/escalations`, `/answer`) with no chitra analog. Reproducing that model in
Python from chitra's GoalRecord schema would mean re-implementing most of
agenttrail's server, not translating one shape into another.

The one clean seam is `/hook`: agenttrail already turns a stream of
Claude-Code-shaped hook events (SessionStart, PreToolUse, PostToolUse, Stop,
...) into its live "run" view without needing PLAN.md at all. boardd drives
that seam instead — see `src/boardd/agenttrail_bridge.py` — running this
vendored, unmodified agenttrail as a co-located Node process and posting
synthesized hook events to it on every lane change. Operators reach the
activity-card view through boardd's own page (an iframe onto the co-located
process), so boardd stays the single board even though the rendering engine
underneath it is Node, not Python. See docs/boardd.md for the deploy shape.

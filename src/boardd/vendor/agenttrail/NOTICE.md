# Vendored: agenttrail

- Upstream: https://github.com/sodiumsun/agenttrail
- Version: v0.2.0
- Commit: 0d5d151
- Vendored: 2026-09-01 (re-vendored into this repository 2026-09-03 for boardd, with the local patches below)
- License: MIT (see `LICENSE` in this directory; copyright Kelly Sun)

## Local patches

Neither vendored file is byte-for-byte upstream, and an earlier version of
this notice wrongly said both were. Both carry local patches, each marked
in place with a `ponytail: local patch` comment naming its number. Five came
in with the board as it was built and approved on 2026-09-01; patches 6, 7
and 8 were added when boardd made that board its only surface. They were one
entry until a review pointed out that a single "mobile reflow" line hid two
changes that are not the reflow — one of which changes the desktop panel.
`diff` against the approved board is exactly five hunks: three are patch 6,
one is patch 7, one is patch 8.

`public/index.html`

1. **Grid wrap.** Upstream lays out one column per dependency depth. Session
   cards have no dependencies, so all of them stacked in a single column;
   `boardLayout` now splits a tall column into about √n sub-columns.
2. **Run cards hidden by default.** `overlays.runs` starts false and the
   `hide-runs` rule was narrowed so plan rows survive the toggle.
3. **A blocked card unfolds itself,** so the ask reads without a click.
4. **No re-animation.** `body.settled` suppresses entrance animations after
   the first paint; replaying them on every refresh was the flicker.
5. **The escalation stack and answer panel** — the `.esc-*` rules, the panel
   markup, and `pollEscalations` / `renderEscalations` / `openEscalation` /
   `closeEscalation` / `toggleFind` / `sendAnswer`.
6. **Mobile reflow.** Below 600px (or with `?m=1`) the file tree and the
   canvas step aside, the escalation stack becomes the full-width queue, and
   the same components render as one column of cards with the same header,
   title and task lines — the `.m-*` rules, the `.m-cards` container, and
   `renderMobileCards()`. `.esc-btn{min-height:0}` rides along: upstream's
   narrow-screen `.primary` rule stretched "Send to session" to fill the
   panel. Desktop is unaffected — the rule sits after the `max-width:720px`
   block at equal specificity, and `min-height:0` is the initial value.
7. **PWA shell.** The page links boardd's manifest, carries two Apple
   web-app metas, and registers boardd's shell-only service worker. Five
   lines in `<head>`, no behaviour change to the page itself.
8. **Generic reveal rows.** "Find the session" renders every `find:` key the
   feed sends rather than four fixed ones, and the hard-coded `cmux` row is
   replaced by the feed's own `how` line. **This one changes the desktop
   panel, not only the narrow layout**: the upstream `cmux rpc surface.list`
   hint is wrong for a chitra lane, which has a tmux pane and a monitor, not
   a cmux surface.

`bin/agenttrail.mjs`

1. **`GET /escalations`** returns the `escalations` object from the
   workspace's `roster.json`.
2. **`POST /answer`** (loopback only, 16 KB cap) appends one JSON line to
   `.agenttrail/answers.log`.

Take any further change upstream and re-vendor at a new pinned commit
rather than growing this list.

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
vendored agenttrail, patched as listed above, as a co-located Node process
and posting synthesized hook events to it on every lane change.

Since 0.21.0 this page is not one view inside boardd — it IS boardd's board,
mounted at `/` through an allowlisted proxy, with its `PLAN.md` and
`roster.json` written from chitra goal state by `src/boardd/board_bridge.py`.
See docs/boardd.md for the deploy shape.

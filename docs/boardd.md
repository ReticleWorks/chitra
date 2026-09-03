# boardd — the single Chitra board

Live fleet dashboard over one or more discovered Chitra state directories.
One page: cockpit (Since-you-looked, Needs feedback, lane grid), a
lane-detail drawer, a History tab, and an Activity tab (agenttrail's
activity-card view, proxied through boardd's own origin). boardd never
writes fleet state directly and never spawns sessions; its two write
endpoints (ack, answer) shell out to the existing `chitra-goals` CLI, which
is the only thing that ever touches goals.json. boardd is the one board — the old Artifact-published
board (`board_publish.py` in the fleet `chitra-launcher` package) is
deprecated in favour of this page; see "Deploy" below.

boardd ships as a sibling package (`src/boardd/`) in this repository. Its web
dependencies are isolated in the `boardd` optional dependency group so the
core `chitra-monitor` install stays lean:

```sh
pip install 'chitra-monitor[boardd]'
```

Python 3.12 + FastAPI. Extra dependencies: `fastapi`, `uvicorn`, `watchfiles`
only. No build step, no CDN; one HTML page with vanilla JS.

## Run against the bundled fixture state dir

Real deployment finds its monitors itself (see "Monitor discovery" below).
For local runs and tests, `BOARDD_DEV=1` turns discovery off in favour of an
explicit map:

```sh
pip install -e '.[boardd]'
BOARDD_DEV=1 BOARDD_STATE_ROOTS=monitor=tests/fixtures/boardd_state \
  uvicorn boardd.app:app --port 8480
```

Then open http://127.0.0.1:8480/.

- `GET /` — the dashboard.
- `GET /api/monitors` — every discovered monitor: id, state root, unit
  active state, lane count, needs-feedback count. Re-scanned on every call.
- `GET /api/state?monitor=<id|all>` — full board state as JSON for one
  monitor, or the union of all of them. This is also Ramble's roster read
  path. Omit `monitor` for the default single-instance id (`monitor`) or the
  first discovered monitor.
- `GET /events?monitor=<id|all>` — Server-Sent Events: an initial `state`
  event, a new `state` event whenever a file in that monitor's state dir
  changes, a `monitors` event every 30 s (`BOARDD_MONITORS_TICK_SECONDS`)
  with a fresh `/api/monitors` result, and a `heartbeat` every 15 s.
- `GET /healthz` — state-dir readability check.
- `POST /api/lanes/{lane_id}/ack` — clear a lane's open asks
  (`chitra-goals resolve-ask --all`) with no answer text.
- `POST /api/lanes/{lane_id}/answer` — body `{"text": "..."}`; clears a
  lane's open asks with the given text recorded as the retirement basis
  (`chitra-goals resolve-ask --all --basis <text>`).

Edit `goals.json` in a state dir while the page is open and the board
updates without a reload. Kill the server and the top bar turns honest:
Delayed, then Disconnected, with a Retry button.

## Monitor discovery

boardd used to take one fixed state directory. A host can run several Chitra
monitor instances (for example `monitor` and `boomtown`), so boardd finds
them itself, every time it is asked, instead of trusting a hand-edited map
that goes stale the day a monitor is added or removed:

- **Unit discovery.** `systemctl list-units --all --plain --no-legend` for
  the four chitra unit templates (`polyphony-chitra-watchd@*`, `triaged@*`,
  `dispatchd@*`, `sweepd@*`). The instance name is the monitor id.
- **Root discovery.** Every `/var/lib/polyphony-chitra*` directory that
  actually contains a `goals.json`. The bare root is monitor id `monitor`;
  `/var/lib/polyphony-chitra-boomtown` is id `boomtown`.

The two signals are unioned by id — either one alone is enough to list a
monitor, so a unit with no state yet and a root left behind by a stopped
unit both show up, letting the operator see the mismatch instead of hiding
it.

`BOARDD_STATE_ROOTS` (an `id=path,id=path` map) replaces both signals, but
**only** when `BOARDD_DEV=1` is also set. That combination exists for tests
and local smoke runs, where there is no real systemd and no `/var/lib` to
scan — it is not a production configuration path.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `BOARDD_STATE_DIR` | `/var/lib/polyphony-chitra` | Fallback root used only when discovery finds nothing (and the `BOARDD_DEV=1`/`BOARDD_STATE_ROOTS` default). |
| `BOARDD_DEV` | unset | `1` disables real discovery in favour of `BOARDD_STATE_ROOTS`. Tests and local smoke only. |
| `BOARDD_STATE_ROOTS` | unset | `id=path,id=path` monitor map, honoured only under `BOARDD_DEV=1`. |
| `BOARDD_TRANSLATION_SEED` | bundled `boardd/data/translations-seed.json` | Hash-keyed translation cache seed. |
| `BOARDD_STALE_AFTER_SECONDS` | `900` | Age after which the state file itself is called stale in the UI. |
| `BOARDD_HEARTBEAT_SECONDS` | `15` | SSE heartbeat interval. |
| `BOARDD_MONITORS_TICK_SECONDS` | `30` | How often `/events` re-runs discovery and pushes a `monitors` event. |
| `BOARDD_AGENTTRAIL_URL` | `http://127.0.0.1:5330/` | Where boardd's own supervisor spawns and reaches the vendored agenttrail process (loopback only). The Activity tab is served `/activity/` on boardd's own origin, which proxies here — the raw port is never given to the browser. |
| `BOARDD_AGENTTRAIL_HOOK_URL` | `http://127.0.0.1:5330/hook` | Where boardd posts synthesized hook events. |
| `BOARDD_AGENTTRAIL_CWD` | `/var/lib/polyphony-chitra` | The `cwd` value stamped on synthesized hook events, and the repo root boardd passes to the spawned agenttrail process; must match. |

## Reading the v3 schema

boardd reads `goals.json` (`chitra.goals.v3`) through chitra's own
`GoalRecord` loader (`chitra.goals.load_goals_document`), not a hand-rolled
parser — the same validation the daemons themselves apply. A done-when
condition renders as machine-tracked only when the lane carries
`enrolled_done_when_items` with a matching, passing
`chitra.completion_gate.CompletionEvidence` record; a lane without
structured items falls back to its plain-text `done_when` clauses,
honestly unbound. The bundled fixture (`tests/fixtures/boardd_state/`) is
generated straight from `GoalRecord` instances by
`tools/gen_boardd_v3_fixture.py`, so it can never drift from what the
schema actually accepts — regenerate it after changing the fixture's lane
set, then `python3.12 tools/gen_boardd_seed.py tests/fixtures/boardd_state`
to refresh the translation seed for any new lines.

## Needs-feedback review queue

The cockpit's top zone lists every lane that needs an operator decision:
status `completion-disputed`, `done-pending-verification`,
`turn-finished-unverified`, or `blocked`, or a non-empty `open_asks` list.
Each card shows the ask text (or, for a status-only trigger with no literal
ask, boardd's own plain-words reason) and the lane's goal, sorted
oldest-lane-update first — v3 carries no per-ask timestamp, so the lane's
own last-write time is the closest honest proxy for how long it has been
waiting. Lane cards elsewhere in the grid carry a "Needs feedback" badge
when they are in this set.

## Rendering: agenttrail's activity-card UI, proxied

boardd vendors agenttrail (`sodiumsun/agenttrail` v0.2.0, pinned commit and
license in `src/boardd/vendor/agenttrail/NOTICE.md`) unmodified. boardd
spawns it itself at startup (`node <vendored>/bin/agenttrail.mjs`, bound to
loopback) and supervises it — restart on exit with backoff, stopped on
boardd's own shutdown, stdout/stderr logged. It is never exposed on the
tailnet directly: the Activity tab's iframe points at `/activity/` on
boardd's own origin, which boardd proxies to the loopback process — GET
only, through a small allowlist of the paths agenttrail's UI actually
fetches (`/`, `/world`, `/tree-of`, `/escalations`, `/events`). `/spawn`,
`/setup-board`, `/answer`, and every other mutating route are refused: the
vendored server's `/spawn` handler takes an arbitrary filesystem path from
an unauthenticated POST and launches a detached Node process, so it must
never reach the tailnet even by accident. boardd separately drives
agenttrail's own live feed by posting synthesized Claude-Code-shaped hook
events (`SessionStart`, `PreToolUse`/`PostToolUse` carrying the lane's `now`
text, `Stop`) to its `/hook` endpoint on every lane status change — the same
event shape and posting pattern this repository's own Orchestra board
bridge (`bridge.py`) already uses successfully.

**Why proxy instead of driving agenttrail's UI directly from boardd's own
endpoint**, which was the first plan: agenttrail's `public/index.html` is
not a stateless renderer of an external feed. It is the client half of a
single self-contained Node service that owns its own world model — a
repo-rooted `PLAN.md` component tree, a multi-board registry served from
`/world`, graph and minimap layout, and several bespoke endpoints
(`/spawn`, `/setup-board`, `/tree-of`, `/suggest`, `/escalations`,
`/answer`) with no analog in chitra's `GoalRecord` schema. Reproducing that
model in Python would mean re-implementing most of agenttrail's server, not
translating one shape into another — clearly more code, and a second,
divergent copy of logic upstream already maintains. The one clean seam is
`/hook`: agenttrail already turns a stream of hook events into its live
"run" view with no `PLAN.md` at all, and this repository already has a
proven implementation of posting to it. Driving that seam is the smaller
diff, so that is what boardd does.

## Mobile

Viewport meta and a single-column layout under 720px (covers the 600px
target) were already in place. This build adds `manifest.webmanifest` and a
service worker (`static/sw.js`) that caches the static shell only — the
page, its CSS and JS, and the manifest — never `/api/state` or `/events`.
Board data is always live or explicitly marked stale; it is never served
from a cache pretending to be current.

### Mobile UI

Under 600px, boardd shows a separate mobile shell (a sibling to the wider
layout above, which stays unchanged) built from the approved design
artboards: three views behind a bottom tab bar.

- **Lanes.** Header (monitor name, last-updated time), status filter chips
  (All / Working / Blocked / Done, each with a live count), a "N lanes need
  you" banner that links to Review when the queue is non-empty, and one
  card per lane — monospaced lane id, a status badge, the goal, and a
  one-line now/ask summary reusing the same server-computed, honesty-marked
  text the wide layout already shows. Done lanes render at 0.72 opacity.
- **Review.** One card per needs-feedback item, oldest first, with the
  action pair its lane actually supports: an open ask gets a text box plus
  Send answer and Acknowledge; `completion-disputed` and
  `done-pending-verification` get Send back and Accept done; everything
  else (`turn-finished-unverified`, or `blocked` with no literal ask) gets
  Nudge and Open lane (switches to Lanes and scrolls to the card). A
  successful action removes the card immediately; a failed one shows a
  toast and leaves the card in place. The Review tab carries an
  unread-count badge.
- **Activity.** The same agenttrail iframe as the wide layout's Activity
  tab, loaded lazily when the tab is first opened.
- **Monitor picker.** A bottom sheet (tap the monitor pill in the header)
  listing every monitor from `/api/monitors` — a state dot colored by unit
  state, lane count, and needs-you count. A monitor with no state root on
  disk yet shows a disabled row. Selection is multi-select, persisted to
  `localStorage`, and drives the same `?monitor=` query the wide layout
  uses: selecting everything (or nothing) maps to `all`, selecting exactly
  one maps to that id. A genuine partial subset (two of three or more) has
  no server-side union endpoint yet, so it degrades to `all` until boardd
  grows a comma-separated filter.

### Dark mode (mobile)

The mobile shell uses agenttrail's own light/dark token set — background,
card, line, dim/soft text, and the accent, success, danger, and purple
status colors — as CSS custom properties, following the same
`prefers-color-scheme` and manual `data-theme` toggle already wired up for
the wide layout, so both layouts and the embedded Activity iframe read as
one system.

### A cascade bug this build fixed

Driving the new shell through a real browser (not just the structural CSS
tests below) surfaced a pre-existing cascade bug: an element toggled by the
`hidden` attribute stays hidden only if nothing else declares `display` at
equal or higher specificity. The desktop drawer, and initially the new
mobile banner/sheet/view sections, all paired `hidden` with an unconditional
class-level `display: flex`, so the attribute silently lost the cascade tie
— the drawer, in particular, is `position: fixed` and full-width under
528px, so it intercepted every click on any narrow viewport regardless of
its `hidden` state. All are now guarded with `:not([hidden])`.

## Dark mode

`prefers-color-scheme: dark` is honoured automatically. A manual toggle in
the top bar (the half-moon button) forces light or dark via
`data-theme` on `<html>`, persisted in `localStorage` (`boardd-theme`). Dark
tokens match the vendored agenttrail UI's own dark palette so the cockpit
and the embedded Activity tab read as one system.

## What is stubbed

**Translation.** The operator ruling is translation-at-render with a small
model pass. This build ships the real interface (`TranslationCache` in
`src/boardd/translate.py`: hash-keyed cache, translate once per changed line,
raw fallback) but does **not** call any model. The single production hook is
`TranslationCache._model_translate`, clearly marked in the source. Lines
missing from the cache render raw with a visible "not yet translated" mark.

**Needs-feedback actions.** "Copy question for Ramble" copies the question
text; there is no deep link into Ramble yet because Ramble has no
addressable surface to link to. Ack and Answer are real writes (see the
endpoints above).

## Honesty rules (enforced in code, tested)

- Agent-reported results always carry "Boardd has not verified this"; there is
  no code path that marks a result verified without an evidence record.
- Liveness is three states with honest copy: Live / Delayed / Disconnected.
  Delayed and Disconnected keep showing the last data, timestamped.
- Stale state files state their age in a banner.
- The banned phrasing "N of M machine-checkable conditions is verified" is
  gone; a test fails if it reappears in the API output.

## Tests

boardd's tests live in the main suite (`tests/test_boardd_*.py`) and run with
the rest of the repository:

```sh
pip install -e '.[test]'
pytest
```

Covers: v3 state loading against the bundled fixture dir (including
structured done-when proof matching), scope-delta detection, the
needs-feedback review-queue selection and sort order, the two write
endpoints, monitor discovery from a temp dir tree, the honesty invariants
above, endpoint + SSE smoke, and a structural no-horizontal-scroll-at-390px
check.

## Deploy

Target: host twinridge, reading each discovered `/var/lib/polyphony-chitra*`
and, through `chitra-goals resolve-ask` only, writing ack/answer
resolutions back into them; reachable only over the tailnet.

1. Release boardd (and the vendored agenttrail Node process) as a pinned
   artifact through a Runline unit on twinridge — no checkouts, no
   hand-copied files on the host.
2. Reach it over Tailscale Serve, tailnet only; no public listener. boardd
   spawns and supervises the vendored agenttrail Node process itself
   (loopback-only, restarted on exit) and proxies its UI at `/activity/*`
   under boardd's own origin — the raw Node port is never exposed on the
   tailnet.
3. Land `packaging/systemd/boardd.service.example` (draft in this repo) via
   the governed host repo, running a dedicated `boardd` user with
   `ReadWritePaths=/var/lib` (the state roots' ids are discovered at
   runtime, so systemd's glob-free path directives cannot name them in
   advance) and `SupplementaryGroups=chitra` so it can write into
   chitra-group-owned state directories. No paired unit is needed for
   agenttrail — boardd owns that process directly.
4. Add a tool-registry entry for boardd in the same PR that lands the unit.
5. Wire the real translation model call behind
   `TranslationCache._model_translate`, with a persisted cache path.
6. After N clean days, retire the old Artifact-published board
   (`board_publish.py` in the fleet `chitra-launcher` package) — that
   retirement is a done-when clause of the boardd lane itself. That path is
   deprecated as of this build; do not add new automation against it.

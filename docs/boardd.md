# boardd — the single Chitra board

The board over one or more discovered Chitra state directories: a pan/zoom
canvas of session cards with a red escalation stack down the right edge,
fed from chitra goal state. It is the board built and approved on
2026-09-01, not a description of one — see "The board" below. boardd never
writes fleet state directly and never spawns sessions; its write endpoints
shell out to the existing `chitra-goals` CLI, which is the only thing that
ever touches goals.json. boardd is the one board — the old Artifact-published
board (`board_publish.py` in the fleet `chitra-launcher` package) is
deprecated in favour of this page; see "Deploy" below.

boardd ships as a sibling package (`src/boardd/`) in this repository. Its web
dependencies are isolated in the `boardd` optional dependency group so the
core `chitra-monitor` install stays lean:

```sh
pip install 'chitra-monitor[boardd]'
```

Python 3.12 + FastAPI. Extra dependencies: `fastapi`, `uvicorn`, `watchfiles`
only. No build step, no CDN. The page itself is the vendored agenttrail
bundle, served by a co-located Node process boardd owns.

## Run against the bundled fixture state dir

Real deployment finds its monitors itself (see "Monitor discovery" below).
For local runs and tests, `BOARDD_DEV=1` turns discovery off in favour of an
explicit map:

```sh
pip install -e '.[boardd]'
BOARDD_DEV=1 BOARDD_STATE_ROOTS=monitor=tests/fixtures/boardd_state \
  uvicorn boardd.app:app --port 8480
```

Then open http://127.0.0.1:8480/. For a run that actually paints, start the
vendored agenttrail process against a workspace directory of your own first
and point boardd at it:

```sh
mkdir -p /tmp/boardd-workspace
node src/boardd/vendor/agenttrail/bin/agenttrail.mjs /tmp/boardd-workspace --port 5331 --no-open &
BOARDD_DEV=1 BOARDD_STATE_ROOTS=monitor=tests/fixtures/boardd_state \
  BOARDD_AGENTTRAIL_CWD=/tmp/boardd-workspace \
  BOARDD_AGENTTRAIL_URL=http://127.0.0.1:5331/ \
  BOARDD_AGENTTRAIL_HOOK_URL=http://127.0.0.1:5331/hook \
  uvicorn boardd.app:app --port 8480
```

Under `BOARDD_DEV=1` boardd does not spawn agenttrail itself, which is why
the command above starts it by hand. The bridge runs either way.

- `GET /` — the board. `?monitor=<id>` filters it to one monitor.
- `GET /api/monitors` — every discovered monitor: id, state root, unit
  active state, lane count, needs-feedback count. Re-scanned on every call.
- `GET /api/state?monitor=<id|all>` — full board state as JSON for one
  monitor, or the union of all of them. This is also Ramble's roster read
  path. Omit `monitor` for the default single-instance id (`monitor`) or the
  first discovered monitor.
- `GET /api/events?monitor=<id|all>` — Server-Sent Events: an initial `state`
  event, a new `state` event whenever a file in that monitor's state dir
  changes, a `monitors` event every 30 s (`BOARDD_MONITORS_TICK_SECONDS`)
  with a fresh `/api/monitors` result, and a `heartbeat` every 15 s.
- `GET /healthz` — state-dir readability check.
- `POST /api/lanes/{lane_id}/ack` — clear a lane's open asks
  (`chitra-goals resolve-ask --all`) with no answer text.
- `POST /api/lanes/{lane_id}/answer` — body `{"text": "..."}`; clears a
  lane's open asks with the given text recorded as the retirement basis
  (`chitra-goals resolve-ask --all --basis <text>`).
- `POST /answer` — what the board's own answer panel calls; body
  `{"key": "<lane>", "answer": "...", "at": "..."}`. See "The answer path".

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

The escalation stack carries every lane that needs an operator decision:
status `completion-disputed`, `done-pending-verification`,
`turn-finished-unverified`, or `blocked`, or a non-empty `open_asks` list.
Each card shows the ask text (or, for a status-only trigger with no literal
ask, boardd's own plain-words reason) and the lane's goal, sorted
oldest-lane-update first — v3 carries no per-ask timestamp, so the lane's
own last-write time is the closest honest proxy for how long it has been
waiting. `/api/state` reports the same set as `needs_you`, so the board and
the JSON API never disagree about who needs attention.

## The board

The board is agenttrail's page, mounted at `/`. There is no second boardd
dashboard: the canvas of session cards and the red escalation stack down the
right edge ARE the board, fed from chitra goal state.

That page was built and approved on 2026-09-01 as the Orchestra board, and
vendored here (`sodiumsun/agenttrail` v0.2.0, pinned commit and license in
`src/boardd/vendor/agenttrail/NOTICE.md`, which lists every local patch).
Until 0.21.0 boardd shipped its own separate cockpit and demoted the
approved board to a third tab behind it. That cockpit is gone.

boardd spawns the vendored process itself at startup
(`node <vendored>/bin/agenttrail.mjs`, bound to loopback) and supervises it —
restart on exit with backoff, stopped on boardd's own shutdown,
stdout/stderr logged. The Node port is never exposed on the tailnet.

### What the board shows

- **Session cards** on a pan/zoom canvas, one per lane: a monospace header
  line (`chitra · <session_ref> · monitor <id>`), the lane name and goal,
  `n of m tasks · <status>`, and the lane's current movement on a `tech:`
  line.
- **Markers**, the same four the approved board used: `[~]` working, `[!]`
  waiting on you, `[x]` done pending close, `[ ]` idle or held.
- **The escalation stack**, red-bordered cards down the right edge ending
  about a third of the way up from the bottom. Every `[!]` lane appears
  once. Clicking one opens the answer panel: Context, Question,
  Recommendation, Your answer, then **Send to session** and **Find the
  session**.
- **A Runs toggle** in the canvas toolbar hides and shows the floating run
  cards. They start hidden.

### How chitra state reaches it

`src/boardd/board_bridge.py` writes the two files agenttrail reads out of
its workspace directory (`BOARDD_AGENTTRAIL_CWD`), then posts the events
that move a card live. It re-renders whenever a state file changes and at
least every 30 s.

| chitra | board |
| --- | --- |
| a `GoalRecord` | one `##` component in `PLAN.md` — one session card |
| `status: working` | `[~]` |
| `blocked`, `turn-finished-unverified`, `completion-disputed`, `done-pending-verification`, or any `open_asks` | `[!]`, and one entry in `roster.json`'s `escalations` |
| `done-pending-close` | `[x]` |
| `idle`, `held` | `[ ]` |
| `now`, else `hold_reason` | the card's `tech:` line |
| an open ask | `tech: NEEDS-INPUT HH:MM — <the ask>` |
| `goal` | the escalation's title |
| `goal` + `now` + `last_verified` | the panel's Context |
| a `foreground_task`, else `hold_reason`, else the proof still owed | the panel's Recommendation |
| host, monitor id, lane id, tmux pane target | the panel's "Find the session" |

An open ask outranks the status: it is a live, unanswered request to the
operator whatever the lane is otherwise doing.

The workspace directory is boardd's own and must never be a chitra state
root. board_bridge writes into it every tick and the SSE watcher watches the
state roots, so rendering into one would feed its own watcher forever.

### The answer path

`POST /answer` — what **Send to session** calls — has two destinations, in
order:

1. agenttrail's own `/answer`, which appends one JSON line to
   `.agenttrail/answers.log`. Parity with the approved board, and the
   operator's own record of what they said.
2. `chitra-goals resolve-ask` for that lane, which is what actually retires
   the ask in chitra state.

The escalation key is the lane id, which `actions._find_record` accepts
directly. A key that resolves nothing returns 409 and a key that matches no
lane returns 404 — never a false success. agenttrail being down does not
fail the write; the answer still reaches chitra state.

### The proxy

Only the paths agenttrail's page actually fetches are proxied, and they
resolve root-absolute because the board is mounted at the root:

- GET `/`, `/world`, `/tree-of`, `/escalations`, `/events`
- POST `/answer`, `/hook`

Everything else 404s before an upstream request is made — `/spawn` and
`/setup-board` included. The vendored server's `/spawn` handler takes an
arbitrary filesystem path from an unauthenticated POST and launches a
detached Node process, so it must never reach the tailnet even by accident.

### Monitors

One `PLAN.md` section per monitor. agenttrail's plan convention has no node
above a component, so a section is a contiguous run of that monitor's lanes
with monitor-prefixed ids — the cards land together on the canvas and the
ids stay unique across monitors. `GET /?monitor=<id>` re-points the bridge
at one monitor; omit it for all of them. There is no new UI chrome for this.

The filter is one process-wide value, not one per viewer: this is a loopback
board with one operator in front of it. Per-viewer filtering would need
agenttrail itself to filter, which it cannot.

## Mobile

agenttrail's canvas does not reflow — at 390 px it stayed a wide pan/zoom
surface and the page scrolled sideways. Patch 6 on the vendored page is a
reflow of the same anatomy, not a second design.

Below 600 px (or with `?m=1` at any width, which is how it is screenshotted
and debugged from a desktop browser):

- the file tree and the canvas step aside;
- the escalation stack becomes the queue — full width, in normal flow, same
  red-bordered card anatomy, opening the same four-section panel;
- the session cards follow it as a single column with the same header,
  title and `n of m tasks` lines.

`manifest.webmanifest`, the two icons, and a shell-only service worker
(`static/sw.js`) ride on the board page itself, so it installs to a home
screen with `start_url` `/`. The worker caches the shell only — never
`/world`, `/escalations` or `/events`. Board data is always live, never
served from a cache pretending to be current.

Three cascade traps are worth knowing before touching this CSS, because all
three were found by screenshotting the running board rather than reading it:

1. The `.esc-*` rules are appended **after** the media queries in the
   stylesheet, so a media query written among them loses on cascade order,
   not specificity. The reflow is the last thing in the file.
2. `.app`'s implicit grid column is auto-sized, so one nowrap escalation
   title widened the page to 983 px inside a 390 px viewport. The reflow
   clamps the column to `minmax(0,1fr)`.
3. The canvas section carries class `primary` too, and upstream gives
   `.primary` `min-height:calc(100dvh - 105px)` below 720 px. That also
   matched the answer panel's `.esc-btn.primary`, stretching "Send to
   session" to fill the panel.

## Dark mode

The board's own dark-first palette, unchanged. agenttrail's light toggle in
the top bar still works and still persists.

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
needs-feedback review-queue selection and sort order, the write endpoints,
monitor discovery from a temp dir tree, the honesty invariants above,
endpoint + SSE smoke, the bridge's goals-to-PLAN/roster rendering, the root
proxy's allow and deny lists, and the 390px reflow.

The tests are structural, not visual. Every layout defect this build fixed
was found by screenshotting the running board with Playwright, and the
tests were written afterwards to hold each fix. Screenshot the board when
you change its CSS.

## Deploy

Target: host twinridge, reading each discovered `/var/lib/polyphony-chitra*`
and, through `chitra-goals resolve-ask` only, writing ack/answer
resolutions back into them; reachable only over the tailnet.

1. Release boardd (and the vendored agenttrail Node process) as a pinned
   artifact through a Runline unit on twinridge — no checkouts, no
   hand-copied files on the host.
2. Reach it over Tailscale Serve, tailnet only; no public listener. boardd
   spawns and supervises the vendored agenttrail Node process itself
   (loopback-only, restarted on exit) and serves its page at `/` under
   boardd's own origin — the raw Node port is never exposed on the tailnet.
   Give the `boardd` user a writable workspace directory
   (`BOARDD_AGENTTRAIL_CWD`, default `/var/lib/boardd/workspace`); it must
   not be a chitra state root.
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

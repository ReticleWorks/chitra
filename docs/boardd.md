# boardd — live fleet dashboard

Live fleet dashboard over the Chitra state directory. One page: cockpit
(Since-you-looked, Needs-you, lane grid), a lane-detail drawer, and a History
tab. Pure reader — boardd never writes fleet state and never spawns sessions.

boardd ships as a sibling package (`src/boardd/`) in this repository. Its web
dependencies are isolated in the `boardd` optional dependency group so the
core `chitra-monitor` install stays lean:

```sh
pip install 'chitra-monitor[boardd]'
```

Python 3.12 + FastAPI. Extra dependencies: `fastapi`, `uvicorn`, `watchfiles`
only. No build step, no CDN; one HTML page with vanilla JS.

## Run against the bundled fixture state dir

```sh
pip install -e '.[boardd]'
BOARDD_STATE_DIR=tests/fixtures/boardd_state \
  uvicorn boardd.app:app --port 8480
```

Then open http://127.0.0.1:8480/.

- `GET /` — the dashboard.
- `GET /api/state` — full board state as JSON. This is also Ramble's roster
  read path.
- `GET /events` — Server-Sent Events: an initial `state` event, a new `state`
  event whenever a file in the state dir changes, and a `heartbeat` every 15 s.
- `GET /healthz` — state-dir readability check.

Edit `goals.json` in the state dir while the page is open and the board
updates without a reload. Kill the server and the top bar turns honest:
Delayed, then Disconnected, with a Retry button.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `BOARDD_STATE_DIR` | `CHITRA_STATE_DIR` or `/var/lib/chitra` | Optional boardd override. The shared Chitra state root holds `goals.json`, `sweep-digest.json`, and `joined-lanes/*.json`. |
| `BOARDD_TRANSLATION_SEED` | bundled `boardd/data/translations-seed.json` | Hash-keyed translation cache seed. |
| `BOARDD_STALE_AFTER_SECONDS` | `900` | Age after which the state file itself is called stale in the UI. |
| `BOARDD_HEARTBEAT_SECONDS` | `15` | SSE heartbeat interval. |

## What is stubbed

**Translation.** The operator ruling is translation-at-render with a small
model pass. This build ships the real interface (`TranslationCache` in
`src/boardd/translate.py`: hash-keyed cache, translate once per changed line,
raw fallback) but does **not** call any model. The single production hook is
`TranslationCache._model_translate`, clearly marked in the source. Lines
missing from the cache render raw with a visible "not yet translated" mark.
The bundled seed covers every line in the fixture state so the demo reads
well; regenerate it with `python3.12 tools/gen_boardd_seed.py <state_dir>`
after editing the fixture state.

**Evidence bindings.** `goals.json` in the fixture carries no evidence
records, so every finish condition honestly renders as "Not checked
automatically — no evidence source is linked." When the daemons publish
evidence (expected shape: `goal.evidence = [{condition, verified, method,
at}]`), conditions bound to passing evidence render as proven with the method
and time; nothing else ever renders as tracked.

**Needs-you actions.** "Copy question for Ramble" copies the question text;
there is no deep link into Ramble yet because Ramble has no addressable
surface to link to.

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

Covers: state loading against the bundled fixture dir, scope-delta detection,
the honesty invariants above, endpoint + SSE smoke, and a structural
no-horizontal-scroll-at-390px check.

## Production deploy (not done in this build)

Target: host twinridge, reading `/var/lib/polyphony-chitra` read-only.

1. Release boardd as a pinned artifact and install at `/opt/boardd` (venv +
   package). No checkouts, no hand-copied files on the host.
2. Land `packaging/systemd/boardd.service.example` (draft in this repo) via
   the governed host repo. It runs a dedicated `boardd` user, binds the
   tailnet address only, and mounts the state dir read-only via
   `ReadOnlyPaths`.
3. Add a tool-registry entry for boardd in the same PR that lands the unit.
4. Wire the real translation model call behind
   `TranslationCache._model_translate`, with a persisted cache path.
5. After N clean days, retire the old artifact board (that retirement is a
   done-when clause of the boardd lane itself).

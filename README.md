# chitra

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)]() [![PyPI](https://img.shields.io/pypi/v/chitra-monitor.svg)](https://pypi.org/project/chitra-monitor/)

chitra is a session monitor for AI coding agents. It watches `tmux`-hosted
Claude and Codex sessions ("lanes"), enrolls each one against a goal through
a short interview, and doggedly pursues that goal: it delivers corrective
nudges, investigates stuck questions, and only calls a lane done once it has
independently checked the evidence.

## Goals

- **Reliable.** Delivery, queueing, and state tracking are deterministic
  code, not model judgment. One process (`dispatchd`) owns writing into a
  session, so two writers can never race and corrupt a lane's next turn.
- **Persistent.** A lane does not get to coast. chitra keeps steering a
  session toward its enrolled goal, escalating corrective nudges as
  problems recur, until it can prove completion — a clean-looking pane or
  an agent's own "done" claim is not enough.
- **Autonomous.** Where a stuck lane needs a decision, a foreground chitra
  agent investigates and acts using live tool access, inside limits frozen
  at enrollment. A human is pulled in only when a permission is missing,
  expired, or a frozen goal itself needs to change.

## Components

**The daemon set.** Four daemons run as templated systemd instances, one
instance per monitor (for example a monitor named `monitor` or one named
`boomtown`), each with its own state root under `/var/lib/polyphony-chitra*`:
`watchd` (semantic pane status), `triaged` (deduplicated state-change
events), `dispatchd` (the sole process allowed to write into a session), and
`sweepd` (a compact fleet-state digest). `watchd`, `triaged`, and `sweepd`
are deprecated in favor of the supervisor below; `dispatchd` stays the sole
terminal writer for both the older set and the newer one.

**The supervisor.** Added in 0.19.2, `monitord` is the persistent-goal-pursuit
engine: it binds a transcript to one frozen goal, runs deterministic
detectors for drift and stalling, and pushes findings through a response
ladder that only escalates after a prior nudge has actually landed — not
after time passes. An operator can put a goal on an explicit hold and resume
it later; chitra never silently drops or times out an unfinished goal. See
[Persistent supervision](docs/persistent-supervision.md).

**Interview-based intake.** A lane is not enrolled by a free-text
description. `chitra-goals set` returns four typed interview questions;
answering them, with evidence, freezes a structured set of done items, each
naming its own validator. This is the `chitra.goals.v3` schema, and it is
what lets chitra check completion against real receipts instead of an
agent's say-so. See [Design notes](docs/DESIGN.md).

## boardd — the fleet board

`boardd` (0.20.0) is the one Chitra board; an older board published as a
claude.ai Artifact is deprecated. It auto-discovers every monitor instance
on a host — by reading the four daemons' systemd units and by globbing
`/var/lib/polyphony-chitra*` for a `goals.json` — so there is no map to keep
in sync as monitors come and go.

The cockpit's **needs-feedback review queue** lists every lane an operator
should look at: any open question, plus lanes in a disputed-completion,
done-pending-verification, unverified-turn, or blocked state, sorted
oldest first. From there an operator can **ack** or **answer** a lane; both
actions write back through the existing `chitra-goals` command-line tool, so
boardd never becomes a second writer of goal state.

The **Activity** tab renders live session activity using `agenttrail`, a
vendored open-source UI component, run as its own supervised process and
reached only through a same-origin proxy inside boardd — the underlying
process is never exposed directly. boardd also ships as an installable
mobile web app (add to home screen, works offline for the shell only) with
light and dark themes.

**Deploy:** boardd runs as a systemd service bound to `127.0.0.1:8480` and is
reached over the tailnet through Tailscale Serve — never a public listener.

**Run it locally:**

```bash
pip install -e '.[boardd]'
BOARDD_DEV=1 BOARDD_STATE_ROOTS=monitor=tests/fixtures/boardd_state \
  uvicorn boardd.app:app --port 8480
```

`BOARDD_DEV=1` swaps real discovery for the fixture state directory checked
into this repo, so it works without a live monitor. See
[docs/boardd.md](docs/boardd.md) for the full endpoint list and configuration.

## Quick start

```bash
git clone https://github.com/ReticleWorks/chitra.git
cd chitra
pip install -e '.[test]'
pytest
```

On macOS, some tests open a Unix domain socket, and macOS caps that socket's
path length well below Linux's. pytest's default temp directory is often too
deep, so run tests with a short base path instead:

```bash
pytest --basetemp=/tmp/ct -q
```

To try boardd against the fixture state, see the boardd section above. For
running the daemons themselves against a real lane, start at
[docs/daemons/README.md](docs/daemons/README.md).

## Release

The version lives in one place, `pyproject.toml`. Cutting a release:

1. Bump the version in `pyproject.toml` and add a `CHANGELOG.md` entry.
2. Tag the commit `vX.Y.Z` and publish a GitHub Release from that tag —
   publishing the Release is the human gate; nothing builds or ships before
   it.
3. The `publish.yml` workflow then builds and uploads to PyPI. That workflow
   is currently blocked by an organization-wide setting that disables GitHub
   Actions, so until that is lifted, publish by hand from the tag instead:
   `python -m build && twine upload dist/*`.
4. A release notifies the fleet repository, which opens its own pull request
   to bump the pinned `chitra-monitor` version for deployed hosts.

## Documentation

Start at [docs/README.md](docs/README.md), or jump straight to
[Getting started](docs/quickstart/README.md), [Concepts](docs/concepts/README.md),
or [Persistent supervision](docs/persistent-supervision.md).

## Getting help

Questions and bug reports: [open an issue](https://github.com/ReticleWorks/chitra/issues).
See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a nontrivial PR;
security reports go through [SECURITY.md](SECURITY.md).

## License

MIT © 2026 Reticle Works. See [LICENSE](LICENSE) for the full text.

<!-- zuul hygiene gate probe, safe to ignore -->

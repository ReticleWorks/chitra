# Migration from Watchd screen-change inference

This release intentionally changes how `watchd` derives and emits pane status.
Operators must deploy the source version, local manifest provisioning, and any
agent lifecycle hooks in the same change.

## What changed

Before this release, `watchd` normalized a tmux capture, hashed it, emitted
`CHANGE DETECTED` when the hash changed, searched a fixed active-turn regular
expression, and emitted one delayed `IDLE` event after an unchanged input row.
Those signals mixed screen activity with semantic status.

After this release:

- Lifecycle integrations are authoritative for their exact pane and session.
- Otherwise, a local or bundled TOML manifest classifies the bottom-buffer
  snapshot as idle, working, or blocked.
- Unknown or unmatched screen shapes are idle. Only recognized visible
  approval, question, or permission rules produce screen-derived blocked.
- The events log emits `AGENT_STATUS` only when semantic status or authority
  changes. Composer typing and unrelated output changes do not emit status.
- Completion review begins on a semantic working-to-idle transition at a
  visible input row. A quiet screen alone is not a turn boundary.
- `done` comes only from Chitra's completion gate.

The old `--idle-threshold-seconds` option and
`CHITRA_WATCHD_IDLE_THRESHOLD_SECONDS` are accepted temporarily so an existing
unit does not fail to start, but they no longer author status. Remove them from
deployment configuration.

## Events-log impact

The old records looked like:

```text
2026-08-13T12:00:00Z lane CHANGE DETECTED: normalized pane text
2026-08-13T12:05:00Z lane IDLE target=lane:0.0 idle_seconds=300 threshold_seconds=300
```

The new record is metadata-only and does not copy pane text:

```text
2026-08-14T12:00:00Z lane AGENT_STATUS state=blocked needs operator input pane_id=%17 target=lane:0.0 agent=codex authority=manifest source=package:codex.toml rule=permission_prompt fallback=none
```

`triaged` and `sweepd` continue to consume the same timestamp, lane ID, and
opaque-text framing. A downstream consumer that depends on `CHANGE DETECTED`,
the old delayed `IDLE` payload, content hashes, or copied screen text must move
to `AGENT_STATUS` or the socket subscription API.

## Required rollout sequence

1. Prepare local manifests under the configured Chitra manifest directory.
   Validate each captured screen with `chitra-agent explain --file`.
2. Prepare lifecycle hooks to use the injected `CHITRA_PANE_ID`,
   `CHITRA_SESSION_REF`, and `CHITRA_SOCKET_PATH`. Do not guess a pane ID.
3. Ship the new `chitra-monitor` version, manifest provisioning, and any fleet
   adapter changes in one package pin and converge.
4. Start the replacement with `watchd --handoff-from /run/chitra/chitra.sock`
   when an old compatible server is running. If the source cannot prove every
   pane, stop and investigate the reported UNKNOWN state.
5. Verify `chitra-agent explain` for one working, idle, and visible permission
   screen. Verify a blocked subscription and one completion-backed done wait.
6. Remove the old idle-threshold setting and update consumers that parse the
   old event text.

Do not deploy the source version first and add manifests later. A known agent
with no matching rule safely appears idle, but that would hide working state
until provisioning catches up.

## Local override path

The default override directory is:

```text
${XDG_CONFIG_HOME:-~/.config}/chitra/agent-detection/
```

`CHITRA_AGENT_MANIFEST_DIR` or `watchd --agent-manifest-dir` can select a
deployment-owned directory. A local `<agent>.toml` replaces the bundled
manifest for that agent. An invalid local override fails closed to idle and is
shown in explain output; Chitra does not silently use the bundled rules behind
an operator override.

## Rollback

Before a handoff commit, the old server remains authoritative and the socket
path is restored on failure. After a successful commit, roll back by starting
the prior compatible version as another verified handoff. If protocol or
manifest compatibility cannot be proved, use the normal controlled restart
path and report session status as UNKNOWN until Watchd observes it again.

Tmux owns the lane processes throughout. Do not kill or recreate a tmux
session merely to roll back Chitra status detection.

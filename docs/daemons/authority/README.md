# Authority and Access Control

Observe-only services for ownership verification and pressure observation. These read canonical state but never modify anything.

## Services

- **[petra](petra.md)** — Dark-launch observe-only authority. Validates advisory "pressure observation" events from Watchtower and records them to SQLite. Never acts on observations; pure recording.
- **[ownership-provider](ownership-provider.md)** — Read-only ownership fence. Answers whether a host:lane:instance belongs to a canonical managed lane by reading goals.json and a digest marker. Never discovers sessions or modifies state.

## See Also

- **[Concepts: Ownership](../../concepts/)** — How ownership verification works
- **[Deep Dive: Petra Authority](../../petra-authority.md)** — Authority model and integration

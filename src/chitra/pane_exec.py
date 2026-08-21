"""Bind tmux's runtime pane id into Chitra's supervised agent environment."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

PANE_ID_ENV_VAR = "CHITRA_PANE_ID"
TMUX_PANE_ENV_VAR = "TMUX_PANE"


def supervised_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a copy with a validated server-unique tmux pane identifier."""
    pane_id = environment.get(TMUX_PANE_ENV_VAR, "")
    if not pane_id.startswith("%") or not pane_id[1:].isdigit():
        raise ValueError("TMUX_PANE is missing or invalid; refusing unsupervised agent launch")
    result = dict(environment)
    result[PANE_ID_ENV_VAR] = pane_id
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        raise SystemExit("agent command is required after --")
    try:
        environment = supervised_environment(os.environ)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    os.execvpe(arguments[0], arguments, environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())

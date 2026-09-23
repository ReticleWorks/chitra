"""The Claude Code tmux lane plug."""

from __future__ import annotations

from typing import ClassVar

from chitra.adapter.tmux import TmuxLanePlug


class ClaudeTmuxPlug(TmuxLanePlug):
    name: ClassVar[str] = "claude-tmux"
    backend: ClassVar[str] = "claude"
    client: ClassVar[str] = "claude"

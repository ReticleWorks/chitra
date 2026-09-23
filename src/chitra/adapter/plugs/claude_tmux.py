"""The Claude Code tmux lane plug."""

from __future__ import annotations

from typing import ClassVar

from chitra.adapter.tmux import TmuxLanePlug
from chitra.journal.models import Client


class ClaudeTmuxPlug(TmuxLanePlug):
    name: ClassVar[str] = "claude-tmux"
    backend: ClassVar[str] = "claude"
    client: ClassVar[str] = Client.CLAUDE

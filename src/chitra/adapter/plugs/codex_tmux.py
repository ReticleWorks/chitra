"""The Codex tmux lane plug."""

from __future__ import annotations

from typing import ClassVar

from chitra.adapter.tmux import TmuxLanePlug
from chitra.journal.models import Client


class CodexTmuxPlug(TmuxLanePlug):
    name: ClassVar[str] = "codex-tmux"
    backend: ClassVar[str] = "codex"
    client: ClassVar[str] = Client.CODEX

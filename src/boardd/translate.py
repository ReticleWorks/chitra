"""Translation-at-render, v1: a deterministic stub behind the real interface.

The operator ruling (boardd-requirements.md, ruling 2): raw session lines are
agent-speak and stay canonical in the state files; boardd translates them into
plain technical English at render time with a small model pass, cached by
source-text hash, with a visible raw affordance and an honest fallback.

THIS BUILD STUBS THE MODEL CALL. The cache and the fallback are real; the
model pass is not wired. `_model_translate` below is the single hook where the
production model call goes. Until then:

- a line whose hash is in the seeded cache renders as its cached translation;
- any other line renders raw, marked untranslated ("not yet translated").

The translation contract (binding, applies to the future model pass too):
never change scope, certainty, or claims; named actor, concrete verb, no
jargon, no acronym left unexpanded.
"""

import hashlib
import json
from pathlib import Path
from typing import Any


class TranslationCache:
    """Hash-keyed cache: translate once per changed line, not per view."""

    def __init__(self, seed_path: Path | None = None):
        self._cache: dict[str, dict[str, str]] = {}
        if seed_path is not None:
            p = Path(seed_path)
            if p.exists():
                self._cache = json.loads(p.read_text())

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    def _model_translate(self, text: str) -> str | None:
        """PRODUCTION HOOK — the small-model translation call goes here.

        v1 deliberately does NOT call any model. Returning None means "no
        translation available", and the caller falls back to the raw line,
        honestly marked. When the model pass is wired: call the model with the
        style contract above, store the result in self._cache under
        self.key(text), and persist the cache.
        """
        return None

    def get(self, text: str) -> dict[str, Any]:
        """Return {text, raw, translated} for one source line.

        `translated` False means: `text` is the raw line, shown exactly as
        received, and the UI must mark it "not yet translated".
        """
        raw = (text or "").strip()
        if not raw:
            return {"text": "", "raw": "", "translated": False}
        entry = self._cache.get(self.key(raw))
        if entry is not None:
            return {"text": entry["text"], "raw": raw, "translated": True}
        modeled = self._model_translate(raw)
        if modeled is not None:
            self._cache[self.key(raw)] = {"raw": raw, "text": modeled}
            return {"text": modeled, "raw": raw, "translated": True}
        return {"text": raw, "raw": raw, "translated": False}

    def __len__(self) -> int:
        return len(self._cache)

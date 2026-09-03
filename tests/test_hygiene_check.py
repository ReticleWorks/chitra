"""Test for scripts/hygiene_check.py: flags a denied term, spares an allow-listed one."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "hygiene_check.py"
DENYLIST = REPO_ROOT / ".hygiene-denylist"


def test_hygiene_check_flags_email_and_spares_allowlisted_unit_prefix(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("contact someone@example.com about polyphony-chitra rollout\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--denylist", str(DENYLIST), str(target)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout.count("matches /") == 1
    assert f"{target}:1: matches /" in result.stdout
    # The matched value itself is never echoed.
    assert "someone@example.com" not in result.stdout

"""Test for scripts/hygiene_check.py: block fails, warn doesn't, allow-list spares a hit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "hygiene_check.py"
DENYLIST = REPO_ROOT / ".hygiene-denylist"


def _run(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--denylist", str(DENYLIST), str(target)],
        capture_output=True,
        text=True,
    )


def test_hygiene_check_blocks_a_personal_name(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("ping Trey about the rollout\n", encoding="utf-8")

    result = _run(target)

    assert result.returncode == 1
    assert result.stdout.count("matches /") == 1
    assert f"{target}:1: matches /" in result.stdout
    assert "hygiene: 1 block, 0 warn" in result.stdout


def test_hygiene_check_warns_but_does_not_fail_on_a_hostname(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("deploy target is twinridge\n", encoding="utf-8")

    result = _run(target)

    assert result.returncode == 0
    assert result.stdout.count("warns /") == 1
    assert f"{target}:1: warns /" in result.stdout
    assert "hygiene: 0 block, 1 warn" in result.stdout


def test_hygiene_check_spares_allowlisted_email_and_unit_prefix(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("contact someone@example.com about polyphony-chitra rollout\n", encoding="utf-8")

    result = _run(target)

    assert result.returncode == 0
    assert "matches /" not in result.stdout
    assert "warns /" not in result.stdout
    assert "hygiene: 0 block, 0 warn" in result.stdout
    assert "someone@example.com" not in result.stdout

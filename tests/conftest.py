"""Shared pytest fixtures for the chitra test suite."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _default_validator_registry(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test at a registry containing the trusted validator names.

    The file lives outside the per-test ``tmp_path`` because suites such as
    the usage-export tests treat that whole directory tree as data (snapshot
    globs, fleet host directories). Individual tests override the environment
    variable when they need their own registered-validator entries.
    """
    registry = tmp_path_factory.mktemp("validator-registry") / "validators.json"
    registry.write_text(
        json.dumps(
            {
                "pytest": {"argv": [sys.executable, "-c", "import sys; sys.exit(0)"]},
                "ruff": {"argv": [sys.executable, "-m", "ruff", "check"]},
                "mypy": {"argv": [sys.executable, "-m", "mypy"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHITRA_VALIDATORS_FILE", str(registry))
    return registry

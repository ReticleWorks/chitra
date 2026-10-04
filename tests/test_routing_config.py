"""Tests for chitra.routing_config: purely mechanical task_type -> routing_hint
lookup. No LLM calls, no content judgment -- a config-driven table only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from chitra.routing_config import load_routing_config


def test_load_routing_config_raises_on_missing_file_when_configured(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(OSError):
        load_routing_config(missing)


def test_load_routing_config_raises_on_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text("defaults: [this is not a mapping: :", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_routing_config(path)

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from chitra.validator_registry import (
    VALIDATORS_ENV_VAR,
    ValidatorRegistryError,
    load_validators,
    run_registered_validator,
    validators_path,
)


def _write_registry(tmp_path: Path, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "registry-root" / "validators.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(path))
    return path


def test_missing_registry_file_is_an_empty_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(VALIDATORS_ENV_VAR, raising=False)
    assert load_validators(tmp_path / "absent-root") == {}


def test_load_validators_maps_names_to_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_registry(
        tmp_path,
        {"pytest": {"argv": [sys.executable, "--version"], "timeout_s": 30.0, "runs_as": "ci"}},
        monkeypatch,
    )
    loaded = load_validators()
    assert set(loaded) == {"pytest"}
    entry = loaded["pytest"]
    assert entry.argv == (sys.executable, "--version")
    assert entry.timeout_s == 30.0
    assert entry.runs_as == "ci"


def test_validators_path_prefers_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "elsewhere" / "validators.json"
    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(override))
    assert validators_path(tmp_path) == override
    monkeypatch.delenv(VALIDATORS_ENV_VAR)
    assert validators_path(tmp_path) == tmp_path / "validators.json"
    assert validators_path() != tmp_path / "validators.json"


def test_malformed_json_is_a_registry_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "registry-root" / "validators.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(VALIDATORS_ENV_VAR, str(path))
    with pytest.raises(ValidatorRegistryError, match="not valid JSON"):
        load_validators()


def test_non_object_payload_is_a_registry_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_registry(tmp_path, ["pytest"], monkeypatch)
    with pytest.raises(ValidatorRegistryError, match="must map names"):
        load_validators()


@pytest.mark.parametrize("spec", [{"argv": []}, {"argv": ["x"], "timeout_s": 0}, {"argv": ["x"], "runs_as": 3}, {"command": ["x"]}])
def test_invalid_entry_is_a_registry_error_naming_the_entry(
    tmp_path: Path, spec: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registry(tmp_path, {"broken": spec}, monkeypatch)
    with pytest.raises(ValidatorRegistryError, match="entry 'broken' is invalid"):
        load_validators()


def test_run_returns_exit_code_and_combined_output(tmp_path: Path) -> None:
    from chitra.validator_registry import RegisteredValidator

    entry = RegisteredValidator(argv=[sys.executable, "-c", "print('out'); raise SystemExit(3)"])
    code, output = run_registered_validator(entry)
    assert code == 3
    assert output == "out\n"


def test_unrunnable_argv_fails_closed_with_125(tmp_path: Path) -> None:
    from chitra.validator_registry import RegisteredValidator

    entry = RegisteredValidator(argv=["definitely-not-a-real-program-xyz"])
    code, output = run_registered_validator(entry)
    assert code == 125
    assert "could not execute" in output


def test_timeout_fails_closed_with_125(tmp_path: Path) -> None:
    from chitra.validator_registry import RegisteredValidator

    entry = RegisteredValidator(argv=[sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.2)
    code, output = run_registered_validator(entry)
    assert code == 125
    assert "timed out" in output

"""Registered validators: the operator-declared commands Chitra itself executes.

A done item's ``validator`` must name an entry in the instance's
``validators.json`` registry. The lane never runs its own proof and never
supplies the result: watchd executes the registered argv at a completion
claim, writes the hash-bound receipt, and the gate reads that disk result.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from chitra.state_paths import state_dir

VALIDATORS_ENV_VAR = "CHITRA_VALIDATORS_FILE"
DEFAULT_VALIDATOR_TIMEOUT_S = 120.0
_UNRUNNABLE_EXIT_CODE = 125


class ValidatorRegistryError(ValueError):
    """Raised when a validators.json file cannot be trusted as a registry."""


class RegisteredValidator(BaseModel):
    """One registered validator command and its execution bound."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1)
    timeout_s: float = Field(default=DEFAULT_VALIDATOR_TIMEOUT_S, gt=0)
    # Reserved for the launcher's declared execution identity. Chitra records
    # it verbatim; it does not drop privileges itself.
    runs_as: str = ""


def validators_path(root: Path | None = None) -> Path:
    """Return the configured registry path for ``root``."""
    configured = os.environ.get(VALIDATORS_ENV_VAR, "").strip()
    if configured:
        return Path(configured)
    return (state_dir() if root is None else root) / "validators.json"


def load_validators(root: Path | None = None) -> dict[str, RegisteredValidator]:
    """Load the registry, treating a missing file as an empty registry."""
    path = validators_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValidatorRegistryError(f"validators.json is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidatorRegistryError(f"validators.json must map names to validator objects: {path}")
    registry: dict[str, RegisteredValidator] = {}
    for name, spec in payload.items():
        try:
            registry[name] = RegisteredValidator.model_validate(spec)
        except ValueError as exc:
            raise ValidatorRegistryError(f"validators.json entry {name!r} is invalid: {exc}") from exc
    return registry


def run_registered_validator(entry: RegisteredValidator) -> tuple[int, str]:
    """Execute one registered argv and return ``(exit_code, output)``.

    An unrunnable or timed-out validator returns the fail-closed 125 exit
    code with the reason as output; it can never produce a passing receipt.
    """
    try:
        completed = subprocess.run(
            list(entry.argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=entry.timeout_s,
        )
    except subprocess.TimeoutExpired:
        return _UNRUNNABLE_EXIT_CODE, f"registered validator timed out after {entry.timeout_s}s"
    except OSError as exc:
        return _UNRUNNABLE_EXIT_CODE, f"registered validator could not execute: {exc}"
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part.strip())
    return completed.returncode, output

"""Drift guards for the shipped systemd units and service examples."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "packaging" / "systemd"
_ENVIRONMENT_NAME = re.compile(r"^Environment=(?P<name>[A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _environment_names(unit_path: Path) -> set[str]:
    return set(_ENVIRONMENT_NAME.findall(unit_path.read_text(encoding="utf-8")))


def test_shipped_systemd_environment_variables_are_consumed_by_their_entrypoints() -> None:
    """Every declared service environment value has a code or CLI consumer.

    The dispatch daemon uses its two values directly. The authority examples
    intentionally expand their host IDs into required command-line arguments,
    so the matching entrypoint parsers are checked as well.
    """
    expected = {
        "boardd.service.example": {"BOARDD_STATE_DIR"},
        "chitra-ownership-provider.service.example": {"CHITRA_HOST_ID"},
        "chitra-petra.service.example": {"PETRA_HOST_UUID"},
        "chitra-rate-limit-guard.service.example": set(),
        "chitra-rate-limit-guard.timer.example": set(),
        # The merge daemon takes its GitHub App token from an EnvironmentFile,
        # never an inline Environment= line, so the credential cannot end up
        # in a unit file that anyone can read.
        "polyphony-chitra-merged.service.example": set(),
    }
    actual = {unit_path.name: _environment_names(unit_path) for unit_path in sorted(SYSTEMD_DIR.glob("*.example"))}

    assert actual == expected

    ownership_unit = (SYSTEMD_DIR / "chitra-ownership-provider.service.example").read_text(encoding="utf-8")
    ownership_source = (REPO_ROOT / "src" / "chitra" / "ownership_provider.py").read_text(encoding="utf-8")
    assert "${CHITRA_HOST_ID}" in ownership_unit
    assert '"--host-id"' in ownership_source

    petra_unit = (SYSTEMD_DIR / "chitra-petra.service.example").read_text(encoding="utf-8")
    petra_source = (REPO_ROOT / "src" / "chitra" / "petra.py").read_text(encoding="utf-8")
    assert "${PETRA_HOST_UUID}" in petra_unit
    assert '"--host-uuid"' in petra_source

    boardd_source = (REPO_ROOT / "src" / "boardd" / "config.py").read_text(encoding="utf-8")
    for env_name in expected["boardd.service.example"]:
        assert f'"{env_name}"' in boardd_source
def test_shared_daemon_units_are_the_canonical_package_layout() -> None:
    """Keep docs and package units on the declaration-driven release shape.

    The old ``*.service.example`` files described a pre-lane CLI and a
    placeholder virtualenv path. The Debian package installs the checked-in
    units below, so retaining a second copy would let the two contracts drift.
    """
    units = {
        "chitra-dispatchd.service": "chitra.dispatchd",
        "chitra-triaged.service": "chitra.triaged",
    }
    for filename, module in units.items():
        unit = (SYSTEMD_DIR / filename).read_text(encoding="utf-8")
        assert f"ExecStart=/opt/chitra/venv/bin/python -m {module} --lanes-file /etc/chitra/lanes.yaml" in unit
        assert "/path/to/venv" not in unit

    assert not (SYSTEMD_DIR / "chitra-dispatchd.service.example").exists()
    assert not (SYSTEMD_DIR / "chitra-triaged.service.example").exists()

    dispatch_docs = (REPO_ROOT / "docs" / "daemons" / "delivery" / "dispatchd.md").read_text(encoding="utf-8")
    triaged_docs = (REPO_ROOT / "docs" / "daemons" / "delivery" / "triaged.md").read_text(encoding="utf-8")
    sweep_docs = (REPO_ROOT / "docs" / "daemons" / "delivery" / "sweepd.md").read_text(encoding="utf-8")
    configuration_docs = (REPO_ROOT / "docs" / "configuration" / "README.md").read_text(encoding="utf-8")
    assert "packaging/systemd/chitra-dispatchd.service`" in dispatch_docs
    assert "packaging/systemd/chitra-dispatchd.service.example" not in dispatch_docs
    assert "packaging/systemd/chitra-triaged.service`" in triaged_docs
    assert "packaging/systemd/chitra-triaged.service.example" not in triaged_docs
    assert "packaging/systemd/chitra-sweepd.service`" in sweep_docs
    assert "packaging/systemd/chitra-sweepd.service.example" not in sweep_docs
    assert "ExecStart=/usr/local/bin/dispatchd" not in configuration_docs
    assert "chitra-dispatchd.service" in configuration_docs


def test_the_merge_daemon_unit_fails_rather_than_starting_without_its_token() -> None:
    """A dash prefix would let systemd start the unit with no token at all.

    The daemon would then resolve whatever identity gh happens to hold. It
    refuses to merge under that identity, so nothing unsafe would land, but
    the unit would sit there logging refusals and look like a broken daemon
    rather than a missing credential. Fail at start instead.
    """
    unit = (SYSTEMD_DIR / "polyphony-chitra-merged.service.example").read_text(encoding="utf-8")
    environment_files = [line for line in unit.splitlines() if line.startswith("EnvironmentFile=")]
    assert environment_files, "the merge daemon unit must name the file holding its GitHub App token"
    for line in environment_files:
        assert not line.startswith("EnvironmentFile=-"), line

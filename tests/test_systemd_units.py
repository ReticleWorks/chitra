"""Drift guards for the shipped systemd service examples."""

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
        "chitra-dispatchd.service.example": {"REMOTE_DISPATCH_HOSTS", "CHITRA_LANE_LOCK_DIR"},
        # The single monitord example is the per-instance template fleet
        # renders; shadow mode is consumed by the daemon, CHITRA_STATE_DIR
        # isolates each %i instance's state root.
        "chitra-monitord@.service.example": {"CHITRA_MONITORD_SHADOW_MODE", "CHITRA_STATE_DIR"},
        "chitra-ownership-provider.service.example": {"CHITRA_HOST_ID"},
        "chitra-petra.service.example": {"PETRA_HOST_UUID"},
        "chitra-rate-limit-guard.service.example": set(),
        "chitra-rate-limit-guard.timer.example": set(),
        "chitra-triaged.service.example": set(),
        # The merge daemon takes its GitHub App token from an EnvironmentFile,
        # never an inline Environment= line, so the credential cannot end up
        # in a unit file that anyone can read.
        "polyphony-chitra-merged.service.example": set(),
    }
    actual = {unit_path.name: _environment_names(unit_path) for unit_path in sorted(SYSTEMD_DIR.glob("*.example"))}

    assert actual == expected

    dispatch_unit = (SYSTEMD_DIR / "chitra-dispatchd.service.example").read_text(encoding="utf-8")
    dispatch_source = (REPO_ROOT / "src" / "chitra" / "dispatch.py").read_text(encoding="utf-8")
    for env_name in expected["chitra-dispatchd.service.example"]:
        assert f'_env("{env_name}"' in dispatch_source
    dispatch_start = next(line for line in dispatch_unit.splitlines() if line.startswith("ExecStart="))
    assert "--lock-dir" not in dispatch_start

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

    monitord_source = (REPO_ROOT / "src" / "chitra" / "monitord.py").read_text(encoding="utf-8")
    assert 'os.environ.get("CHITRA_MONITORD_SHADOW_MODE"' in monitord_source

    state_paths_source = (REPO_ROOT / "src" / "chitra" / "state_paths.py").read_text(encoding="utf-8")
    assert '"CHITRA_STATE_DIR"' in state_paths_source


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

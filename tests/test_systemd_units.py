"""Drift guards for the shipped systemd units and service examples."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "packaging" / "systemd"
OWNERSHIP_MANIFEST = SYSTEMD_DIR / "ownership.json"
_ENVIRONMENT_NAME = re.compile(r"^Environment=(?P<name>[A-Z][A-Z0-9_]*)=", re.MULTILINE)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYSTEMD_TOKEN = re.compile(r"%[A-Za-z]|\$\{[A-Z][A-Z0-9_]*\}")


def _environment_names(unit_path: Path) -> set[str]:
    return set(_ENVIRONMENT_NAME.findall(unit_path.read_text(encoding="utf-8")))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_relative_unit_name(filename: str) -> None:
    path = Path(filename)
    assert not path.is_absolute(), filename
    assert ".." not in path.parts, filename
    assert path.name == filename, filename


def _assert_sha256(value: object, label: str) -> None:
    assert isinstance(value, str) and _SHA256.fullmatch(value), label


def _configured_external_roots(manifest: dict[str, object]) -> dict[str, Path]:
    repositories = manifest["external_repositories"]
    assert isinstance(repositories, dict)
    roots: dict[str, Path] = {}
    for repository, metadata in repositories.items():
        assert isinstance(repository, str)
        assert isinstance(metadata, dict)
        environment_variable = metadata["environment_variable"]
        assert isinstance(environment_variable, str)
        raw_root = os.environ.get(environment_variable)
        if raw_root is None:
            continue
        root = Path(raw_root).expanduser()
        assert root.is_dir(), f"{environment_variable} does not name a directory: {root}"
        roots[repository] = root
    return roots


def test_systemd_ownership_manifest_is_explicit_and_deterministic() -> None:
    """Keep unit ownership and the cross-repository inventory reviewable.

    Chitra owns the shared package units. Fleet and Polyphony own their
    deployment-specific templates. The rate-limit template is the only
    byte-identical cross-repository mirror found by the inventory scan.
    """
    manifest = json.loads(OWNERSHIP_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema"] == "chitra.systemd-ownership.v1"
    assert manifest["hash"] == "sha256-bytes"
    assert manifest["scope"] == "core-daemon-units-and-deployment-variants"
    assert set(manifest["external_repositories"]) == {
        "ReticleWorks/fleet-repo",
        "ReticleWorks/polyphony",
    }
    classes = manifest["classes"]
    assert {entry["owner"] for entry in classes.values()} >= {
        "ReticleWorks/chitra",
        "ReticleWorks/fleet-repo",
        "ReticleWorks/polyphony",
    }
    for class_name, entry in classes.items():
        assert isinstance(class_name, str)
        assert isinstance(entry["owner"], str)
        assert isinstance(entry["parameter_mode"], str)
        for filename, expected_hash in entry["units"].items():
            _assert_relative_unit_name(filename)
            if class_name == "shared-mirror":
                assert set(expected_hash) == {"fleet", "polyphony"}
                for side, side_hash in expected_hash.items():
                    _assert_sha256(side_hash, f"{class_name}.{filename}.{side}")
            else:
                _assert_sha256(expected_hash, f"{class_name}.{filename}")

    shared = classes["shared-package"]
    assert shared["parameter_mode"] == "literal"
    assert set(shared["units"]) == {
        "chitra-dispatchd.service",
        "chitra-sweepd.service",
        "chitra-triaged.service",
        "chitra-watchd.service",
    }
    for filename, expected_hash in shared["units"].items():
        unit = SYSTEMD_DIR / filename
        assert unit.is_file(), filename
        assert _sha256(unit) == expected_hash, filename
        text = unit.read_text(encoding="utf-8")
        assert not any(token in text for token in shared["forbidden_tokens"]), filename

    mirrored = classes["shared-mirror"]["units"]
    assert set(mirrored) == {
        "chitra-rate-limit-guard@.service",
        "chitra-rate-limit-guard@.timer",
    }
    for hashes in mirrored.values():
        assert hashes["fleet"] == hashes["polyphony"]
    mirror = classes["shared-mirror"]
    assert mirror["roots"] == {
        "fleet": "packages/chitra/units",
        "polyphony": "infra/systemd",
    }
    assert mirror["parameter_mode"] == "instance-template"
    assert mirror["required_tokens"] == ["%i"]
    assert set(mirror["required_tokens"]) <= set(mirror["allowed_tokens"])


def test_systemd_ownership_manifest_rejects_duplicate_class_ownership() -> None:
    """A unit name may have intentional variants, but one class owns each copy."""
    manifest = json.loads(OWNERSHIP_MANIFEST.read_text(encoding="utf-8"))
    seen: dict[tuple[str, str, str], str] = {}
    for class_name, entry in manifest["classes"].items():
        if "root" not in entry:
            continue
        units = entry["units"]
        for filename in units:
            key = (entry["owner"], entry["root"], filename)
            previous = seen.setdefault(key, class_name)
            assert previous == class_name, f"{key} appears in {previous} and {class_name}"


def test_instance_template_classes_require_instance_parameter() -> None:
    """Instance units must carry the instance parameter in their contract."""
    manifest = json.loads(OWNERSHIP_MANIFEST.read_text(encoding="utf-8"))
    for class_name in ("package-instance", "fleet-instance", "polyphony-instance", "shared-mirror"):
        entry = manifest["classes"][class_name]
        assert entry["parameter_mode"] == "instance-template"
        assert entry["required_tokens"] == ["%i"]
        assert set(entry["required_tokens"]) <= set(entry["allowed_tokens"])


def test_external_ownership_hashes_when_repositories_are_configured() -> None:
    """Verify external hashes and mirrors when an inventory checkout is supplied.

    Chitra's normal package CI has only this checkout, so the external
    repositories are opt-in through the documented environment variables. A
    consolidation or release job can set both variables to run this check;
    no developer-specific sibling path is assumed here.
    """
    manifest = json.loads(OWNERSHIP_MANIFEST.read_text(encoding="utf-8"))
    roots = _configured_external_roots(manifest)
    required_repositories = {"ReticleWorks/fleet-repo", "ReticleWorks/polyphony"}
    if not required_repositories <= roots.keys():
        pytest.skip(
            "set CHITRA_OWNERSHIP_FLEET_ROOT and CHITRA_OWNERSHIP_POLYPHONY_ROOT "
            "to run cross-repository ownership verification"
        )

    classes = manifest["classes"]
    for class_name, entry in classes.items():
        owner = entry["owner"]
        if class_name == "shared-mirror" or owner not in roots:
            continue
        root = roots[owner] / entry["root"]
        for filename, expected_hash in entry["units"].items():
            unit = root / filename
            assert unit.is_file(), f"{class_name}: missing {unit}"
            assert _sha256(unit) == expected_hash, f"{class_name}: stale hash for {filename}"
            if entry["parameter_mode"] == "instance-template":
                text = unit.read_text(encoding="utf-8")
                for token in entry["required_tokens"]:
                    assert token in text, f"{class_name}: {filename} lacks {token}"
                assert set(_SYSTEMD_TOKEN.findall(text)) <= set(entry["allowed_tokens"]), (
                    f"{class_name}: {filename} has an undeclared systemd token"
                )

    mirror = classes["shared-mirror"]
    fleet_root = roots[mirror["owner"]] / mirror["roots"]["fleet"]
    polyphony_root = roots[mirror["mirror"]] / mirror["roots"]["polyphony"]
    for filename, hashes in mirror["units"].items():
        fleet_unit = fleet_root / filename
        polyphony_unit = polyphony_root / filename
        assert fleet_unit.is_file(), fleet_unit
        assert polyphony_unit.is_file(), polyphony_unit
        assert _sha256(fleet_unit) == hashes["fleet"], f"shared mirror stale in Fleet: {filename}"
        assert _sha256(polyphony_unit) == hashes["polyphony"], f"shared mirror stale in Polyphony: {filename}"
        assert fleet_unit.read_bytes() == polyphony_unit.read_bytes(), f"shared mirror diverged: {filename}"


def test_shipped_systemd_environment_variables_are_consumed_by_their_entrypoints() -> None:
    """Every declared service environment value has a code or CLI consumer.

    The dispatch daemon uses its two values directly. The authority examples
    intentionally expand their host IDs into required command-line arguments,
    so the matching entrypoint parsers are checked as well.
    """
    expected = {
        "boardd.service.example": {"BOARDD_STATE_DIR"},
        # The single monitord example is the per-instance template fleet
        # renders; shadow mode is consumed by the daemon, CHITRA_STATE_DIR
        # isolates each %i instance's state root.
        "chitra-monitord@.service.example": {"CHITRA_MONITORD_SHADOW_MODE", "CHITRA_STATE_DIR"},
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

    monitord_source = (REPO_ROOT / "src" / "chitra" / "monitord.py").read_text(encoding="utf-8")
    assert 'os.environ.get("CHITRA_MONITORD_SHADOW_MODE"' in monitord_source

    state_paths_source = (REPO_ROOT / "src" / "chitra" / "state_paths.py").read_text(encoding="utf-8")
    assert '"CHITRA_STATE_DIR"' in state_paths_source


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


def test_persistent_supervision_units_share_lane_queue_and_binding_topology() -> None:
    """The monitor's per-lane queue must be the dispatcher's lane queue."""
    monitor = (SYSTEMD_DIR / "chitra-monitord@.service.example").read_text(encoding="utf-8")
    dispatch = (SYSTEMD_DIR / "chitra-dispatchd.service").read_text(encoding="utf-8")

    assert "ConditionPathExists=/etc/chitra/transcript-bindings.json" in monitor
    assert "--transcript-bindings-path /etc/chitra/transcript-bindings.json" in monitor
    assert "StateDirectory=chitra" in monitor
    assert "Environment=CHITRA_STATE_DIR=/var/lib/chitra/lane-%i" in monitor
    assert "ExecStartPre=/usr/bin/install -d -o chitra -g chitra -m 0750 ${CHITRA_STATE_DIR}" in monitor
    assert "--dispatch-queue-dir ${CHITRA_STATE_DIR}/queue" in monitor
    assert "--transcript-bindings-path /etc/chitra/transcript-bindings.json" in dispatch
    assert "/var/lib/chitra/lane-<id>" in dispatch

    monitor_docs = (REPO_ROOT / "docs" / "daemons" / "monitord.md").read_text(encoding="utf-8")
    assert "/var/lib/chitra/lane-<lane-id>" in monitor_docs
    assert "/etc/chitra/transcript-bindings.json" in monitor_docs


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

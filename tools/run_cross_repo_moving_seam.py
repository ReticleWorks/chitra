#!/usr/bin/env python3
"""Run the governed Tophand moving seam against explicit source checkouts.

This runner imports the Adapter and Fleet implementations from source.  It
does not build, install, package, contact a host, or use credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ACCEPTED_CHITRA_BASE = "3951285372ca6c46f38a9e4812d72db780bb1cf5"
DEFAULT_ADAPTER_ROOT = Path("/private/tmp/adapter-governed-resume-20260824")
DEFAULT_FLEET_ROOT = Path("/private/tmp/fleet-final-composition-20260824")
TEST_FILE = "tests/test_cross_repo_tophand_close_resume_process.py"


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=check,
    )


def _source_record(
    name: str,
    root: Path,
    required_path: str,
    expected_revision: str | None,
) -> dict[str, object]:
    resolved = root.expanduser().resolve(strict=True)
    if not (resolved / required_path).is_file():
        raise ValueError(f"{name} root lacks {required_path}: {resolved}")
    head = _git(resolved, "rev-parse", "HEAD").stdout.strip()
    if expected_revision is not None:
        expected = _git(resolved, "rev-parse", f"{expected_revision}^{{commit}}").stdout.strip()
        if head != expected:
            raise ValueError(f"{name} HEAD is {head}, expected {expected}")
    status = _git(resolved, "status", "--porcelain=v1").stdout
    diff = _git(resolved, "diff", "--binary", "HEAD").stdout.encode("utf-8")
    return {
        "name": name,
        "root": str(resolved),
        "head": head,
        "dirty": bool(status),
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "status": status.splitlines(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    repository = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--chitra-root",
        type=Path,
        default=Path(os.environ.get("CHITRA_SOURCE_ROOT", repository)),
    )
    parser.add_argument(
        "--adapter-root",
        type=Path,
        default=Path(os.environ.get("ADAPTER_SOURCE_ROOT", DEFAULT_ADAPTER_ROOT)),
    )
    parser.add_argument(
        "--fleet-root",
        type=Path,
        default=Path(os.environ.get("FLEET_SOURCE_ROOT", DEFAULT_FLEET_ROOT)),
    )
    parser.add_argument(
        "--adapter-revision",
        default=os.environ.get("ADAPTER_SOURCE_REVISION"),
        help="Require the Adapter checkout HEAD to equal this revision.",
    )
    parser.add_argument(
        "--fleet-revision",
        default=os.environ.get("FLEET_SOURCE_REVISION"),
        help="Require the Fleet checkout HEAD to equal this revision.",
    )
    parser.add_argument(
        "--require-clean-candidates",
        action="store_true",
        help="Reject dirty Adapter or Fleet checkouts once exact tips are available.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        chitra = _source_record(
            "chitra", arguments.chitra_root, TEST_FILE, None
        )
        chitra_root = Path(str(chitra["root"]))
        accepted = _git(
            chitra_root,
            "merge-base",
            "--is-ancestor",
            ACCEPTED_CHITRA_BASE,
            "HEAD",
            check=False,
        )
        if accepted.returncode != 0:
            raise ValueError(
                f"Chitra HEAD {chitra['head']} does not descend from accepted {ACCEPTED_CHITRA_BASE}"
            )
        adapter = _source_record(
            "adapter",
            arguments.adapter_root,
            "tools/support/chitra_adapter/tophand_adapter.py",
            arguments.adapter_revision,
        )
        fleet = _source_record(
            "fleet",
            arguments.fleet_root,
            "roles/base/files/chitra-codexman-ssh",
            arguments.fleet_revision,
        )
        if arguments.require_clean_candidates and (adapter["dirty"] or fleet["dirty"]):
            raise ValueError("Adapter or Fleet checkout is dirty")
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"moving-seam source error: {exc}", file=sys.stderr)
        return 2

    manifest = {
        "schema": "chitra.cross-repo-moving-seam.v1",
        "accepted_chitra_base": ACCEPTED_CHITRA_BASE,
        "sources": [chitra, adapter, fleet],
    }
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")), flush=True)
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "ADAPTER_SOURCE_ROOT": str(adapter["root"]),
        "FLEET_SOURCE_ROOT": str(fleet["root"]),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            TEST_FILE,
        ],
        cwd=chitra_root,
        env=environment,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

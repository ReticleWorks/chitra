"""Machine-provisioned trust anchor for authority-handoff verification.

Authority over a lane handoff is installed, not claimed.  The provisioning
path (root) writes one document per host at
``/etc/chitra/authority/handoff-authority.json`` containing the only HMAC
key that can sign ``[authority-enrollment]`` and ``[authority-handoff]``
ledger entries plus the SHA-256 of the governed lane manifest the key was
provisioned for.  Verification reads this anchor from its pinned location
and from nowhere else: no environment variable, no caller argument, and no
state-root derivation can redirect it, so a handoff claimant can never mint
or select the authority it is being judged against.  Nothing in this module
creates keys, enrollment, or ledger entries; an absent or untrusted anchor
fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import stat
from dataclasses import dataclass
from pathlib import Path

ANCHOR_SCHEMA = "chitra.handoff-authority-anchor.v1"
ANCHOR_ALGORITHM = "hmac-sha256"
ENROLLMENT_TAG = "[authority-enrollment]"
HANDOFF_TAG = "[authority-handoff]"
MACHINE_ANCHOR_PATH = Path("/etc/chitra/authority/handoff-authority.json")
_CLASS_DEFAULT_ANCHOR_PATH = MACHINE_ANCHOR_PATH
_MAX_ANCHOR_BYTES = 16 * 1024
_KEY_HEX_CHARS = 64


class AuthorityAnchorError(ValueError):
    """The machine handoff-authority anchor is absent, untrusted, or malformed."""


@dataclass(frozen=True, slots=True)
class HandoffAuthorityAnchor:
    """One loaded machine authority: its signing key and declared world."""

    path: Path
    sha256: str
    key: bytes
    provisioned_at: str
    lanes_manifest_sha256: str

def _canonical_anchor_bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _reject_symlink_components(resolved: Path) -> None:
    cursor = Path(resolved.anchor)
    for part in resolved.parts[len(Path(resolved.anchor).parts) :]:
        cursor = cursor / part
        try:
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise AuthorityAnchorError(f"handoff authority anchor path must not contain symlinks: {cursor}")
        except OSError as exc:
            raise AuthorityAnchorError(f"handoff authority anchor path cannot be inspected: {cursor}: {exc}") from exc


def _require_protected_location(path: Path, *, require_root_owned: bool) -> None:
    parent = path.parent
    try:
        parent_mode = stat.S_IMODE(parent.stat().st_mode)
        file_mode = stat.S_IMODE(path.stat().st_mode)
        parent_owner_uid = parent.stat().st_uid
        owner_uid = path.stat().st_uid
    except OSError as exc:
        raise AuthorityAnchorError(f"handoff authority anchor is unreadable at its pinned location: {path}: {exc}") from exc
    if parent_mode & 0o022:
        raise AuthorityAnchorError("handoff authority anchor directory must not be group- or world-writable")
    if file_mode & 0o077:
        raise AuthorityAnchorError("handoff authority HMAC anchor must not be accessible by group or world")
    if require_root_owned and (parent_owner_uid != 0 or owner_uid != 0):
        raise AuthorityAnchorError("handoff authority anchor and directory must be owned by root at the pinned system location")


def load_machine_anchor(path: Path | None = None) -> HandoffAuthorityAnchor:
    """Load the pinned machine anchor; explicit paths are verifier-side fixtures only.

    The production gate always resolves the pinned system location, and that
    location must be root-owned.  A fixture may pass an explicit path for
    verifier-owned test setup; every structural rule still applies, and the
    root-ownership requirement applies whenever resolution lands on the
    class-default pinned path.
    """
    raw_path = (path if path is not None else MACHINE_ANCHOR_PATH).expanduser()
    absolute = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    _reject_symlink_components(absolute)
    resolved = absolute.resolve(strict=False)
    require_root_owned = resolved == _CLASS_DEFAULT_ANCHOR_PATH
    _require_protected_location(resolved, require_root_owned=require_root_owned)
    try:
        if not resolved.is_file():
            raise AuthorityAnchorError(f"machine handoff-authority anchor is not installed: {resolved}")
        size = resolved.stat().st_size
        if size > _MAX_ANCHOR_BYTES:
            raise AuthorityAnchorError("machine handoff-authority anchor exceeds the size bound")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except AuthorityAnchorError:
        raise
    except OSError as exc:
        raise AuthorityAnchorError(f"machine handoff-authority anchor cannot be read: {resolved}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AuthorityAnchorError(f"machine handoff-authority anchor is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AuthorityAnchorError("machine handoff-authority anchor must contain a JSON object")
    integrity = payload.get("integrity")
    unsigned = dict(payload)
    expected_digest = ""
    if isinstance(integrity, dict):
        unsigned.pop("integrity")
        declared = integrity.get("digest")
        expected_digest = declared if isinstance(declared, str) else ""
    actual_digest = hashlib.sha256(_canonical_anchor_bytes(unsigned)).hexdigest()
    if not hmac.compare_digest(expected_digest, actual_digest):
        raise AuthorityAnchorError("machine handoff-authority anchor integrity digest does not match its contents")
    if payload.get("schema") != ANCHOR_SCHEMA:
        raise AuthorityAnchorError("machine handoff-authority anchor schema is invalid")
    if payload.get("algorithm") != ANCHOR_ALGORITHM:
        raise AuthorityAnchorError("machine handoff-authority anchor algorithm is unsupported")
    key_hex = payload.get("key")
    if not isinstance(key_hex, str) or len(key_hex) != _KEY_HEX_CHARS or any(char not in "0123456789abcdef" for char in key_hex):
        raise AuthorityAnchorError("machine handoff-authority anchor key must be 64 lowercase hex characters")
    manifest_sha = payload.get("lanes_manifest_sha256")
    if not isinstance(manifest_sha, str) or len(manifest_sha) != 64 or any(char not in "0123456789abcdef" for char in manifest_sha):
        raise AuthorityAnchorError("machine handoff-authority anchor must declare the governed lanes-manifest SHA-256")
    provisioned_at = payload.get("provisioned_at")
    if not isinstance(provisioned_at, str) or not provisioned_at:
        raise AuthorityAnchorError("machine handoff-authority anchor must declare provisioned_at")
    return HandoffAuthorityAnchor(
        path=resolved,
        sha256=actual_digest,
        key=bytes.fromhex(key_hex),
        provisioned_at=provisioned_at,
        lanes_manifest_sha256=manifest_sha,
    )


def build_machine_anchor_document(*, key: bytes, provisioned_at: str, lanes_manifest_sha256: str) -> dict[str, object]:
    """Return one anchor document for provisioning ceremonies and verifier-owned fixtures."""
    payload: dict[str, object] = {
        "schema": ANCHOR_SCHEMA,
        "algorithm": ANCHOR_ALGORITHM,
        "key": key.hex(),
        "provisioned_at": provisioned_at,
        "lanes_manifest_sha256": lanes_manifest_sha256,
    }
    digest = hashlib.sha256(_canonical_anchor_bytes(payload)).hexdigest()
    payload["integrity"] = {
        "algorithm": "sha256",
        "scope": "entire anchor with integrity.digest omitted",
        "digest": digest,
    }
    return payload

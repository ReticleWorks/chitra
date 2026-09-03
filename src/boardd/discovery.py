"""Monitor discovery: which Chitra state roots exist on this host.

boardd used to take a fixed BOARDD_STATE_DIR (one instance) or a hand-edited
BOARDD_STATE_ROOTS env map (several instances, manually kept in sync). Both
require an operator to remember to update boardd when a monitor instance is
added or removed. Instead boardd finds them itself, every time it is asked:

- unit discovery: which chitra systemd template instances are installed
  (`polyphony-chitra-watchd@<id>`, `triaged@<id>`, `dispatchd@<id>`,
  `sweepd@<id>`) via `systemctl list-units`;
- root discovery: which `/var/lib/polyphony-chitra*` directories actually
  hold a goals.json.

The two lists are keyed by the same monitor id (the unit instance name, or
the state-root's suffix after `polyphony-chitra-`; a bare root is id
"monitor") and unioned. Either signal alone is enough to list a monitor —
a unit that has not written a goals.json yet, or a root left behind by a
stopped unit, both still show up so the operator can see the mismatch.

BOARDD_STATE_ROOTS (an id=path,id=path map) replaces both signals ONLY when
BOARDD_DEV=1 is also set — for tests and local smoke, where there is no real
systemd and no /var/lib to scan.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

UNIT_TEMPLATES = (
    "polyphony-chitra-watchd@*",
    "triaged@*",
    "dispatchd@*",
    "sweepd@*",
)
ROOT_GLOB_BASE = Path("/var/lib")
ROOT_PREFIX = "polyphony-chitra"
DEFAULT_MONITOR_ID = "monitor"

_UNIT_RE = re.compile(r"^\S*@([^.\s]+)\.\S+\s+\S+\s+(\S+)\s")


def root_for_id(monitor_id: str, base: Path = ROOT_GLOB_BASE) -> Path:
    return base / ROOT_PREFIX if monitor_id == DEFAULT_MONITOR_ID else base / f"{ROOT_PREFIX}-{monitor_id}"


def _id_for_root(dirname: str) -> str | None:
    if dirname == ROOT_PREFIX:
        return DEFAULT_MONITOR_ID
    if dirname.startswith(ROOT_PREFIX + "-"):
        return dirname[len(ROOT_PREFIX) + 1 :]
    return None


def discover_state_roots(base: Path = ROOT_GLOB_BASE) -> dict[str, Path]:
    """Glob `<base>/polyphony-chitra*` dirs that hold a goals.json. Pure, testable."""
    found: dict[str, Path] = {}
    try:
        candidates = sorted(base.glob(f"{ROOT_PREFIX}*"))
    except OSError:
        return found
    for path in candidates:
        monitor_id = _id_for_root(path.name)
        if monitor_id is None or not path.is_dir():
            continue
        if (path / config.GOALS_FILE).exists():
            found[monitor_id] = path
    return found


def discover_units(timeout: float = 3.0) -> dict[str, str]:
    """Return {monitor_id: active_state} for installed chitra unit instances.

    Best-effort: no systemctl, no systemd, or a timeout all mean "no unit
    signal", never an error — root discovery still stands on its own.
    """
    found: dict[str, str] = {}
    try:
        proc = subprocess.run(
            ["systemctl", "list-units", "--all", "--plain", "--no-legend", *UNIT_TEMPLATES],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return found
    if proc.returncode not in (0, 1):  # systemctl: 1 = some units listed as not-found, still usable output
        return found
    for line in proc.stdout.splitlines():
        m = _UNIT_RE.match(line)
        if not m:
            continue
        monitor_id, active_state = m.group(1), m.group(2)
        # A unit signal only wins over another unit signal by being active;
        # never let one dead template hide another's live instance.
        if monitor_id not in found or active_state == "active":
            found[monitor_id] = active_state
    return found


def _dev_roots() -> dict[str, Path]:
    raw = os.environ.get("BOARDD_STATE_ROOTS", "")
    if not raw.strip():
        return {DEFAULT_MONITOR_ID: config.STATE_DIR}
    roots: dict[str, Path] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        monitor_id, _, path = entry.partition("=")
        if monitor_id and path:
            roots[monitor_id.strip()] = Path(path.strip())
    return roots or {DEFAULT_MONITOR_ID: config.STATE_DIR}


@dataclass(frozen=True)
class MonitorInfo:
    id: str
    state_root: str
    unit_active_state: str
    lane_count: int
    needs_feedback_count: int
    # False for a unit discovered with no goals.json directory behind it yet
    # (root_for_id's synthetic path doesn't exist on disk). The mobile
    # monitor picker greys this row out rather than pretending it has data.
    has_state_root: bool = True


def is_dev_mode() -> bool:
    return os.environ.get("BOARDD_DEV") == "1"


def discover_monitors() -> dict[str, Path]:
    """Return {monitor_id: state_root} — the roots half only, unit state added by the caller."""
    if is_dev_mode():
        return _dev_roots()
    roots = discover_state_roots()
    for monitor_id in discover_units():
        roots.setdefault(monitor_id, root_for_id(monitor_id))
    return roots

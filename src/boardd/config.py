"""boardd configuration.

Everything is environment-driven so the same code runs against the bundled
fixture state dir (tests/fixtures/boardd_state) and the real
/var/lib/polyphony-chitra on twinridge.
"""

import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent

# Directory holding goals.json (chitra.goals.v1) and sweep-digest.json.
# Real deployment: /var/lib/polyphony-chitra on host twinridge.
STATE_DIR = Path(os.environ.get("BOARDD_STATE_DIR", "/var/lib/polyphony-chitra"))

# Translation cache seed (read-only, ships with the app).
TRANSLATION_SEED = Path(
    os.environ.get("BOARDD_TRANSLATION_SEED", PKG_DIR / "data" / "translations-seed.json")
)

GOALS_FILE = "goals.json"
DIGEST_FILE = "sweep-digest.json"

# Seconds without a state-file write before the data itself is called stale
# in the UI copy (the SSE liveness state is separate and client-observed).
STALE_AFTER_SECONDS = int(os.environ.get("BOARDD_STALE_AFTER_SECONDS", "900"))

# SSE heartbeat interval, seconds.
HEARTBEAT_SECONDS = float(os.environ.get("BOARDD_HEARTBEAT_SECONDS", "15"))

# How often /events re-runs monitor discovery and pushes a "monitors" event.
MONITORS_TICK_SECONDS = float(os.environ.get("BOARDD_MONITORS_TICK_SECONDS", "30"))

# The co-located, vendored agenttrail process (src/boardd/vendor/agenttrail).
# boardd posts synthesized hook events to it and iframes its UI from here.
AGENTTRAIL_HOOK_URL = os.environ.get("BOARDD_AGENTTRAIL_HOOK_URL", "http://127.0.0.1:5330/hook")
AGENTTRAIL_PUBLIC_URL = os.environ.get("BOARDD_AGENTTRAIL_URL", "http://127.0.0.1:5330/")
# agenttrail keys its live "runs" to events whose cwd matches its own repo
# root; boardd's synthesized events carry this fixed value regardless of
# which monitor a lane actually lives on, since agenttrail has no concept of
# multiple chitra monitors. It is also the repo path boardd spawns the
# vendored agenttrail process against — the two must keep matching.
AGENTTRAIL_CWD = os.environ.get("BOARDD_AGENTTRAIL_CWD", "/var/lib/polyphony-chitra")

# `node` binary boardd spawns the vendored agenttrail process with. A bare
# name relies on PATH; override with an absolute path if the deploy unit's
# PATH doesn't carry it.
AGENTTRAIL_NODE_BIN = os.environ.get("BOARDD_AGENTTRAIL_NODE_BIN", "node")

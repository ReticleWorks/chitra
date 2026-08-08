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

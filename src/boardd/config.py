"""boardd configuration.

Everything is environment-driven so the same code runs against the bundled
fixture state dir (tests/fixtures/boardd_state) and the configured Chitra
state root.
"""

import os
from pathlib import Path

from chitra.state_paths import state_dir

PKG_DIR = Path(__file__).resolve().parent

# Directory holding Chitra's goals, digest, and joined-lane records.  The
# existing boardd override remains useful for fixtures and an explicitly
# isolated deployment; normal operation follows Chitra's state-root authority.
STATE_DIR = Path(os.environ.get("BOARDD_STATE_DIR", str(state_dir())))

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

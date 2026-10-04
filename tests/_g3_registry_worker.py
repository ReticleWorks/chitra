"""One-shot account-registry sweep worker for the concurrent-writer test.

Runs the real ``chitra.rate_limit_guard.sweep`` daemon entry over the usage
dir, goals root, and queue dir passed on argv, with the Linux PSI pressure
probe pinned to a quiet sample (an OS edge, not a module fake). Invoked as a
script by tests/test_g3_status_gates_e2e.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

from chitra.load_shed import PressureSample
from chitra.rate_limit_guard import sweep


def main() -> int:
    usage_dir, goals_root, queue_dir = (Path(arg) for arg in sys.argv[1:4])
    report = sweep(
        usage_dir=usage_dir,
        host="host-b",
        goals_root=goals_root,
        queue_dir=queue_dir,
        pressure_sample=PressureSample(mem_available_pct=90.0, memory_some_avg60=0.0, memory_full_avg60=0.0, cpu_some_avg60=0.0),
    )
    print(report.to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

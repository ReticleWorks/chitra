"""Restart-on-exit is the one piece of non-trivial control flow in the
supervisor; everything else is a thin subprocess wrapper. Drives a real,
fast-exiting child process (no Node needed) through several restarts."""

import sys
import time

from boardd.agenttrail_supervisor import AgenttrailSupervisor


def test_supervisor_restarts_a_crashing_process(tmp_path):
    counter = tmp_path / "runs"
    counter.write_text("")

    sup = AgenttrailSupervisor(node_bin=sys.executable, backoff=(0.01, 0.01))
    # Stand in for the real `node <vendored>/bin/agenttrail.mjs ...` argv —
    # a one-liner that appends one byte and exits immediately, so each
    # restart is observable without spawning Node or agenttrail itself.
    sup._argv = lambda: [sys.executable, "-c", f"open({str(counter)!r}, 'a').write('x')"]  # type: ignore[method-assign]

    sup.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(counter.read_text()) < 3:
            time.sleep(0.05)
        assert len(counter.read_text()) >= 3, "expected at least 3 restarts within 3s"
    finally:
        sup.stop()


def test_stop_before_start_is_a_no_op():
    AgenttrailSupervisor(node_bin=sys.executable).stop()

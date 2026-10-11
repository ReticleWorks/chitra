"""A separate process contains a stuck monitor even while its main pass runs."""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def monitor_limits(cpu_percent: float, rss_mb: int, window_seconds: float) -> Iterator[None]:
    """Watch this process; closing the pipe also stops the watchdog on a crash."""
    watcher = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(os.getpid()), str(cpu_percent), str(rss_mb), str(window_seconds)],
        stdin=subprocess.PIPE,
    )
    try:
        yield
    finally:
        assert watcher.stdin is not None
        watcher.stdin.close()
        try:
            watcher.wait(timeout=4)
        except subprocess.TimeoutExpired:
            watcher.kill()
            watcher.wait()


def _sample(pid: int) -> tuple[int, float]:
    # ps reports RSS in KiB and cumulative process CPU on both Darwin and Linux.
    result = subprocess.run(
        ["ps", "-o", "rss=", "-o", "time=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    rss, cpu = result.stdout.split()
    seconds = 0.0
    days, separator, clock = cpu.partition("-")
    if separator:
        seconds = int(days) * 86400.0
        cpu = clock
    clock_seconds = 0.0
    for field in cpu.split(":"):
        clock_seconds = clock_seconds * 60 + float(field)
    return int(rss) * 1024, seconds + clock_seconds


def _watch(pid: int, cpu_percent: float, rss_mb: int, window_seconds: float) -> None:
    samples: deque[tuple[float, float]] = deque()
    while os.getppid() == pid:
        if select.select([sys.stdin], [], [], 0.25)[0]:
            return
        try:
            rss, cpu = _sample(pid)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = f"resource sampling unavailable: {type(exc).__name__}"
        else:
            now = time.monotonic()
            samples.append((now, cpu))
            while len(samples) > 1 and now - samples[1][0] >= window_seconds:
                samples.popleft()
            elapsed = now - samples[0][0]
            measured_cpu = 100 * (cpu - samples[0][1]) / elapsed if elapsed else 0
            reason = ""
            if rss > rss_mb * 1024 * 1024:
                reason = f"RSS {rss} bytes exceeds {rss_mb} MiB"
            elif elapsed >= window_seconds and measured_cpu > cpu_percent:
                reason = f"CPU {measured_cpu:.1f}% over {elapsed:.2f}s exceeds {cpu_percent}%"
        if reason and os.getppid() == pid:
            print(json.dumps({"event": "monitord_self_limit_exceeded", "pid": pid, "reason": reason}), file=sys.stderr, flush=True)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL if reason.startswith("RSS") else signal.SIGTERM)
            if reason.startswith("RSS"):
                return
            # Pipe closure cannot exempt stuck worker threads from containment.
            time.sleep(1.0)
            if os.getppid() == pid:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            return


if __name__ == "__main__":
    _watch(int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))

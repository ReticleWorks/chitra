"""One credential-free acceptance path through real daemon processes.

The worker in this test is deliberately not Claude.  It is a tiny stdin
consumer running in a real tmux pane and writing the same structural JSONL
records that Chitra uses as its lane-consumption proof.  The test therefore
exercises the process boundary, the actual tmux socket, the send nonce, and
restart reconciliation without a model, network, or user login.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from _goal_fixtures import enrollment_fields

from chitra.dispatch import nudge_confirmation_marker
from chitra.goals import GoalRecord, upsert_goal

TMUX = shutil.which("tmux")
CLAUDE_VERSION = "2.1.229"


def _worker_source() -> str:
    """Return a tiny tmux-pane worker with no Chitra or Claude dependency."""
    return '''
import json
import sys
import termios
from pathlib import Path

transcript = Path(sys.argv[1])
session_id = sys.argv[2]
accepted = False
fd = sys.stdin.fileno()
old_attrs = termios.tcgetattr(fd)
attrs = termios.tcgetattr(fd)
attrs[3] &= ~(termios.ECHO | termios.ECHONL)
termios.tcsetattr(fd, termios.TCSANOW, attrs)
print("$", flush=True)
try:
    for raw in sys.stdin:
        text = raw.replace("\\x1b[200~", "").replace("\\x1b[201~", "").strip()
        if not text or accepted:
            continue
        accepted = True
        rows = [
            {
                "sessionId": session_id,
                "uuid": "process-acceptance-user",
                "version": "2.1.229",
                "type": "user",
                "message": {"role": "user", "content": text},
            },
            {
                "sessionId": session_id,
                "uuid": "process-acceptance-assistant",
                "version": "2.1.229",
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "accepted"}]},
            },
        ]
        with transcript.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\\n")
            handle.flush()
        print("accepted", flush=True)
        print("$", flush=True)
finally:
    termios.tcsetattr(fd, termios.TCSANOW, old_attrs)
'''


def _env(repo_root: Path, tmux_tmpdir: Path, projects_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    source = str(repo_root / "src")
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env["TMUX_TMPDIR"] = str(tmux_tmpdir)
    env["CHITRA_CLAUDE_PROJECTS"] = str(projects_root)
    return env


def _run_cli(args: list[str], *, env: dict[str, str], timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


@pytest.mark.skipif(TMUX is None, reason="tmux is required for the real-pane acceptance path")
def test_real_monitord_dispatchd_restart_reconciles_one_tmux_delivery(tmp_path: Path) -> None:
    """A killed dispatchd is recovered by a fresh process without a resend."""
    assert TMUX is not None
    repo_root = Path(__file__).parents[1]
    state = tmp_path / "state"
    queue = tmp_path / "queue"
    projects = tmp_path / "projects"
    transcript = projects / "acceptance" / "session.jsonl"
    bindings = tmp_path / "transcript-bindings.json"
    # macOS and some BSD tmux builds use a Unix socket under
    # ``$TMUX_TMPDIR/tmux-$UID/default``.  Keep this one path short even when
    # pytest's temporary root is deeply nested; otherwise the real socket
    # fails with ``AF_UNIX path too long`` before the daemon is exercised.
    tmux_tmpdir = Path(tempfile.mkdtemp(prefix="ct-", dir="/tmp"))
    session = "chitra-process-acceptance"
    session_ref = f"localhost:{session}:0.0"
    native_session_id = "native-process-acceptance"
    env = _env(repo_root, tmux_tmpdir, projects)

    goal = GoalRecord(
        session_ref=session_ref,
        goal="Keep the credential-free process acceptance lane moving.",
        done_when="The acceptance worker receives one supervised nudge.",
        source="test:real-process-acceptance",
        status="working",
        intent="Exercise persistent oversight through real daemon restarts.",
        scope="This credential-free process acceptance lane only.",
        **enrollment_fields("The acceptance worker receives one supervised nudge."),
    )
    upsert_goal(state, goal)

    fixture = repo_root / "tests" / "fixtures" / "failure-modes" / "claude-unnecessary-steps.jsonl"
    transcript.parent.mkdir(parents=True)
    initial_rows: list[str] = []
    for line in fixture.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if isinstance(row, dict) and "sessionId" in row:
            row["sessionId"] = native_session_id
        initial_rows.append(json.dumps(row))
    transcript.write_text("\n".join(initial_rows) + "\n", encoding="utf-8")
    bindings.write_text(
        json.dumps(
            {
                "schema": "chitra.transcript-bindings.v1",
                "bindings": [
                    {
                        "session_ref": session_ref,
                        "lane": session,
                        "path": str(transcript),
                        "client": "claude",
                        "client_version": CLAUDE_VERSION,
                        "instance": "pytest-real-process",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    worker = tmp_path / "acceptance_worker.py"
    worker.write_text(_worker_source(), encoding="utf-8")
    tmux_start = subprocess.run(
        [TMUX, "new-session", "-d", "-s", session, sys.executable, str(worker), str(transcript), native_session_id],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert tmux_start.returncode == 0, tmux_start.stderr

    first_dispatch: subprocess.Popen[str] | None = None
    try:
        monitor = _run_cli(
            [
                "-m",
                "chitra.monitord",
                "--state-dir",
                str(state),
                "--transcript-bindings-path",
                str(bindings),
                "--dispatch-queue-dir",
                str(queue),
                "--no-shadow-mode",
                "--once",
            ],
            env=env,
        )
        assert monitor.returncode == 0, monitor.stderr
        orders = sorted((queue / "orders").glob("*.json"))
        assert len(orders) == 1
        order: dict[str, Any] = json.loads(orders[0].read_text(encoding="utf-8"))
        order_id = str(order["order_id"])
        marker = nudge_confirmation_marker(str(order["nudge"]))

        dispatch_args = [
            "-m",
            "chitra.dispatchd",
            "--queue-dir",
            str(queue),
            "--lock-dir",
            str(tmp_path / "dispatch-locks"),
            "--ledger-path",
            str(state / "ledger.jsonl"),
            "--ledger-key-path",
            str(state / "ledger.key"),
            "--goals-root",
            str(state),
            "--transcript-bindings-path",
            str(bindings),
            "--post-paste-wait-seconds",
            "5",
            "--once",
        ]
        first_dispatch = subprocess.Popen(
            [sys.executable, *dispatch_args],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and marker not in transcript.read_text(encoding="utf-8"):
            if first_dispatch.poll() is not None:
                break
            time.sleep(0.02)

        if marker not in transcript.read_text(encoding="utf-8"):
            pane = subprocess.run(
                [TMUX, "capture-pane", "-p", "-t", f"{session}:0.0"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            if first_dispatch.poll() is None:
                first_dispatch.kill()
                _stdout, stderr = first_dispatch.communicate(timeout=5)
                detail = f"dispatchd was still running; stderr={stderr}"
            else:
                _stdout, stderr = first_dispatch.communicate(timeout=1)
                detail = f"dispatchd exited rc={first_dispatch.returncode}: {stderr}"
            pytest.fail(f"the real tmux worker never received the nudge; pane={pane.stdout!r}; {detail}")
        assert first_dispatch.poll() is None, "dispatchd finished before the crash window"
        assert (queue / "in_flight" / f"{order_id}.json").exists()
        assert (queue / "in_flight" / f".{order_id}.nonce").exists()

        first_dispatch.kill()
        first_stdout, first_stderr = first_dispatch.communicate(timeout=5)
        assert first_dispatch.returncode != 0
        assert not (queue / "results" / f"{order_id}.json").exists(), (first_stdout, first_stderr)

        restarted = _run_cli(dispatch_args, env=env)
        assert restarted.returncode == 0, restarted.stderr
        result_path = queue / "results" / f"{order_id}.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["status"] == "sent"
        assert "existing nonce reconciled" in result["reason"]
        assert (queue / "processed" / f"{order_id}.json").exists()
        assert not (queue / "orders" / f"{order_id}.json").exists()
        assert not (queue / "in_flight" / f"{order_id}.json").exists()

        user_markers = 0
        for raw_line in transcript.read_text(encoding="utf-8").splitlines():
            row = json.loads(raw_line)
            if row.get("type") == "user" and marker in json.dumps(row):
                user_markers += 1
        assert user_markers == 1
    finally:
        if first_dispatch is not None and first_dispatch.poll() is None:
            first_dispatch.kill()
            first_dispatch.communicate(timeout=5)
        subprocess.run([TMUX, "kill-session", "-t", session], env=env, capture_output=True, check=False)
        shutil.rmtree(tmux_tmpdir, ignore_errors=True)

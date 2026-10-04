"""Process-boundary helpers for the G3 replace-then-remove status/convlog/roster gate tests.

The surfaces under test are driven through their real console entry points
(installed scripts or ``python -m chitra.<module>`` subprocesses) over real
files: goals.json, convlog JSONL, artifacts.json, account_registry.json,
usage snapshots, the dispatch queue, and a real tmux server on a unique
``-S`` socket. The only fakes live at the outermost edges: a stub ``claude``
executable standing in for the model provider, and a fake ``codex`` binary
that gives a tmux pane a recognized agent identity.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest


def script(name: str) -> str:
    """Resolve an installed console script next to the interpreter or on PATH."""
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if found is not None:
        return found
    raise RuntimeError(f"console entry point {name} is not installed")


def run_cli(
    argv: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    stdin: str = "",
    bin_dir: Path | None = None,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a real console entry point with a scrubbed, augmented environment."""
    env = dict(os.environ)
    env.pop("GH_SHIM_CONFIG", None)
    env.pop("GH_SHIM_LOG", None)
    env.pop("STUB_MODE", None)
    env.pop("STUB_FINDINGS", None)
    env.pop("STUB_LOG", None)
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def stdout_json(result: subprocess.CompletedProcess[str]) -> dict:
    """Parse the JSON object a CLI printed, ignoring surrounding log lines."""
    return json.loads(result.stdout[result.stdout.index("{") :])


def real_tmux() -> str:
    """Resolve the unwrapped tmux binary.

    Some hosts put an env-scrubbing tmux wrapper first on PATH: it replaces
    the tmux server's environment, which drops the stub bin directory's PATH
    lead and any stub env vars, so panes end up running the provider's real
    CLI instead of the stubs. A wrapper keeps the real binary beside it as
    ``tmux.real``.
    """
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is required for a real pane launch")
    resolved = Path(tmux).resolve()
    real = resolved.with_name("tmux.real")
    return str(real if real.is_file() else resolved)


def install_real_tmux(bin_dir: Path) -> Path:
    """Symlink the unwrapped tmux binary into a PATH-leading bin dir."""
    link = bin_dir / "tmux"
    link.symlink_to(real_tmux())
    return link


def tmux(socket_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run tmux against one test-owned socket (never the shared default)."""
    return subprocess.run(
        [real_tmux(), "-S", str(socket_path), *argv],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def install_fake_codex(bin_dir: Path) -> Path:
    """Install a ``codex`` executable whose only job is to occupy a pane.

    tmux reports ``pane_current_command`` as the running program's name, so a
    real executable named ``codex`` gives ``list_session_panes`` a recognized
    backend without faking anything inside chitra. A copied interpreter does
    not play that role on macOS: a framework Python re-execs into
    ``Python.app``, so the pane reports ``Python`` instead of ``codex``, and
    copies of platform binaries like ``/bin/sleep`` are SIGKILLed when run
    from a test path. A tiny program compiled at fixture time is ad-hoc
    signed by the linker and reports ``codex`` whatever Python runs the
    suite.
    """
    cc = shutil.which("cc")
    if cc is None:
        pytest.fail("cc is required to build the fake codex binary")
    source = bin_dir / "codex.c"
    source.write_text("#include <unistd.h>\nint main(void) { for (;;) sleep(600); }\n", encoding="utf-8")
    path = bin_dir / "codex"
    result = subprocess.run([cc, "-o", str(path), str(source)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"cc failed to build the fake codex binary: {result.stderr.strip()}")
    return path


@pytest.fixture
def codex_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    return install_fake_codex(bin_dir)


PANE_ROWS = 50


def _pane_shell_command(codex_bin: Path, content: str) -> str:
    """A pane command that pins ``content`` to the bottom of the screen.

    Agent TUIs draw at the bottom of the terminal; the manifest's live-region
    rules only look there. Blank lines first scroll the capture up so the
    snapshot text occupies the last rows, then ``exec`` swaps the pane's
    process for the fake codex binary so ``pane_current_command`` reports
    ``codex`` while the printed lines stay on screen.
    """
    lines = content.splitlines()
    pad = max(0, PANE_ROWS - len(lines))
    literal = content.replace("'", "'\\''")
    return (
        f"yes '' | head -n {pad}; "
        f"printf '%s\\n' '{literal}'; "
        f"exec '{codex_bin}'"
    )


def launch_codex_pane(socket_path: Path, session: str, codex_bin: Path, content: str) -> None:
    """Start one tmux session that renders ``content`` then becomes ``codex``."""
    result = tmux(
        socket_path,
        "new-session",
        "-d",
        "-s",
        session,
        "-x",
        "200",
        "-y",
        str(PANE_ROWS),
        _pane_shell_command(codex_bin, content),
    )
    assert result.returncode == 0, result.stderr


def respawn_codex_pane(socket_path: Path, session: str, codex_bin: Path, content: str) -> None:
    """Replace the pane's content+process (a changed capture on the same pane)."""
    result = tmux(
        socket_path,
        "respawn-pane",
        "-k",
        "-t",
        f"{session}:0.0",
        _pane_shell_command(codex_bin, content),
    )
    assert result.returncode == 0, result.stderr


def kill_tmux(socket_path: Path, session: str) -> None:
    """Kill the named session on the test-owned socket; the server exits with it."""
    tmux(socket_path, "kill-session", "-t", session)


_SOCKET_DIRS: list[Path] = []


def _cleanup_socket_dirs() -> None:
    for directory in _SOCKET_DIRS:
        shutil.rmtree(directory, ignore_errors=True)


atexit.register(_cleanup_socket_dirs)


def unique_socket(tmp_path: Path, name: str = "chitra-g3") -> Path:
    """A unix socket path short enough for every platform's AF_UNIX limit.

    macOS caps socket paths at about 104 characters and pytest ``tmp_path``
    under the default macOS TMPDIR exceeds that, so sockets live in a short
    ``/tmp`` directory instead of under ``tmp_path``.
    """
    del tmp_path  # unused: the socket must not live under tmp_path (see docstring)
    socket_dir = Path(tempfile.mkdtemp(prefix="g3t", dir="/tmp"))
    _SOCKET_DIRS.append(socket_dir)
    return socket_dir / f"{name}-{uuid.uuid4().hex[:8]}.sock"


def wait_pane_command(socket_path: Path, session: str, want: str, timeout: float = 10.0) -> None:
    """Wait until pane_current_command reports the agent binary."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = tmux(socket_path, "list-panes", "-s", "-t", session, "-F", "#{pane_current_command}")
        if result.returncode == 0 and result.stdout.strip() == want:
            return
        time.sleep(0.1)
    raise AssertionError(f"pane on {socket_path} never reported command {want!r}: {result.stdout!r} {result.stderr!r}")


# A stub ``claude`` process standing in for the model provider. Behaviour is
# steered by env vars so one stub covers every scenario:
#   STUB_QUEUE    file holding one verdict JSON object per line; each
#                 invocation pops the first line (lets one round mix verdicts)
#   STUB_MODE     accept|reject|insufficient|invalid_json|fail (default accept)
#   STUB_FINDINGS JSON list of finding objects
#   STUB_TAMPER_GOAL / STUB_TAMPER_BEHAVIOR   wrong binding field
#   STUB_ARGV_LOG append each full argv list as one JSON line
#   STUB_REDIRECT once per STUB_REDIRECT_MARK file, run the recorded redirect
#                 argv (env-expanded) mid-review before answering
REVIEWER_STUB = """\
#!/usr/bin/env python3
import json
import os
import subprocess
import sys

argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
request = {}
if "\\nINPUT=" in prompt:
    request = json.loads(prompt.rsplit("\\nINPUT=", 1)[1])

log_path = os.environ.get("STUB_ARGV_LOG", "")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "prompt": prompt}) + "\\n")

redirect_mark = os.environ.get("STUB_REDIRECT_MARK", "")
redirect_argv = os.environ.get("STUB_REDIRECT_ARGV", "")
if redirect_mark and redirect_argv and not os.path.exists(redirect_mark):
    open(redirect_mark, "w").write("1")
    subprocess.run(redirect_argv.split("\\x1f"), check=False)

queue_path = os.environ.get("STUB_QUEUE", "")
queued = None
if queue_path and os.path.exists(queue_path):
    with open(queue_path, encoding="utf-8") as handle:
        lines = [line for line in handle.read().splitlines() if line.strip()]
    if lines:
        queued = json.loads(lines[0])
        with open(queue_path, "w", encoding="utf-8") as handle:
            handle.write("\\n".join(lines[1:]) + ("\\n" if len(lines) > 1 else ""))

mode = os.environ.get("STUB_MODE", "accept")
if queued is not None:
    sys.stdout.write(json.dumps(queued))
    sys.exit(0)
if mode == "fail":
    sys.stderr.write("isolated reviewer unavailable")
    sys.exit(3)
if mode == "invalid_json":
    sys.stdout.write("not a verdict json object")
    sys.exit(0)

findings = json.loads(os.environ.get("STUB_FINDINGS", "[]"))
if "diff_sha256" in request:
    verdict = {
        "reviewer_id": request["reviewer_id"],
        "diff_sha256": request["diff_sha256"],
        "verdict": "clean",
        "findings": findings,
    }
else:
    contract = request.get("frozen_goal") or request.get("monitor_contract") or {}
    verdict = {
        "reviewer_id": request["reviewer_id"],
        "goal_contract_id": contract.get("contract_id", ""),
        "behavior_sha256": request["watched_session_behavior"]["behavior_sha256"],
        "verdict": mode if mode in ("reject", "insufficient") else "accept",
        "findings": findings,
    }
    if os.environ.get("STUB_TAMPER_GOAL"):
        verdict["goal_contract_id"] = "sha256:" + "0" * 64
    if os.environ.get("STUB_TAMPER_BEHAVIOR"):
        verdict["behavior_sha256"] = "0" * 64

out = json.dumps(verdict)
if mode == "fenced":
    out = "```json\\n" + out + "\\n```"
sys.stdout.write(out)
"""


def install_reviewer_stub(bin_dir: Path, name: str = "stub-claude") -> Path:
    import stat

    path = bin_dir / name
    path.write_text(REVIEWER_STUB, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path

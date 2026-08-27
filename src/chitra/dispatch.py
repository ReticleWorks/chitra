"""Hardened tmux dispatch library for delivering text into a Claude Code
tmux pane.

This module was extracted and hardened from an earlier internal
implementation that had two known bugs, both fixed here:

(a) ``paste-buffer`` without ``-p`` sends no bracketed-paste wrapper, so
    newlines in multiline text act as real Enters — the original
    implementation did NOT pass ``-p`` to ``paste-buffer``. This module
    adds ``-p`` (mandatory).

(b) A pane in tmux copy-mode (``pane_in_mode=1``) silently eats all input.
    The original had no check for this. This module checks
    ``tmux display-message -p -t <target> '#{pane_in_mode}'`` and, if ``1``,
    runs ``tmux send-keys -X cancel`` and waits ~0.3s before injecting. The
    check runs against the actual target host: a plain local ``tmux`` call
    for a local target, or the identical command wrapped in ``ssh_command``
    for a remote one — checking the local tmux server for a remote target's
    copy-mode state would report on the wrong tmux server entirely.

Post-send verification uses transcript-grep against the target session's
own ``~/.claude/projects/*/*.jsonl`` transcript (found by recency + content
match, explicitly excluding the caller's own transcript), replacing the
weaker pane-capture confirmation: a spinner or status line is not evidence
that a message was actually received; the transcript is. For a remote
target, recent candidate paths and their tails are read over ssh against the
**target host's** filesystem (``find_recent_transcript_remote``), then
compared locally — the transcript proving a remote delivery lives on the
remote host, never on the machine chitra runs on.

Single-writer rule
-----------------

``LaneLock`` enforces one writer per session id. ``dispatchd`` acquires a
lock for the order's session id before any delivery attempt and releases it
after. Acquiring a lock for an already-locked session id fails rather than
silently proceeding.

Directive-voice guard
----------------------

``directive_voice_violation`` is a pure regex predicate checked at the top
of ``dispatch_to_tmux``, before the pre-dispatch pane check and before
anything is pasted. Chitra relays instructions; it never speaks as the
operator or claims the operator's authority. A nudge that attributes itself
to "the operator" / "the monitor", or has chitra claim in its own voice to
want/say/need/relay something, is rejected outright: ``dispatch_to_tmux``
returns ``DispatchResult(status=BLOCKED, reason="directive-voice: ...")``
with nothing pasted and no delivery-ledger entry (``dispatchd`` only signs
the ledger on ``SENT`` — see ``dispatchd.process_one_order``).

Completion-claim auditing
--------------------------

``DispatchOrder`` carries citation-bearing completion evidence consumed by
``chitra.completion_gate``. Bare booleans are not evidence and no longer form
part of the order contract.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog

from chitra.orders import DispatchOrder, DispatchResult, DispatchStatus
from chitra.policy_config import PolicyConfig

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants (mirrors of the source)
# ---------------------------------------------------------------------------

DISPATCH_CAPTURE_LINES: int = 12
# Claude Code writes the submitted user turn to its JSONL transcript
# asynchronously. A live production delivery took about 13 seconds to appear,
# so the former one-second allowance produced a FAILED result after the nudge
# had actually been accepted. Keep the wait bounded while covering that
# observed flush delay before declaring a delivery unverified.
DISPATCH_VERIFY_WAIT_SECONDS: float = 15.0
PANE_IN_MODE_CANCEL_WAIT_SECONDS: float = 0.3
DEFAULT_REMOTE_HOSTS: str = ""

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_IDLE_INPUT_LINE_RE = re.compile(
    r"^\s*(?:(?:\([^)]+\)|\[[^\]]+\])\s*)?"
    r"(?:[\w.-]+@[\w.-]+(?::[^$#%>]*)?)?\s*"
    r"(?:[$#%>]|>>>|\.\.\.|In \[\d+\]:)\s*$"
)
_CLAUDE_CODE_HORIZONTAL_RULE_RE = re.compile(r"^─+$")
_CLAUDE_CODE_INPUT_ROW_RE = re.compile(r"^❯(?P<draft>.*)$")
_CODEX_TUI_INPUT_ROW_RE = re.compile(r"^›(?P<draft>.*)$")
_CODEX_TUI_PLACEHOLDER_HINTS: frozenset[str] = frozenset(
    {
        "Explain this codebase",
        "Summarize recent commits",
        "Implement {feature}",
        "Find and fix a bug in @filename",
        "Write tests for @filename",
        "Improve documentation in @filename",
        "Run /review on my current changes",
        "Use /skills to list available skills",
        "Check recently modified functions for compatibility",
        "How many files have been modified?",
        "Will this algorithm scale well?",
        "Ask Codex to do anything",
    }
)
# SGR (Select Graphic Rendition) escape — the subset of ANSI escapes that
# carries intensity styling. Used to tell a dim/faint placeholder hint (SGR 2)
# apart from a normal-intensity operator draft on a TUI input row.
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")

# Active-turn chrome: the same "esc to interrupt" spinner text
# ``chitra.agent_detection``'s codex.toml/claude.toml rules and
# ``chitra.watchd``'s idle-line filter already key off. Its presence means a
# turn is currently running; a submit fallback must never fire an ESC-shaped
# byte sequence into a pane in that state, or it risks cancelling live work
# instead of submitting a stale, unrelated composer row.
_ACTIVE_TURN_CHROME_RE = re.compile(r"esc to interrupt")

# The kitty keyboard protocol's CSI-u encoding of a plain Enter keypress.
# Codex's TUI composer (kitty protocol enabled) does not treat a bare
# ``tmux send-keys Enter`` as a submit -- see ``_send_submit_fallback``.
_CODEX_KITTY_ENTER_SEQUENCE = "\x1b[13u"

# Directive-voice guard: chitra relays instructions, it never speaks AS the
# operator or claims the operator's authority. A nudge that attributes itself
# to "the operator" or "the monitor", or has chitra claim to want/say/need/
# relay something in its own voice, is a directive-voice violation.
_BANNED = re.compile(r"\boperator\b|\bthe monitor\b|\bchitra (wants|says|needs|relays)\b", re.I)
_TRANSCRIPT_GLOB_DEFAULT = "*/*.jsonl"


def enqueue_dispatch_order(queue_dir: Path, order: DispatchOrder) -> Path:
    """Atomically enqueue one order without replacing another producer's payload."""
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True, exist_ok=True)
    path = orders_dir / f"{order.order_id}.json"
    payload = order.model_dump_json().encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{order.order_id}.",
            suffix=".tmp",
            dir=orders_dir,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            if path.is_symlink():
                raise FileExistsError(path) from None
            try:
                existing = DispatchOrder.model_validate_json(path.read_bytes())
            except (OSError, ValueError):
                raise FileExistsError(path) from None
            if existing != order:
                raise FileExistsError(path) from None
        else:
            directory_fd = os.open(orders_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path


# ---------------------------------------------------------------------------
# PaneInputCheck (mirror of the source dataclass)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaneInputCheck:
    """Result of checking whether a pane is safe to dispatch into.

    ``ok`` is True only when the pane is idle (matches a known idle hash or
    shows a bare prompt line with no draft). A pane with an unsubmitted
    operator draft is ``ok=False`` so dispatch is blocked — never silently
    overwrite an operator's pending input.
    """

    ok: bool
    reason: str
    tail_hash: str
    last_line: str


@dataclass(frozen=True, slots=True)
class DispatchTuning:
    """Dispatch reliability bounds, carried together through the daemon."""

    capture_lines: int = DISPATCH_CAPTURE_LINES
    post_paste_wait_seconds: float = DISPATCH_VERIFY_WAIT_SECONDS
    transcript_recency_seconds: float = 300.0
    lane_lock_timeout_seconds: float = 5.0


# ---------------------------------------------------------------------------
# Tmux command runner protocol (for test injection)
# ---------------------------------------------------------------------------


class TmuxRunner(Protocol):
    """Callable that runs a command and returns a CompletedProcess[str]."""

    def __call__(self, cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]: ...


class TmuxInputRunner(Protocol):
    """Callable that runs a command with stdin payload."""

    def __call__(self, cmd: list[str], payload: str, *, timeout: int = 20) -> subprocess.CompletedProcess[str]: ...


def _with_tmux_socket(argv: Sequence[str], tmux_socket: Path | None) -> list[str]:
    """Select one lane's tmux server when a lane supplies a socket path."""
    command = list(argv)
    if tmux_socket is None or not command or command[0] != "tmux":
        return command
    return [command[0], "-S", str(tmux_socket), *command[1:]]


def run_cmd(cmd: list[str], payload: str | None = None, *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    """Run a command with optional stdin, capturing output without raising.

    ``FileNotFoundError`` (binary missing) returns rc=127;
    ``TimeoutExpired`` returns rc=124.
    """
    try:
        return subprocess.run(
            cmd,
            input=payload,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=stderr or f"timed out after {timeout}s")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))


def _pid_alive(pid: int) -> bool:
    """Return whether ``pid`` names a process that still exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Host allowlist + local-host detection
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def allowed_remote_dispatch_hosts(env: str | None = None) -> set[str]:
    """Return the set of remote hosts dispatch may target.

    Parameterized: reads ``REMOTE_DISPATCH_HOSTS`` (or the supplied ``env``
    string) and splits on commas. Defaults to no remote hosts allowed —
    deployments opt in to specific remote host names via the env var or by
    passing ``allowed_hosts`` directly to ``dispatch_to_tmux``.
    """
    raw = env if env is not None else _env("REMOTE_DISPATCH_HOSTS", DEFAULT_REMOTE_HOSTS)
    return {item.strip() for item in raw.split(",") if item.strip()}


def local_host_aliases(extra: set[str] | None = None) -> set[str]:
    """Return the set of aliases that refer to the local host.

    Includes the short hostname, fqdn, ``localhost``, and ``127.0.0.1``.
    Tests inject ``extra`` to pin the local identity.
    """
    aliases: set[str] = {"localhost", "127.0.0.1"}
    try:
        aliases.add(socket.gethostname().split(".", 1)[0])
        aliases.add(socket.getfqdn().split(".", 1)[0])
    except OSError:
        pass
    override = _env("CHITRA_LOCAL_HOST")
    if override:
        aliases.add(override.split(".", 1)[0])
    if extra:
        aliases |= extra
    return aliases


def is_local_host(host: str, extra: set[str] | None = None) -> bool:
    """Return True if ``host`` refers to the local machine."""
    return host.split(".", 1)[0] in local_host_aliases(extra)


def tmux_pane_target(session: str, pane: str) -> str:
    """Build a fully-qualified tmux target from a ``session_ref``'s session
    and pane components.

    A bare pane spec like ``"0.0"`` resolves against tmux's CURRENT session
    when passed alone to ``-t`` — on any host running more than one tmux
    session (this package's entire intended deployment shape), that silently
    targets the wrong session. Qualify with the session name unless the pane
    is already fully-qualified (contains ``:``) or is a globally-unique tmux
    pane id (``%N``, valid on its own regardless of session).
    """
    if not pane or ":" in pane or pane.startswith("%"):
        return pane
    return f"{session}:{pane}"


def governed_capture_target(pane: str) -> str:
    """Translate tmux's ``session:window.pane`` into the grant contract.

    The forced-command visibility surface deliberately accepts only colon
    delimited numeric target components.  Keep native tmux spelling everywhere
    else and normalize only at the governed SSH boundary.
    """
    match = re.fullmatch(r"(?P<session>[A-Za-z0-9_.-]+):(?P<window>[0-9]{1,3})\.(?P<pane>[0-9]{1,3})", pane)
    if match is None:
        return pane
    return f"{match.group('session')}:{match.group('window')}:{match.group('pane')}"


# ---------------------------------------------------------------------------
# Text normalization helpers (mirrors of the source)
# ---------------------------------------------------------------------------


def directive_voice_violation(nudge: str, *, patterns: Sequence[re.Pattern[str]] | None = None) -> str | None:
    """Return the banned attribution phrase found in ``nudge``, or ``None``.

    Pure regex predicate: chitra relays instructions, it never speaks as
    the operator or claims the operator's authority. Matches a bare
    ``operator`` token, ``the monitor``, or chitra claiming to
    want/say/need/relay something in its own voice. Case-insensitive.
    """
    if patterns is None:
        m = _BANNED.search(nudge)
        return m.group(0) if m else None
    for pattern in patterns:
        m = pattern.search(nudge)
        if m:
            return m.group(0)
    return None


def strip_terminal_controls(text: str) -> str:
    """Strip ANSI escape sequences and surrounding whitespace."""
    return _ANSI_ESCAPE_RE.sub("", text).strip()


def _claude_code_input_row(captured_lines: list[str]) -> tuple[str, str, str] | None:
    """Return a Claude Code input row when its TUI shape matches.

    Returns ``(stripped_line, stripped_draft, raw_line)``: the row with
    terminal controls stripped (for display/matching), the captured draft
    text, and the original escape-preserving line (so callers can inspect the
    ANSI styling that distinguishes a dim placeholder hint from a real draft).
    """
    raw = [str(line) for line in captured_lines]
    lines = [strip_terminal_controls(line) for line in raw]
    for index in range(1, len(lines) - 1):
        if not (
            _CLAUDE_CODE_HORIZONTAL_RULE_RE.fullmatch(lines[index - 1]) and _CLAUDE_CODE_HORIZONTAL_RULE_RE.fullmatch(lines[index + 1])
        ):
            continue
        match = _CLAUDE_CODE_INPUT_ROW_RE.fullmatch(lines[index])
        if match:
            return lines[index], match.group("draft"), raw[index]
    return None


def _codex_tui_input_row(captured_lines: list[str]) -> tuple[str, str, str] | None:
    """Return the last Codex TUI composer row in a pane capture.

    Codex renders its composer with a ``›`` marker. Returns the same
    ``(stripped_line, stripped_draft, raw_line)`` shape as
    :func:`_claude_code_input_row` so the caller can distinguish dim rotating
    suggestion text from normal-intensity operator input.
    """
    raw = [str(line) for line in captured_lines]
    lines = [strip_terminal_controls(line) for line in raw]
    for index in range(len(lines) - 1, -1, -1):
        match = _CODEX_TUI_INPUT_ROW_RE.fullmatch(lines[index])
        if match:
            return lines[index], match.group("draft"), raw[index]
    return None


def _input_row_draft_is_all_dim(raw_line: str, *, prompt_marker: str) -> bool:
    """Return True iff every visible char after ``prompt_marker`` is dim.

    Claude Code and Codex paint idle placeholder hints in faint/dim text (ANSI
    SGR 2), whereas a real unsubmitted operator draft is normal intensity. A
    plain ``tmux capture-pane`` (no ``-e``) strips color, so the two are
    indistinguishable by text alone; this reads the escape-aware capture
    instead.

    Fails closed: returns False when there is no faint styling at all (a
    normal-intensity draft, or a capture with no escape sequences), so an
    ambiguous row is treated as a real draft and dispatch stays blocked.
    """
    faint = False
    seen_prompt = False
    saw_visible = False
    i = 0
    n = len(raw_line)
    while i < n:
        sgr = _SGR_RE.match(raw_line, i)
        if sgr:
            params = sgr.group(1)
            for code in params.split(";") if params else [""]:
                if code in ("", "0", "22"):
                    faint = False
                elif code == "2":
                    faint = True
            i = sgr.end()
            continue
        esc = _ANSI_ESCAPE_RE.match(raw_line, i)
        if esc:
            i = esc.end()
            continue
        ch = raw_line[i]
        i += 1
        if not seen_prompt:
            if ch == prompt_marker:
                seen_prompt = True
            continue
        if ch.isspace():
            continue
        saw_visible = True
        if not faint:
            return False
    return saw_visible


def _detect_tui_backend(captured_lines: list[str]) -> str:
    """Return ``"codex"``, ``"claude"``, or ``"unknown"`` for one pane capture.

    Reuses the same composer-row parsers ``pane_input_check`` already relies
    on to distinguish the two TUI shapes, so backend detection never drifts
    from idle/draft detection.
    """
    if _codex_tui_input_row(captured_lines) is not None:
        return "codex"
    if _claude_code_input_row(captured_lines) is not None:
        return "claude"
    return "unknown"


def _active_turn_chrome_visible(captured_lines: list[str]) -> bool:
    """Return True iff the pane shows a running turn ("esc to interrupt")."""
    text = strip_terminal_controls("\n".join(str(line) for line in captured_lines))
    return bool(_ACTIVE_TURN_CHROME_RE.search(text))


def normalized_dispatch_text(text: str) -> str:
    """Collapse whitespace and strip terminal controls for comparison."""
    return re.sub(r"\s+", " ", strip_terminal_controls(text)).strip()


def nudge_confirmation_marker(nudge: str) -> str:
    """Return a short, normalized marker line for a nudge.

    Picks the first line that normalizes to >=8 chars, else the whole
    normalized nudge. Truncated to 160 chars (mirrors the source).
    """
    for line in nudge.splitlines():
        marker = normalized_dispatch_text(line)
        if len(marker) >= 8:
            return marker[:160]
    return normalized_dispatch_text(nudge)[:160]


def _composer_holds_marker(captured_lines: list[str], marker: str) -> bool:
    """Return True iff ``marker`` is still sitting, unsubmitted, in the pane's
    active TUI composer row.

    Shared by the post-Enter submit check (fix for Codex's kitty-keyboard
    composer not submitting on a bare ``send-keys Enter``) and
    ``pane_capture_confirms_nudge``'s existing composer-vs-scrollback split.
    """
    if not marker:
        return False
    for row_parser in (_codex_tui_input_row, _claude_code_input_row):
        input_row = row_parser(captured_lines)
        if input_row is not None:
            _input_line, draft, _raw_line = input_row
            if marker in normalized_dispatch_text(draft):
                return True
    return False


def tmux_buffer_name(nudge: str) -> str:
    """Stable buffer name derived from the nudge text hash."""
    return f"chitra-nudge-{hashlib.sha256(nudge.encode('utf-8', errors='replace')).hexdigest()[:12]}"


def pane_capture_tail_hash(lines: list[str]) -> str:
    """SHA-256 of the joined pane capture, or empty string if no lines."""
    text = "\n".join(str(line) for line in lines)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest() if text else ""


def pane_input_check(
    captured_lines: list[str],
    *,
    baseline_hash: str | None = None,
    snapshot_hash: str | None = None,
    seen_hash: str | None = None,
    extra_idle_regexes: Sequence[re.Pattern[str]] = (),
) -> PaneInputCheck:
    """Check whether a pane is idle and safe to dispatch into.

    Returns ``ok=True`` when the tail hash matches a known idle hash, the
    last line is a bare shell prompt, or a recognized Claude Code/Codex TUI
    input row is empty or shows only a dim placeholder hint (a fresh session's
    ghost text, ANSI SGR 2). Returns ``ok=False`` with a ``blocked:`` reason
    otherwise — never silently overwrite a real, normal-intensity draft.
    """
    current_hash = pane_capture_tail_hash(captured_lines)
    if not captured_lines or not current_hash:
        return PaneInputCheck(False, "blocked: unable to verify pane input is idle", current_hash, "")
    known_idle_hashes = {str(item).strip() for item in (baseline_hash, snapshot_hash, seen_hash) if str(item or "").strip()}
    if current_hash in known_idle_hashes:
        return PaneInputCheck(True, "idle: pane capture matches known idle baseline", current_hash, "")
    claude_code_input = _claude_code_input_row(captured_lines)
    if claude_code_input is not None:
        input_line, draft, raw_line = claude_code_input
        if not draft.strip():
            return PaneInputCheck(True, "idle: Claude Code TUI input row has no draft input", current_hash, input_line)
        if _input_row_draft_is_all_dim(raw_line, prompt_marker="❯"):
            return PaneInputCheck(True, "idle: Claude Code TUI input row shows only a dim placeholder hint", current_hash, input_line)
        return PaneInputCheck(False, "blocked: unsubmitted operator draft detected", current_hash, input_line)
    codex_tui_input = _codex_tui_input_row(captured_lines)
    if codex_tui_input is not None:
        input_line, draft, raw_line = codex_tui_input
        normalized_draft = draft.strip()
        if not normalized_draft:
            return PaneInputCheck(True, "idle: Codex TUI input row has no draft input", current_hash, input_line)
        # Either condition alone is sufficient evidence of a placeholder, not a real draft:
        # an exact match against a known hint (a real draft identical to a rotating hint
        # text is not plausible content worth protecting) or an all-dim row (sufficient even
        # when the hint text is new to the list, since Codex adds hints across releases).
        if normalized_draft in _CODEX_TUI_PLACEHOLDER_HINTS or _input_row_draft_is_all_dim(raw_line, prompt_marker="›"):
            return PaneInputCheck(True, "idle: Codex TUI input row shows only a placeholder hint", current_hash, input_line)
        return PaneInputCheck(False, "blocked: unsubmitted operator draft detected", current_hash, input_line)
    last_line = strip_terminal_controls(str(captured_lines[-1]))
    if _IDLE_INPUT_LINE_RE.match(last_line):
        return PaneInputCheck(True, "idle: prompt line has no draft input", current_hash, last_line)
    if any(pattern.match(last_line) for pattern in extra_idle_regexes):
        return PaneInputCheck(True, "idle: matched configured idle pattern", current_hash, last_line)
    return PaneInputCheck(False, "blocked: unsubmitted operator draft detected", current_hash, last_line)


# ---------------------------------------------------------------------------
# Pane capture
# ---------------------------------------------------------------------------


def capture(
    host: str,
    pane_id: str,
    lines: int,
    *,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
) -> list[str]:
    """Capture a local or remote tmux pane through ``run_on_host``."""
    if host and not is_local_host(host, local_extra) and _env("CHITRA_REMOTE_LANE_GRANT") == "codexman":
        grant_target = governed_capture_target(pane_id)
        proc = (runner or run_cmd)(ssh_command(host, f"chitra-tmux-capture {shlex.quote(grant_target)}"), timeout=8)
        if proc.returncode != 0:
            return []
        try:
            document = json.loads(proc.stdout)
        except (TypeError, json.JSONDecodeError):
            return []
        if document.get("ok") is not True or not isinstance(document.get("content"), str):
            return []
        captured = [line.rstrip() for line in document["content"].splitlines() if line.strip()]
        return captured if lines < 0 else captured[-lines:]
    start = "-" if lines < 0 else f"-{lines}"
    proc = run_on_host(
        host,
        ["tmux", "capture-pane", "-e", "-p", "-t", pane_id, "-S", start],
        runner=runner,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
        timeout=8,
    )
    if proc.returncode != 0:
        return []
    captured = [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]
    return captured if lines < 0 else captured[-lines:]


def ssh_command(target: str, remote_command: str) -> list[str]:
    """Build a BatchMode ssh command (mirrors the source, parameterized)."""
    strict_host_key_checking = _env("CHITRA_SSH_STRICT_HOST_KEY_CHECKING", "accept-new")
    timeout_raw = _env("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS", "4")
    try:
        connect_timeout = int(timeout_raw)
    except ValueError as exc:
        raise ValueError("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS must be a positive integer") from exc
    if connect_timeout <= 0:
        raise ValueError("CHITRA_SSH_CONNECT_TIMEOUT_SECONDS must be a positive integer")
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"StrictHostKeyChecking={strict_host_key_checking}",
        "-o",
        f"ConnectTimeout={connect_timeout}",
    ]
    config = _env("CHITRA_SSH_CONFIG")
    if config:
        cmd.extend(["-F", config])
    identity = _env("CHITRA_SSH_IDENTITY")
    if identity:
        cmd.extend(["-i", identity, "-o", "IdentitiesOnly=yes"])
    known_hosts = _env("CHITRA_SSH_KNOWN_HOSTS")
    if known_hosts:
        cmd.extend(["-o", f"UserKnownHostsFile={known_hosts}"])
    cmd.extend([target, remote_command])
    run_as = _env("CHITRA_SSH_RUN_AS")
    if run_as:
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", run_as):
            raise ValueError("CHITRA_SSH_RUN_AS must be a valid local account name")
        cmd = ["sudo", "-n", "-u", run_as, "--", *cmd]
    return cmd


def run_on_host(
    host: str,
    argv: Sequence[str],
    *,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
    timeout: int = 20,
) -> subprocess.CompletedProcess[str]:
    """Run one argv locally or as a safely quoted command over ssh."""
    command = _with_tmux_socket(argv, tmux_socket)
    if host and not is_local_host(host, local_extra):
        command = ssh_command(host, shlex.join(command))
    return (runner or run_cmd)(command, timeout=timeout)


def capture_dispatch_pane(
    host: str,
    pane: str,
    *,
    lines: int = DISPATCH_CAPTURE_LINES,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
) -> list[str]:
    """Capture a dispatch pane, locally or remotely.

    Local-host detection uses ``is_local_host`` with the supplied
    ``local_extra`` aliases (for tests).
    """
    return capture(host, pane, lines, runner=runner, local_extra=local_extra, tmux_socket=tmux_socket)


# ---------------------------------------------------------------------------
# Copy-mode detection + cancel (BUG FIX (b))
# ---------------------------------------------------------------------------


def pane_in_mode(
    pane: str,
    *,
    host: str = "",
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
) -> bool:
    """Return True if the target pane is in tmux copy-mode (pane_in_mode=1).

    A pane in copy-mode silently eats all input — dispatching into it
    destroys the nudge. This is bug fix (b): the source has no such check.

    ``host`` selects which tmux server is checked: the default (``""``,
    treated as local) or any host that ``is_local_host`` recognizes as this
    machine runs the check via a plain local ``tmux`` invocation; any other
    host runs the identical check over ssh via ``ssh_command``, mirroring
    ``capture_dispatch_pane``'s local/remote split. Checking the local tmux
    server for a remote target's copy-mode state is meaningless — it reports
    on the wrong tmux server entirely.
    """
    proc = run_on_host(
        host,
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_in_mode}"],
        runner=runner,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
        timeout=5,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "1"


def cancel_copy_mode(
    pane: str,
    *,
    host: str = "",
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
    wait_seconds: float = PANE_IN_MODE_CANCEL_WAIT_SECONDS,
) -> bool:
    """Cancel tmux copy-mode on a pane and wait briefly.

    Returns True if a cancel command was issued. The caller should wait
    ``wait_seconds`` (default 0.3s) before injecting. ``host`` selects local
    vs ssh-wrapped execution, exactly like ``pane_in_mode``.
    """
    proc = run_on_host(
        host,
        ["tmux", "send-keys", "-t", pane, "-X", "cancel"],
        runner=runner,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
        timeout=5,
    )
    if proc.returncode != 0:
        logger.warning("cancel_copy_mode_failed", pane=pane, host=host, stderr=proc.stderr.strip())
        return False
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    return True


def ensure_pane_not_in_mode(
    pane: str,
    *,
    host: str = "",
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
) -> bool:
    """Ensure a pane is not in copy-mode; cancel if it is.

    Returns True if the pane is dispatch-ready (was never in copy-mode, or
    was and is now cancelled). Returns False if copy-mode was detected and
    could not be cancelled. ``host`` is forwarded to ``pane_in_mode`` /
    ``cancel_copy_mode`` so the check runs against the actual target host
    (local or ssh-wrapped) rather than always the local tmux server.
    """
    if not pane_in_mode(pane, host=host, runner=runner, local_extra=local_extra, tmux_socket=tmux_socket):
        return True
    return cancel_copy_mode(pane, host=host, runner=runner, local_extra=local_extra, tmux_socket=tmux_socket)


# ---------------------------------------------------------------------------
# Paste commands (BUG FIX (a): -p on paste-buffer)
# ---------------------------------------------------------------------------


def paste_nudge_to_local_tmux(
    pane: str,
    nudge: str,
    *,
    runner: TmuxRunner | None = None,
    input_runner: TmuxInputRunner | None = None,
    tmux_socket: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Inject a nudge into a local tmux pane using the verified recipe.

    Steps: ``load-buffer`` from stdin, ``paste-buffer -p`` (the ``-p`` is
    mandatory — bracketed-paste wrapper so newlines don't act as Enters),
    ``delete-buffer``, then ``send-keys Enter``.

    This is bug fix (a): the source omits ``-p`` on ``paste-buffer``.
    """
    run = runner or run_cmd
    run_in = input_runner or run_cmd
    buffer_name = tmux_buffer_name(nudge)
    load = run_in(_with_tmux_socket(["tmux", "load-buffer", "-b", buffer_name, "-"], tmux_socket), nudge, timeout=5)
    if load.returncode != 0:
        return load
    # NOTE: -p is mandatory here. Without it, newlines act as real Enters.
    paste = run(_with_tmux_socket(["tmux", "paste-buffer", "-p", "-b", buffer_name, "-t", pane], tmux_socket), timeout=5)
    if paste.returncode != 0:
        return paste
    # Buffer cleanup is housekeeping, not the critical step -- a failure here
    # must never block send-keys Enter, or a successfully pasted nudge is
    # left uncommitted in the pane (an orphaned draft, exactly the failure
    # mode this package's own draft_scanner exists to catch, caused here by
    # the dispatch path itself). Log and proceed regardless of cleanup result.
    cleanup = run(_with_tmux_socket(["tmux", "delete-buffer", "-b", buffer_name], tmux_socket), timeout=5)
    if cleanup.returncode != 0:
        logger.warning("tmux_buffer_cleanup_failed", pane=pane, buffer_name=buffer_name, stderr=cleanup.stderr.strip())
    return run(_with_tmux_socket(["tmux", "send-keys", "-t", pane, "Enter"], tmux_socket), timeout=5)


def remote_tmux_paste_command(pane: str, nudge: str, tmux_socket: Path | None = None) -> str:
    """Build the remote paste command string (ssh-safe, single shell line).

    Includes ``-p`` on ``paste-buffer`` (bug fix (a)). The command is a
    single shell string suitable for ``ssh target '<command>'``.
    """
    buffer_name = tmux_buffer_name(nudge)
    tmux = ["tmux"] if tmux_socket is None else ["tmux", "-S", shlex.quote(str(tmux_socket))]
    return " ".join(
        [
            "printf",
            "%s",
            shlex.quote(nudge),
            "|",
            *tmux,
            "load-buffer",
            "-b",
            shlex.quote(buffer_name),
            "-",
            "&&",
            *tmux,
            "paste-buffer",
            "-p",
            "-b",
            shlex.quote(buffer_name),
            "-t",
            shlex.quote(pane),
            "&&",
            *tmux,
            "delete-buffer",
            "-b",
            shlex.quote(buffer_name),
            "&&",
            *tmux,
            "send-keys",
            "-t",
            shlex.quote(pane),
            "Enter",
        ]
    )


# ---------------------------------------------------------------------------
# Verified submit (bug fix (c)): a bare ``send-keys Enter`` does not commit
# Codex's kitty-keyboard-protocol composer -- the pasted text can sit there
# looking delivered while no turn ever starts. After the initial Enter,
# re-capture the pane; if the nudge is still visible in the active composer
# row, fire one backend-appropriate submit fallback (unless a turn is
# already running, in which case the composer text is stale/unrelated and
# must not be touched), then re-verify the composer actually cleared.
# ---------------------------------------------------------------------------


def _send_submit_fallback(
    host: str,
    pane: str,
    session: str,
    backend: str,
    *,
    governed_remote: bool,
    runner: TmuxRunner,
    input_runner: TmuxInputRunner,
    local_extra: set[str] | None,
    tmux_socket: Path | None,
) -> bool:
    """Send one backend-appropriate submit fallback. Returns True iff the
    fallback command(s) ran without error (not proof of a cleared composer --
    the caller re-captures to verify that separately).
    """
    if governed_remote:
        # The governed grant's forced-command surface exposes only
        # ``chitra-tmux-capture`` (read) and ``chitra-lane-steer`` (write) --
        # no raw ``tmux send-keys`` verb. Reuse the same steer transport the
        # original paste went through to deliver the kitty-Enter bytes; every
        # governed lane is a Codex job (codexman), so the codex fallback
        # always applies here.
        proc = input_runner(ssh_command(host, f"chitra-lane-steer {shlex.quote(session)}"), _CODEX_KITTY_ENTER_SEQUENCE, timeout=10)
        return proc.returncode == 0
    if backend == "codex":
        proc = run_on_host(
            host,
            ["tmux", "send-keys", "-t", pane, "-l", _CODEX_KITTY_ENTER_SEQUENCE],
            runner=runner,
            local_extra=local_extra,
            tmux_socket=tmux_socket,
            timeout=5,
        )
        return proc.returncode == 0
    # Claude Code (or an unrecognized composer shape): a bare second Enter
    # can be swallowed for the same reason the first one was, so send a
    # minimal non-empty payload before the follow-up Enter.
    space = run_on_host(
        host,
        ["tmux", "send-keys", "-t", pane, "-l", " "],
        runner=runner,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
        timeout=5,
    )
    if space.returncode != 0:
        return False
    enter = run_on_host(
        host,
        ["tmux", "send-keys", "-t", pane, "Enter"],
        runner=runner,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
        timeout=5,
    )
    return enter.returncode == 0


def ensure_nudge_submitted(
    host: str,
    pane: str,
    session: str,
    marker: str,
    *,
    governed_remote: bool,
    runner: TmuxRunner,
    input_runner: TmuxInputRunner,
    local_extra: set[str] | None,
    tmux_socket: Path | None,
    lines: int = DISPATCH_CAPTURE_LINES,
) -> tuple[bool, str]:
    """Verify a just-pasted nudge actually submitted; fall back once if not.

    Returns ``(ok, detail)``. ``ok=False`` means the composer still holds the
    nudge after the fallback attempt -- the caller must report FAILED with
    ``detail`` as the reason, never SENT.
    """
    captured = capture_dispatch_pane(host, pane, lines=lines, runner=runner, local_extra=local_extra, tmux_socket=tmux_socket)
    if not captured or not _composer_holds_marker(captured, marker):
        return True, "submitted: composer cleared after Enter"
    if _active_turn_chrome_visible(captured):
        # A turn is already running: whatever text is sitting in the
        # composer is not this delivery waiting to be submitted (or it is a
        # genuine race), and an ESC-shaped fallback byte into a live turn
        # cancels it rather than submitting stale text. Never send it.
        return True, "submitted: active-turn chrome visible, skipped fallback to avoid interrupting a running turn"
    backend = _detect_tui_backend(captured)
    if not _send_submit_fallback(
        host,
        pane,
        session,
        backend,
        governed_remote=governed_remote,
        runner=runner,
        input_runner=input_runner,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
    ):
        return False, "submit-failed-composer-still-holds-text (fallback command failed)"
    recaptured = capture_dispatch_pane(host, pane, lines=lines, runner=runner, local_extra=local_extra, tmux_socket=tmux_socket)
    if recaptured and _composer_holds_marker(recaptured, marker):
        return False, "submit-failed-composer-still-holds-text"
    return True, f"submitted: composer cleared after {backend} submit fallback"


# ---------------------------------------------------------------------------
# Transcript-grep verification (replaces pane-capture confirmation)
# ---------------------------------------------------------------------------


def _candidate_transcript_dirs(projects_root: Path | None = None) -> list[Path]:
    """Return candidate ``~/.claude/projects/*`` transcript directories."""
    root = projects_root if projects_root is not None else Path(_env("CHITRA_CLAUDE_PROJECTS", str(Path.home() / ".claude" / "projects")))
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.is_dir()]


def transcript_glob() -> str:
    """Return the configured relative transcript glob, validating its scope."""
    pattern = _env("CHITRA_TRANSCRIPT_GLOB", _TRANSCRIPT_GLOB_DEFAULT) or _TRANSCRIPT_GLOB_DEFAULT
    path = Path(pattern)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("CHITRA_TRANSCRIPT_GLOB must be relative and must not contain '..'")
    return pattern


def _read_transcript_tail(path: Path, max_bytes: int = 262144) -> str:
    """Read the tail of a JSONL transcript file (last ``max_bytes`` bytes)."""
    try:
        size = path.stat().st_size
        offset = max(0, size - max_bytes)
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read()
    except OSError:
        return ""


def transcript_mtime(
    transcript_path: str,
    *,
    host: str,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
) -> float | None:
    """Return a transcript file's last-modified epoch, local or over ssh.

    Used by ``chitra.rate_limit_guard`` to verify a checkpointed session's
    turn actually stopped writing (no growth for a bounded quiet window)
    before recording a graceful hold -- see docs/SOL-ADVERSARIAL-REVIEW
    finding #2. Returns ``None`` if the path cannot be stat'd (missing file,
    unreachable host); a caller treats that as "cannot verify", never as
    "stopped".
    """
    proc = run_on_host(
        host,
        ["stat", "-c", "%Y", transcript_path],
        runner=runner,
        local_extra=local_extra,
        timeout=8,
    )
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _resolve_local_projects_roots(projects_root: Path | None) -> list[Path]:
    """Resolve the local transcript search root(s) for ``find_recent_transcript``.

    An explicit ``projects_root`` (existing single-root callers/tests) always
    wins and is used alone. Otherwise ``CHITRA_CLAUDE_PROJECTS`` may list more
    than one root separated by ``os.pathsep`` -- a local Claude Code session
    running under a non-default ``CLAUDE_CONFIG_DIR`` (e.g. a dedicated
    persona/harness identity such as chitra's own monitor role) writes its
    transcripts under THAT root's ``projects/``, not the default
    ``~/.claude/projects``. Searching only the default root means
    transcript-grep can never confirm a genuine delivery to such a session --
    it always falls through to the weaker pane-capture fallback, or FAILED.
    """
    if projects_root is not None:
        return [projects_root]
    raw = _env("CHITRA_CLAUDE_PROJECTS", str(Path.home() / ".claude" / "projects"))
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


_CONSUMPTION_EVENT_ROLES = frozenset(
    {"assistant", "agent", "agent_message", "tool", "tool_use", "tool_result", "function_call", "function_call_output"}
)
_TRANSCRIPT_RECORD_ROLES = _CONSUMPTION_EVENT_ROLES | {"user", "system", "progress"}


def _record_role(payload: object) -> str | None:
    """Return one parsed JSONL transcript record's semantic role.

    Claude Code uses top-level user/assistant types. Codex wraps messages and
    tool events in ``payload`` under generic ``response_item``/``event_msg``
    envelope types, so inner explicit roles and event kinds take precedence.
    Generic envelope names never count as activity.
    """
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    message_role = message.get("role") if isinstance(message, dict) else None
    nested = payload.get("payload")
    nested_message = nested.get("message") if isinstance(nested, dict) else None
    nested_message_role = nested_message.get("role") if isinstance(nested_message, dict) else None
    for candidate in (
        payload.get("role"),
        message_role,
        nested.get("role") if isinstance(nested, dict) else None,
        nested_message_role,
        nested.get("type") if isinstance(nested, dict) else None,
        payload.get("type"),
    ):
        if isinstance(candidate, str) and candidate in _TRANSCRIPT_RECORD_ROLES:
            return candidate
    return None


def _parse_transcript_records(text: str) -> list[tuple[str | None, str]]:
    """Parse JSONL text into ``(role, normalized_text)`` pairs.

    Lines that are not valid JSON (a torn write mid-flush, or non-JSONL
    scrollback noise) are skipped rather than raising -- matching
    ``chitra.lane_read.read_last_assistant_message``'s tolerance for a
    partially-flushed transcript tail.
    """
    records: list[tuple[str | None, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = _record_role(payload)
        normalized = normalized_dispatch_text("\n".join(_json_string_values(payload)))
        records.append((role, normalized))
    return records


def _structural_transcript_confirms(text: str, marker_norm: str) -> bool:
    """Return True iff ``text`` (a JSONL transcript tail) structurally
    confirms delivery of ``marker_norm`` in one lane transcript.

    A plain substring match over raw JSONL bytes is fooled by an assistant
    reply that merely echoes the nudge text back, or by the marker sitting
    in scrollback with no turn ever having started. Confirmation instead
    requires: (1) the normalized marker appears in a record whose role is
    ``"user"`` -- chitra's tmux paste is persisted by Claude Code/Codex
    exactly like real operator input, as a user-role record -- AND (2) at
    least one later agent or tool record showing the turn actually started
    (see ``_CONSUMPTION_EVENT_ROLES``). A user record with the marker but no
    follow-up, or only a generic system/progress record, is not confirmation.
    Both records must occur in the same transcript, so activity from another
    lane cannot satisfy this lane's delivery proof.
    """
    if not marker_norm:
        return False
    records = _parse_transcript_records(text)
    last_user_index: int | None = None
    for index, (role, normalized) in enumerate(records):
        if role == "user" and marker_norm in normalized:
            last_user_index = index
    if last_user_index is None:
        return False
    return any(role in _CONSUMPTION_EVENT_ROLES for role, _normalized in records[last_user_index + 1 :])


def find_recent_transcript(
    marker: str,
    *,
    projects_root: Path | None = None,
    expected_transcript_path: Path | str | None = None,
    exclude_paths: set[Path] | None = None,
    recency_seconds: float = 300.0,
    now_ts: float | None = None,
) -> Path | None:
    """Find a transcript containing ``marker``.

    When ``expected_transcript_path`` is supplied, inspect only that exact
    path. Otherwise search ``<root>/*/*.jsonl`` by recency + content match
    across one or more roots (see ``_resolve_local_projects_roots``),
    explicitly excluding any path in ``exclude_paths`` (the monitor's /
    dispatchd's own transcript). Returns the matching path or None.
    """
    marker_norm = normalized_dispatch_text(marker)
    if not marker_norm:
        return None
    exclude = exclude_paths or set()
    now = now_ts if now_ts is not None else time.time()

    if expected_transcript_path is not None:
        candidate = Path(expected_transcript_path).expanduser()
        try:
            candidate_key = candidate.resolve()
            excluded_keys = {Path(path).expanduser().resolve() for path in exclude}
        except (OSError, RuntimeError):
            return None
        if candidate_key in excluded_keys:
            return None
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            return None
        if now - mtime > recency_seconds:
            return None
        if _structural_transcript_confirms(_read_transcript_tail(candidate), marker_norm):
            return candidate
        return None

    candidates: list[tuple[float, Path]] = []
    for root in _resolve_local_projects_roots(projects_root):
        for jsonl in root.glob(transcript_glob()):
            if jsonl in exclude:
                continue
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            if now - mtime > recency_seconds:
                continue
            tail = _read_transcript_tail(jsonl)
            if _structural_transcript_confirms(tail, marker_norm):
                candidates.append((mtime, jsonl))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


_REMOTE_CLAUDE_PROJECTS_DEFAULT = "~/.claude/projects"


def _remote_root_shell_value(root: str) -> str:
    """Return ``root`` as a shell value, expanding only a leading ``~/``."""
    if root == "~":
        return '"$HOME"'
    if root.startswith("~/"):
        return f'"$HOME"/{shlex.quote(root[2:])}'
    return shlex.quote(root)


def _remote_transcript_grep_command(marker: str, root: str, recency_seconds: float, max_bytes: int = 262144) -> str:
    """Build an ssh-safe script which lists recent remote transcript paths.

    The marker and tail size remain parameters for compatibility with the
    previous helper signature. Matching intentionally happens locally in
    ``find_recent_transcript_remote`` so it uses the same normalization as
    ``find_recent_transcript``. Uses ``find -mmin`` (minutes, portable across
    GNU and BSD ``find``) and tries GNU ``stat -c`` then BSD ``stat -f``.
    """
    del marker, max_bytes
    pattern = transcript_glob()
    depth = pattern.count("/") + 1
    minutes = max(1, -(-int(recency_seconds) // 60))  # ceil division, minimum 1 minute
    stat_command = "for f do stat -c '%Y %n' \"$f\" 2>/dev/null || stat -f '%m %N' \"$f\" 2>/dev/null; done"
    return (
        f"root={_remote_root_shell_value(root)}; "
        f'find "$root" -mindepth {depth} -maxdepth {depth} -path "$root"/{shlex.quote(pattern)} '
        f"-mmin -{minutes} 2>/dev/null -exec sh -c {shlex.quote(stat_command)} sh {{}} \\;"
    )


def _remote_transcript_tail_command(path: str, max_bytes: int = 262144) -> str:
    """Build an ssh-safe command to read one remote transcript tail."""
    return f"tail -c {max_bytes} {shlex.quote(path)} 2>/dev/null"


def _remote_tail_confirms_marker(tail: str, marker_norm: str) -> bool:
    """Structurally confirm a remote JSONL tail (see
    ``_structural_transcript_confirms``) using the same semantics as the
    local implementation."""
    return _structural_transcript_confirms(tail, marker_norm)


def _json_string_values(value: object) -> list[str]:
    """Return all string leaves from a decoded JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _json_string_values(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _json_string_values(item)]
    return []


def find_recent_transcript_remote(
    host: str,
    marker: str,
    *,
    root: str | None = None,
    expected_transcript_path: str | None = None,
    recency_seconds: float = 300.0,
    runner: TmuxRunner | None = None,
) -> str | None:
    """Remote counterpart to ``find_recent_transcript`` over ssh.

    When ``expected_transcript_path`` is supplied, read only that exact path
    remotely. Otherwise list recent candidates remotely, then read at most
    eight newest tails and compare them locally with the same normalized
    marker semantics as the local implementation.

    Returns the remote path as a string (there is no local ``Path`` for it),
    or ``None`` if no match is found or the ssh call fails.
    """
    marker_norm = normalized_dispatch_text(marker)
    if not marker_norm:
        return None
    run = runner or run_cmd

    if expected_transcript_path is not None:
        tail_proc = run(ssh_command(host, _remote_transcript_tail_command(expected_transcript_path)), timeout=10)
        if tail_proc.returncode == 0 and _remote_tail_confirms_marker(tail_proc.stdout, marker_norm):
            return expected_transcript_path
        return None

    remote_root = root or _env("CHITRA_REMOTE_CLAUDE_PROJECTS", _REMOTE_CLAUDE_PROJECTS_DEFAULT)
    script = _remote_transcript_grep_command(marker, remote_root, recency_seconds)
    proc = run(ssh_command(host, script), timeout=10)
    if proc.returncode != 0:
        return None
    candidates: list[tuple[float, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        mtime_str, _, path = line.partition(" ")
        try:
            candidates.append((float(mtime_str), path))
        except ValueError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for _, path in candidates[:8]:
        tail_proc = run(ssh_command(host, _remote_transcript_tail_command(path)), timeout=10)
        if tail_proc.returncode == 0 and _remote_tail_confirms_marker(tail_proc.stdout, marker_norm):
            return path
    return None


def transcript_confirms_nudge(
    nudge: str,
    *,
    host: str = "",
    projects_root: Path | None = None,
    expected_transcript_path: Path | str | None = None,
    exclude_paths: set[Path] | None = None,
    recency_seconds: float = 300.0,
    now_ts: float | None = None,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    remote_root: str | None = None,
) -> tuple[bool, Path | str | None]:
    """Return ``(confirmed, transcript_path)`` by grepping transcripts.

    Replaces the source's ``pane_capture_confirms_nudge`` — pane capture is
    weaker evidence (a spinner or status line is not confirmation).

    ``expected_transcript_path`` binds verification to one transcript. If it
    is omitted, the existing recent-transcript discovery remains in use.

    ``host`` selects local vs remote verification: the default (``""``,
    treated as local, preserving prior behavior for existing callers) or any
    host ``is_local_host`` recognizes searches this machine's own
    ``~/.claude/projects`` (or ``projects_root``). Any other host checks the
    **target** host's transcripts over ssh instead — a remote delivery's
    transcript lives on the remote host's filesystem, not the caller's; the
    local-only search this function used to perform would never confirm a
    genuine remote delivery.
    """
    marker = nudge_confirmation_marker(nudge)
    if host and not is_local_host(host, local_extra):
        remote_path = find_recent_transcript_remote(
            host,
            marker,
            root=remote_root,
            expected_transcript_path=(str(expected_transcript_path) if expected_transcript_path is not None else None),
            recency_seconds=recency_seconds,
            runner=runner,
        )
        return (remote_path is not None, remote_path)
    path = find_recent_transcript(
        marker,
        projects_root=projects_root,
        expected_transcript_path=expected_transcript_path,
        exclude_paths=exclude_paths,
        recency_seconds=recency_seconds,
        now_ts=now_ts,
    )
    return (path is not None, path)


def pane_capture_confirms_nudge(
    nudge: str,
    *,
    host: str,
    pane: str,
    lines: int = DISPATCH_CAPTURE_LINES,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
    tmux_socket: Path | None = None,
) -> bool:
    """Fallback confirmation: does the delivered nudge marker appear in the
    target pane's recent capture?

    Transcript-grep is the primary, stronger evidence, but it can legitimately
    fail to *locate* the transcript (an unresolvable cwd-slug, a not-yet-flushed
    write, a target whose transcript lives outside the searched roots). When it
    does, the send may still have succeeded — reporting ``FAILED`` in that case
    is a false negative that erodes trust in the queue path and risks a resend.
    This checks the weaker-but-real pane signal (the same "verify by pane" a
    human operator would do): after paste+Enter, the submitted nudge text is
    visible in the pane's scrollback. Only consulted when transcript-grep did
    not confirm. Long nudges delivered by tmux paste render in Claude Code as a
    ``[Pasted text #N ...]`` placeholder, so this fallback inherently cannot
    confirm their text; the transcript path is required for those deliveries.
    """
    marker = nudge_confirmation_marker(nudge)
    if not marker:
        return False
    captured = capture_dispatch_pane(host, pane, lines=lines, runner=runner, local_extra=local_extra, tmux_socket=tmux_socket)
    if not captured:
        return False
    # A marker visible in the active composer is an unsubmitted draft, not
    # delivery evidence.  Fail closed before considering older scrollback.
    if _composer_holds_marker(captured, marker):
        return False
    text = normalized_dispatch_text(strip_terminal_controls("\n".join(captured)))
    return marker in text


# ---------------------------------------------------------------------------
# LaneLock — single-writer enforcement per session id
# ---------------------------------------------------------------------------


class LaneLockError(RuntimeError):
    """Raised when a lane lock cannot be acquired."""


class LaneLock:
    """File-based exclusive lock for a single session id.

    One writer per session id: a lock file per session id / pane target,
    acquired before any delivery attempt, released after. Acquiring a lock
    for an already-locked session id never silently proceeds: non-blocking
    ``acquire()`` (the default) returns ``False``; blocking ``acquire()``
    raises ``LaneLockError`` after ``timeout_seconds`` — the single-writer
    rule, enforced by whichever mode the caller chooses, not by the class
    on its own. ``dispatchd`` always calls ``acquire(blocking=True, ...)``.

    Implementation: an atomic ``O_CREAT|O_EXCL`` create of a lock file. The
    file holds the acquiring pid and a timestamp for diagnostics. On
    release the file is unlinked. Stale locks (pid no longer alive) are
    reclaimed.

    This is intentionally simple and crash-safe: if the process dies, the
    lock file remains but the pid inside is dead, so the next acquirer
    reclaims it.
    """

    def __init__(self, session_ref: str, *, lock_dir: Path | str | None = None) -> None:
        self.session_ref = session_ref
        default_lock_dir = str(Path(tempfile.gettempdir()) / "chitra-locks")
        base = Path(lock_dir) if lock_dir is not None else Path(_env("CHITRA_LANE_LOCK_DIR", default_lock_dir))
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_ref)
        self.lock_path = base / f"lane-{safe}.lock"
        self._acquired = False

    def acquire(self, *, blocking: bool = False, poll_seconds: float = 0.1, timeout_seconds: float = 5.0) -> bool:
        """Acquire the lock.

        If ``blocking`` and the lock is held by a live process, poll until
        acquired or ``timeout_seconds`` elapses (then raise
        ``LaneLockError``). If non-blocking (default), return False
        immediately if the lock is held by a live process. A stale lock
        (dead pid) is reclaimed.
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            reclaimed = self._try_reclaim_stale()
            if reclaimed:
                self._acquired = True
                return True
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if not blocking:
                    return False
                if time.monotonic() >= deadline:
                    raise LaneLockError(f"lane lock held for {self.session_ref}: {self.lock_path}") from None
                time.sleep(poll_seconds)
                continue
            payload = json.dumps({"pid": os.getpid(), "session_ref": self.session_ref, "at": datetime.now(UTC).isoformat()})
            os.write(fd, payload.encode("utf-8"))
            os.close(fd)
            self._acquired = True
            return True

    def _try_reclaim_stale(self) -> bool:
        """If the lock file exists but its pid is dead, reclaim it."""
        try:
            with self.lock_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            pid = int(data.get("pid", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if _pid_alive(pid):
            return False
        try:
            self.lock_path.unlink()
        except OSError:
            return False
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        payload = json.dumps({"pid": os.getpid(), "session_ref": self.session_ref, "at": datetime.now(UTC).isoformat()})
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        return True

    def release(self) -> None:
        """Release the lock if held by this instance."""
        if not self._acquired:
            return
        with contextlib.suppress(OSError):
            self.lock_path.unlink()
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def __enter__(self) -> LaneLock:
        self.acquire(blocking=True)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# dispatch_to_tmux — the main entry point
# ---------------------------------------------------------------------------


def dispatch_to_tmux(
    order: DispatchOrder,
    *,
    runner: TmuxRunner | None = None,
    input_runner: TmuxInputRunner | None = None,
    local_extra: set[str] | None = None,
    allowed_hosts: set[str] | None = None,
    projects_root: Path | None = None,
    expected_transcript_path: Path | str | None = None,
    exclude_transcripts: set[Path] | None = None,
    verify_wait_seconds: float | None = None,
    tuning: DispatchTuning | None = None,
    policy: PolicyConfig | None = None,
    tmux_socket: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DispatchResult:
    """Dispatch a nudge into a tmux pane using the verified recipe.

    Pipeline:
    1. Parse ``host:session:pane`` and enforce the host allowlist.
    2. Pre-dispatch idle/draft check (``pane_input_check``) — the safety net
       from the source; never overwrite an operator draft.
    3. Copy-mode detection + cancel (bug fix (b)).
    4. Paste with ``-p`` (bug fix (a)) + send-keys Enter.
    5. Verify by transcript-grep (replaces pane-capture confirmation).

    Returns a ``DispatchResult`` with status ``sent`` / ``blocked`` / ``failed``.
    """
    run = runner or run_cmd
    run_in = input_runner or run_cmd
    tuning = tuning or DispatchTuning()
    if verify_wait_seconds is not None:
        tuning = DispatchTuning(
            capture_lines=tuning.capture_lines,
            post_paste_wait_seconds=verify_wait_seconds,
            transcript_recency_seconds=tuning.transcript_recency_seconds,
            lane_lock_timeout_seconds=tuning.lane_lock_timeout_seconds,
        )
    voice_patterns = (
        [re.compile(pattern, re.IGNORECASE) for pattern in policy.dispatch.banned_attribution_patterns] if policy is not None else None
    )
    extra_idle_regexes = [re.compile(pattern) for pattern in policy.dispatch.extra_idle_input_regexes] if policy is not None else ()

    def _result(
        status: DispatchStatus,
        reason: str,
        *,
        marker: str = "",
        tail_hash: str = "",
        transcript_path: str | None = None,
    ) -> DispatchResult:
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            status=status,
            reason=reason,
            marker=marker,
            tail_hash=tail_hash,
            transcript_path=transcript_path,
        )

    parts = order.session_ref.split(":")
    if len(parts) != 3:
        return _result(DispatchStatus.FAILED, "unsupported session_ref (expected host:session:pane)")
    host, session, pane_field = parts
    pane = tmux_pane_target(session, pane_field)

    # Directive-voice guard: reject before anything is pasted. A BLOCKED
    # voice violation must never touch the pane and must never generate a
    # delivery-ledger entry (dispatchd only signs/logs on SENT).
    bad = directive_voice_violation(order.nudge, patterns=voice_patterns)
    if bad is not None:
        logger.info("tmux_dispatch_blocked_directive_voice", session_ref=order.session_ref, phrase=bad)
        return _result(DispatchStatus.BLOCKED, f"directive-voice: banned attribution phrase {bad!r}")

    hosts = allowed_hosts if allowed_hosts is not None else allowed_remote_dispatch_hosts()
    if host not in hosts and not is_local_host(host, local_extra):
        return _result(DispatchStatus.BLOCKED, f"remote dispatch to {host} not in allowlist")

    # Pre-dispatch idle/draft check (safety net from the source).
    pre_capture = capture_dispatch_pane(
        host, pane, lines=tuning.capture_lines, runner=run, local_extra=local_extra, tmux_socket=tmux_socket
    )
    pre_check = pane_input_check(
        pre_capture,
        baseline_hash=order.input_baseline_hash,
        snapshot_hash=order.snapshot_tail_hash,
        seen_hash=order.input_seen_hash,
        extra_idle_regexes=extra_idle_regexes,
    )
    if not pre_check.ok:
        logger.info(
            "tmux_dispatch_blocked",
            session_ref=order.session_ref,
            reason=pre_check.reason,
            tail_hash=pre_check.tail_hash,
            last_line=pre_check.last_line,
        )
        return _result(DispatchStatus.BLOCKED, pre_check.reason, tail_hash=pre_check.tail_hash)

    governed_remote = bool(host and not is_local_host(host, local_extra) and _env("CHITRA_REMOTE_LANE_GRANT") == "codexman")

    # Bug fix (b): copy-mode detection + cancel, run against the actual
    # target host (local or ssh-wrapped) — checking the local tmux server
    # for a remote target's copy-mode state would report on the wrong tmux
    # server entirely.
    if not governed_remote and not ensure_pane_not_in_mode(pane, host=host, runner=run, local_extra=local_extra, tmux_socket=tmux_socket):
        return _result(DispatchStatus.BLOCKED, "blocked: pane in copy-mode and cancel failed")

    # Bug fix (a): paste-buffer -p.
    if is_local_host(host, local_extra):
        proc = paste_nudge_to_local_tmux(pane, order.nudge, runner=run, input_runner=run_in, tmux_socket=tmux_socket)
        if proc.returncode != 0:
            return _result(
                DispatchStatus.FAILED,
                proc.stderr.strip() or proc.stdout.strip() or f"tmux paste-buffer failed rc={proc.returncode}",
            )
    else:
        if governed_remote:
            proc = run_in(ssh_command(host, f"chitra-lane-steer {shlex.quote(session)}"), order.nudge, timeout=10)
        else:
            remote_cmd = remote_tmux_paste_command(pane, order.nudge, tmux_socket=tmux_socket)
            proc = run(ssh_command(host, remote_cmd), timeout=10)
        if proc.returncode != 0:
            return _result(
                DispatchStatus.FAILED,
                proc.stderr.strip() or proc.stdout.strip() or f"remote tmux paste-buffer failed rc={proc.returncode}",
            )

    # Verified submit: a bare send-keys Enter does not commit Codex's
    # kitty-keyboard-protocol composer, so re-capture and, only if the nudge
    # is still sitting in the active composer row, fire one
    # backend-appropriate fallback before ever waiting on the transcript.
    submit_marker = nudge_confirmation_marker(order.nudge)
    submitted, submit_detail = ensure_nudge_submitted(
        host,
        pane,
        session,
        submit_marker,
        governed_remote=governed_remote,
        runner=run,
        input_runner=run_in,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
        lines=tuning.capture_lines,
    )
    if not submitted:
        logger.warning(
            "tmux_dispatch_submit_failed",
            session_ref=order.session_ref,
            marker=submit_marker,
            reason=submit_detail,
        )
        return _result(DispatchStatus.FAILED, submit_detail, marker=submit_marker)

    sleep(tuning.post_paste_wait_seconds)

    # Transcript-grep verification (replaces pane-capture confirmation).
    # host-aware: for a remote target, the delivered nudge lands in a
    # transcript on the remote host, not the local one, so verification
    # must run over ssh against that host — see transcript_confirms_nudge.
    confirmed, transcript_path = transcript_confirms_nudge(
        order.nudge,
        host=host,
        projects_root=projects_root,
        expected_transcript_path=expected_transcript_path,
        exclude_paths=exclude_transcripts,
        recency_seconds=tuning.transcript_recency_seconds,
        runner=run,
        local_extra=local_extra,
    )
    marker = nudge_confirmation_marker(order.nudge)
    if confirmed:
        logger.info(
            "tmux_dispatch_sent",
            session_ref=order.session_ref,
            marker=marker,
            transcript=str(transcript_path),
        )
        return _result(
            DispatchStatus.SENT,
            "sent: confirmed via lane transcript (user marker + subsequent agent/tool activity)",
            marker=marker,
            transcript_path=str(transcript_path) if transcript_path is not None else None,
        )
    # Transcript-grep could not confirm — but it can fail to *locate* the
    # transcript even when the send succeeded (unresolvable cwd-slug,
    # not-yet-flushed write, transcript outside the searched roots). Before
    # declaring FAILED (a false negative that erodes queue-path trust and
    # risks a resend), fall back to the weaker-but-real pane signal. Pane
    # capture cannot tell a genuinely-started turn from a scrollback echo or
    # an unsubmitted composer row, so it is never authoritative on its own:
    # this reports DELIVERY_UNCONFIRMED, not SENT. dispatchd keeps the send
    # nonce and retries transcript verification without pasting again.
    if pane_capture_confirms_nudge(
        order.nudge,
        host=host,
        pane=pane,
        lines=tuning.capture_lines,
        runner=run,
        local_extra=local_extra,
        tmux_socket=tmux_socket,
    ):
        logger.info(
            "tmux_dispatch_delivery_unconfirmed_pane_fallback",
            session_ref=order.session_ref,
            marker=marker,
        )
        return _result(
            DispatchStatus.DELIVERY_UNCONFIRMED,
            "delivery-unconfirmed: confirmed only via pane-capture fallback (transcript-grep found no marker)",
            marker=marker,
        )
    logger.info(
        "tmux_dispatch_unverified",
        session_ref=order.session_ref,
        marker=marker,
    )
    return _result(
        DispatchStatus.FAILED,
        "send-failed-no-confirmation (transcript-grep and pane-capture both found no marker)",
        marker=marker,
    )

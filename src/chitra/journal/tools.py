"""Harness-neutral semantics over canonical tool events.

Detectors classify a ``TOOL_CALL`` by what it does, not by a
client-specific tool name or payload shape. These helpers reduce a
canonical call to a tool class, its command text, its target paths, and
a normalized call signature — the Cline-style ``tool name + normalized
parameters`` identity that repeat detection hashes, and the command
argv that check-suite detection parses.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from collections.abc import Sequence
from typing import Any, Literal

from .models import CanonicalEvent, CanonicalType

ToolClass = Literal["write", "shell", "read", "plan", "other"]

# Names are matched after normalization: lowercase with every
# non-alphanumeric removed ("apply_patch" -> "applypatch").
_WRITE_NAMES = frozenset(
    {
        "edit",
        "write",
        "multiedit",
        "notebookedit",
        "applypatch",
        "patch",
        "strreplaceeditor",
        "strreplace",
        "createfile",
        "writefile",
        "editfile",
        "fswrite",
        "fileedit",
        "insertedit",
    }
)
_SHELL_NAMES = frozenset(
    {
        "bash",
        "shell",
        "exec",
        "execcommand",
        "localshell",
        "runcommand",
        "runterminalcommand",
        "terminal",
        "powershell",
        "cmd",
        "process",
        "console",
        "containerexec",
    }
)
_READ_NAMES = frozenset(
    {
        "read",
        "glob",
        "grep",
        "readfile",
        "readfilerange",
        "listdir",
        "listfiles",
        "grepfiles",
        "view",
        "viewfile",
        "openfile",
        "search",
        "filesearch",
        "codebasesearch",
        "find",
        "findfiles",
        "websearch",
        "webfetch",
    }
)
_PLAN_NAMES = frozenset(
    {
        "todowrite",
        "todoread",
        "todo",
        "updateplan",
        "plan",
        "createplan",
        "exitplanmode",
        "writeplan",
    }
)

_COMMAND_FIELDS = ("command", "cmd", "argv", "args", "script", "shell_command")
_TARGET_FIELDS = frozenset(
    {
        "file_path",
        "path",
        "target",
        "target_path",
        "filename",
        "notebook_path",
        "destination",
        "file",
    }
)
_TARGET_LIST_FIELDS = frozenset({"files", "paths", "file_paths", "targets"})

_FILE_SUFFIXES = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".sh",
        ".rb",
        ".md",
        ".rst",
        ".txt",
        ".adoc",
        ".org",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".xml",
        ".html",
        ".css",
        ".sql",
        ".proto",
        ".lock",
        ".log",
        ".csv",
    }
)

# Shell command splitting: one logical command per segment.
_SEGMENT_SPLIT_RE = re.compile(r"\|\|?|&&|;|\n")

# Environment-assignment and launcher prefixes stripped before argv[0].
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ENV_WRAPPERS = frozenset({"env", "command", "builtin", "exec", "nice", "nohup", "time", "ionice"})
_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh"})

_DIRECT_CHECK_RUNNERS = frozenset(
    {
        "pytest",
        "ruff",
        "mypy",
        "pyright",
        "tox",
        "nose",
        "nose2",
        "nosetests",
        "vitest",
        "jest",
        "mocha",
        "ava",
        "playwright",
        "eslint",
        "tsc",
        "golangcilint",
        "ctest",
        "rspec",
        "minitest",
        "phpunit",
        "coverage",
        "behave",
        "cucumber",
        "gotestsum",
        "bandit",
        "pylint",
        "flake8",
        "pyflakes",
        "rubocop",
        "biome",
        "oxlint",
        "swiftlint",
        "ktlint",
        "checkstyle",
    }
)
# ``python -m <module>`` modules that are check runners.
_MODULE_CHECK_RUNNERS = frozenset(
    {"pytest", "unittest", "nose", "mypy", "ruff", "tox", "coverage", "doctest", "pylint", "flake8", "bandit"}
)
# ``uv run``/``poetry run`` style launchers whose next argv word is the real command.
_RUN_WRAPPERS = frozenset({"uv", "poetry", "pipenv", "hatch", "pdm", "rye", "mise"})
# Package managers whose subcommands carry check semantics.
_PM_NAMES = frozenset({"npm", "pnpm", "yarn", "bun"})
_PM_CHECK_SUBCOMMANDS = frozenset({"test", "t", "it", "lint", "check", "ci", "coverage", "typecheck"})
_PM_EXEC_SUBCOMMANDS = frozenset({"exec", "dlx"})
# Build tools where a bare target word carries check semantics.
_BUILD_TOOL_NAMES = frozenset(
    {"make", "just", "task", "bake", "rake", "bazel", "buck", "buck2", "gradle", "mvn", "ant", "ninja", "sbt"}
)
_BUILD_CHECK_TARGET_RE = re.compile(r"^[-\w]*(?:test|check|lint|verify|ci|spec)[-\w]*$", re.IGNORECASE)

_PYTHON_RE = re.compile(r"^python[\d.]*$")
_PYTEST_ALIASES = {"py.test": "pytest"}

# A check signature is a stable tuple: (runner label, normalized positional targets).
CheckSignature = tuple[str, tuple[str, ...]]


def normalized_tool_name(tool_name: object) -> str:
    if not isinstance(tool_name, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", tool_name.lower())


def tool_class(tool_name: object) -> ToolClass:
    name = normalized_tool_name(tool_name)
    if name in _WRITE_NAMES:
        return "write"
    if name in _SHELL_NAMES:
        return "shell"
    if name in _READ_NAMES:
        return "read"
    if name in _PLAN_NAMES:
        return "plan"
    return "other"


def coerce_tool_input(value: object) -> dict[str, Any] | str | None:
    """Return the tool input as a dict or text.

    Codex ``function_call`` arguments arrive as a JSON string; a JSON
    object payload is coerced to a dict. Patch text and other strings
    stay strings.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                return value
            if isinstance(parsed, dict):
                return parsed
        return value
    return None


def command_text(event: CanonicalEvent) -> str:
    """Return the shell command a call would run, or ``""``.

    Dict inputs supply a command only through known command fields; a
    bare string input is a command only for shell-class or unknown-class
    tools — an ``apply_patch`` patch body is not a command.
    """
    payload = event.payload
    tool = tool_class(payload.get("tool_name"))
    value = coerce_tool_input(payload.get("input"))
    if isinstance(value, dict):
        if tool not in {"shell", "other"}:
            return ""
        for key in _COMMAND_FIELDS:
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and all(
                isinstance(item, str) for item in raw
            ):
                return " ".join(raw)
        return ""
    if isinstance(value, str):
        return value if tool in {"shell", "other"} else ""
    return ""


_PATCH_TARGET_RE = re.compile(
    r"^\*{2,4}\s*(?:Update|Add|Delete|Move)\s+File:\s*(?P<path>\S+)|^diff\s+--git\s+a/(?P<old>\S+)\s+b/(?P<new>\S+)|^[-+]{3}\s+[ab]/(?P<diff>\S+)",
    re.MULTILINE,
)


def _paths_in_text(text: str) -> tuple[str, ...]:
    """Extract path-looking tokens from free text or a shell command."""
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        tokens = text.split()
    paths: list[str] = []
    for token in tokens:
        candidate = token.strip("\"'").lstrip("><|;&").rstrip(",;&")
        if not candidate or candidate.startswith("-") or "://" in candidate:
            continue
        suffix = posixpath.splitext(candidate)[1].lower()
        if "/" in candidate or "\\" in candidate or suffix in _FILE_SUFFIXES or "*" in candidate:
            paths.append(candidate)
    return tuple(paths)


def _patch_paths(text: str) -> tuple[str, ...]:
    paths: list[str] = []
    for match in _PATCH_TARGET_RE.finditer(text):
        paths.extend(group for group in match.groups() if group)
    return tuple(paths)


def target_paths(event: CanonicalEvent) -> tuple[str, ...]:
    """Return every path a tool call names as its target.

    Dict inputs contribute explicit target fields plus paths inside a
    command string. String inputs contribute patch-file headers and, for
    shell-class or unknown tools, path tokens in the command text.
    """
    payload = event.payload
    value = coerce_tool_input(payload.get("input"))
    tool = tool_class(payload.get("tool_name"))
    paths: list[str] = []
    if isinstance(value, dict):
        for key, raw in value.items():
            if key in _TARGET_FIELDS and isinstance(raw, str) and raw:
                paths.append(raw)
            elif key in _TARGET_LIST_FIELDS and isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                paths.extend(item for item in raw if isinstance(item, str) and item)
            elif tool == "write" and isinstance(raw, str):
                # Codex ``apply_patch`` nests the patch body under ``input``;
                # its file headers are the targets.
                paths.extend(_patch_paths(raw))
        command = command_text(event)
        if command:
            paths.extend(_paths_in_text(command))
    elif isinstance(value, str):
        paths.extend(_patch_paths(value))
        if tool in {"shell", "other"}:
            paths.extend(_paths_in_text(value))
    return tuple(dict.fromkeys(paths))


def _normalize_arg(token: str) -> str:
    """Normalize one positional argument: collapse ./ and trailing slash."""
    if token.startswith("./"):
        token = token[2:]
    while "//" in token:
        token = token.replace("//", "/")
    if len(token) > 1 and token.endswith("/"):
        token = token.rstrip("/")
    return token


def _argv(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _unwrap_argv(argv: list[str]) -> list[str]:
    """Strip env assignments and launcher prefixes from one argv."""
    while argv:
        head = argv[0]
        if _ENV_ASSIGN_RE.match(head):
            argv = argv[1:]
            continue
        base = posixpath.basename(head)
        if base in _ENV_WRAPPERS:
            argv = argv[1:]
            continue
        if base == "timeout":
            # ``timeout DURATION cmd`` — drop the duration argument too.
            argv = argv[2:] if len(argv) > 1 else []
            continue
        if base == "sudo":
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                flag = argv[0]
                argv = argv[1:]
                if flag in {"-u", "-g", "-p", "-C"} and argv:
                    argv = argv[1:]
            continue
        if base in _SHELL_WRAPPERS and len(argv) >= 3 and argv[1] in {"-c", "-lc", "-ic"}:
            inner = argv[2] if len(argv) == 3 else " ".join(argv[2:])
            return _unwrap_argv(_argv(inner))
        break
    return argv


def command_segments_argv(command: str) -> tuple[tuple[str, ...], ...]:
    """Unwrapped argv per shell segment; env/sudo/``sh -c`` prefixes removed."""
    return tuple(
        argv
        for segment in _SEGMENT_SPLIT_RE.split(command)
        if (argv := tuple(_unwrap_argv(_argv(segment))))
    )


def command_signature(command: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the normalized command identity: one ``(argv0, positionals)`` pair per segment.

    Flags are dropped and positionals are path-normalized, so
    ``git status``, ``git status --porcelain`` and ``git status -sb``
    share one signature while ``cat a`` and ``cat b`` do not.
    """
    signature: list[tuple[str, tuple[str, ...]]] = []
    for segment in _SEGMENT_SPLIT_RE.split(command):
        argv = _unwrap_argv(_argv(segment))
        if not argv:
            continue
        head = _PYTEST_ALIASES.get(posixpath.basename(argv[0]), posixpath.basename(argv[0]))
        positionals = tuple(_normalize_arg(arg) for arg in argv[1:] if not arg.startswith("-"))
        signature.append((head, positionals))
    return tuple(signature)


def call_signature(event: CanonicalEvent) -> str:
    """The Cline-style call identity: normalized tool name + normalized parameters.

    For shell-class calls the parameters are the parsed command
    signature; for other tools the parameters are the canonicalized
    input. Result payloads are never part of the identity.
    """
    payload = event.payload
    name = normalized_tool_name(payload.get("tool_name"))
    tool = tool_class(name)
    if tool == "shell":
        params: object = {"command": [list(segment) for segment in command_signature(command_text(event))]}
    else:
        params = _normalized_params(coerce_tool_input(payload.get("input")))
    encoded = json.dumps(
        {"tool": name or tool, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalized_params(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {key: _normalized_params(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized_params(item) for item in value]
    return value


def check_signature(event: CanonicalEvent) -> CheckSignature | None:
    """Return the check-runner identity for a call, or ``None``.

    The command actually run is parsed — argv[0] plus its check
    subcommand — across env wrappers, ``python -m``, ``uv/poetry run``,
    package managers, and build-tool targets. ``pnpm test`` is its own
    runner, not an ``npm test`` substring hit.
    """
    name = normalized_tool_name(event.payload.get("tool_name"))
    if name in _DIRECT_CHECK_RUNNERS:
        return (name, ())
    command = command_text(event)
    if not command:
        return None
    for segment in _SEGMENT_SPLIT_RE.split(command):
        argv = _unwrap_argv(_argv(segment))
        found = _check_argv(argv)
        if found is not None:
            return found
    return None


def _normalized_positionals(args: list[str]) -> tuple[str, ...]:
    return tuple(_normalize_arg(arg) for arg in args if not arg.startswith("-"))


def _check_argv(argv: list[str]) -> CheckSignature | None:
    while argv:
        head = _PYTEST_ALIASES.get(posixpath.basename(argv[0]), posixpath.basename(argv[0]))
        rest = argv[1:]
        if _PYTHON_RE.match(head):
            if len(rest) >= 2 and rest[0] == "-m" and rest[1] in _MODULE_CHECK_RUNNERS:
                # ``python -m pytest`` is the pytest runner, so it shares the
                # bare runner's identity rather than a python-specific one.
                return (rest[1], _normalized_positionals(rest[2:]))
            return None
        if head in _RUN_WRAPPERS:
            if rest and rest[0] in {"run", "exec"}:
                argv = rest[1:]
                continue
            return None
        if head in {"npx", "bunx"}:
            argv = rest
            continue
        if head in _PM_NAMES:
            if not rest:
                return None
            sub = rest[0]
            if sub == "run":
                # ``npm run <script>`` — the script name is the check surface.
                if len(rest) >= 2 and _BUILD_CHECK_TARGET_RE.match(rest[1]):
                    return (f"{head} {rest[1]}", _normalized_positionals(rest[2:]))
                return None
            if sub in _PM_EXEC_SUBCOMMANDS:
                argv = rest[1:]
                continue
            if sub in _PM_CHECK_SUBCOMMANDS:
                return (f"{head} {sub}", _normalized_positionals(rest[1:]))
            return None
        if head == "deno":
            if rest and rest[0] in {"test", "check", "lint"}:
                return (f"deno {rest[0]}", _normalized_positionals(rest[1:]))
            if rest and rest[0] == "task":
                argv = rest[1:]
                continue
            return None
        if head == "go":
            if rest and rest[0] in {"test", "vet", "build", "lint"}:
                return (f"go {rest[0]}", _normalized_positionals(rest[1:]))
            return None
        if head == "cargo":
            if rest and rest[0] in {"test", "clippy", "check", "build", "nextest"}:
                return (f"cargo {rest[0]}", _normalized_positionals(rest[1:]))
            return None
        if head in _BUILD_TOOL_NAMES:
            check_targets = tuple(
                _normalize_arg(arg) for arg in rest if not arg.startswith("-") and _BUILD_CHECK_TARGET_RE.match(arg)
            )
            if check_targets:
                return (head, check_targets)
            return None
        if head in _DIRECT_CHECK_RUNNERS:
            return (head, _normalized_positionals(rest))
        return None
    return None


def _result_text(result: CanonicalEvent) -> str:
    parts: list[str] = []
    for key in ("content", "output", "tool_use_result", "summary"):
        value = result.payload.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        elif isinstance(value, dict):
            for item in value.values():
                if isinstance(item, str):
                    parts.append(item)
    return "\n".join(parts)


# A failure needs a nonzero count or exit marker — bare "0 failed" and
# success banners stay out of the fail class.
_FAIL_TEXT_RE = re.compile(
    r"\b(?:[1-9]\d*\s+(?:tests?\s+)?(?:failed|errors?)\b|exit(?:\s+code)?\s*[:=]?\s*[1-9]\d*\b|returncode\s*[:=]?\s*[1-9]\d*)",
    re.IGNORECASE,
)
_PASS_TEXT_RE = re.compile(
    r"\b\d+\s+(?:tests?\s+)?passed\b|\bPASSED\b|\bOK\b|\b0\s+failed\b|\bno\s+(?:tests?\s+)?(?:failed|failures?|errors?)\b",
    re.IGNORECASE,
)


def result_class(result: CanonicalEvent | None) -> str:
    """Coarse outcome class of a tool result: error/fail/pass/pending/unknown."""
    if result is None:
        return "pending"
    if result.normalized_type is CanonicalType.TOOL_ERROR or result.payload.get("is_error") is True:
        return "error"
    exit_code = result.payload.get("exit_code")
    if isinstance(exit_code, bool):
        exit_code = int(exit_code)
    if isinstance(exit_code, int) and exit_code != 0:
        return "fail"
    text = _result_text(result)
    if _FAIL_TEXT_RE.search(text):
        return "fail"
    if exit_code == 0 or _PASS_TEXT_RE.search(text):
        return "pass"
    return "unknown"


def result_fingerprint(result: CanonicalEvent) -> str:
    """Content identity of a result: normalized text + outcome markers."""
    payload = result.payload
    encoded = json.dumps(
        {
            "text": " ".join(_result_text(result).split()),
            "is_error": payload.get("is_error") is True or result.normalized_type is CanonicalType.TOOL_ERROR,
            "exit_code": payload.get("exit_code"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def unanswered_tail_calls(events: Sequence[CanonicalEvent]) -> tuple[CanonicalEvent, ...]:
    """Tool calls in the current session tail that have no joined result.

    A call positioned after the latest turn-final is executing right
    now. A ``run_in_background`` call answered nowhere in its session is
    still running. Either form means the lane is mid-command, not idle.
    """
    if not events:
        return ()
    answered = {
        event.native_join_id
        for event in events
        if event.normalized_type in (CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR)
    }
    last_final = -1
    for index, event in enumerate(events):
        if event.normalized_type is CanonicalType.FINAL_RESPONSE:
            last_final = index
    current_session = events[-1].session_id
    in_flight: list[CanonicalEvent] = []
    for index, event in enumerate(events):
        if (
            event.normalized_type is not CanonicalType.TOOL_CALL
            or event.native_join_id is None
            or event.native_join_id in answered
            or event.session_id != current_session
        ):
            continue
        call_input = coerce_tool_input(event.payload.get("input"))
        background = isinstance(call_input, dict) and call_input.get("run_in_background") is True
        if index > last_final or background:
            in_flight.append(event)
    return tuple(in_flight)


# Binaries that only observe: they never change the tree, so a command made
# entirely of them is a read for message/classification purposes.
_SHELL_READONLY_BINS = frozenset(
    {
        "cat",
        "ls",
        "head",
        "tail",
        "wc",
        "grep",
        "rg",
        "find",
        "pwd",
        "stat",
        "du",
        "df",
        "file",
        "tree",
        "echo",
        "printf",
        "type",
        "which",
        "sort",
        "uniq",
        "diff",
        "less",
        "more",
        "nl",
        "cut",
        "tr",
        "env",
        "printenv",
        "date",
        "uname",
        "hostname",
        "whoami",
        "id",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "jq",
        "yq",
        "xargs",
    }
)
_GIT_READONLY_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "rev-list", "ls-files", "blame", "describe", "remote", "config"}
)

# Binaries whose positional arguments are files they create/modify/delete.
_SHELL_WRITE_BINS = frozenset(
    {
        "cp",
        "mv",
        "ln",
        "install",
        "dd",
        "rsync",
        "tee",
        "touch",
        "mkdir",
        "rmdir",
        "truncate",
        "rm",
        "chmod",
        "chown",
        "chgrp",
        "patch",
        "apply_patch",
    }
)
# In-place editors: first positional is the script/expression, the rest are files.
_SHELL_INPLACE_BINS = frozenset({"sed", "perl", "ed"})
_GIT_WRITE_SUBCOMMANDS = frozenset(
    {
        "apply",
        "checkout",
        "restore",
        "reset",
        "clean",
        "rm",
        "mv",
        "commit",
        "merge",
        "rebase",
        "pull",
        "stash",
        "cherry-pick",
        "am",
        "clone",
        "init",
        "tag",
        "branch",
        "switch",
    }
)
_REDIRECT_TARGET_RE = re.compile(r">>?\s*([^\s|;&>]+)")


def shell_write_targets(event: CanonicalEvent) -> tuple[str, ...]:
    """Paths a shell command actually writes, via redirect/write binaries.

    Redirects contribute their target token; write binaries contribute their
    positional arguments; in-place editors contribute arguments after the
    script operand. Reads and pure output are never targets.
    """
    command = command_text(event)
    if not command:
        return ()
    targets: list[str] = []
    for segment in _SEGMENT_SPLIT_RE.split(command):
        targets.extend(match.group(1) for match in _REDIRECT_TARGET_RE.finditer(segment))
        argv = _unwrap_argv(_argv(segment))
        if not argv:
            continue
        head = posixpath.basename(argv[0])
        positionals = [token for token in argv[1:] if not token.startswith("-")]
        if head in _SHELL_WRITE_BINS:
            targets.extend(positionals)
        elif (
            head in _SHELL_INPLACE_BINS
            and any(token == "-i" or token.startswith("-i") for token in argv[1:])
            or head == "git"
            and positionals
            and positionals[0] in _GIT_WRITE_SUBCOMMANDS
        ):
            targets.extend(positionals[1:])
    return tuple(dict.fromkeys(targets))


def action_kind(event: CanonicalEvent) -> str:
    """The verb a call performs: read, check, write, command, or call.

    Used where a finding names the repeated action. A shell call is a
    ``check`` when it invokes a check runner, a ``write`` when it touches
    write targets, a ``read`` when every segment is a known read-only
    binary, and otherwise a generic ``command``.
    """
    cls = tool_class(event.payload.get("tool_name"))
    if cls == "read":
        return "read"
    if cls == "write":
        return "write"
    if cls in {"shell", "other"}:
        if check_signature(event) is not None:
            return "check"
        command = command_text(event)
        if not command:
            return "call"
        heads: list[str] = []
        for argv in command_segments_argv(command):
            head = posixpath.basename(argv[0])
            if head == "git" and len(argv) > 1:
                heads.append(f"git {argv[1]}")
            else:
                heads.append(head)
        if heads and all(
            head in _SHELL_READONLY_BINS or (head.startswith("git ") and head[4:] in _GIT_READONLY_SUBCOMMANDS)
            for head in heads
        ):
            return "read"
        if shell_write_targets(event):
            return "write"
        return "command"
    return "call"


__all__ = [
    "CheckSignature",
    "action_kind",
    "call_signature",
    "check_signature",
    "coerce_tool_input",
    "command_segments_argv",
    "command_signature",
    "command_text",
    "normalized_tool_name",
    "result_class",
    "result_fingerprint",
    "shell_write_targets",
    "target_paths",
    "tool_class",
    "unanswered_tail_calls",
]

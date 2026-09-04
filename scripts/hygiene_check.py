#!/usr/bin/env python3
"""Flag names, emails, hostnames, and org terms per .hygiene-denylist.

Usage:
    scripts/hygiene_check.py [--fix] [--denylist PATH] [files...]

With no files given, scans all git-tracked text files. Deny-list lines are
"block" by default (personal data, secrets) and fail the check; a line
prefixed "warn:" is reported but never fails. In check mode (default),
prints "path:line: matches /<pattern>/" for a block hit and "path:line:
warns /<pattern>/" for a warn hit -- it never prints the matched text
itself -- then prints "hygiene: N block, M warn" and exits 1 if any block
hits were found, 0 otherwise. With --fix, rewrites block-tier matches to
their placeholder in place (warn-tier lines are left alone) and exits 0.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DENYLIST = REPO_ROOT / ".hygiene-denylist"
DEFAULT_PLACEHOLDER = "<redacted>"

Rule = tuple[re.Pattern[str], str, str]  # pattern, placeholder, tier ("block" or "warn")

WARN_PREFIX = "warn:"


def load_rules(path: Path) -> tuple[list[Rule], list[re.Pattern[str]]]:
    deny: list[Rule] = []
    allow: list[re.Pattern[str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            allow.append(re.compile(line[1:]))
            continue
        tier = "block"
        if line.startswith(WARN_PREFIX):
            tier = "warn"
            line = line[len(WARN_PREFIX) :]
        if "\t" in line:
            pattern, placeholder = line.split("\t", 1)
        else:
            pattern, placeholder = line, DEFAULT_PLACEHOLDER
        deny.append((re.compile(pattern), placeholder, tier))
    return deny, allow


def git_tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True, cwd=REPO_ROOT)
    files = []
    for name in out.stdout.splitlines():
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files.append(path)
    return files


def _allow_spans(line: str, allow: list[re.Pattern[str]]) -> list[tuple[int, int]]:
    return [m.span() for pattern in allow for m in pattern.finditer(line)]


def _is_allowed(span: tuple[int, int], allow_spans: list[tuple[int, int]]) -> bool:
    return any(a_start <= span[0] and span[1] <= a_end for a_start, a_end in allow_spans)


def scan_line(line: str, deny: list[Rule], allow: list[re.Pattern[str]]) -> list[tuple[str, str]]:
    """Return (pattern-string, tier) for each deny match that isn't allow-listed."""
    allow_spans = _allow_spans(line, allow)
    hits = []
    for pattern, _placeholder, tier in deny:
        for match in pattern.finditer(line):
            if not _is_allowed(match.span(), allow_spans):
                hits.append((pattern.pattern, tier))
    return hits


def fix_line(line: str, deny: list[Rule], allow: list[re.Pattern[str]]) -> str:
    for pattern, placeholder, tier in deny:
        if tier != "block":
            continue
        allow_spans = _allow_spans(line, allow)

        def replace(match: re.Match[str], placeholder: str = placeholder, allow_spans: list[tuple[int, int]] = allow_spans) -> str:
            return match.group(0) if _is_allowed(match.span(), allow_spans) else placeholder

        line = pattern.sub(replace, line)
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="Files to check (default: all git-tracked text files)")
    parser.add_argument("--fix", action="store_true", help="Rewrite matches to placeholders in place")
    parser.add_argument("--denylist", type=Path, default=DEFAULT_DENYLIST)
    args = parser.parse_args(argv)

    deny, allow = load_rules(args.denylist)
    files = [Path(f) for f in args.files] if args.files else git_tracked_text_files()

    if args.fix:
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            new_text = "\n".join(fix_line(line, deny, allow) for line in text.split("\n"))
            if new_text != text:
                path.write_text(new_text, encoding="utf-8")
                print(f"fixed: {path}")
        return 0

    block_hits = 0
    warn_hits = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(lines, start=1):
            for pattern, tier in scan_line(line, deny, allow):
                if tier == "block":
                    print(f"{path}:{lineno}: matches /{pattern}/")
                    block_hits += 1
                else:
                    print(f"{path}:{lineno}: warns /{pattern}/")
                    warn_hits += 1

    print(f"hygiene: {block_hits} block, {warn_hits} warn")
    if block_hits:
        print(
            f"\n{block_hits} blocking hygiene finding(s). Run with --fix to rewrite to "
            "placeholders, or add an allow-list line to .hygiene-denylist.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

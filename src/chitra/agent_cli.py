"""CLI wrappers for Chitra's semantic agent-status socket API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from chitra.agent_status import ManifestRepository, classify_snapshot
from chitra.api_protocol import api_schema
from chitra.socket_api import default_socket_path, request


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _pane_id(value: str | None) -> str:
    pane_id = value or _env("CHITRA_PANE_ID")
    if pane_id is None:
        raise ValueError("pane id is required; pass --pane-id or set CHITRA_PANE_ID")
    return pane_id


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chitra-agent")
    parser.add_argument("--socket-path", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report", help="Report authoritative integration lifecycle state.")
    report.add_argument("--pane-id", default=None)
    report.add_argument("--session-ref", default=None)
    report.add_argument("--source", required=True)
    report.add_argument("--agent", required=True)
    report.add_argument("--state", choices=("idle", "working", "blocked"), required=True)

    clear = commands.add_parser("clear-authority", help="Release integration authority for a pane.")
    clear.add_argument("--pane-id", default=None)
    clear.add_argument("--source", default=None)

    explain = commands.add_parser("explain", help="Explain live or offline manifest status evidence.")
    explain.add_argument("--pane-id", default=None)
    explain.add_argument("--file", type=Path, default=None)
    explain.add_argument("--agent", default=None)
    explain.add_argument("--manifest-dir", type=Path, default=None)

    wait = commands.add_parser("wait", help="Block until a pane reaches semantic state.")
    wait.add_argument("--pane-id", default=None)
    wait.add_argument("--until", choices=("idle", "working", "blocked", "done", "unknown"), required=True)
    wait.add_argument("--timeout-ms", type=int, default=None)

    schema = commands.add_parser("schema", help="Print the full local socket JSON Schema document.")
    schema.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    socket_path = args.socket_path or default_socket_path()
    try:
        if args.command == "report":
            result = request(
                socket_path,
                "cli:report",
                "pane.report_agent",
                {
                    "pane_id": _pane_id(args.pane_id),
                    "session_ref": args.session_ref or _env("CHITRA_SESSION_REF"),
                    "source": args.source,
                    "agent": args.agent,
                    "state": args.state,
                },
            )
            _print(result)
            return 0
        if args.command == "clear-authority":
            params: dict[str, object] = {"pane_id": _pane_id(args.pane_id)}
            if args.source is not None:
                params["source"] = args.source
            _print(request(socket_path, "cli:clear", "pane.clear_agent_authority", params))
            return 0
        if args.command == "explain":
            if args.file is not None:
                if args.agent is None:
                    raise ValueError("--agent is required with --file")
                explain = classify_snapshot(
                    args.file.read_text(encoding="utf-8"),
                    agent=args.agent,
                    repository=ManifestRepository(args.manifest_dir),
                )
                _print(explain.to_dict())
                return 0
            if args.agent is not None or args.manifest_dir is not None:
                raise ValueError("--agent and --manifest-dir are valid only with --file")
            _print(
                request(
                    socket_path,
                    "cli:explain",
                    "agent.explain",
                    {"pane_id": _pane_id(args.pane_id)},
                )
            )
            return 0
        if args.command == "wait":
            params = {"pane_id": _pane_id(args.pane_id), "until": args.until}
            if args.timeout_ms is not None:
                params["timeout_ms"] = args.timeout_ms
            _print(request(socket_path, "cli:wait", "agent.wait", params))
            return 0
        schema = api_schema()
        rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    except (ConnectionError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())

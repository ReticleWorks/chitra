"""Append-only, tool-mediated record of consequential monitor decisions."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, Field, ValidationError, field_validator

from chitra._fsio import parse_iso8601
from chitra.plain_english import require_plain_english
from chitra.state_paths import default_decisions_path

DecisionKind = Literal[
    "ask-retirement",
    "doctrine-override",
    "adjudication",
    "redirect",
    "pause",
    "resume",
    "lane-architecture-change",
]


class DecisionEntry(BaseModel):
    """One immutable decision and the authority that supports it."""

    schema_: Literal["chitra.decisions.v1"] = Field(default="chitra.decisions.v1", alias="schema")
    decision_id: str = Field(min_length=1)
    at: str = Field(min_length=1)
    kind: DecisionKind
    decision: str = Field(min_length=1)
    basis: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    authority: str = Field(min_length=1)

    @field_validator("at")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        parse_iso8601(value, require_timezone=True)
        return value

    @field_validator("decision", "basis", "authority")
    @classmethod
    def _plain_english(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "text")
        return require_plain_english(value, field=field_name)

    @field_validator("citation")
    @classmethod
    def _citation_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("citation must be non-empty")
        return value


def append_decision(path: Path, entry: DecisionEntry) -> DecisionEntry:
    """Append and flush one entry. Existing bytes are never rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.model_dump_json(by_alias=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def read_decisions(path: Path | None = None) -> list[DecisionEntry]:
    """Read the log strictly so damage cannot look like missing history."""
    source = default_decisions_path() if path is None else path
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [DecisionEntry.model_validate_json(line) for line in lines if line.strip()]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chitra-decisions", description="Append and read consequential monitor decisions.")
    parser.add_argument("--log-path", type=Path, default=default_decisions_path())
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Append one decision with its basis and authority.")
    add.add_argument("--kind", choices=get_args(DecisionKind), required=True)
    add.add_argument("--decision", required=True)
    add.add_argument("--basis", required=True)
    add.add_argument("--citation", required=True)
    add.add_argument("--authority", required=True)
    add.add_argument("--at")
    commands.add_parser("list", help="Print all decisions in append order.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "add":
            entry = DecisionEntry(
                decision_id=uuid.uuid4().hex,
                at=args.at or datetime.now(UTC).isoformat(),
                kind=args.kind,
                decision=args.decision,
                basis=args.basis,
                citation=args.citation,
                authority=args.authority,
            )
            print(append_decision(args.log_path, entry).model_dump_json(by_alias=True))
        else:
            for entry in read_decisions(args.log_path):
                print(entry.model_dump_json(by_alias=True))
    except (OSError, ValueError, ValidationError) as exc:
        print(f"chitra-decisions: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line interface for advisory resource presence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chitra.presence import PresenceError, announce_released, announce_using, list_presence


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chitra-presence", description="Publish and inspect advisory shared-resource presence.")
    parser.add_argument("--shared-dir", type=Path, default=None, help="Coordination root; defaults to CHITRA_SHARED_DIR.")
    sub = parser.add_subparsers(dest="verb", required=True)

    for verb in ("using", "released"):
        command = sub.add_parser(verb, help=f"Append a {verb} declaration to this instance's file.")
        command.add_argument("instance")
        command.add_argument("resource")
        command.add_argument("--session", required=True)
        command.add_argument("--lane", action="append", default=[], dest="lanes")
        command.add_argument("--note", default="")
        command.add_argument("--goal-ref", action="append", default=[], dest="goal_refs")
        command.add_argument("--purpose", default="")
        command.add_argument("--journal-ref", default="", dest="journal_ref")

    listing = sub.add_parser("list", help="List current declarations merged across all writer files.")
    listing.add_argument("--resource", default=None)
    listing.add_argument("--instance", default=None)
    listing.add_argument("--all", action="store_true", help="Include each resource's latest released declaration.")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.verb == "using":
        peers = announce_using(
            args.instance,
            args.resource,
            session=args.session,
            lanes=args.lanes,
            note=args.note,
            goal_refs=args.goal_refs,
            purpose=args.purpose,
            journal_ref=args.journal_ref,
            root=args.shared_dir,
        )
        print(json.dumps({"peers_using": [record.to_dict() for record in peers]}, indent=2, sort_keys=True))
        return 0
    if args.verb == "released":
        record = announce_released(
            args.instance,
            args.resource,
            session=args.session,
            lanes=args.lanes,
            note=args.note,
            goal_refs=args.goal_refs,
            purpose=args.purpose,
            journal_ref=args.journal_ref,
            root=args.shared_dir,
        )
        print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
        return 0

    records = list_presence(root=args.shared_dir, include_released=args.all)
    if args.resource is not None:
        records = [record for record in records if record.resource == args.resource]
    if args.instance is not None:
        records = [record for record in records if record.instance == args.instance]
    print(json.dumps([record.to_dict() for record in records], indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_arg_parser().parse_args(argv))
    except (OSError, PresenceError) as exc:
        print(f"chitra-presence: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

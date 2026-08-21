"""Command-line interface for direct peer messages."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from chitra.peer import PeerMessageError, inbox, say
from chitra.presence import PresenceError


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chitra-peer", description="Send and read atomic file-per-message peer notes.")
    parser.add_argument("--shared-dir", type=Path, default=None, help="Coordination root; defaults to CHITRA_SHARED_DIR.")
    sub = parser.add_subparsers(dest="verb", required=True)

    sending = sub.add_parser("say", help="Write one message into a peer instance's inbox.")
    sending.add_argument("instance", help="Recipient instance.")
    sending.add_argument("text")
    sending.add_argument("--from-instance", default=None, dest="sender")
    sending.add_argument("--message-id", default=None)

    reading = sub.add_parser("inbox", help="Read an inbox in stable order without consuming it.")
    reading.add_argument("instance", nargs="?", default=None, help="Defaults to CHITRA_INSTANCE.")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.verb == "say":
        message = say(
            args.instance,
            args.text,
            sender=args.sender,
            message_id=args.message_id,
            root=args.shared_dir,
        )
        print(json.dumps(message.to_dict(), indent=2, sort_keys=True))
        return 0
    instance = args.instance or os.environ.get("CHITRA_INSTANCE")
    if instance is None:
        raise PeerMessageError("inbox instance is required when CHITRA_INSTANCE is unset")
    print(json.dumps([message.to_dict() for message in inbox(instance, root=args.shared_dir)], indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_arg_parser().parse_args(argv))
    except (OSError, PeerMessageError, PresenceError) as exc:
        print(f"chitra-peer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

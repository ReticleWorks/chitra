"""CLI for per-lane validation receipt storage and verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chitra.state_paths import state_dir
from chitra.validation_receipts import (
    ReceiptError,
    ingest_receipt,
    list_receipts,
    receipt_path,
    verify_receipt,
)


def _add_lane_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=state_dir(), help="Instance state root.")
    parser.add_argument("--session-ref", required=True, help="Exact enrolled lane session reference.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chitra-receipts", description="Store and verify validation receipts.")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="Verify and store one receipt for its exact enrolled item.")
    _add_lane_arguments(ingest)
    ingest.add_argument("receipt", type=Path)

    list_command = commands.add_parser("list", help="List stored receipts for one lane.")
    _add_lane_arguments(list_command)

    verify = commands.add_parser("verify", help="Recheck stored integrity, evidence, current target, and validator result.")
    _add_lane_arguments(verify)
    verify.add_argument("receipt_name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "ingest":
            path = ingest_receipt(args.root, args.session_ref, args.receipt)
            print(json.dumps({"receipt_name": path.stem, "path": str(path), "stored": True}, sort_keys=True))
        elif args.command == "list":
            rows = []
            for receipt in list_receipts(args.root, args.session_ref):
                rows.append(
                    {
                        "receipt_name": receipt.receipt_name,
                        "status": receipt.result["status"],
                        "validator": receipt.validator,
                        "path": str(receipt_path(args.root, args.session_ref, receipt.receipt_name)),
                    }
                )
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            result = verify_receipt(args.root, args.session_ref, args.receipt_name)
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0 if result.verified else 1
    except (OSError, ReceiptError, ValueError) as exc:
        print(f"chitra-receipts: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

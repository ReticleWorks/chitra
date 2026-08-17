"""Command-line surface for the deterministic goal store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chitra import board
from chitra import goals as goal_store
from chitra._fsio import parse_iso8601
from chitra.artifacts import list_unreviewed_artifacts
from chitra.close_gate import lint_done_when
from chitra.lane_read import extract_open_asks, read_last_assistant_message
from chitra.policy_config import load_policy_config, resolve_guidance
from chitra.state_paths import state_dir


def _print_record(record: goal_store.GoalRecord) -> None:
    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))


def _parse_id_value_pairs(items: list[str], flag: str) -> dict[str, str]:
    """Parse repeated ``id=value`` CLI arguments keyed by interview question id."""
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"{flag} must be id=<text>, got {item!r}")
        question_id, _, text = item.partition("=")
        if question_id not in goal_store.INTERVIEW_QUESTION_IDS:
            raise ValueError(f"{flag} id must be one of {', '.join(goal_store.INTERVIEW_QUESTION_IDS)}, got {question_id!r}")
        parsed[question_id] = text
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chitra-goals", description="Store deterministic monitor goal state and render its roster.")
    parser.add_argument("--root", type=Path, default=state_dir())
    commands = parser.add_subparsers(dest="command", required=True)

    def add_root(command: argparse.ArgumentParser) -> None:
        command.add_argument("--root", type=Path, default=argparse.SUPPRESS)

    set_command = commands.add_parser("set", help="Create or update a lane goal.")
    add_root(set_command)
    set_command.add_argument("--session-ref", required=True)
    set_command.add_argument("--goal", required=True)
    set_command.add_argument("--done-when", required=True)
    set_command.add_argument("--source", required=True)
    set_command.add_argument("--intent", default=None)
    set_command.add_argument("--scope", default=None)
    set_command.add_argument("--status", choices=goal_store.GOAL_STATUSES, default="working")
    set_command.add_argument("--now", default="")
    set_command.add_argument("--last-verified", default="")
    set_command.add_argument("--needs", default=None, help="Specific human action required to unblock this lane.")
    asks_group = set_command.add_mutually_exclusive_group()
    asks_group.add_argument("--open-ask", action="append", default=[])
    asks_group.add_argument("--clear-asks", action="store_true")

    get_command = commands.add_parser("get", help="Print one lane goal as JSON.")
    add_root(get_command)
    get_command.add_argument("--session-ref", required=True)

    list_command = commands.add_parser("list", help="List current lane goals.")
    add_root(list_command)
    list_command.add_argument("--json", action="store_true")

    close_command = commands.add_parser("close", help="Inventory-check and remove a closed lane goal.")
    add_root(close_command)
    close_command.add_argument("--session-ref", required=True)
    close_command.add_argument(
        "--delivered-item",
        action="append",
        default=[],
        help="Caller-verified delivered item; repeat once per delivered item.",
    )
    close_command.add_argument(
        "--close-note",
        action="append",
        default=[],
        help="Exact close note to check for follow-on/out-of-scope reclassification; repeat as needed.",
    )
    close_command.add_argument(
        "--operator-acknowledged-item",
        action="append",
        default=[],
        help="Required item the operator explicitly acknowledged may close without delivery; repeat as needed.",
    )
    close_command.add_argument(
        "--administrative",
        action="store_true",
        help="Discard a dead lane's record without a completion claim, bypassing the delivery-inventory gate.",
    )
    close_command.add_argument(
        "--reason",
        default=None,
        help="Required with --administrative: why this record is being closed without a completion claim.",
    )

    hold_command = commands.add_parser("hold", help="Hold an existing lane without discarding its goal.")
    add_root(hold_command)
    hold_command.add_argument("--session-ref", required=True)
    hold_command.add_argument("--reason", required=True)
    hold_command.add_argument("--resume-at", default="")

    transfer_command = commands.add_parser(
        "transfer",
        help="Hold a lane and scaffold its successor on the other backend. Writes records only; check still gates launch.",
    )
    add_root(transfer_command)
    transfer_command.add_argument("--session-ref", required=True)
    transfer_command.add_argument("--to-backend", required=True, choices=("claude", "codex"))
    transfer_command.add_argument("--digest", required=True, help="Handoff digest id the successor reads for its context.")
    transfer_command.add_argument("--reason", required=True, help="Hold reason, e.g. rate-limit:codex-weekly-hard-cap.")
    transfer_command.add_argument("--resume-at", default="", help="When the held original becomes due again.")

    resume_command = commands.add_parser("resume", help="Return an explicitly held lane to working state.")
    add_root(resume_command)
    resume_command.add_argument("--session-ref", required=True)

    redirect_command = commands.add_parser("redirect", help="Record a reasoned revision to a lane's strategic goal.")
    add_root(redirect_command)
    redirect_command.add_argument("--session-ref", required=True)
    redirect_command.add_argument("--reason", required=True)
    redirect_command.add_argument("--goal")
    redirect_command.add_argument("--done-when")
    redirect_command.add_argument("--intent")
    redirect_command.add_argument("--scope")
    redirect_command.add_argument("--source")

    now_command = commands.add_parser("now", help="Update only a lane's tactical current state.")
    add_root(now_command)
    now_command.add_argument("--session-ref", required=True)
    now_command.add_argument("--now")
    now_command.add_argument("--status", choices=goal_store.GOAL_STATUSES)
    now_command.add_argument("--last-verified")

    check_command = commands.add_parser("check", help="Check whether a lane meets the specification threshold.")
    add_root(check_command)
    check_command.add_argument("--session-ref", required=True)

    interview_command = commands.add_parser(
        "interview",
        help="Run or record the SHORT INTERVIEW ingestion gate for a lane (see CLAUDE.md Ingestion gate).",
    )
    add_root(interview_command)
    interview_command.add_argument("--session-ref", required=True)
    interview_command.add_argument(
        "--print-questions",
        action="store_true",
        help="Print the four fixed interview questions with their ids and exit; no write.",
    )
    interview_command.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="id=<answer text>",
        help=f"One interview answer, id in {{{', '.join(goal_store.INTERVIEW_QUESTION_IDS)}}}; repeat for all four.",
    )
    interview_command.add_argument(
        "--provenance",
        action="append",
        default=[],
        metavar="id=<provenance>",
        help='Provenance for the paired --answer id, "operator:<verbatim words>" or "source:<citation>"; repeat for all four.',
    )
    interview_command.add_argument(
        "--waive-legacy",
        action="store_true",
        help="Mark this record's interview waived as migrated-from-legacy. Always allowed; logged loudly.",
    )

    guidance_command = commands.add_parser("guidance", help="Locate canonical operator guidance for a working directory.")
    guidance_command.add_argument("--cwd", type=Path, required=True)
    guidance_command.add_argument("--show", action="store_true")

    due_command = commands.add_parser("due", help="List timed holds that are due for operator review.")
    add_root(due_command)
    due_command.add_argument("--now", default="")

    add_ask_command = commands.add_parser("add-ask", help="Add one persistent open operator ask to a lane.")
    add_root(add_ask_command)
    add_ask_command.add_argument("--session-ref", required=True)
    add_ask_command.add_argument("--ask", required=True)

    resolve_ask_command = commands.add_parser("resolve-ask", help="Explicitly retire persisted open operator asks.")
    add_root(resolve_ask_command)
    resolve_ask_command.add_argument("--session-ref", required=True)
    selectors = resolve_ask_command.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--ask")
    selectors.add_argument("--index", type=int)
    selectors.add_argument("--all", action="store_true")
    resolve_ask_command.add_argument("--retired-by", choices=("operator", "monitor"), default="operator")
    resolve_ask_command.add_argument("--basis", default="Operator answered the ask.")
    resolve_ask_command.add_argument("--citation", default="operator-ruling")
    resolve_ask_command.add_argument("--authority", default="operator")

    scan_asks_command = commands.add_parser("scan-asks", help="Extract verbatim open asks from a lane transcript.")
    add_root(scan_asks_command)
    scan_asks_command.add_argument("--transcript", type=Path, required=True)
    scan_asks_command.add_argument("--session-ref")
    scan_asks_command.add_argument("--record", action="store_true")

    roster_command = commands.add_parser("roster", help="Render the operator roster.")
    add_root(roster_command)
    roster_command.add_argument("--format", choices=("cards", "box", "markdown"), default=board.ROSTER_DEFAULT_FORMAT)
    roster_command.add_argument("--lint", action="store_true", help="Print optional board roster-lint advice to stderr.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "set":
            existing = goal_store.get_goal(args.root, args.session_ref)
            requested_record = goal_store.GoalRecord(
                session_ref=args.session_ref,
                goal=args.goal,
                done_when=args.done_when,
                source=args.source,
                status=args.status,
                intent=args.intent if args.intent is not None else (existing.intent if existing is not None else ""),
                scope=args.scope if args.scope is not None else (existing.scope if existing is not None else ""),
                now=args.now,
                last_verified=args.last_verified,
                open_asks=tuple(args.open_ask),
                needs=args.needs if args.needs is not None else (existing.needs if existing is not None else ""),
            )
            stored = goal_store.upsert_goal(args.root, requested_record, clear_open_asks=args.clear_asks)
            done_when_flag = lint_done_when(stored.done_when)
            if done_when_flag is not None:
                stored = goal_store.add_ask(args.root, stored.session_ref, done_when_flag.message)
            _print_record(stored)
        elif args.command == "get":
            found_record = goal_store.get_goal(args.root, args.session_ref)
            if found_record is None:
                raise goal_store.GoalNotFoundError(args.session_ref)
            _print_record(found_record)
        elif args.command == "list":
            records = goal_store.list_goals(args.root)
            if args.json:
                print(json.dumps([record.to_dict() for record in records], indent=2, sort_keys=True))
            else:
                for record in records:
                    print(f"{record.session_ref}\t{record.status}\t{record.goal}\t{json.dumps(list(record.open_asks))}")
        elif args.command == "close":
            if args.administrative and not (args.reason or "").strip():
                raise ValueError("--administrative requires a non-empty --reason")
            _print_record(
                goal_store.close_goal(
                    args.root,
                    args.session_ref,
                    delivered_items=tuple(args.delivered_item),
                    close_notes=tuple(args.close_note),
                    operator_acknowledged_items=tuple(args.operator_acknowledged_item),
                    administrative=args.administrative,
                    administrative_reason=args.reason or "",
                )
            )
        elif args.command == "hold":
            _print_record(goal_store.hold_goal(args.root, args.session_ref, reason=args.reason, resume_at=args.resume_at))
        elif args.command == "transfer":
            held, successor = goal_store.transfer_goal(
                args.root,
                args.session_ref,
                to_backend=args.to_backend,
                digest=args.digest,
                reason=args.reason,
                resume_at=args.resume_at,
            )
            _print_record(held)
            _print_record(successor)
        elif args.command == "resume":
            _print_record(goal_store.resume_goal(args.root, args.session_ref))
        elif args.command == "redirect":
            _print_record(
                goal_store.redirect_goal(
                    args.root,
                    args.session_ref,
                    reason=args.reason,
                    goal=args.goal,
                    done_when=args.done_when,
                    intent=args.intent,
                    scope=args.scope,
                    source=args.source,
                )
            )
        elif args.command == "now":
            _print_record(
                goal_store.update_now(
                    args.root,
                    args.session_ref,
                    now=args.now,
                    status=args.status,
                    last_verified=args.last_verified,
                )
            )
        elif args.command == "check":
            found_record = goal_store.get_goal(args.root, args.session_ref)
            if found_record is None:
                raise goal_store.GoalNotFoundError(args.session_ref)
            specification_issues = goal_store.check_specification(found_record)
            if specification_issues:
                print("\n".join(specification_issues))
                return 1
            print("well-specified")
        elif args.command == "interview":
            if args.print_questions:
                for question_id in goal_store.INTERVIEW_QUESTION_IDS:
                    print(f"{question_id}: {goal_store.INTERVIEW_QUESTIONS[question_id]}")
            else:
                stored = None
                if args.waive_legacy:
                    stored = goal_store.waive_interview(args.root, args.session_ref)
                    print(f"interview waived: {stored.interview_waiver}", file=sys.stderr)
                if args.answer or args.provenance:
                    answers_raw = _parse_id_value_pairs(args.answer, "--answer")
                    provenance_raw = _parse_id_value_pairs(args.provenance, "--provenance")
                    missing_provenance = [question_id for question_id in answers_raw if question_id not in provenance_raw]
                    if missing_provenance:
                        raise ValueError(f"--provenance missing for: {', '.join(missing_provenance)}")
                    extra_provenance = [question_id for question_id in provenance_raw if question_id not in answers_raw]
                    if extra_provenance:
                        raise ValueError(f"--provenance given without a matching --answer for: {', '.join(extra_provenance)}")
                    answers = {question_id: (answers_raw[question_id], provenance_raw[question_id]) for question_id in answers_raw}
                    stored = goal_store.record_interview(args.root, args.session_ref, answers=answers)
                if stored is None:
                    found_record = goal_store.get_goal(args.root, args.session_ref)
                    if found_record is None:
                        raise goal_store.GoalNotFoundError(args.session_ref)
                    stored = found_record
                _print_record(stored)
                specification_issues = goal_store.check_specification(stored)
                if specification_issues:
                    print("\n".join(specification_issues))
                    return 1
                waiver_suffix = f" (interview waived: {stored.interview_waiver})" if stored.interview_waiver else ""
                print(f"well-specified{waiver_suffix}")
        elif args.command == "guidance":
            guidance_path = resolve_guidance(load_policy_config(), args.cwd)
            if guidance_path is None:
                raise ValueError(f"no guidance is configured for {args.cwd}")
            if not guidance_path.is_file():
                raise ValueError(f"configured guidance file is missing: {guidance_path}")
            if args.show:
                print(guidance_path.read_text(encoding="utf-8"), end="")
            else:
                print(guidance_path)
        elif args.command == "due":
            due_now = (
                parse_iso8601(
                    args.now,
                    invalid_message="resume_at must be an ISO8601 datetime",
                    timezone_message="resume_at must be an ISO8601 datetime with timezone",
                    require_timezone=True,
                )
                if args.now
                else None
            )
            print(json.dumps([record.to_dict() for record in goal_store.due_goals(args.root, now=due_now)], indent=2, sort_keys=True))
        elif args.command == "add-ask":
            _print_record(goal_store.add_ask(args.root, args.session_ref, args.ask))
        elif args.command == "resolve-ask":
            _print_record(
                goal_store.resolve_ask(
                    args.root,
                    args.session_ref,
                    ask=args.ask,
                    index=args.index,
                    all=args.all,
                    retired_by=args.retired_by,
                    basis=args.basis,
                    citation=args.citation,
                    authority=args.authority,
                )
            )
        elif args.command == "scan-asks":
            if args.record and args.session_ref is None:
                raise ValueError("--record requires --session-ref")
            asks = extract_open_asks(read_last_assistant_message(args.transcript))
            for ask in asks:
                print(ask)
                if args.record:
                    assert args.session_ref is not None
                    goal_store.add_ask(args.root, args.session_ref, ask)
        else:
            records = goal_store.list_goals(args.root)
            print(board.render_roster(records, fmt=args.format, artifacts=list_unreviewed_artifacts(args.root)))
            if args.lint:
                roster_lint = getattr(board, "roster_lint", None)
                if roster_lint is not None:
                    for issue in roster_lint(records):
                        print(issue, file=sys.stderr)
    except goal_store.GoalRedirectRequiredError as exc:
        print(f"chitra-goals: {exc}; use chitra-goals redirect --reason ...", file=sys.stderr)
        return 1
    except (goal_store.GoalValidationError, goal_store.GoalNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-goals: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line surface for the deterministic goal store."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shlex
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chitra import board
from chitra import goals as goal_store
from chitra._fsio import locked_json_store, parse_iso8601, write_json_atomic
from chitra.artifacts import list_unreviewed_artifacts
from chitra.autonomy import DEFAULT_AUTONOMY_POLICY, AutonomyPolicy, load_autonomy_policy_json
from chitra.completion_gate import CompletionEvidence
from chitra.lane_read import extract_open_asks, read_last_assistant_message
from chitra.policy_config import load_policy_config, resolve_guidance
from chitra.state_paths import state_dir

_DONE_ITEM_KEYS = ("id", "text", "validator", "receipt")


def _autonomy_policy_from_args(
    args: argparse.Namespace,
    *,
    default: AutonomyPolicy | None,
) -> AutonomyPolicy | None:
    policy_path = getattr(args, "autonomy_policy", None)
    policy_json = getattr(args, "autonomy_policy_json", None)
    if policy_path is not None:
        return load_autonomy_policy_json(policy_path.read_text(encoding="utf-8"))
    if policy_json is not None:
        return load_autonomy_policy_json(policy_json)
    return default


def _add_autonomy_policy_args(command: argparse.ArgumentParser) -> None:
    group = command.add_mutually_exclusive_group()
    group.add_argument(
        "--autonomy-policy",
        type=Path,
        help="Strict chitra.autonomy.v1 JSON file to freeze into this goal.",
    )
    group.add_argument(
        "--autonomy-policy-json",
        help="Strict inline chitra.autonomy.v1 JSON to freeze into this goal.",
    )


def _parse_done_item_specs(specs: Sequence[str]) -> tuple[goal_store.EnrolledDoneWhenItem, ...]:
    """Parse repeated ``--done-item`` specs into frozen done items."""
    items: list[goal_store.EnrolledDoneWhenItem] = []
    for position, spec in enumerate(specs, start=1):
        fields: dict[str, str] = {}
        try:
            tokens = shlex.split(spec)
        except ValueError as exc:
            raise ValueError(f"--done-item {position} is not parsable: {exc}") from exc
        for token in tokens:
            key, separator, value = token.partition("=")
            if not separator or key not in _DONE_ITEM_KEYS or key in fields:
                raise ValueError(
                    f"--done-item {position} has malformed token {token!r}; "
                    f"expected one key=value per token with keys {' '.join(_DONE_ITEM_KEYS)}"
                )
            fields[key] = value
        missing = [key for key in _DONE_ITEM_KEYS if key not in fields]
        if missing:
            raise ValueError(f"--done-item {position} is missing {', '.join(missing)}")
        items.append(
            goal_store.EnrolledDoneWhenItem(
                id=fields["id"],
                text=fields["text"],
                validator=fields["validator"],
                required_receipt=fields["receipt"],
            )
        )
    return tuple(items)


def _interview_nonce_path(root: Path, session_ref: str) -> Path:
    token = hashlib.sha256(session_ref.encode("utf-8")).hexdigest()
    return root / "goal-interviews" / f"{token}.json"


def _set_request_sha256(args: argparse.Namespace) -> str:
    autonomy_policy = _autonomy_policy_from_args(args, default=DEFAULT_AUTONOMY_POLICY)
    assert autonomy_policy is not None
    payload = {
        "session_ref": args.session_ref,
        "goal": args.goal,
        "done_when": args.done_when,
        "done_items": list(getattr(args, "done_item", ()) or ()),
        "source": args.source,
        "intent": args.intent,
        "scope": args.scope,
        "status": args.status,
        "now": args.now,
        "last_verified": args.last_verified,
        "needs": args.needs,
        "open_asks": args.open_ask,
        "autonomy_policy": autonomy_policy.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _interview_required(root: Path, args: argparse.Namespace) -> dict[str, object]:
    nonce_path = _interview_nonce_path(root, args.session_ref)
    request_sha256 = _set_request_sha256(args)
    with locked_json_store(nonce_path):
        persisted: dict[str, Any] = {}
        try:
            loaded = json.loads(nonce_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                persisted = loaded
        except FileNotFoundError:
            pass
        if persisted.get("request_sha256") != request_sha256 or not isinstance(persisted.get("nonce"), str):
            receipt_name = "interview:" + hashlib.sha256(args.session_ref.encode("utf-8")).hexdigest()[:16]
            persisted = {
                "type": "INTERVIEW_NONCE",
                "session_ref": args.session_ref,
                "nonce": secrets.token_urlsafe(24),
                "receipt_name": receipt_name,
                "request_sha256": request_sha256,
                "created_at": datetime.now(UTC).isoformat(),
                "consumed_at": "",
            }
            write_json_atomic(nonce_path, persisted)
    return {
        "type": "INTERVIEW_REQUIRED",
        "session_ref": args.session_ref,
        "nonce": persisted["nonce"],
        "receipt_name": persisted["receipt_name"],
        "questions": [
            {"id": question_id, "text": goal_store.INTERVIEW_QUESTIONS[question_id]} for question_id in goal_store.INTERVIEW_QUESTION_IDS
        ],
    }


def _read_nonce_record(nonce_path: Path) -> dict[str, Any] | None:
    """Read the persisted nonce record under an already-held nonce lock."""
    try:
        loaded: Any = json.loads(nonce_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _parse_interview_result(
    root: Path, args: argparse.Namespace
) -> tuple[
    goal_store.InterviewReceipt,
    tuple[goal_store.EnrolledDoneWhenItem, ...],
    dict[str, str],
    Path,
    dict[str, Any],
]:
    payload = json.loads(args.interview_result.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("type") not in (None, "INTERVIEW_RESULT"):
        raise ValueError("--interview-result must contain an INTERVIEW_RESULT JSON object")
    nonce_path = _interview_nonce_path(root, args.session_ref)
    nonce_record = json.loads(nonce_path.read_text(encoding="utf-8"))
    if not isinstance(nonce_record, dict) or nonce_record.get("session_ref") != args.session_ref:
        raise ValueError("interview nonce does not belong to this session")
    if nonce_record.get("request_sha256") != _set_request_sha256(args):
        raise ValueError("interview nonce does not match this set request")
    if nonce_record.get("consumed_at"):
        raise ValueError("interview nonce was already consumed")
    if not secrets.compare_digest(str(payload.get("nonce", "")), str(nonce_record.get("nonce", ""))):
        raise ValueError("interview nonce does not match")
    supplied_receipt_name = payload.get("receipt_name")
    if supplied_receipt_name is not None and supplied_receipt_name != nonce_record.get("receipt_name"):
        raise ValueError("interview receipt name does not match the issued name")

    raw_answers = payload.get("answers")
    normalized_answers: dict[str, dict[str, str]] = {}
    if isinstance(raw_answers, list):
        for entry in raw_answers:
            if not isinstance(entry, dict):
                raise ValueError("interview answers must be objects")
            question = entry.get("question", entry.get("id"))
            if not isinstance(question, str):
                raise ValueError("interview answer question must be a string")
            normalized_answers[question] = {
                "answer": str(entry.get("answer", "")),
                "provenance": str(entry.get("provenance", "")),
            }
    elif isinstance(raw_answers, dict):
        for question, entry in raw_answers.items():
            if not isinstance(question, str) or not isinstance(entry, dict):
                raise ValueError("interview answers must map question ids to objects")
            normalized_answers[question] = {
                "answer": str(entry.get("answer", "")),
                "provenance": str(entry.get("provenance", "")),
            }
    else:
        raise ValueError("interview result answers must contain all four typed answers")

    if set(normalized_answers) != set(goal_store.INTERVIEW_QUESTION_IDS):
        raise ValueError("interview result must answer exactly intent, done_when, out_of_scope, and constraints")
    ordered_answers: list[dict[str, str]] = []
    for question in goal_store.INTERVIEW_QUESTION_IDS:
        entry = normalized_answers[question]
        if not entry["answer"].strip():
            raise ValueError(f"interview answer for {question!r} must be non-empty")
        provenance = entry["provenance"].strip()
        if not provenance.startswith(("operator:", "source:")) or not provenance.partition(":")[2].strip():
            raise ValueError(f"interview answer for {question!r} requires operator: or source: provenance")
        ordered_answers.append({"question": question, "answer": entry["answer"].strip(), "provenance": provenance})

    raw_items = payload.get("enrolled_done_when_items", payload.get("done_when_items"))
    done_item_specs = _parse_done_item_specs(args.done_item)
    if done_item_specs:
        items = done_item_specs
    else:
        if not isinstance(raw_items, list):
            raise ValueError("interview result enrolled_done_when_items must be a list")
        try:
            items = tuple(goal_store.EnrolledDoneWhenItem(**item) for item in raw_items if isinstance(item, dict))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid enrolled done item: {exc}") from exc
        if len(items) != len(raw_items):
            raise ValueError("interview result done items must be objects")
        if not items:
            raise ValueError("interview result enrolled_done_when_items must contain at least one item")
    answers_sha256 = hashlib.sha256(json.dumps(ordered_answers, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    receipt = goal_store.InterviewReceipt(
        name=str(nonce_record["receipt_name"]),
        completed_at=datetime.now(UTC).isoformat(),
        answers_sha256=answers_sha256,
        provenance=tuple(entry["provenance"] for entry in ordered_answers),
    )
    answer_values = {entry["question"]: entry["answer"] for entry in ordered_answers}
    return receipt, items, answer_values, nonce_path, nonce_record


def _load_completion_evidence(path: Path | None) -> tuple[CompletionEvidence, ...]:
    if path is None:
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("--completion-evidence must contain a JSON list")
    return tuple(CompletionEvidence.model_validate(item) for item in payload)


def _print_record(record: goal_store.GoalRecord) -> None:
    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))


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
    set_command.add_argument("--done-when", default="", help="Display-only proposal; interviewed done items become the stored value.")
    set_command.add_argument(
        "--done-item",
        action="append",
        default=[],
        metavar="ID=<id> TEXT=<...> VALIDATOR=<registry-name> RECEIPT=<name>",
        help=(
            "Structured frozen done condition for a new enrollment; repeat once per item. "
            "Quote values containing spaces. Free-text --done-when is refused on a new record."
        ),
    )
    set_command.add_argument("--source", required=True)
    set_command.add_argument("--intent", default=None)
    set_command.add_argument("--scope", default=None)
    set_command.add_argument("--status", choices=goal_store.GOAL_STATUSES, default="working")
    set_command.add_argument("--now", default="")
    set_command.add_argument("--last-verified", default="")
    _add_autonomy_policy_args(set_command)
    set_command.add_argument("--interview-result", type=Path)
    set_command.add_argument(
        "--migrate",
        action="store_true",
        help="Rewrite goals.json at this package's schema when its file schema is newer.",
    )
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
        help="Legacy input retained for a typed refusal; it cannot satisfy completion close.",
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
        help="Legacy input retained for a typed refusal; it cannot satisfy completion close.",
    )
    close_command.add_argument(
        "--completion-evidence",
        type=Path,
        help="JSON list of structured completion receipts. Stored Watchd receipts are used when omitted.",
    )
    close_command.add_argument(
        "--administrative",
        action="store_true",
        help="Discard the record without claiming the work is done.",
    )
    close_command.add_argument("--reason", default="", help="Required reason for --administrative discard.")

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
    _add_autonomy_policy_args(redirect_command)

    now_command = commands.add_parser("now", help="Update only a lane's tactical current state.")
    add_root(now_command)
    now_command.add_argument("--session-ref", required=True)
    now_command.add_argument("--now")
    now_command.add_argument("--status", choices=goal_store.GOAL_STATUSES)
    now_command.add_argument("--last-verified")

    check_command = commands.add_parser("check", help="Check whether a lane meets the specification threshold.")
    add_root(check_command)
    check_command.add_argument("--session-ref", required=True)

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


def _enroll_from_interview_result(root: Path, args: argparse.Namespace) -> goal_store.GoalRecord:
    """Verify the interview nonce and enroll the goal as one lock-governed transaction.

    The goals lock is held across nonce re-verification, provenance/item
    construction, the goal commit, and the nonce consumption write, so a
    replacement nonce issued concurrently by a changed ``set`` request can
    neither be accepted by this stale parsed result nor overwritten by its
    consumption marker.  The caller has already established that no enrolled
    goal exists, and that fact is re-checked under the lock.

    ``allow_strategic_change`` only widens the pre-existing-goal guard inside
    ``_upsert_goal_locked``; with no existing record there is nothing to
    compare strategic fields against, and a genuine stale second enrollment
    still fails because the re-checked ``existing`` guard above raises first.
    The final nonce comparison and the consumption write are one critical
    section under the nonce lock itself (nested inside the fixed
    goals→nonce order, the same order issuance uses), so a replacement nonce
    cannot slip in between that comparison and consumption: either the
    verified nonce is still current when it is consumed, or the just-written
    goal document is rolled back to its exact pre-commit bytes under the same
    goals lock and the enrollment fails closed.  Crash safety (atomic file
    replacement throughout) is unchanged.
    """
    with locked_json_store(goal_store.goals_path(root)):
        existing = goal_store.get_goal(args.root, args.session_ref)
        if existing is not None:
            raise goal_store.GoalValidationError("goal is already enrolled; its interview receipt and done items are frozen")
        receipt, done_items, answers, nonce_path, nonce_record = _parse_interview_result(args.root, args)
        done_when = goal_store.render_done_when_items(done_items)
        intent = answers["intent"]
        scope = f"Out of scope: {answers['out_of_scope']} Constraints: {answers['constraints']}"
        autonomy_policy = _autonomy_policy_from_args(args, default=DEFAULT_AUTONOMY_POLICY)
        assert autonomy_policy is not None
        requested_record = goal_store.GoalRecord(
            session_ref=args.session_ref,
            goal=args.goal,
            done_when=done_when,
            source=args.source,
            status=args.status,
            intent=intent,
            scope=scope,
            now=args.now,
            last_verified=args.last_verified,
            open_asks=tuple(args.open_ask),
            needs=args.needs if args.needs is not None else "",
            interview_receipt=receipt,
            enrolled_done_when_items=done_items,
            completion_proofs=(),
            autonomy_policy=autonomy_policy,
        )
        pre_commit_payload: dict[str, Any] | None
        try:
            loaded_document: Any = json.loads(goal_store.goals_path(root).read_text(encoding="utf-8"))
            pre_commit_payload = loaded_document if isinstance(loaded_document, dict) else None
        except FileNotFoundError:
            pre_commit_payload = None
        stored = goal_store._upsert_goal_locked(args.root, requested_record, allow_strategic_change=True, migrate=args.migrate)
        try:
            with locked_json_store(nonce_path):
                persisted_nonce_record = _read_nonce_record(nonce_path)
                if persisted_nonce_record is None or persisted_nonce_record.get("nonce") != nonce_record["nonce"]:
                    raise ValueError("interview nonce was replaced before enrollment committed")
                nonce_record["consumed_at"] = stored.enrolled_at
                write_json_atomic(nonce_path, nonce_record)
        except ValueError:
            if pre_commit_payload is None:
                (goal_store.goals_path(root)).unlink(missing_ok=True)
            else:
                write_json_atomic(goal_store.goals_path(root), pre_commit_payload)
            raise
    return stored


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "set":
            existing = goal_store.get_goal(args.root, args.session_ref)
            if existing is None and args.done_when.strip():
                raise goal_store.GoalValidationError(
                    "free-text --done-when is refused on a new record; enroll structured --done-item conditions"
                )
            if existing is not None and args.done_item:
                raise goal_store.GoalValidationError("--done-item applies only to a new enrollment")
            if existing is None and args.interview_result is None:
                print(json.dumps(_interview_required(args.root, args), indent=2, sort_keys=True))
                return 2
            if existing is not None and existing.interview_receipt is None:
                raise goal_store.GoalValidationError("legacy goals are display-only; use a reasoned administrative redirect or discard")
            if existing is not None and args.interview_result is not None:
                raise goal_store.GoalValidationError("goal is already enrolled; its interview receipt and done items are frozen")

            if existing is not None:
                requested_autonomy_policy = _autonomy_policy_from_args(args, default=existing.autonomy_policy)
                assert requested_autonomy_policy is not None
                stored = goal_store.upsert_goal(
                    args.root,
                    goal_store.GoalRecord(
                        session_ref=args.session_ref,
                        goal=args.goal,
                        done_when=args.done_when or existing.done_when,
                        source=args.source,
                        status=args.status,
                        intent=args.intent if args.intent is not None else existing.intent,
                        scope=args.scope if args.scope is not None else existing.scope,
                        now=args.now,
                        last_verified=args.last_verified,
                        open_asks=tuple(args.open_ask),
                        needs=args.needs if args.needs is not None else existing.needs,
                        interview_receipt=existing.interview_receipt,
                        enrolled_done_when_items=existing.enrolled_done_when_items,
                        completion_proofs=existing.completion_proofs,
                        autonomy_policy=requested_autonomy_policy,
                    ),
                    clear_open_asks=args.clear_asks,
                    migrate=args.migrate,
                )
            else:
                stored = _enroll_from_interview_result(args.root, args)
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
            if args.administrative and not args.reason.strip():
                raise ValueError("--administrative requires a non-empty --reason")
            _print_record(
                goal_store.close_goal(
                    args.root,
                    args.session_ref,
                    delivered_items=tuple(args.delivered_item),
                    completion_evidence=_load_completion_evidence(args.completion_evidence),
                    close_notes=tuple(args.close_note),
                    operator_acknowledged_items=tuple(args.operator_acknowledged_item),
                    administrative=args.administrative,
                    administrative_reason=args.reason,
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
            redirected_autonomy_policy = _autonomy_policy_from_args(args, default=None)
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
                    autonomy_policy=redirected_autonomy_policy,
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
    except goal_store.GoalsSchemaNewerError as exc:
        print(f"chitra-goals: {exc}", file=sys.stderr)
        print("chitra-goals: re-run chitra-goals set with --migrate to rewrite goals.json at the installed schema", file=sys.stderr)
        return 3
    except (goal_store.GoalValidationError, goal_store.GoalNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-goals: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

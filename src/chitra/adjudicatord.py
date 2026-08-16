"""adjudicatord -- settle blocker claims before they become operator questions.

Every sweep, this daemon reads the goal records and does two things. It repairs
any record that fails its specification check, by running the short interview
against that record's own primary source. Then it turns each open ask, each
recorded ``needs`` line, and each interview question the source could not answer
into a claim, and settles it.

A claim the evidence refutes becomes a directive back to the session through the
existing dispatch queue, and its ask is retired. A claim that is genuinely about
physical presence, spend, or a change of the agreed scope becomes one operator
brief. A claim nothing settles is recorded as undecided and left visible.

The daemon writes dispatch orders into the queue the delivery daemon already
drains. It never touches a tmux pane itself, and it never merges anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from chitra.adjudication import (
    REFUSING_VERDICTS,
    Adjudication,
    AdjudicationContext,
    BlockerAdjudicator,
    BlockerClaim,
    ClaudeProcessAdjudicator,
    EvidenceSources,
    adjudicate,
    decision_entry,
    escalation_brief,
    load_evidence_sources,
)
from chitra.capabilities import CapabilityDisabledError, CapabilityError, require_enabled
from chitra.convlog import BriefValidationError, open_thread
from chitra.decisions import append_decision
from chitra.dispatch import directive_voice_violation, enqueue_dispatch_order
from chitra.goals import (
    PRESUMED_ASK_PREFIX,
    GoalNotFoundError,
    GoalRecord,
    GoalValidationError,
    check_specification,
    lane_id_from_session_ref,
    list_goals,
    resolve_ask,
)
from chitra.interview import presumptive_repair
from chitra.orders import DispatchOrder
from chitra.policy_config import load_policy_config, resolve_guidance
from chitra.state_paths import (
    default_adjudication_log_path,
    default_convlog_path,
    default_decisions_path,
    default_queue_dir,
)
from chitra.state_paths import state_dir as default_state_dir

logger = structlog.get_logger(__name__)

DEFAULT_POLL_SECONDS = 120.0
TRANSCRIPT_NAME = "tmux-transcript.log"
#: The manifest entry that arms this daemon. It ships disabled, so the package
#: can be installed and inspected while the operator's hold is still on.
CAPABILITY_NAME = "blocker-adjudication"
DEFAULT_AUTHORITY = (
    "The obstacle adjudication service decided this under the standing operator direction "
    "that only physical presence, spend, and a change of agreed scope may be asked upward."
)
DEFAULT_PROGRAM = "Fleet work session oversight"
CANONICAL_DECISION_LIMIT = 40


class AdjudicationRunError(RuntimeError):
    """Raised when a run cannot proceed against the supplied state."""


@dataclass(frozen=True, slots=True)
class AdjudicatordConfig:
    """Every path and interval one adjudication process needs."""

    state_dir: Path
    queue_dir: Path
    convlog_path: Path
    decisions_path: Path
    adjudication_log_path: Path
    fleet_usage_dir: Path | None
    transcript_root: Path | None
    poll_seconds: float
    dry_run: bool
    repair_specifications: bool
    program: str
    authority: str


@dataclass(frozen=True, slots=True)
class RunReport:
    """What one pass over the recorded claims did."""

    claims_seen: int
    already_settled: int
    refused: int
    escalated: int
    undetermined: int

    def to_dict(self) -> dict[str, int]:
        return {
            "claims_seen": self.claims_seen,
            "already_settled": self.already_settled,
            "refused": self.refused,
            "escalated": self.escalated,
            "undetermined": self.undetermined,
        }


def resolve_config(
    *,
    state_dir: Path | None = None,
    queue_dir: Path | None = None,
    convlog_path: Path | None = None,
    decisions_path: Path | None = None,
    adjudication_log_path: Path | None = None,
    fleet_usage_dir: Path | None = None,
    transcript_root: Path | None = None,
    poll_seconds: float | None = None,
    dry_run: bool = False,
    repair_specifications: bool = True,
    program: str = DEFAULT_PROGRAM,
    authority: str = DEFAULT_AUTHORITY,
) -> AdjudicatordConfig:
    """Resolve arguments against the shipped defaults."""
    resolved_state_dir = state_dir or default_state_dir()
    resolved_poll = DEFAULT_POLL_SECONDS if poll_seconds is None else poll_seconds
    if resolved_poll <= 0:
        raise ValueError("poll_seconds must be a positive number")
    return AdjudicatordConfig(
        state_dir=resolved_state_dir,
        queue_dir=queue_dir or default_queue_dir(),
        convlog_path=convlog_path or default_convlog_path(),
        decisions_path=decisions_path or default_decisions_path(),
        adjudication_log_path=adjudication_log_path or default_adjudication_log_path(),
        fleet_usage_dir=fleet_usage_dir,
        transcript_root=transcript_root,
        poll_seconds=resolved_poll,
        dry_run=dry_run,
        repair_specifications=repair_specifications,
        program=program,
        authority=authority,
    )


def collect_claims(records: Sequence[GoalRecord], *, now: datetime | None = None) -> list[BlockerClaim]:
    """Turn every recorded open ask and needs line into one claim.

    A presumption recorded by the short interview is skipped. It is a standing
    invitation to correct a derived value, not a report that anything is stuck,
    and adjudicating it would answer a question nobody asked.
    """
    observed_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    claims: list[BlockerClaim] = []
    for record in records:
        for ask in record.open_asks:
            if ask.startswith(PRESUMED_ASK_PREFIX):
                continue
            if ask.strip():
                claims.append(
                    BlockerClaim(session_ref=record.session_ref, text=ask.strip(), origin="open_ask", observed_at=observed_at)
                )
        if record.needs.strip():
            claims.append(
                BlockerClaim(session_ref=record.session_ref, text=record.needs.strip(), origin="needs", observed_at=observed_at)
            )
    return claims


def repair_specifications(root: Path, records: Sequence[GoalRecord], *, now: datetime | None = None) -> list[BlockerClaim]:
    """Run the short interview on every record that fails its check.

    Each record is repaired from its own primary source so the work can start,
    and every question the source could not answer comes back as a claim for
    adjudication. Only what survives adjudication as physical presence, spend,
    or a change of agreed scope ever becomes a question for the operator.
    """
    observed_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    claims: list[BlockerClaim] = []
    for record in records:
        if not check_specification(record):
            continue
        try:
            outcome = presumptive_repair(root, record.session_ref, now=now)
        except (GoalNotFoundError, GoalValidationError, OSError, ValueError) as exc:
            logger.warning("adjudication_specification_repair_failed", session_ref=record.session_ref, error=str(exc))
            continue
        for unanswered in outcome.unanswered:
            claims.append(
                BlockerClaim(
                    session_ref=record.session_ref,
                    text=f"{unanswered.question.question} The recorded primary source does not settle it: {unanswered.reason}",
                    origin="interview",
                    observed_at=observed_at,
                )
            )
    return claims


def settled_claim_ids(path: Path) -> set[str]:
    """Return the claims already recorded, so one claim is settled once."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    settled: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            logger.warning("adjudication_record_unparseable", path=str(path))
            continue
        claim_id = payload.get("claim_id")
        if isinstance(claim_id, str):
            settled.add(claim_id)
    return settled


def append_adjudication(path: Path, adjudication: Adjudication) -> None:
    """Append one adjudication record, keyed by the claim it settled."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = adjudication.model_dump(mode="json", by_alias=True)
    payload["claim_id"] = adjudication.claim.claim_id
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _order_id(adjudication: Adjudication) -> str:
    digest = hashlib.sha256(adjudication.claim.claim_id.encode("utf-8")).hexdigest()
    return f"adjudicated-{digest[:20]}"


def deliverable_directive(adjudication: Adjudication) -> str:
    """Return a directive the delivery guard will accept, or an empty string.

    A recorded ruling quoted back verbatim can contain wording the delivery
    guard refuses, because that guard exists to stop this software speaking in
    the operator's voice. When the quote trips it, the directive keeps the
    citation and drops the quote rather than losing the delivery.
    """
    directive = adjudication.directive.strip()
    if not directive:
        return ""
    if directive_voice_violation(directive) is None:
        return directive
    if not adjudication.evidence:
        return ""
    first = adjudication.evidence[0]
    fallback = (
        f"The obstacle reported here does not hold. The record at {first.reference} settles it. "
        "Continue against the recorded goal."
    )
    return fallback if directive_voice_violation(fallback) is None else ""


def build_directive_order(adjudication: Adjudication) -> DispatchOrder:
    """Build the plain relay order that carries a refusal back to the session."""
    if adjudication.verdict not in REFUSING_VERDICTS:
        raise AdjudicationRunError("only a refused block produces a directive order")
    directive = deliverable_directive(adjudication)
    if not directive:
        raise AdjudicationRunError("the directive cannot be delivered without speaking in the operator's voice")
    return DispatchOrder(
        order_id=_order_id(adjudication),
        session_ref=adjudication.claim.session_ref,
        nudge=directive,
        task_type="blocker-adjudication",
        created_at=adjudication.adjudicated_at,
    )


def build_context(record: GoalRecord, sources: EvidenceSources) -> AdjudicationContext:
    """Assemble the goal, doctrine, and canonical-decision layers for one claim."""
    doctrine = ""
    try:
        guidance_path = resolve_guidance(load_policy_config(), Path.cwd())
        if guidance_path is not None and guidance_path.is_file():
            doctrine = guidance_path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        logger.warning("adjudication_doctrine_unreadable", error=str(exc))
    canonical = tuple(
        f"{entry.decision} Basis: {entry.basis} Citation: {entry.citation}" for entry in sources.decisions[-CANONICAL_DECISION_LIMIT:]
    )
    return AdjudicationContext(goal=record, doctrine=doctrine, canonical_decisions=canonical)


def _retire_ask(state_dir: Path, adjudication: Adjudication, *, authority: str) -> None:
    """Retire the recorded ask a refusal has now answered.

    The retirement is recorded as the monitor's, never the operator's. The
    operator did not answer this question, and the record must not say so.
    """
    if adjudication.claim.origin != "open_ask":
        return
    citation = (
        f"{adjudication.evidence[0].source}: {adjudication.evidence[0].reference}"
        if adjudication.evidence
        else f"claim {adjudication.claim.claim_id}"
    )
    try:
        resolve_ask(
            state_dir,
            adjudication.claim.session_ref,
            ask=adjudication.claim.text,
            retired_by="monitor",
            basis=adjudication.basis,
            citation=citation,
            authority=authority,
        )
    except (GoalNotFoundError, GoalValidationError, OSError, ValueError) as exc:
        logger.warning(
            "adjudication_ask_retirement_failed",
            session_ref=adjudication.claim.session_ref,
            error=str(exc),
        )


def _raise_to_operator(config: AdjudicatordConfig, adjudication: Adjudication) -> None:
    """Open one operator thread for a question only the operator can answer."""
    source_ref = f"{adjudication.claim.session_ref} {adjudication.claim.origin}"
    try:
        brief = escalation_brief(adjudication, program=config.program, source_ref=source_ref)
        open_thread(config.convlog_path, brief=brief, raw_text=adjudication.claim.text)
    except (BriefValidationError, OSError, ValueError) as exc:
        logger.warning("adjudication_brief_failed", claim_id=adjudication.claim.claim_id, error=str(exc))


def run_once(
    config: AdjudicatordConfig,
    *,
    adjudicator: BlockerAdjudicator | None = None,
    now: datetime | None = None,
) -> RunReport:
    """Settle every unsettled claim once, and act on each outcome."""
    records = list_goals(config.state_dir)
    interview_claims: list[BlockerClaim] = []
    if config.repair_specifications and not config.dry_run:
        interview_claims = repair_specifications(config.state_dir, records, now=now)
        if interview_claims:
            records = list_goals(config.state_dir)
    by_ref = {record.session_ref: record for record in records}
    claims = [*collect_claims(records, now=now), *interview_claims]
    settled = settled_claim_ids(config.adjudication_log_path)

    transcripts: list[tuple[str, Path]] = []
    if config.transcript_root is not None:
        for record in records:
            lane_id = lane_id_from_session_ref(record.session_ref)
            candidate = config.transcript_root / lane_id / TRANSCRIPT_NAME
            if candidate.is_file():
                transcripts.append((lane_id, candidate))

    sources = load_evidence_sources(
        state_dir=config.state_dir,
        convlog_path=config.convlog_path,
        decisions_path=config.decisions_path,
        fleet_usage_dir=config.fleet_usage_dir,
        transcripts=transcripts,
    )

    already = 0
    refused = 0
    escalated = 0
    undetermined = 0
    for claim in claims:
        if claim.claim_id in settled:
            already += 1
            continue
        goal = by_ref.get(claim.session_ref)
        if goal is None:
            continue
        outcome = adjudicate(claim, build_context(goal, sources), sources, adjudicator=adjudicator, now=now)
        if config.dry_run:
            print(json.dumps(outcome.model_dump(mode="json", by_alias=True), ensure_ascii=False, sort_keys=True))
        else:
            _act(config, outcome)
            append_adjudication(config.adjudication_log_path, outcome)
        settled.add(claim.claim_id)
        if outcome.verdict in REFUSING_VERDICTS:
            refused += 1
        elif outcome.verdict == "operator-required":
            escalated += 1
        else:
            undetermined += 1

    report = RunReport(
        claims_seen=len(claims),
        already_settled=already,
        refused=refused,
        escalated=escalated,
        undetermined=undetermined,
    )
    logger.info("adjudication_pass_complete", **report.to_dict())
    return report


def _act(config: AdjudicatordConfig, adjudication: Adjudication) -> None:
    """Carry out the one action the verdict calls for, then record it."""
    if adjudication.verdict in REFUSING_VERDICTS:
        try:
            enqueue_dispatch_order(config.queue_dir, build_directive_order(adjudication))
        except (AdjudicationRunError, OSError, ValueError) as exc:
            logger.warning("adjudication_directive_not_sent", claim_id=adjudication.claim.claim_id, error=str(exc))
        else:
            _retire_ask(config.state_dir, adjudication, authority=config.authority)
    elif adjudication.verdict == "operator-required":
        _raise_to_operator(config, adjudication)
    try:
        append_decision(
            config.decisions_path,
            decision_entry(adjudication, authority=config.authority, decision_id=uuid.uuid4().hex),
        )
    except (OSError, ValueError) as exc:
        logger.warning("adjudication_decision_record_failed", claim_id=adjudication.claim.claim_id, error=str(exc))


def run_forever(
    config: AdjudicatordConfig,
    *,
    adjudicator: BlockerAdjudicator | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Settle claims on an interval until a service signal stops the process."""
    active = stop_event or threading.Event()
    logger.info("adjudicatord_started", state_dir=str(config.state_dir), poll_seconds=config.poll_seconds)
    while not active.is_set():
        run_once(config, adjudicator=adjudicator)
        active.wait(config.poll_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-adjudicatord",
        description="Settle reported obstacles before they become operator questions.",
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--queue-dir", type=Path, default=None)
    parser.add_argument("--convlog-path", type=Path, default=None)
    parser.add_argument("--decisions-path", type=Path, default=None)
    parser.add_argument("--adjudication-log-path", type=Path, default=None)
    parser.add_argument("--fleet-usage-dir", type=Path, default=None, help="Shared usage directory written by the export timer.")
    parser.add_argument("--transcript-root", type=Path, default=None, help="Directory holding one recorded history per work session.")
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--program", default=DEFAULT_PROGRAM, help="Plain-language program name used on any operator brief.")
    parser.add_argument("--authority", default=DEFAULT_AUTHORITY)
    parser.add_argument("--reasoning-command", default="claude", help="Command that runs the bounded reasoning stage.")
    parser.add_argument("--reasoning-model", default=None)
    parser.add_argument("--reasoning-timeout-seconds", type=int, default=180)
    parser.add_argument("--deterministic-only", action="store_true", help="Run stage one only and leave the rest undecided.")
    parser.add_argument(
        "--no-specification-repair",
        action="store_true",
        help="Skip the short interview repair pass and adjudicate only what the sessions themselves reported.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print each outcome and change nothing.")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        config = resolve_config(
            state_dir=args.state_dir,
            queue_dir=args.queue_dir,
            convlog_path=args.convlog_path,
            decisions_path=args.decisions_path,
            adjudication_log_path=args.adjudication_log_path,
            fleet_usage_dir=args.fleet_usage_dir,
            transcript_root=args.transcript_root,
            poll_seconds=args.poll_seconds,
            dry_run=args.dry_run,
            repair_specifications=not args.no_specification_repair,
            program=args.program,
            authority=args.authority,
        )
    except ValueError as exc:
        print(f"chitra-adjudicatord: {exc}", file=sys.stderr)
        return 1

    if not args.dry_run:
        try:
            require_enabled(CAPABILITY_NAME, config.state_dir)
        except (CapabilityError, CapabilityDisabledError, KeyError) as exc:
            print(f"chitra-adjudicatord: {exc}", file=sys.stderr)
            return 1

    adjudicator: BlockerAdjudicator | None = None
    if not args.deterministic_only:
        adjudicator = ClaudeProcessAdjudicator(
            command=args.reasoning_command,
            model=args.reasoning_model,
            timeout_seconds=args.reasoning_timeout_seconds,
        )

    if args.once or args.dry_run:
        try:
            report = run_once(config, adjudicator=adjudicator)
        except (OSError, ValueError, AdjudicationRunError) as exc:
            print(f"chitra-adjudicatord: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0

    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_forever(config, adjudicator=adjudicator, stop_event=stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

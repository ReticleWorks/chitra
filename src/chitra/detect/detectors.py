"""Five event-based detectors over the W1 canonical journal (DESIGN-v3 §4).

Every detector consumes canonical events plus frozen goal data and returns a
list of :class:`Finding`. A finding binds the exact journal event references
that establish it, the unmet enrolled item it blocks, and the expected next
progress that would clear it. Findings never derive from elapsed time; each
predicate is a pure function of event content.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

from chitra.journal.models import CanonicalEvent, CanonicalType, ProgressClass, ProgressClassification
from chitra.validation_receipts import load_receipt_file, receipt_path, verify_receipt

DETECTOR_VERSION = "chitra-detectors.v1"

_DRIFT_TOOL_RE = re.compile(r"^(Edit|Write|MultiEdit|NotebookEdit)$")
_DOC_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_CODE_SUFFIXES = frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".sh", ".rb"})
_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc", ".org"})


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class Finding:
    """One detector output bound to journal evidence and goal state."""

    __slots__ = ("detector", "fingerprint", "event_refs", "unmet_item", "expected_next_progress", "detail")

    def __init__(
        self,
        *,
        detector: str,
        fingerprint_seed: dict[str, Any],
        event_refs: tuple[str, ...],
        unmet_item: str,
        expected_next_progress: str,
        detail: str,
    ) -> None:
        self.detector = detector
        self.fingerprint = _canonical_digest({"detector": detector, "seed": fingerprint_seed})
        self.event_refs = tuple(event_refs)
        self.unmet_item = unmet_item
        self.expected_next_progress = expected_next_progress
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "fingerprint": self.fingerprint,
            "event_refs": list(self.event_refs),
            "unmet_item": self.unmet_item,
            "expected_next_progress": self.expected_next_progress,
            "detail": self.detail,
        }


def _first_unmet_item(enrolled_items: Sequence[object]) -> str:
    for item in enrolled_items:
        item_id = getattr(item, "id", None)
        if isinstance(item_id, str):
            return item_id
    return ""


def _joined_results(events: Sequence[CanonicalEvent]) -> dict[str, CanonicalEvent]:
    joined: dict[str, CanonicalEvent] = {}
    for event in events:
        if event.normalized_type in {CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR} and isinstance(
            event.native_join_id, str
        ):
            joined.setdefault(event.native_join_id, event)
    return joined


def _result_signature(call: CanonicalEvent, result: CanonicalEvent | None) -> str:
    result_value: dict[str, Any] | None = None
    if result is not None:
        result_value = {
            "content": result.payload.get("content"),
            "is_error": result.payload.get("is_error"),
            "tool_use_result": result.payload.get("tool_use_result"),
        }
    return _canonical_digest(
        {
            "tool_name": call.payload.get("tool_name"),
            "target": call.payload.get("input") if call.payload.get("input") is not None else call.payload,
            "result": result_value,
        }
    )


def _has_progress_between(progress_rows: Sequence[ProgressClassification], start: int, end: int) -> bool:
    for row in progress_rows:
        if row.classification is not ProgressClass.PROGRESS:
            continue
        for source in row.source_event_ids:
            if not source.isdecimal():
                continue
            index = int(source)
            if start < index < end:
                return True
    return False


def _strings_from(value: object) -> tuple[str, ...]:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, dict):
        for nested in value.values():
            strings.extend(_strings_from(nested))
    elif isinstance(value, list | tuple):
        for nested in value:
            strings.extend(_strings_from(nested))
    return tuple(strings)


def _target_paths(input_value: object) -> tuple[str, ...]:
    if not isinstance(input_value, dict):
        return ()
    paths: list[str] = []
    for key, value in input_value.items():
        if key in {"cwd", "old", "new", "edit", "command"}:
            continue
        if key in {"file_path", "path", "target", "target_path"} and isinstance(value, str):
            paths.append(value)
        elif key in {"files", "paths"} and isinstance(value, list):
            paths.extend(entry for entry in value if isinstance(entry, str))
    return tuple(paths)


def _violated_scope_clause(text: str, clauses: Sequence[str]) -> str | None:
    for clause in clauses:
        if clause in text:
            return clause
        if "remote install" in clause and "install.sh" in text and ("curl" in text or "http" in text):
            return clause
        if "/etc" in clause and "/etc" in text:
            return clause
    return None


def detect_drift(
    events: Sequence[CanonicalEvent],
    *,
    scope_text: str,
    declared_worktree: str,
    enrolled_items: Sequence[object] = (),
) -> list[Finding]:
    """Flag scoped work events conflicting with the goal's frozen boundaries.

    The predicate is content-only: an edit-family tool whose target path or
    command text names an excluded action from ``scope_text``, or a worktree
    call whose cwd leaves ``declared_worktree``. An allowed diagnostic does
    not drift.
    """
    findings: list[Finding] = []
    unmet = _first_unmet_item(enrolled_items)
    excluded_clauses = tuple(clause.strip().lower() for clause in re.split(r"[;\n]", scope_text) if len(clause.strip()) > 3)
    for event in events:
        if event.normalized_type is not CanonicalType.TOOL_CALL:
            continue
        tool_name = event.payload.get("tool_name")
        input_value = event.payload.get("input")
        text = " ".join(_strings_from(input_value)).lower()
        violated = _violated_scope_clause(text, excluded_clauses)
        if violated is not None:
            findings.append(
                Finding(
                    detector="drift",
                    fingerprint_seed={"event_id": event.event_id, "clause": violated},
                    event_refs=(event.event_id,),
                    unmet_item=unmet,
                    expected_next_progress="return to the enrolled scope and produce evidence toward the first unmet done-when item",
                    detail=f"scoped work conflicts with the goal boundary clause {violated!r}",
                )
            )
            continue
        if isinstance(tool_name, str) and _DRIFT_TOOL_RE.match(tool_name) and declared_worktree:
            paths = _target_paths(input_value)
            cwd = event.payload.get("cwd")
            input_cwd = input_value.get("cwd") if isinstance(input_value, dict) else None
            outside_target = next((path for path in paths if path.startswith("/") and not path.startswith(declared_worktree)), None)
            outside_cwd = next(
                (path for path in (cwd, input_cwd) if isinstance(path, str) and not path.startswith(declared_worktree)),
                None,
            )
            if outside_target is not None or outside_cwd is not None:
                outside = outside_target or outside_cwd or ""
                findings.append(
                    Finding(
                        detector="drift",
                        fingerprint_seed={"event_id": event.event_id, "outside": outside},
                        event_refs=(event.event_id,),
                        unmet_item=unmet,
                        expected_next_progress="resume work inside the declared worktree",
                        detail=f"{tool_name} targeted {outside!r} outside the declared worktree {declared_worktree!r}",
                    )
                )
    return findings


def detect_unnecessary_steps(
    events: Sequence[CanonicalEvent],
    *,
    progress_rows: Sequence[ProgressClassification] = (),
    threshold: int = 3,
    enrolled_items: Sequence[object] = (),
) -> list[Finding]:
    """Flag one normalized tool+target+result signature repeated without progress.

    The recurrence counter resets on any verified progress between repeats;
    a changed result signature starts a new identity rather than extending
    the old one. Threshold three matches DESIGN-v3 §4 ("at least three times").
    """
    findings: list[Finding] = []
    unmet = _first_unmet_item(enrolled_items)
    results = _joined_results(events)
    seen: dict[str, list[tuple[int, str]]] = {}
    for position, event in enumerate(events):
        if event.normalized_type is not CanonicalType.TOOL_CALL:
            continue
        signature = _result_signature(event, results.get(event.native_join_id or ""))
        occurrences = seen.setdefault(signature, [])
        occurrences.append((position, event.event_id))
        if len(occurrences) < threshold:
            continue
        prior = [entry for entry in occurrences[:-1]]
        start_position = prior[-1][0]
        if _has_progress_between(progress_rows, start_position, position):
            occurrences.clear()
            occurrences.append((position, event.event_id))
            continue
        refs = tuple(entry[1] for entry in occurrences[-threshold:])
        findings.append(
            Finding(
                detector="unnecessary_steps",
                fingerprint_seed={"signature": signature},
                event_refs=refs,
                unmet_item=unmet,
                expected_next_progress="change approach so the repeated read produces new scoped state",
                detail=f"identical tool, target, and result occurred {threshold} times with no intervening verified progress",
            )
        )
    return findings


def detect_excessive_testing(
    events: Sequence[CanonicalEvent],
    *,
    progress_rows: Sequence[ProgressClassification] = (),
    threshold: int = 3,
    enrolled_items: Sequence[object] = (),
) -> list[Finding]:
    """Flag a check suite repeating with no artifact change, new failure
    signature, or newly exercised required surface."""
    findings: list[Finding] = []
    unmet = _first_unmet_item(enrolled_items)
    results = _joined_results(events)
    runs: list[tuple[int, str, CanonicalEvent]] = []
    for position, event in enumerate(events):
        if event.normalized_type is not CanonicalType.TOOL_CALL:
            continue
        input_value = event.payload.get("input")
        text = ""
        if isinstance(input_value, str):
            text = input_value
        elif isinstance(input_value, dict):
            text = " ".join(value for value in input_value.values() if isinstance(value, str))
        if _is_check_invocation(text):
            signature = _result_signature(event, results.get(event.native_join_id or ""))
            runs.append((position, signature, event))
    streak: list[tuple[int, str, CanonicalEvent]] = []
    for run in runs:
        if streak and streak[-1][1] == run[1]:
            streak.append(run)
        else:
            streak = [run]
        if len(streak) < threshold:
            continue
        start_position = streak[len(streak) - threshold][0]
        if _has_progress_between(progress_rows, start_position, run[0]):
            streak = [run]
            continue
        refs = tuple(entry[2].event_id for entry in streak[len(streak) - threshold :])
        findings.append(
            Finding(
                detector="excessive_testing",
                fingerprint_seed={"signature": run[1]},
                event_refs=refs,
                unmet_item=unmet,
                expected_next_progress=(
                    "make a targeted artifact change before rerunning the check, or record its failure as the required evidence"
                ),
                detail=f"an unchanged check invocation repeated {threshold} times without an artifact change or new failure signature",
            )
        )
        streak = [run]
    return findings


def _is_check_invocation(text: str) -> bool:
    lowered = text.lower()
    tokens = ("pytest", "ruff", "mypy", "tox", "unittest", "npm test", "cargo test", "go test")
    return any(token in lowered for token in tokens)


def detect_document_dithering(
    events: Sequence[CanonicalEvent],
    *,
    goal_is_document: bool,
    minimum_recurrence: int = 3,
    enrolled_items: Sequence[object] = (),
) -> list[Finding]:
    """For a non-document goal, flag recurring document edits while required
    implementation items gain no evidence. Disabled entirely for doc goals."""
    if goal_is_document:
        return []
    unmet = _first_unmet_item(enrolled_items)
    doc_events: list[CanonicalEvent] = []
    implementation_evidence = False
    for event in events:
        if event.normalized_type is not CanonicalType.TOOL_CALL:
            continue
        tool_name = event.payload.get("tool_name")
        input_value = event.payload.get("input")
        text = input_value if isinstance(input_value, str) else ""
        paths = _target_paths(input_value)
        targets_docs = bool(paths) and isinstance(tool_name, str) and tool_name in _DOC_TOOLS and all(
            path.lower().endswith(tuple(_DOC_SUFFIXES)) for path in paths
        )
        if targets_docs:
            doc_events.append(event)
        elif _looks_like_implementation(text):
            implementation_evidence = True
    if implementation_evidence or len(doc_events) < minimum_recurrence:
        return []
    refs = tuple(event.event_id for event in doc_events[:minimum_recurrence])
    return [
        Finding(
            detector="document_dithering",
            fingerprint_seed={"doc_event_ids": refs},
            event_refs=refs,
            unmet_item=unmet,
            expected_next_progress=(
                "produce implementation or validation evidence for the first unmet done-when item instead of further prose churn"
            ),
            detail=f"{len(doc_events)} document edits with no implementation or validation evidence for a code goal",
        )
    ]


def _looks_like_path(value: str) -> bool:
    return "/" in value or value.endswith(tuple(_DOC_SUFFIXES | _CODE_SUFFIXES))


def _looks_like_implementation(text: str) -> bool:
    lowered = text.lower()
    markers = ("def ", "class ", "function ", "impl ", "fix ", "refactor", "src/", "import ")
    return any(marker in lowered for marker in markers)


def detect_false_done(
    *,
    final_response: CanonicalEvent | None,
    enrolled_items: Sequence[object],
    receipt_names_by_item: dict[str, str],
    receipt_roots: dict[str, object] | None = None,
    session_ref: str = "",
) -> list[Finding]:
    """Reject a completion claim that conflicts with goal state.

    Every open item, missing/stale/failed receipt, or absent claim binding
    yields one finding naming the blocking condition. Only exact current
    bindings of every item pass silently.
    """
    from pathlib import Path

    findings: list[Finding] = []
    if final_response is None:
        return findings
    root: Path | None = None
    if receipt_roots is not None and session_ref:
        candidate = receipt_roots.get(session_ref)
        if isinstance(candidate, Path):
            root = candidate
        elif isinstance(candidate, str):
            root = Path(candidate)
    refs: tuple[str, ...] = (final_response.event_id,)
    for item in enrolled_items:
        item_id = str(getattr(item, "id", ""))
        validator = str(getattr(item, "validator", ""))
        required_receipt = str(getattr(item, "required_receipt", ""))
        if receipt_names_by_item.get(item_id) != required_receipt:
            findings.append(
                Finding(
                    detector="false_done",
                    fingerprint_seed={"item": item_id, "reason": "claim-binding"},
                    event_refs=refs,
                    unmet_item=item_id,
                    expected_next_progress=f"bind completion evidence for item {item_id!r} to receipt {required_receipt!r}",
                    detail=f"completion claim does not bind item {item_id!r} to its required receipt {required_receipt!r}",
                )
            )
            continue
        if root is None:
            continue
        try:
            verification = verify_receipt(root, session_ref, required_receipt)
            receipt, _raw = load_receipt_file(receipt_path(root, session_ref, required_receipt))
        except Exception:
            findings.append(
                Finding(
                    detector="false_done",
                    fingerprint_seed={"item": item_id, "reason": "receipt-unavailable"},
                    event_refs=refs,
                    unmet_item=item_id,
                    expected_next_progress=f"produce the verified PASS receipt {required_receipt!r} for item {item_id!r}",
                    detail=f"required receipt {required_receipt!r} for item {item_id!r} is missing or unreadable",
                )
            )
            continue
        validator_names = {str(receipt.validator.get("name"))} if receipt.validator else set()
        if not verification.completion_eligible or validator not in validator_names:
            findings.append(
                Finding(
                    detector="false_done",
                    fingerprint_seed={"item": item_id, "reason": "receipt-not-pass"},
                    event_refs=refs,
                    unmet_item=item_id,
                    expected_next_progress=f"replace receipt {required_receipt!r} with a verified passing run bound to item {item_id!r}",
                    detail=f"receipt {required_receipt!r} does not currently verify as a passing {validator!r} result",
                )
            )
    return findings


__all__ = [
    "DETECTOR_VERSION",
    "Finding",
    "detect_document_dithering",
    "detect_drift",
    "detect_excessive_testing",
    "detect_false_done",
    "detect_unnecessary_steps",
]

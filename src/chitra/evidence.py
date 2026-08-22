"""Resolve ExhaustionRecord evidence handles against on-disk facts.

An exhaustion attempt used to be a self-reported sentence; the write gate
could not tell a real capture or a real ledger order from an invented one.
Attempts now carry an :class:`EvidenceHandle`, and ``chitra-convo brief``
resolves every handle at write time through an :class:`EvidenceResolver`.
The default resolver checks the monitor's own transcript and the dispatch
delivery ledger on disk, so a fabricated handle fails the brief before it is
rendered or logged. Callers (and tests) may inject any resolver that satisfies
the protocol; nothing here trusts the brief's prose.

No LLM calls in this module's own code path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from chitra.state_paths import default_ledger_path

SELF_TRANSCRIPT_ENV_VAR = "CHITRA_SELF_TRANSCRIPT"

type EvidenceKind = Literal["command", "order", "transcript", "verb_refusal"]

_EXIT_IN_TEXT = re.compile(r"exit(?: code| status)?[: =]+(\d+)", re.IGNORECASE)


class EvidenceHandle(BaseModel):
    """A machine-checkable pointer to the fact that backs one attempt.

    ``ref`` means a different thing per kind: the exact argv string for
    ``command``, the ledger order id for ``order``, an absolute transcript
    path for ``transcript``, and the grant verb name for ``verb_refusal``.
    """

    kind: EvidenceKind
    ref: str = Field(min_length=1)
    exit_status: int | None = None
    sha256: str | None = None


class EvidenceResolver(Protocol):
    """Anything that can confirm or refute one evidence handle."""

    def resolve(self, handle: EvidenceHandle) -> str | None:
        """Return None when the handle resolves, else a human-readable reason it does not."""
        ...


def _iter_json_objects(path: Path) -> list[object]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    objects: list[object] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return objects


def _walk(node: object) -> list[object]:
    if isinstance(node, dict):
        return [node, *(found for value in node.values() for found in _walk(value))]
    if isinstance(node, list):
        return [node, *(found for item in node for found in _walk(item))]
    return []


def _find_events(payload: object, event_type: str) -> list[dict[str, object]]:
    return [node for node in _walk(payload) if isinstance(node, dict) and node.get("type") == event_type]


def _serialized(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _tool_input_text(event: dict[str, object]) -> str:
    return _serialized(event.get("input", ""))


def _result_exit_status(event: dict[str, object]) -> int | None:
    for node in _walk(event):
        if isinstance(node, dict):
            for key in ("exit_code", "exit_status"):
                status = node.get(key)
                if isinstance(status, int):
                    return status
    match = _EXIT_IN_TEXT.search(_serialized(event.get("content", event)))
    return int(match.group(1)) if match else None


class FilesystemEvidenceResolver:
    """Default resolver: check the monitor transcript and the delivery ledger on disk.

    The self-transcript path comes from the caller (the launcher knows it and
    passes ``--self-transcript``) or from ``CHITRA_SELF_TRANSCRIPT``; the
    ledger path defaults to the instance's delivery ledger. Nothing is cached,
    so repeated resolves re-read current files.
    """

    def __init__(self, *, self_transcript: Path | str | None = None, ledger_path: Path | str | None = None) -> None:
        self._self_transcript_arg = self_transcript
        self._ledger_path_arg = ledger_path

    @property
    def self_transcript(self) -> Path | None:
        if self._self_transcript_arg is not None:
            return Path(self._self_transcript_arg)
        configured = os.environ.get(SELF_TRANSCRIPT_ENV_VAR, "").strip()
        return Path(configured) if configured else None

    @property
    def ledger_path(self) -> Path:
        return Path(self._ledger_path_arg) if self._ledger_path_arg is not None else default_ledger_path()

    def resolve(self, handle: EvidenceHandle) -> str | None:
        match handle.kind:
            case "command":
                return self._resolve_command(handle)
            case "order":
                return self._resolve_order(handle)
            case "transcript":
                return self._resolve_transcript_line(handle)
            case "verb_refusal":
                return self._resolve_verb_refusal(handle)

    def _resolve_command(self, handle: EvidenceHandle) -> str | None:
        transcript = self.self_transcript
        if transcript is None:
            return f"self transcript not available (pass --self-transcript or set {SELF_TRANSCRIPT_ENV_VAR})"
        result_id, problem = self._scan_transcript_for_run(transcript, needle=handle.ref, tool_name="Bash")
        if problem is not None:
            return problem
        assert result_id is not None
        exit_problem = self._check_recorded_exit(transcript, result_id, handle.exit_status, label=f"command {handle.ref!r}")
        return exit_problem

    def _resolve_verb_refusal(self, handle: EvidenceHandle) -> str | None:
        transcript = self.self_transcript
        if transcript is None:
            return f"self transcript not available (pass --self-transcript or set {SELF_TRANSCRIPT_ENV_VAR})"
        result_id, problem = self._scan_transcript_for_run(transcript, needle=handle.ref, tool_name=None)
        if problem is not None:
            return problem
        assert result_id is not None
        status = self._exit_of_result(transcript, result_id)
        if status is None:
            return f"could not read the exit status recorded for grant verb {handle.ref!r}"
        if status == 0:
            return f"grant verb {handle.ref!r} did not fail in self transcript; no refusal was observed"
        return None

    def _scan_transcript_for_run(self, transcript: Path, *, needle: str, tool_name: str | None) -> tuple[str | None, str | None]:
        pending: dict[str, str] = {}
        for payload in _iter_json_objects(transcript):
            for use in _find_events(payload, "tool_use"):
                name = str(use.get("name", ""))
                input_text = _tool_input_text(use)
                if tool_name is not None and name != tool_name:
                    continue
                if needle in input_text:
                    use_id = str(use.get("id", ""))
                    if use_id:
                        pending[use_id] = input_text
            for result in _find_events(payload, "tool_result"):
                result_id = str(result.get("tool_use_id", ""))
                if result_id in pending:
                    return result_id, None
        if pending:
            return next(iter(pending)), f"no recorded result in self transcript for {needle!r}"
        looked_for = f"{tool_name} tool use mentioning {needle!r}" if tool_name else f"tool use mentioning {needle!r}"
        return None, f"no {looked_for} in self transcript"

    def _exit_of_result(self, transcript: Path, result_id: str) -> int | None:
        for payload in _iter_json_objects(transcript):
            for result in _find_events(payload, "tool_result"):
                if str(result.get("tool_use_id", "")) == result_id:
                    return _result_exit_status(result)
        return None

    def _check_recorded_exit(self, transcript: Path, result_id: str, expected: int | None, *, label: str) -> str | None:
        if expected is None:
            return None
        status = self._exit_of_result(transcript, result_id)
        if status is None:
            return f"could not read the exit status recorded for {label}"
        if status != expected:
            return f"{label} exited {status}, but the attempt records exit {expected}"
        return None

    def _resolve_order(self, handle: EvidenceHandle) -> str | None:
        for payload in _iter_json_objects(self.ledger_path):
            if isinstance(payload, dict) and payload.get("order_id") == handle.ref:
                return None
        return f"order {handle.ref} not found in ledger"

    def _resolve_transcript_line(self, handle: EvidenceHandle) -> str | None:
        path = Path(handle.ref)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return f"transcript not found: {handle.ref}"
        if handle.sha256 is None:
            return None
        wanted = handle.sha256.casefold()
        for line in lines:
            if hashlib.sha256(line.rstrip("\r\n").encode("utf-8")).hexdigest() == wanted:
                return None
        return f"no line in {handle.ref} hashes to {wanted}"

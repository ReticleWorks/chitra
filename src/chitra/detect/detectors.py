"""Six event-based detectors over the W1 canonical journal (DESIGN-v3 §4).

Every detector consumes canonical events plus frozen goal data and returns a
list of :class:`Finding`. A finding binds the exact journal event references
that establish it, the unmet enrolled item it blocks, and the expected next
progress that would clear it. Findings never derive from elapsed time; each
predicate is a pure function of event content. Tool calls are classified by
behavior through :mod:`chitra.journal.tools`, so a Claude ``Bash`` call and a
Codex ``exec_command`` call land on the same predicate, and completion claims
share the one vocabulary in :mod:`chitra.completion_gate`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chitra.completion_gate import is_completion_claim, scan_deferral_language
from chitra.journal.models import CanonicalEvent, CanonicalType, ProgressClass, ProgressClassification
from chitra.journal.tools import (
    action_kind,
    call_signature,
    check_signature,
    coerce_tool_input,
    command_segments_argv,
    command_text,
    result_class,
    shell_write_targets,
    target_paths,
    tool_class,
    unanswered_tail_calls,
)
from chitra.validation_receipts import load_receipt_file, receipt_path, verify_receipt

DETECTOR_VERSION = "chitra-detectors.v2"

_WORK_CAPABLE_CLASSES = frozenset({"write", "shell", "other"})
_CODE_SUFFIXES = frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".sh", ".rb"})
_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc", ".org"})


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class Finding:
    """One detector output bound to journal evidence and goal state."""

    __slots__ = (
        "detector",
        "fingerprint",
        "fingerprint_seed",
        "event_refs",
        "unmet_item",
        "expected_next_progress",
        "detail",
    )

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
        self.fingerprint_seed = fingerprint_seed
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


def met_done_items(
    enrolled_items: Sequence[object],
    *,
    receipt_root: Path | None,
    session_ref: str,
) -> frozenset[str]:
    """Return the enrolled item ids whose required receipt verifies right now.

    The same per-item receipt check ``detect_false_done`` applies decides
    which items are already met; findings then bind the first item that is
    genuinely unmet instead of the first enrolled row.
    """
    met: set[str] = set()
    if receipt_root is None or not session_ref:
        return frozenset()
    for item in enrolled_items:
        item_id = str(getattr(item, "id", ""))
        validator = str(getattr(item, "validator", ""))
        required_receipt = str(getattr(item, "required_receipt", ""))
        if not item_id or not required_receipt:
            continue
        try:
            verification = verify_receipt(receipt_root, session_ref, required_receipt)
            receipt, _raw = load_receipt_file(receipt_path(receipt_root, session_ref, required_receipt))
        except Exception:
            continue
        validator_names = {str(receipt.validator.get("name"))} if receipt.validator else set()
        if verification.completion_eligible and validator in validator_names:
            met.add(item_id)
    return frozenset(met)


def first_unmet_item[ItemT](
    enrolled_items: Sequence[ItemT],
    met_items: Set[str] = frozenset(),
) -> ItemT | None:
    """Return the first enrolled item not in ``met_items`` (None when all are met)."""
    for item in enrolled_items:
        item_id = getattr(item, "id", None)
        if isinstance(item_id, str) and item_id not in met_items:
            return item
    return None


def first_unmet_item_id(
    enrolled_items: Sequence[object],
    met_items: Set[str] = frozenset(),
) -> str:
    """Return the id of the first unmet enrolled item, or "" when none is unmet."""
    item = first_unmet_item(enrolled_items, met_items)
    if item is None:
        return ""
    item_id = getattr(item, "id", "")
    return item_id if isinstance(item_id, str) else ""


def _joined_results(events: Sequence[CanonicalEvent]) -> dict[str, CanonicalEvent]:
    joined: dict[str, CanonicalEvent] = {}
    for event in events:
        if event.normalized_type in {CanonicalType.TOOL_RESULT, CanonicalType.TOOL_ERROR} and isinstance(
            event.native_join_id, str
        ):
            joined.setdefault(event.native_join_id, event)
    return joined


def _has_progress_between(
    events: Sequence[CanonicalEvent], progress_rows: Sequence[ProgressClassification], start: int, end: int
) -> bool:
    positions = {event.event_id: position for position, event in enumerate(events)}
    for row in progress_rows:
        if row.classification is not ProgressClass.PROGRESS:
            continue
        for source in row.source_event_ids:
            index = positions.get(source)
            if index is not None and start < index < end:
                return True
    return False


def _has_progress_at(
    events: Sequence[CanonicalEvent], progress_rows: Sequence[ProgressClassification], position: int
) -> bool:
    event_id = events[position].event_id
    return any(
        row.classification is ProgressClass.PROGRESS and event_id in row.source_event_ids
        for row in progress_rows
    )


def _contained_in_worktree(path: str, *, declared_worktree: str, cwd: str | None = None) -> bool:
    if not declared_worktree:
        return True
    root = Path(declared_worktree).expanduser().resolve(strict=False)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd).expanduser() / candidate
    elif not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    return resolved == root or root in resolved.parents


def _semantic_path(path: str, *, declared_worktree: str, cwd: str | None = None) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd).expanduser() / candidate
    return str(candidate.resolve(strict=False))


# --- frozen-scope parsing ---------------------------------------------------
# The OverReach mechanism: extract the authorized set from the frozen scope,
# parse what the lane actually touched (tool targets plus the real worktree
# diff), and flag the difference structurally instead of substring-matching
# prose. A clause that yields no typed exclusion stays a residual literal
# match against tool input, exactly as before.

_SCOPE_SPLIT_RE = re.compile(r"[;\n]+")
_SCOPE_DENY_RE = re.compile(
    r"\b(?:never|no|not|don't|dont|do not|avoid|must not|mustn't|prohibit\w*|forbid\w*|forbidden|"
    r"exclude\w*|except|off[- ]?limits|stay out|keep out|keep away|out[- ]of[- ]scope|without|refrain)\b",
    re.IGNORECASE,
)
_SCOPE_ALLOW_RE = re.compile(
    r"\b(?:only|just|restricted to|limited to|confined to|in scope|within|inside)\b", re.IGNORECASE
)
_SCOPE_READONLY_RE = re.compile(
    r"\bread[- ]?only|inspection[- ]?only|observe[- ]?only|look[- ]?only|analysis[- ]?only|no[- ]?edit\b|"
    r"\bno\s+(?:changes|edits|writes|modifications)\b|\bmake\s+no\s+changes\b",
    re.IGNORECASE,
)
_SCOPE_READONLY_GLOBAL_RE = re.compile(
    r"\bread[- ]?only|inspection[- ]?only|observe[- ]?only|look[- ]?only|analysis[- ]?only|no[- ]?edit\b|"
    r"\bmake\s+no\s+changes\b",
    re.IGNORECASE,
)
_REMOTE_INSTALL_SCOPE_RE = re.compile(
    r"\b(?:remote|download\w*|internet|web|external|piped|pipe|online)\b[^\n]*\b(?:install|setup|bootstrap|script)s?\b"
    r"|\b(?:install|setup|bootstrap|script)s?\b[^\n]*\b(?:remote|download\w*|internet|web|external|piped|pipe|online|curl|wget)\b",
    re.IGNORECASE,
)
_REMOTE_INSTALL_CMD_RE = re.compile(
    r"(?:\b(?:curl|wget|fetch|iwr|invoke-webrequest)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba?sh|zsh|sh|dash|ksh|python[\d.]*|perl|ruby|node)\b"
    r"|\b(?:curl|wget)\b[^|;&]*\b(?:install|setup|bootstrap)\.sh\b"
    r"|\b(?:ba?sh|zsh|sh|dash)\b[^|;&]*\b(?:install|setup|bootstrap)\.sh\b"
    r"|\b(?:install|setup|bootstrap)\.sh\b[^|;&]*\|"
    r"|\b(?:ba?sh|zsh|sh|dash|python[\d.]*)\s+-c\s*[\"']?\s*\$?\(?\s*(?:curl|wget)\b)",
    re.IGNORECASE,
)
_SCOPE_RUN_VERB_RE = re.compile(
    r"\b(?:run|runs|execute|executes|invoke|invokes|launch|launchs|start|starts|perform|issue|issues|"
    r"trigger|triggers|push|deploy|publish|delete|remove|drop|migrate|install|download|upload|"
    r"send|restart|kill|stop|reboot|shutdown)\b",
    re.IGNORECASE,
)
_SCOPE_FILEISH_RE = re.compile(
    r"\b(?:file|files|code|edit|edits|touch|touches|change|changes|modify|write|writes|writing|update|updates|"
    r"directory|directories|folder|folders|module|modules|doc|docs|document\w*|worktree|tree|path|paths|"
    r"component\w*|subsystem\w*|config\w*|schema\w*|production|prod|tests?|spec\w*|migration\w*|"
    r"area|section|portion|out of|outside|under|inside|within|into)\b",
    re.IGNORECASE,
)
_SCOPE_PATH_TOKEN_RE = re.compile(r"^(?:[/~.]|[A-Za-z]:[\\/])|[/\\]|\.\w{1,8}$|\*")
_SCOPE_FLAG_TOKEN_RE = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9-]*$")

_SCOPE_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "from", "with", "at", "by", "into",
        "never", "no", "not", "don't", "dont", "do", "does", "did", "cannot", "can't", "cant", "must",
        "mustn't", "mustnt", "avoid", "avoiding", "prohibit", "prohibited", "prohibiting", "forbid",
        "forbids", "forbidden", "exclude", "excludes", "excluding", "except", "without", "refrain",
        "keep", "stay", "off", "out", "outside", "inside", "only", "just", "restricted", "limited",
        "confined", "scope", "scoped", "within", "any", "all", "none", "every", "everything", "anything",
        "something", "nothing", "run", "runs", "execute", "executes", "invoke", "invokes", "launch",
        "start", "starts", "perform", "issue", "issues", "trigger", "triggers", "touch", "touches",
        "touched", "change", "changes", "changed", "edit", "edits", "edited", "modify", "modifies",
        "modified", "write", "writes", "writing", "update", "updates", "updated", "create", "creates",
        "delete", "deletes", "remove", "removes", "make", "makes", "made", "use", "using", "work",
        "works", "working", "file", "files", "directory", "directories", "folder", "folders", "module",
        "modules", "code", "codes", "coding", "path", "paths", "area", "areas", "section", "sections",
        "part", "parts", "portion", "component", "components", "system", "systems", "repo", "repository",
        "worktree", "tree", "stuff", "thing", "things", "else", "other", "others", "anywhere",
        "everywhere", "please", "ever", "etc", "own", "new", "existing", "your", "their", "its", "our",
        "my", "read", "reads", "readonly", "read-only", "inspection", "observe", "observing", "analysis",
        "look", "looking", "remote", "remotely", "download", "downloads", "downloaded", "install",
        "installs", "installed", "installing", "script", "scripts", "setup", "bootstrap", "pipe",
        "piped", "piping", "external", "online", "internet", "web", "curl", "wget", "fetch", "this",
        "that", "these", "those", "them", "it", "be", "been", "being", "is", "are", "was", "were",
    }
)

_SCOPE_SUFFIX_STRIP = (
    "ational", "ation", "tional", "tion", "sion",
    "ings", "ing", "ers", "er", "ies", "ied", "es",
    "ments", "ment", "s", "ed", "ly",
)


def _scope_stem(word: str) -> str:
    for suffix in _SCOPE_SUFFIX_STRIP:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: len(word) - len(suffix)]
    return word


def _scope_tokens(clause: str) -> list[str]:
    return [token.strip("\"'`.,:;()[]{}<>") for token in clause.split()]


def _scope_content_words(clause: str) -> list[str]:
    words: list[str] = []
    for token in _scope_tokens(clause):
        if not token or token in _SCOPE_STOPWORDS or _SCOPE_FLAG_TOKEN_RE.match(token):
            continue
        if _SCOPE_PATH_TOKEN_RE.search(token):
            continue
        words.append(token)
    return words


def _is_scope_path_token(token: str) -> bool:
    return bool(token) and token not in _SCOPE_STOPWORDS and bool(_SCOPE_PATH_TOKEN_RE.search(token))


@dataclass(frozen=True, slots=True)
class _ScopeModel:
    """Typed exclusions and allowed set extracted from the frozen scope text."""

    denied_paths: tuple[str, ...]
    denied_command_patterns: tuple[re.Pattern[str], ...]
    denied_command_words: tuple[tuple[str | None, frozenset[str], tuple[str, ...]], ...]
    deny_writes: bool
    allowed_paths: tuple[str, ...]
    residual_clauses: tuple[str, ...]


def _compile_scope(scope_text: str) -> _ScopeModel:
    denied_paths: list[str] = []
    denied_patterns: list[re.Pattern[str]] = []
    denied_words: list[tuple[str | None, frozenset[str], tuple[str, ...]]] = []
    allowed_paths: list[str] = []
    residual: list[str] = []
    deny_writes = False
    for raw_clause in _SCOPE_SPLIT_RE.split(scope_text):
        clause = raw_clause.strip().lower()
        if len(clause) <= 3:
            continue
        has_deny = bool(_SCOPE_DENY_RE.search(clause))
        has_allow = bool(_SCOPE_ALLOW_RE.search(clause))
        path_tokens = [token for token in _scope_tokens(clause) if _is_scope_path_token(token)]
        words = _scope_content_words(clause)
        flag_tokens = [token for token in _scope_tokens(clause) if _SCOPE_FLAG_TOKEN_RE.match(token)]
        if has_allow and not has_deny:
            if _SCOPE_READONLY_RE.search(clause):
                deny_writes = True
            else:
                allowed_paths.extend(path_tokens)
                allowed_paths.extend(words)
            continue
        if not has_deny:
            continue
        consumed = False
        if _REMOTE_INSTALL_SCOPE_RE.search(clause):
            denied_patterns.append(_REMOTE_INSTALL_CMD_RE)
            consumed = True
        if _SCOPE_READONLY_RE.search(clause) and (
            _SCOPE_READONLY_GLOBAL_RE.search(clause) or not (path_tokens or words)
        ):
            deny_writes = True
            consumed = True
        denied_paths.extend(path_tokens)
        if consumed:
            continue
        actionish = bool(_SCOPE_RUN_VERB_RE.search(clause)) or bool(flag_tokens)
        fileish = bool(_SCOPE_FILEISH_RE.search(clause)) or bool(path_tokens)
        extracted = False
        if actionish:
            flags: set[str] = set()
            for token in flag_tokens:
                if token.startswith("--"):
                    flags.add(token[2:].lower())
                else:
                    flags.update(letter.lower() for letter in token[1:] if letter.isalpha())
            stems = tuple(dict.fromkeys(_scope_stem(word) for word in words))
            argv0: str | None = None
            if flag_tokens and stems:
                argv0, stems = stems[0], stems[1:]
            if argv0 or stems or flags:
                denied_words.append((argv0, frozenset(flags), stems))
                extracted = True
        if fileish:
            denied_paths.extend(words)
            extracted = True
        if not extracted and not path_tokens:
            residual.append(clause)
    return _ScopeModel(
        denied_paths=tuple(dict.fromkeys(denied_paths)),
        denied_command_patterns=tuple(denied_patterns),
        denied_command_words=tuple(denied_words),
        deny_writes=deny_writes,
        allowed_paths=tuple(dict.fromkeys(allowed_paths)),
        residual_clauses=tuple(residual),
    )


def _scope_path_hit(path: str, term: str) -> bool:
    """Whether one touched path matches one scope term (dir, file, glob, stem)."""
    normalized = path.replace("\\", "/").lstrip("./").rstrip("/")
    cleaned = term.replace("\\", "/").lstrip("./").rstrip("/")
    if not normalized or not cleaned:
        return False
    if term.startswith(("/", "~")):
        absolute = normalized if path.startswith("/") else "/" + normalized
        return absolute == cleaned or absolute.startswith(cleaned + "/")
    if "*" in cleaned:
        return fnmatch.fnmatch(normalized, cleaned) or fnmatch.fnmatch(posixpath.basename(normalized), cleaned)
    components = [component for component in normalized.split("/") if component]
    if cleaned in components or normalized == cleaned or normalized.startswith(cleaned + "/"):
        return True
    basename = components[-1] if components else ""
    stem = posixpath.splitext(basename)[0]
    return bool(stem) and stem == cleaned


def _scope_flag_set(argv: Sequence[str]) -> frozenset[str]:
    flags: set[str] = set()
    for token in argv[1:]:
        if token.startswith("--"):
            flags.add(token[2:].split("=", 1)[0].lower())
        elif token.startswith("-") and len(token) > 1:
            flags.update(letter for letter in token[1:] if letter.isalpha())
    return frozenset(flags)


def _command_words_match(command: str, argv0_stem: str | None, flags: frozenset[str], stems: tuple[str, ...]) -> bool:
    stem_res = tuple(re.compile(rf"\b{re.escape(stem)}\w*\b", re.IGNORECASE) for stem in stems)
    for argv in command_segments_argv(command):
        if not argv:
            continue
        head = posixpath.basename(argv[0]).lower()
        if argv0_stem is not None and not re.search(rf"\b{re.escape(argv0_stem)}\w*\b", head, re.IGNORECASE):
            continue
        if not flags <= _scope_flag_set(argv):
            continue
        segment_words = " ".join(argv).lower()
        if all(stem_re.search(segment_words) for stem_re in stem_res):
            return True
    return False


def _denied_command_hit(command: str, scope: _ScopeModel) -> str | None:
    if any(pattern.search(command) for pattern in scope.denied_command_patterns):
        return "a denied command pattern"
    for argv0, flags, stems in scope.denied_command_words:
        if _command_words_match(command, argv0, flags, stems):
            return f"the denied command terms {stems!r}"
    return None


def _denied_path_hit(path: str, scope: _ScopeModel) -> str | None:
    for term in scope.denied_paths:
        if _scope_path_hit(path, term):
            return term
    return None


def _outside_allowed(path: str, scope: _ScopeModel) -> bool:
    return bool(scope.allowed_paths) and not any(_scope_path_hit(path, term) for term in scope.allowed_paths)


def _input_strings(value: dict[str, Any] | str | None) -> str:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            if isinstance(item, str):
                strings.append(item)
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                strings.extend(entry for entry in item if isinstance(entry, str))
    return " ".join(strings).lower()


def _call_cwd(event: CanonicalEvent, input_value: dict[str, Any] | str | None) -> str | None:
    if isinstance(input_value, dict):
        cwd = input_value.get("cwd")
        if isinstance(cwd, str):
            return cwd
    cwd = event.payload.get("cwd")
    return cwd if isinstance(cwd, str) else None


def _normalized_command_text(command: str) -> str:
    return " ".join(command.lower().split())


def detect_drift(
    events: Sequence[CanonicalEvent],
    *,
    scope_text: str,
    declared_worktree: str,
    enrolled_items: Sequence[object] = (),
    met_items: Set[str] = frozenset(),
    changed_files: Sequence[str] = (),
) -> list[Finding]:
    """Flag actual work conflicting with the goal's frozen boundaries.

    The check is structural: the frozen ``scope_text`` compiles into denied
    paths, denied command terms, an optional write prohibition, and an
    optional allowed file set; a call is drift when the command it runs, the
    paths it targets, or the files the worktree diff shows changed fall on
    the wrong side of that model — or when a work-capable call leaves
    ``declared_worktree``. Prose clauses the compiler cannot type stay
    residual literal matches, so unrecognized wording keeps its old meaning.
    """
    findings: list[Finding] = []
    unmet = first_unmet_item_id(enrolled_items, met_items)
    scope = _compile_scope(scope_text)
    declared_root = str(Path(declared_worktree).resolve(strict=False)) if declared_worktree else ""
    for event in events:
        if event.normalized_type is not CanonicalType.TOOL_CALL:
            continue
        cls = tool_class(event.payload.get("tool_name"))
        if cls == "plan":
            continue
        input_value = coerce_tool_input(event.payload.get("input"))
        command = command_text(event)
        paths = target_paths(event)
        write_targets = paths if cls == "write" else shell_write_targets(event)
        cwd = _call_cwd(event, input_value)

        denied_command = _denied_command_hit(command, scope) if command else None
        if denied_command is not None:
            findings.append(
                Finding(
                    detector="drift",
                    fingerprint_seed={
                        "excluded_command": _normalized_command_text(command),
                        "scope_term": denied_command,
                    },
                    event_refs=(event.event_id,),
                    unmet_item=unmet,
                    expected_next_progress="return to the enrolled scope and produce evidence toward the first unmet done-when item",
                    detail=f"the command run hits {denied_command} from the goal's frozen scope",
                )
            )
            continue
        if scope.deny_writes and write_targets:
            outside = _semantic_path(write_targets[0], declared_worktree=declared_worktree, cwd=cwd)
            findings.append(
                Finding(
                    detector="drift",
                    fingerprint_seed={"write_denied": outside},
                    event_refs=(event.event_id,),
                    unmet_item=unmet,
                    expected_next_progress="return to the enrolled scope and produce evidence toward the first unmet done-when item",
                    detail=f"the frozen scope forbids writes but the call wrote {write_targets[0]!r}",
                )
            )
            continue
        excluded = next((term for path in paths if (term := _denied_path_hit(path, scope)) is not None), None)
        if excluded is not None:
            hit_path = next(path for path in paths if _denied_path_hit(path, scope) is not None)
            findings.append(
                Finding(
                    detector="drift",
                    fingerprint_seed={
                        "excluded_path": _semantic_path(hit_path, declared_worktree=declared_worktree, cwd=cwd),
                        "scope_term": excluded,
                    },
                    event_refs=(event.event_id,),
                    unmet_item=unmet,
                    expected_next_progress="return to the enrolled scope and produce evidence toward the first unmet done-when item",
                    detail=f"the call targeted {hit_path!r}, excluded by the frozen scope term {excluded!r}",
                )
            )
            continue
        if write_targets and (outside_allowed := next((p for p in write_targets if _outside_allowed(p, scope)), None)):
            findings.append(
                Finding(
                    detector="drift",
                    fingerprint_seed={
                        "outside_allowed": _semantic_path(outside_allowed, declared_worktree=declared_worktree, cwd=cwd)
                    },
                    event_refs=(event.event_id,),
                    unmet_item=unmet,
                    expected_next_progress="return to the enrolled scope and produce evidence toward the first unmet done-when item",
                    detail=f"the call wrote {outside_allowed!r} outside the scope's allowed file set",
                )
            )
            continue
        residual = next(
            (clause for clause in scope.residual_clauses if clause in _input_strings(input_value)),
            None,
        )
        if residual is not None:
            findings.append(
                Finding(
                    detector="drift",
                    fingerprint_seed={
                        "clause": residual,
                        "tool_name": event.payload.get("tool_name"),
                        "paths": tuple(sorted(_semantic_path(p, declared_worktree=declared_worktree, cwd=cwd) for p in paths)),
                        "command": _normalized_command_text(command),
                    },
                    event_refs=(event.event_id,),
                    unmet_item=unmet,
                    expected_next_progress="return to the enrolled scope and produce evidence toward the first unmet done-when item",
                    detail=f"scoped work conflicts with the goal boundary clause {residual!r}",
                )
            )
            continue
        if cls in _WORK_CAPABLE_CLASSES and declared_root:
            outside_target = next(
                (p for p in paths if not _contained_in_worktree(p, declared_worktree=declared_worktree, cwd=cwd)),
                None,
            )
            outside_cwd = (
                cwd
                if isinstance(cwd, str) and not _contained_in_worktree(cwd, declared_worktree=declared_worktree)
                else None
            )
            if outside_target is not None or outside_cwd is not None:
                outside = outside_target or outside_cwd or ""
                findings.append(
                    Finding(
                        detector="drift",
                        fingerprint_seed={
                            "wrong_worktree": _semantic_path(outside, declared_worktree=declared_worktree, cwd=cwd),
                            "declared_worktree": declared_root,
                        },
                        event_refs=(event.event_id,),
                        unmet_item=unmet,
                        expected_next_progress="resume work inside the declared worktree",
                        detail=(
                            f"{event.payload.get('tool_name')} targeted {outside!r} "
                            f"outside the declared worktree {declared_worktree!r}"
                        ),
                    )
                )
    for changed in dict.fromkeys(changed_files):
        excluded = _denied_path_hit(changed, scope)
        if excluded is not None:
            findings.append(
                Finding(
                    detector="drift",
                    fingerprint_seed={"diff_path": changed, "scope_term": excluded},
                    event_refs=(),
                    unmet_item=unmet,
                    expected_next_progress="return to the enrolled scope and produce evidence toward the first unmet done-when item",
                    detail=f"the worktree diff touches {changed!r}, excluded by the frozen scope term {excluded!r}",
                )
            )
            continue
        if _outside_allowed(changed, scope):
            findings.append(
                Finding(
                    detector="drift",
                    fingerprint_seed={"diff_path": changed, "outside_allowed": True},
                    event_refs=(),
                    unmet_item=unmet,
                    expected_next_progress="return to the enrolled scope and produce evidence toward the first unmet done-when item",
                    detail=f"the worktree diff touches {changed!r} outside the scope's allowed file set",
                )
            )
    return findings


def detect_unnecessary_steps(
    events: Sequence[CanonicalEvent],
    *,
    progress_rows: Sequence[ProgressClassification] = (),
    threshold: int = 2,
    enrolled_items: Sequence[object] = (),
    met_items: Set[str] = frozenset(),
) -> list[Finding]:
    """Flag one normalized tool call repeated without progress.

    The repeat identity is the Cline-style signature — normalized tool name
    plus normalized parameters — qualified by the result's coarse outcome
    class, never by result bytes. The recurrence counter resets on verified
    progress between repeats; a changed outcome class starts a new identity
    rather than extending the old one. Two identical outcomes are the first
    evidence of a loop.
    """
    findings: list[Finding] = []
    unmet = first_unmet_item_id(enrolled_items, met_items)
    results = _joined_results(events)
    positions = {event.event_id: position for position, event in enumerate(events)}
    seen: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for position, event in enumerate(events):
        if event.normalized_type is not CanonicalType.TOOL_CALL:
            continue
        result = results.get(event.native_join_id or "")
        signature = (call_signature(event), result_class(result))
        outcome_position = positions.get(result.event_id, position) if result is not None else position
        occurrences = seen.setdefault(signature, [])
        if _has_progress_at(events, progress_rows, outcome_position):
            occurrences.clear()
            continue
        if occurrences and _has_progress_between(events, progress_rows, occurrences[-1][0], outcome_position):
            occurrences.clear()
        occurrences.append((outcome_position, event.event_id))
        if len(occurrences) < threshold:
            continue
        prior = [entry for entry in occurrences[:-1]]
        start_position = prior[-1][0]
        if _has_progress_between(events, progress_rows, start_position, outcome_position):
            occurrences.clear()
            occurrences.append((position, event.event_id))
            continue
        refs = tuple(entry[1] for entry in occurrences[-threshold:])
        kind = action_kind(event)
        findings.append(
            Finding(
                detector="unnecessary_steps",
                fingerprint_seed={"signature": signature},
                event_refs=refs,
                unmet_item=unmet,
                expected_next_progress=f"change approach so the repeated {kind} produces new scoped state",
                detail=f"the identical normalized tool call repeated {threshold} times with no intervening verified progress",
            )
        )
        occurrences.clear()
    return findings


def detect_excessive_testing(
    events: Sequence[CanonicalEvent],
    *,
    progress_rows: Sequence[ProgressClassification] = (),
    threshold: int = 2,
    enrolled_items: Sequence[object] = (),
    met_items: Set[str] = frozenset(),
) -> list[Finding]:
    """Flag a check suite repeating with no artifact change, new failure
    signature, or newly exercised required surface.

    The suite identity comes from the command actually run — ``pytest``,
    ``uv run pytest``, ``npm test``, ``make check`` — parsed by
    :func:`chitra.journal.tools.check_signature`, qualified by the result's
    coarse outcome class so a flipped result starts a new streak.
    """
    findings: list[Finding] = []
    unmet = first_unmet_item_id(enrolled_items, met_items)
    results = _joined_results(events)
    positions = {event.event_id: position for position, event in enumerate(events)}
    runs: list[tuple[int, tuple[tuple[str, tuple[str, ...]], str], CanonicalEvent]] = []
    for position, event in enumerate(events):
        if event.normalized_type is not CanonicalType.TOOL_CALL:
            continue
        check = check_signature(event)
        if check is not None:
            result = results.get(event.native_join_id or "")
            signature = (check, result_class(result))
            outcome_position = positions.get(result.event_id, position) if result is not None else position
            if _has_progress_at(events, progress_rows, outcome_position):
                continue
            runs.append((outcome_position, signature, event))
    streak: list[tuple[int, tuple[tuple[str, tuple[str, ...]], str], CanonicalEvent]] = []
    for run in runs:
        if streak and streak[-1][1] == run[1] and not _has_progress_between(events, progress_rows, streak[-1][0], run[0]):
            streak.append(run)
        else:
            streak = [run]
        if len(streak) < threshold:
            continue
        start_position = streak[len(streak) - threshold][0]
        if _has_progress_between(events, progress_rows, start_position, run[0]):
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
        streak = []
    return findings


def _write_targets(event: CanonicalEvent) -> tuple[str, ...]:
    """Paths a call actually writes: explicit targets for write tools,
    redirect/exec targets for shell commands."""
    cls = tool_class(event.payload.get("tool_name"))
    if cls == "write":
        return target_paths(event)
    if cls in {"shell", "other"}:
        return shell_write_targets(event)
    return ()


def detect_document_dithering(
    events: Sequence[CanonicalEvent],
    *,
    goal_is_document: bool,
    minimum_recurrence: int = 3,
    enrolled_items: Sequence[object] = (),
    met_items: Set[str] = frozenset(),
) -> list[Finding]:
    """For a non-document goal, flag recurring document edits while required
    implementation items gain no evidence. Disabled entirely for doc goals.

    A document churn event is any write-class call whose targets are all
    prose files — Edit on ``notes.md``, or ``apply_patch``/redirects hitting
    only docs. Validation runs and code-path writes are implementation
    evidence and reset the dithering case.
    """
    if goal_is_document:
        return []
    unmet = first_unmet_item_id(enrolled_items, met_items)
    doc_events: list[CanonicalEvent] = []
    implementation_evidence = False
    for event in events:
        if event.normalized_type is not CanonicalType.TOOL_CALL:
            continue
        cls = tool_class(event.payload.get("tool_name"))
        if cls not in _WORK_CAPABLE_CLASSES:
            continue
        targets = _write_targets(event)
        if targets and all(path.lower().endswith(tuple(_DOC_SUFFIXES)) for path in targets):
            doc_events.append(event)
            continue
        if any(path.lower().endswith(tuple(_CODE_SUFFIXES)) for path in targets) or check_signature(event) is not None:
            implementation_evidence = True
    if implementation_evidence or len(doc_events) < minimum_recurrence:
        return []
    refs = tuple(event.event_id for event in doc_events[:minimum_recurrence])
    semantic_targets = tuple(
        sorted({path.lower() for event in doc_events[:minimum_recurrence] for path in _write_targets(event)})
    )
    return [
        Finding(
            detector="document_dithering",
            fingerprint_seed={"doc_targets": semantic_targets, "minimum_recurrence": minimum_recurrence},
            event_refs=refs,
            unmet_item=unmet,
            expected_next_progress=(
                "produce implementation or validation evidence for the first unmet done-when item instead of further prose churn"
            ),
            detail=f"{len(doc_events)} document edits with no implementation or validation evidence for a code goal",
        )
    ]


def detect_stall(
    events: Sequence[CanonicalEvent],
    *,
    minimum_turns: int = 2,
    enrolled_items: Sequence[object] = (),
) -> list[Finding]:
    """Flag consecutive turn-ends that produced narration without action.

    The codexmon distinction: a lane with a tool call still in flight is
    mid-command, not idle, so any unanswered tail call suppresses the
    finding. A lane whose last ``minimum_turns`` completed turns emitted
    only final-response text — no tool call, no result — is stalling.
    Completion claims and questions are left to the detectors and handlers
    that own them.
    """
    if unanswered_tail_calls(events):
        return []
    trailing: list[CanonicalEvent] = []
    for event in reversed(events):
        if event.normalized_type in {
            CanonicalType.TOOL_CALL,
            CanonicalType.TOOL_RESULT,
            CanonicalType.TOOL_ERROR,
        }:
            break
        if event.normalized_type is CanonicalType.FINAL_RESPONSE:
            trailing.append(event)
    if len(trailing) < minimum_turns:
        return []
    latest_text = trailing[0].payload.get("text")
    if isinstance(latest_text, str) and (is_completion_claim(latest_text) or "?" in latest_text):
        return []
    return [
        Finding(
            detector="stall",
            fingerprint_seed={"streak_anchor": trailing[-1].event_id},
            event_refs=tuple(event.event_id for event in reversed(trailing)),
            unmet_item=first_unmet_item_id(enrolled_items),
            expected_next_progress="take the next in-scope tool action or answer the pending question instead of narrating",
            detail=(
                f"the last {len(trailing)} completed turns produced narration "
                "with no tool call, result, or in-flight command"
            ),
        )
    ]


def detect_false_done(
    *,
    final_response: CanonicalEvent | None,
    enrolled_items: Sequence[object],
    receipt_names_by_item: dict[str, str],
    receipt_roots: dict[str, object] | None = None,
    session_ref: str = "",
    target_dirty: bool = False,
    material_questions: Sequence[str] = (),
    live_proof_required: bool = False,
    live_proof_present: bool = True,
    verified_results: Mapping[str, str] | None = None,
) -> list[Finding]:
    """Reject a completion claim that conflicts with goal state.

    Every open item, missing/stale/failed receipt, or absent claim binding
    yields one finding naming the blocking condition. Only exact current
    bindings of every item pass silently. When ``verified_results`` is
    supplied (receipt name → ``pass``/``fail``), it is a worker-recorded
    verification map and stands in for re-running ``verify_receipt`` on the
    monitor pass; the receipt file itself is still read for validator
    binding.
    """
    findings: list[Finding] = []
    root: Path | None = None
    root_available = False
    if receipt_roots is not None and session_ref:
        candidate = receipt_roots.get(session_ref)
        if isinstance(candidate, Path):
            root = candidate
        elif isinstance(candidate, str):
            root = Path(candidate)
        root_available = root is not None and root.exists()
    unmet = first_unmet_item_id(
        enrolled_items,
        met_done_items(enrolled_items, receipt_root=root, session_ref=session_ref)
        if root_available and root is not None
        else frozenset(),
    )
    if final_response is None:
        return [
            Finding(
                detector="false_done",
                fingerprint_seed={"item": unmet, "reason": "exit-before-contract"},
                event_refs=(),
                unmet_item=unmet,
                expected_next_progress="produce a final response that binds the completion contract to current evidence",
                detail="session exited before a final response could bind the completion contract",
            )
        ]
    final_text = _final_response_text(final_response)
    if not is_completion_claim(final_text):
        return []
    refs: tuple[str, ...] = (final_response.event_id,)
    if target_dirty:
        findings.append(
            Finding(
                detector="false_done",
                fingerprint_seed={"reason": "dirty-target"},
                event_refs=refs,
                unmet_item=unmet,
                expected_next_progress="cleanly commit or discard target worktree changes before claiming completion",
                detail="completion claim was made while the target worktree was dirty",
            )
        )
    if material_questions:
        findings.append(
            Finding(
                detector="false_done",
                fingerprint_seed={"reason": "material-questions", "questions": tuple(material_questions)},
                event_refs=refs,
                unmet_item=unmet,
                expected_next_progress="answer or carry forward material open questions before claiming completion",
                detail="completion claim was made while material questions remained open",
            )
        )
    if live_proof_required and not live_proof_present:
        findings.append(
            Finding(
                detector="false_done",
                fingerprint_seed={"reason": "absent-live-proof"},
                event_refs=refs,
                unmet_item=unmet,
                expected_next_progress="produce the required live proof before claiming completion",
                detail="completion claim was made without the required live proof",
            )
        )
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
        if not root_available or root is None:
            findings.append(
                Finding(
                    detector="false_done",
                    fingerprint_seed={"item": item_id, "reason": "receipt-store-unavailable"},
                    event_refs=refs,
                    unmet_item=item_id,
                    expected_next_progress=f"make the validation receipt store available and verify receipt {required_receipt!r}",
                    detail=f"required receipt store/root is unavailable for session {session_ref!r}",
                )
            )
            continue
        try:
            receipt, _raw = load_receipt_file(receipt_path(root, session_ref, required_receipt))
            if verified_results is None:
                eligible = verify_receipt(root, session_ref, required_receipt).completion_eligible
            else:
                eligible = verified_results.get(required_receipt) == "pass"
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
        if not eligible or validator not in validator_names:
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


def detect_deferral_language(
    events: Sequence[CanonicalEvent],
    *,
    enrolled_items: Sequence[object] = (),
    met_items: Set[str] = frozenset(),
    phrases: Sequence[str] | None = None,
) -> list[Finding]:
    """Flag deferral vocabulary in every assistant turn, not only claims.

    The completion gate already scans claim text for deferral phrases; a lane
    that defers work in an ordinary turn never reaches that path, so the same
    deterministic vocabulary runs on every final response and the finding
    binds the actually-unmet enrolled item.
    """
    findings: list[Finding] = []
    unmet = first_unmet_item_id(enrolled_items, met_items)
    for event in events:
        if event.normalized_type is not CanonicalType.FINAL_RESPONSE:
            continue
        text = _final_response_text(event)
        if not text:
            continue
        matches = scan_deferral_language(text, phrases=phrases)
        if not matches:
            continue
        matched_phrases = tuple(dict.fromkeys(match["phrase"] for match in matches))
        findings.append(
            Finding(
                detector="deferral",
                fingerprint_seed={"event_id": event.event_id, "phrases": matched_phrases},
                event_refs=(event.event_id,),
                unmet_item=unmet,
                expected_next_progress=(
                    "bind the deferred work to the unmet done-when item and produce its evidence now, "
                    "or carry it forward through an explicit ask"
                ),
                detail=f"assistant turn defers work without binding it to evidence: {', '.join(matched_phrases)}",
            )
        )
    return findings


def _final_response_text(event: CanonicalEvent) -> str:
    value = event.payload.get("text")
    return value if isinstance(value, str) else ""


__all__ = [
    "DETECTOR_VERSION",
    "Finding",
    "detect_deferral_language",
    "detect_document_dithering",
    "detect_drift",
    "detect_excessive_testing",
    "detect_false_done",
    "detect_stall",
    "detect_unnecessary_steps",
    "first_unmet_item",
    "first_unmet_item_id",
    "met_done_items",
]

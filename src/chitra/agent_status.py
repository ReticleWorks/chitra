"""Deterministic layered agent-status detection for live tmux panes.

Lifecycle reports are authoritative for the pane/session pair that reported
them.  Otherwise a bounded TOML manifest classifies the captured bottom
buffer.  Screen-derived ``blocked`` is deliberately unavailable unless a
matched rule names a recognized visible blocker kind.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal, cast

AgentState = Literal["idle", "working", "blocked", "done", "unknown", "rate_limited_hard", "rate_limited_warn"]
ManifestState = Literal["idle", "working", "blocked", "rate_limited_hard", "rate_limited_warn"]
MatcherKind = Literal["contains", "regex", "line_regex"]
ManifestSourceKind = Literal["local", "bundled"]
BlockerKind = Literal["approval", "question", "permission"]

MANIFEST_STATES = ("idle", "working", "blocked", "rate_limited_hard", "rate_limited_warn")
AGENT_STATES = ("idle", "working", "blocked", "done", "unknown", "rate_limited_hard", "rate_limited_warn")
# A pane that has hit a provider cap still draws its input row, so without a
# state of its own it classifies as idle. That is exactly how a Codex lane sat
# on a weekly hard cap for roughly two days: the monitor saw idle and had no
# reason to look closer. These two states are therefore never overridden by
# another rule that also matched.
RATE_LIMITED_STATES = frozenset({"rate_limited_hard", "rate_limited_warn"})

DEFAULT_KNOWN_AGENT_IDLE_FALLBACK = "default_known_agent_idle_fallback"
UNKNOWN_AGENT_IDLE_FALLBACK = "unknown_agent_idle_fallback"
MANIFEST_ERROR_IDLE_FALLBACK = "manifest_error_idle_fallback"
LIFECYCLE_AUTHORITY_SKIP_REASON = "integration_authoritative"
SCHEMA_VERSION = 1
MAX_RULES = 128
MAX_MATCHERS_PER_RULE = 32
MAX_PATTERN_CHARS = 512
MAX_SNAPSHOT_CHARS = 64 * 1024
SUPPORTED_REGIONS = frozenset({"whole", "bottom"})
SUPPORTED_BLOCKER_KINDS = frozenset({"approval", "question", "permission"})
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

# "…or try again at Aug 19th, 2026 11:37 PM." — the tail of the Codex hard-cap
# banner. The date is the one thing in the banner an operator can act on, so it
# is parsed out rather than left in prose.
_RESUME_AT_RE = re.compile(r"try again at\s+(?P<when>[^.\n]+?)\s*[.\n]", re.IGNORECASE)
_ORDINAL_RE = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)
_RESUME_AT_FORMATS = (
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d %Y %I:%M %p",
    "%B %d %Y %I:%M %p",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
)


def parse_resume_at(text: str) -> str | None:
    """Return the banner's resume time as an ISO8601-UTC string, or None.

    The banner carries no timezone and no seconds. It is rendered in the pane's
    own local time, so it is read in the host's local zone and converted; the
    result is therefore minute-precision. When a provider also reports a
    ``resets_at`` epoch -- Codex does, through ``chitra-usage`` -- that reading
    is the more precise one and should win. This exists for the case the
    incident actually presented: a capped pane on screen and no usage reading
    for that host at all.
    """
    match = _RESUME_AT_RE.search(text)
    if match is None:
        return None
    raw = _ORDINAL_RE.sub("", " ".join(match.group("when").split()))
    for pattern in _RESUME_AT_FORMATS:
        try:
            parsed = datetime.strptime(raw, pattern)
        except ValueError:
            continue
        local = parsed.astimezone() if parsed.tzinfo is None else parsed
        return local.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return None


def default_manifest_dir() -> Path:
    """Return Chitra's native local-override directory."""
    configured = os.environ.get("CHITRA_AGENT_MANIFEST_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "chitra" / "agent-detection"


class ManifestError(ValueError):
    """A detection manifest is invalid or cannot be read safely."""


@dataclass(frozen=True, slots=True)
class Matcher:
    """One bounded literal or regular-expression screen matcher."""

    kind: MatcherKind
    value: str
    case_sensitive: bool
    compiled: re.Pattern[str] | None = None

    def matches(self, text: str) -> bool:
        if self.kind == "contains":
            if self.case_sensitive:
                return self.value in text
            return self.value.casefold() in text.casefold()
        assert self.compiled is not None
        if self.kind == "line_regex":
            return any(self.compiled.search(line) is not None for line in text.splitlines())
        return self.compiled.search(text) is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "value": self.value,
            "case_sensitive": self.case_sensitive,
        }


@dataclass(frozen=True, slots=True)
class ManifestRule:
    """One explicit AND/OR/NOT evidence gate."""

    identifier: str
    state: ManifestState
    priority: int
    region: Literal["whole", "bottom"]
    lines: int
    blocker_kind: BlockerKind | None
    all_matchers: tuple[Matcher, ...]
    any_matchers: tuple[Matcher, ...]
    not_matchers: tuple[Matcher, ...]

    def region_text(self, snapshot: str) -> str:
        if self.region == "whole":
            return snapshot
        return "\n".join(snapshot.splitlines()[-self.lines :])

    def evaluate(self, snapshot: str) -> RuleEvaluation:
        text = self.region_text(snapshot)
        all_results = tuple(matcher.matches(text) for matcher in self.all_matchers)
        any_results = tuple(matcher.matches(text) for matcher in self.any_matchers)
        not_results = tuple(matcher.matches(text) for matcher in self.not_matchers)
        matched = all(all_results) and (not any_results or any(any_results)) and not any(not_results)
        return RuleEvaluation(
            rule_id=self.identifier,
            state=self.state,
            priority=self.priority,
            region=self.region,
            region_lines=self.lines,
            matched=matched,
            all_results=all_results,
            any_results=any_results,
            not_results=not_results,
        )


@dataclass(frozen=True, slots=True)
class AgentManifest:
    """A parsed Chitra agent-detection manifest."""

    agent: str
    version: str
    source: str
    source_kind: ManifestSourceKind
    rules: tuple[ManifestRule, ...]


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """Explain evidence for one evaluated manifest rule."""

    rule_id: str
    state: ManifestState
    priority: int
    region: str
    region_lines: int
    matched: bool
    all_results: tuple[bool, ...]
    any_results: tuple[bool, ...]
    not_results: tuple[bool, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "state": self.state,
            "priority": self.priority,
            "region": self.region,
            "region_lines": self.region_lines,
            "matched": self.matched,
            "all_results": list(self.all_results),
            "any_results": list(self.any_results),
            "not_results": list(self.not_results),
        }


@dataclass(frozen=True, slots=True)
class DetectionExplain:
    """Complete status-authority and manifest evidence for one pane."""

    agent: str
    state: AgentState
    authority: Literal["integration", "manifest", "fallback", "completion"]
    source: str | None
    source_kind: Literal["integration", "local", "bundled", "internal"] | None
    manifest_version: str | None
    matched_rule: str | None
    blocker_kind: BlockerKind | None
    fallback_reason: str | None
    screen_detection_skipped: bool
    screen_detection_skip_reason: str | None
    evaluated_rules: tuple[RuleEvaluation, ...]
    warning: str | None = None
    # Set only for rate_limited_hard, and only when the banner named a time.
    # A cap with no readable resume time still reports the state; the response
    # protocol then has to source the time from the provider reading instead.
    resume_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "state": self.state,
            "authority": self.authority,
            "source": self.source,
            "source_kind": self.source_kind,
            "manifest_version": self.manifest_version,
            "matched_rule": self.matched_rule,
            "blocker_kind": self.blocker_kind,
            "fallback_reason": self.fallback_reason,
            "screen_detection_skipped": self.screen_detection_skipped,
            "screen_detection_skip_reason": self.screen_detection_skip_reason,
            "evaluated_rules": [evaluation.to_dict() for evaluation in self.evaluated_rules],
            "warning": self.warning,
            "resume_at": self.resume_at,
        }


def integration_explain(*, agent: str, state: AgentState, source: str) -> DetectionExplain:
    """Build explain output for an authoritative lifecycle report."""
    return DetectionExplain(
        agent=agent,
        state=state,
        authority="integration",
        source=source,
        source_kind="integration",
        manifest_version=None,
        matched_rule=None,
        blocker_kind=None,
        fallback_reason=None,
        screen_detection_skipped=True,
        screen_detection_skip_reason=LIFECYCLE_AUTHORITY_SKIP_REASON,
        evaluated_rules=(),
    )


def completion_explain(*, agent: str) -> DetectionExplain:
    """Build explain output for Chitra's verified completion boundary."""
    return DetectionExplain(
        agent=agent,
        state="done",
        authority="completion",
        source="chitra:completion_gate",
        source_kind="internal",
        manifest_version=None,
        matched_rule=None,
        blocker_kind=None,
        fallback_reason=None,
        screen_detection_skipped=True,
        screen_detection_skip_reason="completion_gate_authoritative",
        evaluated_rules=(),
    )


def _mapping(value: object, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{path} must be a table")
    return cast(dict[str, Any], value)


def _exact_fields(mapping: dict[str, Any], expected: set[str], *, path: str) -> None:
    unknown = sorted(set(mapping) - expected)
    if unknown:
        raise ManifestError(f"{path} has unsupported fields: {', '.join(unknown)}")


def _text(mapping: dict[str, Any], key: str, *, path: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{path}.{key} must be a non-empty string")
    return value


def _integer(mapping: dict[str, Any], key: str, *, path: str, default: int) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{path}.{key} must be an integer")
    return value


def _parse_matcher(value: object, *, path: str) -> Matcher:
    raw = _mapping(value, path=path)
    _exact_fields(raw, {"kind", "value", "case_sensitive"}, path=path)
    kind = _text(raw, "kind", path=path)
    if kind not in ("contains", "regex", "line_regex"):
        raise ManifestError(f"{path}.kind must be contains, regex, or line_regex")
    pattern = _text(raw, "value", path=path)
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ManifestError(f"{path}.value exceeds {MAX_PATTERN_CHARS} characters")
    case_sensitive = raw.get("case_sensitive", False)
    if not isinstance(case_sensitive, bool):
        raise ManifestError(f"{path}.case_sensitive must be boolean")
    compiled: re.Pattern[str] | None = None
    if kind != "contains":
        flags = 0 if case_sensitive else re.IGNORECASE
        if kind == "regex":
            flags |= re.MULTILINE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ManifestError(f"{path}.value is not a valid regular expression: {exc}") from exc
    return Matcher(kind=cast(MatcherKind, kind), value=pattern, case_sensitive=case_sensitive, compiled=compiled)


def _parse_matchers(raw: dict[str, Any], key: str, *, path: str) -> tuple[Matcher, ...]:
    values = raw.get(key, [])
    if not isinstance(values, list):
        raise ManifestError(f"{path}.{key} must be an array of matcher tables")
    if len(values) > MAX_MATCHERS_PER_RULE:
        raise ManifestError(f"{path}.{key} exceeds {MAX_MATCHERS_PER_RULE} matchers")
    return tuple(_parse_matcher(value, path=f"{path}.{key}[{index}]") for index, value in enumerate(values))


def _parse_rule(value: object, *, index: int) -> ManifestRule:
    path = f"manifest.rules[{index}]"
    raw = _mapping(value, path=path)
    _exact_fields(raw, {"id", "state", "priority", "region", "lines", "blocker_kind", "all", "any", "not"}, path=path)
    identifier = _text(raw, "id", path=path)
    if _RULE_ID_RE.fullmatch(identifier) is None:
        raise ManifestError(f"{path}.id must be a safe diagnostic identifier")
    state = _text(raw, "state", path=path)
    if state not in MANIFEST_STATES:
        raise ManifestError(f"{path}.state must be one of: {', '.join(MANIFEST_STATES)}")
    region = raw.get("region", "bottom")
    if region not in SUPPORTED_REGIONS:
        raise ManifestError(f"{path}.region must be whole or bottom")
    lines = _integer(raw, "lines", path=path, default=20)
    if lines < 1 or lines > 200:
        raise ManifestError(f"{path}.lines must be between 1 and 200")
    blocker_kind_raw = raw.get("blocker_kind")
    blocker_kind: BlockerKind | None = None
    if blocker_kind_raw is not None:
        if blocker_kind_raw not in SUPPORTED_BLOCKER_KINDS:
            raise ManifestError(f"{path}.blocker_kind must be approval, question, or permission")
        blocker_kind = cast(BlockerKind, blocker_kind_raw)
    if state == "blocked" and blocker_kind is None:
        raise ManifestError(f"{path}.blocked rules require blocker_kind")
    if state == "blocked" and region == "whole":
        raise ManifestError(f"{path}.blocked rules must use the live bottom region")
    if state != "blocked" and blocker_kind is not None:
        raise ManifestError(f"{path}.blocker_kind is valid only for blocked rules")
    all_matchers = _parse_matchers(raw, "all", path=path)
    any_matchers = _parse_matchers(raw, "any", path=path)
    not_matchers = _parse_matchers(raw, "not", path=path)
    if not all_matchers and not any_matchers:
        raise ManifestError(f"{path} requires at least one positive matcher in all or any")
    return ManifestRule(
        identifier=identifier,
        state=cast(ManifestState, state),
        priority=_integer(raw, "priority", path=path, default=0),
        region=cast(Literal["whole", "bottom"], region),
        lines=lines,
        blocker_kind=blocker_kind,
        all_matchers=all_matchers,
        any_matchers=any_matchers,
        not_matchers=not_matchers,
    )


def parse_manifest(text: str, *, source: str, source_kind: ManifestSourceKind) -> AgentManifest:
    """Parse one strict, bounded TOML manifest."""
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"invalid TOML in {source}: {exc}") from exc
    raw = _mapping(payload, path="manifest")
    _exact_fields(raw, {"schema_version", "agent", "version", "rules"}, path="manifest")
    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ManifestError(f"manifest.schema_version must be {SCHEMA_VERSION}")
    agent = _text(raw, "agent", path="manifest")
    if _AGENT_RE.fullmatch(agent) is None:
        raise ManifestError("manifest.agent must be a lowercase Chitra agent identifier")
    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ManifestError("manifest.rules must be a non-empty array")
    if len(rules_raw) > MAX_RULES:
        raise ManifestError(f"manifest.rules exceeds {MAX_RULES} entries")
    rules = tuple(_parse_rule(value, index=index) for index, value in enumerate(rules_raw))
    identifiers = [rule.identifier for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        raise ManifestError("manifest rule ids must be unique")
    ordered = tuple(sorted(rules, key=lambda rule: rule.priority, reverse=True))
    version = _text(raw, "version", path="manifest")
    if version != version.strip() or len(version) > 80 or any(ord(character) < 0x20 for character in version):
        raise ManifestError("manifest.version must be a canonical string of at most 80 characters")
    return AgentManifest(
        agent=agent,
        version=version,
        source=source,
        source_kind=source_kind,
        rules=ordered,
    )


class ManifestRepository:
    """Load local overrides before package-bundled manifests."""

    def __init__(self, local_dir: Path | None = None) -> None:
        self.local_dir = local_dir or default_manifest_dir()

    def load(self, agent: str) -> AgentManifest | None:
        if _AGENT_RE.fullmatch(agent) is None:
            return None
        if self.local_dir is not None:
            local_path = self.local_dir / f"{agent}.toml"
            if local_path.exists():
                try:
                    text = local_path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise ManifestError(f"cannot read local manifest {local_path}: {exc}") from exc
                manifest = parse_manifest(text, source=str(local_path), source_kind="local")
                if manifest.agent != agent:
                    raise ManifestError(f"local manifest {local_path} declares agent {manifest.agent!r}, expected {agent!r}")
                return manifest
        resource = resources.files("chitra").joinpath("agent_detection", f"{agent}.toml")
        if not resource.is_file():
            return None
        manifest = parse_manifest(resource.read_text(encoding="utf-8"), source=f"package:{agent}.toml", source_kind="bundled")
        if manifest.agent != agent:
            raise ManifestError(f"bundled manifest declares agent {manifest.agent!r}, expected {agent!r}")
        return manifest


def classify_snapshot(snapshot: str, *, agent: str, repository: ManifestRepository) -> DetectionExplain:
    """Classify a bottom-buffer snapshot, biased to idle on every ambiguity."""
    bounded_snapshot = snapshot[-MAX_SNAPSHOT_CHARS:]
    try:
        manifest = repository.load(agent)
    except ManifestError as exc:
        return DetectionExplain(
            agent=agent,
            state="idle",
            authority="fallback",
            source=None,
            source_kind=None,
            manifest_version=None,
            matched_rule=None,
            blocker_kind=None,
            fallback_reason=MANIFEST_ERROR_IDLE_FALLBACK,
            screen_detection_skipped=False,
            screen_detection_skip_reason=None,
            evaluated_rules=(),
            warning=str(exc),
        )
    if manifest is None:
        return DetectionExplain(
            agent=agent,
            state="idle",
            authority="fallback",
            source=None,
            source_kind=None,
            manifest_version=None,
            matched_rule=None,
            blocker_kind=None,
            fallback_reason=UNKNOWN_AGENT_IDLE_FALLBACK,
            screen_detection_skipped=False,
            screen_detection_skip_reason=None,
            evaluated_rules=(),
        )
    evaluations: list[RuleEvaluation] = []
    matched_rules: list[ManifestRule] = []
    for rule in manifest.rules:
        evaluation = rule.evaluate(bounded_snapshot)
        evaluations.append(evaluation)
        if evaluation.matched:
            matched_rules.append(rule)
    matched_rule = matched_rules[0] if matched_rules else None
    # A capped pane draws an input row and can even still show a working
    # footer from the turn that hit the wall, so both an idle and a working
    # rule can match at the same time. Neither is the state that matters:
    # until the cap lifts the lane is going nowhere, and calling it idle is
    # what made the last one invisible for two days.
    rate_limited = next((rule for rule in matched_rules if rule.state in RATE_LIMITED_STATES), None)
    if rate_limited is not None:
        matched_rule = rate_limited
    elif matched_rule is not None and matched_rule.state == "blocked":
        # A live working footer is newer evidence than blocker-shaped text
        # retained above it in the bounded capture. Working rules therefore
        # suppress a simultaneous screen-derived blocker match regardless of
        # manifest priority. Bundled working rules are anchored to live footer
        # shapes so ordinary prose cannot trigger this override.
        matched_rule = next((rule for rule in matched_rules if rule.state == "working"), matched_rule)
    if matched_rule is None:
        return DetectionExplain(
            agent=agent,
            state="idle",
            authority="fallback",
            source=manifest.source,
            source_kind=manifest.source_kind,
            manifest_version=manifest.version,
            matched_rule=None,
            blocker_kind=None,
            fallback_reason=DEFAULT_KNOWN_AGENT_IDLE_FALLBACK,
            screen_detection_skipped=False,
            screen_detection_skip_reason=None,
            evaluated_rules=tuple(evaluations),
        )
    return DetectionExplain(
        agent=agent,
        state=matched_rule.state,
        authority="manifest",
        source=manifest.source,
        source_kind=manifest.source_kind,
        manifest_version=manifest.version,
        matched_rule=matched_rule.identifier,
        blocker_kind=matched_rule.blocker_kind,
        fallback_reason=None,
        resume_at=(
            parse_resume_at(matched_rule.region_text(bounded_snapshot))
            if matched_rule.state == "rate_limited_hard"
            else None
        ),
        screen_detection_skipped=False,
        screen_detection_skip_reason=None,
        evaluated_rules=tuple(evaluations),
    )

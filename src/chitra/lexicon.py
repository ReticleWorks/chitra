"""Shared deterministic language patterns used by Chitra's policy gates."""

from __future__ import annotations

import re

from chitra.taxonomy import cue_phrases

# The shipped deferral vocabulary lives in the packaged evasion taxonomy
# (taxonomy.json, DEFERRAL_STUB entry), the single source for the completion
# gate's cue phrases; this re-exports it for lexicon callers. Incident scar
# tissue lives in chitra.policy_config (see
# INCIDENT_COMPLETION_DEFERRAL_PHRASES), not in the taxonomy: those are
# one-off findings named verbatim, not cue-derived reviewer phrases.
COMPLETION_DEFERRAL_PHRASES: tuple[str, ...] = cue_phrases("DEFERRAL_STUB")

COMPLETION_CLAIM_RE = re.compile(
    r"^\s*(?:"
    r"(?:(?:I|we|it|this|task|work|lane)|the\s+[^\n.!?]{1,80}?)"
    r"\s+(?:am|is|was|are|were|now|has|have)\s+"
    r")?"
    # A markdown emphasis marker (e.g. "**DONE**") may sit between the
    # optional subject clause and the claim word without breaking the
    # sentence-initial anchor.
    r"(?:[*_`]{1,3}\s*)?"
    r"(done|complete(?:d)?|finished|fixed|repaired|shipped|deployed|publication-ready|ready for (?:merge|release))\b",
    re.I | re.M,
)
COMPLETION_EVIDENCE_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)
COMPLETION_EVIDENCE_PATH_RE = re.compile(
    r"(?:^|\s)(?:/|\./)[^\s,;]+|\b[^\s]+\.(?:json|jsonl|log|png|jpg|jpeg|webp|txt)\b", re.I
)
COMPLETION_EVIDENCE_PR_RE = re.compile(r"\b(?:merged\s+)?pr\s*#\d+\b", re.I)
COMPLETION_EVIDENCE_LIVE_RESULT_RE = re.compile(
    r"\b(?:health|probe|curl|http|status|requests?|latency|exit)\b[^\n]*\b\d+(?:\.\d+)?\b", re.I
)
COMPLETION_EVIDENCE_FAILURE_RE = re.compile(r"\b(?:error|failed|failure|http)\b[^\n]*\b(?:[45]\d\d|\d+)\b", re.I)

# Known intentional drift: delivery briefs accept a broader, single-pattern
# evidence vocabulary than completion citations. Keep both variants distinct
# until a later behavior-changing stage resolves their semantics.
ARTIFACT_WORK_EVIDENCE_RE = re.compile(
    r"(?:\b[0-9a-f]{7,40}\b|(?:^|\s)(?:/|\./)[^\s]+|\b\d+\s+(?:passed|requests?|checks?)\b|"
    r"\b(?:status|http|probe|exit)\s*[=: ]\s*\d+\b)",
    re.I,
)
ARTIFACT_PROCESS_ONLY_RE = re.compile(
    r"\b(i (?:reviewed|worked|investigated|started|followed)|steps? (?:taken|performed))\b", re.I
)

OPERATOR_GATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("spend", re.compile(r"\b(spend|purchase|buy|billing|payment|paid plan|costs?\s+\$)\b", re.I)),
    ("credentials", re.compile(r"\b(credentials?|password|secret|api[- ]?key|oauth|login|authentication token)\b", re.I)),
    ("irreversible action", re.compile(r"\b(irreversible|delete|destroy|drop database|force[- ]push|terminate|revoke)\b", re.I)),
    ("strategy redirect", re.compile(r"\b(redirect|change (?:the )?goal|switch objectives?|expand (?:the )?scope)\b", re.I)),
)

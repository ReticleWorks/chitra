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

# One completion-claim vocabulary shared by the completion gate and the
# detectors. The claim word sits at the start of a line, optionally behind a
# short subject clause that ends in a copula ("all work is ...", "the fix has
# ..."), so natural phrasings land without matching mid-sentence filler. A
# claim plus a same-line negation ("the work is not done", "still working")
# is not a claim; ``is_completion_claim`` applies the sieve per line.
COMPLETION_CLAIM_RE = re.compile(
    r"^\s*(?:"
    r"[^\n.!?;]{0,80}?\s+"
    r"(?:am|is|was|are|were|has|have|had|been|now|finally)\s+"
    r"|[A-Za-z][A-Za-z ]{0,60}?['’](?:m|s|re|ve|d)\s+"
    r")?"
    # A markdown emphasis marker (e.g. "**DONE**") may sit between the
    # optional subject clause and the claim word without breaking the
    # sentence-initial anchor.
    r"(?:[*_`]{1,3}\s*)?"
    r"(?:"
    r"done|complete(?:d)?|finished|fixed|repaired|implemented|resolved|"
    r"shipped|deployed|publication[- ]ready|all\s+set|wrapped\s+up"
    r"|(?:task|work|job|step|phase|part|item|ticket|build|deploy(?:ment)?|implementation|fix|"
    r"migration|patch|review|analysis|writeup|write-up)\s+"
    r"\w{0,15}?\s*(?:is\s+|was\s+|now\s+)?(?:done|complete|completed|finished)\b"
    r"|ready\s+(?:for|to)\s+"
    r"(?:merge|merging|ship|shipping|release|review|publication|submission|deploy\w*|"
    r"publish\w*|close|closing|submit|land|landing|sign[- ]?off|delivery)\w*"
    r"|ready\b(?!\s+to\b)"
    r"|all\s+(?:the\s+)?tests?\s+(?:now\s+)?(?:pass(?:es|ed|ing)?|are\s+green|is\s+green)"
    r"|tests?\s+(?:are|is|now)\s+green"
    r"|all\s+(?:the\s+)?checks?\s+(?:now\s+)?pass(?:es|ed|ing)?"
    r")\b",
    re.I | re.M,
)
# Negation applies per line: a claim survives caveats on other lines but not
# its own ("done" beside "not done" on the same line is no claim).
COMPLETION_NEGATION_RE = re.compile(
    r"\b(?:nothing|none|no\s+part)\s+(?:is|was|are|were)\s+(?:done|complete|finished|ready)\b"
    r"|\bnot\s+(?:quite\s+|fully\s+|entirely\s+|completely\s+|yet\s+)*"
    r"(?:done|complete|completed|finished|ready|implemented|fixed|resolved)\b"
    r"|\b(?:isn't|is\s+not|aren't|are\s+not|wasn't|was\s+not|weren't|were\s+not|"
    r"haven't|hasn't|have\s+not|hadn't|had\s+not)\s+"
    r"(?:quite\s+|fully\s+|entirely\s+|completely\s+)?"
    r"(?:done|complete|completed|finished|ready|implemented|fixed|resolved)\b"
    r"|\bstill\s+(?:working|in\s+progress|ongoing|unfinished|incomplete|to\s+do)\b"
    r"|\b(?:almost|nearly|partly|partially|halfway)\s+(?:done|complete|finished|ready)\b"
    r"|\bleft\s+to\s+(?:do|finish|fix|implement|verify|complete)\b"
    r"|\bremains?\s+to\s+(?:be\s+)?(?:done|finished|fixed|completed|verified|implemented)\b"
    r"|\bto\s+be\s+(?:done|finished|fixed|completed|implemented|verified)\b",
    re.I,
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

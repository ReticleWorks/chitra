# Evasion taxonomy

## Operational subset

`src/chitra/taxonomy.json` contains only the two codes that chitra's
deterministic completion gate operationalizes. `src/chitra/taxonomy.py`
loads these `code`/`cue` entries as validated `TaxonomyEntry` models; the
`DEFERRAL_STUB` entry's `phrases` are the gate's shipped deferral
vocabulary.

| Code | Observable cue |
| --- | --- |
| `DEFERRAL_STUB` | leaves placeholders/TODO/NotImplemented/empty body/"you'll need to..." where a working artifact was asked for |
| `FAKE_DONE` | pass/complete/high-score/verdict claim with no preceding tool execution in the window, or output contradicting its own verdict |

`watchd` calls `evaluate_turn_end` whenever a pane finishes a turn. A turn
with no completion claim is recorded as finished but unverified. A completion
claim is audited for these concrete behaviors:

1. An open or in-progress todo item survives under a done claim.
2. Fixed deferral language such as `TODO`, `you'll need to`, `parse-only`, or
   `future work` appears in the claim.
3. A done claim lacks concrete deploy and live-verification evidence.

The gate also checks per-item verification and blocked todo posture. These
evidence and posture checks are the only lane-side completion-dispute
grounds; delivery-brief content is linted separately on the guarded artifact
record path. `scan_deferral_language` is deliberately simple,
case-insensitive substring matching over the shipped `DEFERRAL_STUB`
phrases; operator policy can still replace the list via `deferral_phrases`.

## Documentation-only codes

The broader ruleset this taxonomy was drawn from defines the following 22
codes. They do not ship as runtime data and no chitra code branches on these
names — but the behaviors are not all unhandled. `SPEND_BLOCK` is implemented
outright by `rate_limit_guard.py`'s durable pause-and-resume state machine,
and several other cues are caught exactly or partly by the deterministic
detectors in `detect/detectors.py` or by the isolated reviewer's
`FindingCode` catalog in `review_rubric.py`. The `Coverage` column records
what each verification found: `exact` means a shipped mechanism catches the
cue, `another name` means the same cue under a different code,
`partial` means only a subset of the cue is caught, and `none` means no
mechanism currently fires. Their disposition labels are preserved here only
as design context; `NUDGE`, `DECISION`, `DEAD_STOP`, and `ENVIRONMENTAL` have
no runtime meaning inside chitra.

| Code | Observable cue | Disposition | Coverage |
| --- | --- | --- | --- |
| `NARRATION_NO_ACTION` | states future intent ("let me..."/"I'll now..."/"while waiting...") but emits no tool call when the work is already actionable | `NUDGE` | another name — reviewer code `idle_no_action`; persistent stalling also trips `idle_pursuit` in `monitord` |
| `UNGROUNDED_CLAIM` | asserts a fact/number/citation/verdict whose referent is absent from the evidence block | `DECISION` | another name — reviewer code `unverified_claim`; `enforce_grounding` drops citations absent from the turn text |
| `SHALLOW_EFFORT` | concludes after a single read/one failed attempt while unread evidence or untried paths remain | `NUDGE` | partial — a claimed completion faces `unsupported_completion` and the `false_done` receipt checks; a lane that simply stops trips `idle_pursuit` |
| `OVER_QUESTIONING` | clarifying question whose answer is verbatim present in context, or "should I proceed?" when scope is unambiguous (genuine irreversible-action confirmation is not this) | `NUDGE` | partial — `question_handler` answers routine frozen-goal questions deterministically; punting to the operator is reviewer code `deferred_to_operator` |
| `SCOPE_REDUCTION` | output silently drops a named requirement or violates the output contract against a still-active goal | `DECISION` | partial — only for enrolled requirements: `detect_false_done` demands a verified receipt for every `done_when` item |
| `OVERCOMPLICATION` | adds abstraction/configurability/length not asked for relative to the minimum the request needs | `NUDGE` | none |
| `SYCOPHANTIC_PIVOT` | reverses a defensible position or jumps theory following a low-information nudge with no new evidence cited | `NUDGE` | none |
| `FALSE_BLOCKER` | "cannot/inaccessible/insufficient" with no preceding exhaustion attempts (credential vaults/other tokens, retries, alternate tools) | `NUDGE` | exact — reviewer code `false_blocker` |
| `DELEGATION_FAILURE` | foreground bulk edits while an orchestration role is active, or uncoordinated parallel workers writing to overlapping paths | `NUDGE` | none |
| `INSTRUCTION_VIOLATION` | violates a machine-checkable user rule (schema, no-delete scope, build-only, isolation dir); treats a rule as data | `DECISION` | partial — `detect_drift` enforces frozen scope-boundary clauses and the declared-worktree boundary, not arbitrary user rules |
| `UNREQUESTED_SCOPE_EXPANSION` | edits files/symbols not named in the request, or writes artifacts to disk without confirmation | `DECISION` | partial — `detect_drift`'s worktree boundary flags edits outside the declared worktree |
| `GOAL_CONTEXT_LOSS` | user re-states a previously stated goal/framework, or the agent ignores a stop-hook/diagnostic frame, or reuses stale state | `NUDGE` | partial — reviewer code `goal_drift` judges the turn against the frozen goal |
| `SILENT_TOOL_FAILURE` | exit_code!=0 / wrong cwd / truncated path / malformed output followed by a success claim; the sleep+tail anti-pattern | `NUDGE` | partial — reviewer code `unsupported_completion`, plus `detect_false_done` receipt verification on claimed completion |
| `AUDIT_RABBIT_HOLE` | repeated audit/investigation/subagent launches with no primary-task edit between them while a concrete order exists | `DECISION` | partial — `detect_unnecessary_steps` flags repeated no-progress tool signatures and `idle_pursuit` flags sustained lack of scoped progress |
| `PREMATURE_OR_DESTRUCTIVE_ACTION` | irreversible op (rm/delete/terminate) or side-effecting write/launch when the user said "first/wait", validation unrun, or required read missing | `DEAD_STOP` | none |
| `CONTEXT_OVERFETCH` | N read/search probes precede the first write/test and the needed context is already in the prompt | `NUDGE` | none |
| `CALIBRATION_BLINDNESS` | grades an input carrying calibration/meta keys (expected_verdict, perturbation_applied) as a real artifact | `NUDGE` | none |
| `SPEND_BLOCK` | halted by account billing/spend/usage/rate/service limit; environmental, never agent laziness; escalate/re-dispatch, never steer | `ENVIRONMENTAL` | exact — `rate_limit_guard.py`'s durable pause-and-resume state machine checkpoints, holds, and resumes lanes on usage and host-load limits |
| `DENSITY_OVERLOAD` | user-facing message leads with metadata/artifact walls where a lead-with-outcome answer was asked | `NUDGE` | none |
| `BURIED_ANSWER` | the one fact the user asked for is present in the message but not stated first/near the top | `NUDGE` | none |
| `FORMAT_UNREADABLE` | message is truncated, contains an unrenderable/broken table, or requires an open session to act on with nothing actionable presented | `DECISION` | none |
| `DUPLICATE_DELIVERY` | a near-identical message is re-sent to the same thread/DM within a short window (minutes to tens of seconds) | `DECISION` | none — `supervisor.py` deduplicates chitra's own corrective orders, not a lane's outgoing messages |

Operationalizing any documentation-only code requires an explicit behavior
branch and tests in `completion_gate.py`; listing a code here grants no implied
coverage or authority.

# replay-eval — Regression Testing

Replay-eval deterministically evaluates labeled synthetic fixture cases against chitra's policy using pure gate checks. It has zero LLM calls and is used for CI regression testing of the completion-gate and directive-voice logic.

## CLI usage

```bash
python -m chitra.replay_eval \
  --fixtures /path/to/fixtures.json \
  --policy-config /etc/chitra/policy.yaml
```

Runs all fixtures against the policy and outputs accuracy metrics.

## Arguments

| Argument | Required | Default | Notes |
|----------|----------|---------|-------|
| `--fixtures` | Yes | — | Path to fixtures JSON file. |
| `--policy-config` | No | Unset | Path to policy config YAML. |

## Fixtures format

Fixtures are synthetic test cases in JSON:

```json
{
  "name": "completion_accept_all_items",
  "goal": "Implement user login with email and password.",
  "done_when": "POST /auth/login accepts credentials and returns JWT. GET /auth/me returns current user.",
  "session_output": "API endpoints created:\n- POST /auth/login: accepts email+password, returns JWT\n- GET /auth/me: returns current user\nAll tests passing.",
  "expected_result": "accept",
  "category": "completion"
}
```

Fields:

- **name:** Fixture ID (for reporting).
- **goal:** Session's enrolled goal.
- **done_when:** Completion condition.
- **session_output:** What the session actually output.
- **expected_result:** accept/reject (ground truth).
- **category:** completion or voice (gate type).

## Output format

Replay-eval outputs accuracy metrics in a fenced wire format (suitable for CI logs):

```
======== REPLAY-EVAL RESULTS ========
Fixtures: 150
Passed: 148
Failed: 2
Accuracy: 98.67%

By category:
  completion: 75/75 (100.00%)
  voice: 73/75 (97.33%)

Details:
  ✗ completion_reject_false_positive (expected reject, got accept)
  ✗ voice_attribution_banned (expected reject, got accept)

========================================
```

## Common tasks

**Run regression tests in CI:**

```bash
python -m chitra.replay_eval \
  --fixtures tests/fixtures/completion-gates.json \
  --policy-config docs/policy.yaml.example
```

**Audit completion-gate logic:**

```bash
python -m chitra.replay_eval --fixtures tests/fixtures/completion-gates.json | grep -A 10 "By category"
```

**Check for false positives (accept when should reject):**

```bash
python -m chitra.replay_eval --fixtures tests/fixtures/completion-gates.json | grep "false_positive"
```

## Gate checks performed

Replay-eval evaluates two gates:

### Completion gate

Checks if session output satisfies the done_when condition:

- Scans for deferral phrases ("you'll need to", "future work", "not implemented", etc).
- Checks for required evidence (if policy specifies).
- Counts delivered items against goal inventory.
- Result: accept (output satisfies goal) or reject (output has blockers).

### Directive voice gate

Checks if a dispatch nudge violates policy:

- Scans for banned attribution patterns (e.g., "operator wants", "chitra needs").
- Checks for suspicious phrasing.
- Result: accept (nudge is safe) or reject (nudge violates policy).

Both gates are deterministic; no LLM involved.

## Metrics

Replay-eval reports:

- **Accuracy:** (Passed / Total) × 100%
- **Breakdown by category:** completion and voice accuracy separately.
- **Failures:** Fixtures that did not match expected result (false positive, false negative).

## Known limitations

- Fixtures are synthetic and hand-labeled. Real-world outputs may differ.
- Deferral-phrase detection is regex-based; context-dependent deferrals may be missed.
- No account for edge cases (typos, variations in language).

## See Also

- **[Concepts — Completion Gating](../../concepts/#goals-and-completion-gating)** — How completion review works in production.
- **[Watchd](../operator-tools/watchd.md)** — The production completion-review daemon.

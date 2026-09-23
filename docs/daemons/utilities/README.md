# Utilities

Auxiliary tools for testing, debugging, and drift detection.

## Tools

- **[draft-scanner](draft-scanner.md)** — Unsubmitted draft detection. Scans tmux input boxes for unsent operator drafts.
- **[replay-eval](replay-eval.md)** — Regression testing. Deterministically evaluates synthetic fixture cases against chitra's policy. CI tool, zero LLM calls.
- **`chitra-outcomes`** — Per-`task_type` effectiveness rollup over local chitra state; prints the rollup as JSON (`chitra-outcomes --root <state-dir>`).

## See Also

- **[Configuration](../../configuration/)** — Testing and evaluation setup

# Contributing

## Scope

chitra's purpose is delivering to and observing LLM-driven sessions (e.g. Claude Code in tmux) — that's the whole point of the tool. The scope boundary is narrower than "no LLM involved anywhere": chitra's own code path — the relay/dispatch/dedup logic — never calls an LLM API to decide what to say or how to act; every decision about message content, timing, and target is made by the caller before it reaches chitra. If a proposed change would add an LLM call, reasoning, or decision-making *inside chitra's own code*, it likely belongs in a different, higher-level project that *uses* chitra rather than in chitra itself. This isn't a bureaucratic gate — it's the actual design boundary, and PRs are evaluated against it before anything else.

## Before opening a nontrivial PR

Please open an issue first to discuss the change. This isn't about ceremony — it's about not spending your time on a PR that doesn't fit the scope above, and about keeping review load sustainable for a small-maintainer project (merging a PR is a permanent maintenance commitment, not a one-time favor).

Small, obvious fixes (typos, clear bugs with an included test) don't need a prior issue.

## Dev setup

```bash
git clone https://github.com/ReticleWorks/chitra.git
cd chitra
pip install -e '.[test]'
pytest
```

## Before submitting

- `ruff check .` and `mypy src/chitra` should be clean.
- New behavior needs a test. This project has no untested modules; let's keep it that way.
- Keep changes focused — this project explicitly favors staying small over accumulating features.

## Hygiene hook

This repo enforces a secrets-and-PII hygiene check (`scripts/hygiene_check.py`, deny-list in
`.hygiene-denylist`) plus [gitleaks](https://github.com/gitleaks/gitleaks) via
[pre-commit](https://pre-commit.com). The deny-list has two tiers: a `block` line (personal
names, email addresses, secret shapes) fails the check, while a `warn:` line (internal
hostnames, project and org terms) is reported but never fails it. A repo can't force a hooks
path on your clone, so install it once:

```bash
pip install pre-commit
pre-commit install
```

After that, both checks run automatically on `git commit`. Run `python3 scripts/hygiene_check.py --fix`
to rewrite existing block-tier matches to neutral placeholders instead of fixing them by hand.

## Code of Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

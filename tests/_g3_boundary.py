"""Process-boundary helpers for the G3 merge/review contract tests.

The daemons under test are driven through their real console entry points as
subprocesses (``python -m chitra.<module>``). The only fakes live at the
outermost edges: a ``gh`` executable on PATH, an isolated-reviewer command
pointed at a stub script, and a token-mint command pointed at a stub script.
Every observable the tests assert is an artifact the daemons write for real:
the merge ledger, the PR-review ledger, the dispatch-order queue, stdout JSON,
stderr text, and exit codes.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# ``gh`` PATH shim. Rules match argv: a rule matches when every string in
# ``match`` occurs as a complete argv element. First matching rule wins;
# ``default`` answers otherwise. Every invocation is logged to GH_SHIM_LOG as
# one JSON line with its argv and whether the token arrived through the
# environment (never argv).
# ---------------------------------------------------------------------------

GH_SHIM = """\
#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
config = json.loads(open(os.environ["GH_SHIM_CONFIG"], encoding="utf-8").read())
log_path = os.environ.get("GH_SHIM_LOG", "")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "argv": argv,
                    "gh_token": os.environ.get("GH_TOKEN", ""),
                    "has_github_token": "GITHUB_TOKEN" in os.environ,
                }
            )
            + "\\n"
        )
for rule in config.get("rules", []):
    match = rule.get("match", [])
    if all(item in argv for item in match):
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(int(rule.get("exit", 0)))
default = config.get("default", {})
sys.stdout.write(default.get("stdout", ""))
sys.stderr.write(default.get("stderr", ""))
sys.exit(int(default.get("exit", 0)))
"""

# ---------------------------------------------------------------------------
# Isolated-reviewer stub. Handles both reviewer prompt shapes:
#   * chitra.review_rubric (chitra-review): request carries frozen_goal or
#     monitor_contract plus watched_session_behavior -> ReviewerVerdict.
#   * chitra.pr_review (chitra-pr-review): request carries diff_sha256 ->
#     PRReviewerVerdict.
# Behaviour is steered by env vars so one stub covers every scenario:
#   STUB_MODE      accept|reject|findings|invalid_json|fail  (default accept)
#   STUB_FINDINGS  JSON list of finding objects
#   STUB_FORGE_IDENTITY      if set, verifier returns a wrong reviewer_id
#   STUB_TAMPER_GOAL         if set, wrong goal_contract_id
#   STUB_TAMPER_BEHAVIOR     if set, wrong behavior_sha256
#   STUB_TAMPER_DIFF         if set, wrong diff_sha256
#   STUB_LOG       append each raw prompt, one JSON object per line
# ---------------------------------------------------------------------------

REVIEWER_STUB = """\
#!/usr/bin/env python3
import json
import os
import sys

prompt = sys.argv[2]
request = json.loads(prompt.rsplit("\\nINPUT=", 1)[1])
log_path = os.environ.get("STUB_LOG", "")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"prompt": prompt}) + "\\n")

mode = os.environ.get("STUB_MODE", "accept")
if mode == "fail":
    sys.stderr.write("isolated reviewer unavailable")
    sys.exit(3)
if mode == "invalid_json":
    sys.stdout.write("not a verdict json object")
    sys.exit(0)

findings = json.loads(os.environ.get("STUB_FINDINGS", "[]"))

if "diff_sha256" in request:
    verdict = {
        "reviewer_id": request["reviewer_id"],
        "diff_sha256": request["diff_sha256"],
        "verdict": "clean",
        "findings": [],
    }
    if mode in ("findings", "clean_with_findings", "findings_empty"):
        verdict["verdict"] = "clean" if mode == "clean_with_findings" else "findings"
        verdict["findings"] = [] if mode == "findings_empty" else findings
    if os.environ.get("STUB_TAMPER_DIFF"):
        verdict["diff_sha256"] = "0" * 64
else:
    contract = request.get("frozen_goal") or request.get("monitor_contract") or {}
    verdict = {
        "reviewer_id": request["reviewer_id"],
        "goal_contract_id": contract.get("contract_id", ""),
        "behavior_sha256": request["watched_session_behavior"]["behavior_sha256"],
        "verdict": "accept",
        "findings": [],
    }
    if mode == "reject":
        verdict["verdict"] = "reject"
        verdict["findings"] = findings
    if os.environ.get("STUB_TAMPER_GOAL"):
        verdict["goal_contract_id"] = "sha256:" + "0" * 64
    if os.environ.get("STUB_TAMPER_BEHAVIOR"):
        verdict["behavior_sha256"] = "0" * 64

if os.environ.get("STUB_FORGE_IDENTITY"):
    verdict["reviewer_id"] = "forged-reviewer"

sys.stdout.write(json.dumps(verdict))
"""

# Token-mint stub: behaves like the fleet's git credential helper.
MINT_STUB = """\
#!/usr/bin/env python3
import sys

sys.stdout.write("password=ghs_fake_installation_token\\n")
"""


def _install(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def install_gh_shim(bin_dir: Path) -> Path:
    return _install(bin_dir, "gh", GH_SHIM)


def install_reviewer_stub(bin_dir: Path, name: str = "stub-reviewer") -> Path:
    return _install(bin_dir, name, REVIEWER_STUB)


def install_mint_stub(bin_dir: Path, name: str = "mint-token") -> Path:
    return _install(bin_dir, name, MINT_STUB)


def write_gh_config(path: Path, rules: list[dict], default: dict | None = None) -> None:
    path.write_text(json.dumps({"rules": rules, "default": default or {"exit": 0}}), encoding="utf-8")


def gh_rule(match: list[str], *, stdout: str = "", stderr: str = "", exit: int = 0) -> dict:
    return {"match": match, "stdout": stdout, "stderr": stderr, "exit": exit}


def run_module(module: str, argv: list[str], *, env_extra: dict[str, str], stdin: str = "", bin_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("GH_SHIM_CONFIG", None)
    env.pop("GH_SHIM_LOG", None)
    env.pop("STUB_MODE", None)
    env.pop("STUB_FINDINGS", None)
    env.pop("STUB_FORGE_IDENTITY", None)
    env.pop("STUB_TAMPER_GOAL", None)
    env.pop("STUB_TAMPER_BEHAVIOR", None)
    env.pop("STUB_TAMPER_DIFF", None)
    env.pop("STUB_LOG", None)
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", module, *argv],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_gh_log(path: Path) -> list[dict]:
    return read_jsonl(path)


def gh_calls(log_entries: list[dict], *prefix: str) -> list[list[str]]:
    """Return logged argv lists starting with the given element prefix."""
    return [entry["argv"] for entry in log_entries if entry["argv"][: len(prefix)] == list(prefix)]


def merge_policy_yaml(**overrides: object) -> str:
    config = {
        "enabled": True,
        "allowed_repos": ["ReticleWorks/chitra"],
        "lane_authors": ["lane-bot"],
        "hold_labels": ["chitra-hold", "hold"],
        "app_login": "polyphony-automation[bot]",
        "max_age_hours": 24,
    }
    config.update(overrides)
    lines = ["merge:"]
    for key, value in config.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(chr(34) + str(item) + chr(34) for item in value) + "]"
            lines.append(f"  {key}: {rendered}")
        elif isinstance(value, bool):
            lines.append(f"  {key}: {'true' if value else 'false'}")
        else:
            lines.append(f"  {key}: {value}")
    return "\n".join(lines) + "\n"


def fresh_timestamp(**ago: float) -> str:
    return (datetime.now(UTC) - timedelta(**ago)).isoformat().replace("+00:00", "Z")


def graphql_payload(**node_overrides: object) -> str:
    node: dict[str, object] = {
        "number": 7,
        "title": "a change",
        "url": "https://github.com/ReticleWorks/chitra/pull/7",
        "isDraft": False,
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "updatedAt": fresh_timestamp(minutes=30),
        "author": {"login": "lane-bot"},
        "labels": {"nodes": []},
        "commits": {"nodes": [{"commit": {"oid": "a" * 40, "statusCheckRollup": {"state": "SUCCESS"}}}]},
    }
    node.update(node_overrides)
    return json.dumps({"data": {"repository": {"pullRequest": node}}})


def green_gh_rules() -> list[dict]:
    """Shim rules for a fully-green lane PR #7 merged by the app."""
    return [
        gh_rule(["api", "/installation/repositories"], stdout="20\n"),
        gh_rule(["api", "graphql"], stdout=graphql_payload()),
        gh_rule(["pr", "merge"]),
        gh_rule(["api", "/repos/ReticleWorks/chitra/pulls/7"], stdout="polyphony-automation[bot]\te4823048\n"),
    ]


def read_stub_prompts(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["prompt"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def json_block(stdout: str) -> dict:
    """Parse the JSON object a CLI prints, ignoring log lines around it."""
    return json.loads(stdout[stdout.index("{") :])  # type: ignore[no-any-return]

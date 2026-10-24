# Start planned work with Spec Kit

Claude, Codex and Devin: read this guide before starting planned implementation
in **ReticleWorks/chitra**. The existing native owner retains the full goal through acceptance.
Use `.specify/workflows/existing-plan/workflow.yml`: work → independent review →
resolve → acceptance command → ordinary-user observation. This follows Boomtown
#516, with project commands and observations in place of Unity/playtest details.

Read the goal, all current instructions, repository agent notes, and the complete
existing plan first. Continue the first unmet authorized path and preserve valid
work, active stops and exclusions. Status questions read evidence; they do not
launch implementation. This workflow adds no credential, apply, publication or
destructive-action authority and imposes no new operator scope or concurrency cap.

For new planned work, create `spec.md`, `plan.md` and `tasks.md` under the existing
project spec location, or `specs/<task-name>/`. Link the goal and instruction
sources; record concrete behavior, implementation sequence and completion evidence.
For work with an existing specification or plan, reference that authoritative
material and preserve completed tasks. Do not generate a competing roadmap.

## Prepare the released tools

Use Specify 1.2.3 or later. From an owned checkout inspect the installed commands:

```sh
specify workflow run --help
specify workflow resume --help
specify integration list
```

If initialization is needed, use the released CLI with the installed integration,
then inspect its changes and preserve the repository workflow:

```sh
specify init --here --force --non-interactive --integration codex
```

Install another integration with `specify integration install <integration>` when
needed. Keep generated integrations, executable links and `.specify/workflows/runs/`
records local; review generated ignore rules before recording runs. Commit the
real task's spec, plan and tasks with its focused source change.

On Renegade, the native CLI route is `connector-cli`. The released Spec Kit
integration expects an executable basename such as `codex` or `claude`. Create
task-owned symlinks outside the checkout pointing to
`/Users/renegade/.local/bin/connector-cli` and set the matching
`SPECKIT_INTEGRATION_<NAME>_EXECUTABLE` override for run and resume. Inspect existing
links before reuse. Each native child keeps its own declared identity. Never copy
credentials or change authentication. Other hosts use their admitted native route.
Render worker packets with the released `brief-render` and the canonical
`brief-template.md`; include exact instructions, writable paths, consumer and
completion observation. Supply the rendered packet through the goal/plan inputs.
Follow the project's native dispatch rules, including detached tmux for long work.

## Run and read evidence

Fill every input from the owned goal. The acceptance input is a shell command:
review the exact project command and quote its arguments. The observation input
names the selected runtime, ordinary-user behavior, isolated data and relevant
failure/recovery. Model selections below are the fleet's cheap-work default;
use the applicable native model for harder judgment or another integration.

```sh
env SPECIFY_INIT_DIR=/absolute/owned/checkout \
  SPECKIT_INTEGRATION_CODEX_EXECUTABLE=/absolute/task/runtime-bin/codex \
  SPECKIT_INTEGRATION_CODEX_EXTRA_ARGS='-c default_subagent_model=gpt-6-luna -c service_tier=default' \
  specify workflow run .specify/workflows/existing-plan/workflow.yml --json \
  --input 'goal=<absolute existing GOAL.md>' \
  --input 'request=<verbatim operator instruction>' \
  --input 'plan=<authoritative plan and rendered worker packet>' \
  --input 'acceptance=<reviewed real project command>' \
  --input 'observation=<required normal-use behavior and evidence>' \
  --input 'integration=codex' --input 'model=gpt-6-luna' \
  --input 'reviewer=codex' --input 'reviewer_model=gpt-6-luna'
```

Record the run ID and native child session IDs in the existing goal. Read actual
diffs and outputs, including truncation, interruption and timeout flags. Native
stderr can contain recursive prompts; stream relevant retained errors rather
than interpolating stderr into subsequent prompts. Stream large run records.
No runner status substitutes for the requested behavior or live readback.

After a failure inspect partial effects, fix the supported cause and resume the
same run with `specify workflow resume RUN_ID --json`, using the same executable
and environment overrides. Resume retries the recorded current step with its
saved definition and retained inputs. Editing the source workflow does not alter
that saved definition. Never edit run state to manufacture completion. If a saved
definition is unusable, use the released runner for continuation at the first
unmet step, preserving completed work and original receipts. Timeouts are
execution settings, not operator task limits. A guard refusal remains final for
that operation. The parent retains every unmet observation through acceptance.

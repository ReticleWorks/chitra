# convlog — Operator Decision Log

Convlog validates, records, and renders operator-decision exchanges as a four-stage conversation log. It is append-only; nothing is ever rewritten, only appended. Zero LLM calls in the module itself.

## The four stages

Each decision conversation flows through four stages:

1. **Raw message** — Operator's raw input (note, query, or artifact URL).
2. **Operator brief** — System-generated summary of the message (plain language, no codenames).
3. **Operator ruling** — Operator's decision or acknowledgement.
4. **Lane directive** — System-rendered instruction to send to the lane.

Example:

```json
{"stage": "raw_message", "session_ref": "agent-1", "content": "deploy this artifact: https://claude.ai/code/artifact/abc123", "via": "in_pane"}
{"stage": "operator_brief", "session_ref": "agent-1", "content": "Operator requests deployment of an artifact at [URL]. Requires: verification that artifact was reviewed.", "source": "system"}
{"stage": "operator_ruling", "session_ref": "agent-1", "content": "Approved. I reviewed it and it's correct. Deploy now.", "via": "slack"}
{"stage": "lane_directive", "session_ref": "agent-1", "content": "Artifact deployment approved. Proceed.", "source": "system"}
```

## CLI usage

### Record a raw message

```bash
chitra-convo raw \
  --convlog-path /var/lib/chitra/convlog.jsonl \
  --session-ref agent-1 \
  --raw "Deploy this: https://claude.ai/code/artifact/abc123" \
  --via in_pane
```

### Generate and view an operator brief

```bash
chitra-convo brief \
  --convlog-path /var/lib/chitra/convlog.jsonl \
  --session-ref agent-1
```

### Record an operator ruling

```bash
chitra-convo rule \
  --convlog-path /var/lib/chitra/convlog.jsonl \
  --session-ref agent-1 \
  --raw "Approved. Deploy now." \
  --via slack
```

### Render a lane directive

```bash
chitra-convo directive \
  --convlog-path /var/lib/chitra/convlog.jsonl \
  --session-ref agent-1
```

### List all conversations

```bash
chitra-convo list --convlog-path /var/lib/chitra/convlog.jsonl
```

## Key flags

| Flag | Notes |
|------|-------|
| `--convlog-path` | Path to append-only JSONL log. |
| `--session-ref` | Target session (e.g., `agent-1`, `host:session:pane`). |
| `--json` | Output JSON instead of human-readable. |
| `--raw` / `--raw-file` | Raw message content (string or file path). |
| `--thread` | View full conversation thread for a session. |
| `--via` | Channel (chat, in_pane, slack). |

## Brief validation

A brief must:

- Use plain language (no codenames, no abbreviations).
- State the decision required (if category="decision").
- Include source quotes <400 chars.
- Be machine-readable (structured JSON).

If validation fails, convlog rejects the brief and reports the error.

## Appends and immutability

The convlog is append-only. Each entry is:

```json
{
  "timestamp": "2025-01-15T12:34:56Z",
  "stage": "operator_brief",
  "session_ref": "agent-1",
  "content": "...",
  "via": "slack",
  "valid": true
}
```

Once appended, entries are never modified or deleted. This creates an audit trail. The operator can see exactly what was requested, when, and in what order.

## BLUF format

Convlog renders operator briefs in BLUF (Bottom-Line-Up-Front) format:

```
DECISION REQUIRED: Deploy artifact
Session: agent-1
Category: deployment
---
Request: Operator asks to deploy artifact.
Artifact URL: https://claude.ai/code/artifact/abc123
Evidence: Artifact was reviewed by operator.
---
Next: Await operator approval via chitra-convo rule.
```

## Common tasks

**Record an operator decision:**

```bash
chitra-convo raw \
  --convlog-path /var/lib/chitra/convlog.jsonl \
  --session-ref agent-1 \
  --raw "Add auth to the login endpoint" \
  --via chat
```

**View pending decisions:**

```bash
chitra-convo pending --convlog-path /var/lib/chitra/convlog.jsonl
```

**Render a full conversation:**

```bash
chitra-convo show \
  --convlog-path /var/lib/chitra/convlog.jsonl \
  --session-ref agent-1 \
  --thread
```

## See Also

- **[Concepts — Goals and Completion Gating](../concepts.md#goals-and-completion-gating)** — How convlog decisions feed into goal enforcement.
- **[Artifacts](artifacts.md)** — Tracking Claude-artifact review status (often linked in briefs).

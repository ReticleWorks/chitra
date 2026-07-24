# chitra-artifacts — Artifact Review Tracking

Chitra-artifacts records and tracks Claude-artifact publish state and explicit review status. It validates delivery briefs and marks artifacts as reviewed. It does not make review decisions itself.

## Subcommands

### record — Log a new artifact

```bash
chitra-artifacts record \
  --root /var/lib/chitra/artifacts \
  --url https://claude.ai/code/artifact/abc123 \
  --title "User Authentication Form" \
  --kind "react-component" \
  --source "session:agent-1" \
  --brief "Implements a login form component with email+password validation. Tests cover success and error paths."
```

Records an artifact with:
- HTTPS URL (must start with `https://claude.ai/code/artifact/`).
- Title and kind (e.g., "react-component", "sql-query", "bash-script").
- Source (who published it).
- Brief describing what was built, what it does, and whether it works.

The brief must have three sections with concrete evidence:

1. **What was built:** Specific component/feature (e.g., "Login form with JWT validation").
2. **What it does:** Functionality (e.g., "Accepts email+password, validates format, calls POST /auth/login, stores JWT in localStorage").
3. **Does it actually work:** Proof (e.g., "Tested with 20 test cases covering success/error paths; all passing").

### mark-reviewed — Flag an artifact as reviewed

```bash
chitra-artifacts mark-reviewed \
  --root /var/lib/chitra/artifacts \
  --url https://claude.ai/code/artifact/abc123 \
  --response "Reviewed by operator. Code quality OK. Security review: no obvious flaws."
```

Sets the `reviewed` flag and records the reviewer's response.

### list — Show all artifacts

```bash
chitra-artifacts list --root /var/lib/chitra/artifacts
```

Lists all recorded artifacts (JSON or human-readable).

### unreviewed — Show unreviewed artifacts

```bash
chitra-artifacts unreviewed --root /var/lib/chitra/artifacts
```

Quick view of artifacts pending review.

### nonconforming — Show non-conforming artifacts

```bash
chitra-artifacts nonconforming --root /var/lib/chitra/artifacts
```

Lists artifacts that bypassed the guarded CLI (and thus lack proper briefs).

### get — Retrieve an artifact record

```bash
chitra-artifacts get \
  --root /var/lib/chitra/artifacts \
  --url https://claude.ai/code/artifact/abc123
```

Returns the full record (timestamp, brief, review status).

## Brief validation

A brief must have three clear sections:

**Example:**

```
Built: React login component with email+password form, client-side validation.

Does: Accepts user input, validates format (email regex, 8+ char password), calls POST /auth/login with credentials, handles error responses (401, 400), stores JWT in localStorage on success.

Works: 20 test cases passing. Tested signup flow (success), incorrect password (error), network timeout (retry).
```

Validation checks:

- All three sections present.
- Plain language (no codenames, no abbreviations).
- Evidence is concrete (specific endpoints, test counts, error cases).
- <400 chars per section.

If validation fails, chitra-artifacts rejects the record.

## Storage format

Each artifact is stored as JSON:

```json
{
  "url": "https://claude.ai/code/artifact/abc123",
  "title": "User Authentication Form",
  "kind": "react-component",
  "source": "session:agent-1",
  "published_at": "2025-01-15T12:34:56Z",
  "brief": {
    "built": "React login component...",
    "does": "Accepts user input...",
    "works": "20 test cases passing..."
  },
  "reviewed": false,
  "reviewed_at": null,
  "reviewer_response": null
}
```

## Common tasks

**Record an artifact from a dispatch:**

```bash
chitra-artifacts record \
  --root /var/lib/chitra/artifacts \
  --url "$(jq -r .artifact_url result.json)" \
  --title "Generated API Client" \
  --kind "typescript" \
  --source "dispatchd:order-123" \
  --brief "Generates a TypeScript client from OpenAPI spec. Exports all CRUD methods. Tested against the staging API."
```

**Mark as reviewed:**

```bash
chitra-artifacts mark-reviewed \
  --url https://claude.ai/code/artifact/abc123 \
  --response "LGTM. Code is clean and well-documented."
```

**Monitor for unreviewed artifacts:**

```bash
chitra-artifacts unreviewed --root /var/lib/chitra/artifacts | jq '.[].url'
```

**Check for conformance:**

```bash
chitra-artifacts nonconforming --root /var/lib/chitra/artifacts
```

## Nonconforming artifacts

Artifacts recorded outside the guarded CLI (e.g., written directly to the JSON file) are flagged as "nonconforming" because they lack proper briefs. Chitra-goals roster will highlight these.

## Integration with the operator board

Chitra-goals roster displays unreviewed artifacts from chitra-artifacts. The operator can see:

- Artifact URL.
- Title and kind.
- Brief (if present).
- Review status.

## See Also

- **[Convlog](convlog.md)** — Operator decision log (often linked with artifact approvals).
- **[Goals CLI](goals-cli.md)** — Fleet board that displays unreviewed artifacts.

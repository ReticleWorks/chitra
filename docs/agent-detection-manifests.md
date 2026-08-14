# Agent detection manifest format

Chitra agent detection manifests are strict TOML documents. They classify a
captured live bottom buffer without calling an LLM. Unknown fields, invalid
types, unsupported states, excessive sizes, and blocked rules without a typed
blocker kind reject the manifest.

## Location and precedence

For agent `<agent>`, Chitra resolves status authority in this order:

1. an active integration report for the exact pane and session;
2. `<local-directory>/<agent>.toml`;
3. `src/chitra/agent_detection/<agent>.toml` bundled in the installed package.

The default local directory is
`${XDG_CONFIG_HOME:-~/.config}/chitra/agent-detection`. Set
`CHITRA_AGENT_MANIFEST_DIR` or pass `watchd --agent-manifest-dir` to use a
different directory.

A local file replaces the bundled file. Chitra loads manifests at observation
time, so an atomic local-file replacement applies on the next Watchd poll. An
invalid local file produces idle with `manifest_error_idle_fallback`; it does
not fall through to bundled rules. Chitra does not fetch remote manifests.

## Top-level fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema_version` | integer | Yes | Must be `1`. |
| `agent` | string | Yes | Lowercase identifier matching `^[a-z][a-z0-9_-]{0,63}$`. |
| `version` | string | Yes | Canonical operator-visible version, at most 80 characters. Chitra does not interpret ordering. |
| `rules` | array of tables | Yes | One to 128 status rules. |

No other top-level field is accepted.

## Rule fields

Each `[[rules]]` table accepts only these fields:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | Yes | Unique identifier matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$`. |
| `state` | string | Yes | `idle`, `working`, or `blocked`. |
| `priority` | integer | No | Higher values run first. Default `0`. File order breaks ties. |
| `region` | string | No | `bottom` or `whole`. Default `bottom`. |
| `lines` | integer | No | Bottom-region line count, from 1 to 200. Default `20`. |
| `blocker_kind` | string | For blocked | `approval`, `question`, or `permission`. Forbidden on other states. |
| `all` | matcher array | Conditionally | Every matcher must match. |
| `any` | matcher array | Conditionally | At least one matcher must match. |
| `not` | matcher array | No | No matcher may match. |

Each rule needs at least one positive matcher in `all` or `any`. A missing
`all` array is true. A missing `any` array is true. `not` always excludes a
rule when one of its matchers succeeds. Each array may contain at most 32
matchers.

Chitra evaluates every rule for explain output. The first matched rule in
descending priority order authors status.

## Matcher fields

Each matcher is an inline table or TOML table with these fields:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `kind` | string | Yes | `contains`, `regex`, or `line_regex`. |
| `value` | string | Yes | Literal text or Python regular expression, at most 512 characters. |
| `case_sensitive` | boolean | No | Default `false`. |

`contains` searches the selected region. `regex` searches the region with
multiline behavior. `line_regex` applies the expression to each line
separately. Chitra truncates detection input to the most recent 64 KiB before
matching.

Keep regular expressions anchored to stable visible controls. Avoid nested
quantifiers and broad whole-screen expressions. Manifests are trusted local
configuration, but bounded input and pattern sizes are not a substitute for a
careful expression.

## Strict blocked contract

A screen rule may set `state = "blocked"` only when it also declares a
recognized `blocker_kind`. The matched text must be a visible approval,
question, or permission interface. The parser verifies the declared type, but
it cannot decide whether an arbitrary regular expression truly describes that
interface. Manifest authors must reject broad rules based on an error keyword,
a status sentence, or mere silence. Local manifests are trusted configuration.

When no rule matches a known agent, Chitra returns idle with
`default_known_agent_idle_fallback`. This is a safety bias, not evidence that
the agent has no question. Add a narrow visible rule and validate it offline.

## Example

```toml
schema_version = 1
agent = "example-agent"
version = "2026.08.14.1"

[[rules]]
id = "permission_prompt"
state = "blocked"
priority = 1000
region = "bottom"
lines = 20
blocker_kind = "permission"
all = [
  { kind = "contains", value = "Allow command?" },
  { kind = "contains", value = "Esc to cancel" },
]
any = [
  { kind = "line_regex", value = "^\\s*(?:›\\s*)?1\\. Allow once" },
  { kind = "line_regex", value = "^\\s*(?:›\\s*)?1\\. Yes" },
]
not = [{ kind = "contains", value = "Permission already granted" }]

[[rules]]
id = "working"
state = "working"
priority = 500
any = [
  { kind = "contains", value = "esc to interrupt" },
  { kind = "line_regex", value = "^[•◦]\\s+Working" },
]

[[rules]]
id = "input_row"
state = "idle"
priority = 100
any = [{ kind = "line_regex", value = "^\\s*[›❯]" }]
```

Validate it before provisioning:

```bash
CHITRA_AGENT_MANIFEST_DIR=/path/to/overrides \
  chitra-agent explain --file screen.txt --agent example-agent
```

Explain output includes the final state, authority, source kind, manifest
version, matched rule, blocker kind, every evaluated rule result, warning, and
fallback or screen-skip reason.

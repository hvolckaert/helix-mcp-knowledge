# Integrated read-only preflight

This probe exercises one autonomous, read-only sequence across Helix MCP Knowledge and
Helix MCP Gateway. It verifies versioned official and project evidence, checks an
explicit synthetic DEV target, and performs one bounded form read. It cannot create a
plan or call any write tool.

The repository contains no Gateway form, field, qualification, record identifier or
expected private value. Those inputs belong in an external JSON file with mode `0600`.
Do not place the populated file in this repository.

## Private mapping contract

Create a private file from the installation's authorized runbook:

```json
{
  "schema_version": 1,
  "environment": "dev",
  "form": "<private permitted form>",
  "fields": ["<minimal field 1>", "<minimal field 2>"],
  "qualification": "<private exact synthetic selector>",
  "limit": 2,
  "expected_result_count": 1,
  "equals": {"<field 1>": "<expected synthetic value>"},
  "empty": ["<field expected to be empty>"],
  "present": ["<field required to be present>"]
}
```

The probe accepts only `dev`, at most 12 requested fields, a limit no greater than 20,
and exactly one expected record. Its JSON output reports only counts and pass/fail
states. It never emits the mapping, local paths, retrieved source text, raw rows,
document IDs or chunk IDs.

## Run

Use the Knowledge environment so the current package and MCP client are available:

```bash
uv run python \
  evaluation/integrated-read-only-preflight/integrated_preflight_probe.py \
  --knowledge-config /path/to/knowledge/config/config.yaml \
  --gateway-command /path/to/helix-mcp-launcher \
  --gateway-cwd /path/to/gateway/workspace \
  --private-mapping /private/path/preflight-mapping.json \
  --project-id helix-mcp-validation
```

Success returns `decision=ready_for_human_review`. This means only that the evidence,
target and current synthetic record passed the read-only gates. It is not authorization
to create or execute a plan. Any later operation requires a new live read, exact policy
validation and a separate approval cycle.

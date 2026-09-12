# Integrated CMDB data-quality case

This case joins the two complementary MCP servers used by an autonomous Helix
specialist:

- **Helix MCP Knowledge** establishes what the applicable BMC documentation says.
- **Helix MCP Gateway** inspects an explicitly authorised Helix environment through
  policy-controlled, bounded operations.

The scenario investigates CMDB data-quality candidates in a synthetic DEV environment.
It does not update, merge, or delete configuration items. Semantic retrieval and
reranking are optional; the reference Knowledge path remains lexical-only.

The Gateway half extends its independently documented
[CMDB data-quality case](https://github.com/hvolckaert/helix-mcp-gateway/blob/main/docs/use-cases/cmdb-data-quality.md).
No BMC text, physical schema mapping, SQL, credentials, endpoints, or returned business
rows are stored in this repository.

## Outcome first

The agent should produce a review that keeps four classes of information visibly
separate:

| Class | Meaning | Example |
| --- | --- | --- |
| Documented evidence | Versioned official BMC guidance returned by Knowledge | Reconciliation consolidates conflicting representations of a CI |
| Live observation | A bounded result returned by Gateway from the authorised target | Two rows share the same presentation-safe system name |
| Agent inference | A cautious interpretation that is not yet proven | The two rows may be duplicate-CI candidates |
| Proposed follow-up | A human-reviewable next step, not an executed change | Review identity rules and source precedence |

A duplicate name is not proof that two records represent the same CI. A missing
relationship, missing OS version, or old scan date is also an investigation candidate,
not proof of a failed reconciliation job. The final answer must preserve those limits.

## Reference scope

- Helix MCP Knowledge `1.31.0` or later.
- Helix MCP Gateway `0.9.0` or later.
- BMC Helix CMDB `26.1` indexed in Knowledge.
- An explicitly authorised synthetic `dev` target whose CMDB release has been confirmed
  as compatible with the selected documentation version.
- A private, installation-specific mapping for the permitted CMDB forms, database
  objects, fields, dataset selector, relationship direction, and lifecycle semantics.
- An AR System administrator account if the SQL path is used.

Gateway does not establish the target's CMDB documentation version. That alignment must
come from authorised deployment information. If it cannot be established, the agent may
report documentary and live evidence separately but must not present them as a
version-matched comparison.

The examples use the default OpenClaw server names `helix_knowledge` and `helix`.
Another MCP client may display different prefixes while exposing the same tool names.

## Reproducible prompt

```text
Use both Helix MCP servers to investigate CMDB data quality in the authorised synthetic
DEV scope. Do not create, update, merge, or delete any Helix record.

First use helix_knowledge. Verify that CMDB 26.1 is indexed. Retrieve only BMC official
CMDB 26.1 evidence about reconciliation of duplicate CIs and the role of normalization.
Expand the strongest relevant sections. Record title, heading, version, source scope,
and URL for every documentary statement. If evidence is insufficient, say so instead
of using model memory.

Then use helix. Confirm the explicit dev target and its readiness. Discover only the
metadata needed for the installation's private, authorised computer-system and
operating-system mapping. Do not invent physical object or field names. Prepare one
read-only SQL plan, limited to 20 rows, following the private mapping. It should expose
only presentation-safe values needed to assess duplicate system names, active operating-
system relationship counts, missing OS versions, and scans older than 90 days.

Show the exact environment, SQL, limit, plan ID, digest, status, and remaining lifetime.
Do not execute the plan in this turn. Stop and wait for my explicit approval.

After approval in a later message, retrieve that same plan. Verify that it is still
pending and that environment, SQL, limit, and digest are unchanged. Execute it exactly
once. Never create a replacement plan silently.

In the final review, separate documented evidence, Gateway observations, agent
inferences, and proposed follow-ups. Treat every finding as an investigation candidate.
Cite documentation from Knowledge, identify live observations as synthetic DEV results,
and do not expose private mapping, raw rows, credentials, endpoints, or identifiers.
```

## Tool sequence

### Turn 1 — establish documentary expectations

1. Verify that the requested corpus exists:

   ```json
   {
     "tool": "helix_knowledge__list_versions",
     "arguments": {"product": "cmdb"}
   }
   ```

   Continue with the version-matched comparison only when `26.1` reports
   `indexed=true`.

2. Retrieve official reconciliation evidence:

   ```json
   {
     "tool": "helix_knowledge__search_docs",
     "arguments": {
       "query": "How does BMC Helix CMDB identify and reconcile duplicate CIs from multiple data sources?",
       "source_scope": "bmc_official",
       "product": "cmdb",
       "version": "26.1",
       "top_k": 5
     }
   }
   ```

3. Retrieve the complementary normalization evidence:

   ```json
   {
     "tool": "helix_knowledge__search_docs",
     "arguments": {
       "query": "What role does normalization play before CMDB identification and reconciliation?",
       "source_scope": "bmc_official",
       "product": "cmdb",
       "version": "26.1",
       "top_k": 5
     }
   }
   ```

4. Call `helix_knowledge__get_section` for each selected result with one adjacent chunk
   before and after. The returned `document_id` must match its search result. Do not
   treat a search snippet as sufficient context when the adjacent section is available.

The lexical-only path should report `lexical=true`, `semantic=false`, and
`reranked=false`. Different enabled retrieval modes may change rank order; cite the
current returned metadata rather than hard-coding ranks.

### Turn 1 — prepare the bounded live read

5. Call `helix__list_targets` and select `dev` explicitly. Confirm from its safe
   capabilities that the required reads and SQL planning are permitted.
6. Call `helix__health_check` with `environment="dev"`. Stop the live half if readiness
   is not healthy.
7. Use the minimum required Gateway discovery tools from the private mapping:
   `list_forms`, `list_form_fields`, `list_database_objects`,
   `list_database_columns`, or `describe_database_object`. Database metadata requires an
   AR System administrator account.
8. Call `helix__plan_sql_query` with `environment="dev"`, the exact privately mapped
   SELECT, and `limit=20`.
9. Present the returned environment, SQL, limit, plan ID, digest, status, expiry, and
   `remaining_seconds`. End the turn. Planning performs no SQL execution and is not
   approval to continue.

The SQL must use explicit safe aliases, no output wildcard, one read-only statement,
only policy-allowed objects and functions, and no direct database connection. The
complete SQL remains visible to the approver because AR API SQL does not support bound
parameters.

### Turn 2 — verify approval and execute once

After the user explicitly approves the displayed plan in a new message:

1. Call `helix__get_sql_query_plan` using the original `dev` environment and `plan_id`.
2. Compare the returned environment, SQL, limit, digest, status, and remaining lifetime
   with the approved values.
3. Stop if the plan is changed, expired, cancelled, already used, or not pending. A new
   plan requires a new review and a later approval.
4. Call `helix__execute_sql_query` once with the original environment, plan ID, and
   digest.
5. Retain only the bounded, presentation-safe observations needed for the final review.

Declining approval should call `helix__cancel_sql_query_plan` or allow the plan to
expire. It must never cause execution.

## Evidence ledger

The final answer should use a ledger such as this before writing its narrative:

| Candidate | Documentary evidence | Synthetic DEV observation | Permitted inference | Follow-up requiring human review |
| --- | --- | --- | --- | --- |
| Duplicate system names | Cite retrieved identification/reconciliation guidance | Count and presentation-safe name only | Possible duplicate; identity is not established | Review identification rules, source data, and precedence |
| No active OS relationship | Cite only if the retrieved corpus supports the expected relationship | System has zero active mapped relationships | Coverage or lifecycle issue may exist | Check discovery, relationship generation, and logical deletion |
| Multiple active OS relationships | Cite only applicable model guidance | System has more than one active mapped relationship | May be valid or stale; model semantics decide | Review relationship direction and active-state rules |
| Missing OS version | Cite applicable normalization/source-completeness evidence when found | Related OS lacks the mapped version value | Source completeness may need investigation | Review source feed and normalization behavior |
| Scan older than 90 days | Do not invent a BMC requirement for the scenario's chosen threshold | Last scan precedes the agreed synthetic cutoff | CI may be inactive, unreachable, or undiscovered | Confirm operational policy and discovery coverage |

The 90-day threshold is a scenario decision unless the retrieved evidence explicitly
establishes it. It must not be attributed to BMC by default.

## Expected final structure

1. **Scope and readiness** — Knowledge corpus, confirmed version alignment, explicit
   synthetic DEV target, and query limit.
2. **Documented evidence** — paraphrased conclusions with Knowledge provenance.
3. **Observed candidates** — sanitized Gateway results, with no raw rows or identifiers.
4. **Interpretation** — cautious links between evidence and observations, labelled as
   inference.
5. **Recommended review** — checks a CMDB specialist could perform next.
6. **Actions not taken** — no create, update, merge, deletion, attachment, or retry.

## Failure and abstention paths

| Condition | Required behavior |
| --- | --- |
| CMDB 26.1 is not indexed | Stop version-specific retrieval and report that Knowledge is not ready |
| Target version cannot be aligned | Keep documentary and live results separate; do not claim a version-matched assessment |
| DEV target or bridge is unhealthy | Return documentary evidence only and make no live-state claim |
| Private mapping is absent or ambiguous | Do not invent forms, objects, fields, relationship direction, or lifecycle semantics |
| SQL or metadata is not permitted | Stop the SQL path; explain the policy or `ARAPI_ADMIN_REQUIRED` boundary |
| Evidence does not support a live finding | Report the observation without manufacturing a BMC rule |
| Approval is absent or ambiguous | Do not execute; cancel or allow expiry |
| Plan differs or is no longer pending | Do not execute and do not replace it without a new approval cycle |
| Execution outcome is uncertain | Do not retry automatically; report the sanitized Gateway state |

## Acceptance checklist

- [ ] Both MCP servers are connected and their responsibilities are stated correctly.
- [ ] The Knowledge corpus is restricted to official CMDB 26.1 evidence.
- [ ] Selected Knowledge sections are expanded and retain full provenance.
- [ ] Retrieval can run lexical-only without loading semantic or reranker models.
- [ ] The Gateway target is explicitly `dev`, synthetic, healthy, and version-aligned.
- [ ] Every physical name comes from the authorised private mapping or live discovery.
- [ ] The planned SQL is SELECT-only, policy-allowed, aliased, and limited to 20 rows.
- [ ] Planning and execution occur in separate turns with explicit approval between them.
- [ ] The approved plan is retrieved and compared before its single execution.
- [ ] Documentary facts, observations, inferences, and proposed follow-ups remain distinct.
- [ ] Findings are described as candidates rather than automatic diagnoses.
- [ ] No write or remediation tool is called.
- [ ] No BMC text, raw row, local path, token, endpoint, customer name, private schema,
      or stable business identifier is committed or shown publicly.

## Why this is the first integrated case

The scenario demonstrates the complete product boundary without requiring an automatic
change. Knowledge prevents the agent from reasoning only from memory; Gateway prevents
it from treating documentation as proof of live state. The approval boundary then makes
even a high-impact read reviewable before execution. Remediation can be evaluated in a
later case only after these evidence and control disciplines are shown to hold.

The next step in the sequence is the
[integrated controlled-update case](integrated-controlled-update-case.md), which adds one
human-approved synthetic DEV write without widening the authority boundary.

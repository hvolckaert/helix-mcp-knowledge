# Integrated controlled Helix update case

This case demonstrates a governed action by one autonomous Helix specialist using both
MCP servers:

- **Helix MCP Knowledge** retrieves the applicable official documentation and the
  authorized project runbook.
- **Helix MCP Gateway** reads one synthetic DEV record, creates an exact temporary
  update plan, waits for human approval, applies it once, and verifies the result.

The scenario updates one fictional, non-sensitive assignment marker on one authorized
synthetic configuration item. It does not create records, delete data, attach files,
perform a bulk change, or modify PROD.

The Gateway half extends its independently validated
[controlled form-update case](https://github.com/hvolckaert/helix-mcp-gateway/blob/main/docs/use-cases/controlled-form-update.md).
This public guide contains no copied BMC text, private form or field name, entry
identifier, business value, credential, endpoint, or raw tool result.
The [short case study](https://github.com/hvolckaert/helix-mcp-knowledge/blob/main/docs/agent-evidence-to-controlled-dev-update-case.md) separates
the observed validation from this full reference procedure.

## Outcome first

The successful workflow must prove all of the following:

1. Official evidence and the private project runbook support the proposed procedure.
2. The current value was observed on one explicitly selected synthetic DEV record.
3. The exact update was planned without changing Helix.
4. A human reviewed the current and proposed values, reason, target, digest, and expiry.
5. Approval arrived in a later message and applied only to that unchanged plan.
6. Gateway checked the stored current-state precondition immediately before the write.
7. A bounded read verified the final value, and sanitized audit metadata recorded the
   operation without recording business data.
8. The same form remained outside the configured PROD update scope.

Documentation supports the decision; it does not prove current state or grant
authorization. Project evidence describes the installation-specific procedure; it does
not override Gateway policy or the BMC account's permissions.

## Reference scope

- Helix MCP Knowledge `1.31.0` or later.
- Helix MCP Gateway `0.9.0` or later.
- BMC Helix CMDB `26.1` indexed in Knowledge.
- One explicitly selected Knowledge project with an indexed, authorized runbook for the
  synthetic demonstration.
- One fictional DEV record and one initially empty, non-sensitive field.
- Gateway policy permitting update of only the required DEV form and field.
- The demonstration form absent from PROD's update scope.
- Confirmed alignment between the DEV target, the official documentation version, and
  the project runbook.

The private runbook should define the approved environment, purpose, record-selection
rule, physical form and field mapping, fictional proposed value, verification step, and
rollback owner. These details remain in the local project corpus and private
demonstration notes.

The examples use the default OpenClaw server names `helix_knowledge` and `helix`.
Another MCP client may display different prefixes while exposing the same tool names.

## Reproducible prompt

```text
Use helix_knowledge and helix to prepare one controlled update of an authorized
synthetic CMDB record in DEV. Do not modify any other record or environment.

First use helix_knowledge. Verify that CMDB 26.1 is indexed and identify the explicit
project selected for this demonstration. Search BMC official CMDB 26.1 documentation
for evidence applicable to modifying a CI attribute. Separately search only that
project's evidence for the approved synthetic assignment-marker procedure. Expand the
strongest official and project sections. Record the title, heading, version, source
scope, project when applicable, and source URL when present. Do not use model memory to
fill evidence gaps.

Continue only if the official evidence, project runbook, target version, and requested
operation are compatible. Project documentation cannot override Gateway policy or BMC
permissions.

Then use helix. Select dev explicitly, check readiness, discover or verify only the
privately mapped form and non-sensitive field, and perform a bounded read that selects
one fictional record and its current value. Prepare an update plan for the agreed
fictional value with the reason from the runbook.

Show the exact environment, form, entry identifier, current and proposed values,
reason, plan ID, digest, status, expiry, and remaining lifetime. Do not apply the plan in
this turn. Stop and wait for my explicit approval.

After approval in a later message, retrieve the same pending plan. Verify that every
approval-bound element is unchanged, then apply it exactly once. Read the selected field
back with a bounded query and report the verification result. If the outcome is unknown,
do not retry automatically.

Separate documentary evidence, project decision, live observation, approved action,
and verification in the final response. Do not expose private mapping, raw records,
credentials, endpoints, or unrelated values.
```

## Tool sequence

### Turn 1 — establish the authority chain

1. Verify the official corpus:

   ```json
   {
     "tool": "helix_knowledge__list_versions",
     "arguments": {"product": "cmdb"}
   }
   ```

   Continue only when `26.1` reports `indexed=true`.

2. Call `helix_knowledge__list_projects` and select the exact authorized project. Use
   its returned `project_id` explicitly in every project-scoped call rather than relying
   on an unrelated session's active project.

3. Retrieve official evidence:

   ```json
   {
     "tool": "helix_knowledge__search_docs",
     "arguments": {
       "query": "What documentation and data-integrity considerations apply when modifying a CI attribute in BMC Helix CMDB?",
       "source_scope": "bmc_official",
       "product": "cmdb",
       "version": "26.1",
       "top_k": 5
     }
   }
   ```

4. Retrieve the installation-specific runbook separately:

   ```json
   {
     "tool": "helix_knowledge__search_docs",
     "arguments": {
       "query": "approved procedure for the synthetic DEV assignment-marker update",
       "source_scope": "project",
       "project_id": "<project_id returned by list_projects>",
       "product": "cmdb",
       "version": "26.1",
       "top_k": 5
     }
   }
   ```

5. Call `helix_knowledge__get_section` for the selected official chunk. Call it again
   for the selected project chunk with the same explicit `project_id`. Request one
   adjacent chunk before and after. Each returned `document_id` must match the selected
   result.

6. Build a decision record containing:

   | Evidence class | Required conclusion |
   | --- | --- |
   | Official BMC evidence | What the retrieved documentation actually supports |
   | Project evidence | Why this exact synthetic change is permitted and how it is verified |
   | Deployment context | Why CMDB 26.1 evidence applies to the selected DEV target |
   | Unresolved gap | Any missing evidence that prevents safe planning |

The lexical-only path remains valid. Semantic search and reranking may stay disabled.
Retrieval mode affects ranking, not the authority of the returned source.

### Turn 1 — observe and plan without writing

7. Call `helix__list_targets` and select `dev` explicitly. A high-level
   `form_update=true` capability does not authorize every form or field; the plan call
   remains the authoritative policy check.
8. Call `helix__health_check` with `environment="dev"` and stop if the target is not
   ready.
9. Use `helix__list_forms` and `helix__list_form_fields` only as needed to verify the
   private mapping. Do not discover broadly or copy a mapping from another installation.
10. Call `helix__query_form` with the exact permitted form, explicit minimal field list,
    private synthetic qualification, and `limit=1`. Confirm that precisely the intended
    fictional record and current marker value were selected.
11. Call `helix__plan_update_entry` with:

    ```json
    {
      "environment": "dev",
      "form": "<private authorized form>",
      "entry_id": "<selected fictional entry>",
      "values": {"<private non-sensitive field>": "<agreed fictional value>"},
      "reason": "<runbook-supported reason of at least ten characters>"
    }
    ```

12. Display the environment, form, entry ID, current and proposed values, reason,
    `plan_id`, `plan_digest`, status, expiry, and `remaining_seconds`. End the turn.

Planning performs no write. It stores the normalized proposal and a precondition derived
from the current `Modified Date`; it is not permission to apply the update.

### Turn 2 — verify approval and apply once

Only after explicit approval in a later user message:

1. Call `helix__get_write_plan` with the original `dev` environment and `plan_id`.
2. Compare environment, operation, form, entry ID, current values, proposed values,
   reason, digest, status, and remaining lifetime with the plan the user reviewed.
3. Stop if any element differs or the plan is not pending. A replacement plan requires
   a new review and a later approval.
4. Call `helix__apply_update_entry` once with the original environment, plan ID, and
   digest.
5. If Gateway reports `status=applied`, call `helix__query_form` with the same bounded
   selector and minimal fields to verify the resulting marker value.
6. Report the sanitized audit outcome separately from business evidence. Audit metadata
   proves that operations occurred but deliberately omits form names, entry IDs, values,
   and returned records.

If the record's `Modified Date` changed after planning, Gateway must reject the stale
precondition. If the apply result is `outcome_unknown`, investigate rather than retrying:
the Helix write may have succeeded even though local result persistence failed.

Declining approval should call `helix__cancel_write_plan` or allow the plan to expire.
It must never cause an apply call.

## Optional PROD policy proof

Only in the authorized synthetic validation setup, attempt to plan the same form update
against `prod`. The expected result is `FORM_WRITE_FORM_NOT_ALLOWED` before a plan or
record read exists. Never apply anything in PROD.

If Gateway unexpectedly returns a plan, cancel it immediately, record a policy failure,
and end the scenario. A broad `form_update=true` capability never substitutes for the
exact form-and-field policy check.

## Evidence and action ledger

| Stage | Source | What it proves | What it cannot prove |
| --- | --- | --- | --- |
| Official retrieval | Knowledge, `bmc_official` | Applicable statements in versioned BMC documentation | Current target state or local authorization |
| Runbook retrieval | Knowledge, explicit project | Approved local intent, mapping, value, and verification procedure | Gateway policy or BMC permission |
| Initial read | Gateway, synthetic DEV | Current value of the selected fictional record | That a change is authorized |
| Update plan | Gateway local plan store | Exact proposed operation passed structural policy validation | Human approval or successful Helix execution |
| Later approval | User message plus unchanged digest | The reviewed pending plan may be applied | That its optimistic precondition still holds |
| Apply result | Gateway through AR API | Known applied, rejected, or uncertain outcome | Business correctness without verification |
| Verification read | Gateway, synthetic DEV | Observed final value after a known apply | Broader CMDB quality or production suitability |
| Audit event | Gateway closed schema | Sanitized operational occurrence and outcome | Business payload, raw response, or documentary meaning |

## Failure and abstention paths

| Condition | Required behavior |
| --- | --- |
| CMDB 26.1 is not indexed | Stop the version-specific evidence path |
| Project runbook is absent, ambiguous, or not indexed | Do not invent the local procedure or mapping |
| Official, project, and target versions are not aligned | Do not present a version-matched decision |
| Official evidence conflicts with the project runbook | Stop and request human resolution; project evidence cannot overrule product constraints |
| DEV is unhealthy or the exact form/field is denied | Do not create a plan |
| Initial query selects zero or multiple records | Refine the private selector; do not guess an entry ID |
| Current value already equals the proposal | Report a no-op and do not plan a write |
| Approval is missing or ambiguous | Do not apply; cancel or allow expiry |
| Plan changed, expired, cancelled, failed, or already consumed | Do not apply or replace it silently |
| Optimistic precondition fails | Report the concurrent change and require a fresh review cycle |
| Apply outcome is unknown | Never retry automatically; investigate the target state |
| Verification does not match a known successful apply | Report the discrepancy without attempting remediation |
| PROD unexpectedly accepts planning | Cancel any returned plan and treat the policy as failed |

## Acceptance checklist

- [ ] Both MCP servers are connected and their responsibilities remain separate.
- [ ] Official CMDB 26.1 and the explicit project are both indexed and version-aligned.
- [ ] Official and project searches run separately and preserve their provenance.
- [ ] Selected sections are expanded before the decision is formed.
- [ ] Semantic search and reranking may remain disabled.
- [ ] The target is an explicitly authorized fictional record in `dev`.
- [ ] Only one non-sensitive field and one fictional value are proposed.
- [ ] The bounded initial read selects exactly one record and detects a no-op.
- [ ] Gateway policy authorizes the exact DEV form and field.
- [ ] Planning and applying occur in separate turns with visible approval between them.
- [ ] The same pending plan and digest are retrieved and compared before apply.
- [ ] The optimistic current-state precondition is enforced.
- [ ] The plan is applied at most once and the result is verified with a bounded read.
- [ ] `outcome_unknown` is never retried automatically.
- [ ] No PROD write occurs; the optional policy proof rejects before a plan exists.
- [ ] The final answer separates documentation, project decision, live observation,
      approved action, verification, and sanitized audit outcome.
- [ ] No BMC text, private mapping, raw record, token, endpoint, customer name, or
      unrelated business value is committed or published.

## Position in the case sequence

The [integrated CMDB data-quality case](integrated-cmdb-data-quality-case.md) proves that
the agent can compare documentary evidence with bounded live observations. This second
case adds one governed write without widening the authority boundary. A later case can
address a complete ITSM objective only after both evidence discipline and write-plan
discipline have been demonstrated independently.

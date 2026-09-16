# Observed integrated read-only preflight report

This report records one uninterrupted execution of the repository's
[integrated preflight probe](../evaluation/integrated-read-only-preflight/README.md)
across separate Helix MCP Knowledge and Helix MCP Gateway sessions. It demonstrates an
authority-preserving read path for an autonomous Helix specialist without publishing
the installation's private mapping or source text.

## Outcome first

The probe returned **`ready_for_human_review`**. This means that the selected Knowledge
evidence, explicit synthetic DEV target and one bounded live record passed the configured
read-only gates. It does **not** mean `ready_to_plan`, authorize a write, confirm target
release alignment, or prove that reconciliation is inactive.

A later read-only follow-up verified the exact write-policy gate for the private form and
field. A further administrative read established that the DEV AR System platform reports
26.1.01, but this does not establish the separately reported CMDB component release. The
overall decision therefore remains `ready_for_human_review` rather than `ready_to_plan`.

No plan was created, no record was changed, and neither QA nor PROD was contacted.

## Scope

| Layer | Selected scope |
| --- | --- |
| Knowledge | Helix MCP Knowledge 1.31.8; official CMDB 26.1 plus one explicit private validation project |
| Retrieval | FTS5 lexical only; semantic retrieval and reranking disabled |
| Gateway | Helix MCP Gateway 0.10.3; explicit non-production `dev` target |
| Live operation | One policy-bounded form read, limit 2, expecting exactly one synthetic record |
| Output | Counts, booleans, health and decision only |

The populated Gateway mapping remained outside the repository with mode `0600`. The
probe received it as a private runtime input and did not print its form, fields,
qualification, identifiers or expected values.

## Evidence ledger

| Evidence class | Observed result | What it establishes | What it does not establish |
| --- | --- | --- | --- |
| Official documentation | CMDB 26.1 was indexed; five official results were returned; the selected section and provenance were verified. | Applicable versioned documentary context exists. | The live target runs CMDB 26.1 or permits a change. |
| Project procedure | The explicit validation project was registered; five project results were returned; the selected section and project provenance were verified privately. | A local synthetic procedure and selector exist. | Product correctness, Gateway policy or human approval. |
| Target readiness | DEV was enabled, non-production and healthy. | The authorized read path was available during the run. | Future availability or write readiness. |
| Bounded live observation | Exactly one record matched; four equality, one empty-state and one presence check passed. | The private synthetic selector and expected current state agreed at that moment. | Broader data quality, inactive reconciliation or an unchanged future state. |
| Effective write policy | One form and one field matched the private mapping; no write wildcard was active; human approval and a write reason were required. | The exact policy gate for this case was satisfied at follow-up time. | Approval of a plan, a future policy state or least-privilege read scope. |
| Platform version | The standard Server Information path reported AR System 26.1.01 through a bounded administrative read. | The DEV AR System platform version is attributable. | The separately reported CMDB component version. |
| CMDB release verification | Standard application metadata and documented CMDB version-property paths were inspected through bounded reads, but none exposed an attributable current CMDB release. | The component gate was attempted without guessing or conflating products. | That DEV runs CMDB 26.1. |
| Agent inference | The documentary, project and live read gates were mutually consistent. | Continuing to a human review is reasonable. | Authority to create a plan or modify Helix. |

## Documentary evidence

The selected official result was
[Using CMDB Data Analyzer to investigate CMDB data issues — BMC Helix Documentation](https://docs.helixops.ai/bin/Service-Management/IT-Service-Management/BMC-Helix-CMDB/ac261/Troubleshooting/Investigating-CMDB-data-issues/Using-CMDB-Data-Analyzer-to-investigate-CMDB-data-issues/),
CMDB 26.1, official scope. Its adjacent section was expanded before the ledger was
formed. It supplies documentary context for investigating CMDB data issues; it is not an
authorization or a record-specific instruction.

The project result came from the explicitly selected synthetic validation project. Its
section was expanded separately with project scope. The source title, local path,
mapping and contents were inspected only in the authorized private environment and are
intentionally omitted here.

## Live observation

Gateway reported a healthy DEV bridge and performed one bounded form read. The output
ledger retained only:

- one result out of one expected;
- four of four equality checks passed;
- one of one empty-state check passed;
- one of one required-presence check passed.

These booleans are sufficient for the preflight decision without exposing the record,
field names, values, qualification or raw response.

## Read-only gate follow-up

On 2026-09-16, the effective Gateway configuration was compared privately with the
case mapping. The write allowlist contained exactly the mapped form and field, did not
use write wildcards, and required both human approval and a write reason. The broader
read discovery scope remains wider than this single case requires and should be narrowed
as a separate least-privilege improvement.

The platform portion of the release gate is now attributable: the standard Server
Information path reported AR System 26.1.01 through BMC's documented
[Server Information route](https://docs.helixops.ai/bin/Service-Management/Innovation-Suite/BMC-Helix-Innovation-Suite/is261/Troubleshooting/Collecting-diagnostics/Displaying-version-information/).
BMC's product guidance presents the AR System and CMDB versions as separate diagnostic
facts, so that observation was not relabelled as a CMDB version.

The CMDB component portion did not close. The standard application version registry was
empty; the CMDB information data path did not expose an attributable current release;
the interface form containing the CMDB version display fields was not queryable through
the API; and BMC's documented
[CMDB version properties](https://docs.bmc.com/xwiki/bin/view/Service-Management/IT-Service-Management/BMC-Helix-CMDB/ac252/Developing/Integrating-your-services-with-external-products-by-using-the-CMDB-web-services-API/Modifying-the-web-services-configuration/)
were not present with attributable values in the standard configuration form. No
auxiliary, historical-looking or component-adjacent value was treated as proof of CMDB
26.1.

The follow-up used only form catalog, field catalog and bounded query operations. It did
not create a plan or attempt a write.

## Interpretation and recommendation

The observed state is consistent with the private synthetic procedure, so the agent may
present the case for human review. It must remain read-only until an operator explicitly
requests a new planning step.

Before any plan, a later run must independently confirm:

1. the live target's CMDB release is aligned with the selected 26.1 documentation;
2. the selected record still has the reviewed current state;
3. no reconciliation can propagate the synthetic change outside the intended dataset;
4. a human reviewer owns verification and reversal.

The exact-policy gate has been verified, but it must still be rechecked if the mapping or
Gateway configuration changes before a future plan.

Failure of any gate requires `stop`. A future plan would be a new artifact with its own
digest and expiry and would require approval in a later turn. This report cannot serve
as that approval.

## Acceptance result

- [x] One orchestrated run used both MCP servers without making them call each other.
- [x] Official and project Knowledge searches remained separate and version-scoped.
- [x] Both selected sections were expanded and their provenance checked.
- [x] Semantic retrieval and reranking remained disabled.
- [x] Gateway used only explicit synthetic DEV reads.
- [x] Exactly one bounded record passed all six private checks.
- [x] Output contained no private mapping token or raw row.
- [x] A non-DEV mapping was rejected before connection.
- [x] The effective write allowlist matched the private form and field exactly.
- [x] DEV's AR System platform version is attributable as 26.1.01.
- [ ] DEV's live CMDB release is attributable and aligned with 26.1.
- [x] No plan, write, QA call or PROD call occurred.

This is a deterministic acceptance run of the authority chain, not a benchmark of a
language model's reasoning quality or evidence that an autonomous write is safe.

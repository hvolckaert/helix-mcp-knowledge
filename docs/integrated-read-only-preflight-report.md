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

## Interpretation and recommendation

The observed state is consistent with the private synthetic procedure, so the agent may
present the case for human review. It must remain read-only until an operator explicitly
requests a new planning step.

Before any plan, a later run must independently confirm:

1. the live target's CMDB release is aligned with the selected 26.1 documentation;
2. the exact DEV form and field are admitted by current Gateway policy;
3. the selected record still has the reviewed current state;
4. no reconciliation can propagate the synthetic change outside the intended dataset;
5. a human reviewer owns verification and reversal.

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
- [x] No plan, write, QA call or PROD call occurred.

This is a deterministic acceptance run of the authority chain, not a benchmark of a
language model's reasoning quality or evidence that an autonomous write is safe.

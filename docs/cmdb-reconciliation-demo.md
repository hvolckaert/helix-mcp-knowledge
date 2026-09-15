# CMDB reconciliation evidence demo

This three-to-five-minute scenario demonstrates how an autonomous Helix specialist can
answer a CMDB question from versioned BMC evidence without accessing or changing a live
environment. It uses only the lightweight lexical retrieval path; semantic search and
reranking may remain disabled.

The repository contains the scenario and expected metadata, but no copied BMC content.
Each participant retrieves documentation that they are authorized to access into their
own local index.

## Outcome first

The agent should explain, in its own words, that reconciliation addresses conflicting
representations of the same CI by producing a consolidated production view. It should
also distinguish the role of normalization from the later identification and merging of
duplicates. Every supported statement must cite the retrieved document title, section,
CMDB version, source scope, and URL.

The agent must not claim that it inspected a live CMDB. That would require Helix MCP
Gateway and a separately authorized environment.

## Prerequisites

- Helix MCP Knowledge `1.31.0` or later.
- BMC Helix CMDB `26.1` selected and fully synchronized.
- The `helix_knowledge` MCP server connected to the agent.
- Semantic retrieval and reranking disabled for the lightweight reference run.

Confirm readiness before recording:

```text
Call list_versions with product="cmdb". Continue only if version 26.1 reports
indexed=true. Otherwise explain that the corpus is not ready and request synchronization.
```

## Reproducible agent prompt

```text
Use only the helix_knowledge tools for this task. Do not inspect or change a live Helix
environment.

First verify that CMDB 26.1 is indexed. Then answer:

How does BMC Helix CMDB resolve conflicting duplicate CIs into a trusted production view?

Search only BMC official evidence for product cmdb and version 26.1. Retrieve adjacent
section context for the strongest result before answering. Separate supported BMC
evidence from your inference. For each important statement, cite the document title,
heading when present, version, source scope, and source URL. If the evidence is
insufficient, say so instead of filling gaps from model memory.
```

## Expected MCP sequence

### 1. Verify the indexed version

```json
{
  "tool": "helix_knowledge__list_versions",
  "arguments": {
    "product": "cmdb"
  }
}
```

The response must contain `{"version": "26.1", "indexed": true}`.

### 2. Retrieve official evidence

```json
{
  "tool": "helix_knowledge__search_docs",
  "arguments": {
    "query": "How does BMC Helix CMDB resolve conflicting duplicate CIs into a trusted production view?",
    "source_scope": "bmc_official",
    "product": "cmdb",
    "version": "26.1",
    "top_k": 5
  }
}
```

The evidence set should include both the reconciliation overview and the procedure for
merging duplicate CIs from multiple sources. Record the returned ranks rather than
hard-coding them into the answer; a later authorized corpus refresh may change ordering.

The lightweight reference run should report `lexical=true`, `semantic=false`, and
`reranked=false` in the match metadata.

### 3. Expand the strongest section

Use the `chunk_id` of the strongest relevant result:

```json
{
  "tool": "helix_knowledge__get_section",
  "arguments": {
    "chunk_id": "<chunk_id returned by search_docs>",
    "context_before": 1,
    "context_after": 1
  }
}
```

The returned `document_id` must match the selected search result. The answer should use
the expanded context, while its citation metadata comes from the corresponding
`search_docs` result.

## Evidence matrix

| Statement to assess | Evidence role | Required citation metadata |
| --- | --- | --- |
| Multiple sources can create conflicting representations of one CI. | Reconciliation overview | Title, CMDB 26.1, official scope, URL |
| Reconciliation creates a consolidated production view from those representations. | Reconciliation overview | Title, CMDB 26.1, official scope, URL |
| Normalization prepares CI data before duplicate identification and merging. | Duplicate-CI merging procedure | Title, CMDB 26.1, official scope, URL |
| A specific job setting or live dataset is configured correctly. | Not established by this scenario | Must be omitted or labelled unverified |

This matrix separates documentary evidence from live state and from agent inference. The
[integrated CMDB data-quality case](integrated-cmdb-data-quality-case.md) continues the
workflow with Gateway after Knowledge has established the documentary expectation.

## Reference observation

On 12 September 2026, the `1.31.0` lexical-only reference run over the author's authorized
CMDB 26.1 index returned:

1. `Reconciliation - BMC Helix Documentation`.
2. `Merging duplicate CIs by reconciling data from multiple sources - BMC Helix
   Documentation`.

The observation records titles and ranks only. It does not freeze or redistribute the
underlying BMC text. Re-run the scenario after synchronization and preserve the current
tool output only within the authorized local environment.

## Recording outline

1. Open with the final question and the evidence-backed conclusion.
2. Show `list_versions` proving that CMDB 26.1 is indexed.
3. Show the `search_docs` filters and the two complementary evidence results.
4. Point out version, official scope, source URL, and lexical-only match metadata.
5. Call `get_section` and explain why adjacent context matters.
6. Show the answer divided into supported evidence, inference, and unverified live state.
7. Close by stating that Gateway would be required for any environment observation or
   governed action.

## Acceptance checklist

- [ ] The run starts from the prompt above without private instructions.
- [ ] CMDB 26.1 readiness is checked before retrieval.
- [ ] `search_docs` is restricted to official CMDB 26.1 evidence.
- [ ] The overview and duplicate-CI procedure both appear in the top five.
- [ ] The selected section is expanded with adjacent context.
- [ ] Every material statement carries title, version, scope, and URL provenance.
- [ ] Inference is visually separated from documentary evidence.
- [ ] No claim about live CMDB state is presented as known.
- [ ] No BMC text, local path, token, customer name, or private project data is committed.
- [ ] The complete recording remains under five minutes.

## Insufficient-evidence variant

Repeat the prompt with a CMDB version that `list_versions` does not report as indexed. A
correct agent stops before retrieval and explains that version-specific evidence is not
available. This demonstrates that honest abstention is part of the product, not an error
to hide in the recording.

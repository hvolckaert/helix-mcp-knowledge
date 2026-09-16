# Retrieval evaluation

This directory provides a copyright-safe starting point for a frozen retrieval
benchmark. The included corpus and questions are fictional; they do not reproduce
BMC documentation, customer material, or private project decisions.

## Run the synthetic baseline

Use a disposable workspace so the sample cannot affect a real index:

```bash
helix-mcp-knowledge init --workspace /tmp/helix-knowledge-evaluation
mkdir -p /tmp/helix-knowledge-evaluation/data/sources/bmc/official/synthetic
cp evaluation/synthetic-corpus/*.md \
  /tmp/helix-knowledge-evaluation/data/sources/bmc/official/synthetic/
helix-mcp-knowledge \
  --config /tmp/helix-knowledge-evaluation/config/config.yaml \
  ingest /tmp/helix-knowledge-evaluation/data/sources/bmc/official/synthetic \
  --scope bmc_official --document-type other --recursive
helix-mcp-knowledge \
  --config /tmp/helix-knowledge-evaluation/config/config.yaml \
  evaluate-retrieval evaluation/synthetic-baseline.json \
  --top-k 5 --format markdown --output evaluation/synthetic-report.md
```

The evaluator writes a SHA-256 fingerprint of the exact dataset, records index counts
and active retrieval modes, warms each mode before measurement, and rotates timed mode
order between cases. The Markdown report deliberately excludes retrieved text and local
paths so it can be reviewed before sharing.

## Build the private BMC baseline

Create a separate dataset that is not committed until its provenance and publication
rights have been reviewed. Freeze the questions and relevance judgments before looking
at comparative rankings.

Target 30 questions for the first baseline:

| Category | Cases | Purpose |
|---|---:|---|
| concept | 6 | Vocabulary differs between the question and evidence. |
| exact | 5 | Product identifiers, configuration keys, or error tokens. |
| procedure | 6 | Ordered operational or administration steps. |
| compatibility | 4 | Supported combinations and prerequisites. |
| version | 4 | Evidence that changes between product versions. |
| project-decision | 3 | Authorized local decisions and runbooks. |
| multilingual | 2 | Equivalent intent expressed in another language. |

Balance the set across the priority product families and assign `easy`, `medium`, or
`hard` before execution. Every case must have a stable `case_id` and exactly one form of
relevance judgment:

- `expected_document_ids` when stable document identifiers are available; or
- `expected_terms` when all listed terms identify one acceptable evidence result.

Document identifiers give the stronger evaluation because they measure recall when
more than one source is relevant. Expected terms are useful during initial authoring,
but they should be replaced with identifiers after the corpus is frozen.

## Dataset contract

```json
{
  "schema_version": 1,
  "name": "Private BMC retrieval baseline 1",
  "description": "Provenance reviewed separately; no source text embedded.",
  "cases": [
    {
      "case_id": "cmdb-concept-en-001",
      "name": "Human-readable description",
      "category": "concept",
      "difficulty": "medium",
      "language": "en",
      "query": "Question supplied to search",
      "product": "cmdb",
      "version": "26.1",
      "source_scope": "bmc_official",
      "expected_document_ids": ["doc_reviewed_identifier"]
    }
  ]
}
```

Search request fields (`project_id`, `source_scope`, `product`, `version`, and
`document_types`) are optional. Unknown fields, unsupported schema versions, duplicate
case IDs, empty evidence, and invalid difficulty labels are rejected.

## Interpretation

The aggregate report exposes Hit Rate@k, MRR, Recall@k, nDCG@k, and p50/p95 latency for
lexical, configured baseline, and reranked retrieval. It also groups quality by category,
difficulty, and language in JSON. A reranked comparison is published only when the
backend scored every non-empty result set.

Do not tune on this baseline indefinitely. Record difficult cases, make a change only
for a stated hypothesis, then confirm it on a separate holdout set or with external
tester evidence.

## Resource-light fictional first-query case

The repository-only
[`first-query-fixture`](https://github.com/hvolckaert/helix-mcp-knowledge/tree/main/evaluation/first-query-fixture)
supports a source-linked MCP result using one fictional project document. No BMC
corpus, semantic model or reranker is required. A local rehearsal is not an
external tester pass. The repository-only
[written Knowledge case](https://github.com/hvolckaert/helix-mcp-knowledge/blob/main/docs/knowledge-source-grounded-answer-case.md)
records both the supported answer and a deliberately unsupported query. The
external tester pilot protocol is deferred.

## CMDB 26.1 evidence case

The metadata-only
[`cmdb-evidence-case`](cmdb-evidence-case/cmdb_evidence_probe.py) validates the
documentary path used by the
[`CMDB reconciliation demo`](../docs/cmdb-reconciliation-demo.md). It requires an
authorized local CMDB 26.1 index, restricts retrieval to official scope and runs with
semantic retrieval and reranking disabled.

The probe verifies that the reconciliation overview and duplicate-CI procedure occur in
the top five, expands both sections and records an abstention for an unindexed fictional
version. Its JSON output contains provenance metadata and boolean checks, but no BMC
source text, local paths or runtime-generated identifiers.

## Integrated Knowledge + Gateway preflight

The
[`integrated-read-only-preflight`](integrated-read-only-preflight/README.md) performs a
single sanitized readiness sequence across both MCP servers. Knowledge verifies the
official CMDB version and a separately scoped project procedure; Gateway checks only an
explicit synthetic DEV target and one bounded form read.

All installation-specific Gateway inputs remain in an external private JSON file. The
probe can return `ready_for_human_review` or stop with a presentation-safe blocker, but
it has no planning or write path. It is an acceptance harness for the authority chain,
not approval for a later operation.

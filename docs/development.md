# Developing Helix MCP Knowledge

Use this guide for a development checkout, optional local components, and MCP
acceptance testing. Contribution and security rules remain in the repository
root policies.

## Development setup

Public interfaces, documentation, contribution metadata, and release notes use
English. See [Contributing](../CONTRIBUTING.md) for the language and quality policy.

```bash
uv sync --extra dev
uv run helix-mcp-knowledge init-db
uv run helix-mcp-knowledge configure
uv run helix-mcp-knowledge status
uv run helix-mcp-knowledge serve
```

`configure` shows the products and versions available from the official catalog.
The same selection can be saved non-interactively and synchronized immediately:

```bash
uv run helix-mcp-knowledge configure \
  --product arsystem=26.1 \
  --product cmdb=26.1 \
  --product itsm=26.1 \
  --automatic-sync \
  --interval-hours 24 \
  --sync-now
```

### Optional semantic search

Semantic retrieval is disabled and not installed in a new deployment. In the
dashboard, select **Enable semantic search** to install the isolated component,
download the pinned BGE-M3 model, validate it, and create vectors for the active
SQLite chunks. The dashboard reports installation and vectorization progress.
Before encoding, chunks are grouped by length to avoid unnecessary padding and
input is capped at 1,024 model tokens, above the configured 900-token ingestion
limit. After the first batch the dashboard reports measured throughput and an
estimated completion time. **Cancel semantic setup** stops safely after the
current batch and leaves semantic retrieval disabled.
Every activation reconciles Qdrant against authoritative active SQLite chunk
identifiers: stale vectors are deleted and only missing chunks are encoded. The
worker repeats the consistency pass when documentation changes concurrently, so
re-enabling after a disabled synchronization does not require rebuilding an
already current vector index.

Vector metadata stores product/version pairs rather than independent product
and version lists. After a payload-format upgrade, the dashboard reports
`reindex_required`; lexical search remains available and enabling semantic
search again rebuilds the optional index safely.

The managed component requires no Docker installation. A private token-protected
service bound to `127.0.0.1` owns both the model and persistent local Qdrant
storage, allowing MCP and synchronization workers to share one model process.
The current pinned BGE-M3 model produces 1,024-dimensional dense vectors.
Plan for approximately 4 GB of RAM while semantic search is enabled; initial and
incremental vector creation can temporarily use more depending on batch and chunk
length. Disabling semantic search stops the model service and releases that memory.

Lexical FTS5 remains active during setup and is always the fallback. When the
component is ready, Reciprocal Rank Fusion combines lexical and semantic
candidates. SQLite remains authoritative: Qdrant stores vectors and filter
metadata, not canonical document text, and candidates are revalidated against
SQLite before being returned.

Disabling semantic search stops its use but retains the component and vectors.
The separate **Remove semantic data and component** action is available only
while disabled and permanently removes the managed model, dependencies, and
vectors without changing source documents or the SQLite lexical index.

Quality can be measured with a repeatable JSON evidence set instead of relying
on anecdotal searches:

```json
{
  "schema_version": 1,
  "name": "Private BMC retrieval baseline 1",
  "description": "Relevance judgments reviewed before execution.",
  "cases": [
    {
      "case_id": "cmdb-concept-en-001",
      "name": "CMDB data consistency",
      "category": "concept",
      "difficulty": "medium",
      "language": "en",
      "query": "How are duplicate configuration items prevented?",
      "product": "cmdb",
      "version": "26.1",
      "source_scope": "bmc_official",
      "expected_terms": ["normalization", "reconciliation"]
    }
  ]
}
```

```bash
helix-mcp-knowledge evaluate-retrieval evaluation.json --top-k 10 \
  --format markdown --output evaluation-report.md
```

The report compares lexical, configured baseline, and reranked retrieval. It reports
Hit Rate@k, mean reciprocal rank, Recall@k, nDCG@k, and p50/p95 latency, including
JSON slices by category, difficulty, and language. The exact dataset is identified by
SHA-256 and the report records index counts and enabled retrieval modes. Evaluation
warms every mode once and rotates their timed order between cases to reduce cold-start
and cache-order bias. Each case must identify expected evidence using either
`expected_terms` or `expected_document_ids`, never both. A reranked comparison is
marked valid only when the optional backend actually scores every non-empty case;
otherwise its aggregate comparison metrics are `null` with actionable status guidance.
The [evaluation workspace](../evaluation/README.md) includes a fictional corpus, a
ten-question executable sample, and the target composition for the private 30-question
BMC baseline. Review provenance before publishing any real question or relevance judgment.

### Optional result reranking

Result reranking is optional, disabled, and not installed in a new deployment. In the dashboard,
select **Enable result reranking** to install the isolated component, download the
pinned multilingual [`BAAI/bge-reranker-v2-m3`](https://huggingface.co/BAAI/bge-reranker-v2-m3)
model, run its local self-test, and enable it only after validation succeeds. The
Apache-2.0 model is fixed to an exact revision and inference never permits remote code.

The reranker uses 10 candidates by default (with a hard maximum of 32) that have
already passed SQLite project,
product, version, document-type, and source-scope filtering. It reorders them after
lexical/semantic fusion and before result diversification. The final order blends the
baseline and model ranks 50/50, limiting regressions from an isolated model decision.
Exact technical identifiers remain in a protected priority tier, stable ties keep the
baseline order, and raw model scores are not exposed as confidence.

The component uses a bearer-protected service bound to `127.0.0.1`, so concurrent MCP
processes share one loaded model. It creates no embeddings, vectors, or Qdrant data;
documentation synchronization therefore requires no reranker rebuild. If the service
is starting, unavailable, slow, or returns invalid data, search preserves the existing
ranking automatically. Disabling stops the service and retains its files; the separate
removal action is available only while disabled.

The measured WSL reference installation retained about 3.3 GB and used approximately
1.8 GB of RAM while loaded. With 10 candidates on CPU, evaluation latency was roughly
10–16 seconds per reranked search; actual performance depends on the processor. The
dashboard presents these costs before downloading anything.

## MCP acceptance test

The E2E suite creates a temporary official-index snapshot, adds a synthetic
private-project document, and starts the real MCP executable over `stdio`. It validates
all nine tools, the supported catalog, provenance, active-project selection and
cleanup, source isolation, and section retrieval. It never modifies production
data.

```bash
HELIX_MCP_E2E=1 uv run pytest -m e2e tests/e2e/test_mcp_stdio.py -v
```

Without `HELIX_MCP_E2E=1`, the test is skipped by the regular fast suite.

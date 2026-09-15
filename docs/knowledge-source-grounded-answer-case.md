# Case study: a source-grounded answer from fictional project knowledge

This runnable Knowledge-only case shows what an autonomous Helix specialist can do
with a small, explicitly authorized local corpus: answer one question from a
retrieved section, cite its scope and version, and decline another question when
the selected index supplies no evidence. It is intentionally **fictional** and
copyright-safe. It does not reproduce BMC documentation, query a live Helix
environment, or demonstrate the correctness of a real CMDB procedure.

## Outcome first

An evidence-backed answer to the positive question is:

> In the fictional Aster project, the recovery window begins at **02:15 UTC**
> and lasts twenty minutes. The **quartz-lock remains enabled** during that
> window. Source: *Aster Operations Acceptance Note* → *Recovery window*,
> `aster-operations.md`, project scope `aster-tester`, declared `cmdb 26.1`.

The answer is composed from the observed `search_docs` result and its expanded
`get_section` text. It is not presented as a measured autonomous-agent answer.
The `26.1` mapping comes from the **fictional project manifest**; it is a scope
tag, not proof that BMC published this instruction or that any live Helix target
is running that version.

For an unsupported question, the appropriate answer is:

> I found no indexed evidence for a “zircon override” in the selected Aster
> project corpus. I cannot state its rule from this source. This does not prove
> that such a rule does not exist elsewhere; it means this index cannot support
> the answer.

## Minimal corpus and reproduction

The [Aster fixture](../evaluation/first-query-fixture/) contains one Markdown
document, two project manifests and a client-neutral MCP probe. The manifests
restrict this document to the fictional `aster-tester` project and declare a
`cmdb 26.1` project context. No official BMC product is selected. The default
release configuration enables FTS5 lexical search and disables semantic search
and reranking.

Use the public `v1.31.5` release CLI and Python environment. The
[installation guide](installation.md) explains release installation. For a disposable WSL/Linux run, from a checkout
containing the fixture:

```bash
CASE_ROOT="$(mktemp -d /tmp/helix-knowledge-case.XXXXXX)"
CASE_CLI=/absolute/path/to/release/venv/bin/helix-mcp-knowledge
CASE_PYTHON=/absolute/path/to/release/venv/bin/python

"$CASE_CLI" init --workspace "$CASE_ROOT"
"$CASE_CLI" --config "$CASE_ROOT/config/config.yaml" init-db
mkdir -p "$CASE_ROOT/data/sources/projects/aster-tester/docs"
cp evaluation/first-query-fixture/aster-tester*.yaml \
  "$CASE_ROOT/config/projects/"
cp evaluation/first-query-fixture/aster-operations.md \
  "$CASE_ROOT/data/sources/projects/aster-tester/docs/"
"$CASE_CLI" --config "$CASE_ROOT/config/config.yaml" sync-project aster-tester
"$CASE_PYTHON" evaluation/first-query-fixture/first_query_probe.py \
  --config "$CASE_ROOT/config/config.yaml"
```

The probe starts the released server over MCP stdio, checks the exact nine-tool
inventory, calls `search_docs` twice, expands the positive hit with
`get_section`, and prints only presentation-safe metadata. It never starts a
BMC documentation download or a Helix operation. The fixture can also be
received as a small folder without cloning this repository.

## Agent prompt and tool sequence

```text
Use only Helix MCP Knowledge. The Aster project is fictional; do not describe
its instructions as official BMC guidance or as live Helix configuration.

For project_id=aster-tester, source_scope=project, product=cmdb and version=26.1:
When does the recovery window begin, and what remains enabled? Search the local
project corpus, expand the relevant section, and answer with the document title,
heading, file, scope and declared version. If the selected corpus cannot support
an answer, say so rather than relying on model memory.
```

The relevant MCP calls are:

```json
{"tool":"search_docs","arguments":{"query":"quartz-lock recovery window","project_id":"aster-tester","source_scope":"project","product":"cmdb","version":"26.1","top_k":3}}
```

Use the returned `chunk_id`, rather than a hard-coded ID, to expand context:

```json
{"tool":"get_section","arguments":{"chunk_id":"<returned chunk_id>","project_id":"aster-tester","context_before":1,"context_after":1}}
```

For the insufficient-evidence branch, repeat `search_docs` with the same scope
and `query="zircon override"`. Do not broaden to unrelated projects or claim a
rule merely because an agent can generate plausible prose.

## Observed evidence, not a generated answer

On 15 September 2026, an isolated run of the public `v1.31.5` wheel produced:

| Index state or question | Observed tool result |
| --- | --- |
| Project registered, no documents indexed | Positive query: `0` results |
| One fictional document indexed | Positive query: `1` result; title *Aster Operations Acceptance Note*; heading *Recovery window*; `project`/`aster-tester`; product `cmdb`; versions `["26.1"]` |
| Positive match and section expansion | `lexical=true`, `semantic=false`, `reranked=false`; section text supports `02:15 UTC`, twenty minutes and `quartz-lock` enabled |
| `zircon override` in the same selected corpus | `0` results |

The result's full local `source_path` and runtime-generated IDs are omitted here;
the public citation uses the source filename, title, heading, project and declared
version. The empty and indexed states were tested in separate disposable
workspaces. This case neither measures a general LLM without retrieval nor
compares hybrid or reranked quality; those claims would require separate,
resource-appropriate evaluation.

## What this case establishes

The indexed answer is traceable to a concrete section in one selected project,
while an unrelated question remains unsupported. Explicit scope prevents the
project rule from masquerading as official BMC guidance. The case demonstrates a
lightweight Knowledge retrieval path, **not** an external usability pass, an
official-document accuracy benchmark, or an agent acting on Helix. The
[integrated CMDB case](integrated-cmdb-data-quality-case.md) adds Gateway when a
separately authorized live observation is actually needed.

# helix-mcp-knowledge

[![CI](https://github.com/hvolckaert/helix-mcp-knowledge/actions/workflows/ci.yml/badge.svg)](https://github.com/hvolckaert/helix-mcp-knowledge/actions/workflows/ci.yml)
[![Release](https://github.com/hvolckaert/helix-mcp-knowledge/actions/workflows/release.yml/badge.svg)](https://github.com/hvolckaert/helix-mcp-knowledge/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An MCP server for evidence-based retrieval across the BMC Helix documentation
ecosystem. It keeps documentary knowledge (`helix-mcp-knowledge`) separate from
live environment data (`helix-mcp-gateway`).

## One autonomous Helix agent, two MCP servers

An autonomous Helix specialist needs two different kinds of evidence:

1. **Knowledge** answers what the applicable BMC and project documentation says,
   with product, version, project, source, and section provenance.
2. **Gateway** observes the authorized live environment and exposes governed
   operations under its own policy and approval controls.
3. The agent compares documentation with live state, distinguishes facts from
   inference, and acts only through the capabilities it has been granted.

Knowledge deliberately has no tool for changing a live Helix system. Its nine MCP
tools search and expand local evidence, expose index readiness, and select only the
process-local project context. See [MCP tools](#mcp-tools) for the complete contract.
The [Knowledge architecture diagram](docs/architecture/v1-specification.md#component-flow)
shows the ingestion and retrieval path, including optional local components.
The [joint agent architecture](docs/agent-architecture.md) explains how one
agent routes work through Knowledge and Gateway without merging their authority.

## Quick start from a checkout

```bash
uv sync --frozen --extra dev
uv run helix-mcp-knowledge init-db
uv run helix-mcp-knowledge status
uv run helix-mcp-knowledge serve
```

The repository and packaged configurations start with no product or private project
selected, so this sequence downloads no documentation. Use `configure` or the local
dashboard when you are ready to select an authorized product and version.

For a runnable, copyright-safe first answer, use the
[fictional project evidence case](https://github.com/hvolckaert/helix-mcp-knowledge/blob/main/docs/knowledge-source-grounded-answer-case.md).
It shows a lexical MCP search, section expansion, a source-and-version citation,
and an explicit insufficient-evidence answer without downloading BMC content.

For a result-first walkthrough, run the
[CMDB reconciliation evidence demo](docs/cmdb-reconciliation-demo.md). It provides an
exact agent prompt, MCP calls, evidence matrix, acceptance checklist, and recording
outline without redistributing BMC content.

For the first complete two-server workflow, use the
[integrated CMDB data-quality case](docs/integrated-cmdb-data-quality-case.md). Knowledge
establishes versioned documentary expectations, Gateway prepares a bounded synthetic DEV
read, and the agent waits for explicit approval in a later turn before executing it.

The
[integrated controlled-update case](docs/integrated-controlled-update-case.md) adds one
governed synthetic DEV write. Knowledge establishes the official and project authority
chain; Gateway binds the exact proposal to a later approval, checks current state, applies
once, and verifies the result.
For a shorter, sanitized account of the validation and its evidence boundaries, see the
[two-server controlled-update case study](https://github.com/hvolckaert/helix-mcp-knowledge/blob/main/docs/agent-evidence-to-controlled-dev-update-case.md).

## Resource planning

The base server requires Python 3.12 or later with virtual-environment support
on a supported WSL/Linux or native Windows host. OpenClaw is optional; HTTPS is
needed when selected official documentation is synchronized. A new installation
selects no products, downloads no documents or models, and uses SQLite/FTS5
lexical retrieval without Torch. No numeric minimum RAM or disk requirement for
the base server has been validated across platforms and corpus sizes; source
files, downloaded pages, and indexes grow with the selected corpus.

| Mode | Disk planning | RAM and CPU planning | Basis and limits |
| --- | --- | --- | --- |
| Base lexical search | No fixed published figure; SQLite/FTS5 and cached sources grow with the corpus. | No validated numeric minimum; no resident model service. | Required path. Start with no product selected, then index a bounded corpus before expanding it. |
| Optional project-PDF OCR | Approximately 500 MiB estimated installation allowance. | CPU and memory are used only while scanned project PDF pages are indexed; no permanent OCR service. | Estimate shown before installation; not a measured retained footprint. Official BMC HTML and text-extractable PDFs do not use OCR. |
| Optional semantic search | Approximately 5 GiB estimated first-install allowance **plus** corpus-dependent vector storage; retained size is not fixed. | Plan for approximately 4 GiB of RAM while enabled; vector creation can temporarily use more. | Dashboard planning estimates, not measured minima or installation caps. BGE-M3 and local Qdrant are absent from the base install. |
| Optional result reranking | Approximately 5 GiB estimated setup allowance; a WSL reference installation retained about 3.3 GB. | WSL reference: about 1.8 GB of RAM while loaded and roughly 10–16 seconds per CPU search with 10 candidates. | Setup allowance versus measured WSL reference; native Windows and other CPUs may differ. No vectors are created. |

Optional components run locally rather than calling a hosted inference API; the
costs are download time, disk, memory, and CPU latency, not a server-side model
subscription. Setup allowances are planning figures, not guaranteed maxima.
Disabling a model service releases its active memory but retains downloaded
files until the separate removal action is used. On a resource-constrained host,
keep semantic search and reranking disabled and evaluate lexical retrieval first.

## Documentation paths

- **Install:** [client-neutral installation](docs/installation.md),
  [WSL/Linux](docs/openclaw-wsl-guide.md), [native Windows](docs/openclaw-windows-guide.md),
  and [MCP client integration](docs/mcp-client-integration.md).
- **Operate:** [dashboard, synchronization, projects, updates, and diagnostics](docs/operations.md).
- **Understand the design:** [architecture and component flow](docs/architecture/v1-specification.md).
- **Use both servers:** [agent architecture: Knowledge + Gateway](docs/agent-architecture.md).
- **Handle security:** [security policy and private vulnerability reporting](SECURITY.md).
- **Develop:** [development setup and evaluation](docs/development.md) and
  [contribution rules](CONTRIBUTING.md).

## Scope and limits

Knowledge retrieves documentary evidence; it does not inspect or change a live
Helix environment. Gateway provides live observation and governed actions as a
separate MCP server. Knowledge currently uses `stdio`, has no embedded LLM or
full RBAC, and confines the official crawler to selected product/version,
domain, path, and page-count limits. Semantic search, OCR, and reranking are
optional; SQLite/FTS5 remains the base retrieval path. See the
[architecture reference](docs/architecture/v1-specification.md#current-limits)
for the technical boundaries.

## Release status

Version 1.31.5 runs lexical SQLite/FTS5 retrieval by default and makes OCR,
semantic search, and reranking opt-in. See the [detailed release status](docs/release-status.md)
for the complete capability inventory.

## Supported official catalog

Every installation can enable any subset of products and retain more than one
version of the same product.

| Product | Selectable versions |
| --- | --- |
| BMC Helix Innovation Suite / AR System | 26.3, 26.2, 26.1 |
| BMC Helix CMDB | 26.3, 26.2, 26.1 |
| BMC Helix ITSM | 26.3, 26.2, 26.1 |
| BMC Helix Digital Workplace | 26.3, 26.2, 26.1 |
| BMC Helix Business Workflows | 26.3, 26.2, 26.1 |
| BMC Helix Discovery (SaaS) | `current` |

Discovery uses `current` because BMC maintains one continuously updated SaaS
documentation space rather than separate historical trees. The numeric product
spaces are version-bound and can coexist in the local index.

Product metadata is data-driven rather than hard-coded into the runtime. New
catalog revisions are published as immutable `catalog-v<revision>` prereleases,
verified with SHA-256, cached atomically, and exposed in the dashboard. See
[BMC catalog maintenance](docs/catalog-maintenance.md) for detection, validation,
review, publication, and new-product procedures.

## Distributed installation

A clean installation creates a per-user workspace with no products selected and
no documentation download. OpenClaw is optional; other MCP clients can register
the stable `stdio` launcher. Follow the [client-neutral installation guide](docs/installation.md),
the [WSL/Linux guide](docs/openclaw-wsl-guide.md), the [native Windows guide](docs/openclaw-windows-guide.md),
or the [MCP client integration guide](docs/mcp-client-integration.md).

## Local dashboard

The dashboard is a local administration interface for source selection, project
registration, synchronization, optional components, and update review. It is
not an agent-callable MCP tool. See [operations](docs/operations.md#local-dashboard).

## Server updates

Managed updates are verified and activated transactionally; installation requires
local administrator confirmation and is not exposed through MCP. See
[operations](docs/operations.md#server-updates).

## Development setup

Use the [development guide](docs/development.md#development-setup) for checkout
commands, optional local components, and retrieval evaluation. Follow
[CONTRIBUTING.md](CONTRIBUTING.md) for publication-safe examples and quality checks.

## Ingestion

Official and project sources are ingested only from their authorized roots, with
structural chunking and provenance retained in SQLite. Scanned project PDFs can
use optional OCR. See [operations](docs/operations.md#ingestion) and the
[architecture reference](docs/architecture/v1-specification.md#ingestion).

## Automatic official synchronization

Official synchronization is bounded to selected product/version sources and
continues without blocking the first MCP startup. See
[operations](docs/operations.md#automatic-official-synchronization).

## MCP tools

| Tool | What it returns | Scope and access | Effect |
| --- | --- | --- | --- |
| `search_docs` | Ranked documentary evidence with source and version provenance. | Official BMC documentation, the effective or explicitly requested project, or both via `all_relevant`; never unrelated projects. Supports product, version, document-type, and result-count filters. | Local read-only search. |
| `get_section` | A selected active chunk and bounded adjacent chunks from its document. | Official evidence or the effective or explicitly requested project; a chunk from another project is rejected. | Local read-only retrieval. |
| `list_products` | Configured or indexed BMC products and their readiness flags. | Catalog and index metadata; no document text. | Local read-only lookup. |
| `list_versions` | Configured or indexed versions for one BMC product. | Catalog and index metadata for the requested product; no document text. | Local read-only lookup. |
| `list_projects` | Registered project IDs, names, status, and configured product versions. | Project metadata across registered projects, optionally including archived projects; no document content. | Local read-only lookup. |
| `get_active_project` | The project selected in this `stdio` server instance. | Process-local selection only; it does not report the configured default project. | Local read-only lookup. |
| `set_active_project` | The newly selected project, or no project when passed `null`. | A registered project in this `stdio` instance; `null` overrides a configured default and returns subsequent unqualified searches to official-only scope. | Changes process-local context only; no corpus or Helix write. |
| `get_update_status` | Cached release status, or a fresh status when `refresh=true`. | Public GitHub release metadata when refreshed; no corpus or private-project content. | Read-only status check; refresh may contact GitHub but never installs an update. |
| `get_sync_status` | Sanitized official index and synchronization readiness, plus bounded project status. | Official corpus and only the active or explicitly requested project; no raw errors, paths, or document text. | Local read-only status check; does not start synchronization. |

Knowledge has no MCP tool for writing to a live Helix environment. Its MCP tools
also do not administer the source corpus, start synchronization, or install a
release; those operations belong to separate local administration workflows.

`list_products` and `list_versions` combine configured selections with content
that already has queryable evidence. `list_products` distinguishes `configured`
from `indexed`; `indexed` remains false during the first synchronization and
becomes true when active evidence exists.

`get_sync_status` summarizes the official corpus, selected products, indexed
totals, next run, elapsed time, safe progress aggregates, cancellation state, and
non-fatal notice/error counts. It exposes only the active or explicitly requested
project and never returns raw errors, source paths, owner IDs, or process IDs.

## MCP acceptance test

The optional `stdio` E2E suite uses synthetic, temporary evidence and never
modifies production data. See the [development guide](docs/development.md#mcp-acceptance-test).

## Installation diagnostics

Use the documented smoke checks and troubleshooting steps in
[operations](docs/operations.md#installation-diagnostics).

## Local registration in Codex

Register the stable launcher, then restart the client to load the MCP server.
See the [installation guide](docs/installation.md#local-registration-in-codex)
and [MCP client integration guide](docs/mcp-client-integration.md#codex).

## Active project and MCP v2

V1 uses `stdio`; each client has its own process-local active-project context.
An explicit `project_id` still takes precedence. See
[operations](docs/operations.md#active-project-and-mcp-v2) and the
[architecture reference](docs/architecture/v1-specification.md#isolation).

## Configuring projects

Private projects are registered locally and never included in a generic
release. Queries cannot cross into unrelated projects. See
[operations](docs/operations.md#configuring-projects).

## Storage design

SQLite holds canonical text, chunks, metadata, and FTS5; optional Qdrant holds
vectors and filter metadata only. See the
[architecture reference](docs/architecture/v1-specification.md#persistence-and-retrieval).

## Contact and support

- Use [GitHub Issues](https://github.com/hvolckaert/helix-mcp-knowledge/issues)
  for sanitized bug reports, feature proposals, and support requests.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing a significant change.
- Report security issues through the private process in [SECURITY.md](SECURITY.md).

Never include credentials, private endpoints, organization or customer names,
private document titles, paths or content, database copies, indexed chunks, or
raw diagnostics in a public issue.

## License, third-party content, and trademarks

Original project code and repository-authored documentation are available under
the [MIT License](LICENSE).

The MIT License does not apply to third-party material retrieved, downloaded,
cached, indexed, or supplied to an installation. BMC documentation and software
remain governed by BMC's applicable terms, and private project documents remain
governed by their respective owners and agreements. This repository, its wheel,
and its releases do not include downloaded BMC documentation, private project
documents, or derived indexes. Users are responsible for ensuring that they are
authorized to access and process every configured source.

This is an independent project. It is not affiliated with, sponsored by, or
endorsed by BMC Software, Inc. BMC, BMC Helix, and related product names are
trademarks of their respective owners.

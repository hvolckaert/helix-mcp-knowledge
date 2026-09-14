# helix-mcp-knowledge — V1 technical reference

This document summarizes the architectural decisions that govern the
implementation. It does not belong under `data/sources`: it describes the
server and must never be retrieved as official BMC evidence or private-project
knowledge.

## Responsibility

`helix-mcp-knowledge` returns documentary evidence. It does not inspect the live
state of a BMC Helix environment; that responsibility belongs to
`helix-mcp-gateway`.

Supported documentary scopes are:

- official BMC documentation;
- documentation from the effective project.

`all_relevant` combines those two scopes. It never expands a search to every
registered project.

## Persistence and retrieval

- SQLite is the source of truth for documents, chunks, and metadata.
- FTS5/BM25 provides lexical retrieval. Recognizably Spanish queries receive a bounded,
  deterministic expansion into English Helix terminology before FTS execution; original
  terms are retained and other query languages are unchanged.
- A dashboard-managed isolated component provides optional BGE-M3 dense embeddings.
- One token-protected loopback service owns the model and persistent local Qdrant
  storage across MCP and synchronization processes.
- Qdrant stores vectors and filter metadata, but not canonical text.
- Reciprocal Rank Fusion combines lexical and semantic candidates.
- Results are revalidated against SQLite before they are returned.
- Exact technical-term matching adds transparent priority.
- Final result selection prefers one ranked chunk per document before backfilling
  additional distinct sections and repeated-section chunks.
- An optional local reranker evaluates 10 SQLite-authorized candidates by default
  (hard maximum 32) after fusion and before diversification. Its rank is blended 50/50
  with the baseline before exact-match protection and diversification. It creates no
  persistent search index and remains disabled and absent from the base installation.

The contract does not publish a synthetic confidence score. It exposes rank,
provenance, and the retrieval modes that matched.

## Isolation

The project resolution order is:

1. an explicit `project_id` in the request;
2. the active project for the `stdio` instance;
3. the configured `default_project`;
4. no project, which means official BMC documentation only.

Documents from other projects are excluded in both FTS5 and Qdrant and are
filtered again when SQLite materializes the result.

## Ingestion

Local ingestion supports PDF, DOCX, Markdown, HTML, and plain text. Project
files must reside under the directory declared by the project; official files
must reside under `data/sources/bmc/official`.

The pipeline is:

1. validate path, extension, size, and scope;
2. calculate the file fingerprint;
3. parse and normalize content;
4. generate structural chunks;
5. prepare embeddings and vector points when enabled;
6. replace the document, relationships, chunks, and FTS5 entries in one
   transaction;
7. retire the previous vector points.

If SQLite fails after the vector upsert, the new points are removed. An
unchanged fingerprint produces an `unchanged` result.

Encrypted PDFs are rejected. PDFs without sufficient native text can use the
optional project-only OCR component when it is installed and enabled; otherwise
they are rejected with an actionable message. Native extraction remains the
first pass, and official BMC HTML never enters the OCR pipeline.

## Public MCP tools

- `search_docs`
- `get_section`
- `list_products`
- `list_versions`
- `list_projects`
- `get_active_project`
- `set_active_project`
- `get_update_status`
- `get_sync_status`

Administration and ingestion are CLI operations rather than MCP tools.
`get_update_status` is a strict read-only exception: it exposes the release
cache or refreshes it but never installs a version. `list_versions` returns the
union of configured and indexed official versions, with `indexed` indicating
whether queryable evidence exists. `list_products` applies the same union and
uses `configured` and `indexed` to distinguish selection from evidence.

`get_sync_status` returns a read-only summary of the official index and the
effective project. It omits error messages, paths, owners, and processes. No
private project details are returned when no project is selected.

## Local administration

The dashboard is an administrative HTTP process separate from MCP transport. It
listens only on `127.0.0.1`, uses an ephemeral anti-forgery token, and shares the
CLI validation services. It selects products and versions, configures scheduling,
creates and removes project registrations, manages their document roots and
documentation versions, provides an authenticated server-side folder navigator,
and requests detached synchronization. Project roots
inside the managed sources tree require no extra authorization. External roots
are allowlisted individually in the main configuration. Removing a project purges
its derived index and archives its YAML registration while preserving all source
documents.
It also checks and installs stable releases through a detached transactional
worker. Managed installations create separate stable MCP and dashboard launchers.
The worker shuts down only the local dashboard, preserves rollback state,
switches both launchers in one transaction, restarts the managed dashboard, and
accepts the target runtime only after its health endpoint reports the expected
version. If OpenClaw is the managed integration, it
also switches and probes its definition and restarts the Gateway. The MCP server
continues to use `stdio` and exposes no administrative write tools to the agent.

WSL/Linux installs `helix-mcp-knowledge-dashboard.service` under the current
user's systemd manager with automatic startup and `Restart=on-failure`. Native
Windows registers the packaged supervisor under the current user's `HKCU` Run
key; the supervisor implements the same crash-restart behavior without Task
Scheduler, elevation, or a Windows service. A detached supervisor is the Linux
fallback when `systemd --user` is unavailable. The dashboard reports the selected
manager, registration, active state, port, and restart policy.

The native installers default to automatic client detection. OpenClaw is the
recommended integration and is registered when available, but it is not a
runtime dependency. Without it, installation completes in standalone mode. The
dashboard reports only whether OpenClaw is connected. Claude Code, Codex, and
generic `stdio` clients are documented separately and point to the workspace
`bin` launcher rather than a version-specific virtual environment.

The release contains the supported official-source catalog. The user workspace
contains its effective copy and the user's selection. Opening the dashboard or
running `configure` adds new entries bundled with a server release without
modifying existing local definitions with matching identifiers. A
checksum-verified cached catalog whose positive revision is the same or newer is
authoritative for same-identifier corrections; it still does not alter the
administrator's product selection.

The catalog supports six canonical products. Innovation Suite/AR System, CMDB,
ITSM, Digital Workplace, and Business Workflows expose 26.1, 26.2, and 26.3.
Discovery SaaS uses the logical `current` version because BMC publishes one
rolling corpus; the server does not duplicate it under numeric labels that would
imply immutable historical snapshots.

A clean installation has no selected products and performs no official download.
Registration and all read-only catalog tools remain available in that state. The
installer opens the local dashboard for a new empty workspace unless the
administrator requests headless mode. The first-run view explains product and
version selection, expected indexing duration, and asks for confirmation before
the initial download. Enabling a product preselects the highest available catalog
version; no numeric release is hard-coded as a global default. Command-line
installers can still receive explicit product/version pairs for unattended
deployment.

## Documentation automation

Local project documents are reconciled from their manifests at startup and when
the filesystem changes. The dashboard also persists per-project immediate-sync
requests for the elected coordinator, exposes the latest project result and index
counts, and previews files without reading their content in its folder selector.
Official documentation is filtered by an explicit set
of product/version pairs and updated in the background after the configured
interval. Both processes use persistent SQLite leases to elect one leader among
concurrent `stdio` processes.

Unselected official data is physically removed by default only after every newly
selected pair has indexed evidence and the administrator has confirmed the
dashboard preview. Cleanup deletes official SQLite and FTS rows, optional vectors,
downloaded HTML, and obsolete synchronization state before compacting SQLite. An
empty selection is a valid cleanup-only run. Project documents and their source
files are outside this operation. Existing installations preserve their explicit
retention preference until it is changed.

A complete collection crawl also retires pages that have disappeared from BMC
navigation: the document is marked missing, its chunks are removed from SQLite
and FTS, and its cached HTML is deleted. Vector deletion is journaled
and retried after transient semantic-service failures. A bounded or cancelled
crawl never performs this retirement.

Release checks also run within the MCP lifecycle and use another SQLite lease.
They persist the latest state, rate-limit GitHub queries, and retain failures in
a controlled form. Installation remains an explicit administrative action from
the dashboard or CLI. A short non-blocking delay prevents short-lived catalog
probes from starting an external process that cannot finish.

The first MCP startup is never blocked by an official download. Configuration
and next-run state are available through the CLI; failures are persisted and
retried. Versions never advance implicitly: they must exist in the official
catalog and be selected by an administrator.

## Independently distributed BMC catalog

The effective official catalog merges the local workspace manifest and the
newest checksum- and provenance-verified cached catalog release. Public release
metadata, assets, and attestation bundles are retrieved anonymously; the private
pinned GitHub CLI managed in the workspace verifies each bundle locally. Schema
version 2 declares
canonical product metadata as data, so a new generic HTML product or version
does not require a runtime change. A cached positive revision that is the same
or newer can correct entries with the same identifier; older revisions cannot
override local definitions. A failed download or validation leaves the previous
effective catalog untouched.

Catalog releases use immutable `catalog-v<revision>` prerelease tags and do not
participate in stable runtime update detection. A SQLite lease elects one catalog
checker across concurrent `stdio` processes. New catalog entries remain
unselected until an administrator changes `official_docs.products`.

Repository automation probes only the next release of curated product URL
templates. It verifies robots policy and navigation boundaries, runs the
production parser and chunker against a bounded sample, and tests an in-memory
FTS5 index. Passing candidates become a review-only pull request. Publication is
a separate manual, commit-pinned workflow.

## Managed storage retention

Updates, official and project synchronization, and retention share one
cross-platform advisory lock. Cleanup runs only after successful activation or
during a normal MCP/dashboard start when no maintenance operation owns that
lock. The default policy retains the active runtime, one previous
runtime, the newest successful backup, failed-update backups for 14 days, and
only the active version's downloaded wheel. Ingestion diagnostics expire after
30 days and operational logs are capped at 10 MB. Manual, legacy, malformed,
linked, or otherwise unclassified entries are protected rather than guessed.

Retention first enumerates and validates every candidate below its exact managed
root. Recursive deletion never follows symbolic links or Windows junctions. A
failure stops the remaining cleanup and is reported without invalidating an
already activated release.

## Current limits

- The supported transport is `stdio`.
- Semantic retrieval is optional, absent from a clean installation, and disabled
  by default. Its managed model, dependencies, and vectors can be removed without
  affecting lexical search or source documents.
- The official crawler is bounded by product, version, domain, path prefix, and
  maximum page count; it is not a general web crawler.
- The server does not include full RBAC, OCR for official sources or non-PDF
  formats, GraphRAG, or an embedded LLM.

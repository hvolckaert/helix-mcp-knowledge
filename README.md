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
2. **Gateway** observes the authorised live environment and exposes governed
   operations under its own policy and approval controls.
3. The agent compares documentation with live state, distinguishes facts from
   inference, and acts only through the capabilities it has been granted.

Knowledge deliberately has no tool for changing a live Helix system. Its nine MCP
tools search and expand local evidence, expose index readiness, and select only the
process-local project context. See [MCP tools](#mcp-tools) for the complete contract.

## Quick start from a checkout

```bash
uv sync --frozen --extra dev
uv run helix-mcp-knowledge init-db
uv run helix-mcp-knowledge status
uv run helix-mcp-knowledge serve
```

The repository and packaged configurations start with no product or private project
selected, so this sequence downloads no documentation. Use `configure` or the local
dashboard when you are ready to select an authorised product and version.

## Release status

Version 1.29.0 provides:

- validated YAML configuration and Pydantic domain models;
- project registration and active-project resolution;
- SQLite as the source of truth, with FTS5 lexical search;
- structural parsers for PDF, DOCX, HTML, Markdown, and plain text;
- heading-aware chunking with block types, limits, overlap, and provenance;
- idempotent ingestion and atomic replacement of documents, chunks, and FTS5 data;
- durable cleanup of superseded project vectors, including retry after backend failures;
- optional, dashboard-managed BGE-M3 and persistent local Qdrant retrieval;
- hybrid BM25/semantic ranking through Reciprocal Rank Fusion and exact matching;
- optional local multilingual reranking of an authorized, bounded candidate set;
- checksum-locked, independently audited dependency sets for every optional component;
- explicit degraded semantic status with automatic lexical fallback when the local
  vector service is unavailable;
- section retrieval with adjacent context and result diversification;
- nine public MCP tools, including update and synchronization status;
- explicit MCP safety annotations for read-only, session-mutating, and network-aware tools;
- a local IntelliAgentia-branded administration dashboard;
- low-overhead dashboard state queries with indexed corpus summaries and adaptive
  polling that slows down while idle or hidden;
- a two-tab dashboard separating documentation operations from settings, with
  full-width per-project selectors for every supported product and version and
  a persistent Gateway-style save bar;
- a shared IntelliAgentia dashboard contract with consistent update wording,
  unsaved-change indicators, review-before-save behavior, and runtime status language;
- dashboard-managed creation, document-root editing, and safe removal of multiple
  isolated projects, including explicit external-folder authorization;
- product-free installation by default, with no download until the user makes a selection;
- product-free smoke testing that treats intentionally absent corpus checks as skipped;
- safe handling of official pages that redirect to interactive OAuth authentication;
- persisted per-product/version synchronization progress with phase, elapsed time,
  and last-activity reporting;
- confirmation-backed physical cleanup of deselected official products and versions,
  including SQLite/FTS, vectors, downloaded HTML, synchronization state, and compaction;
- guarded version switching that retains old evidence until every new selection has
  indexed successfully, plus cleanup-only operation when the last product is removed;
- cooperative cancellation between documents, preserving every completed index update;
- automatic first-run dashboard onboarding after a new product-free installation;
- a stable, client-neutral MCP launcher with optional automatic OpenClaw registration;
- a separate stable dashboard launcher switched in the same update transaction;
- persistent dashboard startup through `systemd --user` on WSL/Linux or an
  `HKCU` startup supervisor on native Windows, with no administrator rights;
- automatic dashboard restart after a crash and a detached fallback when a user
  systemd manager is unavailable;
- an update card that always reports the active runtime as `Updated to <version>`
  without exposing internal cleanup metrics;
- shared maintenance coordination for on-demand OCR installation, synchronization,
  configuration, cleanup, and runtime updates;
- automatic retention of strictly recognized pre-manifest backups while preserving
  unknown directories and the latest successful recovery point;
- bounded operational logs and automatic expiry of old ingestion diagnostics;
- draft-first, asset-verified publication of immutable BMC catalog releases;
- one background-service owner per managed workspace, with automatic MCP fallback
  whenever the dashboard is unavailable;
- native Windows OpenClaw registration through a `cmd.exe` stdio adapter compatible
  with current Node process-spawning rules;
- shell-free Windows OpenClaw invocation, including paths and arguments containing
  command metacharacters;
- a concise OpenClaw connection status in the dashboard, with other MCP client
  setup kept in the documentation;
- optional, dashboard-managed OCR for scanned project PDFs, installed only after
  explicit confirmation and disabled by default;
- transactional semantic setup, background vector creation, and reversible
  component removal without affecting SQLite or source documents;
- safe semantic-component removal across standard Windows and POSIX virtual environments;
- one-click, transactional server updates from the local dashboard;
- hash-locked runtime dependencies and immutable-release-ready draft publication;
- POSIX protection for managed workspace metadata and private project documents;
- queued dashboard updates that wait for active documentation synchronization;
- visual selection of products, multiple versions, synchronization settings, and
  per-project documentation versions;
- automatic preselection of the latest catalog version when a product is enabled;
- additive adoption of catalog entries bundled with server releases, plus
  authoritative corrections from newer checksum-verified catalog revisions,
  without changing the user's current selection;
- checksum-verified catalog updates distributed independently from the server
  runtime, without enabling or indexing new versions automatically;
- scheduled BMC version detection with bounded navigation, temporary FTS5
  indexing, and review-only pull requests;
- size-limited catalog probes with manual, authority-preserving redirect validation;
- a curated official catalog covering six BMC products;
- versions 26.1, 26.2, and 26.3 for five versioned product spaces, plus the rolling
  BMC Helix Discovery SaaS corpus;
- incremental synchronization of private project documentation;
- transactional updates from immutable GitHub releases with backup and rollback;
- automatic retention of the active and previous runtimes, the latest successful
  backup, recent failed-update backups, and only the active wheel download;
- unit, isolation, packaging, Linux, Windows, and always-on hermetic MCP `stdio`
  contract tests;
- release publication gated by the complete Linux, Windows, OCR, semantic, and reranker
  acceptance workflows, with a generic source distribution validated separately.

Reranking and semantic retrieval are optional and disabled by default, allowing
the lexical server to run without Torch or model downloads. Local project
synchronization runs automatically inside the MCP server lifecycle.

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

The package does not depend on a repository checkout or Windows Task Scheduler.
On first run it creates a per-user workspace containing a generic configuration,
the supported official catalog, and the index directories.

```bash
python -m pip install helix_mcp_knowledge-1.29.0-py3-none-any.whl
helix-mcp-knowledge install
```

`install` creates the workspace, initializes SQLite, and creates a stable,
self-configuring MCP launcher without requiring a particular client. A clean
installation intentionally selects no products and downloads nothing. The user
then chooses products and versions in the dashboard, which opens automatically
after a new empty installation.

Use `install-openclaw` to additionally register or update `helix_knowledge`
through OpenClaw's own CLI. It exposes all nine public tools with 30/60-second
timeouts, probes the server, and reloads the catalog without editing
`openclaw.json` directly. OpenClaw is the recommended automatic integration,
but it is not a package dependency.

The first probe starts no documentation worker while the selection is empty. If
products were preselected on the command line, it may start the official
download in a detached worker. The MCP server remains available while the index
is being built. Use `--no-probe` and
`--no-reload` to prepare the registration without connecting it,
`--no-dashboard` to suppress the browser during unattended installation, `--server-name`
to choose another name, and `--workspace PATH` to choose a workspace. Place the
global `--config PATH` option before the subcommand when reusing a configuration.

For clients other than OpenClaw, create the managed installation first:

```bash
helix-mcp-knowledge install
```

Register the stable launcher under the workspace `bin` directory so release
updates do not require editing client configuration. See the
[MCP client integration guide](docs/mcp-client-integration.md) for Claude Code,
Codex, native client interfaces, and generic `stdio` configuration.

The default workspace follows the host operating-system convention
(`~/.local/share/helix-mcp-knowledge` on Linux). `init --workspace PATH` selects
another location. Initialization never overwrites an existing configuration
unless `--force` is explicitly supplied, and it never deletes the database or
downloaded documents.

Configuration is resolved in this order: `--config`,
`HELIX_KNOWLEDGE_CONFIG`, `config/config.yaml` in a development checkout, then
the per-user workspace. The repository and distributed templates enable no
products and contain no private projects; each user selects products and versions
through `configure` or the dashboard.

Detailed operating guides cover release download, first synchronization,
acceptance, updates, and rollback:

- [OpenClaw on WSL or Linux](docs/openclaw-wsl-guide.md)
- [Native OpenClaw on Windows with PowerShell](docs/openclaw-windows-guide.md)
- [Claude Code, Codex, and other MCP clients](docs/mcp-client-integration.md)

### One-command installers

Windows:

```powershell
.\scripts\install-windows.ps1 -Version 1.29.0
```

WSL or Linux from a checkout:

```bash
./scripts/install-linux.sh --version 1.29.0
```

WSL or Linux directly from the GitHub release:

```bash
gh release download v1.29.0 \
  --repo hvolckaert/helix-mcp-knowledge \
  --pattern install-linux.sh \
  --output - | bash -s -- --version 1.29.0
```

Both installers create a working MCP server with no products selected. Their
default `auto` client mode registers OpenClaw when its command is available;
otherwise installation completes in client-neutral mode. Use `--client none`
on Linux or `-Client none` on Windows to explicitly skip client registration,
or `--client openclaw` / `-Client openclaw` to require it. The dashboard opens
and guides product and version selection. Pass `--no-dashboard`
on Linux or `-NoDashboard` on Windows to suppress the first browser window; the
local dashboard manager remains installed and enabled. Passing
`--product PRODUCT=VERSION` on Linux, or `-Product` on Windows, preselects
products for an unattended installation. Repeat the option to retain multiple
versions:

```bash
./scripts/install-linux.sh --version 1.29.0 \
  --product cmdb=26.3 \
  --product discovery=current
```

## Local dashboard

Day-to-day configuration does not require editing YAML:

```bash
helix-mcp-knowledge dashboard
```

The dashboard listens only on `127.0.0.1:8765`. Managed installation enables it
at user login and opens the local browser for first-run setup unless that window
was suppressed. WSL/Linux uses `helix-mcp-knowledge-dashboard.service` under the
user systemd manager. Native Windows uses the current user's `HKCU` startup key
and the same packaged supervisor; it does not use Task Scheduler or require an
administrator. If `systemd --user` is unavailable, Linux falls back to a detached
supervisor for the current session. The dashboard allows an administrator to:

- enable any product published in the verified catalog;
- select one or more available versions per product;
- preselect the latest available version when a product is first enabled;
- enable the initial load and automatic synchronization;
- choose the update interval and whether unselected official documentation is kept;
- create multiple isolated projects and choose a managed or external document folder
  with the built-in folder browser or an editable path; the browser previews files
  in the current folder and marks the formats that can be indexed;
- select products and documentation versions independently for each project;
- change a project's document folder after creation;
- inspect each project's detected files, indexed documents, chunks, latest result,
  and automatic synchronization state, then queue **Index documents now** without
  copying or uploading the source files;
- remove a project registration and its derived index without deleting source documents;
- save changes and launch a detached synchronization;
- inspect the current phase, per-product/version progress, elapsed time, last
  activity, indexed totals, non-fatal notices, errors, and the next run;
- cancel an active synchronization safely after its current document;
- see whether OpenClaw was connected automatically;
- inspect whether its own service is installed, enabled, active, and protected
  by automatic restart;
- check for a newer stable release and install it transactionally after explicit
  confirmation.
- install and enable the optional scanned-PDF OCR component after showing its
  estimated disk requirement, or disable its use without uninstalling it;
- install and enable optional BGE-M3/Qdrant semantic search, monitor vectorization,
  disable it without data loss, or explicitly remove its managed storage.
- install and enable optional multilingual result reranking without vectors or corpus
  reindexing, disable it to release memory, or remove its isolated component.

After the dashboard validates and persists an effective configuration change, an
OpenClaw-managed installation automatically runs `openclaw mcp reload`. The next
tool request therefore starts Knowledge with the new products, versions, projects,
and maintenance settings. A reload failure never rolls back a valid saved
configuration: the dashboard reports that manual reload is required. Standalone,
Codex, Claude Code, and other MCP clients must reconnect their Knowledge process.
Submitting content identical to the active configuration does not trigger a reload.

Dashboard updates work in both modes. OpenClaw registrations are reloaded,
probed, and the Gateway is restarted. Client-neutral installations switch only
the stable launcher; other clients pick up the new runtime in their next
session. Their setup instructions remain in the
[MCP client integration guide](docs/mcp-client-integration.md).

Write operations require an ephemeral anti-forgery token delivered only to the
local page. The server does not expose CORS, validates the request origin, limits
request size, and never binds to a network interface. Use `--no-browser` to skip
opening the browser or `--port PORT` to select another loopback port for a manual
process. For a managed installation, use installer option `--dashboard-port PORT`
on Linux or `-DashboardPort PORT` on Windows so startup and update metadata use
the same port.

When the dashboard opens, or `configure` runs, the runtime compares the user
catalog with the catalog bundled in the release. Packaged catalogs add new
collections and sources while preserving local entries with matching
identifiers. A separately published, checksum-verified catalog with the same or
a newer positive revision may also correct same-identifier definitions. Neither path
changes the user's product selection.

With no product selected, the page presents first-run guidance, synchronization
actions remain disabled, and nothing is downloaded. Before the first download,
the dashboard summarizes the selection, warns that indexing may take tens of
minutes, and requires confirmation. A three-product corpus took about 44 minutes
in reference testing; the MCP server remains usable while indexing continues.
During a run, the progress estimate can increase as the crawler discovers more
pages. **Cancel safely** finishes the current document, keeps completed work, and
stops before starting the next document.

## Server updates

Since version 1.9.0, an administrator can select **Check for updates** and then
**Install** in the dashboard. The page explains the operation and requests
confirmation. A detached worker verifies the release digest, creates backups,
runs the smoke test, switches the MCP and dashboard launchers together, restarts
the managed dashboard, and verifies its reported runtime version. For
OpenClaw-managed installations, it also switches and probes the MCP registration
and restarts the Gateway. The browser reconnects automatically. If the new
dashboard does not become healthy, rollback restores the previous launchers,
data, applicable client registration, and dashboard.

When documentation synchronization is active, a dashboard update is queued and
starts automatically after the worker reaches an idle state. The dashboard stays
available while it waits. Direct CLI updates remain fail-fast and ask the operator
to retry after synchronization finishes.

Since version 1.1.0, the installed runtime can inspect the latest stable private
release and prepare a safe update:

```bash
helix-mcp-knowledge --config /path/to/config/config.yaml update --dry-run
helix-mcp-knowledge --config /path/to/config/config.yaml update
```

The command requires an authenticated GitHub CLI. It verifies the published
SHA-256 digest, installs the wheel in `runtime/<version>/venv`, retains the
previous runtime, creates online backups of SQLite, YAML, and managed-launcher
state, runs the smoke test, and switches the stable command. When OpenClaw is
managed, its definition is also backed up and switched with `openclaw mcp set`.
A failed probe automatically restores the data, launcher, and registration.
CLI updates are rejected while documentation synchronization is running. `--resume`
repairs an incomplete new runtime and `--allow-downgrade` enables an explicit
rollback. When downgrading below the release that introduced managed reranking,
the updater stops its detached worker before switching runtimes; downloaded
component files are retained so a later upgrade can reuse them.

Synchronization, update activation, optional-component installation, configuration,
and managed storage cleanup share one cross-platform lock. This prevents maintenance
operations from overlapping and protects SQLite, YAML, launchers, and backups from
concurrent changes.

After a successful update, automatic storage retention keeps the active runtime,
one previous runtime, and the newest successful backup. Normal MCP and dashboard
starts enforce the same policy, so expired failed-update backups do not depend on
another release. Failed-update backups are retained for 14 days. Older download
directories are removed as soon as the new runtime has been validated. Historical
backups matching the exact managed `pre-v<version>[-manual]-<UTC timestamp>` pattern
participate in successful-backup retention; unrecognized directories remain protected.
Completed ingestion diagnostics are retained for 30 days and operational logs are
capped at 10 MB during safe startup maintenance.
Configure the policy under `updates.retention`:

```yaml
updates:
  retention:
    enabled: true
    previous_runtimes: 1
    successful_backups: 1
    failed_backup_days: 14
    diagnostic_days: 30
    max_log_size_mb: 10
```

The CLI update result records the removed artifacts and reclaimed bytes. The dashboard
shows only `Updated to <version>` unless cleanup needs attention. A cleanup error never
invalidates an already successful update and stops the remaining cleanup without
retrying with a stronger deletion method.

Since version 1.3.1, the updater reads the target runtime's tool contract before
changing the registration. If the current filter represents the previous full
public contract, new public tools are added automatically. A user-defined partial
filter is preserved.

The dashboard workflow restarts the OpenClaw Gateway automatically only when
OpenClaw is the managed integration. Other MCP clients use the new runtime when
their next session launches the stable command. Binary updates always require
explicit administrator confirmation; documentation synchronization is automatic
and independent.

The server also performs a non-destructive release check in the background at
most once every 24 hours. A SQLite lease prevents duplicate checks. The result is
cached and exposed through `get_update_status`; `refresh=true` forces a read-only
GitHub query. Missing GitHub CLI access produces a controlled error and never
stops the MCP server. Configure this behavior under `updates` in
`config/config.yaml`, including an absolute `gh_command` when the service `PATH`
does not contain GitHub CLI.

CI tests and packages the project on both Ubuntu and Windows Server, including
native `openclaw.cmd` integration on Windows.

## Development setup

Public interfaces, documentation, contribution metadata, and release notes use
English. See [Contributing](CONTRIBUTING.md) for the language and quality policy.

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
The [evaluation workspace](evaluation/README.md) includes a fictional corpus, a
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

## Ingestion

Files must remain under their authorized roots:

- official sources: `data/sources/bmc/official/`
- project sources: the directory declared in `config/projects/<id>.yaml`

Example project ingestion:

```bash
uv run helix-mcp-knowledge ingest \
  data/sources/projects/sample-project/docs/architecture.md \
  --scope project \
  --project-id sample-project \
  --document-type architecture \
  --product cmdb
```

When `--product` does not include a version, the version is inherited from the
project. It can also be supplied explicitly as `--product cmdb=26.1`.
Directories support `--recursive`.

Ingestion rejects disallowed extensions, symbolic links, oversized files, and
encrypted PDFs. A PDF without usable text is rejected while OCR is disabled.

### Optional OCR for scanned project PDFs

OCR is disabled and not installed in a new deployment. In the dashboard, select
**Enable OCR for scanned project PDFs** to install the isolated local component
after confirming an estimated 500 MiB disk requirement. Installation is
transactional: OCR becomes active only after its dependencies and recognition
self-test succeed. A failure leaves the feature disabled.

The parser always attempts native text extraction first and sends only pages with
insufficient text to OCR. OCR applies exclusively to project PDFs; official BMC
HTML ingestion is unaffected. Rendered page images are kept in memory and are
not retained. Clearing the checkbox disables OCR but preserves the installed
component, avoiding another download if it is enabled later.

The component uses RapidOCR with ONNX Runtime and pypdfium2 in an isolated runtime
under the managed workspace. It does not require Tesseract, administrator access,
or a continuously running OCR service. CPU and memory use occur only while a
scanned project PDF is being indexed.

### Automatic project synchronization

Each project can declare `documents.sources_manifest` in its YAML file. The
manifest lists files or globs relative to the documentation directory and assigns
a type, language, products, and optional title, classification, and metadata:

```yaml
schema_version: 1
project_id: sample-project
sources:
  - path: "integrations/*.docx"
    document_type: integration
    language: en
    products:
      cmdb: null
      arsystem: null
```

A `null` version inherits the project version for that product. Run a complete
reconciliation with:

```bash
uv run helix-mcp-knowledge sync-project sample-project
```

The command is idempotent and reindexes when either file content or manifest
metadata changes. It also marks missing or deselected manifest-managed documents
as `missing`, deactivates their chunks, and removes their vectors. Pruning occurs
only after every present file has been processed successfully. It never deletes
source files, manually ingested documents, or content from another project. Use
`--no-prune` to disable retirement and `--verbose-results` for per-document output.

When `ingestion.watch.enabled` is true, the MCP server performs an initial
reconciliation in the background and watches for later changes. It requires no
cron, systemd service, or Windows scheduled task. The watcher polls lightweight
filesystem metadata, applies a debounce interval, and verifies the final content
hash before reindexing. With the default two-second poll and five-second debounce,
a stable new file is normally detected and queued in about five to seven seconds.
The project card shows the transition from detected changes to active indexing and
the final result. **Index documents now** bypasses the debounce and persists the
request for the elected local coordinator; it does not upload project content.

Multiple `stdio` clients can create multiple server processes. Instances compete
for a temporary SQLite lease so that only one leader synchronizes, maintains a
heartbeat, and releases leadership on shutdown. Another instance takes over when
an unexpected termination causes the lease to expire. SQLite uses WAL mode to
keep queries available during updates.

Default watcher settings:

```yaml
watch:
  enabled: true
  debounce_seconds: 5
  poll_seconds: 2
  lease_seconds: 120
  retry_seconds: 30
```

`helix-mcp-knowledge status` returns the latest persisted result for every
project. If no client keeps a `stdio` process open, there is no resident process;
the next MCP startup performs any pending reconciliation immediately.

## Automatic official synchronization

Selectable pages and collections are declared in
`config/sources/bmc-official-26.1.yaml`. The historical file name is retained for
workspace compatibility even though the catalog is now multi-version. Merely
listing a source does not enable it: `official_docs.products` contains the user's
explicit selection.

Catalog availability and user selection are deliberately separate. The server
checks for a newer catalog every 24 hours, and the dashboard also provides a
manual **Refresh catalog** action. A catalog refresh can add selectable products
or versions but does not change `official_docs.products` or start a download.

This example selection enables daily updates for three products:

```yaml
official_docs:
  automatic_sync: true
  bootstrap_on_empty: true
  interval_hours: 24
  retain_unselected_versions: false
  products:
    arsystem: {versions: ["26.1"]}
    cmdb: {versions: ["26.1"]}
    itsm: {versions: ["26.1"]}
```

On an empty database, the server launches a one-shot worker from the installed
package and accepts MCP requests without waiting for crawling to finish. The
worker does not inherit MCP `stdio` and survives client process recycling. It
requires no external scheduler. Later starts compare the persisted schedule and
run an update whenever the configured interval has expired.

Official workers use an independent SQLite lease, ensuring that only one process
downloads documentation. `status` and `get_sync_status` report persisted phase,
per-product/version progress, elapsed time, last activity, result categories, and
the next run. Missing or empty official pages are non-fatal notices rather than
errors.
An abrupt termination can leave `status: running` until the lease expires; the
next worker then treats it as interrupted and retries. Detached-process errors are
written to `data/errors/official-sync-worker.log`.

If `bootstrap_on_empty` is disabled, an empty installation waits for the first
interval. New installations default to `retain_unselected_versions: false`. After
the selected products and versions have synchronized successfully, the worker
physically removes unselected official documents and chunks from SQLite and FTS,
their optional semantic vectors, downloaded HTML, and obsolete synchronization
state, then compacts SQLite. The dashboard previews the affected product versions,
document and chunk counts, and estimated storage, and requires confirmation before
saving a destructive selection change. Project documents are never included.
Selecting a removed version later downloads and indexes it again. Existing
installations keep their current retention setting until an administrator changes
it explicitly.

Public documentation is downloaded directly from BMC's official
`docs.helixops.ai` domain and currently requires no BMC credentials. Run a manual
update with:

```bash
uv run helix-mcp-knowledge sync
```

By default, `sync` updates explicit sources and discovers pages inside every
selected collection. The crawler honors `robots.txt`, accepts HTTPS only, remains
within the configured domain and product/version prefix, ignores editing, export,
download, and authentication routes, and delays requests. Each collection has a
per-run page limit. When reached, its frontier is persisted in SQLite so the next
run can continue. A collection is complete only when no URLs remain pending.

Limit a run to one source or collection, disable discovery, or show page-level
results with:

```bash
uv run helix-mcp-knowledge sync --source-id bmc-cmdb-26-1-home
uv run helix-mcp-knowledge sync --collection-id bmc-cmdb-26-1
uv run helix-mcp-knowledge sync --no-discovery
uv run helix-mcp-knowledge sync --verbose-results
```

The normal output is a compact summary of page states, indexed chunks, and errors.
`status` also reports the schema version and persisted collection-state count.
The dashboard can request cooperative cancellation; the worker checks that flag
between documents and during crawl delays, so it never interrupts an atomic
document replacement.

The downloader accepts only HTTPS and configured domains, revalidates redirects,
enforces a size limit, writes atomically, and uses ETag/Last-Modified when
available. Idempotency is based on normalized documentary content so dynamic web
elements do not cause unnecessary reindexing.

Restricted pages support basic authentication or a persistent browser session.
Secrets come from `BMC_DOCS_USERNAME` and `BMC_DOCS_PASSWORD` and are never stored
in YAML or SQLite. Install browser mode with `uv sync --extra browser-sync` and
`uv run playwright install chromium`. If BMC requires MFA or CAPTCHA, an
authorized non-interactive BMC method is required; the server never bypasses
those controls.

V1 uses `stdio`. Supply configuration through `--config` or
`HELIX_KNOWLEDGE_CONFIG`; normal installations discover the per-user workspace
automatically.

## MCP tools

- `search_docs`
- `get_section`
- `list_products`
- `list_versions`
- `list_projects`
- `get_active_project`
- `set_active_project`
- `get_update_status`
- `get_sync_status`

`list_products` and `list_versions` combine configured selections with content
that already has queryable evidence. `list_products` distinguishes `configured`
from `indexed`; `indexed` remains false during the first synchronization and
becomes true when active evidence exists.

`get_sync_status` summarizes the official corpus, selected products, indexed
totals, next run, elapsed time, safe progress aggregates, cancellation state, and
non-fatal notice/error counts. It exposes only the active or explicitly requested
project and never returns raw errors, source paths, owner IDs, or process IDs.

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

## Installation diagnostics

`smoke-test` runs a local, non-destructive acceptance check against the effective
configuration:

```bash
helix-mcp-knowledge smoke-test --require-project-isolation
```

The JSON output contains seven independent checks:

- SQLite integrity and foreign keys;
- active documents and chunks;
- selected product/version pairs against the official catalog;
- configured project paths;
- exact registration of the nine MCP tools;
- official search with valid scope, version, and canonical URL;
- temporary project activation, cleanup, and isolation.

The command does not start synchronization coordinators, modify configuration, or
reindex documents. It always restores the previous project context. Exit code `0`
means success and `1` means at least one failure. Project isolation is reported as
`skip` when no project evidence exists; `--require-project-isolation` turns that
case into a failure. A new product-free installation also skips index, catalog,
and official-search checks until the user selects a corpus; this is a valid
smoke-test result rather than a failure.

## Local registration in Codex

Codex CLI, the desktop app, and the extension share the MCP configuration. Use
the stable launcher created by the native installer:

```bash
codex mcp add helix_knowledge -- \
  /home/<user>/.local/share/helix-mcp-knowledge/bin/helix-mcp-knowledge-server
```

Verify the registration:

```bash
codex mcp get helix_knowledge
codex mcp list
```

After adding the server from an already open session, restart Codex and use
`/mcp` to confirm that `helix_knowledge` is connected. See the
[MCP client integration guide](docs/mcp-client-integration.md) for additional
clients and configuration methods.

`all_relevant` means official BMC documentation plus the effective project. It
never means every project. Without an effective project, it searches official
documentation only.

## Active project and MCP v2

The original specification associated the active project with an MCP session
identifier. MCP 2026-07-28 removed protocol sessions. In V1 `stdio`, every client
creates its own process and application instance, so active-project state remains
isolated to that instance rather than a module-level global. A future Streamable
HTTP transport must include `project_id` in each search or persist it against an
external authenticated identity.

## Configuring projects

Use **Project documentation > Add project** in the dashboard. Enter a name, accept
or edit the generated project ID, choose the document language, and select the
folder where its private source documents are stored. **Browse...** opens a local
folder navigator with shortcuts to managed storage, the user's home directory,
and mounted Windows drives when running under WSL. It displays folders plus the
files in the current location, identifies supported formats, and keeps files
non-selectable because the configured source is always a folder. The path remains
editable for advanced locations. The suggested location is
`data/sources/projects/<project-id>/docs`. An explicitly selected external folder
is recorded as an exact allowed root; project ingestion remains confined to that
folder. On WSL, a pasted `C:\...` path is translated to the corresponding mounted
`/mnt/<drive>/...` path when that drive is available.

The dashboard creates the project YAML, a managed catch-all source manifest, and
the folder when necessary, then queues its first scan. Choose the project's BMC
products and one version per product in its card, then save; changing the folder or
product mapping queues a fresh reconciliation. The card reports detected files,
indexed documents and chunks, errors, and completion time. Use **Index documents
now** whenever an immediate rescan is useful. Multiple projects remain isolated and must still
be selected explicitly with `set_active_project` or `project_id`. Removing a
project removes its registration and derived SQLite index but preserves its source
folder and files. A recoverable copy of its YAML configuration is retained under
`config/projects/.removed/`.

For advanced source-specific document types, language rules, or product mappings,
copy and adapt `config/projects/example.yaml.example` and
`config/projects/example.sources.yaml.example`. The standard distribution has no
default project.

Private project configuration and source documents are deployment-specific and
are never included in the generic distribution. Each installation creates its
projects in the dashboard or supplies their YAML and documents through an
authorized private channel. Queries with `source_scope=project` are restricted
to the explicitly selected project; `all_relevant` combines only that project
with official documentation.

## Storage design

SQLite stores projects, products, versions, documents, chunks, sources, and
synchronization state. `chunks_fts` is the lexical index. Qdrant stores only
vectors and filter metadata; canonical text always comes from SQLite.

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

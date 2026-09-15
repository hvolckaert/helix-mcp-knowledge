# Operating Helix MCP Knowledge

Administration remains local and separate from the nine agent-callable MCP
tools. Use this guide for the dashboard, updates, ingestion, synchronization,
project selection, and diagnostics.

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
[MCP client integration guide](mcp-client-integration.md).

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

Since version 1.1.0, the installed runtime can inspect the latest stable public
release and prepare a safe update:

```bash
helix-mcp-knowledge --config /path/to/config/config.yaml update --dry-run
helix-mcp-knowledge --config /path/to/config/config.yaml update
```

The command installs or reuses the pinned GitHub CLI managed inside the
Knowledge workspace. It retrieves release metadata, assets, and attestation
bundles from anonymous public endpoints, verifies each published SHA-256 digest
and signed provenance locally, installs the wheel in `runtime/<version>/venv`,
retains the previous runtime, creates online backups of SQLite, YAML, and
managed-launcher state, runs the smoke test, and switches the stable command.
GitHub token, host, and repository environment overrides are not forwarded.
When OpenClaw is managed, its definition is also backed up and switched with
`openclaw mcp set`. A failed probe automatically restores the data, launcher,
and registration.
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
GitHub query. Public release checks do not invoke `gh`; update installation and
catalog provenance verification use the private managed copy. Any check failure
produces a controlled error and never stops the MCP server. Configure this
behavior under `updates` in `config/config.yaml`.

CI tests and packages the project on both Ubuntu and Windows Server, including
native `openclaw.cmd` integration on Windows.

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

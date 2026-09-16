# Release status

The detailed capabilities of the current version are retained here so the
repository README can stay focused on first use and evaluation.

## Release status

Version 1.31.6 provides:

- validated YAML configuration and Pydantic domain models;
- project registration and active-project resolution;
- SQLite as the source of truth, with FTS5 lexical search, bounded Spanish-to-English
  Helix terminology expansion, and targeted CMDB reconciliation-intent expansion;
- structural parsers for PDF, DOCX, HTML, Markdown, and plain text;
- heading-aware chunking with block types, limits, overlap, and provenance;
- idempotent ingestion and atomic replacement of documents, chunks, and FTS5 data;
- durable cleanup of superseded project vectors, including retry after backend failures;
- optional, dashboard-managed BGE-M3 and persistent local Qdrant retrieval;
- a corrected Semantic Search self-test using the same paired product/version payload
  as the production Qdrant filter;
- hybrid BM25/semantic ranking through Reciprocal Rank Fusion and exact matching;
- optional local multilingual reranking of an authorized, bounded candidate set;
- checksum-locked, independently audited dependency sets for every optional component;
- explicit degraded semantic status with automatic lexical fallback when the local
  vector service is unavailable;
- section retrieval with adjacent context and document-first result diversification;
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
- systemd-isolated dashboard update workers on Linux/WSL, with automatic recovery
  from interrupted or orphaned update state;
- hash-locked runtime dependencies and immutable-release-ready draft publication;
- POSIX protection for managed workspace metadata and private project documents;
- queued dashboard updates that wait for active documentation synchronization;
- visual selection of products, multiple versions, synchronization settings, and
  per-project documentation versions;
- decimal-aligned dashboard validation that accepts whole-hour synchronization intervals;
- automatic preselection of the latest catalog version when a product is enabled;
- additive adoption of catalog entries bundled with server releases, plus
  authoritative corrections from newer checksum-verified catalog revisions,
  without changing the user's current selection;
- checksum-verified catalog updates distributed independently from the server
  runtime, without enabling or indexing new versions automatically;
- anonymous public discovery and download of runtime and catalog releases, with
  local attestation verification and no GitHub token forwarding;
- Windows PowerShell 5.1-safe public installer output and UTF-8 attestation parsing;
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

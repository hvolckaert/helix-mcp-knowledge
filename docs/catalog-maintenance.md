# BMC catalog maintenance

Helix Knowledge distributes BMC product metadata and official documentation
roots as a revisioned catalog. Catalog releases are independent from server
releases: an installed runtime can discover a newer catalog without installing a
new wheel.

Catalog updates only make products and versions selectable. They never alter the
user's selection and never download or index a newly published version until an
administrator selects it in the dashboard or CLI.

## Runtime update channel

The canonical catalog remains
`config/sources/bmc-official-26.1.yaml`. The historical filename is preserved for
workspace compatibility. Schema version 2 adds:

- `catalog_revision`, a monotonically increasing integer;
- `products`, containing canonical IDs, display names, aliases, and active state;
- the existing bounded collections and priority sources.

Published catalogs use immutable prerelease tags named `catalog-v<revision>`.
Each release contains:

- `bmc-official-catalog.yaml`;
- `bmc-official-catalog.sha256`.

The server discovers releases and downloads both assets and their attestation
bundles through anonymous public GitHub endpoints. It verifies the published
asset digests, uses the installed but unauthenticated GitHub CLI to validate the
bundles locally against the catalog publication workflow, checks the catalog's
own SHA-256 file, validates the complete Pydantic schema, checks product aliases
and the URL domain allowlist, and only then atomically replaces its cached
catalog. `GH_TOKEN` and `GITHUB_TOKEN` are neither required nor forwarded. The
packaged and local catalogs remain a safe fallback when GitHub is unavailable.

The default cache is
`data/cache/official-catalog/bmc-official-catalog.yaml`. Multiple MCP processes
coordinate checks through a SQLite lease. Existing processes notice an adopted
cache revision during their periodic check; new processes load it immediately.
A cached positive revision that is the same as or newer than the workspace
catalog is authoritative for corrections to existing product, collection, and
source IDs. Older revisions cannot override local definitions, and new versions
remain unselected.

```yaml
catalog_updates:
  enabled: true
  interval_hours: 24
  retry_minutes: 30
  repository: hvolckaert/helix-mcp-knowledge
  release_prefix: catalog-v
  manifest_asset: bmc-official-catalog.yaml
  checksum_asset: bmc-official-catalog.sha256
  cache_path: data/cache/official-catalog/bmc-official-catalog.yaml
  gh_command: gh
  timeout_seconds: 30
```

## Automated version watch

`.github/workflows/catalog-watch.yml` runs every Monday and can also be started
manually. Its monitored URL templates and validation markers are defined in
`config/catalog-watch.yaml`.

For every curated, numerically versioned product, the monitor:

1. derives the next quarterly version from the newest catalog entry;
2. verifies `robots.txt` and probes the candidate BMC root;
3. rejects authentication, not-found, non-HTML, cross-domain, and cross-version
   navigation;
4. downloads at most 10 MB per page and crawls at most 25 pages inside the exact
   product/version prefix, without automatically following redirects;
5. parses and chunks those pages using the production HTML pipeline;
6. creates an in-memory SQLite FTS5 index and executes the product smoke query;
7. increments the catalog revision and prepares a pull request only when the
   candidate checks succeed.

An unavailable next version is an expected no-change result. A transport or
validation failure appears in the workflow summary and is not published.

The pull-request branch is stable and updated idempotently. The generated report
contains the candidate URL, sampled pages, indexed chunks, smoke-query hits, and
any warning. The workflow does not merge its own pull request.

Discovery is excluded from quarterly probing because BMC publishes its SaaS
documentation as the continuously updated `current` space.

## Controlled publication

After reviewing and merging a catalog pull request:

1. copy the exact full commit SHA at the tip of `main`;
2. run the **Publish BMC catalog** workflow manually;
3. enter that SHA as `approved_commit`;
4. enable `confirm_publication`;
5. approve the `catalog-production` environment when GitHub requests it.

The workflow refuses to publish from a different commit and refuses to replace
an existing immutable catalog revision. Where the repository plan supports
environment protection rules, configure required reviewers on
`catalog-production` as an additional gate. On plans that do not support them
for private repositories, manual dispatch, explicit confirmation, and the exact
reviewed commit remain mandatory.

Publishing a catalog does not require or create a server release. Stable server
update detection ignores catalog prereleases.

## Adding a completely new product

A product's first release is intentionally reviewed rather than inferred. Add:

1. its canonical product entry under `products`;
2. at least one bounded collection and home source;
3. a future-version template, required markers, and smoke query in
   `config/catalog-watch.yaml` when it uses numbered releases;
4. tests covering alias resolution, available versions, navigation boundaries,
   and temporary indexing.

After the initial product definition is published, the weekly monitor can propose
later versions without a server code change.

## Local validation

Run the normal repository checks plus the catalog-specific live probe:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python scripts/watch_bmc_catalog.py \
  --catalog config/sources/bmc-official-26.1.yaml \
  --watch-config config/catalog-watch.yaml \
  --report catalog-watch-report.md
```

The final command performs live, read-only BMC probes. Omit it when working
offline. Add `--write` only when intentionally preparing a catalog proposal.

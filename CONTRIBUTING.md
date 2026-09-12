# Contributing

## Language policy

English is the canonical language for every public surface of this project:

- the dashboard and other user-facing interfaces;
- README files, guides, architecture documents, and code examples;
- command help, diagnostics, source comments, tests, release notes, pull
  requests, and repository metadata.

Keep source-document titles and paths in their original language when changing
them would break provenance or references to externally managed files. Describe
those sources in English and label the retained text as an original title when
it appears in public documentation.

## Repository rules

- Do not include credentials, private endpoints, organization or customer names,
  private document titles, paths, content, indexed chunks, or database copies.
- Do not commit downloaded BMC pages, proprietary software, third-party
  documentation, screenshots, caches, indexes, or generated corpora.
- Use fictional, publication-safe documents and identifiers in examples and tests.
- Preserve explicit project selection, cross-project isolation, source provenance,
  and the distinction between documentary evidence and live Gateway observations.
- Keep network discovery bounded and read-only; catalog automation may propose a
  change but must not approve or publish its own proposal.
- Document any user-visible behavior or configuration migration.

Report vulnerabilities through the private process in
[SECURITY.md](SECURITY.md), never through a public issue.

Use clear international English and avoid locale-specific assumptions. The
dashboard uses `en-GB` for human-readable dates and numbers while protocol
values, timestamps, identifiers, and paths remain locale-independent.

## Quality checks

Run the complete local validation before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
uv run python scripts/verify_built_wheel.py
```

Pull-request titles, descriptions, commits, and release notes must also be in
English.

By submitting a contribution, you agree that it may be distributed under the
repository's MIT License. Do not submit material unless you have the right to
license it on those terms.

## Catalog contributions

BMC product metadata and documentation roots use the revisioned schema-2 catalog
in `config/sources/bmc-official-26.1.yaml`. Product additions belong in the
catalog rather than `ProductCatalog` source code. Increment `catalog_revision`
for every published catalog change and update `config/catalog-watch.yaml` when a
numbered product should participate in automated version detection.

The weekly catalog workflow may prepare a pull request after bounded live
navigation and temporary FTS5 indexing. It cannot merge or publish its own
proposal. Publication uses the manually dispatched, commit-pinned
**Publish BMC catalog** workflow, an explicit confirmation, and the
`catalog-production` environment. Required environment reviewers add another
gate when the repository plan supports them. Full procedures are in
[`docs/catalog-maintenance.md`](docs/catalog-maintenance.md).

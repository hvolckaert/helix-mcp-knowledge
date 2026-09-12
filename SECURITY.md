# Security policy

## Supported versions

Security fixes are provided for the latest published Helix MCP Knowledge
release. Managed installations can check their status from the dashboard or by
calling `get_update_status`.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the repository owner. Include a
minimal reproduction, the affected version and the expected impact. Do not put
credentials, private project documents, database copies or access tokens in an
issue, log excerpt or test fixture.

The project does not request BMC credentials through the dashboard. When an
authenticated documentation source is explicitly configured, credentials must
be supplied through the documented environment variables and are never stored
in the catalog or SQLite database.

## Release integrity

Server and catalog releases are immutable. A repeated server workflow verifies
that an existing release already contains every expected asset and never
replaces published files. Catalog downloads are accepted only after their
SHA-256 checksum has been verified.

Base-runtime, OCR, semantic-component, and reranker-component dependencies are
installed from version- and SHA-256-locked requirement sets. CI audits all four
sets for known vulnerabilities. PyTorch CPU wheels use a `+cpu` local-version suffix;
CI therefore also audits their identical upstream source versions through explicit
normalized inputs so the package-index skip cannot hide an advisory. On POSIX systems, managed configuration,
operational metadata, and project-document roots are restricted to the owning
user; explicitly configured external project folders keep the permissions chosen
by their owner.

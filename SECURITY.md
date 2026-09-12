# Security policy

## Supported versions

| Version | Security support |
| --- | --- |
| 1.29.x | Yes |
| 1.27.x and earlier | No |

Security fixes target the latest supported release line. Managed installations
can check their status from the dashboard or by calling `get_update_status`.

## Responsible disclosure

Do not publish vulnerabilities, credentials, private endpoints, project
documents, indexed chunks, database copies, or exploitation details in a public
issue.

Use **Report a vulnerability** in the repository's Security tab. If private
reporting is unavailable, open an issue without sensitive details and ask the
maintainer for a private channel.

When possible, include:

- affected version and commit;
- affected component and a local reproduction environment;
- expected impact;
- minimal steps using fictional data;
- any known mitigation.

Do not test a vulnerability against BMC infrastructure, a third-party service,
or a private documentation source without explicit authorization.

## Credential and local-data handling

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

## Scope

This policy covers the original server code, configuration templates, installer,
dashboard, release workflows, and repository-authored documentation. BMC
products and documentation, third-party services, optional models, and
user-supplied project documents retain their own support channels, licences, and
security policies.

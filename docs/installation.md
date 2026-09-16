# Installing Helix MCP Knowledge

This is the client-neutral installation path. Use the platform-specific guides
for full WSL/Linux or native Windows verification, update, and rollback steps.

## Distributed installation

The package does not depend on a repository checkout or Windows Task Scheduler.
On first run it creates a per-user workspace containing a generic configuration,
the supported official catalog, and the index directories.

```bash
python -m pip install helix_mcp_knowledge-1.31.7-py3-none-any.whl
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
[MCP client integration guide](mcp-client-integration.md) for Claude Code,
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

- [OpenClaw on WSL or Linux](openclaw-wsl-guide.md)
- [Native OpenClaw on Windows with PowerShell](openclaw-windows-guide.md)
- [Claude Code, Codex, and other MCP clients](mcp-client-integration.md)

### One-command installers

GitHub CLI is not a system prerequisite. Guided setup installs a private,
pinned copy under the Knowledge workspace for release provenance verification,
without `sudo`, `gh auth login`, or changes to the user's `PATH`. Release
metadata, assets, and attestation bundles are retrieved through anonymous
public endpoints and verified locally.

Windows:

```powershell
.\scripts\install-windows.ps1 -Version 1.31.7
```

WSL or Linux from a checkout:

```bash
./scripts/install-linux.sh --version 1.31.7
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
./scripts/install-linux.sh --version 1.31.7 \
  --product cmdb=26.3 \
  --product discovery=current
```

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
[MCP client integration guide](mcp-client-integration.md) for additional
clients and configuration methods.

`all_relevant` means official BMC documentation plus the effective project. It
never means every project. Without an effective project, it searches official
documentation only.

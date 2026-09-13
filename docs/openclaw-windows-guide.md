# Native Helix Knowledge installation on Windows

This guide installs `helix-mcp-knowledge` directly on Windows with PowerShell.
It does not require WSL, a repository checkout, or Windows Task Scheduler.

## 1. Support status

The distribution is validated on `windows-latest` with Python 3.12. CI runs the
complete suite, builds the wheel, installs the `.exe` entry points, creates a
clean database, and verifies product selection. It also tests the
`openclaw.cmd` wrapper used by native npm installations.

Automatic synchronization launches a detached Windows process. SQLite and the
workspace are stored in the current user's local application-data directory.
The dashboard is kept available by a packaged supervisor registered under the
current user's `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` key. It does
not install a Windows service and requires no administrator rights.

## 2. Requirements

- Windows 10, Windows 11, or an equivalent Windows Server release.
- PowerShell 7 recommended.
- Python 3.12 or later with the `py` launcher.
- OpenClaw is optional and provides the recommended automatic MCP integration.
- A recent GitHub CLI (`gh`) with `gh attestation verify`; authentication is not
  required for this public repository.
- HTTPS access to `docs.helixops.ai`.

Verify the environment:

```powershell
py -3.12 --version
gh --version
gh attestation verify --help
```

If OpenClaw is installed, also run `openclaw.cmd --version` and
`Get-Command openclaw.cmd`.

Use `openclaw.cmd`, not `openclaw.ps1`, so installation is independent of the
PowerShell script-execution policy.

## 3. Clean installation of v1.31.4

### Recommended automated installation

From a checkout of this version, the installer downloads the wheel through the
public release endpoint, verifies its GitHub SHA-256 and signed provenance,
creates an isolated runtime, and registers the server:

```powershell
.\scripts\install-windows.ps1 -Version 1.31.4
```

For controlled installations or tests, provide a local wheel:

```powershell
.\scripts\install-windows.ps1 `
  -Version 1.31.4 `
  -WheelPath C:\Temp\helix_mcp_knowledge-1.31.4-py3-none-any.whl `
  -RequirementsPath C:\Temp\runtime-requirements.txt
```

The locked requirements file is a release asset. Keep it beside a local wheel,
or pass `-RequirementsPath` explicitly as above. The installer verifies every
runtime dependency hash before it installs the wheel without dependency
resolution.

By default, the installer selects no products. It creates a usable MCP server
but downloads and indexes nothing until the user saves a selection in the
dashboard, which opens automatically after a new empty installation. When
OpenClaw is available, `-Client auto` registers it automatically; otherwise
installation completes without a client dependency. Use `-Client none` to skip
detection or `-Client openclaw` to require it. Use
`-NoDashboard` to suppress the first browser window. The dashboard startup
supervisor remains installed and enabled. Pass a list to `-Product` only for
an unattended installation:

```powershell
.\scripts\install-windows.ps1 `
  -Version 1.31.4 `
  -Product @('cmdb=26.3', 'discovery=current')
```

Use `-DashboardPort PORT` only when 8765 conflicts with another local service.

The registration uses `cmd.exe` as a narrow stdio adapter for the stable batch
launcher. Current OpenClaw releases do not spawn `.cmd` MCP commands directly;
the adapter keeps the configured command stable across server updates.

The initial catalog exposes six products. Innovation Suite/AR System, CMDB, ITSM,
Digital Workplace, and Business Workflows support 26.1, 26.2, and 26.3.
Discovery SaaS uses `current` because BMC publishes one continuously updated
documentation space.

The installer never replaces an existing runtime. If installation is
interrupted, fix the cause and run it again with `-Resume`.

### Manual installation

Open PowerShell as the same user that runs OpenClaw and define persistent
paths:

```powershell
$HelixVersion = "1.31.4"
$HelixHome = Join-Path $env:LOCALAPPDATA "helix-mcp-knowledge"
$HelixRuntime = Join-Path $HelixHome "runtime\$HelixVersion"
$HelixDownload = Join-Path $HelixHome "downloads\$HelixVersion"

New-Item -ItemType Directory -Force -Path $HelixRuntime, $HelixDownload | Out-Null
```

Download the official wheel and locked requirements from the public GitHub release:

```powershell
$HelixWheelName = "helix_mcp_knowledge-$HelixVersion-py3-none-any.whl"
Invoke-WebRequest `
  -Uri "https://github.com/hvolckaert/helix-mcp-knowledge/releases/download/v$HelixVersion/$HelixWheelName" `
  -OutFile (Join-Path $HelixDownload $HelixWheelName)
Invoke-WebRequest `
  -Uri "https://github.com/hvolckaert/helix-mcp-knowledge/releases/download/v$HelixVersion/runtime-requirements.txt" `
  -OutFile (Join-Path $HelixDownload "runtime-requirements.txt")
```

Create a versioned runtime and install the package:

```powershell
py -3.12 -m venv (Join-Path $HelixRuntime "venv")

$HelixPython = Join-Path $HelixRuntime "venv\Scripts\python.exe"
$HelixCli = Join-Path $HelixRuntime "venv\Scripts\helix-mcp-knowledge.exe"
$HelixServer = Join-Path $HelixRuntime "venv\Scripts\helix-mcp-knowledge-server.exe"
$HelixWheel = Join-Path $HelixDownload "helix_mcp_knowledge-$HelixVersion-py3-none-any.whl"
$HelixRequirements = Join-Path $HelixDownload "runtime-requirements.txt"

& $HelixPython -m pip install --require-hashes --requirement $HelixRequirements
& $HelixPython -m pip install --no-deps $HelixWheel
& $HelixPython -m pip check
Test-Path $HelixServer
```

The final command must return `True`.

Create the client-neutral managed installation without selecting products:

```powershell
& $HelixCli install `
  --workspace $HelixHome `
  --automatic-sync
```

This creates stable MCP and dashboard launchers under `$HelixHome\bin`, registers
the dashboard supervisor for the current Windows user, and starts it immediately.

The command creates the configuration, SQLite database, and stable `.cmd`
launcher under `$HelixHome\bin`. It opens the first-run dashboard when the
workspace is new and has no selected products. Add `--no-dashboard` for a
headless installation.

To register OpenClaw automatically, resolve `$OpenClawCommand` as shown above
and use `install-openclaw` instead:

```powershell
& $HelixCli install-openclaw `
  --workspace $HelixHome `
  --server-name helix_knowledge `
  --openclaw-command $OpenClawCommand
```

Registration probes all nine tools and reloads the catalog. It can take 60–90
seconds even when healthy.

No official worker starts while the selection is empty. After the user saves
products and versions, the first full synchronization runs in the background
and can take tens of minutes. As a reference, a WSL run for three 26.1 products indexed
3,014 documents and 19,184 chunks in about 44 minutes. Windows duration depends
on network, storage, and BMC content.

If the gateway was already active, restart it:

```powershell
openclaw.cmd gateway restart
openclaw.cmd gateway status
openclaw.cmd channels status
```

### Visual configuration

The dashboard starts automatically after the current user signs in. Open it to
change products, versions, synchronization frequency, and to create, remove, or
configure private projects and their document folders without editing YAML:

```powershell
& "$env:LOCALAPPDATA\helix-mcp-knowledge\runtime\1.31.4\venv\Scripts\helix-mcp-knowledge.exe" `
  --config "$env:LOCALAPPDATA\helix-mcp-knowledge\config\config.yaml" `
  dashboard
```

Open `http://127.0.0.1:8765/`. Enabling a product preselects its latest catalog
version; the user can replace it or add older versions. The dashboard validates
the selection and, before the first download, confirms the number of products and
versions and warns that indexing may take tens of minutes. When unselected
documentation is not retained, it also previews and confirms the official data
that will be physically removed. New selections are indexed before old data is
deleted; an empty selection starts a cleanup-only run. Project documents are never
included. Once confirmed, the dashboard writes the selection atomically and
launches a detached worker.
The Synchronization card shows the current phase, product/version, processed and
estimated items, elapsed time, last activity, non-fatal unavailable/empty pages,
and real errors. The estimate may grow while new links are discovered. **Cancel
safely** completes the current document and preserves all indexed work.
Under **Project documentation**, **Add project** suggests an isolated managed
folder but also accepts another local folder. Use **Browse...** to select it from
the local directory navigator without typing the path. The picker previews files
in the current folder and marks supported formats. Project cards show detected and
indexed totals and provide **Index documents now** for an immediate local rescan;
source files are never uploaded. Removing a project deletes only its registration
and derived search index; the source folder is preserved.
Keep the dashboard bound to loopback; do not publish this port on the network.

The **Dashboard service** card identifies the `Windows startup supervisor`, its
startup registration, and current status. The supervisor restarts the dashboard
after a failure. Disabling the first browser window does not remove this startup
registration. Neither Windows Task Scheduler nor an elevated Windows service is
used.

Optional semantic search is not part of the base installation. The dashboard can
install the pinned BGE-M3 model and managed local Qdrant component on demand,
then vectorize existing chunks in the background. Disabling retains the files;
use the separate removal action while disabled to recover their disk space. Plan
for approximately 4 GB of RAM while semantic search is enabled; vector creation
can temporarily use more.
Optional result reranking is also absent from the base installation and disabled
by default. Enabling it
downloads a pinned multilingual model into an isolated component and validates it
before use. It reranks only a bounded set of already-authorized search candidates,
creates no vectors, and needs no work after documentation synchronization. Disabling
stops the local service and retains its files; remove it separately while disabled.
The WSL reference footprint is about 3.3 GB on disk and 1.8 GB of RAM while loaded;
CPU-only latency was roughly 10–16 seconds with the default pool. Native Windows varies
by processor and still requires acceptance on real hardware.
Use **Check for updates** in the Server card to refresh release information. If
a newer stable release is available, **Install** requests confirmation and then
performs the verified update, backup, smoke test, stable launcher switch, and
dashboard reconnection automatically. For an OpenClaw-managed installation it
also switches and probes the MCP registration and restarts the Gateway.
If documentation synchronization is active, the dashboard queues the update and
starts it automatically when synchronization finishes. Direct CLI updates remain
fail-fast while a synchronization owns the database lease.

The **Runtime connections** card reports whether OpenClaw was connected automatically
and whether the dashboard itself is managed persistently.
After a validated dashboard save changes the active configuration, a managed
OpenClaw installation reloads its MCP runtimes automatically. The next tool
request starts Knowledge with the new settings. If reload fails, the saved
configuration is retained and the dashboard asks you to run
`openclaw.cmd mcp reload` manually. Other MCP clients must reconnect themselves;
an unchanged save triggers no reload.
Installation remains valid when OpenClaw is unavailable. Configuration for
Claude Code, Codex, and other clients is documented in the
[MCP client integration guide](mcp-client-integration.md).

## 4. Verification

Check the real catalog:

```powershell
openclaw.cmd mcp doctor helix_knowledge --probe
$Probe = openclaw.cmd mcp probe helix_knowledge --json | ConvertFrom-Json
$Probe.servers.helix_knowledge.launch
$Probe.tools
$Probe.diagnostics
```

The `launch` path must end in
`bin\helix-mcp-knowledge-server.cmd`; exactly nine tools must be present and
`diagnostics` must be empty.

Inspect persistent status:

```powershell
$HelixConfig = Join-Path $HelixHome "config\config.yaml"
& $HelixCli --config $HelixConfig status
```

A clean CLI status shows an empty `official_docs.products` map and no official
worker state; the dashboard and `get_sync_status` report `not_configured`.
After products are saved, the status may become `running`; the detached worker
continues after the probe exits. When the status is `ok`, run:

```powershell
& $HelixCli --config $HelixConfig smoke-test
```

Without private projects, `project_isolation` is expected to be `skip`; all
other checks should be `pass`.

## 5. Test from an agent

After selecting and indexing CMDB, start a new session and request real tool
calls:

```text
/new
Call helix_knowledge__list_projects,
helix_knowledge__list_versions with product=cmdb, and
helix_knowledge__get_sync_status. Then search for
"normalization reconciliation" in CMDB 26.1 with
source_scope=bmc_official. Cite the official URLs returned by the tools.
```

Before synchronization finishes, selected products and versions are reported
as `configured: true` and `indexed: false`. They change to `indexed: true` when
searchable evidence exists. `get_sync_status` reports safe phase, progress,
elapsed-time, cancellation, notice, and error fields without returning raw paths
or errors. Use `openclaw.cmd mcp probe`—not agent inference—to confirm the active
runtime path.

## 6. Synchronization and persistence

Synchronization does not use Windows Task Scheduler. With `automatic_sync`
enabled:

- an empty installation launches the worker on the first MCP startup;
- product selection is stored in `config\config.yaml`;
- a SQLite lease prevents duplicate workers;
- the task becomes due every 24 hours;
- the next startup catches up when OpenClaw was stopped at the due time.

The worker log is stored at:

```text
%LOCALAPPDATA%\helix-mcp-knowledge\data\errors\official-sync-worker.log
```

The dashboard can request cooperative cancellation. The worker stops between
documents and keeps every completed atomic index update.

The MCP process also checks stable GitHub releases, by default once every 24
hours. It never installs updates automatically. `status` exposes the cached
result as `release_update`; agents can call `get_update_status` with
`refresh=true` for an immediate read-only check.

Use the `updates` section in `config\config.yaml` to change the interval,
disable checks, or set an absolute path to `gh.exe`. Public-endpoint errors never
block search or MCP startup. `gh` is used only when installing an update to
verify the anonymously downloaded attestation bundles locally.

## 7. Private projects

The standard wheel contains no private project material. A clean installation
must return zero projects. Deliver private YAML manifests
and documents through an authorized channel and copy them into the workspace
separately. After indexing them, verify isolation:

```powershell
& $HelixCli --config $HelixConfig smoke-test --require-project-isolation
```

Pass `project_id` explicitly whenever a query requires private project content.

## 8. Transactional update

Preview the plan without changing the installation:

```powershell
& $HelixCli --config $HelixConfig update `
  --openclaw-command $OpenClawCommand `
  --dry-run
```

When the plan is correct and no documentation sync is active, install and
activate the latest stable release:

```powershell
& $HelixCli --config $HelixConfig update `
  --openclaw-command $OpenClawCommand
```

Use `--version 1.31.4` to pin a release. The updater requires `gh attestation
verify` but no GitHub login. It obtains metadata, assets, and attestation bundles
from anonymous public endpoints, does not forward `GH_TOKEN` or `GITHUB_TOKEN`,
verifies the published SHA-256 and provenance locally, installs a versioned
runtime, backs up configuration, SQLite, and the stable launcher, and runs the
smoke test. It
then switches the stable launcher. For OpenClaw-managed installations it also
backs up and switches the MCP definition and probes all nine tools. Any failure
restores the previous data, launcher, and applicable registration automatically.

After correcting an incomplete destination runtime, retry with `--resume`.
Downgrades additionally require `--allow-downgrade`. A downgrade below v1.25.0
stops the optional reranker worker before activation but retains its downloaded
files for a possible later upgrade; remove it from the dashboard first if you want
to reclaim that storage. After successful activation,
automatic retention keeps the active runtime, one previous runtime, and the
newest successful backup. Failed-update backups are kept for 14 days. Historical
backups matching the exact managed `pre-v<version>[-manual]-<UTC timestamp>` pattern
participate in the same retention policy; unknown directories remain protected.
Older wheel downloads are removed automatically. Ingestion diagnostics are kept for
30 days and operational logs are capped at 10 MB. The CLI update result records
reclaimed bytes, while the dashboard displays only the active version.

Restart the gateway after activation:

```powershell
openclaw.cmd gateway restart
openclaw.cmd mcp probe helix_knowledge --json
```

Versions 1.0.x do not include `update`. Install v1.31.4 once in a parallel
runtime while retaining the same workspace; subsequent upgrades can use the
integrated updater.

## 9. Rollback

Update failures trigger automatic rollback. To deliberately return to a prior
stable release:

```powershell
& $HelixCli --config $HelixConfig update `
  --version 1.1.0 `
  --allow-downgrade `
  --resume `
  --openclaw-command $OpenClawCommand

openclaw.cmd gateway restart
openclaw.cmd mcp probe helix_knowledge --json
```

Replace `1.1.0` with the target release and review its notes for irreversible
schema or configuration changes first.

## 10. Troubleshooting

### PowerShell blocks `openclaw.ps1`

Use `openclaw.cmd`. The installer runs `.cmd` and `.bat` wrappers through the
Windows command processor.

### The agent retains an older version

```powershell
openclaw.cmd mcp reload
openclaw.cmd gateway restart
openclaw.cmd mcp probe helix_knowledge --json
```

Then start a `/new` session.

### Searches return no documents

Inspect `status` and the worker log. Verify HTTPS connectivity to
`docs.helixops.ai`. Do not start a second synchronization while the status is
`running`.

### Installation takes more than 30 seconds

OpenClaw can spend about 30 seconds negotiating or discarding MCP runtimes.
Installation and update allow at least 60 seconds for reload; a total of 60–90
seconds is not, by itself, a failure.

## 11. Windows acceptance checklist

- [ ] Python 3.12 and GitHub CLI work for the same user.
- [ ] The wheel came from the GitHub release, not a checkout.
- [ ] `pip check` reports no broken dependencies.
- [ ] The `.exe` entry points exist under `venv\Scripts`.
- [ ] The stable `.cmd` launcher exists under the workspace `bin` directory.
- [ ] `openclaw.cmd`, when selected, registers and probes that launcher.
- [ ] A clean installation reports no selected products.
- [ ] Enabling a product in the dashboard preselects its latest version.
- [ ] Exactly nine tools and no diagnostics are reported.
- [ ] Official synchronization finishes without errors.
- [ ] The standard wheel contains no private projects.
- [ ] The gateway reconnects after restart.
- [ ] The previous runtime remains available for rollback.

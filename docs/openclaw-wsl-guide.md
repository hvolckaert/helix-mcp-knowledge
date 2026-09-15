# Installing and maintaining Helix Knowledge on WSL/Linux

This guide installs `helix-mcp-knowledge` from a GitHub release without
cloning the repository or relying on Windows Task Scheduler. It also covers the
first synchronization, acceptance checks, transactional updates, and rollback.

## 1. Requirements

- WSL with an active Linux distribution, or a supported Linux host.
- Python 3.12 or later with virtual-environment support.
- OpenClaw is optional and provides the recommended automatic MCP integration.
- HTTPS access to `docs.helixops.ai`.

Verify the environment:

```bash
python3 --version
```

If OpenClaw is installed, also run `openclaw --version`.

The configured official BMC source does not require BMC credentials. Never
store GitHub tokens, OpenClaw credentials, or other secrets in the server YAML.
See [resource planning](../README.md#resource-planning) for the base lexical
profile and the separate estimates for OCR, semantic search, and reranking.

## 2. Clean installation of v1.31.5

### Recommended one-command installation

From a checkout of this version:

```bash
./scripts/install-linux.sh --version 1.31.5
```

By default, the installer selects no products. It creates a usable MCP server
but downloads and indexes nothing until the user saves a selection in the
dashboard, which opens automatically after a new empty installation. When
OpenClaw is available, `--client auto` registers it automatically; otherwise
installation completes without a client dependency. Use `--client none` to
skip detection or `--client openclaw` to require the automatic OpenClaw
integration. Use
`--no-dashboard` to suppress the first browser window. The dashboard user service
is still installed and enabled. Repeat `--product PRODUCT=VERSION`
only for unattended installations
that must start with a predefined selection:

```bash
./scripts/install-linux.sh --version 1.31.5 \
  --product cmdb=26.3 \
  --product discovery=current
```

Use `--dashboard-port PORT` only when 8765 conflicts with another local service.

The initial catalog exposes six products. Innovation Suite/AR System, CMDB, ITSM,
Digital Workplace, and Business Workflows support 26.1, 26.2, and 26.3.
Discovery SaaS uses `current` because BMC publishes one continuously updated
documentation space.

The installer never replaces an existing runtime. If installation is
interrupted, fix the cause and run it again with `--resume`.

### Manual installation

Define persistent paths in the Linux user profile:

```bash
export HELIX_KNOWLEDGE_VERSION="1.31.5"
export HELIX_KNOWLEDGE_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/helix-mcp-knowledge"
export HELIX_KNOWLEDGE_RUNTIME="$HELIX_KNOWLEDGE_HOME/runtime/$HELIX_KNOWLEDGE_VERSION"
export HELIX_KNOWLEDGE_DOWNLOAD="$HELIX_KNOWLEDGE_HOME/downloads/$HELIX_KNOWLEDGE_VERSION"

mkdir -p "$HELIX_KNOWLEDGE_RUNTIME" "$HELIX_KNOWLEDGE_DOWNLOAD"
```

Download the wheel and locked requirements from the public GitHub release:

```bash
curl --fail --location --output \
  "$HELIX_KNOWLEDGE_DOWNLOAD/helix_mcp_knowledge-$HELIX_KNOWLEDGE_VERSION-py3-none-any.whl" \
  "https://github.com/hvolckaert/helix-mcp-knowledge/releases/download/v$HELIX_KNOWLEDGE_VERSION/helix_mcp_knowledge-$HELIX_KNOWLEDGE_VERSION-py3-none-any.whl"
curl --fail --location --output "$HELIX_KNOWLEDGE_DOWNLOAD/runtime-requirements.txt" \
  "https://github.com/hvolckaert/helix-mcp-knowledge/releases/download/v$HELIX_KNOWLEDGE_VERSION/runtime-requirements.txt"
```

Create an isolated runtime and install the package:

```bash
python3 -m venv "$HELIX_KNOWLEDGE_RUNTIME/venv"

"$HELIX_KNOWLEDGE_RUNTIME/venv/bin/python" -m pip install \
  --require-hashes \
  --requirement "$HELIX_KNOWLEDGE_DOWNLOAD/runtime-requirements.txt"

"$HELIX_KNOWLEDGE_RUNTIME/venv/bin/python" -m pip install \
  --no-deps \
  "$HELIX_KNOWLEDGE_DOWNLOAD/helix_mcp_knowledge-$HELIX_KNOWLEDGE_VERSION-py3-none-any.whl"

"$HELIX_KNOWLEDGE_RUNTIME/venv/bin/python" -m pip check
```

Create the client-neutral managed installation without selecting products:

```bash
"$HELIX_KNOWLEDGE_RUNTIME/venv/bin/helix-mcp-knowledge" install \
  --workspace "$HELIX_KNOWLEDGE_HOME" \
  --automatic-sync
```

`install` creates the configuration, SQLite database, and stable launcher under
`$HELIX_KNOWLEDGE_HOME/bin`. It also installs and enables
`helix-mcp-knowledge-dashboard.service` in the current user's systemd manager.
It opens the first-run dashboard when the workspace is new and has no selected
products. Add `--no-dashboard` only to suppress the browser window.

To register OpenClaw automatically, use `install-openclaw` instead and add
`--server-name helix_knowledge --openclaw-command /usr/bin/openclaw`. The
registration exposes and probes all nine tools and reloads the catalog. It can
take 60–90 seconds even when healthy.

No official worker starts while the selection is empty. After the user saves
products and versions, the first complete synchronization runs in the
background and takes longer.
As a reference, a clean WSL run for the three 26.1 products indexed 3,014
documents and 19,184 chunks in about 44 minutes. Duration varies with network
and BMC content; `running` is expected while the worker makes progress.

If the gateway was already running, restart it so persistent agent processes
discard the previous runtime:

```bash
openclaw gateway restart
openclaw gateway status
openclaw channels status
```

### Visual configuration

The managed dashboard starts automatically with the WSL/Linux user session. Open
it to change products, versions, synchronization frequency, and to create, remove,
or configure private projects and their document folders without editing YAML:

```bash
~/.local/share/helix-mcp-knowledge/runtime/1.31.5/venv/bin/helix-mcp-knowledge \
  --config ~/.local/share/helix-mcp-knowledge/config/config.yaml \
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
folder but also accepts another folder visible to WSL. A Windows `C:\...` path is
translated to `/mnt/c/...` when the drive is mounted. Use **Browse...** to navigate
managed storage, the WSL home directory, or mounted Windows drives without typing
the path. The picker previews files in the current folder and marks supported
formats. Project cards show detected and indexed totals and provide **Index
documents now** for an immediate local rescan; source files are never uploaded.
Removing a project deletes only its registration and derived search index; the
source folder is preserved.
Keep the dashboard bound to loopback; do not publish this port on the network.

The **Dashboard service** card shows whether the user service is installed,
enabled, and active. The service uses `Restart=on-failure` and restarts after five
seconds. Inspect it without administrator rights:

```bash
systemctl --user status helix-mcp-knowledge-dashboard.service
journalctl --user -u helix-mcp-knowledge-dashboard.service
```

No root-owned unit or Windows Task Scheduler entry is created. If the distribution
does not provide a working `systemd --user` manager, installation starts a detached
supervisor for the current session and reports that fallback in the dashboard.

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
The measured reference footprint is about 3.3 GB on disk and 1.8 GB of RAM while loaded;
CPU-only searches can take roughly 10–16 seconds with the default candidate pool.
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
`openclaw mcp reload` manually. Other MCP clients must reconnect themselves;
an unchanged save triggers no reload.
Installation remains valid when OpenClaw is unavailable. Configuration for
Claude Code, Codex, and other clients is documented in the
[MCP client integration guide](mcp-client-integration.md).

## 3. Initial verification

Check the real OpenClaw registration and tool catalog:

```bash
openclaw mcp doctor helix_knowledge --probe
openclaw mcp probe helix_knowledge --json
```

The result should contain:

- a `launch` path ending in `bin/helix-mcp-knowledge-server`;
- exactly nine `helix_knowledge__*` tools;
- `requestTimeoutMs: 60000`;
- an empty `diagnostics` array.

Inspect persistent status:

```bash
export HELIX_KNOWLEDGE_CLI="$HELIX_KNOWLEDGE_RUNTIME/venv/bin/helix-mcp-knowledge"
export HELIX_KNOWLEDGE_CONFIG="$HELIX_KNOWLEDGE_HOME/config/config.yaml"

"$HELIX_KNOWLEDGE_CLI" --config "$HELIX_KNOWLEDGE_CONFIG" status
```

A clean CLI status shows an empty `official_docs.products` map and no official
worker state; the dashboard and `get_sync_status` report `not_configured`.
After products are saved, the status may become `running`. Do not start a second
synchronization while the detached worker is active. When the status is `ok`,
run the non-destructive acceptance test:

```bash
"$HELIX_KNOWLEDGE_CLI" --config "$HELIX_KNOWLEDGE_CONFIG" smoke-test
```

Without private projects, `project_isolation` is expected to be `skip`; all
other checks should be `pass`.

## 4. Test from an agent

After selecting and indexing CMDB, start a new OpenClaw conversation and request
real tool calls:

```text
/new
Call helix_knowledge__list_projects,
helix_knowledge__list_versions with product=cmdb, and
helix_knowledge__get_sync_status. Then search for
"normalization reconciliation" in CMDB 26.1 with
source_scope=bmc_official. Cite the official URLs returned by the tools.
```

Before the first synchronization finishes, selected products and versions are
reported as `configured: true` and `indexed: false`. They change to
`indexed: true` when searchable evidence exists. `get_sync_status` describes
whether loading is pending, running, ready, cancelled, or failed. It also returns
safe progress aggregates and notice/error counts without exposing internal paths
or raw errors.

Use `openclaw mcp probe`—not agent inference—to confirm the stable launch path.
Since v1.2.0, `search_docs` prefers one chunk per document and section before
using repeated chunks to fill `top_k`.

## 5. Automatic synchronization and release checks

The product and version selection is stored in `config/config.yaml`. With
`automatic_sync: true`:

- an empty database starts synchronization during the first MCP launch;
- each product is limited to its selected versions;
- reconciliation becomes due every 24 hours;
- the next MCP launch catches up when no process was running at the due time;
- a SQLite lease prevents duplicate workers.

Inspect phase, per-version progress, elapsed time, and last activity with `status`
or `get_sync_status`. Missing and empty pages are reported as non-fatal notices.
The worker log is stored at
`data/errors/official-sync-worker.log`.

The MCP process also checks stable GitHub releases, by default once every 24
hours. It never installs an update automatically. `status` exposes the cached
result as `release_update`; an agent can perform a read-only refresh with:

```text
Call helix_knowledge__get_update_status. Repeat with refresh=true and report
current_version, latest_version, and update_available. Do not install anything.
```

Use the `updates` section in `config/config.yaml` to change the interval or
disable checks. Public-endpoint failures do not block search or MCP startup.
Installation provisions a private, pinned GitHub CLI inside the workspace; it
requires no `sudo`, login, or `PATH` changes and is used only for local
verification of anonymously downloaded attestation bundles.

## 6. Private projects

The standard wheel contains no private project material. A clean installation
must return zero projects.

Deliver private YAML manifests and documents through an authorized channel,
copy them into the workspace separately, index them, and verify isolation:

```bash
"$HELIX_KNOWLEDGE_CLI" --config "$HELIX_KNOWLEDGE_CONFIG" \
  smoke-test --require-project-isolation
```

`all_relevant` combines official evidence only with the effective project; it
never searches every project. Pass `project_id` explicitly whenever a query
requires private project material.

## 7. Transactional update

Preview the update plan without changing the installation:

```bash
"$HELIX_KNOWLEDGE_CLI" --config "$HELIX_KNOWLEDGE_CONFIG" \
  update --openclaw-command /usr/bin/openclaw --dry-run
```

When the plan is correct and no documentation sync is active, install and
activate the latest stable release:

```bash
"$HELIX_KNOWLEDGE_CLI" --config "$HELIX_KNOWLEDGE_CONFIG" \
  update --openclaw-command /usr/bin/openclaw
```

Use `--version 1.31.5` to pin a release. The updater installs or reuses the
private, pinned GitHub CLI in the Knowledge workspace. It obtains metadata,
assets, and attestation bundles from anonymous public endpoints, strips GitHub
token, host, and repository overrides, verifies SHA-256 and provenance locally,
installs a versioned
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

```bash
openclaw gateway restart
openclaw mcp probe helix_knowledge --json
```

Versions 1.0.x do not include `update`. Install v1.31.5 once in a parallel
runtime while retaining the same workspace; subsequent upgrades can use the
integrated updater.

## 8. Rollback

Update failures trigger automatic rollback. To deliberately return to a prior
stable release:

```bash
"$HELIX_KNOWLEDGE_CLI" --config "$HELIX_KNOWLEDGE_CONFIG" \
  update --version 1.1.0 --allow-downgrade --resume \
  --openclaw-command /usr/bin/openclaw

openclaw gateway restart
openclaw mcp probe helix_knowledge --json
```

Replace `1.1.0` with the target release and review its notes for irreversible
schema or configuration changes first.

## 9. Troubleshooting

### `helix_knowledge` is missing from a conversation

```bash
openclaw mcp doctor helix_knowledge --probe
openclaw mcp reload
openclaw gateway restart
```

Then start a `/new` session. Restarting only the conversation may retain a
persistent Codex process.

### The active path still reports an older version

Run `openclaw mcp probe helix_knowledge --json`. If `launch` already points to
the new runtime, restart the gateway and start a new session.

### Searches return no documents

Inspect `status`. On a clean installation, wait for `official_sync.status` to
change from `running` to `ok`. If it reports `error`, inspect the worker log and
verify HTTPS access to `docs.helixops.ai`.

### Startup takes more than 30 seconds

OpenClaw can spend about 30 seconds negotiating or discarding MCP runtimes.
Installation and update allow at least 60 seconds for reload; a total of 60–90
seconds is not, by itself, a failure.

## 10. Pilot acceptance checklist

- [ ] The wheel was downloaded from the GitHub release, without a local checkout.
- [ ] The package is installed in an independent versioned runtime.
- [ ] `pip check` reports no broken dependencies.
- [ ] The stable launcher exists under the workspace `bin` directory.
- [ ] OpenClaw, when selected, exposes exactly nine tools through that launcher.
- [ ] A clean installation reports no selected products.
- [ ] Enabling a product in the dashboard preselects its latest version.
- [ ] Official synchronization finishes without errors.
- [ ] Search returns canonical `docs.helixops.ai` URLs.
- [ ] A standard installation contains no private projects.
- [ ] Gateway and channels reconnect after restart.
- [ ] The previous runtime remains available for rollback.

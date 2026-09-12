# MCP client integration

Helix Knowledge is a standard local `stdio` MCP server. OpenClaw integration is
automatic when OpenClaw is available, but no MCP client is required to install,
configure, synchronize, or update the server.

The native installer creates one stable launcher. Always register this launcher
instead of a command inside a version-specific runtime:

| Platform | Stable launcher |
| --- | --- |
| WSL or Linux | `~/.local/share/helix-mcp-knowledge/bin/helix-mcp-knowledge-server` |
| Native Windows | `%LOCALAPPDATA%\helix-mcp-knowledge\bin\helix-mcp-knowledge-server.cmd` |

The launcher already supplies `HELIX_KNOWLEDGE_CONFIG`. A release update can
therefore switch runtimes without requiring a client configuration change.

## OpenClaw

The Linux and Windows installers use automatic client detection by default. If
OpenClaw is in `PATH`, they register `helix_knowledge`, probe its tools, and
reload the OpenClaw MCP catalog. If OpenClaw is absent, installation completes
in standalone mode.

Use `--client none` on WSL/Linux or `-Client none` on Windows to skip detection.
Use `--client openclaw` or `-Client openclaw` only when installation must fail if
OpenClaw cannot be configured.

The local dashboard reports whether the managed installation is connected to
OpenClaw. It does not configure other clients. After a validated dashboard save
changes the active configuration, it runs `openclaw mcp reload`; the next tool
request starts a fresh Knowledge process with those settings. If that reload
fails, the saved configuration remains active on disk and the dashboard requests
a manual reload. An unchanged save does not reload OpenClaw.

## Claude Code

Register the stable WSL/Linux launcher at user scope:

```bash
claude mcp add --transport stdio --scope user helix_knowledge -- \
  /home/<user>/.local/share/helix-mcp-knowledge/bin/helix-mcp-knowledge-server
```

Verify it and then start a new Claude Code session:

```bash
claude mcp get helix_knowledge
claude mcp list
```

See the [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp)
for project and user scopes, native Windows details, and client-specific
troubleshooting.

## Codex

Register the stable WSL/Linux launcher:

```bash
codex mcp add helix_knowledge -- \
  /home/<user>/.local/share/helix-mcp-knowledge/bin/helix-mcp-knowledge-server
```

Verify it and then start a new Codex task:

```bash
codex mcp get helix_knowledge
codex mcp list
```

The ChatGPT desktop app, Codex CLI, and Codex IDE extension share the MCP
configuration. See the [official OpenAI MCP documentation](https://developers.openai.com/codex/mcp/)
for the graphical setup and `config.toml` alternatives.

## Other MCP clients

Configure a local `stdio` server named `helix_knowledge` with the stable launcher
as its command and no arguments. For example, on WSL/Linux:

```json
{
  "mcpServers": {
    "helix_knowledge": {
      "command": "/home/<user>/.local/share/helix-mcp-knowledge/bin/helix-mcp-knowledge-server",
      "args": []
    }
  }
}
```

Configuration file names and restart behavior are client-specific. After adding
or changing the server—or after saving Knowledge settings in the dashboard—reconnect
the client or create a new session, then verify that all nine Helix Knowledge
tools are available.

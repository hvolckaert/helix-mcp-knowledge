# IntelliAgentia dashboard UI contract

Status: version 1, shared by Helix MCP Knowledge and Helix MCP Gateway.

This contract keeps both dashboards recognizably part of the same IntelliAgentia product family while allowing each product to retain the navigation and workflows required by its domain. Each repository owns its implementation and release lifecycle.

## Shared shell and visual language

- Use the IntelliAgentia brand header, product subtitle, content width, canvas, typography, palette, spacing scale, rounded surfaces, shadows, and focus treatment consistently.
- Use the same component language for summary metrics, cards, tabs, fields, buttons, badges, notices, dialogs, toasts, progress indicators, and the sticky save bar.
- Preserve keyboard navigation, visible focus, meaningful labels, responsive layouts, and readable status contrast.
- Keep product-specific information architecture: Knowledge uses Documentation and Settings; Gateway uses environment tabs and Advanced settings.

## Information hierarchy

The operational summary should prioritize information a user needs during normal operation:

1. product or configuration health;
2. server update state;
3. a product-specific operational metric, such as indexed documents in Knowledge or configured credentials in Gateway.

MCP client integration and dashboard process supervision are secondary operational details. Show them together in Settings, Advanced settings, or Diagnostics rather than duplicating them in the primary summary. Distinguish these dashboard cases clearly:

- Managed and running: automatic startup and restart are enabled.
- Running manually: the dashboard responds, but persistence is not managed.
- Automatic startup unavailable: the current environment cannot register a supported supervisor.
- Stopped or failed: show the actionable failure in the diagnostic area.

## Status vocabulary

User-facing copy must describe the outcome rather than expose internal state names.

| Internal state | User-facing label | Suggested detail |
| --- | --- | --- |
| `current` | Updated | Updated to `<version>` |
| `available` | Update available | `<version>` is available |
| `pending`, `waiting`, `running` | Updating | Explain the current safe step |
| `success`, `updated` | Updated | Updated to `<version>` |
| `error` | Update failed | State the recoverable next step |
| OpenClaw detected and configured | Connected | Connected to OpenClaw |
| OpenClaw unavailable | Not connected | OpenClaw was not detected; MCP remains usable with other clients |

`Current` may remain an internal API value or a version alias such as a SaaS documentation channel, but it is not the visible server update result.

## Persistent changes

- Mark the affected tab when it contains unsaved changes.
- Keep the sticky save bar visible so the save location is predictable, with actions disabled when there are no pending changes and with consistent primary and secondary hierarchy.
- Review persistent changes before applying them when the effect spans multiple sections or includes cleanup, restart, or other material consequences.
- Use a structured review dialog for ordinary configuration changes and a dedicated confirmation for destructive actions.
- Keep destructive actions explicit, scoped, and separate from normal save actions.
- After saving, clear dirty indicators only after the server confirms the new state.
- Apply a valid effective save to a managed OpenClaw integration with `mcp reload`;
  never include that reload in the configuration transaction or roll back a valid
  save because the client could not reload.
- Report successful application, a reload failure requiring manual action, and
  reconnection requirements for non-OpenClaw clients in both the save result and
  the persistent save-bar guidance.
- Do not reload the MCP client when the persisted configuration did not change.

## Independent delivery

- Do not introduce a shared runtime package in this phase.
- Keep the shared design tokens and interaction rules synchronized through this versioned contract and review both dashboards together before release.
- Implement and commit each repository independently so either dashboard can be rolled back without changing the other product.
- Publish each product only after its own tests pass and the cross-product desktop and narrow-screen comparison has been reviewed.

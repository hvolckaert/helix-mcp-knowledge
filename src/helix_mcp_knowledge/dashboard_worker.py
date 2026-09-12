"""Detached process entry point for the local administration dashboard."""

from __future__ import annotations

import argparse

from .dashboard import DEFAULT_DASHBOARD_PORT, run_dashboard
from .openclaw import DEFAULT_SERVER_NAME


def main() -> int:
    parser = argparse.ArgumentParser(prog="helix-mcp-knowledge-dashboard")
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    parser.add_argument("--openclaw-command", default="openclaw")
    args = parser.parse_args()
    run_dashboard(
        args.config,
        port=args.port,
        open_browser=not args.no_browser,
        server_name=args.server_name,
        openclaw_command=args.openclaw_command,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

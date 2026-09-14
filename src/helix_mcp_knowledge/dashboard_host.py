"""Stable dashboard host with crash recovery for managed installations."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .dashboard import DEFAULT_DASHBOARD_PORT
from .dashboard_runtime import (
    DASHBOARD_MODE_ENV,
    DashboardRuntimeManager,
    dashboard_workspace_id,
)
from .managed_installation import load_managed_installation
from .openclaw import DEFAULT_SERVER_NAME


def supervisor_state_path(workspace: Path) -> Path:
    return workspace.resolve() / "runtime" / "dashboard-supervisor.json"


def supervisor_stop_path(workspace: Path) -> Path:
    return workspace.resolve() / "runtime" / "dashboard-supervisor.stop"


def supervise_dashboard(
    *,
    config_path: Path,
    port: int,
    server_name: str,
    openclaw_command: str,
    restart_seconds: float = 5.0,
) -> int:
    """Keep the dashboard alive, while treating a clean exit as an intentional stop."""

    workspace = config_path.resolve().parent.parent
    state_path = supervisor_state_path(workspace)
    stop_path = supervisor_stop_path(workspace)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.unlink(missing_ok=True)
    _refresh_runtime_registration(
        workspace=workspace,
        server_name=server_name,
        openclaw_command=openclaw_command,
    )
    stopping = False
    child: subprocess.Popen[bytes] | None = None

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    if os.name != "nt":
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    while not stopping:
        while _port_is_open(port) and not stopping and not stop_path.exists():
            _write_state(state_path, port=port, child_pid=None, status="waiting_for_port")
            time.sleep(0.5)
        if stop_path.exists():
            stopping = True
            break
        if stopping:
            break

        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        command = [
            sys.executable,
            "-m",
            "helix_mcp_knowledge.dashboard_worker",
            "--config",
            str(config_path),
            "--port",
            str(port),
            "--server-name",
            server_name,
            "--openclaw-command",
            openclaw_command,
            "--no-browser",
        ]
        child = subprocess.Popen(command, cwd=workspace, env=environment)
        _write_state(state_path, port=port, child_pid=child.pid, status="running")
        next_heartbeat = time.monotonic() + 1.0
        while child.poll() is None and not stopping and not stop_path.exists():
            time.sleep(0.25)
            if time.monotonic() >= next_heartbeat:
                _write_state(state_path, port=port, child_pid=child.pid, status="running")
                next_heartbeat = time.monotonic() + 1.0
        if stopping or stop_path.exists():
            stopping = True
            _terminate_child(child)
            break
        exit_code = child.returncode or 0
        if exit_code == 0:
            state_path.unlink(missing_ok=True)
            return 0
        _write_state(
            state_path,
            port=port,
            child_pid=None,
            status="restarting",
            exit_code=exit_code,
        )
        deadline = time.monotonic() + restart_seconds
        while time.monotonic() < deadline and not stopping and not stop_path.exists():
            time.sleep(min(0.25, deadline - time.monotonic()))

    if child is not None and child.poll() is None:
        _terminate_child(child)
    stop_path.unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)
    return 0


def _refresh_runtime_registration(
    *, workspace: Path, server_name: str, openclaw_command: str
) -> None:
    """Let the newly activated runtime migrate its own persistent service definition."""

    mode = os.environ.get(DASHBOARD_MODE_ENV, "detached")
    if mode not in {"systemd_user", "windows_startup"}:
        return
    installation = load_managed_installation(workspace)
    if installation is None:
        return
    DashboardRuntimeManager(
        installation,
        server_name=(installation.server_name or server_name),
        openclaw_command=(installation.openclaw_command or openclaw_command),
    ).refresh_registration()


def _terminate_child(child: subprocess.Popen[bytes]) -> None:
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.25)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def _write_state(
    path: Path,
    *,
    port: int,
    child_pid: int | None,
    status: str,
    exit_code: int | None = None,
) -> None:
    temporary = path.with_suffix(".tmp")
    payload = {
        "supervisor_pid": os.getpid(),
        "child_pid": child_pid,
        "port": port,
        "status": status,
        "mode": os.environ.get(DASHBOARD_MODE_ENV, "detached"),
        "workspace_id": dashboard_workspace_id(path.parent.parent),
        "heartbeat_at_epoch": time.time(),
    }
    if exit_code is not None:
        payload["last_exit_code"] = exit_code
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(prog="helix-mcp-knowledge-dashboard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    supervise = subparsers.add_parser("supervise", help="Keep the managed dashboard running")
    supervise.add_argument("--config", required=True)
    supervise.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    supervise.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    supervise.add_argument("--openclaw-command", default="openclaw")
    supervise.add_argument("--gh-command", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("dashboard port must be between 1 and 65535")
    return supervise_dashboard(
        config_path=Path(args.config).expanduser().resolve(),
        port=args.port,
        server_name=args.server_name,
        openclaw_command=args.openclaw_command,
    )


if __name__ == "__main__":
    raise SystemExit(main())

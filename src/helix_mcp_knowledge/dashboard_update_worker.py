"""Detached transactional update worker used by the local dashboard."""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import load_config
from .dashboard_runtime import (
    DASHBOARD_MODE_ENV,
    DashboardRuntimeManager,
    dashboard_workspace_id,
)
from .logging import configure_logging
from .managed_installation import load_managed_installation
from .openclaw import DEFAULT_SERVER_NAME, _resolve_command, _run
from .storage.automation import AutomationStore
from .storage.database import Database
from .updater import DEFAULT_REPOSITORY, update_installation

DASHBOARD_UPDATE_JOB = "dashboard-update"
DASHBOARD_TOKEN_ENV = "HELIX_KNOWLEDGE_DASHBOARD_TOKEN"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DashboardUpdateWorkerProcess:
    pid: int


class DashboardUpdateWorkerLauncher:
    """Start an update worker that survives the dashboard HTTP process."""

    def __init__(
        self,
        *,
        config_path: Path,
        workspace: Path,
        errors_path: Path,
        repository: str,
        target_version: str,
        server_name: str,
        openclaw_command: str | Path,
        gh_command: str | Path,
        dashboard_port: int,
        dashboard_token: str | None = None,
        python_executable: str | None = None,
    ) -> None:
        self.config_path = config_path.resolve()
        self.workspace = workspace.resolve()
        self.errors_path = errors_path.resolve()
        self.repository = repository
        self.target_version = target_version
        self.server_name = server_name
        self.openclaw_command = str(openclaw_command)
        self.gh_command = str(gh_command)
        self.dashboard_port = dashboard_port
        self.dashboard_token = dashboard_token
        self.python_executable = python_executable or sys.executable
        self._process: subprocess.Popen[bytes] | None = None
        self._started = False

    def start(self) -> DashboardUpdateWorkerProcess:
        if self._started:
            raise RuntimeError("dashboard update worker already launched")
        self.errors_path.mkdir(parents=True, exist_ok=True)
        log_path = self.errors_path / "dashboard-update-worker.log"
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        if self.dashboard_token:
            environment[DASHBOARD_TOKEN_ENV] = self.dashboard_token
        kwargs: dict[str, object] = {
            "cwd": self.workspace,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":  # pragma: no cover - exercised on Windows installations
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        command = [
            self.python_executable,
            "-m",
            "helix_mcp_knowledge.dashboard_update_worker",
            "--config",
            str(self.config_path),
            "--repository",
            self.repository,
            "--target-version",
            self.target_version,
            "--server-name",
            self.server_name,
            "--openclaw-command",
            self.openclaw_command,
            "--gh-command",
            self.gh_command,
            "--dashboard-port",
            str(self.dashboard_port),
        ]
        if os.name != "nt" and environment.get(DASHBOARD_MODE_ENV) == "systemd_user":
            process = self._start_systemd_worker(command, log_path)
            self._started = True
            return process
        with log_path.open("ab", buffering=0) as log:
            self._process = subprocess.Popen(command, stderr=log, **kwargs)
        self._started = True
        threading.Thread(
            target=self._process.wait,
            name="helix-dashboard-update-worker-reaper",
            daemon=True,
        ).start()
        return DashboardUpdateWorkerProcess(pid=self._process.pid)

    def _start_systemd_worker(
        self, command: list[str], log_path: Path
    ) -> DashboardUpdateWorkerProcess:
        """Launch outside the dashboard unit so its control group can stop safely."""

        systemd_run = shutil.which("systemd-run")
        systemctl = shutil.which("systemctl")
        if systemd_run is None or systemctl is None:
            raise RuntimeError(
                "the dashboard is managed by systemd but systemd-run/systemctl is unavailable"
            )
        token_path: Path | None = None
        if self.dashboard_token:
            token_path = self.errors_path / (f".dashboard-update-token-{secrets.token_hex(16)}")
            descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(self.dashboard_token)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                token_path.unlink(missing_ok=True)
                raise
            command.extend(["--dashboard-token-file", str(token_path)])
        unit = (
            f"helix-mcp-knowledge-update-{dashboard_workspace_id(self.workspace)}-"
            f"{secrets.token_hex(4)}.service"
        )
        transient_command = [
            systemd_run,
            "--user",
            "--quiet",
            "--collect",
            "--service-type=exec",
            f"--unit={unit}",
            f"--working-directory={self.workspace}",
            "--property=StandardInput=null",
            "--property=StandardOutput=null",
            f"--property=StandardError=append:{log_path}",
            "--property=UMask=0077",
            "--setenv=PYTHONUNBUFFERED=1",
            *command,
        ]
        launcher_environment = os.environ.copy()
        launcher_environment.pop(DASHBOARD_TOKEN_ENV, None)
        try:
            launched = subprocess.run(
                transient_command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=launcher_environment,
            )
            if launched.returncode != 0:
                detail = (launched.stderr or launched.stdout).strip()
                raise RuntimeError(
                    "could not start the independent dashboard update service"
                    + (f": {detail}" if detail else "")
                )
            for _ in range(40):
                shown = subprocess.run(
                    [
                        systemctl,
                        "--user",
                        "show",
                        unit,
                        "--property=MainPID",
                        "--value",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    env=launcher_environment,
                )
                raw_pid = shown.stdout.strip()
                if shown.returncode == 0 and raw_pid.isdigit() and int(raw_pid) > 0:
                    return DashboardUpdateWorkerProcess(pid=int(raw_pid))
                time.sleep(0.05)
            raise RuntimeError(
                "the independent dashboard update service did not report a worker process"
            )
        except BaseException:
            subprocess.run(
                [systemctl, "--user", "stop", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                env=launcher_environment,
            )
            if token_path is not None:
                token_path.unlink(missing_ok=True)
            raise


def run_update(
    *,
    config_path: str | Path,
    repository: str,
    target_version: str,
    server_name: str,
    openclaw_command: str | Path,
    gh_command: str | Path,
    dashboard_port: int,
    dashboard_token: str | None = None,
    sync_wait_timeout_seconds: float = 6 * 3600,
) -> bool:
    """Apply an update and relaunch the dashboard for any supported MCP client."""

    config = load_config(config_path)
    configure_logging(config.logging.level)
    current_python = Path(sys.executable).resolve()
    store = AutomationStore(Database(config.sqlite_path))
    requested_at = _timestamp()
    if not _wait_for_documentation_sync(
        store,
        target_version=target_version,
        timeout_seconds=sync_wait_timeout_seconds,
    ):
        store.update_state(
            DASHBOARD_UPDATE_JOB,
            {
                "status": "error",
                "current_version": _current_version(),
                "target_version": target_version,
                "requested_at": requested_at,
                "finished_at": _timestamp(),
                "process_id": os.getpid(),
                "error": "timed out waiting for documentation synchronization to finish",
            },
        )
        return False

    try:
        if dashboard_token:
            _request_dashboard_shutdown(dashboard_port, dashboard_token)
        _wait_until_port_is_free(dashboard_port)
    except Exception as exc:
        store.update_state(
            DASHBOARD_UPDATE_JOB,
            {
                "status": "error",
                "current_version": _current_version(),
                "target_version": target_version,
                "requested_at": requested_at,
                "finished_at": _timestamp(),
                "process_id": os.getpid(),
                "error": str(exc)[:1000],
            },
        )
        LOGGER.exception("could not prepare the dashboard for update")
        return False
    started_at = _timestamp()
    store.update_state(
        DASHBOARD_UPDATE_JOB,
        {
            "status": "running",
            "current_version": _current_version(),
            "target_version": target_version,
            "started_at": started_at,
            "process_id": os.getpid(),
        },
    )

    result = None
    error: str | None = None
    gateway_restarted = False
    gateway_warning: str | None = None
    dashboard_runtime: dict[str, object] | None = None

    def activate_dashboard(installation) -> None:
        nonlocal dashboard_runtime
        manager = DashboardRuntimeManager(
            installation,
            server_name=server_name,
            openclaw_command=openclaw_command,
        )
        dashboard_runtime = manager.restart_and_verify(
            expected_version=installation.active_version
        ).to_dict()

    try:
        result = update_installation(
            config_path=config.config_path,
            repository=repository,
            target_version=target_version,
            openclaw_command=openclaw_command,
            gh_command=gh_command,
            server_name=server_name,
            probe=True,
            reload=True,
            resume=True,
            current_python=current_python,
            post_activation_check=activate_dashboard,
        )
        if (
            result.status == "updated"
            and getattr(result, "client_integration", "openclaw") == "openclaw"
        ):
            try:
                managed = load_managed_installation(config.base_dir)
                effective_openclaw = (
                    managed.openclaw_command
                    if managed and managed.openclaw_command
                    else openclaw_command
                )
                resolved_openclaw = _resolve_command(effective_openclaw, label="OpenClaw")
                _run(
                    [str(resolved_openclaw), "gateway", "restart"],
                    runner=subprocess.run,
                    timeout=120,
                    action="OpenClaw Gateway restart",
                )
                gateway_restarted = True
            except Exception as exc:  # update remains valid even if the gateway restart fails
                gateway_warning = str(exc)[:500]
    except Exception as exc:
        error = str(exc)[:1000]
        LOGGER.exception("dashboard-triggered update failed")

    config = load_config(config_path)
    store = AutomationStore(Database(config.sqlite_path))
    installed_version = (
        result.target_version
        if result is not None and result.status in {"up_to_date", "updated"}
        else _current_version()
    )
    state: dict[str, object] = {
        "status": "error" if error else "success",
        "current_version": installed_version,
        "target_version": target_version,
        "started_at": started_at,
        "finished_at": _timestamp(),
        "process_id": os.getpid(),
        "gateway_restarted": gateway_restarted,
    }
    if result is not None:
        state["result_status"] = result.status
        retention = getattr(result, "storage_retention", None)
        if retention is not None:
            state["storage_retention"] = retention.to_dict()
    if gateway_warning:
        state["gateway_warning"] = gateway_warning
    if error:
        state["error"] = error
    if dashboard_runtime is not None:
        state["dashboard_runtime"] = dashboard_runtime
    store.update_state(DASHBOARD_UPDATE_JOB, state)

    if dashboard_runtime is None:
        try:
            managed = load_managed_installation(config.base_dir)
            if managed is None:
                raise RuntimeError("managed installation metadata disappeared during update")
            restored = DashboardRuntimeManager(
                managed,
                server_name=server_name,
                openclaw_command=openclaw_command,
            ).restart_and_verify(expected_version=managed.active_version)
            dashboard_runtime = restored.to_dict()
            state["dashboard_runtime"] = dashboard_runtime
            if restored.process_id is not None:
                state["dashboard_process_id"] = restored.process_id
            store.update_state(DASHBOARD_UPDATE_JOB, state)
        except Exception as exc:
            state["status"] = "error"
            state["error"] = (
                f"{error}; dashboard recovery failed: {exc}"
                if error
                else f"dashboard recovery failed: {exc}"
            )[:1000]
            store.update_state(DASHBOARD_UPDATE_JOB, state)
            LOGGER.exception("could not relaunch the managed dashboard after update")
            return False
    return error is None


def _wait_for_documentation_sync(
    store: AutomationStore,
    *,
    target_version: str,
    timeout_seconds: float,
    poll_seconds: float = 2.0,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    waiting_recorded = False
    while _documentation_sync_active(store):
        if not waiting_recorded:
            store.update_state(
                DASHBOARD_UPDATE_JOB,
                {
                    "status": "waiting_for_sync",
                    "current_version": _current_version(),
                    "target_version": target_version,
                    "waiting_since": _timestamp(),
                    "process_id": os.getpid(),
                },
            )
            waiting_recorded = True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
    return True


def _documentation_sync_active(store: AutomationStore) -> bool:
    with store.database.connect() as connection:
        rows = connection.execute(
            """SELECT job_id, state_json FROM automation_state
               WHERE job_id = 'official-docs' OR job_id LIKE 'project:%'"""
        ).fetchall()
    for row in rows:
        try:
            state = json.loads(row["state_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        if store.running_state_is_active(state):
            return True
        if row["job_id"] == "official-docs" and recent_dashboard_request(state):
            return True
    return False


def recent_dashboard_request(state: dict[str, object]) -> bool:
    if state.get("status") != "pending" or state.get("reason") != "dashboard_request":
        return False
    requested = state.get("requested_at")
    if not isinstance(requested, str):
        return False
    try:
        timestamp = datetime.fromisoformat(requested.replace("Z", "+00:00"))
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - timestamp).total_seconds()
    return 0 <= age <= 300


def _request_dashboard_shutdown(port: int, token: str) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            "POST",
            "/api/update/prepare",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "X-Helix-Dashboard-Token": token,
            },
        )
        response = connection.getresponse()
        response.read()
        if response.status != 202:
            raise RuntimeError(f"dashboard restart preparation returned HTTP {response.status}")
    finally:
        connection.close()


def _wait_until_port_is_free(port: int, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.25)
            if connection.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.25)
    raise RuntimeError(f"dashboard port {port} did not become available")


def _current_version() -> str:
    from . import __version__

    return __version__


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _consume_dashboard_token(path: Path) -> str:
    """Read and remove the one-time token passed to a transient update service."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise RuntimeError("dashboard update token file is not private")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            token = stream.read(4097)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
    if not token or len(token) > 4096:
        raise RuntimeError("dashboard update token file is empty or invalid")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(prog="helix-mcp-knowledge-dashboard-update")
    parser.add_argument("--config", required=True)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    parser.add_argument("--openclaw-command", default="openclaw")
    parser.add_argument("--gh-command", default="gh")
    parser.add_argument("--dashboard-port", type=int, required=True)
    parser.add_argument("--dashboard-token-file", type=Path)
    args = parser.parse_args()
    dashboard_token = os.environ.get(DASHBOARD_TOKEN_ENV)
    if args.dashboard_token_file is not None:
        dashboard_token = _consume_dashboard_token(args.dashboard_token_file)
    return (
        0
        if run_update(
            config_path=args.config,
            repository=args.repository,
            target_version=args.target_version,
            server_name=args.server_name,
            openclaw_command=args.openclaw_command,
            gh_command=args.gh_command,
            dashboard_port=args.dashboard_port,
            dashboard_token=dashboard_token,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())

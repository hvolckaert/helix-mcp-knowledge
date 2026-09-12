"""Cross-platform lifecycle management for the local dashboard."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .errors import KnowledgeError
from .managed_installation import ManagedInstallation

DASHBOARD_SERVICE_NAME = "helix-mcp-knowledge-dashboard.service"
DASHBOARD_WINDOWS_RUN_NAME = "HelixMcpKnowledgeDashboard"
DASHBOARD_MODE_ENV = "HELIX_KNOWLEDGE_DASHBOARD_MODE"
DASHBOARD_MANAGER_ENV = "HELIX_KNOWLEDGE_DASHBOARD_MANAGER"


def dashboard_workspace_id(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:16]


def dashboard_health_payload(port: int) -> dict[str, object] | None:
    """Read the local dashboard health endpoint without requiring its browser token."""

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            return None
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def dashboard_supervises_workspace(workspace: Path, port: int) -> bool:
    payload = dashboard_health_payload(port)
    return bool(
        payload and payload.get("workspace_id") == dashboard_workspace_id(workspace.resolve())
    )


class WindowsRunRegistry(Protocol):
    def read(self, name: str) -> str | None: ...

    def write(self, name: str, command: str) -> None: ...


class _NativeWindowsRunRegistry:
    KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def read(self, name: str) -> str | None:  # pragma: no cover - Windows only
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return str(value)

    def write(self, name: str, command: str) -> None:  # pragma: no cover - Windows only
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)


@dataclass(frozen=True)
class DashboardRuntimeStatus:
    manager: str
    installed: bool
    enabled: bool
    active: bool
    status: str
    restart_policy: str
    service_name: str | None
    launcher: str
    port: int
    process_managed: bool
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DashboardRuntimeResult:
    status: DashboardRuntimeStatus
    process_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {**self.status.to_dict(), "process_id": self.process_id}


class DashboardRuntimeManager:
    """Install and operate the best no-admin dashboard manager for this platform."""

    def __init__(
        self,
        installation: ManagedInstallation,
        *,
        server_name: str,
        openclaw_command: str | Path,
        runner=subprocess.run,
        process_factory=subprocess.Popen,
        platform_name: str | None = None,
        user_config_dir: Path | None = None,
        windows_registry: WindowsRunRegistry | None = None,
    ) -> None:
        self.installation = installation
        self.workspace = installation.config_path.parent.parent.resolve()
        self.server_name = server_name
        self.openclaw_command = str(openclaw_command)
        self.runner = runner
        self.process_factory = process_factory
        self.platform_name = platform_name or ("windows" if os.name == "nt" else "posix")
        config_root = user_config_dir or Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        )
        self.unit_path = (
            config_root.expanduser().resolve() / "systemd/user" / DASHBOARD_SERVICE_NAME
        )
        self.log_path = self.workspace / "data/errors/dashboard-supervisor.log"
        self.windows_registry = windows_registry

    @property
    def port(self) -> int:
        return self.installation.dashboard_port

    def install_and_start(self, *, verify: bool = True) -> DashboardRuntimeResult:
        if not self.installation.dashboard_launcher.is_file():
            raise KnowledgeError(
                f"managed dashboard launcher was not found: {self.installation.dashboard_launcher}"
            )
        if self._port_is_open():
            current = self._health_payload()
            if (
                current is None
                or current.get("workspace_id") != dashboard_workspace_id(self.workspace)
                or current.get("server_version") != self.installation.active_version
            ):
                raise KnowledgeError(
                    f"dashboard port {self.port} is already used by another process or runtime"
                )
        if self.platform_name == "windows":
            registry = self.windows_registry or _NativeWindowsRunRegistry()
            registry.write(DASHBOARD_WINDOWS_RUN_NAME, self._windows_startup_command())
            process_id = None
            if (
                os.environ.get(DASHBOARD_MODE_ENV) != "windows_startup"
                and not self._supervisor_alive()
            ):
                process_id = self._launch_supervisor("windows_startup")
            if verify:
                process_id = self._verify_windows_startup(
                    expected_version=self.installation.active_version,
                    process_id=process_id,
                )
            return DashboardRuntimeResult(self.status(), process_id)

        if self._systemd_available():
            previous_unit = (
                self.unit_path.read_text(encoding="utf-8") if self.unit_path.is_file() else None
            )
            previous_enabled = bool(
                previous_unit is not None and self._systemctl_status("is-enabled") == "enabled"
            )
            previous_active = bool(
                previous_unit is not None
                and self._systemctl_status("is-active") in {"active", "activating", "reloading"}
            )
            registration_changed = previous_unit != self._render_unit()
            try:
                self._write_unit()
                self._systemctl("daemon-reload")
                self._systemctl("enable", "--now", DASHBOARD_SERVICE_NAME)
                # `enable --now` starts an inactive unit but deliberately does not
                # restart one that is already running.  Restart after changing the
                # command so health verification observes the newly registered
                # workspace/runtime instead of the old process.
                if registration_changed and previous_active:
                    self._systemctl("restart", DASHBOARD_SERVICE_NAME)
                if verify:
                    self.wait_until_healthy(expected_version=self.installation.active_version)
                return DashboardRuntimeResult(self.status())
            except Exception as exc:
                if registration_changed:
                    try:
                        self._restore_systemd_registration(
                            previous_unit,
                            enabled=previous_enabled,
                            active=previous_active,
                        )
                    except Exception as rollback_exc:
                        raise KnowledgeError(
                            f"{exc}; dashboard service rollback failed: {rollback_exc}"
                        ) from exc
                raise

        process_id = None
        if not self._supervisor_alive():
            process_id = self._launch_supervisor("detached")
        if verify:
            self.wait_until_healthy(expected_version=self.installation.active_version)
        return DashboardRuntimeResult(self.status(), process_id)

    def restart_and_verify(self, *, expected_version: str) -> DashboardRuntimeResult:
        try:
            if self.platform_name == "windows":
                registry = self.windows_registry or _NativeWindowsRunRegistry()
                registry.write(DASHBOARD_WINDOWS_RUN_NAME, self._windows_startup_command())
                self._stop_supervisor()
                process_id = self._launch_supervisor("windows_startup")
                process_id = self._verify_windows_startup(
                    expected_version=expected_version,
                    process_id=process_id,
                )
                return DashboardRuntimeResult(self.status(), process_id)
            elif self._systemd_available():
                self._write_unit()
                self._systemctl("daemon-reload")
                self._systemctl("enable", DASHBOARD_SERVICE_NAME)
                self._systemctl("restart", DASHBOARD_SERVICE_NAME)
                result = DashboardRuntimeResult(self.status())
            else:
                self._stop_supervisor()
                process_id = self._launch_supervisor("detached")
                result = DashboardRuntimeResult(self.status(), process_id)
            self.wait_until_healthy(expected_version=expected_version)
            return result
        except Exception:
            self.stop()
            raise

    def _verify_windows_startup(
        self,
        *,
        expected_version: str,
        process_id: int | None,
    ) -> int | None:
        """Retry one launcher that exited without binding or recording state."""

        try:
            self.wait_until_healthy(expected_version=expected_version)
        except KnowledgeError:
            if self._supervisor_alive() or self._port_is_open():
                raise
            process_id = self._launch_supervisor("windows_startup")
            self.wait_until_healthy(expected_version=expected_version)
        return process_id

    def refresh_registration(self) -> DashboardRuntimeStatus:
        """Refresh persistent startup metadata without restarting the active host."""

        if self.platform_name == "windows":
            registry = self.windows_registry or _NativeWindowsRunRegistry()
            registry.write(DASHBOARD_WINDOWS_RUN_NAME, self._windows_startup_command())
        elif self._systemd_available():
            self._write_unit()
            self._systemctl("daemon-reload")
            self._systemctl("enable", DASHBOARD_SERVICE_NAME)
        return self.status()

    def stop(self) -> None:
        if self.platform_name == "windows":
            self._stop_supervisor()
        elif self._systemd_available() and self.unit_path.is_file():
            self._systemctl("stop", DASHBOARD_SERVICE_NAME, check=False)
        else:
            self._stop_supervisor()

    def status(self) -> DashboardRuntimeStatus:
        process_mode = os.environ.get(DASHBOARD_MODE_ENV)
        if self.platform_name == "windows":
            try:
                registry = self.windows_registry or _NativeWindowsRunRegistry()
                installed = (
                    registry.read(DASHBOARD_WINDOWS_RUN_NAME) == self._windows_startup_command()
                )
                active = process_mode == "windows_startup" or self._supervisor_alive()
                return self._status(
                    manager="windows_startup",
                    installed=installed,
                    enabled=installed,
                    active=active,
                    process_managed=process_mode == "windows_startup",
                )
            except Exception as exc:
                return self._status(manager="windows_startup", error=str(exc))
        if self._systemd_available():
            try:
                installed = bool(
                    self.unit_path.is_file()
                    and self.unit_path.read_text(encoding="utf-8") == self._render_unit()
                )
                enabled = self._systemctl_status("is-enabled") == "enabled" if installed else False
                active_state = self._systemctl_status("is-active") if installed else "inactive"
                active = active_state in {"active", "activating", "reloading"}
                return self._status(
                    manager="systemd_user",
                    installed=installed,
                    enabled=enabled,
                    active=active,
                    process_managed=process_mode == "systemd_user",
                    explicit_status=active_state,
                )
            except Exception as exc:
                return self._status(manager="systemd_user", error=str(exc))
        active = process_mode == "detached" or self._supervisor_alive()
        return self._status(
            manager="detached",
            installed=False,
            enabled=False,
            active=active,
            process_managed=process_mode == "detached",
        )

    def wait_until_healthy(
        self,
        *,
        expected_version: str,
        timeout_seconds: float = 45.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error = "dashboard did not answer"
        while time.monotonic() < deadline:
            payload = self._health_payload()
            try:
                correct_workspace = bool(
                    payload
                    and payload.get("workspace_id") == dashboard_workspace_id(self.workspace)
                )
                if (
                    payload
                    and payload.get("server_version") == expected_version
                    and correct_workspace
                ):
                    return
                last_error = (
                    f"expected dashboard {expected_version} for this workspace; found "
                    f"{(payload or {}).get('server_version', 'an unknown version')} for "
                    f"workspace {(payload or {}).get('workspace_id', 'unknown')}"
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            time.sleep(0.5)
        diagnostic = self._failure_diagnostic()
        raise KnowledgeError(f"managed dashboard health check failed: {last_error}{diagnostic}")

    def _health_payload(self) -> dict[str, object] | None:
        return dashboard_health_payload(self.port)

    def _port_is_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.25)
            return connection.connect_ex(("127.0.0.1", self.port)) == 0

    def _status(
        self,
        *,
        manager: str,
        installed: bool = False,
        enabled: bool = False,
        active: bool = False,
        process_managed: bool = False,
        explicit_status: str | None = None,
        error: str | None = None,
    ) -> DashboardRuntimeStatus:
        status = explicit_status or ("running" if active else "stopped")
        if error:
            status = "error"
        return DashboardRuntimeStatus(
            manager=manager,
            installed=installed,
            enabled=enabled,
            active=active,
            status=status,
            restart_policy="on_failure" if manager != "detached" else "process_lifetime",
            service_name=(
                DASHBOARD_WINDOWS_RUN_NAME
                if manager == "windows_startup"
                else DASHBOARD_SERVICE_NAME
                if manager == "systemd_user"
                else None
            ),
            launcher=str(self.installation.dashboard_launcher),
            port=self.port,
            process_managed=process_managed,
            error=error,
        )

    def _systemd_available(self) -> bool:
        if os.environ.get(DASHBOARD_MANAGER_ENV, "").strip().casefold() == "detached":
            return False
        if self.platform_name != "posix" or shutil.which("systemctl") is None:
            return False
        try:
            completed = self._systemctl("show-environment", check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def _systemctl(self, *arguments: str, check: bool = True):
        completed = self.runner(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            raise KnowledgeError(f"systemd user service operation failed: {detail}")
        return completed

    def _systemctl_status(self, action: str) -> str:
        completed = self._systemctl(action, DASHBOARD_SERVICE_NAME, check=False)
        return (completed.stdout or "inactive").strip().splitlines()[0]

    def _write_unit(self) -> None:
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        content = self._render_unit()
        if self.unit_path.is_file() and self.unit_path.read_text(encoding="utf-8") == content:
            return
        self._write_unit_content(content)

    def _write_unit_content(self, content: str) -> None:
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.unit_path.name}.", dir=self.unit_path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.unit_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _restore_systemd_registration(
        self,
        previous_unit: str | None,
        *,
        enabled: bool,
        active: bool,
    ) -> None:
        """Restore the exact pre-install unit after a failed candidate start."""

        self._systemctl("stop", DASHBOARD_SERVICE_NAME, check=False)
        if previous_unit is None:
            self._systemctl("disable", DASHBOARD_SERVICE_NAME, check=False)
            self.unit_path.unlink(missing_ok=True)
            self._systemctl("daemon-reload")
            return
        self._write_unit_content(previous_unit)
        self._systemctl("daemon-reload")
        if enabled:
            self._systemctl("enable", DASHBOARD_SERVICE_NAME)
        else:
            self._systemctl("disable", DASHBOARD_SERVICE_NAME, check=False)
        if active:
            self._systemctl("start", DASHBOARD_SERVICE_NAME)

    def _render_unit(self) -> str:
        arguments = self._supervisor_arguments()
        command = " ".join(
            _systemd_quote(item) for item in [str(self.installation.dashboard_launcher), *arguments]
        )
        loader_environment = ""
        if loader_path := os.environ.get("LD_LIBRARY_PATH"):
            loader_environment = f"Environment={_systemd_quote(f'LD_LIBRARY_PATH={loader_path}')}\n"
        return (
            "[Unit]\n"
            "Description=Helix MCP Knowledge dashboard\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={_systemd_path(str(self.workspace))}\n"
            f"Environment={DASHBOARD_MODE_ENV}=systemd_user\n"
            "Environment=PYTHONUNBUFFERED=1\n"
            f"{loader_environment}"
            f"ExecStart={command}\n"
            f"StandardOutput=append:{_systemd_path(str(self.log_path))}\n"
            f"StandardError=append:{_systemd_path(str(self.log_path))}\n"
            "Restart=on-failure\n"
            "RestartSec=5s\n"
            "KillMode=control-group\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def _supervisor_arguments(self) -> list[str]:
        return [
            "supervise",
            "--config",
            str(self.installation.config_path),
            "--port",
            str(self.port),
            "--server-name",
            self.server_name,
            "--openclaw-command",
            self.openclaw_command,
        ]

    def _windows_startup_command(self) -> str:
        powershell = shutil.which("powershell.exe") or "powershell.exe"
        return subprocess.list2cmdline(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.installation.dashboard_launcher),
                *self._supervisor_arguments(),
            ]
        )

    def _launch_supervisor(self, mode: str) -> int:
        environment = os.environ.copy()
        environment[DASHBOARD_MODE_ENV] = mode
        environment["PYTHONUNBUFFERED"] = "1"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_stream = self.log_path.open("ab", buffering=0)
        kwargs: dict[str, object] = {
            "cwd": self.workspace,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": log_stream,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if self.platform_name == "windows":  # pragma: no cover - Windows process flags
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
            ) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            command = [
                shutil.which("powershell.exe") or "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.installation.dashboard_launcher),
                *self._supervisor_arguments(),
            ]
        else:
            kwargs["start_new_session"] = True
            command = [str(self.installation.dashboard_launcher), *self._supervisor_arguments()]
        try:
            process = self.process_factory(command, **kwargs)
        finally:
            log_stream.close()
        return int(process.pid)

    def _failure_diagnostic(self) -> str:
        try:
            status = self.status()
            summary = f"; manager={status.manager}, status={status.status}"
            if status.error:
                summary += f", manager_error={status.error}"
        except Exception as exc:
            summary = f"; dashboard manager status failed: {exc}"
        state_path = self.workspace / "runtime/dashboard-supervisor.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        else:
            summary += f"; supervisor_state={json.dumps(state, sort_keys=True)}"
        if self.platform_name == "posix" and self._systemd_available():
            try:
                completed = self._systemctl(
                    "status", DASHBOARD_SERVICE_NAME, "--no-pager", check=False
                )
            except Exception:
                pass
            else:
                systemd_detail = (completed.stdout or completed.stderr or "").strip()
                if systemd_detail:
                    summary += "; systemd: " + " | ".join(systemd_detail.splitlines())[-3000:]
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return summary
        if lines:
            summary += "; supervisor log: " + " | ".join(lines[-12:])[-3000:]
        return summary

    def _supervisor_alive(self) -> bool:
        state_path = self.workspace / "runtime/dashboard-supervisor.json"
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            pid = int(payload["supervisor_pid"])
            heartbeat = float(payload["heartbeat_at_epoch"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False
        if payload.get("workspace_id") != dashboard_workspace_id(self.workspace):
            return False
        if payload.get("port") != self.port or abs(time.time() - heartbeat) > 15.0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _stop_supervisor(self, *, timeout_seconds: float = 15.0) -> None:
        if not self._supervisor_alive():
            return
        stop_path = self.workspace / "runtime/dashboard-supervisor.stop"
        stop_path.write_text("stop\n", encoding="utf-8")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self._supervisor_alive():
                stop_path.unlink(missing_ok=True)
                return
            time.sleep(0.25)
        raise KnowledgeError("dashboard supervisor did not stop cleanly")


def _systemd_quote(value: str) -> str:
    if any(character in value for character in ("\n", "\r", "\0")):
        raise KnowledgeError("dashboard service values cannot contain control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _systemd_path(value: str) -> str:
    if not Path(value).is_absolute() or any(character in value for character in ("\n", "\r", "\0")):
        raise KnowledgeError("dashboard service paths must be absolute and valid")
    return value.replace("\\", "\\x5c").replace(" ", "\\x20").replace("%", "%%")

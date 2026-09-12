"""Client-neutral metadata and stable launchers for managed installations."""

from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .errors import KnowledgeError
from .workspace import CONFIG_ENV

ClientIntegration = Literal["standalone", "openclaw", "claude", "codex"]
INSTALLATION_SCHEMA_VERSION = 2
SUPPORTED_INSTALLATION_SCHEMA_VERSIONS = frozenset({1, INSTALLATION_SCHEMA_VERSION})
DEFAULT_MANAGED_DASHBOARD_PORT = 8765


@dataclass(frozen=True)
class ManagedInstallation:
    schema_version: int
    active_version: str
    client: ClientIntegration
    launcher: Path
    dashboard_launcher: Path
    config_path: Path
    dashboard_port: int = DEFAULT_MANAGED_DASHBOARD_PORT
    server_name: str | None = None
    openclaw_command: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["launcher"] = str(self.launcher)
        payload["dashboard_launcher"] = str(self.dashboard_launcher)
        payload["config_path"] = str(self.config_path)
        return payload


def installation_metadata_path(workspace: Path) -> Path:
    return workspace.resolve() / "runtime" / "installation.json"


def stable_launcher_path(workspace: Path) -> Path:
    name = "helix-mcp-knowledge-server.cmd" if os.name == "nt" else "helix-mcp-knowledge-server"
    return workspace.resolve() / "bin" / name


def stable_dashboard_launcher_path(workspace: Path) -> Path:
    name = (
        "helix-mcp-knowledge-dashboard.ps1" if os.name == "nt" else "helix-mcp-knowledge-dashboard"
    )
    return workspace.resolve() / "bin" / name


def versioned_runtime_paths(workspace: Path, version: str) -> tuple[Path, Path]:
    executable_dir = (
        workspace.resolve()
        / "runtime"
        / version
        / "venv"
        / ("Scripts" if os.name == "nt" else "bin")
    )
    suffix = ".exe" if os.name == "nt" else ""
    return (
        executable_dir / f"python{suffix}",
        executable_dir / f"helix-mcp-knowledge-server{suffix}",
    )


def supports_transactional_updates(workspace: Path, installation: ManagedInstallation) -> bool:
    python, server = versioned_runtime_paths(workspace, installation.active_version)
    return python.is_file() and server.is_file()


def load_managed_installation(workspace: Path) -> ManagedInstallation | None:
    path = installation_metadata_path(workspace)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeError(f"could not read managed installation metadata: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") not in SUPPORTED_INSTALLATION_SCHEMA_VERSIONS
    ):
        raise KnowledgeError("unsupported managed installation metadata")
    client = payload.get("client")
    if client not in ("standalone", "openclaw", "claude", "codex"):
        raise KnowledgeError("managed installation has an unsupported client integration")
    try:
        launcher = Path(str(payload["launcher"])).expanduser().resolve()
        config_path = Path(str(payload["config_path"])).expanduser().resolve()
        active_version = str(payload["active_version"])
    except KeyError as exc:
        raise KnowledgeError(f"managed installation metadata is missing {exc.args[0]}") from exc
    expected_launcher = stable_launcher_path(workspace)
    if launcher != expected_launcher:
        raise KnowledgeError("managed installation launcher is outside the expected workspace path")
    dashboard_launcher = (
        Path(str(payload.get("dashboard_launcher") or stable_dashboard_launcher_path(workspace)))
        .expanduser()
        .resolve()
    )
    if dashboard_launcher != stable_dashboard_launcher_path(workspace):
        raise KnowledgeError("managed dashboard launcher is outside the expected workspace path")
    try:
        dashboard_port = int(payload.get("dashboard_port", DEFAULT_MANAGED_DASHBOARD_PORT))
    except (TypeError, ValueError) as exc:
        raise KnowledgeError("managed dashboard port must be an integer") from exc
    if not 1 <= dashboard_port <= 65535:
        raise KnowledgeError("managed dashboard port must be between 1 and 65535")
    return ManagedInstallation(
        schema_version=INSTALLATION_SCHEMA_VERSION,
        active_version=active_version,
        client=client,
        launcher=launcher,
        dashboard_launcher=dashboard_launcher,
        config_path=config_path,
        dashboard_port=dashboard_port,
        server_name=(str(payload["server_name"]) if payload.get("server_name") else None),
        openclaw_command=(
            str(payload["openclaw_command"]) if payload.get("openclaw_command") else None
        ),
    )


def activate_managed_installation(
    *,
    workspace: Path,
    version: str,
    server_command: Path,
    config_path: Path,
    client: ClientIntegration,
    server_name: str | None = None,
    openclaw_command: str | Path | None = None,
    dashboard_port: int = DEFAULT_MANAGED_DASHBOARD_PORT,
) -> ManagedInstallation:
    """Point both stable launchers at one validated versioned runtime as one transaction."""

    resolved_workspace = workspace.expanduser().resolve()
    resolved_server = server_command.expanduser().resolve()
    resolved_config = config_path.expanduser().resolve()
    if not resolved_server.is_file():
        raise KnowledgeError(f"MCP server entry point was not found: {resolved_server}")
    if not resolved_config.is_file():
        raise KnowledgeError(f"configuration was not found: {resolved_config}")
    if client == "openclaw" and (not server_name or not openclaw_command):
        raise KnowledgeError("OpenClaw managed installations require its server name and command")
    if not 1 <= dashboard_port <= 65535:
        raise KnowledgeError("managed dashboard port must be between 1 and 65535")
    python_command = resolved_server.parent / ("python.exe" if os.name == "nt" else "python")
    if os.name == "nt" and not python_command.is_file():
        python_command = resolved_server.parent.parent / "python.exe"
    if not python_command.is_file():
        raise KnowledgeError(f"Python entry point was not found: {python_command}")

    launcher = stable_launcher_path(resolved_workspace)
    dashboard_launcher = stable_dashboard_launcher_path(resolved_workspace)
    metadata_path = installation_metadata_path(resolved_workspace)
    for directory in (launcher.parent, dashboard_launcher.parent, metadata_path.parent):
        if directory.is_symlink() or (
            hasattr(directory, "is_junction") and directory.is_junction()
        ):
            raise KnowledgeError(f"managed installation directory cannot be a link: {directory}")
        directory.mkdir(parents=True, exist_ok=True)

    installation = ManagedInstallation(
        schema_version=INSTALLATION_SCHEMA_VERSION,
        active_version=version,
        client=client,
        launcher=launcher,
        dashboard_launcher=dashboard_launcher,
        config_path=resolved_config,
        dashboard_port=dashboard_port,
        server_name=server_name if client == "openclaw" else None,
        openclaw_command=str(openclaw_command) if client == "openclaw" else None,
    )
    originals = {
        path: path.read_bytes() if path.is_file() else None
        for path in (launcher, dashboard_launcher, metadata_path)
    }
    try:
        _atomic_write(
            dashboard_launcher,
            _render_dashboard_launcher(python_command, resolved_config),
            executable=True,
        )
        _atomic_write(
            launcher,
            _render_launcher(resolved_server, resolved_config),
            executable=True,
        )
        _atomic_write(
            metadata_path,
            json.dumps(installation.to_dict(), indent=2, ensure_ascii=False) + "\n",
            executable=False,
        )
    except Exception:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, content.decode("utf-8"), executable=path != metadata_path)
        raise
    return installation


def _render_launcher(server_command: Path, config_path: Path) -> str:
    if os.name == "nt":  # pragma: no cover - rendered on Windows installations
        server = str(server_command).replace("%", "%%")
        config = str(config_path).replace("%", "%%")
        return f'@echo off\r\nset "{CONFIG_ENV}={config}"\r\n"{server}" %*\r\n'
    return (
        "#!/bin/sh\n"
        f"export {CONFIG_ENV}={shlex.quote(str(config_path))}\n"
        f'exec {shlex.quote(str(server_command))} "$@"\n'
    )


def _render_dashboard_launcher(python_command: Path, config_path: Path) -> str:
    if os.name == "nt":  # pragma: no cover - rendered on Windows installations
        python = str(python_command).replace("'", "''")
        config = str(config_path).replace("'", "''")
        return (
            f"$env:{CONFIG_ENV} = '{config}'\r\n"
            f"& '{python}' -m helix_mcp_knowledge.dashboard_host @args\r\n"
            "exit $LASTEXITCODE\r\n"
        )
    return (
        "#!/bin/sh\n"
        f"export {CONFIG_ENV}={shlex.quote(str(config_path))}\n"
        f'exec {shlex.quote(str(python_command))} -m helix_mcp_knowledge.dashboard_host "$@"\n'
    )


def _atomic_write(path: Path, content: str, *, executable: bool) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        mode = stat.S_IRUSR | stat.S_IWUSR
        if executable:
            mode |= stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()

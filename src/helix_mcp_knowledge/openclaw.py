"""OpenClaw MCP registration without shell-specific configuration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import KnowledgeError
from .workspace import CONFIG_ENV

DEFAULT_SERVER_NAME = "helix_knowledge"
MINIMUM_RELOAD_TIMEOUT_SECONDS = 60
OPENCLAW_INTEGRATION_ERROR = "OPENCLAW_INTEGRATION_ERROR"
WINDOWS_BATCH_SUFFIXES = frozenset({".bat", ".cmd"})
WINDOWS_OPENCLAW_MODULE_CANDIDATES = (
    Path("node_modules/openclaw/openclaw.mjs"),
    Path("../openclaw/openclaw.mjs"),
)
EXPOSED_TOOLS = (
    "search_docs",
    "get_section",
    "list_products",
    "list_versions",
    "list_projects",
    "get_active_project",
    "set_active_project",
    "get_update_status",
    "get_sync_status",
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class OpenClawInstallation:
    server_name: str
    config: Path
    workspace: Path
    server_command: Path
    openclaw_command: Path
    probed: bool
    reloaded: bool
    registration_output: str
    reload_output: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "server_name": self.server_name,
            "config": str(self.config),
            "workspace": str(self.workspace),
            "server_command": str(self.server_command),
            "openclaw_command": str(self.openclaw_command),
            "tools": list(EXPOSED_TOOLS),
            "probed": self.probed,
            "reloaded": self.reloaded,
            "registration_output": self.registration_output,
            "reload_output": self.reload_output,
        }


def reload_openclaw(
    openclaw_command: str | Path = "openclaw",
    *,
    runner: CommandRunner = subprocess.run,
    timeout: int = 90,
) -> None:
    """Dispose OpenClaw's cached MCP runtimes after a validated configuration save."""

    if timeout < MINIMUM_RELOAD_TIMEOUT_SECONDS:
        raise KnowledgeError(
            f"OpenClaw MCP reload timeout must be at least {MINIMUM_RELOAD_TIMEOUT_SECONDS} seconds"
        )
    resolved_openclaw = _resolve_command(openclaw_command, label="OpenClaw")
    _run(
        [str(resolved_openclaw), "mcp", "reload"],
        runner=runner,
        timeout=timeout,
        action="OpenClaw MCP reload",
    )


def install_openclaw_server(
    *,
    config_path: Path,
    workspace: Path,
    server_name: str = DEFAULT_SERVER_NAME,
    openclaw_command: str | Path = "openclaw",
    server_command: str | Path | None = None,
    probe: bool = True,
    reload: bool = True,
    connect_timeout: int = 30,
    request_timeout: int = 60,
    runner: CommandRunner = subprocess.run,
) -> OpenClawInstallation:
    """Register or replace the packaged stdio server in OpenClaw."""

    normalized_name = server_name.strip()
    if not normalized_name:
        raise KnowledgeError("OpenClaw MCP server name cannot be empty")
    if connect_timeout <= 0 or request_timeout <= 0:
        raise KnowledgeError("OpenClaw MCP timeouts must be positive")

    resolved_config = config_path.expanduser().resolve()
    resolved_workspace = workspace.expanduser().resolve()
    if not resolved_config.is_file():
        raise KnowledgeError(f"configuration file not found: {resolved_config}")
    if not resolved_workspace.is_dir():
        raise KnowledgeError(f"workspace directory not found: {resolved_workspace}")

    resolved_openclaw = _resolve_command(openclaw_command, label="OpenClaw")
    resolved_server = _resolve_server_command(server_command)
    stdio_command, stdio_args = openclaw_stdio_invocation(resolved_server)
    command = [
        str(resolved_openclaw),
        "mcp",
        "add",
        normalized_name,
        "--command",
        str(stdio_command),
    ]
    for argument in stdio_args:
        command.extend(["--arg", argument])
    command.extend(
        [
            "--cwd",
            str(resolved_workspace),
            "--env",
            f"{CONFIG_ENV}={resolved_config}",
            "--include",
            ",".join(EXPOSED_TOOLS),
            "--connect-timeout",
            str(connect_timeout),
            "--timeout",
            str(request_timeout),
        ]
    )
    if not probe:
        command.append("--no-probe")

    registration = _execute(
        command,
        runner=runner,
        timeout=max(30, connect_timeout + request_timeout + 15),
        action="OpenClaw MCP registration",
    )
    previous_definition: dict[str, object] | None = None
    replacement = registration.returncode != 0
    if replacement:
        try:
            inspection = _run(
                [str(resolved_openclaw), "mcp", "show", normalized_name, "--json"],
                runner=runner,
                timeout=60,
                action="OpenClaw MCP definition inspection",
            )
            previous_definition = _parse_definition(inspection.stdout)
        except KnowledgeError:
            _raise_for_failure(registration, action="OpenClaw MCP registration")
        definition = {
            "command": str(stdio_command),
            "args": list(stdio_args),
            "cwd": str(resolved_workspace),
            "env": {CONFIG_ENV: str(resolved_config)},
            "connectionTimeoutMs": connect_timeout * 1000,
            "requestTimeoutMs": request_timeout * 1000,
            "toolFilter": {"include": list(EXPOSED_TOOLS)},
        }
        try:
            registration = _run(
                [
                    str(resolved_openclaw),
                    "mcp",
                    "set",
                    normalized_name,
                    json.dumps(definition, ensure_ascii=False, separators=(",", ":")),
                ],
                runner=runner,
                timeout=60,
                action="OpenClaw MCP replacement",
            )
        except Exception as exc:
            rollback = _restore_openclaw_definition(
                openclaw_command=resolved_openclaw,
                server_name=normalized_name,
                definition=previous_definition,
                reload=reload,
                runner=runner,
            )
            suffix = f"; rollback failed: {rollback}" if rollback else "; rollback succeeded"
            raise KnowledgeError(f"OpenClaw MCP replacement failed: {exc}{suffix}") from exc
    reload_output = None
    try:
        if reload:
            refreshed = _run(
                [str(resolved_openclaw), "mcp", "reload"],
                runner=runner,
                timeout=max(MINIMUM_RELOAD_TIMEOUT_SECONDS, connect_timeout + 30),
                action="OpenClaw MCP reload",
            )
            reload_output = _output(refreshed)
        if replacement and probe:
            _run(
                [str(resolved_openclaw), "mcp", "probe", normalized_name, "--json"],
                runner=runner,
                timeout=max(60, connect_timeout + request_timeout + 15),
                action="OpenClaw MCP replacement probe",
            )
    except Exception as exc:
        if not replacement:
            raise
        rollback = _restore_openclaw_definition(
            openclaw_command=resolved_openclaw,
            server_name=normalized_name,
            definition=previous_definition,
            reload=reload,
            runner=runner,
        )
        suffix = f"; rollback failed: {rollback}" if rollback else "; rollback succeeded"
        raise KnowledgeError(f"OpenClaw MCP replacement failed: {exc}{suffix}") from exc

    return OpenClawInstallation(
        server_name=normalized_name,
        config=resolved_config,
        workspace=resolved_workspace,
        server_command=resolved_server,
        openclaw_command=resolved_openclaw,
        probed=probe,
        reloaded=reload,
        registration_output=_output(registration),
        reload_output=reload_output,
    )


def _resolve_server_command(candidate: str | Path | None) -> Path:
    if candidate is not None:
        return _resolve_command(candidate, label="helix-mcp-knowledge-server")
    executable_name = "helix-mcp-knowledge-server"
    if os.name == "nt":
        executable_name += ".exe"
    adjacent = Path(sys.executable).parent.resolve() / executable_name
    if adjacent.is_file():
        return adjacent
    return _resolve_command(executable_name, label="helix-mcp-knowledge-server")


def openclaw_stdio_invocation(server_command: Path) -> tuple[Path, tuple[str, ...]]:
    """Return an OpenClaw-safe stdio command for the current operating system."""

    resolved_server = server_command.expanduser().resolve()
    if os.name != "nt" or resolved_server.suffix.casefold() not in WINDOWS_BATCH_SUFFIXES:
        return resolved_server, ()
    command_shell = os.environ.get("COMSPEC", "").strip()
    if not command_shell:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        command_shell = str(Path(system_root) / "System32" / "cmd.exe")
    resolved_shell = _resolve_command(command_shell, label="Windows command processor")
    return resolved_shell, ("/d", "/s", "/c", str(resolved_server))


def _resolve_command(candidate: str | Path, *, label: str) -> Path:
    rendered = str(candidate).strip()
    if not rendered:
        raise KnowledgeError(f"{label} command cannot be empty")
    path = Path(rendered).expanduser()
    if path.parent != Path(".") or path.is_absolute():
        resolved = path.resolve()
        if resolved.is_file():
            return resolved
        raise KnowledgeError(f"{label} command not found: {resolved}")
    discovered = shutil.which(rendered)
    if discovered:
        return Path(discovered).resolve()
    raise KnowledgeError(f"{label} command not found in PATH: {rendered}")


def _run(
    command: Sequence[str],
    *,
    runner: CommandRunner,
    timeout: int,
    action: str,
) -> subprocess.CompletedProcess[str]:
    completed = _execute(command, runner=runner, timeout=timeout, action=action)
    _raise_for_failure(completed, action=action)
    return completed


def _execute(
    command: Sequence[str],
    *,
    runner: CommandRunner,
    timeout: int,
    action: str,
) -> subprocess.CompletedProcess[str]:
    prepared_command = _prepare_command(command)
    try:
        completed = runner(
            prepared_command,
            check=False,
            capture_output=True,
            shell=False,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KnowledgeError(f"{action} timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise KnowledgeError(f"{action} could not start: {exc}") from exc
    return completed


def _raise_for_failure(completed: subprocess.CompletedProcess[str], *, action: str) -> None:
    if completed.returncode != 0:
        detail = _output(completed) or "no diagnostic output"
        raise KnowledgeError(f"{action} failed with exit code {completed.returncode}: {detail}")


def _parse_definition(value: str) -> dict[str, object]:
    try:
        definition = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise KnowledgeError("OpenClaw MCP definition did not return valid JSON") from exc
    if not isinstance(definition, dict):
        raise KnowledgeError("OpenClaw MCP definition must be a JSON object")
    return definition


def _restore_openclaw_definition(
    *,
    openclaw_command: Path,
    server_name: str,
    definition: dict[str, object] | None,
    reload: bool,
    runner: CommandRunner,
) -> str | None:
    if definition is None:
        return "previous definition is unavailable"
    try:
        _run(
            [
                str(openclaw_command),
                "mcp",
                "set",
                server_name,
                json.dumps(definition, ensure_ascii=False, separators=(",", ":")),
            ],
            runner=runner,
            timeout=60,
            action="OpenClaw MCP definition restore",
        )
        if reload:
            _run(
                [str(openclaw_command), "mcp", "reload"],
                runner=runner,
                timeout=MINIMUM_RELOAD_TIMEOUT_SECONDS,
                action="OpenClaw MCP rollback reload",
            )
    except Exception as exc:
        return str(exc)
    return None


def _prepare_command(command: Sequence[str]) -> list[str]:
    rendered = [str(item) for item in command]
    executable = Path(rendered[0])
    if os.name != "nt" or executable.suffix.casefold() not in WINDOWS_BATCH_SUFFIXES:
        return rendered

    # npm's batch shim needs a command processor, which would parse characters in
    # paths and JSON a second time.  Execute the package entry point through Node
    # instead so every value remains one literal subprocess argument.
    if executable.stem.casefold() == "openclaw":
        node = shutil.which("node.exe") or shutil.which("node")
        if node:
            for relative_module in WINDOWS_OPENCLAW_MODULE_CANDIDATES:
                module = executable.parent / relative_module
                if module.is_file():
                    return [
                        str(Path(node).resolve()),
                        str(module.resolve()),
                        *rendered[1:],
                    ]

    # PowerShell 7 preserves native argument boundaries. Windows PowerShell 5.1
    # does not reliably preserve quotes inside JSON when an npm .ps1 shim forwards
    # its arguments to Node, so it is deliberately not used as a fallback here.
    powershell = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not powershell:
        raise KnowledgeError(
            "the OpenClaw Windows launcher requires its npm module or PowerShell 7"
        )
    powershell_launcher = executable.with_suffix(".ps1")
    if not powershell_launcher.is_file() or powershell_launcher.is_symlink():
        raise KnowledgeError(
            "the OpenClaw Windows batch launcher requires its PowerShell companion: "
            f"{powershell_launcher}"
        )
    return [
        str(Path(powershell).resolve()),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(powershell_launcher.resolve()),
        *rendered[1:],
    ]


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        item.strip() for item in (completed.stdout, completed.stderr) if item and item.strip()
    )

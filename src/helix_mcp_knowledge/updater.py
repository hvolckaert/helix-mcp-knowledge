"""Transactional, versioned updates for managed MCP installations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig, load_config
from .errors import KnowledgeError
from .managed_installation import (
    DEFAULT_MANAGED_DASHBOARD_PORT,
    ManagedInstallation,
    activate_managed_installation,
    installation_metadata_path,
    load_managed_installation,
    stable_dashboard_launcher_path,
    stable_launcher_path,
)
from .openclaw import (
    DEFAULT_SERVER_NAME,
    EXPOSED_TOOLS,
    CommandRunner,
    _resolve_command,
    _run,
    openclaw_stdio_invocation,
)
from .storage_retention import RetentionResult, apply_storage_retention
from .update_lock import UpdateLock
from .workspace import CONFIG_ENV

DEFAULT_REPOSITORY = "hvolckaert/helix-mcp-knowledge"
RUNTIME_REQUIREMENTS_NAME = "runtime-requirements.txt"
LEGACY_COMPLETE_TOOL_FILTERS = (
    frozenset(
        {
            "search_docs",
            "get_section",
            "list_products",
            "list_versions",
            "list_projects",
            "get_active_project",
            "set_active_project",
            "get_update_status",
        }
    ),
    frozenset(
        {
            "search_docs",
            "get_section",
            "list_products",
            "list_versions",
            "list_projects",
            "get_active_project",
            "set_active_project",
        }
    ),
)
_VERSION_PATTERN = re.compile(r"^(?:v)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RERANKER_COMPONENT_INTRODUCED_VERSION = (1, 25, 0)


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    wheel_name: str
    sha256: str
    requirements_name: str
    requirements_sha256: str


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    venv: Path
    python: Path
    cli: Path
    server: Path


@dataclass(frozen=True)
class UpdateResult:
    status: str
    current_version: str
    target_version: str
    workspace: Path
    target_runtime: Path
    wheel: Path | None
    backup: Path | None
    sha256: str
    smoke_summary: dict[str, int] | None = None
    probed: bool = False
    reloaded: bool = False
    runtime_exists: bool = False
    client_integration: str = "standalone"
    storage_retention: RetentionResult | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "current_version": self.current_version,
            "target_version": self.target_version,
            "workspace": str(self.workspace),
            "target_runtime": str(self.target_runtime),
            "runtime_exists": self.runtime_exists,
            "wheel": str(self.wheel) if self.wheel else None,
            "sha256": self.sha256,
            "backup": str(self.backup) if self.backup else None,
            "smoke_summary": self.smoke_summary,
            "probed": self.probed,
            "reloaded": self.reloaded,
            "client_integration": self.client_integration,
            "previous_runtime_retained": True,
            "gateway_restart_required_for_existing_sessions": (
                self.status == "updated" and self.client_integration == "openclaw"
            ),
            "storage_retention": (
                self.storage_retention.to_dict() if self.storage_retention else None
            ),
        }


def update_installation(
    *,
    config_path: str | Path,
    repository: str = DEFAULT_REPOSITORY,
    target_version: str | None = None,
    openclaw_command: str | Path = "openclaw",
    gh_command: str | Path = "gh",
    server_name: str = DEFAULT_SERVER_NAME,
    probe: bool = True,
    reload: bool = True,
    dry_run: bool = False,
    resume: bool = False,
    allow_downgrade: bool = False,
    runner: CommandRunner = subprocess.run,
    current_version: str | None = None,
    current_python: str | Path | None = None,
    base_python: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
    manage_openclaw: bool | None = None,
    post_activation_check: Callable[[ManagedInstallation], None] | None = None,
) -> UpdateResult:
    """Install and activate a verified release while retaining rollback state."""

    normalized_repository = repository.strip()
    if not _REPOSITORY_PATTERN.fullmatch(normalized_repository):
        raise KnowledgeError("GitHub repository must use OWNER/REPOSITORY syntax")
    resolved_config = Path(config_path).expanduser().resolve()
    config = load_config(resolved_config)
    workspace = config.base_dir.resolve()
    managed = load_managed_installation(workspace)
    use_openclaw = (
        managed.client == "openclaw" if manage_openclaw is None and managed else manage_openclaw
    )
    if use_openclaw is None:
        use_openclaw = True  # Backward compatibility for pre-metadata OpenClaw installations.
    normalized_server_name = (
        managed.server_name if managed and managed.server_name else server_name
    ).strip()
    effective_openclaw_command: str | Path = (
        managed.openclaw_command
        if managed and managed.openclaw_command and openclaw_command == "openclaw"
        else openclaw_command
    )
    if use_openclaw and not normalized_server_name:
        raise KnowledgeError("OpenClaw MCP server name cannot be empty")
    installed_version = _normalize_version(current_version or __version__)
    installed_runtime = _runtime_paths(workspace, installed_version)
    resolved_current_python = Path(current_python or sys.executable).expanduser().resolve()
    _validate_managed_runtime(installed_runtime, resolved_current_python)

    resolved_openclaw = (
        _resolve_command(effective_openclaw_command, label="OpenClaw") if use_openclaw else None
    )
    resolved_gh = _resolve_command(gh_command, label="GitHub CLI")
    release = _resolve_release(
        resolved_gh,
        repository=normalized_repository,
        requested_version=target_version,
        runner=runner,
    )
    target_runtime = _runtime_paths(workspace, release.version)
    runtime_exists = target_runtime.root.exists()

    current_tuple = _version_tuple(installed_version)
    target_tuple = _version_tuple(release.version)
    if target_tuple == current_tuple:
        return UpdateResult(
            status="up_to_date",
            current_version=installed_version,
            target_version=release.version,
            workspace=workspace,
            target_runtime=target_runtime.root,
            runtime_exists=runtime_exists,
            wheel=None,
            backup=None,
            sha256=release.sha256,
            client_integration="openclaw" if use_openclaw else "standalone",
        )
    if target_tuple < current_tuple and not allow_downgrade:
        raise KnowledgeError(
            f"target version {release.version} is older than installed version "
            f"{installed_version}; pass --allow-downgrade to continue"
        )
    if runtime_exists and not resume:
        raise KnowledgeError(
            f"target runtime already exists: {target_runtime.root}; pass --resume to repair it"
        )

    if dry_run:
        return UpdateResult(
            status="planned",
            current_version=installed_version,
            target_version=release.version,
            workspace=workspace,
            target_runtime=target_runtime.root,
            runtime_exists=runtime_exists,
            wheel=workspace / "downloads" / release.version / release.wheel_name,
            backup=None,
            sha256=release.sha256,
            client_integration="openclaw" if use_openclaw else "standalone",
        )

    retention_clock = clock or (lambda: datetime.now(UTC))
    with UpdateLock(workspace / ".update.lock"):
        locked_installation = load_managed_installation(workspace)
        if (
            locked_installation is not None
            and locked_installation.active_version != installed_version
        ):
            raise KnowledgeError(
                "the active managed runtime changed while this update was being prepared; "
                "restart the update from the current runtime"
            )
        if target_runtime.root.exists() and not resume:
            raise KnowledgeError(
                f"target runtime already exists: {target_runtime.root}; pass --resume to repair it"
            )
        _ensure_database_idle(config.sqlite_path)
        previous_definition = None
        if use_openclaw:
            if resolved_openclaw is None:
                raise KnowledgeError("OpenClaw command was not resolved for this managed update")
            previous_definition = _show_server_definition(
                resolved_openclaw,
                server_name=normalized_server_name,
                runner=runner,
            )
            _validate_previous_definition(
                previous_definition,
                installed_runtime.server,
                stable_launcher_path(workspace),
            )
        wheel = _download_release(
            resolved_gh,
            workspace=workspace,
            repository=normalized_repository,
            release=release,
            runner=runner,
        )
        requirements = _download_release_asset(
            resolved_gh,
            workspace=workspace,
            repository=normalized_repository,
            release=release,
            asset_name=release.requirements_name,
            expected_sha256=release.requirements_sha256,
            runner=runner,
        )
        _install_runtime(
            target_runtime,
            wheel=wheel,
            requirements=requirements,
            base_python=Path(base_python or getattr(sys, "_base_executable", sys.executable)),
            resume=resume,
            runner=runner,
        )
        _verify_installed_version(
            target_runtime.python,
            expected=release.version,
            runner=runner,
        )
        target_tools = (
            _read_target_tools(target_runtime.python, runner=runner) if use_openclaw else ()
        )

        backup = _create_backup(
            workspace=workspace,
            config_path=resolved_config,
            database_path=config.sqlite_path,
            previous_definition=previous_definition,
            current_version=installed_version,
            target_version=release.version,
            clock=retention_clock,
        )

        activation_attempted = False
        registration_attempted = False
        reranker_stop_attempted = False
        restore_reranker_after_rollback = _reranker_restore_required_for_failed_downgrade(
            config=config,
            current_version=current_tuple,
            target_version=target_tuple,
        )
        try:
            smoke_summary = _run_smoke_test(
                target_runtime.cli,
                config_path=resolved_config,
                runner=runner,
            )
            reranker_stop_attempted = _downgrade_crosses_reranker_boundary(
                current_version=current_tuple,
                target_version=target_tuple,
            )
            _stop_unsupported_optional_services_for_downgrade(
                config=config,
                current_version=current_tuple,
                target_version=target_tuple,
            )
            activation_attempted = True
            activated = activate_managed_installation(
                workspace=workspace,
                version=release.version,
                server_command=target_runtime.server,
                config_path=resolved_config,
                client="openclaw" if use_openclaw else "standalone",
                server_name=normalized_server_name if use_openclaw else None,
                openclaw_command=resolved_openclaw if use_openclaw else None,
                dashboard_port=(
                    managed.dashboard_port if managed else DEFAULT_MANAGED_DASHBOARD_PORT
                ),
            )
            if use_openclaw:
                if resolved_openclaw is None or previous_definition is None:
                    raise KnowledgeError(
                        "OpenClaw state changed while the managed runtime was being activated"
                    )
                target_definition = _target_definition(
                    previous_definition,
                    server_command=activated.launcher,
                    workspace=workspace,
                    config_path=resolved_config,
                    target_tools=target_tools,
                )
                expected_tools = _expected_exposed_tools(target_definition, target_tools)
                registration_attempted = True
                _set_server_definition(
                    resolved_openclaw,
                    server_name=normalized_server_name,
                    definition=target_definition,
                    runner=runner,
                )
                if reload:
                    _reload_openclaw(resolved_openclaw, runner=runner)
                if probe:
                    _probe_openclaw(
                        resolved_openclaw,
                        server_name=normalized_server_name,
                        target_server=activated.launcher,
                        expected_tools=expected_tools,
                        runner=runner,
                    )
            if post_activation_check is not None:
                post_activation_check(activated)
            _write_success_manifest(
                backup,
                current_version=installed_version,
                target_version=release.version,
                runtime=target_runtime.root,
                wheel=wheel,
                sha256=release.sha256,
            )
        except Exception as exc:
            rollback_error = _rollback(
                openclaw_command=resolved_openclaw,
                server_name=normalized_server_name,
                previous_definition=previous_definition,
                config_path=resolved_config,
                database_path=config.sqlite_path,
                backup=backup,
                restore_registration=registration_attempted,
                restore_managed_installation=activation_attempted,
                restore_reranker_service=(
                    restore_reranker_after_rollback and reranker_stop_attempted
                ),
                config=config,
                reload=reload and use_openclaw,
                runner=runner,
            )
            manifest_error = _write_failure_manifest(
                backup,
                current_version=installed_version,
                target_version=release.version,
                error=exc,
                rollback_error=rollback_error,
                failed_at=retention_clock(),
            )
            detail = (
                f"; rollback failed: {rollback_error}" if rollback_error else "; rollback succeeded"
            )
            if manifest_error:
                detail += f"; failure manifest could not be written: {manifest_error}"
            raise KnowledgeError(f"update to {release.version} failed: {exc}{detail}") from exc

        storage_retention = apply_storage_retention(
            workspace=workspace,
            settings=config.updates.retention,
            active_version=release.version,
            previous_version=installed_version,
            now=retention_clock(),
        )

        return UpdateResult(
            status="updated",
            current_version=installed_version,
            target_version=release.version,
            workspace=workspace,
            target_runtime=target_runtime.root,
            runtime_exists=runtime_exists,
            wheel=wheel,
            backup=backup,
            sha256=release.sha256,
            smoke_summary=smoke_summary,
            probed=probe and use_openclaw,
            reloaded=reload and use_openclaw,
            client_integration="openclaw" if use_openclaw else "standalone",
            storage_retention=storage_retention,
        )


def _stop_unsupported_optional_services_for_downgrade(
    *,
    config: AppConfig,
    current_version: tuple[int, int, int],
    target_version: tuple[int, int, int],
) -> None:
    """Stop detached workers that the selected older runtime cannot manage."""

    if not _downgrade_crosses_reranker_boundary(
        current_version=current_version,
        target_version=target_version,
    ):
        return
    # Import lazily so normal updates and the dependency-light base runtime do
    # not load optional-component lifecycle code.
    from .reranker_component import RerankerComponentManager

    RerankerComponentManager(config).stop_service()


def _downgrade_crosses_reranker_boundary(
    *,
    current_version: tuple[int, int, int],
    target_version: tuple[int, int, int],
) -> bool:
    return (
        current_version >= _RERANKER_COMPONENT_INTRODUCED_VERSION
        and target_version < _RERANKER_COMPONENT_INTRODUCED_VERSION
    )


def _reranker_restore_required_for_failed_downgrade(
    *,
    config: AppConfig,
    current_version: tuple[int, int, int],
    target_version: tuple[int, int, int],
) -> bool:
    """Remember whether rollback must restore the reranker's desired state."""

    if not config.retrieval.reranker.enabled or not _downgrade_crosses_reranker_boundary(
        current_version=current_version,
        target_version=target_version,
    ):
        return False
    from .reranker_client import RERANKER_MODEL_ID

    return config.retrieval.reranker.model == RERANKER_MODEL_ID


def _normalize_version(value: str) -> str:
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        raise KnowledgeError(f"unsupported release version: {value!r}; expected MAJOR.MINOR.PATCH")
    return ".".join(match.groups())


def _version_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(item) for item in _normalize_version(value).split("."))  # type: ignore[return-value]


def _runtime_paths(workspace: Path, version: str) -> RuntimePaths:
    runtime_parent = workspace / "runtime"
    root = runtime_parent / version
    venv = root / "venv"
    executable_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    return RuntimePaths(
        root=root,
        venv=venv,
        python=executable_dir / f"python{suffix}",
        cli=executable_dir / f"helix-mcp-knowledge{suffix}",
        server=executable_dir / f"helix-mcp-knowledge-server{suffix}",
    )


def _validate_managed_runtime(runtime: RuntimePaths, current_python: Path) -> None:
    if _is_link(runtime.root.parent):
        raise KnowledgeError(f"runtime root cannot be a link: {runtime.root.parent}")
    if _is_link(runtime.root):
        raise KnowledgeError(f"installed runtime cannot be a link: {runtime.root}")
    if not runtime.server.is_file():
        raise KnowledgeError(
            f"installed runtime is not version-managed: server not found at {runtime.server}"
        )
    if current_python != runtime.python.resolve():
        raise KnowledgeError(
            "run update from the active versioned runtime: "
            f"expected {runtime.python}, found {current_python}"
        )


def _resolve_release(
    gh_command: Path,
    *,
    repository: str,
    requested_version: str | None,
    runner: CommandRunner,
) -> Release:
    command = [str(gh_command), "release", "view"]
    if requested_version:
        command.append(f"v{_normalize_version(requested_version)}")
    command.extend(["--repo", repository, "--json", "tagName,isDraft,isPrerelease,assets"])
    completed = _run(
        command,
        runner=runner,
        timeout=60,
        action="GitHub release discovery",
    )
    payload = _parse_json(completed.stdout, label="GitHub release metadata")
    if payload.get("isDraft") or payload.get("isPrerelease"):
        raise KnowledgeError("only stable, published GitHub releases can be installed")
    tag = str(payload.get("tagName", ""))
    version = _normalize_version(tag)
    if requested_version and version != _normalize_version(requested_version):
        raise KnowledgeError(
            f"GitHub returned release {tag} for requested version {requested_version}"
        )
    wheel_name = f"helix_mcp_knowledge-{version}-py3-none-any.whl"
    sha256 = _release_asset_sha256(payload, asset_name=wheel_name, tag=tag)
    requirements_sha256 = _release_asset_sha256(
        payload,
        asset_name=RUNTIME_REQUIREMENTS_NAME,
        tag=tag,
    )
    return Release(
        version=version,
        tag=tag,
        wheel_name=wheel_name,
        sha256=sha256,
        requirements_name=RUNTIME_REQUIREMENTS_NAME,
        requirements_sha256=requirements_sha256,
    )


def _release_asset_sha256(payload: dict[str, Any], *, asset_name: str, tag: str) -> str:
    assets = [item for item in payload.get("assets", []) if item.get("name") == asset_name]
    if len(assets) != 1:
        raise KnowledgeError(f"release {tag} must contain exactly one {asset_name} asset")
    digest = str(assets[0].get("digest", ""))
    algorithm, separator, value = digest.partition(":")
    sha256 = value.casefold() if separator and algorithm.casefold() == "sha256" else ""
    if not _SHA256_PATTERN.fullmatch(sha256):
        raise KnowledgeError(
            f"release {tag} does not expose a valid SHA-256 digest for {asset_name}"
        )
    return sha256


def _download_release(
    gh_command: Path,
    *,
    workspace: Path,
    repository: str,
    release: Release,
    runner: CommandRunner,
) -> Path:
    return _download_release_asset(
        gh_command,
        workspace=workspace,
        repository=repository,
        release=release,
        asset_name=release.wheel_name,
        expected_sha256=release.sha256,
        runner=runner,
    )


def _download_release_asset(
    gh_command: Path,
    *,
    workspace: Path,
    repository: str,
    release: Release,
    asset_name: str,
    expected_sha256: str,
    runner: CommandRunner,
) -> Path:
    download_root = workspace / "downloads"
    if _is_link(download_root):
        raise KnowledgeError(f"download root cannot be a link: {download_root}")
    download_dir = download_root / release.version
    if _is_link(download_dir):
        raise KnowledgeError(f"release download directory cannot be a link: {download_dir}")
    destination = download_dir / asset_name
    download_dir.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        _verify_sha256(destination, expected_sha256)
        return destination
    if destination.exists():
        raise KnowledgeError(f"release asset destination is not a regular file: {destination}")
    with tempfile.TemporaryDirectory(prefix=".download-", dir=download_dir) as temporary:
        temporary_dir = Path(temporary)
        _run(
            [
                str(gh_command),
                "release",
                "download",
                release.tag,
                "--repo",
                repository,
                "--pattern",
                asset_name,
                "--dir",
                str(temporary_dir),
            ],
            runner=runner,
            timeout=300,
            action="GitHub release download",
        )
        downloaded = temporary_dir / asset_name
        if not downloaded.is_file():
            raise KnowledgeError(
                f"GitHub CLI did not download the expected release asset: {downloaded}"
            )
        _verify_sha256(downloaded, expected_sha256)
        os.replace(downloaded, destination)
    return destination


def _verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != expected:
        raise KnowledgeError(f"release asset digest mismatch: expected {expected}, found {actual}")


def _install_runtime(
    runtime: RuntimePaths,
    *,
    wheel: Path,
    requirements: Path,
    base_python: Path,
    resume: bool,
    runner: CommandRunner,
) -> None:
    resolved_base_python = _resolve_command(base_python, label="base Python")
    if _is_link(runtime.root.parent):
        raise KnowledgeError(f"runtime root cannot be a link: {runtime.root.parent}")
    if _is_link(runtime.root):
        raise KnowledgeError(f"target runtime cannot be a link: {runtime.root}")
    if _is_link(runtime.venv):
        raise KnowledgeError(f"target virtual environment cannot be a link: {runtime.venv}")
    if runtime.root.exists() and not runtime.root.is_dir():
        raise KnowledgeError(f"target runtime is not a directory: {runtime.root}")
    runtime.root.mkdir(parents=True, exist_ok=True)
    if not runtime.python.is_file():
        _run(
            [str(resolved_base_python), "-m", "venv", str(runtime.venv)],
            runner=runner,
            timeout=300,
            action="target virtual environment creation",
        )
    _run(
        [
            str(runtime.python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "--requirement",
            str(requirements),
        ],
        runner=runner,
        timeout=900,
        action="locked runtime dependency installation",
    )
    install_command = [
        str(runtime.python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
    ]
    if resume:
        install_command.append("--force-reinstall")
    install_command.append(str(wheel))
    _run(
        install_command,
        runner=runner,
        timeout=900,
        action="target package installation",
    )
    _run(
        [str(runtime.python), "-m", "pip", "check"],
        runner=runner,
        timeout=120,
        action="target dependency validation",
    )
    for label, path in (("CLI", runtime.cli), ("MCP server", runtime.server)):
        if not path.is_file():
            raise KnowledgeError(f"target {label} entry point was not installed: {path}")


def _verify_installed_version(
    python: Path,
    *,
    expected: str,
    runner: CommandRunner,
) -> None:
    completed = _run(
        [
            str(python),
            "-c",
            "from importlib.metadata import version; print(version('helix-mcp-knowledge'))",
        ],
        runner=runner,
        timeout=30,
        action="target version validation",
    )
    actual = completed.stdout.strip()
    if actual != expected:
        raise KnowledgeError(f"installed version mismatch: expected {expected}, found {actual}")


def _read_target_tools(python: Path, *, runner: CommandRunner) -> tuple[str, ...]:
    completed = _run(
        [
            str(python),
            "-c",
            (
                "import json; "
                "from helix_mcp_knowledge.openclaw import EXPOSED_TOOLS; "
                "print(json.dumps(EXPOSED_TOOLS))"
            ),
        ],
        runner=runner,
        timeout=30,
        action="target MCP tool contract inspection",
    )
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise KnowledgeError("target MCP tool contract did not return valid JSON") from exc
    if (
        not isinstance(payload, list)
        or not payload
        or any(not isinstance(item, str) or not item.strip() for item in payload)
        or len(payload) != len(set(payload))
    ):
        raise KnowledgeError("target MCP tool contract must be a unique non-empty string list")
    return tuple(payload)


def _show_server_definition(
    openclaw_command: Path,
    *,
    server_name: str,
    runner: CommandRunner,
) -> dict[str, Any]:
    completed = _run(
        [str(openclaw_command), "mcp", "show", server_name, "--json"],
        runner=runner,
        timeout=60,
        action="OpenClaw MCP definition inspection",
    )
    payload = _parse_json(completed.stdout, label="OpenClaw MCP definition")
    if not isinstance(payload, dict):
        raise KnowledgeError("OpenClaw MCP definition must be a JSON object")
    return payload


def _validate_previous_definition(
    definition: dict[str, Any], current_server: Path, stable_launcher: Path
) -> None:
    command = definition.get("command")
    arguments = definition.get("args")
    normalized_arguments = (
        ()
        if arguments is None
        else (
            tuple(arguments)
            if isinstance(arguments, list) and all(isinstance(item, str) for item in arguments)
            else None
        )
    )
    allowed_commands = {current_server.resolve(), stable_launcher.resolve()}
    direct_definition = (
        isinstance(command, str)
        and Path(command).expanduser().resolve() in allowed_commands
        and normalized_arguments == ()
    )
    wrapper_command, wrapper_arguments = openclaw_stdio_invocation(stable_launcher)
    wrapped_definition = (
        isinstance(command, str)
        and Path(command).expanduser().resolve() == wrapper_command
        and normalized_arguments == wrapper_arguments
    )
    if not direct_definition and not wrapped_definition:
        raise KnowledgeError(
            "the configured OpenClaw server does not point to this managed installation"
        )


def _target_definition(
    previous: dict[str, Any],
    *,
    server_command: Path,
    workspace: Path,
    config_path: Path,
    target_tools: tuple[str, ...],
) -> dict[str, Any]:
    definition = deepcopy(previous)
    stdio_command, stdio_arguments = openclaw_stdio_invocation(server_command)
    definition["command"] = str(stdio_command)
    definition["args"] = list(stdio_arguments)
    definition["cwd"] = str(workspace.resolve())
    environment = dict(definition.get("env") or {})
    environment[CONFIG_ENV] = str(config_path.resolve())
    definition["env"] = environment
    tool_filter = definition.get("toolFilter")
    if isinstance(tool_filter, dict) and isinstance(tool_filter.get("include"), list):
        included = [str(value) for value in tool_filter["include"]]
        complete_contracts = (frozenset(EXPOSED_TOOLS), *LEGACY_COMPLETE_TOOL_FILTERS)
        if any(contract.issubset(included) for contract in complete_contracts):
            tool_filter["include"] = [
                *included,
                *(tool for tool in target_tools if tool not in included),
            ]
    return definition


def _expected_exposed_tools(
    definition: dict[str, Any], target_tools: tuple[str, ...]
) -> tuple[str, ...]:
    tool_filter = definition.get("toolFilter")
    if not isinstance(tool_filter, dict) or not isinstance(tool_filter.get("include"), list):
        return target_tools
    included = {str(value) for value in tool_filter["include"]}
    return tuple(tool for tool in target_tools if tool in included)


def _set_server_definition(
    openclaw_command: Path,
    *,
    server_name: str,
    definition: dict[str, Any],
    runner: CommandRunner,
) -> None:
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
        action="OpenClaw MCP atomic switch",
    )


def _reload_openclaw(openclaw_command: Path, *, runner: CommandRunner) -> None:
    _run(
        [str(openclaw_command), "mcp", "reload"],
        runner=runner,
        timeout=90,
        action="OpenClaw MCP reload",
    )


def _probe_openclaw(
    openclaw_command: Path,
    *,
    server_name: str,
    target_server: Path,
    expected_tools: tuple[str, ...],
    runner: CommandRunner,
) -> None:
    completed = _run(
        [str(openclaw_command), "mcp", "probe", server_name, "--json"],
        runner=runner,
        timeout=120,
        action="OpenClaw MCP probe",
    )
    payload = _parse_json(completed.stdout, label="OpenClaw MCP probe")
    diagnostics = payload.get("diagnostics")
    if diagnostics:
        raise KnowledgeError(f"OpenClaw MCP probe returned diagnostics: {diagnostics}")
    expected_names = {f"{server_name}__{tool}" for tool in expected_tools}
    actual_tools = set(payload.get("tools") or [])
    if actual_tools != expected_names:
        raise KnowledgeError(
            f"OpenClaw MCP tool mismatch: expected {sorted(expected_names)}, "
            f"found {sorted(actual_tools)}"
        )
    server = (payload.get("servers") or {}).get(server_name) or {}
    launch = str(server.get("launch", ""))
    if str(target_server.resolve()) not in launch:
        raise KnowledgeError("OpenClaw MCP probe did not launch the target runtime")


def _run_smoke_test(
    cli: Path,
    *,
    config_path: Path,
    runner: CommandRunner,
) -> dict[str, int]:
    completed = _run(
        [str(cli), "--config", str(config_path), "smoke-test"],
        runner=runner,
        timeout=180,
        action="target smoke test",
    )
    payload = _parse_json(completed.stdout, label="target smoke test")
    if payload.get("status") != "pass":
        raise KnowledgeError("target smoke test did not pass")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise KnowledgeError("target smoke test did not return a summary")
    return {str(key): int(value) for key, value in summary.items()}


def _ensure_database_idle(database_path: Path) -> None:
    if not database_path.is_file():
        raise KnowledgeError(f"SQLite database not found: {database_path}")
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            active_owners = {
                str(row[0])
                for row in connection.execute(
                    "SELECT owner_id FROM automation_leases WHERE expires_at > ?", (time.time(),)
                ).fetchall()
            }
            states = connection.execute(
                """SELECT state_json FROM automation_state
                   WHERE job_id = 'official-docs' OR job_id LIKE 'project:%'"""
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return
        else:
            raise KnowledgeError(f"could not inspect SQLite update leases: {exc}") from exc
    try:
        active_jobs = [
            state
            for (raw_state,) in states
            if isinstance((state := json.loads(raw_state)), dict)
            and state.get("status") == "running"
            and (not isinstance(state.get("owner_id"), str) or state["owner_id"] in active_owners)
        ]
    except (TypeError, json.JSONDecodeError) as exc:
        raise KnowledgeError("could not parse SQLite automation state") from exc
    if active_jobs:
        raise KnowledgeError("documentation synchronization is active; retry after it finishes")


def _create_backup(
    *,
    workspace: Path,
    config_path: Path,
    database_path: Path,
    previous_definition: dict[str, Any] | None,
    current_version: str,
    target_version: str,
    clock: Callable[[], datetime],
) -> Path:
    stamp = clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_root = workspace / "backups"
    if _is_link(backup_root):
        raise KnowledgeError(f"backup root cannot be a link: {backup_root}")
    backup = _unique_directory(
        backup_root / f"update-{stamp}-{current_version}-to-{target_version}"
    )
    backup.mkdir(parents=True, mode=0o700)
    config_backup = backup / "config.yaml"
    database_backup = backup / "helix_mcp_knowledge.db"
    shutil.copy2(config_path, config_backup)
    _backup_database(database_path, database_backup)
    protected_paths = [config_backup, database_backup]
    if previous_definition is not None:
        definition_backup = backup / "openclaw-server.json"
        definition_backup.write_text(
            json.dumps(previous_definition, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        protected_paths.append(definition_backup)
    launcher = stable_launcher_path(workspace)
    dashboard_launcher = stable_dashboard_launcher_path(workspace)
    metadata = installation_metadata_path(workspace)
    if launcher.is_file():
        shutil.copy2(launcher, backup / "stable-launcher")
        protected_paths.append(backup / "stable-launcher")
    if dashboard_launcher.is_file():
        shutil.copy2(dashboard_launcher, backup / "stable-dashboard-launcher")
        protected_paths.append(backup / "stable-dashboard-launcher")
    if metadata.is_file():
        shutil.copy2(metadata, backup / "installation.json")
        protected_paths.append(backup / "installation.json")
    (backup / "managed-files.json").write_text(
        json.dumps(
            {
                "launcher_existed": launcher.is_file(),
                "dashboard_launcher_existed": dashboard_launcher.is_file(),
                "metadata_existed": metadata.is_file(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    protected_paths.append(backup / "managed-files.json")
    for path in protected_paths:
        path.chmod(0o600)
    return backup


def _unique_directory(candidate: Path) -> Path:
    if not candidate.exists():
        return candidate
    for suffix in range(2, 1000):
        alternative = candidate.with_name(f"{candidate.name}-{suffix}")
        if not alternative.exists():
            return alternative
    raise KnowledgeError(f"could not allocate a unique backup directory under {candidate.parent}")


def _backup_database(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as source_connection,
        closing(sqlite3.connect(destination)) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def _restore_database(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as source_connection,
        closing(sqlite3.connect(destination)) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def _rollback(
    *,
    openclaw_command: Path | None,
    server_name: str,
    previous_definition: dict[str, Any] | None,
    config_path: Path,
    database_path: Path,
    backup: Path,
    restore_registration: bool,
    restore_managed_installation: bool,
    restore_reranker_service: bool,
    config: AppConfig,
    reload: bool,
    runner: CommandRunner,
) -> str | None:
    failures: list[str] = []
    try:
        shutil.copy2(backup / "config.yaml", config_path)
        _restore_database(backup / "helix_mcp_knowledge.db", database_path)
    except Exception as exc:
        failures.append(f"data restore: {exc}")
    if restore_managed_installation:
        try:
            _restore_managed_files(backup, config_path.parent.parent)
        except Exception as exc:
            failures.append(f"managed launcher restore: {exc}")
    if restore_registration:
        try:
            if openclaw_command is None or previous_definition is None:
                raise KnowledgeError("OpenClaw rollback state is incomplete")
            _set_server_definition(
                openclaw_command,
                server_name=server_name,
                definition=previous_definition,
                runner=runner,
            )
            if reload:
                _reload_openclaw(openclaw_command, runner=runner)
        except Exception as exc:
            failures.append(f"OpenClaw restore: {exc}")
    if restore_reranker_service:
        try:
            _restore_reranker_service_after_rollback(config)
        except Exception as exc:
            failures.append(f"reranker service restore: {exc}")
    return "; ".join(failures) or None


def _restore_reranker_service_after_rollback(config: AppConfig) -> None:
    """Undo a downgrade stop after the older runtime failed to activate."""

    from .reranker_component import RerankerComponentManager

    manager = RerankerComponentManager(config)
    # Clearing the durable stop request is necessary even when the component is
    # absent or already degraded; otherwise the restored runtime cannot recover
    # it later without an administrator toggling the setting.
    manager.request_service_start()
    status = manager.status()
    if status.installed and status.status == "ready":
        manager.ensure_service()


def _restore_managed_files(backup: Path, workspace: Path) -> None:
    state = json.loads((backup / "managed-files.json").read_text(encoding="utf-8"))
    launcher = stable_launcher_path(workspace)
    dashboard_launcher = stable_dashboard_launcher_path(workspace)
    metadata = installation_metadata_path(workspace)
    for destination, backup_name, existed_key in (
        (launcher, "stable-launcher", "launcher_existed"),
        (
            dashboard_launcher,
            "stable-dashboard-launcher",
            "dashboard_launcher_existed",
        ),
        (metadata, "installation.json", "metadata_existed"),
    ):
        existed = bool(state.get(existed_key))
        if existed:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / backup_name, destination)
        elif destination.is_file():
            destination.unlink()


def _write_success_manifest(
    backup: Path,
    *,
    current_version: str,
    target_version: str,
    runtime: Path,
    wheel: Path,
    sha256: str,
) -> None:
    manifest = backup / "update-result.json"
    manifest.write_text(
        json.dumps(
            {
                "status": "updated",
                "current_version": current_version,
                "target_version": target_version,
                "runtime": str(runtime),
                "wheel": str(wheel),
                "sha256": sha256,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)


def _write_failure_manifest(
    backup: Path,
    *,
    current_version: str,
    target_version: str,
    error: Exception,
    rollback_error: str | None,
    failed_at: datetime,
) -> str | None:
    manifest = backup / "update-result.json"
    try:
        manifest.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "current_version": current_version,
                    "target_version": target_version,
                    "failed_at": failed_at.astimezone(UTC).isoformat(),
                    "error": str(error)[:1000],
                    "rollback": "failed" if rollback_error else "succeeded",
                    "rollback_error": rollback_error,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        manifest.chmod(0o600)
    except OSError as exc:
        return str(exc)
    return None


def _parse_json(value: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise KnowledgeError(f"{label} did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise KnowledgeError(f"{label} must be a JSON object")
    return payload


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (
        bool(path.is_junction()) if hasattr(path, "is_junction") else False
    )

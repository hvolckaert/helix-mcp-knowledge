"""User workspace discovery and first-run initialization."""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from platformdirs import user_data_path

from .errors import ConfigurationError

APP_NAME = "helix-mcp-knowledge"
CONFIG_ENV = "HELIX_KNOWLEDGE_CONFIG"
_RESOURCE_FILES = {
    "config/config.yaml": "config/config.yaml",
    "config/sources/bmc-official-26.1.yaml": "config/sources/bmc-official-26.1.yaml",
    "config/projects/example.yaml.example": "config/projects/example.yaml.example",
    "config/projects/example.sources.yaml.example": (
        "config/projects/example.sources.yaml.example"
    ),
}
_DATA_DIRECTORIES = (
    "data/cache",
    "data/errors",
    "data/sources/bmc/official",
    "data/sources/projects",
    "data/sqlite",
)


@dataclass(frozen=True)
class WorkspaceInitialization:
    workspace: Path
    config: Path
    created: tuple[Path, ...]
    overwritten: tuple[Path, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace": str(self.workspace),
            "config": str(self.config),
            "created": [str(path) for path in self.created],
            "overwritten": [str(path) for path in self.overwritten],
            "next_steps": [
                f"helix-mcp-knowledge --config {self.config} configure",
                f"helix-mcp-knowledge --config {self.config} init-db",
            ],
        }


def default_workspace_path() -> Path:
    """Return the platform-native per-user data directory."""

    return user_data_path(APP_NAME, appauthor=False)


def discover_config_path(
    explicit: str | Path | None = None,
    *,
    cwd: Path | None = None,
) -> Path:
    """Resolve config precedence: explicit, environment, checkout, user workspace."""

    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    environment_path = os.environ.get(CONFIG_ENV)
    if environment_path:
        return Path(environment_path).expanduser().resolve()
    local_config = ((cwd or Path.cwd()) / "config/config.yaml").resolve()
    if local_config.is_file():
        return local_config
    return (default_workspace_path() / "config/config.yaml").resolve()


def initialize_workspace(
    workspace: str | Path | None = None,
    *,
    force: bool = False,
) -> WorkspaceInitialization:
    """Materialize packaged configuration and safe empty data directories."""

    root = (
        Path(workspace).expanduser().resolve() if workspace else default_workspace_path().resolve()
    )
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    destinations = {relative: root / relative for relative in _RESOURCE_FILES}
    existing = tuple(path for path in destinations.values() if path.exists())
    if existing and not force:
        rendered = ", ".join(str(path) for path in existing)
        raise ConfigurationError(
            f"workspace configuration already exists: {rendered}; use --force to overwrite it"
        )

    created: list[Path] = []
    overwritten: list[Path] = []
    for relative, resource_relative in _RESOURCE_FILES.items():
        destination = destinations[relative]
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = read_packaged_resource(resource_relative)
        if destination.exists():
            overwritten.append(destination)
        else:
            created.append(destination)
        destination.write_bytes(content)
        if os.name != "nt":
            destination.chmod(0o600)
    for relative in _DATA_DIRECTORIES:
        directory = root / relative
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)

    return WorkspaceInitialization(
        workspace=root,
        config=destinations["config/config.yaml"],
        created=tuple(created),
        overwritten=tuple(overwritten),
    )


def missing_config_message(path: Path) -> str:
    hint = "run 'helix-mcp-knowledge init' to create a user workspace"
    return f"configuration file not found: {path}; {hint}"


def read_packaged_resource(relative: str) -> bytes:
    """Read one immutable workspace seed bundled with the current runtime."""
    resource = files("helix_mcp_knowledge").joinpath("resources", *Path(relative).parts)
    if resource.is_file():
        return resource.read_bytes()

    # Editable source trees use the canonical repository manifests. Hatch force-includes
    # these same files in built wheels, so installed distributions never need this fallback.
    repository_resource = Path(__file__).resolve().parents[2] / relative
    if repository_resource.is_file():
        return repository_resource.read_bytes()
    raise ConfigurationError(f"packaged workspace resource is missing: {relative}")


def secure_workspace_metadata(
    *,
    config_path: Path,
    projects_path: Path,
    errors_path: Path,
    workspace_path: Path | None = None,
    data_path: Path | None = None,
    sources_path: Path | None = None,
) -> None:
    """Restrict managed workspace metadata and parent directories on POSIX."""

    if os.name == "nt":
        return
    directories = [projects_path, errors_path]
    directories.extend(
        directory
        for directory in (workspace_path, data_path, sources_path)
        if directory is not None
    )
    for directory in directories:
        if (
            directory.is_dir()
            and not directory.is_symlink()
            and directory.stat().st_mode & 0o777 != 0o700
        ):
            directory.chmod(0o700)
    candidates = [config_path]
    for directory in (projects_path, errors_path):
        if directory.is_dir() and not directory.is_symlink():
            candidates.extend(
                path for path in directory.iterdir() if path.is_file() and not path.is_symlink()
            )
    for path in candidates:
        if path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o777 != 0o600:
            path.chmod(0o600)


def secure_managed_project_documents(*, sources_path: Path, documents_paths: list[Path]) -> None:
    """Restrict project documents managed inside the workspace without touching external roots."""

    if os.name == "nt" or not sources_path.is_dir() or sources_path.is_symlink():
        return
    resolved_sources = sources_path.resolve()
    for documents_path in documents_paths:
        try:
            resolved_documents = documents_path.resolve()
            resolved_documents.relative_to(resolved_sources)
        except (OSError, ValueError):
            continue
        if not resolved_documents.is_dir() or resolved_documents.is_symlink():
            continue
        for root, directory_names, file_names in os.walk(
            resolved_documents, topdown=True, followlinks=False
        ):
            current = Path(root)
            directory_names[:] = [
                name for name in directory_names if not (current / name).is_symlink()
            ]
            if current.stat().st_mode & 0o777 != 0o700:
                current.chmod(0o700)
            for name in file_names:
                path = current / name
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.stat().st_mode & 0o777 != 0o600
                ):
                    path.chmod(0o600)

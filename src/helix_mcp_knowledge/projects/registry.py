"""Immutable registry built from project YAML files."""

from __future__ import annotations

from pathlib import Path

from ..catalog.products import ProductCatalog
from ..config import AppConfig
from ..errors import ConfigurationError, ProjectDisabledError, ProjectNotFoundError
from ..models.project import Project, ProjectStatus
from .loader import LoadedProject, load_project


class ProjectRegistry:
    def __init__(self, projects: dict[str, LoadedProject]) -> None:
        self._projects = dict(projects)

    @classmethod
    def load(cls, config: AppConfig, catalog: ProductCatalog) -> ProjectRegistry:
        directory = config.projects_config_path
        if not directory.exists():
            return cls({})
        if not directory.is_dir():
            raise ConfigurationError(f"projects_config is not a directory: {directory}")

        loaded: dict[str, LoadedProject] = {}
        paths = project_config_paths(directory)
        for path in paths:
            item = load_project(path, config, catalog)
            if item.project.id in loaded:
                raise ConfigurationError(f"duplicate project id: {item.project.id}")
            loaded[item.project.id] = item
        return cls(loaded)

    def require(self, project_id: str, *, allow_disabled: bool = False) -> Project:
        loaded = self._projects.get(project_id)
        if loaded is None:
            raise ProjectNotFoundError(f"unknown project: {project_id}")
        if loaded.project.status is ProjectStatus.DISABLED and not allow_disabled:
            raise ProjectDisabledError(f"project is disabled: {project_id}")
        return loaded.project

    def get_loaded(self, project_id: str) -> LoadedProject:
        self.require(project_id, allow_disabled=True)
        return self._projects[project_id]

    def list(self, *, include_archived: bool = False) -> list[Project]:
        projects = [
            item.project
            for item in self._projects.values()
            if item.project.status is not ProjectStatus.DISABLED
            and (include_archived or item.project.status is not ProjectStatus.ARCHIVED)
        ]
        return sorted(projects, key=lambda project: (project.name.casefold(), project.id))

    def all_projects(self) -> list[Project]:
        """Return every configured project, including disabled projects for persistence."""
        return sorted(
            (item.project for item in self._projects.values()),
            key=lambda project: (project.name.casefold(), project.id),
        )

    def __len__(self) -> int:
        return len(self._projects)


def project_config_paths(directory: Path) -> list[Path]:
    """Return project files; useful for diagnostics and tests."""
    return sorted(
        path
        for path in [*directory.glob("*.yaml"), *directory.glob("*.yml")]
        if not path.name.endswith((".sources.yaml", ".sources.yml"))
    )

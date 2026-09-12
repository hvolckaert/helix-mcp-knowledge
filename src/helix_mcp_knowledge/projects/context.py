"""Instance-scoped active project state for the stdio V1 server."""

from dataclasses import dataclass
from threading import RLock

from ..errors import ConfigurationError
from ..models.project import Project
from .registry import ProjectRegistry


@dataclass(frozen=True, slots=True)
class EffectiveProject:
    project: Project | None
    source: str


class ProjectContext:
    """Resolve explicit, active and configured projects without module globals."""

    def __init__(self, registry: ProjectRegistry, default_project: str | None) -> None:
        self._registry = registry
        self._lock = RLock()
        self._active_project_id: str | None = None
        self._session_override = False
        self._default_project_id = default_project
        if default_project is not None:
            try:
                registry.require(default_project)
            except Exception as exc:
                raise ConfigurationError(
                    f"invalid default_project {default_project!r}: {exc}"
                ) from exc

    def resolve(self, explicit_project_id: str | None = None) -> EffectiveProject:
        if explicit_project_id is not None:
            return EffectiveProject(self._registry.require(explicit_project_id), "request")
        with self._lock:
            active_project_id = self._active_project_id
            session_override = self._session_override
        if active_project_id is not None:
            return EffectiveProject(self._registry.require(active_project_id), "session")
        if session_override:
            return EffectiveProject(None, "none")
        if self._default_project_id is not None:
            return EffectiveProject(
                self._registry.require(self._default_project_id), "configuration"
            )
        return EffectiveProject(None, "none")

    def set_active(self, project_id: str | None) -> EffectiveProject:
        project = self._registry.require(project_id) if project_id is not None else None
        with self._lock:
            self._active_project_id = project.id if project is not None else None
            self._session_override = True
        return EffectiveProject(project, "session" if project is not None else "none")

    def get_active(self) -> EffectiveProject:
        with self._lock:
            active_project_id = self._active_project_id
        if active_project_id is None:
            return EffectiveProject(None, "none")
        return EffectiveProject(self._registry.require(active_project_id), "session")

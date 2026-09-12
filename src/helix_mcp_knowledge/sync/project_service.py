"""Manifest-driven synchronization of local project documents."""

from dataclasses import asdict, dataclass
from pathlib import Path

from ..errors import ConfigurationError, IngestionError, SourceSyncError
from ..ingestion.manager import IngestionManager
from ..models.ingestion import IngestRequest
from ..models.project import Project
from ..models.source import SourceScope
from ..projects.registry import ProjectRegistry
from ..storage.vector_cleanup import drain_vector_cleanup
from .project_manifest import ProjectSourceDefinition, ProjectSourceManifest


@dataclass(frozen=True, slots=True)
class ProjectSyncResult:
    project_id: str
    status: str
    source_path: str
    document_id: str | None = None
    chunks_indexed: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ProjectSourceSynchronizer:
    def __init__(
        self,
        *,
        registry: ProjectRegistry,
        ingestion_manager: IngestionManager,
        allowed_extensions: list[str],
    ) -> None:
        self.registry = registry
        self.ingestion_manager = ingestion_manager
        self.allowed_extensions = {value.casefold() for value in allowed_extensions}

    def sync(self, project_id: str, *, prune: bool = True) -> list[ProjectSyncResult]:
        project = self.registry.require(project_id)
        manifest_path = project.sources_manifest_path
        if manifest_path is None:
            raise ConfigurationError(
                f"project {project_id!r} does not declare documents.sources_manifest"
            )
        manifest = ProjectSourceManifest.load(manifest_path, expected_project_id=project_id)
        manifest_id = f"project:{project_id}"
        documents_root = project.documents_path.resolve()
        root_unavailable = not documents_root.is_dir() or project.documents_path.is_symlink()
        if root_unavailable:
            managed = self.ingestion_manager.store.managed_project_documents(
                project.id, manifest_id
            )
            if managed or any(source.enabled for source in manifest.sources):
                raise SourceSyncError(
                    f"project documentation root is unavailable: {documents_root}"
                )
            return []
        selected = self._selected_files(project, manifest)
        results: list[ProjectSyncResult] = []
        desired_paths: set[str] = set()
        for path, definition in selected:
            desired_paths.add(str(path))
            results.append(self._sync_one(project, manifest_path, manifest_id, path, definition))

        if prune and not any(result.status == "error" for result in results):
            results.extend(self._mark_stale_missing(project, manifest_id, desired_paths))
        return results

    def _selected_files(
        self, project: Project, manifest: ProjectSourceManifest
    ) -> list[tuple[Path, ProjectSourceDefinition]]:
        root = project.documents_path.resolve()
        selected: dict[Path, ProjectSourceDefinition] = {}
        for definition in manifest.sources:
            if not definition.enabled:
                continue
            try:
                declared_path = root / definition.path
                candidates = (
                    sorted(root.glob(definition.path))
                    if "*" in definition.path or "?" in definition.path
                    else [declared_path]
                )
            except (OSError, ValueError) as exc:
                raise SourceSyncError(
                    f"invalid project source pattern {definition.path!r}: {exc}"
                ) from exc
            for candidate in candidates:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                self._require_inside(resolved, root, definition.path)
                if resolved.suffix.casefold() not in self.allowed_extensions:
                    continue
                previous = selected.get(resolved)
                if previous is not None:
                    raise SourceSyncError(
                        f"project source matches more than one manifest entry: {resolved} "
                        f"({previous.path!r}, {definition.path!r})"
                    )
                selected[resolved] = definition
        return sorted(selected.items(), key=lambda item: str(item[0]).casefold())

    @staticmethod
    def _require_inside(path: Path, root: Path, pattern: str) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SourceSyncError(
                f"project source pattern escapes configured root: {pattern!r} -> {path}"
            ) from exc

    def _sync_one(
        self,
        project: Project,
        manifest_path: Path,
        manifest_id: str,
        path: Path,
        definition: ProjectSourceDefinition,
    ) -> ProjectSyncResult:
        try:
            ingested = self.ingestion_manager.ingest(
                IngestRequest(
                    source_path=path,
                    source_scope=SourceScope.PROJECT,
                    project_id=project.id,
                    document_type=definition.document_type,
                    language=definition.language,
                    classification=definition.classification,
                    product_versions=definition.products,
                    title=definition.title,
                    metadata={
                        **definition.metadata,
                        "_project_sync": {
                            "manifest_id": manifest_id,
                            "manifest_path": str(manifest_path),
                            "source_pattern": definition.path,
                        },
                    },
                )
            )
            return ProjectSyncResult(
                project_id=project.id,
                status=ingested.status,
                source_path=str(path),
                document_id=ingested.document_id,
                chunks_indexed=(ingested.chunks_indexed if ingested.status == "indexed" else 0),
            )
        except IngestionError as exc:
            return ProjectSyncResult(
                project_id=project.id,
                status="error",
                source_path=str(path),
                error=str(exc),
            )

    def _mark_stale_missing(
        self, project: Project, manifest_id: str, desired_paths: set[str]
    ) -> list[ProjectSyncResult]:
        managed = self.ingestion_manager.store.managed_project_documents(project.id, manifest_id)
        results: list[ProjectSyncResult] = []
        for source_path, (document_id, status) in sorted(managed.items()):
            if source_path in desired_paths and status == "indexed":
                continue
            try:
                vector_index = self.ingestion_manager.vector_index
                self.ingestion_manager.store.mark_missing(
                    document_id,
                    queue_vectors=vector_index.enabled,
                )
                if vector_index.enabled:
                    cleanup = drain_vector_cleanup(
                        self.ingestion_manager.store.database,
                        vector_index,
                    )
                    if cleanup.status == "error":
                        raise SourceSyncError(
                            "retired document is hidden but vector cleanup remains pending: "
                            f"{cleanup.error}"
                        )
                else:
                    self.ingestion_manager.store.purge_missing(document_id)
                results.append(
                    ProjectSyncResult(
                        project_id=project.id,
                        status="missing",
                        source_path=source_path,
                        document_id=document_id,
                    )
                )
            except Exception as exc:
                results.append(
                    ProjectSyncResult(
                        project_id=project.id,
                        status="error",
                        source_path=source_path,
                        document_id=document_id,
                        error=f"failed to retire missing project document: {exc}",
                    )
                )
        return results

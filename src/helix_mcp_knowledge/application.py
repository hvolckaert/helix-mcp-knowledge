"""Composition root for config, project state, SQLite and retrieval."""

import json
import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from .automation import EmbeddedSyncCoordinator
from .catalog.distribution import load_effective_official_catalog
from .catalog.products import ProductCatalog
from .catalog.updater import CatalogUpdateChecker
from .config import AppConfig, WatchSettings, load_config
from .errors import KnowledgeError
from .ingestion.chunker import Chunker
from .ingestion.manager import IngestionManager
from .ingestion.parsers.registry import ParserRegistry
from .models.project import (
    ActiveProjectResponse,
    ProjectListResponse,
    ProjectSummary,
)
from .models.sync_status import SyncStatusResponse
from .ocr_component import OcrClient, OcrComponentManager
from .official_automation import EmbeddedOfficialSyncCoordinator
from .official_worker import OfficialSyncWorkerLauncher
from .projects.context import ProjectContext
from .projects.registry import ProjectRegistry, project_config_paths
from .release_checker import ReleaseUpdateChecker
from .reranker_client import RERANKER_MODEL_ID
from .reranker_component import RERANKER_COMPONENT_VERSION, RerankerComponentManager
from .reranker_install_worker import RERANKER_INSTALL_JOB
from .retrieval.reranker import Reranker, RerankerRuntime
from .retrieval.search_engine import SearchEngine
from .semantic_component import SEMANTIC_COMPONENT_VERSION, SemanticComponentManager
from .semantic_install_worker import SEMANTIC_INSTALL_JOB, SEMANTIC_PAYLOAD_VERSION
from .storage.automation import AutomationStore
from .storage.database import Database
from .storage.documents import DocumentStore
from .storage.sources import SourceStore
from .storage.vectors import NullVectorIndex
from .sync.project_service import ProjectSourceSynchronizer, ProjectSyncResult
from .sync.service import (
    CancelCheck,
    OfficialSourceSynchronizer,
    ProgressCallback,
    SourceSyncResult,
)
from .sync_status import SyncStatusReader
from .update_lock import UpdateLock
from .workspace import secure_managed_project_documents, secure_workspace_metadata

LOGGER = logging.getLogger(__name__)
_RERANKER_SERVICE_RECONCILE_SECONDS = 30.0


class _LiveRerankerRuntimeProvider:
    """Expose dashboard config changes to long-lived MCP sessions safely."""

    def __init__(
        self,
        config: AppConfig,
        backend: Reranker | None,
        automation_store: AutomationStore | None = None,
        *,
        manage_service_lifecycle: bool = True,
    ) -> None:
        self._config_path = config.config_path
        self._component_metadata_path = config.reranker_component_path / "current.json"
        self._lock = threading.Lock()
        self._service_operation_lock = threading.Lock()
        self._automation_store = automation_store
        self._manage_service_lifecycle = manage_service_lifecycle
        self._generation = 0
        self._pending_service_operations: set[tuple[int, bool]] = set()
        self._fingerprint = self._current_fingerprint()
        self._manager = (
            RerankerComponentManager(config)
            if (
                manage_service_lifecycle
                and config.retrieval.reranker.enabled
                and backend is not None
            )
            else None
        )
        self._next_service_reconcile = time.monotonic() + _RERANKER_SERVICE_RECONCILE_SECONDS
        self._runtime = RerankerRuntime(
            enabled=config.retrieval.reranker.enabled,
            candidates=config.retrieval.reranker.candidates,
            backend=backend,
        )

    def __call__(self) -> RerankerRuntime:
        with self._lock:
            fingerprint = self._current_fingerprint()
            if fingerprint == self._fingerprint:
                self._schedule_service_reconcile_locked()
                return self._runtime

            # Parse and validate a complete snapshot. If a writer is between atomic
            # steps, the exception is handled by SearchEngine and this fingerprint
            # is not committed, so the next search retries automatically.
            refreshed = load_config(self._config_path)
            confirmed_fingerprint = self._current_fingerprint()
            if confirmed_fingerprint != fingerprint:
                refreshed = load_config(self._config_path)
                fingerprint = confirmed_fingerprint
                if self._current_fingerprint() != fingerprint:
                    raise RuntimeError("reranker configuration changed during refresh")
            settings = refreshed.retrieval.reranker
            if not self._manage_service_lifecycle:
                self._runtime = RerankerRuntime(
                    enabled=settings.enabled,
                    candidates=settings.candidates,
                    backend=None,
                )
                self._manager = None
                self._fingerprint = fingerprint
                return self._runtime
            backend: Reranker | None = None
            manager = RerankerComponentManager(refreshed)
            enable_supported = settings.enabled and settings.model == RERANKER_MODEL_ID
            installation_owns_disabled_config = False
            if not settings.enabled and self._automation_store is not None:
                operation = self._automation_store.state(RERANKER_INSTALL_JOB)
                installation_owns_disabled_config = (
                    operation.get("status") == "installing"
                    and operation.get("desired_enabled") is not False
                )
            if enable_supported:
                manager.request_service_start()
            elif not installation_owns_disabled_config:
                manager.request_service_stop()
            if enable_supported:
                try:
                    backend = manager.client()
                except Exception:
                    # Component validation and startup run in the lifecycle thread.
                    # Until it succeeds, the search path remains fail-open.
                    backend = None
            runtime = RerankerRuntime(
                enabled=settings.enabled,
                candidates=settings.candidates,
                backend=backend,
            )
            self._generation += 1
            generation = self._generation
            self._manager = None if installation_owns_disabled_config else manager
            self._next_service_reconcile = time.monotonic() + _RERANKER_SERVICE_RECONCILE_SECONDS
            self._runtime = runtime
            self._fingerprint = fingerprint
            if not installation_owns_disabled_config:
                self._launch_service_operation_locked(
                    manager,
                    generation=generation,
                    enable=enable_supported,
                )
            return runtime

    def _schedule_service_reconcile_locked(self) -> None:
        manager = self._manager
        if manager is None or time.monotonic() < self._next_service_reconcile:
            return
        self._next_service_reconcile = time.monotonic() + _RERANKER_SERVICE_RECONCILE_SECONDS
        self._launch_service_operation_locked(
            manager,
            generation=self._generation,
            enable=self._runtime.enabled,
        )

    def _launch_service_operation_locked(
        self,
        manager: RerankerComponentManager,
        *,
        generation: int,
        enable: bool,
    ) -> None:
        operation = (generation, enable)
        if operation in self._pending_service_operations:
            return
        self._pending_service_operations.add(operation)
        thread = threading.Thread(
            target=self._apply_service_generation,
            args=(manager, generation, enable),
            name=(
                "helix-reranker-service-enabler" if enable else "helix-reranker-service-disabler"
            ),
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            self._pending_service_operations.discard(operation)
            self._next_service_reconcile = 0.0

    def _apply_service_generation(
        self,
        manager: RerankerComponentManager,
        generation: int,
        enable: bool,
    ) -> None:
        try:
            with self._service_operation_lock:
                with self._lock:
                    current = generation == self._generation and self._manager is manager
                if not current:
                    return

                if self._automation_store is not None:
                    self._automation_store.patch_state(
                        RERANKER_INSTALL_JOB,
                        {"desired_enabled": enable},
                    )

                backend: Reranker | None = None
                operation_failed = False
                try:
                    if enable:
                        # A superseded disable can recreate the durable stop marker
                        # after the config watcher has already observed this newer
                        # enable generation. Clear it inside the serialized operation,
                        # immediately before startup, so the latest intent wins.
                        manager.request_service_start()
                        backend = manager.ensure_service()
                    else:
                        manager.stop_service()
                except Exception:
                    operation_failed = True

                with self._lock:
                    still_current = generation == self._generation and self._manager is manager
                    if still_current and enable and backend is not None:
                        self._runtime = RerankerRuntime(
                            enabled=True,
                            candidates=self._runtime.candidates,
                            backend=backend,
                        )
                    elif still_current and not enable and not operation_failed:
                        self._manager = None

                if enable and not still_current:
                    # An enable/reconcile that completes after a disable must not
                    # leave its service alive. A newer operation is serialized and
                    # will restore the service if the latest generation is enabled.
                    with suppress(Exception):
                        manager.stop_service()
        finally:
            with self._lock:
                self._pending_service_operations.discard((generation, enable))

    def _current_fingerprint(
        self,
    ) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
        return (
            self._path_fingerprint(self._config_path),
            self._path_fingerprint(self._component_metadata_path),
        )

    @staticmethod
    def _path_fingerprint(path: Path) -> tuple[int, int, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class KnowledgeApplication:
    def __init__(self, config: AppConfig, *, manage_optional_services: bool = True) -> None:
        self.config = config
        secure_workspace_metadata(
            config_path=config.config_path,
            projects_path=config.projects_config_path,
            errors_path=config.resolve_path(config.paths.errors),
            workspace_path=config.base_dir,
            data_path=config.sources_path.parent,
            sources_path=config.sources_path,
        )
        if config.official_manifest_path.is_file():
            self.catalog = ProductCatalog.from_manifest(load_effective_official_catalog(config))
        else:
            self.catalog = ProductCatalog()
        self.registry = ProjectRegistry.load(config, self.catalog)
        secure_managed_project_documents(
            sources_path=config.sources_path,
            documents_paths=[project.documents_path for project in self.registry.all_projects()],
        )
        self.project_context = ProjectContext(self.registry, config.projects.default_project)
        self.database = Database(config.sqlite_path)
        self.database.initialize()
        self.database.sync_catalog(self.catalog)
        self.database.sync_projects(self.registry)
        ocr_component = OcrComponentManager(config).status()
        self.embedder = None
        self.vector_index = NullVectorIndex()
        self.reranker = None
        automation_store = AutomationStore(self.database)
        semantic_state = automation_store.state(SEMANTIC_INSTALL_JOB)
        semantic_payload_is_current = (
            semantic_state.get("payload_version") == SEMANTIC_PAYLOAD_VERSION
        )
        if config.retrieval.semantic.enabled and semantic_payload_is_current:
            semantic = SemanticComponentManager(config)
            component = semantic.status()
            if (
                component.installed
                and component.status == "ready"
                and component.component_version == SEMANTIC_COMPONENT_VERSION
            ):
                try:
                    client = (
                        semantic.ensure_service() if manage_optional_services else semantic.client()
                    )
                    if not manage_optional_services and not client.health(timeout=0.25):
                        raise KnowledgeError("semantic service is not already running")
                except (KnowledgeError, OSError) as exc:
                    # The optional backend must never prevent lexical retrieval
                    # or the MCP server itself from starting.
                    LOGGER.warning("semantic service unavailable; using lexical search: %s", exc)
                    if manage_optional_services:
                        automation_store.update_state(
                            SEMANTIC_INSTALL_JOB,
                            {
                                **semantic_state,
                                "service_status": "degraded",
                                "service_error": str(exc)[:1000],
                            },
                        )
                else:
                    self.embedder = client
                    self.vector_index = client
                    if manage_optional_services and semantic_state.get("service_status") != "ready":
                        automation_store.update_state(
                            SEMANTIC_INSTALL_JOB,
                            {
                                **semantic_state,
                                "service_status": "ready",
                                "service_error": None,
                            },
                        )
        reranker_state = automation_store.state(RERANKER_INSTALL_JOB)
        if (
            config.retrieval.reranker.enabled
            and config.retrieval.reranker.model == RERANKER_MODEL_ID
        ):
            reranker = RerankerComponentManager(config)
            component = reranker.status()
            if (
                component.installed
                and component.status == "ready"
                and component.component_version == RERANKER_COMPONENT_VERSION
            ):
                client = reranker.client()
                self.reranker = client
                if client.health(timeout=0.25):
                    if manage_optional_services and reranker_state.get("service_status") != "ready":
                        ready_values: dict[str, object] = {
                            "service_status": "ready",
                            "service_error": None,
                        }
                        if reranker_state.get("service_status") not in {
                            "starting",
                            "stopping",
                        }:
                            ready_values["desired_enabled"] = True
                        automation_store.patch_state(
                            RERANKER_INSTALL_JOB,
                            ready_values,
                            defaults={"desired_enabled": True},
                        )
                elif manage_optional_services:
                    service_values: dict[str, object] = {
                        "service_status": "starting",
                        "service_error": None,
                    }
                    if reranker_state.get("service_status") not in {"starting", "stopping"}:
                        # A steady enabled configuration starts a new desired cycle.
                        # Transitional state, however, may contain a concurrent cancel.
                        service_values["desired_enabled"] = True
                    automation_store.patch_state(
                        RERANKER_INSTALL_JOB,
                        service_values,
                        defaults={"desired_enabled": True},
                    )
                    threading.Thread(
                        target=self._start_reranker_service,
                        args=(reranker, automation_store),
                        name="helix-reranker-background-start",
                        daemon=True,
                    ).start()
            else:
                automation_store.patch_state(
                    RERANKER_INSTALL_JOB,
                    {
                        "service_status": "degraded",
                        "service_error": component.error
                        or "The configured reranker component is unavailable.",
                    },
                )
        elif config.retrieval.reranker.enabled and manage_optional_services:
            automation_store.patch_state(
                RERANKER_INSTALL_JOB,
                {
                    "service_status": "degraded",
                    "service_error": (
                        "The configured reranker model is not supported by this release; "
                        "the standard ranking remains active."
                    ),
                },
            )
        elif (
            manage_optional_services
            and (
                config.reranker_component_path.exists()
                or reranker_state.get("service_status") in {"starting", "stopping"}
            )
            and not (
                reranker_state.get("status") == "installing"
                and reranker_state.get("desired_enabled") is not False
            )
        ):
            # A process/WSL restart can leave a worker alive even when the last
            # durable service state says "ready". Persisted disabled config is
            # authoritative, except while an explicitly requested installation
            # owns the provisional false value.
            reranker = RerankerComponentManager(config)
            try:
                service_inactive = reranker.service_is_inactive()
            except (KnowledgeError, OSError, ValueError):
                service_inactive = False
            if service_inactive:
                automation_store.patch_state(
                    RERANKER_INSTALL_JOB,
                    {
                        "desired_enabled": False,
                        "service_status": "stopped",
                        "service_error": None,
                    },
                )
            else:
                automation_store.patch_state(
                    RERANKER_INSTALL_JOB,
                    {"desired_enabled": False, "service_status": "stopping"},
                )
                threading.Thread(
                    target=self._start_reranker_service,
                    args=(reranker, automation_store),
                    name="helix-reranker-background-stop-recovery",
                    daemon=True,
                ).start()
        self.ingestion_manager = IngestionManager(
            parser_registry=ParserRegistry(
                allowed_extensions=config.ingestion.allowed_extensions,
                max_file_size_mb=config.ingestion.max_file_size_mb,
                ocr_settings=config.ingestion.ocr,
                ocr_client=(
                    OcrClient(config)
                    if config.ingestion.ocr.enabled and ocr_component.installed
                    else None
                ),
            ),
            chunker=Chunker(
                target_tokens=config.ingestion.chunking.target_tokens,
                max_tokens=config.ingestion.chunking.max_tokens,
                overlap_tokens=config.ingestion.chunking.overlap_tokens,
            ),
            document_store=DocumentStore(self.database),
            project_registry=self.registry,
            official_sources_root=config.sources_path / "bmc" / "official",
            errors_path=config.resolve_path(config.paths.errors),
            catalog=self.catalog,
            embedder=self.embedder,
            vector_index=self.vector_index,
            processing_profile={
                "ocr": {
                    "enabled": config.ingestion.ocr.enabled,
                    "component_version": (
                        ocr_component.component_version if config.ingestion.ocr.enabled else None
                    ),
                    "min_text_characters": config.ingestion.ocr.min_text_characters,
                    "dpi": config.ingestion.ocr.dpi,
                    "min_confidence": config.ingestion.ocr.min_confidence,
                }
            },
        )
        self._reranker_runtime_provider = _LiveRerankerRuntimeProvider(
            config,
            self.reranker,
            automation_store if manage_optional_services else None,
            manage_service_lifecycle=manage_optional_services,
        )
        self.search_engine = SearchEngine(
            self.database,
            self.config,
            self.catalog,
            self.project_context,
            embedder=self.embedder,
            vector_index=self.vector_index,
            reranker=self.reranker,
            reranker_runtime_provider=self._reranker_runtime_provider,
        )
        self.release_update_checker = ReleaseUpdateChecker(
            store=AutomationStore(self.database),
            settings=self.config.updates,
        )
        self.catalog_update_checker = CatalogUpdateChecker(
            config=self.config,
            store=AutomationStore(self.database),
            on_updated=self._reload_catalog,
        )

    @staticmethod
    def _start_reranker_service(
        manager: RerankerComponentManager,
        store: AutomationStore,
    ) -> None:
        while True:
            state = store.patch_state(
                RERANKER_INSTALL_JOB,
                {},
                defaults={"desired_enabled": True},
            )
            desired_enabled = state.get("desired_enabled") is not False
            transition = store.patch_state(
                RERANKER_INSTALL_JOB,
                {
                    "service_status": "starting" if desired_enabled else "stopping",
                    "service_error": None,
                },
            )
            if (transition.get("desired_enabled") is not False) != desired_enabled:
                continue
            try:
                if desired_enabled:
                    # Persisted config is authoritative after an upgrade. A
                    # successful downgrade intentionally leaves a durable stop
                    # marker because the older runtime cannot manage this
                    # component; clear it only inside the fenced enabled cycle.
                    manager.request_service_start()
                    manager.ensure_service()
                else:
                    manager.stop_service()
            except (KnowledgeError, OSError, json.JSONDecodeError, ValueError) as exc:
                LOGGER.warning(
                    "reranker service transition failed; preserving standard ranking (%s)",
                    type(exc).__name__,
                )
                failed = store.patch_state(
                    RERANKER_INSTALL_JOB,
                    {
                        "service_status": "degraded",
                        "service_error": str(exc)[:1000],
                    },
                )
                if (failed.get("desired_enabled") is not False) != desired_enabled:
                    continue
                return
            completed = store.patch_state(
                RERANKER_INSTALL_JOB,
                {
                    "service_status": "ready" if desired_enabled else "stopped",
                    "service_error": None,
                },
            )
            if (completed.get("desired_enabled") is not False) == desired_enabled:
                return

    def _reload_catalog(self) -> None:
        refreshed = ProductCatalog.from_manifest(load_effective_official_catalog(self.config))
        self.catalog.replace(refreshed.all())
        self.database.sync_catalog(self.catalog)

    def sync_official_sources(
        self,
        source_ids: set[str] | None = None,
        *,
        collection_ids: set[str] | None = None,
        discover: bool = True,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> list[SourceSyncResult]:
        with UpdateLock(self.config.base_dir / ".update.lock"):
            return OfficialSourceSynchronizer(
                config=self.config,
                ingestion_manager=self.ingestion_manager,
                source_store=SourceStore(self.database),
                catalog=self.catalog,
            ).sync(
                source_ids,
                collection_ids=collection_ids,
                discover=discover,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )

    def sync_project_sources(
        self, project_id: str, *, prune: bool = True
    ) -> list[ProjectSyncResult]:
        with UpdateLock(self.config.base_dir / ".update.lock"):
            return ProjectSourceSynchronizer(
                registry=self.registry,
                ingestion_manager=self.ingestion_manager,
                allowed_extensions=self.config.ingestion.allowed_extensions,
            ).sync(project_id, prune=prune)

    def create_sync_coordinator(self) -> EmbeddedSyncCoordinator:
        return EmbeddedSyncCoordinator(
            registry=self.registry,
            store=AutomationStore(self.database),
            settings=self.config.ingestion.watch,
            allowed_extensions=self.config.ingestion.allowed_extensions,
            sync_project=self.sync_project_sources,
            configuration_snapshot=self._project_configuration_snapshot,
            reload_configuration=self._reload_project_sync_configuration,
        )

    def _project_configuration_snapshot(self) -> tuple[tuple[str, int, int, int], ...]:
        current = load_config(self.config.config_path)
        paths = [current.config_path, *project_config_paths(current.projects_config_path)]
        entries: list[tuple[str, int, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                entries.append((str(path), -1, -1, -1))
            else:
                entries.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        return tuple(entries)

    def _reload_project_sync_configuration(
        self,
    ) -> tuple[
        ProjectRegistry,
        WatchSettings,
        list[str],
        Callable[[str], list[ProjectSyncResult]],
    ]:
        refreshed = type(self).from_config(self.config.config_path)
        return (
            refreshed.registry,
            refreshed.config.ingestion.watch,
            refreshed.config.ingestion.allowed_extensions,
            refreshed.sync_project_sources,
        )

    def create_official_sync_coordinator(self) -> EmbeddedOfficialSyncCoordinator:
        return EmbeddedOfficialSyncCoordinator(
            store=AutomationStore(self.database),
            settings=self.config.official_docs,
            coordination=self.config.ingestion.watch,
            sync_official=self.sync_official_sources,
            official_document_count=self.official_document_count,
        )

    def create_official_sync_worker_launcher(self) -> OfficialSyncWorkerLauncher:
        return OfficialSyncWorkerLauncher(
            config_path=self.config.config_path,
            workspace=self.config.base_dir,
            errors_path=self.config.resolve_path(self.config.paths.errors),
        )

    def official_document_count(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT count(*) AS count
                FROM documents
                WHERE source_scope = 'bmc_official' AND status = 'indexed'
                """
            ).fetchone()
        return int(row["count"])

    def automation_status(self) -> dict[str, object]:
        store = AutomationStore(self.database)
        return {
            project.id: store.state(f"project:{project.id}")
            for project in self.registry.list()
            if project.sources_manifest_path is not None
        }

    def get_sync_status(self, project_id: str | None = None) -> SyncStatusResponse:
        return SyncStatusReader(
            database=self.database,
            config=self.config,
            catalog=self.catalog,
            project_context=self.project_context,
        ).get(project_id)

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        manage_optional_services: bool = True,
    ) -> "KnowledgeApplication":
        return cls(
            load_config(path),
            manage_optional_services=manage_optional_services,
        )

    def list_projects(self, include_archived: bool = False) -> ProjectListResponse:
        projects = self.registry.list(include_archived=include_archived)
        return ProjectListResponse(
            projects=[
                ProjectSummary(
                    id=project.id,
                    name=project.name,
                    status=project.status,
                    products={
                        product_id: item.version
                        for product_id, item in project.bmc_products.items()
                    },
                )
                for project in projects
            ]
        )

    def get_active_project(self) -> ActiveProjectResponse:
        effective = self.project_context.get_active()
        return ActiveProjectResponse(
            project_id=effective.project.id if effective.project else None,
            source=effective.source,
        )

    def set_active_project(self, project_id: str | None) -> ActiveProjectResponse:
        effective = self.project_context.set_active(project_id)
        return ActiveProjectResponse(
            project_id=effective.project.id if effective.project else None,
            source=effective.source,
        )

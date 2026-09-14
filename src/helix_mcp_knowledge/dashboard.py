"""Local administrative dashboard for products, versions and synchronization."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .application import KnowledgeApplication
from .catalog.distribution import (
    CatalogMergeResult,
    load_effective_official_catalog,
    reconcile_packaged_official_catalog,
)
from .catalog.products import ProductCatalog
from .catalog.versions import normalize_version
from .cli.configure import available_official_versions, configure_official_docs
from .component_install_recovery import (
    candidate_cleanup_minimum_age,
    current_boot_id,
    install_state_is_stale,
    mark_interrupted_install,
)
from .config import AppConfig, load_config
from .dashboard_runtime import (
    DASHBOARD_MODE_ENV,
    DashboardRuntimeManager,
    dashboard_workspace_id,
)
from .dashboard_update_worker import (
    DASHBOARD_UPDATE_JOB,
    DashboardUpdateWorkerLauncher,
    recent_dashboard_request,
)
from .errors import ConfigurationError, KnowledgeError
from .ingestion.parsers.registry import SUPPORTED_DOCUMENT_EXTENSIONS
from .managed_installation import (
    DEFAULT_MANAGED_DASHBOARD_PORT,
    activate_managed_installation,
    load_managed_installation,
    supports_transactional_updates,
    versioned_runtime_paths,
)
from .models.project import Classification, Project, ProjectStatus
from .ocr_component import OcrComponentManager
from .ocr_install_worker import OCR_INSTALL_JOB, OcrInstallWorkerLauncher
from .official_cleanup import OfficialCorpusCleaner
from .official_worker import PersistentOfficialSyncScheduler
from .openclaw import (
    DEFAULT_SERVER_NAME,
    OPENCLAW_INTEGRATION_ERROR,
    reload_openclaw,
)
from .projects.registry import project_config_paths
from .reranker_component import (
    RERANKER_COMPONENT_VERSION,
    RERANKER_MODEL_ID,
    RERANKER_MODEL_REVISION,
    RerankerComponentManager,
)
from .reranker_install_worker import RERANKER_INSTALL_JOB, RerankerInstallWorkerLauncher
from .retrieval.reranker import RERANKER_MAX_CANDIDATES
from .semantic_component import (
    SEMANTIC_COMPONENT_VERSION,
    SEMANTIC_MODEL_REVISION,
    SemanticComponentManager,
)
from .semantic_install_worker import (
    SEMANTIC_INSTALL_JOB,
    SEMANTIC_PAYLOAD_VERSION,
    SemanticInstallWorkerLauncher,
)
from .storage.automation import AutomationStore
from .storage.database import Database
from .storage.documents import DocumentStore
from .storage.vector_cleanup import drain_vector_cleanup
from .storage_retention import maintain_managed_storage
from .update_lock import UpdateLock, UpdateLockBusyError

MAX_REQUEST_BYTES = 256 * 1024
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = DEFAULT_MANAGED_DASHBOARD_PORT
LOGGER = logging.getLogger(__name__)
RERANKER_DEFAULT_CANDIDATES = 10
RERANKER_LEGACY_PLACEHOLDER_CANDIDATES = 20


def _dashboard_update_worker_is_active(process_id: int) -> bool:
    """Return whether a PID still belongs to the dashboard update worker."""

    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except (OSError, ValueError):
        return False
    if os.name == "nt":  # pragma: no cover - process inspection differs on Windows
        return True
    try:
        command_line = Path(f"/proc/{process_id}/cmdline").read_bytes()
    except PermissionError:
        return True
    except OSError:
        return False
    return b"helix_mcp_knowledge.dashboard_update_worker" in command_line


def _dashboard_update_request_is_recent(state: dict[str, object]) -> bool:
    requested_at = state.get("requested_at")
    if not isinstance(requested_at, str):
        return False
    try:
        requested = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if requested.tzinfo is None:
        requested = requested.replace(tzinfo=UTC)
    return 0 <= (datetime.now(UTC) - requested).total_seconds() <= 60


@dataclass(frozen=True)
class DashboardProcess:
    pid: int
    url: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class DashboardProcessLauncher:
    """Start the local dashboard independently of the installer process."""

    def __init__(
        self,
        *,
        config_path: Path,
        workspace: Path,
        errors_path: Path,
        python_executable: str | None = None,
        port: int = DEFAULT_DASHBOARD_PORT,
        server_name: str = DEFAULT_SERVER_NAME,
        openclaw_command: str | Path = "openclaw",
    ) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("dashboard port must be between 1 and 65535")
        self.config_path = config_path.resolve()
        self.workspace = workspace.resolve()
        self.errors_path = errors_path.resolve()
        self.python_executable = python_executable or sys.executable
        self.port = port
        self.server_name = server_name
        self.openclaw_command = str(openclaw_command)
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, *, open_browser: bool = True) -> DashboardProcess:
        if self._process is not None:
            raise RuntimeError("dashboard already launched")
        self.errors_path.mkdir(parents=True, exist_ok=True)
        log_path = self.errors_path / "dashboard.log"
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment[DASHBOARD_MODE_ENV] = "detached"
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
            "helix_mcp_knowledge.dashboard_worker",
            "--config",
            str(self.config_path),
            "--port",
            str(self.port),
            "--server-name",
            self.server_name,
            "--openclaw-command",
            self.openclaw_command,
        ]
        if not open_browser:
            command.append("--no-browser")
        with log_path.open("ab", buffering=0) as log:
            self._process = subprocess.Popen(command, stderr=log, **kwargs)
        threading.Thread(
            target=self._process.wait,
            name="helix-dashboard-reaper",
            daemon=True,
        ).start()
        return DashboardProcess(
            pid=self._process.pid,
            url=f"http://{LOOPBACK_HOST}:{self.port}/",
        )


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DashboardProjectSelection(_StrictModel):
    products: dict[str, str] = Field(default_factory=dict)
    documents_path: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("products")
    @classmethod
    def non_empty_versions(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not str(version).strip() for version in values.values()):
            raise ValueError("project versions cannot be empty")
        return values

    @field_validator("documents_path", mode="before")
    @classmethod
    def strip_documents_path(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class DashboardProjectCreate(_StrictModel):
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    name: str = Field(min_length=1, max_length=120)
    documents_path: str | None = Field(default=None, min_length=1, max_length=4096)
    description: str | None = Field(default=None, max_length=500)
    classification: Classification = Classification.CONFIDENTIAL
    language: str = Field(default="en", pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?$")

    @field_validator(
        "project_id",
        "name",
        "documents_path",
        "description",
        "language",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class DashboardProjectRemove(_StrictModel):
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")


class DashboardProjectSync(_StrictModel):
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")


class DashboardDirectoryBrowse(_StrictModel):
    path: str | None = Field(default=None, max_length=4096)

    @field_validator("path", mode="before")
    @classmethod
    def strip_path(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class DashboardConfiguration(_StrictModel):
    products: dict[str, list[str]] = Field(default_factory=dict)
    automatic_sync: bool
    bootstrap_on_empty: bool
    interval_hours: float = Field(ge=0.01, le=8760.0)
    retain_unselected_versions: bool
    projects: dict[str, DashboardProjectSelection] = Field(default_factory=dict)

    @field_validator("products")
    @classmethod
    def non_empty_selections(cls, values: dict[str, list[str]]) -> dict[str, list[str]]:
        for product, versions in values.items():
            if (
                not product.strip()
                or not versions
                or any(not str(item).strip() for item in versions)
            ):
                raise ValueError("selected products require at least one non-empty version")
            if len(versions) != len(set(versions)):
                raise ValueError("selected versions must be unique per product")
        return values


class DashboardCleanupPreview(_StrictModel):
    products: dict[str, list[str]] = Field(default_factory=dict)


class DashboardOcrConfiguration(_StrictModel):
    enabled: bool


class DashboardSemanticConfiguration(_StrictModel):
    enabled: bool


class DashboardRerankerConfiguration(_StrictModel):
    enabled: bool


class DashboardService:
    """Validated operations shared by the browser UI and tests."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        server_name: str = DEFAULT_SERVER_NAME,
        openclaw_command: str | Path = "openclaw",
        dashboard_port: int = DEFAULT_DASHBOARD_PORT,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.server_name = server_name
        self.openclaw_command = str(openclaw_command)
        self.dashboard_port = dashboard_port
        self._mutex = threading.Lock()
        self._state_lock = threading.Lock()
        self._retention_lock = threading.Lock()
        self._semantic_recovery_lock = threading.Lock()
        self._reranker_recovery_lock = threading.Lock()
        config = load_config(self.config_path)
        manifest_exists = config.official_manifest_path.is_file()
        packaged_update = (
            self._reconcile_packaged_catalog(config) if manifest_exists else CatalogMergeResult()
        )
        application = KnowledgeApplication(config)
        self._state_application = application
        self._state_signature = self._application_signature(application.config)
        self._state_manifest = (
            load_effective_official_catalog(application.config) if manifest_exists else None
        )
        self._state_available_versions = (
            available_official_versions(self._state_manifest, application.catalog)
            if self._state_manifest is not None
            else {}
        )
        self._state_packaged_update = packaged_update
        self.storage_retention = maintain_managed_storage(application.config)
        self.release_update_checker = application.release_update_checker
        self.catalog_update_checker = application.catalog_update_checker

    def state(self) -> dict[str, object]:
        config = load_config(self.config_path)
        self._recover_interrupted_semantic_setup(config)
        self._recover_interrupted_reranker_install(config)
        config = load_config(self.config_path)
        self._retry_deferred_storage_retention(config)
        application, manifest, available, packaged_update = self._cached_state(config)
        if manifest is None:
            raise ConfigurationError(
                f"official source manifest not found: {config.official_manifest_path}"
            )
        catalog_status = application.catalog_update_checker.status()
        indexed = application.search_engine.indexed_versions()
        selected = {
            product: list(settings.versions)
            for product, settings in config.official_docs.products.items()
        }
        products = [
            {
                "product_id": product.product_id,
                "name": product.name,
                "aliases": product.aliases,
                "available_versions": available.get(product.product_id, []),
                "selected_versions": selected.get(product.product_id, []),
                "indexed_versions": sorted(indexed.get(product.product_id, set()), reverse=True),
            }
            for product in application.catalog.all()
        ]
        store = AutomationStore(application.database)
        project_counts = self._project_index_counts(application.database)
        active_owners = store.active_owner_ids()
        projects = []
        for project in application.registry.list(include_archived=True):
            counts = project_counts.get(project.id, {"documents": 0, "chunks": 0})
            sync_state = store.state(f"project:{project.id}")
            projects.append(
                {
                    "project_id": project.id,
                    "name": project.name,
                    "status": project.status.value,
                    "documents_path": str(project.documents_path),
                    "managed_documents_path": self._path_is_within(
                        project.documents_path, config.sources_path
                    ),
                    "products": {
                        product_id: settings.version
                        for product_id, settings in sorted(project.bmc_products.items())
                    },
                    "sync": self._project_sync_summary(
                        project=project,
                        state=sync_state,
                        counts=counts,
                        active_owners=active_owners,
                        automatic_sync=config.ingestion.watch.enabled,
                    ),
                }
            )
        ocr_component = OcrComponentManager(config).status()
        ocr_operation = store.state(OCR_INSTALL_JOB)
        ocr_status = ocr_component.status
        ocr_error = ocr_component.error
        if ocr_operation.get("status") == "installing":
            ocr_status = "installing"
        elif ocr_operation.get("status") == "error" and not ocr_component.installed:
            ocr_status = "error"
            ocr_error = str(ocr_operation.get("error") or "OCR installation failed")
        semantic_component = SemanticComponentManager(config).status(
            check_service=config.retrieval.semantic.enabled
        )
        semantic_operation = store.state(SEMANTIC_INSTALL_JOB)
        semantic_status = semantic_component.status
        semantic_error = semantic_component.error
        if (
            semantic_component.component_version == SEMANTIC_COMPONENT_VERSION
            and semantic_component.status in {"ready", "degraded"}
            and semantic_operation.get("service_status") == "degraded"
        ):
            semantic_status = "degraded"
            semantic_error = str(
                semantic_operation.get("service_error")
                or "Semantic service is unavailable; lexical search remains available."
            )
        if semantic_operation.get("status") in {"installing", "indexing"}:
            semantic_status = str(semantic_operation["status"])
        elif semantic_operation.get("status") == "error":
            semantic_status = "error"
            semantic_error = str(semantic_operation.get("error") or "semantic installation failed")
        elif (
            semantic_component.installed
            and semantic_component.component_version == SEMANTIC_COMPONENT_VERSION
            and semantic_component.status in {"ready", "degraded"}
            and config.retrieval.semantic.enabled
            and semantic_operation.get("payload_version") != SEMANTIC_PAYLOAD_VERSION
        ):
            semantic_status = "reindex_required"
            semantic_error = (
                "The semantic payload format changed. Enable semantic search again to rebuild "
                "the optional index. Lexical search remains available."
            )
        reranker_settings_supported = config.retrieval.reranker.model == RERANKER_MODEL_ID
        reranker_component = RerankerComponentManager(config).status(
            check_service=(config.retrieval.reranker.enabled and reranker_settings_supported)
        )
        reranker_operation = store.state(RERANKER_INSTALL_JOB)
        reranker_status = reranker_component.status
        reranker_error = reranker_component.error
        if reranker_operation.get("status") == "installing":
            reranker_status = "installing"
        elif reranker_operation.get("status") == "error":
            reranker_status = "error"
            reranker_error = str(reranker_operation.get("error") or "reranker installation failed")
        elif config.retrieval.reranker.enabled and not reranker_settings_supported:
            reranker_status = "degraded"
            reranker_error = (
                "The configured reranker model is not supported by this release; "
                "the standard ranking remains active. Re-enable it here to use the fixed model."
            )
        elif reranker_operation.get("service_status") in {"starting", "stopping"}:
            reranker_status = (
                "starting" if reranker_operation.get("desired_enabled") is not False else "stopping"
            )
            reranker_error = None
        elif reranker_operation.get("service_status") == "degraded":
            reranker_status = "degraded"
            reranker_error = str(
                reranker_operation.get("service_error")
                or "The reranker is unavailable; the standard ranking remains active."
            )
        return {
            "server_version": __version__,
            "storage_retention": self.storage_retention.to_dict(),
            "catalog": {
                **catalog_status.to_dict(),
                "effective_revision": manifest.catalog_revision,
                "packaged_additions": packaged_update.to_dict(),
            },
            "products": products,
            "settings": {
                "automatic_sync": config.official_docs.automatic_sync,
                "bootstrap_on_empty": config.official_docs.bootstrap_on_empty,
                "interval_hours": config.official_docs.interval_hours,
                "retain_unselected_versions": (config.official_docs.retain_unselected_versions),
            },
            "projects": projects,
            "project_documentation": {
                "default_root": str((config.sources_path / "projects").resolve()),
                "allowed_extensions": list(config.ingestion.allowed_extensions),
            },
            "sync": application.get_sync_status().model_dump(mode="json"),
            "update": application.release_update_checker.status().model_dump(mode="json"),
            "dashboard_update": self._dashboard_update_state(
                self._reconcile_dashboard_update_state(
                    store,
                    store.state(DASHBOARD_UPDATE_JOB),
                    server_version=__version__,
                ),
                server_version=__version__,
            ),
            "ocr": {
                **ocr_component.to_dict(),
                "enabled": config.ingestion.ocr.enabled,
                "status": ocr_status,
                "error": ocr_error,
                "scope": "project_pdfs",
            },
            "semantic": {
                **semantic_component.to_dict(),
                "enabled": (
                    config.retrieval.semantic.enabled
                    and semantic_component.component_version == SEMANTIC_COMPONENT_VERSION
                    and semantic_component.status in {"ready", "degraded"}
                    and semantic_operation.get("payload_version") == SEMANTIC_PAYLOAD_VERSION
                ),
                "status": semantic_status,
                "error": semantic_error,
                "indexed_chunks": semantic_operation.get("indexed_chunks", 0),
                "total_chunks": semantic_operation.get("total_chunks", 0),
                "chunks_per_second": semantic_operation.get("chunks_per_second"),
                "estimated_seconds_remaining": semantic_operation.get(
                    "estimated_seconds_remaining"
                ),
                "model": config.embeddings.model,
                "model_revision": SEMANTIC_MODEL_REVISION,
                "backend": "managed_local_qdrant",
            },
            "reranker": {
                **reranker_component.to_dict(),
                "setup_requires_download": (
                    not reranker_component.installed
                    or reranker_component.status in {"update_required", "incompatible"}
                ),
                "enabled": (
                    config.retrieval.reranker.enabled
                    and reranker_settings_supported
                    and reranker_component.component_version == RERANKER_COMPONENT_VERSION
                    and reranker_component.status in {"ready", "degraded"}
                ),
                "status": reranker_status,
                "error": reranker_error,
                "model": config.retrieval.reranker.model,
                "model_revision": RERANKER_MODEL_REVISION,
                "backend": "managed_local_service",
            },
            "dashboard_runtime": self._dashboard_runtime(config.base_dir),
            "client_integrations": self._client_integrations(config.base_dir),
        }

    def _retry_deferred_storage_retention(self, config) -> None:
        if self.storage_retention.status != "deferred":
            return
        with self._retention_lock:
            if self.storage_retention.status == "deferred":
                self.storage_retention = maintain_managed_storage(config)

    def _cached_state(self, config):
        signature = self._application_signature(config)
        with self._state_lock:
            if signature != self._state_signature:
                packaged_update = self._reconcile_packaged_catalog(config)
                config = load_config(self.config_path)
                application = KnowledgeApplication(config)
                manifest = load_effective_official_catalog(config)
                self._state_application = application
                self._state_manifest = manifest
                self._state_available_versions = available_official_versions(
                    manifest, application.catalog
                )
                self._state_packaged_update = packaged_update
                self._state_signature = self._application_signature(config)
            return (
                self._state_application,
                self._state_manifest,
                self._state_available_versions,
                self._state_packaged_update,
            )

    @staticmethod
    def _reconcile_packaged_catalog(config) -> CatalogMergeResult:
        try:
            with UpdateLock(config.base_dir / ".update.lock"):
                return reconcile_packaged_official_catalog(config)
        except UpdateLockBusyError:
            return CatalogMergeResult()

    def _application_signature(self, config) -> tuple[tuple[str, int, int, int], ...]:
        paths = [
            self.config_path,
            config.official_manifest_path,
            config.official_catalog_cache_path,
            *project_config_paths(config.projects_config_path),
        ]
        entries: list[tuple[str, int, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                entries.append((str(path), -1, -1, -1))
            else:
                entries.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        return tuple(entries)

    def check_update(self) -> dict[str, object]:
        application = KnowledgeApplication.from_config(self.config_path)
        return application.release_update_checker.check(force=True).model_dump(mode="json")

    def health(self) -> dict[str, str]:
        return {
            "status": "ok",
            "server_version": __version__,
            "workspace_id": dashboard_workspace_id(self.config_path.parent.parent),
        }

    def refresh_catalog(self) -> dict[str, object]:
        application = KnowledgeApplication.from_config(self.config_path)
        result = application.catalog_update_checker.check(force=True)
        if result.status == "error":
            raise ConfigurationError(result.error or "catalog refresh failed")
        return self.state()

    def cleanup_preview(self, raw_payload: object) -> dict[str, object]:
        """Describe official data removed after a successful synchronization."""

        try:
            request = DashboardCleanupPreview.model_validate(raw_payload)
        except ValueError as exc:
            raise ConfigurationError(f"invalid cleanup preview request: {exc}") from exc
        config = load_config(self.config_path)
        application = KnowledgeApplication(config)
        manifest = load_effective_official_catalog(config)
        available = available_official_versions(manifest, application.catalog)
        specs = self._validate_official_selection(request.products, available, application.catalog)
        selected_pairs = {
            (product, normalize_version(version))
            for product, version in (spec.split("=", 1) for spec in specs)
        }
        preview = OfficialCorpusCleaner(
            database=application.database,
            vector_index=application.vector_index,
            official_sources_root=application.ingestion_manager.official_sources_root,
            catalog=application.catalog,
            manifest=manifest,
        ).preview(selected_pairs)
        return {
            **preview.to_dict(),
            "project_documents_affected": False,
            "execution": "after_successful_sync",
        }

    def configure_ocr(self, raw_payload: object) -> dict[str, object]:
        try:
            request = DashboardOcrConfiguration.model_validate(raw_payload)
        except ValueError as exc:
            raise ConfigurationError(f"invalid OCR configuration: {exc}") from exc
        with self._mutex:
            config = load_config(self.config_path)
            application = KnowledgeApplication(config)
            store = AutomationStore(application.database)
            operation = store.state(OCR_INSTALL_JOB)
            if operation.get("status") == "installing":
                store.patch_state_if_current(
                    OCR_INSTALL_JOB,
                    expected_status="installing",
                    values={"desired_enabled": request.enabled},
                )
                return self.state()
            with UpdateLock(config.base_dir / ".update.lock"):
                return self._configure_ocr_while_locked(
                    request=request,
                    config=config,
                    store=store,
                )

    def _configure_ocr_while_locked(
        self,
        *,
        request: DashboardOcrConfiguration,
        config: AppConfig,
        store: AutomationStore,
    ) -> dict[str, object]:
        operation = store.state(OCR_INSTALL_JOB)
        if operation.get("status") == "installing":
            store.patch_state_if_current(
                OCR_INSTALL_JOB,
                expected_status="installing",
                values={"desired_enabled": request.enabled},
            )
            return self.state()
        component = OcrComponentManager(config).status()
        if not request.enabled:
            self._set_ocr_enabled(False)
            store.update_state(
                OCR_INSTALL_JOB,
                {
                    "status": "ready" if component.installed else "not_installed",
                    "desired_enabled": False,
                },
            )
            return self.state()
        if component.installed:
            self._set_ocr_enabled(True)
            store.update_state(
                OCR_INSTALL_JOB,
                {
                    "status": "ready",
                    "desired_enabled": True,
                    "component_version": component.component_version,
                    "installed_bytes": component.installed_bytes,
                },
            )
            return self.state()
        pending = {
            "status": "installing",
            "desired_enabled": True,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        store.update_state(OCR_INSTALL_JOB, pending)
        try:
            process = OcrInstallWorkerLauncher(
                config_path=self.config_path,
                workspace=config.base_dir,
                errors_path=config.resolve_path(config.paths.errors),
            ).start()
        except Exception as exc:
            store.update_state(
                OCR_INSTALL_JOB,
                {**pending, "status": "error", "error": str(exc)[:2000]},
            )
            raise
        store.patch_state_if_current(
            OCR_INSTALL_JOB,
            expected_status="installing",
            values={"process_id": process.pid},
        )
        return self.state()

    def _set_ocr_enabled(self, enabled: bool) -> None:
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        payload.setdefault("ingestion", {}).setdefault("ocr", {})["enabled"] = enabled
        self._atomic_yaml_write(self.config_path, payload)
        load_config(self.config_path)

    def _recover_interrupted_reranker_install(
        self,
        config: AppConfig,
        *,
        store: AutomationStore | None = None,
    ) -> None:
        """Recover a detached reranker installer after proving its lock is free."""

        with self._reranker_recovery_lock:
            if store is None:
                database = Database(config.sqlite_path)
                database.initialize()
                store = AutomationStore(database)
            now = time.time()
            operation = store.state(RERANKER_INSTALL_JOB)
            if (
                not config.retrieval.reranker.enabled
                and operation.get("desired_enabled") is False
                and operation.get("service_status") == "stopping"
            ):
                try:
                    inactive = RerankerComponentManager(config).service_is_inactive()
                except (KnowledgeError, OSError, ValueError):
                    inactive = False
                if inactive:
                    # A short-lived process can exit after requesting recovery but
                    # before its daemon thread records completion.  Only collapse
                    # the transition after the lifecycle lock and port jointly
                    # prove that the optional service is already stopped.
                    latest = store.state(RERANKER_INSTALL_JOB)
                    latest_config = load_config(self.config_path)
                    if (
                        not latest_config.retrieval.reranker.enabled
                        and latest.get("desired_enabled") is False
                        and latest.get("service_status") == "stopping"
                    ):
                        store.patch_state(
                            RERANKER_INSTALL_JOB,
                            {"service_status": "stopped", "service_error": None},
                        )
                        operation = store.state(RERANKER_INSTALL_JOB)
            stale = install_state_is_stale(operation, now_epoch=now)
            cleanup_pending = operation.get("cleanup_pending") is True
            retry_at = operation.get("cleanup_retry_at_epoch")
            if cleanup_pending and isinstance(retry_at, (int, float)) and retry_at > now:
                cleanup_pending = False
            if not stale and not cleanup_pending:
                return
            try:
                with UpdateLock(config.base_dir / ".update.lock", timeout_seconds=0):
                    operation = store.state(RERANKER_INSTALL_JOB)
                    if install_state_is_stale(operation, now_epoch=now):
                        if not mark_interrupted_install(
                            store,
                            RERANKER_INSTALL_JOB,
                            now_epoch=now,
                        ):
                            return
                        operation = store.state(RERANKER_INSTALL_JOB)
                    elif operation.get("cleanup_pending") is not True:
                        return

                    self._set_reranker_enabled(False)
                    manager = RerankerComponentManager(load_config(self.config_path))
                    manager.stop_service()
                    cleanup = manager.cleanup_incomplete_candidates(
                        minimum_age_seconds=candidate_cleanup_minimum_age(operation)
                    )
                    component = manager.status()
                    deferred = cleanup.deferred_candidates > 0
                    reclaimed = int(operation.get("interrupted_reclaimed_bytes") or 0)
                    store.patch_state(
                        RERANKER_INSTALL_JOB,
                        {
                            "status": "ready" if component.installed else "error",
                            "desired_enabled": False,
                            "cleanup_pending": deferred,
                            "cleanup_retry_at_epoch": now + 300 if deferred else None,
                            "interrupted_reclaimed_bytes": reclaimed + cleanup.reclaimed_bytes,
                            "error": (
                                None
                                if component.installed
                                else (
                                    "The previous installation was interrupted; "
                                    "enable it again to retry."
                                )
                            ),
                            "service_status": "stopped" if component.installed else None,
                            "service_error": None,
                        },
                    )
            except UpdateLockBusyError:
                # A live installer or another maintenance task still owns the lock.
                return
            except (KnowledgeError, OSError, ValueError) as exc:
                store.patch_state(
                    RERANKER_INSTALL_JOB,
                    {
                        "status": "error",
                        "desired_enabled": False,
                        "cleanup_pending": True,
                        "cleanup_retry_at_epoch": now + 300,
                        "error": f"Interrupted installation cleanup failed: {exc}",
                    },
                )

    def configure_semantic(self, raw_payload: object) -> dict[str, object]:
        try:
            request = DashboardSemanticConfiguration.model_validate(raw_payload)
        except ValueError as exc:
            raise ConfigurationError(f"invalid semantic configuration: {exc}") from exc
        with self._mutex:
            config = load_config(self.config_path)
            self._recover_interrupted_semantic_setup(config)
            database = Database(config.sqlite_path)
            database.initialize()
            store = AutomationStore(database)
            operation = store.state(SEMANTIC_INSTALL_JOB)
            active_status = str(operation.get("status") or "")
            if active_status in {"installing", "indexing"}:
                updated = store.patch_state_if_current(
                    SEMANTIC_INSTALL_JOB,
                    expected_status=active_status,
                    values={"desired_enabled": request.enabled},
                )
                if updated:
                    # Cancellation and reversal are control-plane writes and must
                    # not wait behind the long-running install/index update lock.
                    return self.state()
                config = load_config(self.config_path)
            with UpdateLock(self.config_path.parent.parent / ".update.lock"):
                return self._configure_semantic_while_locked(request, config)

    def _configure_semantic_while_locked(
        self,
        request: DashboardSemanticConfiguration,
        config: AppConfig,
    ) -> dict[str, object]:
        database = Database(config.sqlite_path)
        database.initialize()
        store = AutomationStore(database)
        operation = store.state(SEMANTIC_INSTALL_JOB)
        component = SemanticComponentManager(config).status()
        active_status = str(operation.get("status") or "")
        if not request.enabled:
            self._set_semantic_enabled(False)
            if active_status in {"installing", "indexing"}:
                store.patch_state_if_current(
                    SEMANTIC_INSTALL_JOB,
                    expected_status=active_status,
                    values={"desired_enabled": False},
                )
            else:
                if component.installed:
                    SemanticComponentManager(config).stop_service()
                store.update_state(
                    SEMANTIC_INSTALL_JOB,
                    {
                        **operation,
                        "status": "ready" if component.installed else "not_installed",
                        "desired_enabled": False,
                    },
                )
            return self.state()
        if active_status in {"installing", "indexing"}:
            store.patch_state_if_current(
                SEMANTIC_INSTALL_JOB,
                expected_status=active_status,
                values={"desired_enabled": True},
            )
            return self.state()
        pending_status = (
            "indexing"
            if (
                component.installed
                and component.status == "ready"
                and component.component_version == SEMANTIC_COMPONENT_VERSION
            )
            else "installing"
        )
        pending = {
            "status": pending_status,
            "desired_enabled": True,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "requested_at_epoch": time.time(),
            "boot_id": current_boot_id(),
            "indexed_chunks": 0,
        }
        self._set_semantic_enabled(False)
        store.update_state(SEMANTIC_INSTALL_JOB, pending)
        try:
            process = SemanticInstallWorkerLauncher(
                config_path=self.config_path,
                workspace=config.base_dir,
                errors_path=config.resolve_path(config.paths.errors),
            ).start()
        except Exception as exc:
            store.update_state(
                SEMANTIC_INSTALL_JOB,
                {**pending, "status": "error", "error": str(exc)[:2000]},
            )
            raise
        store.patch_state_if_current(
            SEMANTIC_INSTALL_JOB,
            expected_status=pending_status,
            values={"process_id": process.pid},
        )
        return self.state()

    @staticmethod
    def _semantic_setup_is_stale(
        operation: dict[str, object],
        *,
        now_epoch: float,
    ) -> bool:
        status = operation.get("status")
        if status not in {"installing", "indexing"}:
            return False
        operation_boot = operation.get("boot_id")
        boot = current_boot_id()
        if isinstance(operation_boot, str) and boot is not None and operation_boot != boot:
            return True
        return install_state_is_stale(
            {**operation, "status": "installing"},
            now_epoch=now_epoch,
        )

    def _recover_interrupted_semantic_setup(self, config: AppConfig) -> None:
        """Release a stale dashboard toggle after proving its worker is gone."""

        with self._semantic_recovery_lock:
            database = Database(config.sqlite_path)
            database.initialize()
            store = AutomationStore(database)
            now = time.time()
            operation = store.state(SEMANTIC_INSTALL_JOB)
            if not self._semantic_setup_is_stale(operation, now_epoch=now):
                return
            try:
                with UpdateLock(config.base_dir / ".update.lock", timeout_seconds=0):
                    operation = store.state(SEMANTIC_INSTALL_JOB)
                    if not self._semantic_setup_is_stale(operation, now_epoch=now):
                        return
                    active_status = str(operation.get("status"))
                    self._set_semantic_enabled(False)
                    manager = SemanticComponentManager(load_config(self.config_path))
                    manager.recover_interrupted_promotion()
                    component = manager.status()
                    if component.installed:
                        manager.stop_service()
                    store.patch_state_if_current(
                        SEMANTIC_INSTALL_JOB,
                        expected_status=active_status,
                        values={
                            "status": "error",
                            "desired_enabled": False,
                            "process_id": None,
                            "interrupted": True,
                            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "service_status": "stopped" if component.installed else None,
                            "service_error": None,
                            "error": (
                                "The previous semantic setup was interrupted. "
                                "Enable it again to resume safely."
                            ),
                        },
                    )
            except UpdateLockBusyError:
                # The detached installer or another maintenance operation still
                # owns the authoritative process lock; retry on a later request.
                return
            except (KnowledgeError, OSError, ValueError) as exc:
                store.patch_state(
                    SEMANTIC_INSTALL_JOB,
                    {
                        "status": "error",
                        "desired_enabled": False,
                        "error": f"Interrupted semantic setup recovery failed: {exc}",
                    },
                )

    def _set_semantic_enabled(self, enabled: bool) -> None:
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        payload.setdefault("retrieval", {}).setdefault("semantic", {})["enabled"] = enabled
        self._atomic_yaml_write(self.config_path, payload)
        load_config(self.config_path)

    def remove_semantic(self) -> dict[str, object]:
        with self._mutex, UpdateLock(self.config_path.parent.parent / ".update.lock"):
            config = load_config(self.config_path)
            database = Database(config.sqlite_path)
            database.initialize()
            store = AutomationStore(database)
            operation = store.state(SEMANTIC_INSTALL_JOB)
            if operation.get("status") in {"installing", "indexing"}:
                raise ConfigurationError(
                    "semantic storage cannot be removed while setup or indexing is running"
                )
            self._set_semantic_enabled(False)
            reclaimed = SemanticComponentManager(config).remove()
            store.update_state(
                SEMANTIC_INSTALL_JOB,
                {
                    "status": "not_installed",
                    "desired_enabled": False,
                    "removed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "reclaimed_bytes": reclaimed,
                },
            )
        return self.state()

    def configure_reranker(self, raw_payload: object) -> dict[str, object]:
        try:
            request = DashboardRerankerConfiguration.model_validate(raw_payload)
        except ValueError as exc:
            raise ConfigurationError(f"invalid reranker configuration: {exc}") from exc
        with self._mutex:
            config = load_config(self.config_path)
            database = Database(config.sqlite_path)
            database.initialize()
            store = AutomationStore(database)
            self._recover_interrupted_reranker_install(config, store=store)
            config = load_config(self.config_path)
            operation = store.state(RERANKER_INSTALL_JOB)
            if request.enabled and operation.get("cleanup_pending") is True:
                raise ConfigurationError(
                    "the interrupted reranker installation is still being cleaned up; "
                    "retry after the dashboard reports that cleanup is complete"
                )
            if operation.get("status") == "installing":
                manager = RerankerComponentManager(config)
                if request.enabled:
                    manager.request_service_start()
                else:
                    manager.request_service_stop()
                updated = store.patch_state_if_current(
                    RERANKER_INSTALL_JOB,
                    expected_status="installing",
                    values={"desired_enabled": request.enabled},
                )
                if updated:
                    return self.state()
                # Installation may have completed between the state read and
                # compare-and-swap. Re-read and apply the same user intent to the
                # now steady component instead of silently losing the toggle.
                config = load_config(self.config_path)
                operation = store.state(RERANKER_INSTALL_JOB)
            if operation.get("service_status") in {"starting", "stopping"}:
                manager = RerankerComponentManager(config)
                if request.enabled:
                    manager.request_service_start()
                else:
                    manager.request_service_stop()
                self._set_reranker_enabled(request.enabled)
                updated = store.patch_state(
                    RERANKER_INSTALL_JOB,
                    {"desired_enabled": request.enabled},
                )
                if updated.get("service_status") in {"starting", "stopping"}:
                    return self.state()
                config = load_config(self.config_path)
            with UpdateLock(self.config_path.parent.parent / ".update.lock"):
                return self._configure_reranker_while_locked(
                    request=request,
                    config=config,
                    store=store,
                )

    def _configure_reranker_while_locked(
        self,
        *,
        request: DashboardRerankerConfiguration,
        config: AppConfig,
        store: AutomationStore,
    ) -> dict[str, object]:
        operation = store.state(RERANKER_INSTALL_JOB)
        if operation.get("status") == "installing":
            manager = RerankerComponentManager(config)
            if request.enabled:
                manager.request_service_start()
            else:
                manager.request_service_stop()
            updated = store.patch_state_if_current(
                RERANKER_INSTALL_JOB,
                expected_status="installing",
                values={"desired_enabled": request.enabled},
            )
            if updated:
                return self.state()
            # The worker crossed into its terminal state while the dashboard was
            # waiting for the global maintenance lock. Continue against that new
            # state so this click remains authoritative.
            operation = store.state(RERANKER_INSTALL_JOB)
        manager = RerankerComponentManager(config)
        if not request.enabled:
            manager.request_service_stop()
        # Installation state and service liveness are separate concerns. A current
        # component remains ready while disabled, so re-enabling it only needs to
        # start the existing service; it must not enter the installer workflow.
        component = manager.status()
        if not request.enabled:
            self._set_reranker_enabled(False)
            if component.installed:
                manager.stop_service()
            store.update_state(
                RERANKER_INSTALL_JOB,
                {
                    **operation,
                    "status": "ready" if component.installed else "not_installed",
                    "desired_enabled": False,
                    "service_status": "stopped" if component.installed else None,
                    "service_error": None,
                },
            )
            return self.state()
        if component.installed and component.status == "ready":
            config = self._normalize_reranker_activation_settings(
                config,
                component_installed=True,
            )
            manager = RerankerComponentManager(config)
            manager.request_service_start()
            manager.ensure_service()
            self._set_reranker_enabled(True)
            store.update_state(
                RERANKER_INSTALL_JOB,
                {
                    "status": "ready",
                    "desired_enabled": True,
                    "service_status": "ready",
                    "service_error": None,
                    "component_version": component.component_version,
                    "installed_bytes": component.installed_bytes,
                },
            )
            return self.state()
        launcher = RerankerInstallWorkerLauncher(
            config_path=self.config_path,
            workspace=config.base_dir,
            errors_path=config.resolve_path(config.paths.errors),
        )
        pending = {
            "status": "installing",
            "desired_enabled": True,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "requested_at_epoch": time.time(),
            "boot_id": current_boot_id(),
            "owner_id": launcher.owner_id,
        }
        # Publish ownership before any provisional config write. Long-lived MCP
        # sessions use this intent to distinguish setup from an administrator's
        # disable request and therefore cannot cancel the new installer.
        store.update_state(RERANKER_INSTALL_JOB, pending)
        try:
            config = self._normalize_reranker_activation_settings(
                config,
                component_installed=component.installed,
            )
            self._set_reranker_enabled(False)
            RerankerComponentManager(config).request_service_start()
            process = launcher.start()
        except Exception as exc:
            store.update_state(
                RERANKER_INSTALL_JOB,
                {**pending, "status": "error", "error": str(exc)[:2000]},
            )
            raise
        store.patch_state_if_current(
            RERANKER_INSTALL_JOB,
            expected_status="installing",
            values={"process_id": process.pid},
        )
        return self.state()

    def _normalize_reranker_activation_settings(
        self,
        config: AppConfig,
        *,
        component_installed: bool,
    ) -> AppConfig:
        """Persist the fixed model and safely migrate pre-feature placeholders."""

        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        settings = payload.setdefault("retrieval", {}).setdefault("reranker", {})
        candidates = min(config.retrieval.reranker.candidates, RERANKER_MAX_CANDIDATES)
        if not component_installed and candidates == RERANKER_LEGACY_PLACEHOLDER_CANDIDATES:
            candidates = RERANKER_DEFAULT_CANDIDATES
        changed = (
            settings.get("model") != RERANKER_MODEL_ID or settings.get("candidates") != candidates
        )
        settings["model"] = RERANKER_MODEL_ID
        settings["candidates"] = candidates
        if changed:
            self._atomic_yaml_write(self.config_path, payload)
            return load_config(self.config_path)
        return config

    def _set_reranker_enabled(self, enabled: bool) -> None:
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        payload.setdefault("retrieval", {}).setdefault("reranker", {})["enabled"] = enabled
        self._atomic_yaml_write(self.config_path, payload)
        load_config(self.config_path)

    def remove_reranker(self) -> dict[str, object]:
        with self._mutex, UpdateLock(self.config_path.parent.parent / ".update.lock"):
            config = load_config(self.config_path)
            database = Database(config.sqlite_path)
            database.initialize()
            store = AutomationStore(database)
            operation = store.state(RERANKER_INSTALL_JOB)
            if operation.get("status") == "installing" or operation.get("service_status") in {
                "starting",
                "stopping",
            }:
                raise ConfigurationError(
                    "the reranker cannot be removed while installation or shutdown is running"
                )
            if config.retrieval.reranker.enabled:
                raise ConfigurationError("disable result reranking before removing it")
            reclaimed = RerankerComponentManager(config).remove()
            store.update_state(
                RERANKER_INSTALL_JOB,
                {
                    "status": "not_installed",
                    "desired_enabled": False,
                    "service_status": "stopped",
                    "service_error": None,
                    "removed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "reclaimed_bytes": reclaimed,
                },
            )
        return self.state()

    def prepare_update_restart(self) -> dict[str, str]:
        application = KnowledgeApplication.from_config(self.config_path)
        state = AutomationStore(application.database).state(DASHBOARD_UPDATE_JOB)
        if state.get("status") not in {"pending", "waiting_for_sync", "running"}:
            raise ConfigurationError("no dashboard update is waiting to restart the server")
        return {"status": "ready"}

    def request_update(
        self, *, dashboard_port: int, dashboard_token: str | None = None
    ) -> dict[str, object]:
        with self._mutex:
            application = KnowledgeApplication.from_config(self.config_path)
            managed = load_managed_installation(application.config.base_dir)
            if managed is None or not supports_transactional_updates(
                application.config.base_dir, managed
            ):
                raise ConfigurationError(
                    "dashboard updates require a managed installation; "
                    "run the native installer first"
                )
            store = AutomationStore(application.database)
            current = self._reconcile_dashboard_update_state(
                store,
                store.state(DASHBOARD_UPDATE_JOB),
                server_version=__version__,
            )
            if current.get("status") in {"pending", "waiting_for_sync", "running"}:
                return {
                    "status": "already_running",
                    "target_version": current.get("target_version"),
                }
            release = application.release_update_checker.check(force=True)
            if release.status != "available" or not release.latest_version:
                detail = f": {release.error}" if release.error else ""
                raise ConfigurationError(f"no newer stable release is available{detail}")
            pending = {
                "status": "pending",
                "current_version": release.current_version,
                "target_version": release.latest_version,
                "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            store.update_state(DASHBOARD_UPDATE_JOB, pending)
            try:
                process = DashboardUpdateWorkerLauncher(
                    config_path=self.config_path,
                    workspace=application.config.base_dir,
                    errors_path=application.config.resolve_path(application.config.paths.errors),
                    repository=application.config.updates.repository,
                    target_version=release.latest_version,
                    server_name=self.server_name,
                    openclaw_command=self.openclaw_command,
                    gh_command=None,
                    dashboard_port=dashboard_port,
                    dashboard_token=dashboard_token,
                ).start()
            except Exception as exc:
                store.update_state(
                    DASHBOARD_UPDATE_JOB,
                    {**pending, "status": "error", "error": str(exc)[:1000]},
                )
                raise
            store.update_state(
                DASHBOARD_UPDATE_JOB,
                {**pending, "status": "pending", "process_id": process.pid},
            )
        sync_active = application.get_sync_status().official.automation_status in {
            "pending",
            "running",
        }
        return {
            "status": "queued" if sync_active else "started",
            "process_id": process.pid,
            "target_version": release.latest_version,
        }

    def configure(self, raw_payload: object) -> dict[str, object]:
        try:
            request = DashboardConfiguration.model_validate(raw_payload)
        except ValueError as exc:
            raise ConfigurationError(f"invalid dashboard configuration: {exc}") from exc

        with self._mutex, UpdateLock(self.config_path.parent.parent / ".update.lock"):
            config = load_config(self.config_path)
            reconcile_packaged_official_catalog(config)
            application = KnowledgeApplication(config)
            self._ensure_idle(application)
            available = available_official_versions(
                load_effective_official_catalog(application.config),
                application.catalog,
            )
            product_specs = self._validate_official_selection(
                request.products, available, application.catalog
            )
            project_updates, changed_projects = self._prepare_project_updates(
                request.projects,
                available=available,
                application=application,
            )
            originals = {self.config_path: self.config_path.read_bytes()}
            originals.update({path: path.read_bytes() for path in project_updates})
            try:
                configure_official_docs(
                    self.config_path,
                    product_specs=product_specs,
                    no_products=not product_specs,
                    automatic_sync=request.automatic_sync,
                    bootstrap_on_empty=request.bootstrap_on_empty,
                    interval_hours=request.interval_hours,
                    retain_unselected_versions=request.retain_unselected_versions,
                )
                for path, payload in project_updates.items():
                    self._atomic_yaml_write(path, payload)
                self._reconcile_external_document_roots(config)
                # Reloading performs complete validation and refreshes database metadata.
                updated_application = KnowledgeApplication.from_config(self.config_path)
                for project_id, selection in request.projects.items():
                    if selection.documents_path is not None:
                        updated_application.registry.require(
                            project_id, allow_disabled=True
                        ).documents_path.mkdir(parents=True, exist_ok=True, mode=0o700)
                sync_store = AutomationStore(updated_application.database)
                requested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for project_id in changed_projects:
                    project = updated_application.registry.require(project_id, allow_disabled=True)
                    if (
                        project.status is ProjectStatus.ACTIVE
                        and project.sources_manifest_path is not None
                    ):
                        sync_store.update_state(
                            f"project:{project_id}",
                            {
                                "status": "pending",
                                "project_id": project_id,
                                "reason": "configuration_changed",
                                "requested_at": requested_at,
                            },
                        )
                configuration_changed = any(
                    not path.is_file() or path.read_bytes() != content
                    for path, content in originals.items()
                )
            except Exception:
                for path, content in originals.items():
                    self._atomic_bytes_write(path, content)
                try:
                    KnowledgeApplication.from_config(self.config_path)
                except Exception:
                    LOGGER.exception("failed to refresh metadata after configuration rollback")
                raise
        application = self._apply_saved_configuration(changed=configuration_changed)
        response = self.state()
        response["application"] = application
        response["restart_required"] = application["status"] in {
            "reload_failed",
            "restart_required",
        }
        return response

    def _apply_saved_configuration(self, *, changed: bool) -> dict[str, object]:
        """Apply one persisted configuration without making reload part of the transaction."""

        managed = load_managed_installation(self.config_path.parent.parent)
        client = managed.client if managed is not None else "unmanaged"
        if not changed:
            return {
                "client": client,
                "status": "unchanged",
                "error_code": None,
            }
        if managed is None or managed.client != "openclaw":
            return {
                "client": client,
                "status": "restart_required",
                "error_code": None,
            }
        try:
            reload_openclaw(managed.openclaw_command or self.openclaw_command)
        except Exception:
            LOGGER.exception("configuration was saved but OpenClaw MCP reload failed")
            return {
                "client": "openclaw",
                "status": "reload_failed",
                "error_code": OPENCLAW_INTEGRATION_ERROR,
            }
        return {
            "client": "openclaw",
            "status": "applied",
            "error_code": None,
        }

    def create_project(self, raw_payload: object) -> dict[str, object]:
        try:
            request = DashboardProjectCreate.model_validate(raw_payload)
        except ValueError as exc:
            raise ConfigurationError(f"invalid project: {exc}") from exc

        with self._mutex, UpdateLock(self.config_path.parent.parent / ".update.lock"):
            config = load_config(self.config_path)
            application = KnowledgeApplication(config)
            self._ensure_idle(application)
            if request.project_id in {
                project.id for project in application.registry.all_projects()
            }:
                raise ConfigurationError(f"project already exists: {request.project_id}")

            projects_dir = config.projects_config_path
            project_path = projects_dir / f"{request.project_id}.yaml"
            manifest_path = projects_dir / f"{request.project_id}.sources.yaml"
            if project_path.exists() or manifest_path.exists():
                raise ConfigurationError(
                    f"project configuration already exists: {request.project_id}"
                )
            documents_path = self._resolve_project_documents_path(
                config,
                request.project_id,
                request.documents_path,
            )
            documents_value = self._path_for_yaml(config, documents_path)
            manifest_value = self._path_for_yaml(config, manifest_path)
            project_payload: dict[str, Any] = {
                "schema_version": 1,
                "id": request.project_id,
                "name": request.name,
                "description": request.description or None,
                "status": "active",
                "classification": request.classification.value,
                "documents": {
                    "path": documents_value,
                    "sources_manifest": manifest_value,
                },
                "languages": [request.language.casefold()],
                "bmc": {"products": {}},
                "tags": [],
            }
            manifest_payload: dict[str, Any] = {
                "schema_version": 1,
                "project_id": request.project_id,
                "sources": [
                    {
                        "path": "**/*",
                        "document_type": "other",
                        "language": request.language.casefold(),
                        "products": {},
                        "metadata": {"dashboard_managed": True},
                    }
                ],
            }
            main_original = self.config_path.read_bytes()
            projects_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            documents_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                projects_dir.chmod(0o700)
                if self._path_is_within(documents_path, config.sources_path):
                    documents_path.chmod(0o700)
            try:
                self._atomic_yaml_write(project_path, project_payload)
                self._atomic_yaml_write(manifest_path, manifest_payload)
                self._reconcile_external_document_roots(config)
                updated_application = KnowledgeApplication.from_config(self.config_path)
                AutomationStore(updated_application.database).update_state(
                    f"project:{request.project_id}",
                    {
                        "status": "pending",
                        "project_id": request.project_id,
                        "reason": "project_created",
                        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                )
            except Exception:
                project_path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
                self._atomic_bytes_write(self.config_path, main_original)
                try:
                    DocumentStore(application.database).remove_project_index(request.project_id)
                    KnowledgeApplication.from_config(self.config_path)
                except Exception:
                    LOGGER.exception("failed to refresh metadata after project creation rollback")
                raise
        return self.state()

    def remove_project(self, raw_payload: object) -> dict[str, object]:
        try:
            request = DashboardProjectRemove.model_validate(raw_payload)
        except ValueError as exc:
            raise ConfigurationError(f"invalid project removal: {exc}") from exc

        with self._mutex, UpdateLock(self.config_path.parent.parent / ".update.lock"):
            config = load_config(self.config_path)
            application = KnowledgeApplication(config)
            self._ensure_idle(application)
            loaded = application.registry.get_loaded(request.project_id)
            project = loaded.project
            main_original = self.config_path.read_bytes()
            archive_root = config.projects_config_path / ".removed"
            archive_dir = archive_root / (
                f"{request.project_id}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-"
                f"{secrets.token_hex(3)}"
            )
            archive_dir.mkdir(parents=True, exist_ok=False)
            moved: list[tuple[Path, Path]] = []
            try:
                destination = archive_dir / loaded.config_path.name
                loaded.config_path.replace(destination)
                moved.append((destination, loaded.config_path))
                manifest_path = project.sources_manifest_path
                if manifest_path is not None and manifest_path.is_file():
                    manifest_destination = archive_dir / manifest_path.name
                    manifest_path.replace(manifest_destination)
                    moved.append((manifest_destination, manifest_path))
                self._reconcile_external_document_roots(
                    config,
                    removed_project_id=request.project_id,
                )
                KnowledgeApplication.from_config(self.config_path)
                DocumentStore(application.database).remove_project_index(
                    request.project_id,
                    queue_vectors=application.vector_index.enabled,
                )
            except Exception:
                for source, destination in reversed(moved):
                    if source.exists():
                        source.replace(destination)
                self._atomic_bytes_write(self.config_path, main_original)
                try:
                    KnowledgeApplication.from_config(self.config_path)
                except Exception:
                    LOGGER.exception("failed to refresh metadata after project removal rollback")
                raise
            cleanup = drain_vector_cleanup(application.database, application.vector_index)
            if cleanup.status == "error":  # lexical isolation is already complete
                LOGGER.warning(
                    "project %s was removed but vector cleanup remains pending: %s",
                    request.project_id,
                    cleanup.error,
                )
        return self.state()

    def browse_directories(self, raw_payload: object) -> dict[str, object]:
        """List local folders and files for the authenticated folder picker."""
        try:
            request = DashboardDirectoryBrowse.model_validate(raw_payload)
        except ValueError as exc:
            raise ConfigurationError(f"invalid directory browser request: {exc}") from exc

        config = load_config(self.config_path)
        roots = self._directory_browser_roots(config)
        requested = (
            self._resolve_local_path(config, request.path)
            if request.path
            else (config.sources_path / "projects").resolve()
        )
        target = requested
        while not target.exists() and target.parent != target:
            target = target.parent
        if not target.exists() or not target.is_dir():
            raise ConfigurationError(f"folder is not accessible: {requested}")
        if not any(self._path_is_within(target, root_path) for _, root_path in roots):
            raise ConfigurationError(
                "folder browsing is limited to the available local locations; "
                "enter another path manually if needed"
            )

        try:
            with os.scandir(target) as scanned:
                scanned_entries = list(scanned)
        except OSError as exc:
            raise ConfigurationError(f"cannot read folder {target}: {exc}") from exc
        directories = sorted(
            (entry for entry in scanned_entries if self._directory_entry_is(entry, directory=True)),
            key=lambda entry: entry.name.casefold(),
        )
        files = sorted(
            (
                entry
                for entry in scanned_entries
                if self._directory_entry_is(entry, directory=False)
            ),
            key=lambda entry: entry.name.casefold(),
        )
        limit = 500
        reserved_file_slots = min(len(files), 100)
        visible_directories = directories[: limit - reserved_file_slots]
        visible_files = files[: max(0, limit - len(visible_directories))]
        allowed_extensions = {
            extension.casefold() for extension in config.ingestion.allowed_extensions
        } & SUPPORTED_DOCUMENT_EXTENSIONS
        max_file_size = config.ingestion.max_file_size_mb * 1024 * 1024
        parent = target.parent
        parent_path = (
            str(parent)
            if parent != target
            and any(self._path_is_within(parent, root_path) for _, root_path in roots)
            else None
        )
        selection_error: str | None = None
        try:
            self._validate_project_documents_path(config, target)
        except ConfigurationError as exc:
            selection_error = str(exc)
        navigation_roots = {
            root_path for label, root_path in roots if not label.startswith("Project folder ·")
        }
        if selection_error is None and target in navigation_roots:
            selection_error = "open a dedicated subfolder before selecting it"
        return {
            "path": str(target),
            "parent": parent_path,
            "roots": [{"label": label, "path": str(root_path)} for label, root_path in roots],
            "entries": [
                {"name": entry.name, "path": str(target / entry.name)}
                for entry in visible_directories
            ],
            "files": [
                self._folder_file_summary(
                    entry,
                    allowed_extensions=allowed_extensions,
                    max_file_size=max_file_size,
                    max_file_size_mb=config.ingestion.max_file_size_mb,
                )
                for entry in visible_files
            ],
            "supported_extensions": sorted(allowed_extensions),
            "truncated": len(directories) + len(files) > limit,
            "selectable": selection_error is None,
            "selection_error": selection_error,
        }

    def request_project_sync(self, raw_payload: object) -> dict[str, object]:
        """Queue an immediate project reconciliation for the elected local watcher."""
        try:
            request = DashboardProjectSync.model_validate(raw_payload)
        except ValueError as exc:
            raise ConfigurationError(f"invalid project synchronization request: {exc}") from exc

        with self._mutex:
            application = KnowledgeApplication.from_config(self.config_path)
            project = application.registry.require(request.project_id, allow_disabled=True)
            if project.status is not ProjectStatus.ACTIVE:
                raise ConfigurationError(
                    f"project is not active and cannot be indexed: {request.project_id}"
                )
            if project.sources_manifest_path is None:
                raise ConfigurationError(
                    f"project does not declare a source manifest: {request.project_id}"
                )
            store = AutomationStore(application.database)
            job_id = f"project:{request.project_id}"
            current = store.state(job_id)
            if current.get("status") == "pending":
                return {"status": "already_pending", "project_id": request.project_id}
            if store.running_state_is_active(current):
                return {"status": "already_running", "project_id": request.project_id}
            requested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            store.update_state(
                job_id,
                {
                    "status": "pending",
                    "project_id": request.project_id,
                    "reason": "user_requested",
                    "requested_at": requested_at,
                },
            )
        return {"status": "started", "project_id": request.project_id}

    def request_sync(self) -> dict[str, object]:
        with self._mutex:
            application = KnowledgeApplication.from_config(self.config_path)
            if (
                not application.config.official_docs.products
                and application.config.official_docs.retain_unselected_versions
            ):
                raise ConfigurationError("select at least one product before synchronizing")
            store = AutomationStore(application.database)
            current = store.state("official-docs")
            if store.running_state_is_active(current) or recent_dashboard_request(current):
                return {"status": "already_running", "sync": self.state()["sync"]}
            store.update_state(
                "official-docs",
                {
                    **current,
                    "status": "pending",
                    "reason": "dashboard_request",
                    "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "next_run_epoch": 0,
                },
            )
            process = application.create_official_sync_worker_launcher().start(force=True)
        return {"status": "started", "process_id": process.pid}

    def request_sync_cancellation(self) -> dict[str, object]:
        """Request a cooperative stop without terminating the worker process."""

        with self._mutex:
            application = KnowledgeApplication.from_config(self.config_path)
            store = AutomationStore(application.database)
            current = store.state("official-docs")
            status = current.get("status")
            requested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if status == "pending":
                delay = application.config.official_docs.interval_hours * 3600
                next_run = time.time() + delay
                updated = store.patch_state_if_current(
                    "official-docs",
                    expected_status="pending",
                    values={
                        "status": "cancelled",
                        "reason": "user_requested",
                        "cancel_requested": False,
                        "finished_at": requested_at,
                        "next_run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(next_run)),
                        "next_run_epoch": next_run,
                    },
                )
                return {"status": "cancelled" if updated else "state_changed"}
            if status != "running":
                return {"status": "not_running"}
            if not store.running_state_is_active(current):
                store.patch_state_if_current(
                    "official-docs",
                    expected_status="running",
                    values={
                        "status": "interrupted",
                        "reason": "worker_lease_expired",
                        "cancel_requested": False,
                        "finished_at": requested_at,
                    },
                )
                return {"status": "not_running"}
            if current.get("cancel_requested") is True:
                return {"status": "already_requested"}
            updated = store.patch_state_if_current(
                "official-docs",
                expected_status="running",
                values={
                    "cancel_requested": True,
                    "cancel_requested_at": requested_at,
                },
            )
            return {"status": "requested" if updated else "state_changed"}

    @staticmethod
    def _ensure_idle(application: KnowledgeApplication) -> None:
        store = AutomationStore(application.database)
        state = store.state("official-docs")
        if store.running_state_is_active(state) or recent_dashboard_request(state):
            raise ConfigurationError(
                "official synchronization is running; wait until it finishes before saving"
            )
        if state.get("status") in {"pending", "running"}:
            DashboardService._mark_abandoned_state(store, "official-docs", state)
        running_projects: list[str] = []
        for project in application.registry.all_projects():
            job_id = f"project:{project.id}"
            project_state = store.state(job_id)
            if store.running_state_is_active(project_state):
                running_projects.append(project.id)
            elif project_state.get("status") == "running":
                DashboardService._mark_abandoned_state(store, job_id, project_state)
        if running_projects:
            raise ConfigurationError(
                "project synchronization is running; wait before saving: "
                + ", ".join(running_projects)
            )

    @staticmethod
    def _mark_abandoned_state(
        store: AutomationStore,
        job_id: str,
        state: dict[str, object],
    ) -> None:
        store.update_state(
            job_id,
            {
                **state,
                "status": "interrupted",
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error": "previous worker lease expired",
            },
        )

    @staticmethod
    def _validate_official_selection(
        requested: dict[str, list[str]],
        available: dict[str, list[str]],
        catalog: ProductCatalog,
    ) -> list[str]:
        specs: list[str] = []
        for configured_product, versions in sorted(requested.items()):
            product = catalog.resolve(configured_product).product_id
            for raw_version in versions:
                version = normalize_version(raw_version)
                if version not in available.get(product, []):
                    choices = ", ".join(available.get(product, [])) or "none"
                    raise ConfigurationError(
                        f"official documentation {product}={version} is unavailable; "
                        f"available versions: {choices}"
                    )
                specs.append(f"{product}={version}")
        return specs

    def _prepare_project_updates(
        self,
        requested: dict[str, DashboardProjectSelection],
        *,
        available: dict[str, list[str]],
        application: KnowledgeApplication,
    ) -> tuple[dict[Path, dict[str, Any]], set[str]]:
        updates: dict[Path, dict[str, Any]] = {}
        changed_projects: set[str] = set()
        known_projects = {project.id for project in application.registry.all_projects()}
        unknown = sorted(set(requested) - known_projects)
        if unknown:
            raise ConfigurationError(f"unknown projects: {', '.join(unknown)}")
        for project_id, selection in requested.items():
            loaded = application.registry.get_loaded(project_id)
            original_payload = yaml.safe_load(loaded.config_path.read_text(encoding="utf-8")) or {}
            payload = deepcopy(original_payload)
            current_products = {
                product_id: settings.version
                for product_id, settings in loaded.project.bmc_products.items()
            }
            products: dict[str, dict[str, str]] = {}
            for configured_product, raw_version in sorted(selection.products.items()):
                product = application.catalog.resolve(configured_product).product_id
                version = normalize_version(raw_version)
                if (
                    version not in available.get(product, [])
                    and current_products.get(product) != version
                ):
                    choices = ", ".join(available.get(product, [])) or "none"
                    raise ConfigurationError(
                        f"project {project_id} cannot use {product}={version}; "
                        f"available versions: {choices}"
                    )
                products[product] = {"version": version}
            payload.setdefault("bmc", {})["products"] = products
            if selection.documents_path is not None:
                documents_path = self._resolve_project_documents_path(
                    application.config,
                    project_id,
                    selection.documents_path,
                )
                payload.setdefault("documents", {})["path"] = self._path_for_yaml(
                    application.config,
                    documents_path,
                )
            if payload != original_payload:
                updates[loaded.config_path] = payload
                changed_projects.add(project_id)
            manifest_path = loaded.project.sources_manifest_path
            if manifest_path is not None and manifest_path.is_file():
                original_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
                manifest = deepcopy(original_manifest)
                managed_sources = [
                    source
                    for source in manifest.get("sources", [])
                    if isinstance(source, dict)
                    and isinstance(source.get("metadata"), dict)
                    and source["metadata"].get("dashboard_managed") is True
                ]
                if managed_sources:
                    for source in managed_sources:
                        source["products"] = {product: None for product in products}
                    if manifest != original_manifest:
                        updates[manifest_path] = manifest
                        changed_projects.add(project_id)
        return updates, changed_projects

    @staticmethod
    def _project_index_counts(database: Database) -> dict[str, dict[str, int]]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.project_id AS project_id,
                       count(DISTINCT d.document_id) AS documents,
                       count(c.chunk_id) AS chunks
                FROM documents AS d
                JOIN chunks AS c
                  ON c.document_id = d.document_id
                 AND c.active = 1
                WHERE d.source_scope = 'project'
                  AND d.status = 'indexed'
                  AND d.project_id IS NOT NULL
                GROUP BY d.project_id
                """
            ).fetchall()
        return {
            str(row["project_id"]): {
                "documents": int(row["documents"]),
                "chunks": int(row["chunks"]),
            }
            for row in rows
        }

    @staticmethod
    def _project_sync_summary(
        *,
        project: Project,
        state: dict[str, object],
        counts: dict[str, int],
        active_owners: set[str],
        automatic_sync: bool,
    ) -> dict[str, object]:
        raw_status = str(state.get("status") or "")
        owner_id = state.get("owner_id")
        if project.status is not ProjectStatus.ACTIVE:
            status = project.status.value
        elif raw_status == "running" and owner_id in active_owners:
            status = "running"
        elif raw_status in {"running", "interrupted"}:
            status = "interrupted"
        elif raw_status in {"pending", "waiting", "error", "cancelled"}:
            status = raw_status
        elif raw_status == "ok" or counts["documents"] > 0:
            status = "ready"
        else:
            status = "not_started"

        detected_files = state.get("detected_files")
        if not isinstance(detected_files, int) or detected_files < 0:
            detected_files = None
        raw_results = state.get("result_counts")
        result_counts = (
            {
                str(key): int(value)
                for key, value in raw_results.items()
                if isinstance(key, str) and isinstance(value, int) and value >= 0
            }
            if isinstance(raw_results, dict)
            else {}
        )
        raw_errors = state.get("errors")
        errors = (
            [str(value)[:500] for value in raw_errors if value]
            if isinstance(raw_errors, list)
            else []
        )
        if not errors and state.get("error"):
            errors = [str(state["error"])[:500]]
        return {
            "status": status,
            "available": (
                project.status is ProjectStatus.ACTIVE and project.sources_manifest_path is not None
            ),
            "automatic_sync": (
                automatic_sync
                and project.status is ProjectStatus.ACTIVE
                and project.sources_manifest_path is not None
            ),
            "detected_files": detected_files,
            "indexed_documents": counts["documents"],
            "indexed_chunks": counts["chunks"],
            "result_counts": result_counts,
            "error_count": len(errors),
            "error": errors[0] if errors else None,
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "duration_seconds": state.get("duration_seconds"),
        }

    @staticmethod
    def _directory_entry_is(entry: os.DirEntry[str], *, directory: bool) -> bool:
        try:
            return (
                entry.is_dir(follow_symlinks=False)
                if directory
                else entry.is_file(follow_symlinks=False)
            )
        except OSError:
            return False

    @staticmethod
    def _directory_entry_size(entry: os.DirEntry[str]) -> int | None:
        try:
            return max(0, int(entry.stat(follow_symlinks=False).st_size))
        except OSError:
            return None

    @classmethod
    def _folder_file_summary(
        cls,
        entry: os.DirEntry[str],
        *,
        allowed_extensions: set[str] | frozenset[str],
        max_file_size: int,
        max_file_size_mb: int,
    ) -> dict[str, object]:
        extension = Path(entry.name).suffix.casefold()
        size = cls._directory_entry_size(entry)
        if extension not in allowed_extensions:
            supported = False
            support_reason = "unsupported format"
        elif size is None:
            supported = False
            support_reason = "size unavailable"
        elif size > max_file_size:
            supported = False
            support_reason = f"exceeds {max_file_size_mb} MB limit"
        else:
            supported = True
            support_reason = "indexable"
        return {
            "name": entry.name,
            "extension": extension,
            "size_bytes": size,
            "supported": supported,
            "support_reason": support_reason,
        }

    def _resolve_project_documents_path(
        self,
        config,
        project_id: str,
        raw_path: str | None,
    ) -> Path:
        if raw_path is None or not raw_path.strip():
            resolved = (config.sources_path / "projects" / project_id / "docs").resolve()
        else:
            resolved = self._resolve_local_path(config, raw_path)
        self._validate_project_documents_path(config, resolved)
        return resolved

    @staticmethod
    def _resolve_local_path(config, raw_path: str) -> Path:
        value = raw_path.strip()
        windows_path = re.fullmatch(r"([A-Za-z]):[\\/](.*)", value)
        if windows_path and os.name != "nt":
            mounted = Path("/mnt") / windows_path.group(1).casefold()
            if not mounted.is_dir():
                raise ConfigurationError(
                    "Windows paths can only be used from WSL when their /mnt drive is mounted"
                )
            suffix = windows_path.group(2).replace("\\", "/")
            return (mounted / suffix).expanduser().resolve()
        return config.resolve_path(Path(value).expanduser())

    def _directory_browser_roots(self, config) -> list[tuple[str, Path]]:
        candidates: list[tuple[str, Path]] = [
            ("Managed projects", (config.sources_path / "projects").resolve()),
            ("Home", Path.home().resolve()),
        ]
        for root in config.projects.external_document_roots:
            candidates.append((f"Project folder · {root.name}", config.resolve_path(root)))
        if os.name == "nt":  # pragma: no cover - exercised on Windows installations
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                drive = Path(f"{letter}:\\")
                if drive.is_dir():
                    candidates.append((f"{letter}: drive", drive.resolve()))
        else:
            mount_root = Path("/mnt")
            if mount_root.is_dir():
                for drive in sorted(mount_root.iterdir(), key=lambda path: path.name.casefold()):
                    if (
                        len(drive.name) == 1
                        and drive.name.isalpha()
                        and drive.is_dir()
                        and not drive.is_symlink()
                    ):
                        candidates.append((f"Windows {drive.name.upper()}:", drive.resolve()))

        roots: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        for label, path in candidates:
            if path in seen or not path.is_dir():
                continue
            seen.add(path)
            roots.append((label, path))
        return roots

    def _validate_project_documents_path(self, config, path: Path) -> None:
        protected = {
            config.base_dir.resolve(),
            config.sources_path.resolve(),
            config.projects_config_path.resolve(),
            Path.home().resolve(),
        }
        if os.name != "nt":
            mount_root = Path("/mnt")
            if mount_root.is_dir():
                protected.update(
                    drive.resolve()
                    for drive in mount_root.iterdir()
                    if len(drive.name) == 1
                    and drive.name.isalpha()
                    and drive.is_dir()
                    and not drive.is_symlink()
                )
        if path.parent == path or path in protected:
            raise ConfigurationError("select a dedicated project documentation folder")
        if self._path_is_within(config.base_dir, path):
            raise ConfigurationError(
                "project documentation cannot contain the application workspace"
            )
        if self._path_is_within(path, config.projects_config_path) or self._path_is_within(
            config.projects_config_path, path
        ):
            raise ConfigurationError(
                "project documentation cannot overlap the project configuration folder"
            )
        if path.exists() and not path.is_dir():
            raise ConfigurationError(f"project documentation path is not a directory: {path}")
        if path.is_symlink():
            raise ConfigurationError("project documentation root cannot be a symbolic link")

    def _reconcile_external_document_roots(
        self,
        config,
        *,
        removed_project_id: str | None = None,
    ) -> None:
        roots: set[str] = set()
        configured_ids: set[str] = set()
        for path in project_config_paths(config.projects_config_path):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            project_id = str(payload.get("id") or "")
            raw_documents = payload.get("documents")
            if not project_id or not isinstance(raw_documents, dict):
                continue
            configured_ids.add(project_id)
            raw_path = raw_documents.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            documents_path = config.resolve_path(Path(raw_path).expanduser())
            if not self._path_is_within(documents_path, config.sources_path):
                roots.add(str(documents_path))

        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        projects = payload.setdefault("projects", {})
        projects["external_document_roots"] = sorted(roots)
        default_project = projects.get("default_project")
        if (
            removed_project_id is not None and default_project == removed_project_id
        ) or default_project not in configured_ids:
            projects["default_project"] = None
        self._atomic_yaml_write(self.config_path, payload)

    @staticmethod
    def _path_is_within(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _path_for_yaml(config, path: Path) -> str:
        try:
            return path.resolve().relative_to(config.base_dir.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    @staticmethod
    def _atomic_yaml_write(path: Path, payload: dict[str, Any]) -> None:
        serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode()
        DashboardService._atomic_bytes_write(path, serialized)

    @staticmethod
    def _atomic_bytes_write(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_bytes(content)
            if os.name != "nt":
                temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _reconcile_dashboard_update_state(
        store: AutomationStore,
        state: dict[str, object],
        *,
        server_version: str,
    ) -> dict[str, object]:
        """Recover update state after a worker or dashboard was interrupted."""

        if state.get("status") not in {"pending", "waiting_for_sync", "running"}:
            return state
        if state.get("target_version") == server_version:
            reconciled = {
                **state,
                "status": "success",
                "current_version": server_version,
                "finished_at": datetime.now(UTC).isoformat(),
                "result_status": "updated",
            }
            reconciled.pop("process_id", None)
            reconciled.pop("error", None)
            store.update_state(DASHBOARD_UPDATE_JOB, reconciled)
            return reconciled

        process_id = state.get("process_id")
        if isinstance(process_id, int) and not isinstance(process_id, bool):
            if _dashboard_update_worker_is_active(process_id):
                return state
        elif _dashboard_update_request_is_recent(state):
            return state

        reconciled = {
            **state,
            "status": "error",
            "current_version": server_version,
            "finished_at": datetime.now(UTC).isoformat(),
            "error": (
                "the dashboard update worker stopped unexpectedly; "
                "check its log and retry the update"
            ),
        }
        reconciled.pop("process_id", None)
        store.update_state(DASHBOARD_UPDATE_JOB, reconciled)
        return reconciled

    @staticmethod
    def _dashboard_update_state(
        state: dict[str, object], *, server_version: str
    ) -> dict[str, object] | None:
        if not state:
            return None
        allowed = {
            "status",
            "current_version",
            "target_version",
            "requested_at",
            "started_at",
            "waiting_since",
            "finished_at",
            "process_id",
            "dashboard_process_id",
            "dashboard_runtime",
            "result_status",
            "gateway_restarted",
            "gateway_warning",
            "error",
            "storage_retention",
        }
        filtered = {key: state[key] for key in allowed if key in state}
        status = filtered.get("status")
        if status == "success" and filtered.get("target_version") != server_version:
            return {"status": "success", "target_version": server_version}
        if status == "error" and filtered.get("current_version") != server_version:
            return None
        return filtered

    def _client_integrations(self, workspace: Path) -> dict[str, object]:
        managed = load_managed_installation(workspace)
        openclaw_connected = bool(managed and managed.client == "openclaw")
        return {
            "managed": managed is not None,
            "transactional_updates": bool(
                managed and supports_transactional_updates(workspace, managed)
            ),
            "active_client": managed.client if managed else "unmanaged",
            "openclaw": {
                "name": "OpenClaw",
                "connected": openclaw_connected,
                "status": "connected" if openclaw_connected else "not_connected",
                "description": (
                    "Registered and ready for new OpenClaw sessions."
                    if openclaw_connected
                    else (
                        "Not detected during installation. Helix Knowledge remains "
                        "available as a standalone MCP server."
                    )
                ),
            },
        }

    def _dashboard_runtime(self, workspace: Path) -> dict[str, object]:
        managed = load_managed_installation(workspace)
        if managed is None:
            return {
                "manager": "unmanaged",
                "installed": False,
                "enabled": False,
                "active": True,
                "status": "running",
                "restart_policy": "none",
                "service_name": None,
                "launcher": None,
                "port": self.dashboard_port,
                "process_managed": False,
                "error": None,
            }
        return (
            DashboardRuntimeManager(
                managed,
                server_name=self.server_name,
                openclaw_command=self.openclaw_command,
            )
            .status()
            .to_dict()
        )


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: DashboardService,
        token: str,
    ) -> None:
        super().__init__(address, DashboardRequestHandler)
        self.service = service
        self.dashboard_token = token


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def do_GET(self) -> None:
        if not self._trusted_host():
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid request host"})
            return
        path = urlsplit(self.path).path
        if path == "/":
            template = (
                files("helix_mcp_knowledge.resources")
                .joinpath("dashboard", "index.html")
                .read_text(encoding="utf-8")
            )
            html = template.replace("__DASHBOARD_TOKEN__", self.server.dashboard_token)
            self._send(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            if self.headers.get("X-Helix-Dashboard-Token") != self.server.dashboard_token:
                self._json(HTTPStatus.FORBIDDEN, {"error": "invalid dashboard token"})
                return
            origin = self.headers.get("Origin")
            if origin and not self._trusted_origin(origin):
                self._json(HTTPStatus.FORBIDDEN, {"error": "invalid request origin"})
                return
            self._json(HTTPStatus.OK, self.server.service.state())
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, self.server.service.health())
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._trusted_host():
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid request host"})
            return
        if self.headers.get("X-Helix-Dashboard-Token") != self.server.dashboard_token:
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid dashboard token"})
            return
        origin = self.headers.get("Origin")
        if origin and not self._trusted_origin(origin):
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid request origin"})
            return
        try:
            path = urlsplit(self.path).path
            if path == "/api/configuration":
                self._json(HTTPStatus.OK, self.server.service.configure(self._read_json()))
                return
            if path == "/api/cleanup/preview":
                self._json(
                    HTTPStatus.OK,
                    self.server.service.cleanup_preview(self._read_json()),
                )
                return
            if path == "/api/projects/create":
                self._json(
                    HTTPStatus.CREATED,
                    self.server.service.create_project(self._read_json()),
                )
                return
            if path == "/api/projects/remove":
                self._json(HTTPStatus.OK, self.server.service.remove_project(self._read_json()))
                return
            if path == "/api/projects/sync":
                self._json(
                    HTTPStatus.ACCEPTED,
                    self.server.service.request_project_sync(self._read_json()),
                )
                return
            if path == "/api/directories/browse":
                self._json(
                    HTTPStatus.OK,
                    self.server.service.browse_directories(self._read_json()),
                )
                return
            if path == "/api/sync":
                self._json(HTTPStatus.ACCEPTED, self.server.service.request_sync())
                return
            if path == "/api/sync/cancel":
                self._json(
                    HTTPStatus.ACCEPTED,
                    self.server.service.request_sync_cancellation(),
                )
                return
            if path == "/api/update/check":
                self._json(HTTPStatus.OK, self.server.service.check_update())
                return
            if path == "/api/catalog/refresh":
                self._json(HTTPStatus.OK, self.server.service.refresh_catalog())
                return
            if path == "/api/ocr/configuration":
                self._json(
                    HTTPStatus.ACCEPTED,
                    self.server.service.configure_ocr(self._read_json()),
                )
                return
            if path == "/api/semantic/configuration":
                self._json(
                    HTTPStatus.ACCEPTED,
                    self.server.service.configure_semantic(self._read_json()),
                )
                return
            if path == "/api/semantic/remove":
                self._json(
                    HTTPStatus.OK,
                    self.server.service.remove_semantic(),
                )
                return
            if path == "/api/reranker/configuration":
                self._json(
                    HTTPStatus.ACCEPTED,
                    self.server.service.configure_reranker(self._read_json()),
                )
                return
            if path == "/api/reranker/remove":
                self._json(
                    HTTPStatus.OK,
                    self.server.service.remove_reranker(),
                )
                return
            if path == "/api/update/install":
                result = self.server.service.request_update(
                    dashboard_port=self.server.server_address[1],
                    dashboard_token=self.server.dashboard_token,
                )
                self._json(HTTPStatus.ACCEPTED, result)
                return
            if path == "/api/update/prepare":
                self._json(HTTPStatus.ACCEPTED, self.server.service.prepare_update_restart())
                threading.Thread(
                    target=self.server.shutdown,
                    name="helix-dashboard-update-shutdown",
                    daemon=True,
                ).start()
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (KnowledgeError, ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            LOGGER.exception("unexpected dashboard request failure for %s", self.path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "unexpected server error"})

    def _trusted_origin(self, origin: str) -> bool:
        try:
            parsed = urlsplit(origin)
            port = parsed.port or (80 if parsed.scheme == "http" else None)
        except ValueError:
            return False
        return bool(
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.username is None
            and parsed.password is None
            and port == self.server.server_address[1]
        )

    def _trusted_host(self) -> bool:
        raw_host = self.headers.get("Host", "")
        try:
            parsed = urlsplit(f"//{raw_host}")
            port = parsed.port or 80
        except ValueError:
            return False
        return bool(
            parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.username is None
            and parsed.password is None
            and port == self.server.server_address[1]
        )

    def _read_json(self) -> object:
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdigit():
            raise ValueError("missing or invalid Content-Length")
        length = int(raw_length)
        if length > MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("Content-Type must be application/json")
        return json.loads(self.rfile.read(length))

    def _json(self, status: HTTPStatus, payload: object) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode(),
            "application/json; charset=utf-8",
        )

    def _send(self, status: HTTPStatus, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


def run_dashboard(
    config_path: str | Path,
    *,
    port: int = DEFAULT_DASHBOARD_PORT,
    open_browser: bool = True,
    server_name: str = DEFAULT_SERVER_NAME,
    openclaw_command: str | Path = "openclaw",
) -> None:
    if not 0 <= port <= 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    service = DashboardService(
        config_path,
        server_name=server_name,
        openclaw_command=openclaw_command,
        dashboard_port=port,
    )
    server = DashboardHTTPServer(
        (LOOPBACK_HOST, port),
        service=service,
        token=secrets.token_urlsafe(32),
    )
    actual_port = server.server_address[1]
    url = f"http://{LOOPBACK_HOST}:{actual_port}/"
    print(f"Helix Knowledge dashboard: {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    if service.catalog_update_checker.settings.enabled:
        service.catalog_update_checker.start()
    if service.release_update_checker.settings.enabled:
        service.release_update_checker.start()
    project_sync_coordinator = None
    dashboard_application = KnowledgeApplication.from_config(config_path)
    drain_vector_cleanup(
        dashboard_application.database,
        dashboard_application.vector_index,
    )
    # The persistent dashboard always owns a project coordinator. When automatic
    # watching is disabled it remains idle except for explicit dashboard requests.
    project_sync_coordinator = dashboard_application.create_sync_coordinator()
    project_sync_coordinator.start()
    official_sync_scheduler = PersistentOfficialSyncScheduler(
        Path(config_path).expanduser().resolve()
    )
    official_sync_scheduler.start()
    if not os.environ.get(DASHBOARD_MODE_ENV):
        threading.Thread(
            target=_adopt_managed_dashboard,
            kwargs={
                "config_path": Path(config_path).expanduser().resolve(),
                "port": actual_port,
                "server_name": server_name,
                "openclaw_command": openclaw_command,
            },
            name="helix-dashboard-runtime-adoption",
            daemon=True,
        ).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if project_sync_coordinator is not None:
            project_sync_coordinator.stop(timeout=0.5)
        official_sync_scheduler.stop(timeout=0.5)
        service.release_update_checker.stop(timeout=0.25)
        service.catalog_update_checker.stop(timeout=0.25)
        server.server_close()


def _adopt_managed_dashboard(
    *,
    config_path: Path,
    port: int,
    server_name: str,
    openclaw_command: str | Path,
) -> None:
    """Migrate dashboards launched by an older release without interrupting their port."""

    try:
        workspace = config_path.parent.parent
        managed = load_managed_installation(workspace)
        if managed is None:
            return
        if not managed.dashboard_launcher.is_file() or managed.dashboard_port != port:
            _, server = versioned_runtime_paths(workspace, managed.active_version)
            if not server.is_file():
                return
            managed = activate_managed_installation(
                workspace=workspace,
                version=managed.active_version,
                server_command=server,
                config_path=managed.config_path,
                client=managed.client,
                server_name=managed.server_name,
                openclaw_command=managed.openclaw_command,
                dashboard_port=port,
            )
        DashboardRuntimeManager(
            managed,
            server_name=server_name,
            openclaw_command=openclaw_command,
        ).install_and_start(verify=False)
    except Exception:
        LOGGER.exception("could not adopt the dashboard into a persistent runtime manager")

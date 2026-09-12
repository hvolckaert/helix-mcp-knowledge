"""MCP v2 server composition and stdio entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server import MCPServer

from . import __version__
from .application import KnowledgeApplication
from .dashboard_runtime import dashboard_supervises_workspace
from .errors import KnowledgeError
from .logging import configure_logging
from .managed_installation import load_managed_installation
from .storage.vector_cleanup import drain_vector_cleanup
from .storage_retention import maintain_managed_storage
from .tools import register_tools
from .workspace import discover_config_path

COORDINATOR_STOP_TIMEOUT_SECONDS = 0.25
LOGGER = logging.getLogger(__name__)


def _dashboard_owns_background_services(app: KnowledgeApplication) -> bool:
    try:
        installation = load_managed_installation(app.config.base_dir)
    except KnowledgeError as exc:
        LOGGER.warning("managed dashboard metadata could not be read: %s", exc)
        return False
    return bool(
        installation
        and installation.config_path == app.config.config_path
        and dashboard_supervises_workspace(
            app.config.base_dir,
            installation.dashboard_port,
        )
    )


def create_server(
    config_path: str | Path | None = None,
    *,
    application: KnowledgeApplication | None = None,
) -> MCPServer:
    app = application or KnowledgeApplication.from_config(config_path)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[dict[str, object]]:
        project_coordinator = None
        official_worker = None
        release_update_checker = None
        catalog_update_checker = None
        storage_retention = maintain_managed_storage(app.config)
        if storage_retention.status == "error":
            LOGGER.error("automatic storage retention failed: %s", storage_retention.error)
        drain_vector_cleanup(app.database, app.vector_index)
        dashboard_owner = _dashboard_owns_background_services(app)
        if app.config.ingestion.watch.enabled and not dashboard_owner:
            project_coordinator = app.create_sync_coordinator()
            project_coordinator.start()
        if (
            not dashboard_owner
            and app.config.official_docs.automatic_sync
            and (
                app.config.official_docs.products
                or not app.config.official_docs.retain_unselected_versions
            )
        ):
            try:
                official_worker = app.create_official_sync_worker_launcher().start()
            except Exception:
                LOGGER.exception("failed to launch detached official synchronization worker")
        if app.config.updates.enabled and not dashboard_owner:
            release_update_checker = app.release_update_checker
            release_update_checker.start()
        if app.config.catalog_updates.enabled and not dashboard_owner:
            catalog_update_checker = app.catalog_update_checker
            catalog_update_checker.start()
        try:
            yield {
                "project_sync_coordinator": project_coordinator,
                "official_sync_worker": official_worker,
                "release_update_checker": release_update_checker,
                "catalog_update_checker": catalog_update_checker,
                "storage_retention": storage_retention,
                "background_owner": "dashboard" if dashboard_owner else "mcp",
            }
        finally:
            if project_coordinator is not None:
                project_coordinator.stop(timeout=COORDINATOR_STOP_TIMEOUT_SECONDS)
            if release_update_checker is not None:
                release_update_checker.stop(timeout=COORDINATOR_STOP_TIMEOUT_SECONDS)
            if catalog_update_checker is not None:
                catalog_update_checker.stop(timeout=COORDINATOR_STOP_TIMEOUT_SECONDS)

    server = MCPServer(
        name=app.config.server.name,
        title="BMC Helix Knowledge",
        description="Evidence retrieval over official BMC and isolated project documentation.",
        instructions=(
            "Use search_docs to retrieve evidence. Preserve source provenance. "
            "Never treat all_relevant as permission to search every project."
        ),
        version=__version__,
        log_level=app.config.logging.level,
        lifespan=lifespan,
    )
    register_tools(server, app)
    return server


def main() -> None:
    config_path = discover_config_path()
    application = KnowledgeApplication.from_config(config_path)
    configure_logging(application.config.logging.level)
    create_server(application=application).run(transport="stdio")


if __name__ == "__main__":
    main()

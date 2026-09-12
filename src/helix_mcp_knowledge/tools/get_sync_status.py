"""MCP tool for read-only index and synchronization readiness."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from ..models.sync_status import SyncStatusResponse
from .annotations import LOCAL_READ_ONLY


def register_get_sync_status(server: MCPServer, application: KnowledgeApplication) -> None:
    @server.tool(
        name="get_sync_status",
        description=(
            "Report official index readiness and synchronization state; project details are "
            "limited to the explicitly requested or active project."
        ),
        annotations=LOCAL_READ_ONLY,
        structured_output=True,
    )
    def get_sync_status(project_id: str | None = None) -> SyncStatusResponse:
        return application.get_sync_status(project_id)

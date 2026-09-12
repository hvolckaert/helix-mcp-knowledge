"""MCP get_active_project tool."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from ..models.project import ActiveProjectResponse
from .annotations import LOCAL_READ_ONLY


def register_get_active_project(server: MCPServer, application: KnowledgeApplication) -> None:
    @server.tool(
        name="get_active_project",
        description="Return the project selected in this stdio server instance.",
        annotations=LOCAL_READ_ONLY,
        structured_output=True,
    )
    def get_active_project() -> ActiveProjectResponse:
        return application.get_active_project()

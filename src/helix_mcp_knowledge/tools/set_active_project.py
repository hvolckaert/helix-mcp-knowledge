"""MCP set_active_project tool."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from ..models.project import ActiveProjectResponse
from .annotations import SESSION_MUTATION


def register_set_active_project(server: MCPServer, application: KnowledgeApplication) -> None:
    @server.tool(
        name="set_active_project",
        description=(
            "Select a project in this stdio server instance. Pass null to return to official-only."
        ),
        annotations=SESSION_MUTATION,
        structured_output=True,
    )
    def set_active_project(project_id: str | None = None) -> ActiveProjectResponse:
        return application.set_active_project(project_id)

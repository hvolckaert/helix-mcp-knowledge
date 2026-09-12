"""MCP list_projects tool."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from ..models.project import ProjectListResponse
from .annotations import LOCAL_READ_ONLY


def register_list_projects(server: MCPServer, application: KnowledgeApplication) -> None:
    @server.tool(
        name="list_projects",
        description=(
            "List selectable projects and their BMC product versions without document content."
        ),
        annotations=LOCAL_READ_ONLY,
        structured_output=True,
    )
    def list_projects(include_archived: bool = False) -> ProjectListResponse:
        return application.list_projects(include_archived)

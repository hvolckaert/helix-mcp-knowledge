"""MCP list_versions tool."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from ..models.product import VersionListResponse
from .annotations import LOCAL_READ_ONLY


def register_list_versions(server: MCPServer, application: KnowledgeApplication) -> None:
    @server.tool(
        name="list_versions",
        description=(
            "List configured or indexed versions for one canonical or aliased BMC product; "
            "indexed is false until searchable evidence exists."
        ),
        annotations=LOCAL_READ_ONLY,
        structured_output=True,
    )
    def list_versions(product: str) -> VersionListResponse:
        return application.search_engine.list_versions(product)

"""MCP list_products tool."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from ..models.product import ProductListResponse
from .annotations import LOCAL_READ_ONLY


def register_list_products(server: MCPServer, application: KnowledgeApplication) -> None:
    @server.tool(
        name="list_products",
        description=(
            "List configured or indexed BMC products; indexed is false until searchable "
            "evidence exists."
        ),
        annotations=LOCAL_READ_ONLY,
        structured_output=True,
    )
    def list_products() -> ProductListResponse:
        return application.search_engine.list_products()

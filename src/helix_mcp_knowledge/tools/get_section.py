"""MCP get_section tool."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from ..models.search import SectionResponse
from .annotations import LOCAL_READ_ONLY


def register_get_section(server: MCPServer, application: KnowledgeApplication) -> None:
    @server.tool(
        name="get_section",
        description=(
            "Return a selected chunk and adjacent active chunks from the same document. "
            "Project sections require that project to be active or passed explicitly."
        ),
        annotations=LOCAL_READ_ONLY,
        structured_output=True,
    )
    def get_section(
        chunk_id: str,
        context_before: int = 1,
        context_after: int = 1,
        project_id: str | None = None,
    ) -> SectionResponse:
        return application.search_engine.get_section(
            chunk_id,
            context_before,
            context_after,
            project_id=project_id,
        )

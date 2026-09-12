"""MCP search_docs tool."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from ..models.document import DocumentType
from ..models.search import SearchRequest, SearchResponse
from ..models.source import SourceScope
from .annotations import LOCAL_READ_ONLY


def register_search_docs(server: MCPServer, application: KnowledgeApplication) -> None:
    @server.tool(
        name="search_docs",
        description=(
            "Retrieve documentary evidence from official BMC knowledge and the effective "
            "project. all_relevant never searches unrelated projects."
        ),
        annotations=LOCAL_READ_ONLY,
        structured_output=True,
    )
    def search_docs(
        query: str,
        project_id: str | None = None,
        source_scope: SourceScope = SourceScope.ALL_RELEVANT,
        product: str | None = None,
        version: str | None = None,
        document_types: list[DocumentType] | None = None,
        top_k: int | None = None,
    ) -> SearchResponse:
        request = SearchRequest(
            query=query,
            project_id=project_id,
            source_scope=source_scope,
            product=product,
            version=version,
            document_types=document_types,
            top_k=(top_k if top_k is not None else application.config.retrieval.default_top_k),
        )
        return application.search_engine.search(request)

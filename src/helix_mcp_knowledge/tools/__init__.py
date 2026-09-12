"""Registration of the public MCP tools."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from .get_active_project import register_get_active_project
from .get_section import register_get_section
from .get_sync_status import register_get_sync_status
from .get_update_status import register_get_update_status
from .list_products import register_list_products
from .list_projects import register_list_projects
from .list_versions import register_list_versions
from .search_docs import register_search_docs
from .set_active_project import register_set_active_project


def register_tools(server: MCPServer, application: KnowledgeApplication) -> None:
    register_search_docs(server, application)
    register_get_section(server, application)
    register_list_products(server, application)
    register_list_versions(server, application)
    register_list_projects(server, application)
    register_get_active_project(server, application)
    register_set_active_project(server, application)
    register_get_update_status(server, application)
    register_get_sync_status(server, application)


__all__ = ["register_tools"]

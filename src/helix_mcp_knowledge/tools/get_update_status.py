"""MCP get_update_status tool."""

from mcp.server import MCPServer

from ..application import KnowledgeApplication
from ..models.update import UpdateStatus
from .annotations import REMOTE_READ_ONLY


def register_get_update_status(server: MCPServer, application: KnowledgeApplication) -> None:
    @server.tool(
        name="get_update_status",
        description=(
            "Return the cached release status. Pass refresh=true to perform a "
            "read-only GitHub check; this tool never installs an update."
        ),
        annotations=REMOTE_READ_ONLY,
        structured_output=True,
    )
    def get_update_status(refresh: bool = False) -> UpdateStatus:
        return (
            application.release_update_checker.check(force=True)
            if refresh
            else application.release_update_checker.status()
        )

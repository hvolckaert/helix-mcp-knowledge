"""Hermetic MCP stdio acceptance test suitable for every CI run."""

import sys
from pathlib import Path

import pytest
from anyio import fail_after
from mcp import Client, StdioServerParameters, stdio_client

from helix_mcp_knowledge.application import KnowledgeApplication

EXPECTED_TOOLS = {
    "search_docs",
    "get_section",
    "list_products",
    "list_versions",
    "list_projects",
    "get_active_project",
    "set_active_project",
    "get_update_status",
    "get_sync_status",
}


def _structured(result) -> dict:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.anyio
async def test_synthetic_index_over_real_mcp_stdio(config_path: Path, insert_document) -> None:
    application = KnowledgeApplication.from_config(config_path)
    insert_document(
        application,
        document_id="doc_stdio_official",
        chunk_id="chk_stdio_official",
        title="Official reconciliation",
        text="STDIO_OFFICIAL_SENTINEL documents normalization and reconciliation.",
        source_scope="bmc_official",
        project_id=None,
        version="26.1",
    )
    insert_document(
        application,
        document_id="doc_stdio_project",
        chunk_id="chk_stdio_project",
        title="Private project design",
        text="STDIO_PROJECT_SENTINEL belongs only to the example_project project.",
        source_scope="project",
        project_id="example_project",
        version="26.1",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "helix_mcp_knowledge.server"],
        env={"HELIX_KNOWLEDGE_CONFIG": str(config_path)},
        cwd=Path(__file__).resolve().parents[2],
    )

    with fail_after(30):
        async with Client(stdio_client(parameters), read_timeout_seconds=20) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS

            official = _structured(
                await client.call_tool(
                    "search_docs",
                    {
                        "query": "STDIO_OFFICIAL_SENTINEL",
                        "source_scope": "bmc_official",
                        "product": "cmdb",
                        "version": "26.1",
                    },
                )
            )
            assert [item["document_id"] for item in official["results"]] == ["doc_stdio_official"]

            selected = _structured(
                await client.call_tool("set_active_project", {"project_id": "example_project"})
            )
            assert selected == {"project_id": "example_project", "source": "session"}
            project = _structured(
                await client.call_tool(
                    "search_docs",
                    {
                        "query": "STDIO_PROJECT_SENTINEL",
                        "source_scope": "project",
                        "product": "cmdb",
                    },
                )
            )
            assert [item["document_id"] for item in project["results"]] == ["doc_stdio_project"]

            await client.call_tool("set_active_project", {"project_id": None})
            isolated = _structured(
                await client.call_tool(
                    "search_docs",
                    {
                        "query": "STDIO_PROJECT_SENTINEL",
                        "source_scope": "all_relevant",
                        "product": "cmdb",
                    },
                )
            )
            assert isolated["results"] == []

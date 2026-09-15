"""Client-neutral MCP first-query check against the fictional Aster fixture."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.metadata import version
from pathlib import Path

from mcp import Client, StdioServerParameters, stdio_client

from helix_mcp_knowledge.config import load_config

EXPECTED_TOOLS = {
    "get_active_project",
    "get_section",
    "get_sync_status",
    "get_update_status",
    "list_products",
    "list_projects",
    "list_versions",
    "search_docs",
    "set_active_project",
}


async def probe(config_path: Path) -> dict[str, object]:
    config = load_config(config_path)
    if config.official_docs.products:
        raise RuntimeError("official BMC products must remain unselected for this case")
    if config.retrieval.semantic.enabled or config.retrieval.reranker.enabled:
        raise RuntimeError("semantic search and reranking must remain disabled")

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "helix_mcp_knowledge.server"],
        env={"HELIX_KNOWLEDGE_CONFIG": str(config_path)},
        cwd=config_path.parent,
    )
    async with Client(stdio_client(parameters), read_timeout_seconds=20) as client:
        tool_names = {tool.name for tool in (await client.list_tools()).tools}
        if tool_names != EXPECTED_TOOLS:
            raise RuntimeError("unexpected MCP tool inventory")

        search = await client.call_tool(
            "search_docs",
            {
                "query": "quartz-lock recovery window",
                "project_id": "aster-tester",
                "source_scope": "project",
                "product": "cmdb",
                "version": "26.1",
                "top_k": 3,
            },
        )
        if search.is_error or search.structured_content is None:
            raise RuntimeError("search_docs failed")
        matches = search.structured_content["results"]
        if not matches:
            raise RuntimeError("fictional project document was not found")
        first = matches[0]
        if (
            first["project_id"] != "aster-tester"
            or first["source_scope"] != "project"
            or "26.1" not in first["versions"]
            or Path(first["source_path"]).name != "aster-operations.md"
            or not first["match"]["lexical"]
            or first["match"]["semantic"]
            or first["match"]["reranked"]
        ):
            raise RuntimeError("scope, version, source or lexical-only check failed")

        section = await client.call_tool(
            "get_section",
            {
                "chunk_id": first["chunk_id"],
                "project_id": "aster-tester",
                "context_before": 1,
                "context_after": 1,
            },
        )
        if section.is_error or section.structured_content is None:
            raise RuntimeError("get_section failed")
        if section.structured_content["title"] != first["title"]:
            raise RuntimeError("section provenance did not match the search result")

        unsupported = await client.call_tool(
            "search_docs",
            {
                "query": "zircon override",
                "project_id": "aster-tester",
                "source_scope": "project",
                "product": "cmdb",
                "version": "26.1",
                "top_k": 3,
            },
        )
        if unsupported.is_error or unsupported.structured_content is None:
            raise RuntimeError("insufficient-evidence search failed")
        unsupported_count = len(unsupported.structured_content["results"])
        if unsupported_count:
            raise RuntimeError("unsupported query unexpectedly returned evidence")

        return {
            "release": version("helix-mcp-knowledge"),
            "tool_count": len(tool_names),
            "query": "quartz-lock recovery window",
            "result_count": len(matches),
            "title": first["title"],
            "heading": " > ".join(first["heading_path"]),
            "source_file": Path(first["source_path"]).name,
            "scope": first["source_scope"],
            "project": first["project_id"],
            "version": first["versions"][0],
            "lexical_only": True,
            "section_retrieved": True,
            "unsupported_query_result_count": unsupported_count,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve(strict=True)
    print(json.dumps(asyncio.run(asyncio.wait_for(probe(config_path), timeout=30)), indent=2))


if __name__ == "__main__":
    main()

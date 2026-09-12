"""Real stdio acceptance test over an isolated snapshot of the production index."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml
from anyio import fail_after
from mcp import Client, StdioServerParameters, stdio_client

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.models.document import DocumentType
from helix_mcp_knowledge.models.ingestion import IngestRequest
from helix_mcp_knowledge.models.source import SourceScope

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_CONFIG = REPOSITORY_ROOT / "config/config.yaml"
PRODUCTION_DATABASE = REPOSITORY_ROOT / "data/sqlite/helix_mcp_knowledge.db"
PROJECT_SENTINEL = "EXAMPLE_PROJECT_ACCEPTANCE_SENTINEL_261"
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

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("HELIX_MCP_E2E") != "1",
        reason="set HELIX_MCP_E2E=1 to run the real stdio acceptance test",
    ),
]


def _snapshot_database(destination: Path) -> None:
    if not PRODUCTION_DATABASE.is_file():
        pytest.skip(f"official index not found: {PRODUCTION_DATABASE}")
    destination.parent.mkdir(parents=True)
    with (
        sqlite3.connect(f"file:{PRODUCTION_DATABASE}?mode=ro", uri=True) as source,
        sqlite3.connect(destination) as target,
    ):
        source.backup(target)


def _prepare_acceptance_workspace(tmp_path: Path) -> Path:
    sources = tmp_path / "data/sources"
    project_documents = sources / "projects/example_project/docs"
    project_documents.mkdir(parents=True)
    errors = tmp_path / "data/errors"
    cache = tmp_path / "data/cache"
    projects = tmp_path / "config/projects"
    projects.mkdir(parents=True)

    database = tmp_path / "data/sqlite/e2e.db"
    _snapshot_database(database)

    config_payload = yaml.safe_load(PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    config_payload["paths"].update(
        {
            "sources": str(sources),
            "projects_config": str(projects),
            "cache": str(cache),
            "errors": str(errors),
            "official_manifest": str(REPOSITORY_ROOT / "config/sources/bmc-official-26.1.yaml"),
        }
    )
    config_payload["storage"]["sqlite"]["path"] = str(database)
    config_payload["ingestion"]["watch"]["enabled"] = False
    config_payload["official_docs"]["automatic_sync"] = False
    config_payload["updates"]["enabled"] = False
    config_path = tmp_path / "config/config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(config_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    project_payload = {
        "schema_version": 1,
        "id": "example_project",
        "name": "Example Project",
        "description": "Synthetic project used only by the MCP acceptance test.",
        "status": "active",
        "classification": "confidential",
        "documents": {"path": str(project_documents)},
        "languages": ["en"],
        "bmc": {
            "products": {
                "arsystem": {"version": "26.1"},
                "cmdb": {"version": "26.1"},
                "itsm": {"version": "26.1"},
            }
        },
        "tags": ["arsystem", "cmdb", "itsm"],
    }
    project_payload["documents"]["path"] = str(project_documents)
    (projects / "example_project.yaml").write_text(
        yaml.safe_dump(project_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    fixture = project_documents / "acceptance.md"
    fixture.write_text(
        "# Example Project MCP acceptance\n\n"
        f"{PROJECT_SENTINEL} identifies evidence that belongs only to Example Project.\n\n"
        "## CMDB reconciliation rule\n\n"
        "The project rule takes precedence only inside the selected example_project context.\n",
        encoding="utf-8",
    )
    app = KnowledgeApplication.from_config(config_path)
    ingested = app.ingestion_manager.ingest(
        IngestRequest(
            source_path=fixture,
            source_scope=SourceScope.PROJECT,
            project_id="example_project",
            document_type=DocumentType.PROCEDURE,
            language="en",
            product_versions={"cmdb": None},
        )
    )
    assert ingested.status == "indexed"
    return config_path


def _structured(result) -> dict:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.anyio
async def test_all_tools_over_real_stdio_and_example_project_context(tmp_path: Path) -> None:
    config_path = _prepare_acceptance_workspace(tmp_path)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "helix_mcp_knowledge.server"],
        env={"HELIX_KNOWLEDGE_CONFIG": str(config_path)},
        cwd=REPOSITORY_ROOT,
    )

    with fail_after(45):
        async with Client(stdio_client(parameters), read_timeout_seconds=30) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS

            products = _structured(await client.call_tool("list_products", {}))
            assert {item["product_id"] for item in products["products"]} >= {
                "cmdb",
                "itsm",
                "arsystem",
            }

            versions = _structured(await client.call_tool("list_versions", {"product": "cmdb"}))
            assert versions["product"] == "cmdb"
            assert "26.1" in {item["version"] for item in versions["versions"]}

            projects = _structured(await client.call_tool("list_projects", {}))
            example_project = next(
                item for item in projects["projects"] if item["id"] == "example_project"
            )
            assert example_project["products"] == {
                "arsystem": "26.1",
                "cmdb": "26.1",
                "itsm": "26.1",
            }

            active = _structured(await client.call_tool("get_active_project", {}))
            assert active == {"project_id": None, "source": "none"}

            update_status = _structured(await client.call_tool("get_update_status", {}))
            assert update_status["status"] == "disabled"
            assert update_status["update_available"] is None

            sync_status = _structured(await client.call_tool("get_sync_status", {}))
            assert sync_status["official"]["ready"] is True
            assert sync_status["project"] is None

            official = _structured(
                await client.call_tool(
                    "search_docs",
                    {
                        "query": "reconciliation normalization",
                        "source_scope": "bmc_official",
                        "product": "cmdb",
                        "version": "26.1",
                        "top_k": 3,
                    },
                )
            )
            assert official["results"]
            assert all(item["source_scope"] == "bmc_official" for item in official["results"])
            assert all("26.1" in item["versions"] for item in official["results"])
            assert all(
                item["source_url"].startswith("https://docs.helixops.ai/")
                for item in official["results"]
            )

            selected = _structured(
                await client.call_tool("set_active_project", {"project_id": "example_project"})
            )
            assert selected == {"project_id": "example_project", "source": "session"}
            assert _structured(await client.call_tool("get_active_project", {})) == selected
            active_sync = _structured(await client.call_tool("get_sync_status", {}))
            assert active_sync["project"]["project_id"] == "example_project"
            assert active_sync["project"]["context_source"] == "session"

            project_search = _structured(
                await client.call_tool(
                    "search_docs",
                    {
                        "query": PROJECT_SENTINEL,
                        "source_scope": "project",
                        "product": "cmdb",
                        "top_k": 3,
                    },
                )
            )
            assert project_search["context"]["project_id"] == "example_project"
            assert project_search["context"]["version"] == "26.1"
            assert project_search["results"]
            assert all(item["source_scope"] == "project" for item in project_search["results"])
            assert all(
                item["project_id"] == "example_project" for item in project_search["results"]
            )

            official_only = _structured(
                await client.call_tool(
                    "search_docs",
                    {
                        "query": PROJECT_SENTINEL,
                        "source_scope": "bmc_official",
                        "product": "cmdb",
                        "version": "26.1",
                    },
                )
            )
            assert official_only["results"] == []

            selected_chunk = project_search["results"][0]["chunk_id"]
            section = _structured(
                await client.call_tool(
                    "get_section",
                    {"chunk_id": selected_chunk, "context_before": 1, "context_after": 1},
                )
            )
            assert section["selected_chunk_id"] == selected_chunk
            assert any(PROJECT_SENTINEL in chunk["text"] for chunk in section["chunks"])

            cleared = _structured(
                await client.call_tool("set_active_project", {"project_id": None})
            )
            assert cleared == {"project_id": None, "source": "none"}

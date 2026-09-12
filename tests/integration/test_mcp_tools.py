import time

import pytest
from anyio import fail_after
from mcp import Client

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.server import create_server


class RecordingCoordinator:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self, *, timeout: float) -> None:
        self.stopped = True
        self.stop_timeout = timeout


class RecordingWorkerLauncher:
    def __init__(self) -> None:
        self.started = False

    def start(self):
        self.started = True
        return {"pid": 1234}


class FailingWorkerLauncher:
    def start(self):
        raise OSError("cannot launch worker")


@pytest.mark.anyio
async def test_server_exposes_exact_public_tools(app) -> None:
    server = create_server(application=app)
    async with Client(server) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {
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


@pytest.mark.anyio
async def test_public_tools_expose_side_effect_annotations(app) -> None:
    server = create_server(application=app)
    async with Client(server) as client:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name, tool in listed.items():
        assert tool.annotations is not None
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        if name == "set_active_project":
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.open_world_hint is False
        elif name == "get_update_status":
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.open_world_hint is True
        else:
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.open_world_hint is False


@pytest.mark.anyio
async def test_update_status_tool_is_read_only_when_checker_is_disabled(app) -> None:
    server = create_server(application=app)
    async with Client(server) as client:
        result = await client.call_tool("get_update_status", {})

    assert result.structured_content["status"] == "disabled"
    assert result.structured_content["repository"] == "hvolckaert/helix-mcp-knowledge"
    assert result.structured_content["latest_version"] is None
    assert result.structured_content["update_available"] is None


@pytest.mark.anyio
async def test_sync_status_tool_reports_only_active_project(app) -> None:
    server = create_server(application=app)
    async with Client(server) as client:
        without_project = await client.call_tool("get_sync_status", {})
        await client.call_tool("set_active_project", {"project_id": "example_project"})
        with_project = await client.call_tool("get_sync_status", {})

    assert without_project.structured_content["project"] is None
    project = with_project.structured_content["project"]
    assert project["project_id"] == "example_project"
    assert project["context_source"] == "session"
    assert project["indexed_documents"] == 1


@pytest.mark.anyio
async def test_list_versions_exposes_configured_version_before_sync(config_path) -> None:
    application = KnowledgeApplication.from_config(config_path)
    server = create_server(application=application)

    async with Client(server) as client:
        result = await client.call_tool("list_versions", {"product": "CMDB"})

    assert result.structured_content == {
        "product": "cmdb",
        "versions": [{"version": "26.1", "indexed": False}],
    }


@pytest.mark.anyio
async def test_list_versions_supports_rolling_discovery_corpus(config_path) -> None:
    content = config_path.read_text(encoding="utf-8").replace(
        'cmdb: {versions: ["26.1"]}', 'discovery: {versions: ["current"]}'
    )
    config_path.write_text(content, encoding="utf-8")
    application = KnowledgeApplication.from_config(config_path)
    server = create_server(application=application)

    async with Client(server) as client:
        result = await client.call_tool("list_versions", {"product": "Discovery"})

    assert result.structured_content == {
        "product": "discovery",
        "versions": [{"version": "current", "indexed": False}],
    }


@pytest.mark.anyio
async def test_list_products_exposes_configured_product_before_sync(config_path) -> None:
    application = KnowledgeApplication.from_config(config_path)
    server = create_server(application=application)

    async with Client(server) as client:
        result = await client.call_tool("list_products", {})

    assert result.structured_content == {
        "products": [
            {
                "product_id": "cmdb",
                "name": "BMC Helix CMDB",
                "aliases": ["CMDB", "BMC CMDB", "Helix CMDB"],
                "configured": True,
                "indexed": False,
            }
        ]
    }


@pytest.mark.anyio
async def test_server_lifespan_runs_release_checker_when_enabled(app, monkeypatch) -> None:
    checker = RecordingCoordinator()
    app.config.updates.enabled = True
    monkeypatch.setattr(app, "release_update_checker", checker)
    server = create_server(application=app)

    async with Client(server) as client:
        await client.list_tools()
        assert checker.started is True

    assert checker.stopped is True
    assert checker.stop_timeout == 0.25


@pytest.mark.anyio
async def test_server_lifespan_runs_embedded_sync_when_enabled(app, monkeypatch) -> None:
    coordinator = RecordingCoordinator()
    app.config.ingestion.watch.enabled = True
    monkeypatch.setattr(app, "create_sync_coordinator", lambda: coordinator)
    server = create_server(application=app)

    async with Client(server) as client:
        await client.list_tools()
        assert coordinator.started is True

    assert coordinator.stopped is True
    assert coordinator.stop_timeout == 0.25


@pytest.mark.anyio
async def test_dashboard_owns_background_services_for_managed_workspace(app, monkeypatch) -> None:
    project = RecordingCoordinator()
    release = RecordingCoordinator()
    catalog = RecordingCoordinator()
    official = RecordingWorkerLauncher()
    app.config.ingestion.watch.enabled = True
    app.config.official_docs.automatic_sync = True
    app.config.official_docs.products = {"cmdb": {"versions": ["26.1"]}}
    app.config.updates.enabled = True
    app.config.catalog_updates.enabled = True
    monkeypatch.setattr(app, "create_sync_coordinator", lambda: project)
    monkeypatch.setattr(app, "create_official_sync_worker_launcher", lambda: official)
    monkeypatch.setattr(app, "release_update_checker", release)
    monkeypatch.setattr(app, "catalog_update_checker", catalog)
    monkeypatch.setattr(
        "helix_mcp_knowledge.server._dashboard_owns_background_services",
        lambda _app: True,
    )
    server = create_server(application=app)

    async with Client(server) as client:
        await client.list_tools()

    assert project.started is False
    assert official.started is False
    assert release.started is False
    assert catalog.started is False


@pytest.mark.anyio
async def test_server_launches_official_sync_outside_stdio_lifespan(app, monkeypatch) -> None:
    launcher = RecordingWorkerLauncher()
    app.config.official_docs.automatic_sync = True
    app.config.official_docs.products = {"cmdb": {"versions": ["26.1"]}}
    monkeypatch.setattr(app, "create_official_sync_worker_launcher", lambda: launcher)
    server = create_server(application=app)

    started = time.monotonic()
    async with Client(server) as client:
        await client.list_tools()
    elapsed = time.monotonic() - started

    assert launcher.started is True
    assert elapsed < 1


@pytest.mark.anyio
async def test_worker_launch_failure_does_not_block_mcp_tools(app, monkeypatch) -> None:
    app.config.official_docs.automatic_sync = True
    app.config.official_docs.products = {"cmdb": {"versions": ["26.1"]}}
    monkeypatch.setattr(
        app,
        "create_official_sync_worker_launcher",
        lambda: FailingWorkerLauncher(),
    )
    server = create_server(application=app)

    async with Client(server) as client:
        tools = await client.list_tools()

    assert len(tools.tools) == 9


@pytest.mark.anyio
async def test_active_project_affects_search_through_mcp(app) -> None:
    server = create_server(application=app)
    async with Client(server) as client:
        with fail_after(5):
            selected = await client.call_tool(
                "set_active_project", {"project_id": "example_project"}
            )
        assert selected.structured_content["project_id"] == "example_project"
        with fail_after(5):
            result = await client.call_tool("search_docs", {"query": "reconciliation"})
        chunk_ids = {item["chunk_id"] for item in result.structured_content["results"]}
        assert chunk_ids == {"chk_official", "chk_example_project"}

from pathlib import Path

import httpx
import pytest

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.errors import (
    ConfigurationError,
    OfficialSyncCancelled,
    SourceNotFoundError,
    SourceSyncError,
)
from helix_mcp_knowledge.models.document import DocumentType
from helix_mcp_knowledge.models.ingestion import IngestRequest
from helix_mcp_knowledge.models.source import SourceScope
from helix_mcp_knowledge.official_cleanup import CLEANUP_JOB_ID, OfficialCorpusCleaner
from helix_mcp_knowledge.storage.automation import AutomationStore
from helix_mcp_knowledge.storage.sources import SourceStore
from helix_mcp_knowledge.sync.browser import OfficialBrowserDownloader
from helix_mcp_knowledge.sync.crawler import RobotsPolicy
from helix_mcp_knowledge.sync.http import OfficialHttpDownloader
from helix_mcp_knowledge.sync.manifest import OfficialSourceManifest
from helix_mcp_knowledge.sync.service import OfficialSourceSynchronizer

MANIFEST = """
schema_version: 1
publisher: BMC Software
sources:
  - source_id: bmc-cmdb-26-1-test
    title: BMC Helix CMDB 26.1 Test
    url: https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/Test/
    local_path: cmdb/26.1/test.html
    product: cmdb
    version: "26.1"
    document_type: reference
    language: en
"""

COLLECTION_MANIFEST = """
schema_version: 1
publisher: BMC Software
collections:
  - collection_id: bmc-cmdb-26-1-test
    root_url: https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/
    local_path_prefix: cmdb/26.1/discovered
    product: cmdb
    version: "26.1"
    max_pages: 2
    request_delay_seconds: 0
sources:
  - source_id: bmc-cmdb-26-1-root
    title: CMDB root
    url: https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/
    local_path: cmdb/26.1/home.html
    product: cmdb
    version: "26.1"
    document_type: concept
"""

MULTI_PRODUCT_MANIFEST = """
schema_version: 1
publisher: BMC Software
sources:
  - source_id: bmc-cmdb-26-1-test
    url: https://docs.bmc.com/cmdb/
    local_path: cmdb/26.1/test.html
    product: cmdb
    version: "26.1"
  - source_id: bmc-itsm-26-1-test
    url: https://docs.bmc.com/itsm/
    local_path: itsm/26.1/test.html
    product: itsm
    version: "26.1"
"""

TRANSITION_COLLECTION_MANIFEST = """
schema_version: 1
publisher: BMC Software
collections:
  - collection_id: bmc-itsm-26-1-transition
    root_url: https://docs.bmc.com/itsm/
    local_path_prefix: itsm/26.1/discovered
    product: itsm
    version: "26.1"
    max_pages: 1
    request_delay_seconds: 0
sources:
  - source_id: bmc-cmdb-26-1-transition
    url: https://docs.bmc.com/cmdb/
    local_path: cmdb/26.1/test.html
    product: cmdb
    version: "26.1"
"""


def _write_manifest(config_path: Path, content: str = MANIFEST) -> Path:
    path = config_path.parent / "sources/bmc-official-26.1.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_manifest_rejects_parent_path_traversal(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(MANIFEST.replace("cmdb/26.1/test.html", "../escaped.html"), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="relative HTML path"):
        OfficialSourceManifest.load(path)


def test_manifest_rejects_nonstandard_https_ports(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(
        MANIFEST.replace("https://docs.bmc.com/", "https://docs.bmc.com:8443/"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="credential-free HTTPS URL"):
        OfficialSourceManifest.load(path)


def test_downloader_rejects_redirect_to_non_allowlisted_domain(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://example.com/content"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        downloader = OfficialHttpDownloader(
            client=client, allowed_domains=["docs.bmc.com"], max_bytes=1024
        )
        with pytest.raises(SourceSyncError, match="not allowlisted"):
            downloader.fetch("https://docs.bmc.com/source", tmp_path / "official/source.html")


@pytest.mark.parametrize(
    "url",
    [
        "https://" + "example-user" + ":" + "example-password" + "@docs.bmc.com/source",
        "https://docs.bmc.com:8443/source",
        "https://example.com/source",
        "http://docs.bmc.com/source",
    ],
)
def test_browser_downloader_enforces_the_same_strict_allowlist(config_path: Path, url: str) -> None:
    downloader = OfficialBrowserDownloader(KnowledgeApplication.from_config(config_path).config)

    with pytest.raises(SourceSyncError, match="browser left"):
        downloader._validate_source_url(url)

    downloader._validate_source_url("https://docs.bmc.com./source")


def test_downloader_reports_interactive_authentication_redirect_as_unavailable(
    tmp_path: Path,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={
                "Location": (
                    "https://helixops.okta.com/oauth2/default/v1/authorize"
                    "?response_type=code&client_id=public-client"
                )
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        downloader = OfficialHttpDownloader(
            client=client, allowed_domains=["docs.helixops.ai"], max_bytes=1024
        )
        with pytest.raises(
            SourceNotFoundError,
            match=r"requires interactive authentication at helixops\.okta\.com",
        ):
            downloader.fetch(
                "https://docs.helixops.ai/bin/example/",
                tmp_path / "official/source.html",
            )

    assert requests == ["https://docs.helixops.ai/bin/example/"]


def test_downloader_reports_cloudflare_challenge(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"cf-mitigated": "challenge"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        downloader = OfficialHttpDownloader(
            client=client, allowed_domains=["docs.bmc.com"], max_bytes=1024
        )
        with pytest.raises(SourceSyncError, match="Cloudflare"):
            downloader.fetch("https://docs.bmc.com/source", tmp_path / "official/source.html")


def test_downloader_retries_transient_network_failure(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("disconnected", request=request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><body><p>Recovered</p></body></html>",
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        downloader = OfficialHttpDownloader(
            client=client, allowed_domains=["docs.bmc.com"], max_bytes=1024
        )
        result = downloader.fetch("https://docs.bmc.com/source", tmp_path / "official/source.html")

    assert attempts == 2
    assert result.status == "downloaded"


def test_robots_policy_retries_transient_network_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("disconnected", request=request)
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        policy = RobotsPolicy.load(
            client,
            "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/",
            "helix-mcp-knowledge/0.1",
        )

    assert attempts == 2
    assert policy.is_allowed("https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/")


def test_robots_policy_follows_same_origin_redirect() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(302, headers={"Location": "/security/robots.txt"})
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        policy = RobotsPolicy.load(
            client,
            "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/",
            "helix-mcp-knowledge/0.1",
        )

    assert requested == [
        "https://docs.bmc.com/robots.txt",
        "https://docs.bmc.com/security/robots.txt",
    ]
    assert policy.is_allowed("https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/")


def test_robots_policy_rejects_cross_origin_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://attacker.example/robots.txt"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceSyncError, match="outside the official documentation host"),
    ):
        RobotsPolicy.load(
            client,
            "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/",
            "helix-mcp-knowledge/0.1",
        )


def test_robots_policy_rejects_nonstandard_port_redirect_without_following_it() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://docs.bmc.com:8443/robots.txt"},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceSyncError, match="outside the official documentation host"),
    ):
        RobotsPolicy.load(
            client,
            "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/",
            "helix-mcp-knowledge/0.1",
        )

    assert requested == ["https://docs.bmc.com/robots.txt"]


def test_robots_policy_rejects_oversized_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1025)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceSyncError, match="exceeds the size limit"),
    ):
        RobotsPolicy.load(
            client,
            "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/",
            "helix-mcp-knowledge/0.1",
            max_bytes=1024,
        )


def test_official_sync_downloads_indexes_and_revalidates(config_path: Path) -> None:
    _write_manifest(config_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match") == '"revision-1"':
            return httpx.Response(304, headers={"ETag": '"revision-1"'})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html", "ETag": '"revision-1"'},
            text=(
                "<html><head><title>BMC</title></head><body>"
                '<main id="xwikicontent"><h1>CMDB reference</h1>'
                "<p>OfficialUniqueMarker documentation.</p></main></body></html>"
            ),
        )

    app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        synchronizer = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=SourceStore(app.database),
            catalog=app.catalog,
            client=client,
        )
        first = synchronizer.sync()
        second = synchronizer.sync()

    assert first[0].status == "indexed"
    assert second[0].status == "unchanged"
    assert requests[1].headers["if-none-match"] == '"revision-1"'
    with app.database.connect() as connection:
        document = connection.execute(
            "SELECT source_type, source_url, etag FROM documents WHERE document_id = ?",
            (first[0].document_id,),
        ).fetchone()
        state = connection.execute(
            "SELECT state_json FROM sync_state WHERE source_id = 'bmc-cmdb-26-1-test'"
        ).fetchone()
    assert document["source_type"] == "bmc_public_url"
    assert document["source_url"] == "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/Test/"
    assert document["etag"] == '"revision-1"'
    assert '"last_status": "unchanged"' in state["state_json"]


def test_official_sync_downloads_only_configured_product_versions(config_path: Path) -> None:
    _write_manifest(config_path, MULTI_PRODUCT_MANIFEST)
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><main><h1>Selected documentation</h1><p>Evidence.</p></main></html>",
        )

    app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=SourceStore(app.database),
            catalog=app.catalog,
            client=client,
        ).sync(discover=False)

    assert [result.source_id for result in results] == ["bmc-cmdb-26-1-test"]
    assert requested_paths == ["/cmdb/"]


def test_official_sync_marks_interactive_authentication_redirect_as_missing(
    config_path: Path,
) -> None:
    _write_manifest(config_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={
                "Location": (
                    "https://helixops.okta.com/oauth2/default/v1/authorize"
                    "?response_type=code&client_id=public-client"
                )
            },
        )

    app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=SourceStore(app.database),
            catalog=app.catalog,
            client=client,
        ).sync(discover=False)

    assert len(results) == 1
    assert results[0].status == "missing"
    assert results[0].error == (
        "official source requires interactive authentication at helixops.okta.com"
    )
    with app.database.connect() as connection:
        state = connection.execute(
            "SELECT state_json FROM sync_state WHERE source_id = 'bmc-cmdb-26-1-test'"
        ).fetchone()
    assert '"last_status": "missing"' in state["state_json"]


def test_official_sync_rejects_configured_version_missing_from_manifest(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    content = config_path.read_text(encoding="utf-8").replace(
        'cmdb: {versions: ["26.1"]}', 'cmdb: {versions: ["25.1"]}'
    )
    config_path.write_text(content, encoding="utf-8")
    app = KnowledgeApplication.from_config(config_path)

    with pytest.raises(SourceSyncError, match=r"cmdb=25\.1"):
        app.sync_official_sources(discover=False)


def test_official_sync_requires_an_explicit_product_selection(config_path: Path) -> None:
    _write_manifest(config_path)
    content = config_path.read_text(encoding="utf-8").replace(
        '    cmdb: {versions: ["26.1"]}', "    {}"
    )
    config_path.write_text(content, encoding="utf-8")
    app = KnowledgeApplication.from_config(config_path)

    with pytest.raises(SourceSyncError, match="run helix-mcp-knowledge configure"):
        app.sync_official_sources(discover=False)


def test_official_sync_purges_unselected_versions(config_path: Path) -> None:
    _write_manifest(config_path, MULTI_PRODUCT_MANIFEST)

    def handler(request: httpx.Request) -> httpx.Response:
        product = request.url.path.strip("/")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=(
                f"<html><main><h1>{product}</h1><p>Official {product} evidence.</p></main></html>"
            ),
        )

    first_app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first_results = OfficialSourceSynchronizer(
            config=first_app.config,
            ingestion_manager=first_app.ingestion_manager,
            source_store=SourceStore(first_app.database),
            catalog=first_app.catalog,
            client=client,
        ).sync(discover=False)
    cmdb_source = Path(first_results[0].source_path)
    assert cmdb_source.is_file()

    content = config_path.read_text(encoding="utf-8")
    content = content.replace('cmdb: {versions: ["26.1"]}', 'itsm: {versions: ["26.1"]}').replace(
        "retain_unselected_versions: true", "retain_unselected_versions: false"
    )
    config_path.write_text(content, encoding="utf-8")
    second_app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = OfficialSourceSynchronizer(
            config=second_app.config,
            ingestion_manager=second_app.ingestion_manager,
            source_store=SourceStore(second_app.database),
            catalog=second_app.catalog,
            client=client,
        ).sync(discover=False)

    assert [result.status for result in results] == ["indexed", "purged"]
    assert not cmdb_source.exists()
    with second_app.database.connect() as connection:
        statuses = {
            row["product_id"]: row["status"]
            for row in connection.execute(
                """
                SELECT p.product_id, d.status
                FROM documents d
                JOIN document_products p ON p.document_id = d.document_id
                WHERE d.source_scope = 'bmc_official'
                """
            ).fetchall()
        }
        sources = {
            row["source_id"]
            for row in connection.execute("SELECT source_id FROM sources").fetchall()
        }
        fts_rows = connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    assert statuses == {"itsm": "indexed"}
    assert sources == {"bmc-itsm-26-1-test"}
    assert fts_rows == 1


def test_official_cleanup_retries_failed_external_artifacts(config_path: Path) -> None:
    _write_manifest(config_path, MULTI_PRODUCT_MANIFEST)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><main><h1>CMDB</h1><p>Old evidence.</p></main></html>",
        )

    app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        indexed = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=SourceStore(app.database),
            catalog=app.catalog,
            client=client,
        ).sync(discover=False)
    source_path = Path(indexed[0].source_path)

    class FailOnceVectorIndex:
        enabled = True

        def __init__(self) -> None:
            self.calls = 0

        def delete(self, _chunk_ids) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary vector failure")

    vectors = FailOnceVectorIndex()
    cleaner = OfficialCorpusCleaner(
        database=app.database,
        vector_index=vectors,
        official_sources_root=app.ingestion_manager.official_sources_root,
        catalog=app.catalog,
        manifest=OfficialSourceManifest.load(config_path.parent / "sources/bmc-official-26.1.yaml"),
    )

    first = cleaner.purge({("itsm", "26.1")})
    assert "temporary vector failure" in first.warnings[0]
    assert source_path.exists() is False
    assert AutomationStore(app.database).state(CLEANUP_JOB_ID)["status"] == "retry_pending"

    second = cleaner.purge({("itsm", "26.1")})
    assert second.warnings == ()
    assert vectors.calls == 2
    state = AutomationStore(app.database).state(CLEANUP_JOB_ID)
    assert state["status"] == "completed"
    assert state["chunk_ids"] == []


def test_official_sync_can_purge_all_products_after_last_selection_is_removed(
    config_path: Path,
) -> None:
    _write_manifest(config_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><main><h1>CMDB</h1><p>Official evidence.</p></main></html>",
        )

    first_app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        indexed = OfficialSourceSynchronizer(
            config=first_app.config,
            ingestion_manager=first_app.ingestion_manager,
            source_store=SourceStore(first_app.database),
            catalog=first_app.catalog,
            client=client,
        ).sync(discover=False)
    source_path = Path(indexed[0].source_path)
    project_source = first_app.registry.require("example_project").documents_path / "keep.md"
    project_source.parent.mkdir(parents=True, exist_ok=True)
    project_source.write_text(
        "# example_project\n\nPrivate project evidence remains.", encoding="utf-8"
    )
    project_result = first_app.ingestion_manager.ingest(
        IngestRequest(
            source_path=project_source,
            source_scope=SourceScope.PROJECT,
            project_id="example_project",
            document_type=DocumentType.DESIGN,
            product_versions={"cmdb": "26.1"},
        )
    )

    content = config_path.read_text(encoding="utf-8")
    content = content.replace('    cmdb: {versions: ["26.1"]}', "    {}")
    content = content.replace(
        "retain_unselected_versions: true", "retain_unselected_versions: false"
    )
    config_path.write_text(content, encoding="utf-8")
    second_app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = OfficialSourceSynchronizer(
            config=second_app.config,
            ingestion_manager=second_app.ingestion_manager,
            source_store=SourceStore(second_app.database),
            catalog=second_app.catalog,
            client=client,
        ).sync(discover=False)

    assert [result.status for result in results] == ["purged"]
    assert not source_path.exists()
    assert project_source.is_file()
    with second_app.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM documents WHERE source_scope = 'bmc_official'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 1
        project = connection.execute(
            "SELECT status FROM documents WHERE document_id = ?",
            (project_result.document_id,),
        ).fetchone()
        assert project["status"] == "indexed"


def test_official_sync_does_not_purge_old_data_until_new_selection_is_indexed(
    config_path: Path,
) -> None:
    _write_manifest(config_path, MULTI_PRODUCT_MANIFEST)

    def first_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><main><h1>CMDB</h1><p>Existing evidence.</p></main></html>",
        )

    first_app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(first_handler)) as client:
        OfficialSourceSynchronizer(
            config=first_app.config,
            ingestion_manager=first_app.ingestion_manager,
            source_store=SourceStore(first_app.database),
            catalog=first_app.catalog,
            client=client,
        ).sync(discover=False)

    content = config_path.read_text(encoding="utf-8")
    content = content.replace('cmdb: {versions: ["26.1"]}', 'itsm: {versions: ["26.1"]}')
    content = content.replace(
        "retain_unselected_versions: true", "retain_unselected_versions: false"
    )
    config_path.write_text(content, encoding="utf-8")
    second_app = KnowledgeApplication.from_config(config_path)

    def failing_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(failing_handler)) as client:
        results = OfficialSourceSynchronizer(
            config=second_app.config,
            ingestion_manager=second_app.ingestion_manager,
            source_store=SourceStore(second_app.database),
            catalog=second_app.catalog,
            client=client,
        ).sync(discover=False)

    assert [result.status for result in results] == ["missing", "error"]
    assert "cleanup was deferred" in str(results[-1].error)
    with second_app.database.connect() as connection:
        cmdb = connection.execute(
            """
            SELECT d.status
            FROM documents d
            JOIN document_product_versions dpv ON dpv.document_id = d.document_id
            WHERE dpv.product_version_id = 'cmdb:26.1'
            """
        ).fetchone()
    assert cmdb["status"] == "indexed"


def test_official_sync_does_not_purge_old_data_after_a_truncated_new_collection(
    config_path: Path,
) -> None:
    _write_manifest(config_path, TRANSITION_COLLECTION_MANIFEST)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/cmdb/":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="<html><main><h1>CMDB</h1><p>Existing evidence.</p></main></html>",
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=(
                "<html><main><h1>ITSM</h1><p>New evidence.</p>"
                '<a href="https://docs.bmc.com/itsm/second/">Second</a>'
                "</main></html>"
            ),
        )

    first_app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        OfficialSourceSynchronizer(
            config=first_app.config,
            ingestion_manager=first_app.ingestion_manager,
            source_store=SourceStore(first_app.database),
            catalog=first_app.catalog,
            client=client,
        ).sync(discover=False)

    content = config_path.read_text(encoding="utf-8")
    content = content.replace('cmdb: {versions: ["26.1"]}', 'itsm: {versions: ["26.1"]}')
    content = content.replace(
        "retain_unselected_versions: true", "retain_unselected_versions: false"
    )
    config_path.write_text(content, encoding="utf-8")
    second_app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = OfficialSourceSynchronizer(
            config=second_app.config,
            ingestion_manager=second_app.ingestion_manager,
            source_store=SourceStore(second_app.database),
            catalog=second_app.catalog,
            client=client,
        ).sync()

    assert [result.status for result in results] == ["indexed", "cleanup_deferred"]
    assert "is truncated" in str(results[-1].error)
    with second_app.database.connect() as connection:
        cmdb = connection.execute(
            """
            SELECT d.status
            FROM documents d
            JOIN document_product_versions dpv ON dpv.document_id = d.document_id
            WHERE dpv.product_version_id = 'cmdb:26.1'
            """
        ).fetchone()
    assert cmdb["status"] == "indexed"


def test_official_sync_ignores_dynamic_html_outside_document_content(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=(
                f"<html><head><title>BMC</title></head><body><nav>{request_count}</nav>"
                '<main id="xwikicontent"><h1>Stable reference</h1>'
                "<p>Stable official content.</p></main></body></html>"
            ),
        )

    app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        synchronizer = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=SourceStore(app.database),
            catalog=app.catalog,
            client=client,
        )
        first = synchronizer.sync()
        second = synchronizer.sync()

    assert first[0].status == "indexed"
    assert second[0].status == "unchanged"
    assert second[0].document_id == first[0].document_id


def test_collection_crawl_is_bounded_and_version_scoped(config_path: Path) -> None:
    _write_manifest(config_path, COLLECTION_MANIFEST)
    fetched_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: */download/\n",
            )
        fetched_paths.append(request.url.path)
        if request.url.path.endswith("/ac261/"):
            body = """
            <main id="xwikicontent"><h1>Root</h1><p>RootMarker</p>
              <a href="Child/">Child</a>
              <a href="download/file.zip">Blocked attachment</a>
              <a href="?viewer=comments">Blocked query</a>
              <a href="https://example.com/external">External</a>
              <a href="/xwiki/bin/view/cmdb/ac252/Old/">Old version</a>
            </main>
            """
        else:
            body = """
            <main id="xwikicontent"><h1>Child</h1>
              <p>ChildMarker</p><a href="Grandchild/">Grandchild</a>
            </main>
            """
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=f"<html><head><title>Test</title></head><body>{body}</body></html>",
        )

    app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=SourceStore(app.database),
            catalog=app.catalog,
            client=client,
        ).sync()

    assert [result.status for result in results] == ["indexed", "indexed", "indexed"]
    assert fetched_paths == [
        "/xwiki/bin/view/cmdb/ac261/",
        "/xwiki/bin/view/cmdb/ac261/Child/",
        "/xwiki/bin/view/cmdb/ac261/Child/Grandchild/",
    ]
    with app.database.connect() as connection:
        sources = connection.execute(
            "SELECT source_url FROM sources ORDER BY source_url"
        ).fetchall()
    assert [row["source_url"] for row in sources] == [
        "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/",
        "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/Child/",
        "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/Child/Grandchild/",
    ]


def test_complete_collection_removes_retired_page_artifacts_and_retries_vectors(
    config_path: Path,
) -> None:
    _write_manifest(config_path, COLLECTION_MANIFEST.replace("max_pages: 2", "max_pages: 10"))
    include_child = True

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path.endswith("/Child/"):
            body = "<main><h1>Retired page</h1><p>RetiredMarker</p></main>"
        else:
            link = '<a href="Child/"></a>' if include_child else ""
            body = f"<main><h1>Root</h1><p>StableMarker</p>{link}</main>"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=f"<html><body>{body}</body></html>",
        )

    app = KnowledgeApplication.from_config(config_path)
    source_store = SourceStore(app.database)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        synchronizer = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=source_store,
            catalog=app.catalog,
            client=client,
        )
        synchronizer.sync()
        child_path = Path(
            source_store.collection_sources("bmc-cmdb-26-1-test")[
                "https://docs.bmc.com/xwiki/bin/view/cmdb/ac261/Child/"
            ]
        )
        with app.database.connect() as connection:
            child = connection.execute(
                "SELECT document_id FROM documents WHERE source_url LIKE '%/Child/'"
            ).fetchone()
            child_chunk_ids = {
                row["chunk_id"]
                for row in connection.execute(
                    "SELECT chunk_id FROM chunks WHERE document_id = ?",
                    (child["document_id"],),
                ).fetchall()
            }

        class FailOnceVectorIndex:
            enabled = True

            def __init__(self) -> None:
                self.calls = 0

            def delete(self, chunk_ids) -> None:
                assert set(chunk_ids) == child_chunk_ids
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary vector cleanup failure")

        vectors = FailOnceVectorIndex()
        app.ingestion_manager.vector_index = vectors
        include_child = False
        synchronizer.sync()

        assert not child_path.exists()
        with app.database.connect() as connection:
            document = connection.execute(
                "SELECT status FROM documents WHERE document_id = ?", (child["document_id"],)
            ).fetchone()
            chunk_count = connection.execute(
                "SELECT count(*) FROM chunks WHERE document_id = ?", (child["document_id"],)
            ).fetchone()[0]
            fts_count = connection.execute(
                "SELECT count(*) FROM chunks_fts WHERE chunk_id IN ({})".format(
                    ",".join("?" for _ in child_chunk_ids)
                ),
                sorted(child_chunk_ids),
            ).fetchone()[0]
        assert document["status"] == "missing"
        assert chunk_count == 0
        assert fts_count == 0
        state = source_store.collection_state("bmc-cmdb-26-1-test")
        assert set(state["vector_cleanup_pending"]) == child_chunk_ids

        synchronizer.sync()

    assert vectors.calls == 2
    assert source_store.collection_state("bmc-cmdb-26-1-test")["vector_cleanup_pending"] == []


def test_collection_retries_cached_sources_in_error_state(config_path: Path) -> None:
    _write_manifest(config_path, COLLECTION_MANIFEST)
    child_fetches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal child_fetches
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path.endswith("/ac261/"):
            body = '<main id="xwikicontent"><h1>Root</h1><a href="Child/">Child</a></main>'
        elif request.url.path.endswith("/Child/"):
            child_fetches += 1
            body = (
                '<main id="xwikicontent"><h1>Child</h1><p>Content</p>'
                '<a href="Grandchild/">Grandchild</a></main>'
            )
        else:
            body = '<main id="xwikicontent"><h1>Grandchild</h1><p>Content</p></main>'
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=f"<html><body>{body}</body></html>",
        )

    app = KnowledgeApplication.from_config(config_path)
    source_store = SourceStore(app.database)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        synchronizer = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=source_store,
            catalog=app.catalog,
            client=client,
        )
        synchronizer.sync()
        with app.database.connect() as connection:
            child_source_id = connection.execute(
                "SELECT source_id FROM sources WHERE source_url LIKE '%/Child/'"
            ).fetchone()["source_id"]
        source_store.update_state(child_source_id, {"last_status": "error"})
        synchronizer.sync(collection_ids={"bmc-cmdb-26-1-test"})

    assert child_fetches == 2
    assert source_store.state(child_source_id)["last_status"] == "unchanged"


def test_official_sync_reports_progress_and_stops_cooperatively(config_path: Path) -> None:
    _write_manifest(config_path)
    progress: list[dict[str, object]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=(
                "<html><body><main id='xwikicontent'><h1>CMDB</h1>"
                "<p>Progress marker.</p></main></body></html>"
            ),
        )

    app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        synchronizer = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=SourceStore(app.database),
            catalog=app.catalog,
            client=client,
        )
        with pytest.raises(OfficialSyncCancelled):
            synchronizer.sync(
                discover=False,
                progress_callback=progress.append,
                cancel_check=lambda: bool(progress and progress[-1]["processed_items"] == 1),
            )

    assert progress[0]["phase"] == "preparing"
    assert progress[-1]["phase"] == "downloading"
    assert progress[-1]["processed_items"] == 1
    assert progress[-1]["percent"] == 100.0
    assert progress[-1]["result_counts"] == {"indexed": 1}
    assert progress[-1]["product_versions"] == [
        {
            "product": "cmdb",
            "version": "26.1",
            "processed_items": 1,
            "estimated_total_items": 1,
        }
    ]


def test_official_sync_reports_exact_terminal_progress(config_path: Path) -> None:
    _write_manifest(config_path)
    progress: list[dict[str, object]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=(
                "<html><body><main id='xwikicontent'><h1>CMDB</h1>"
                "<p>Terminal progress marker.</p></main></body></html>"
            ),
        )

    app = KnowledgeApplication.from_config(config_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        synchronizer = OfficialSourceSynchronizer(
            config=app.config,
            ingestion_manager=app.ingestion_manager,
            source_store=SourceStore(app.database),
            catalog=app.catalog,
            client=client,
        )
        synchronizer.sync(discover=False, progress_callback=progress.append)

    assert progress[-1]["phase"] == "finishing"
    assert progress[-1]["processed_items"] == 1
    assert progress[-1]["estimated_total_items"] == 1
    assert progress[-1]["percent"] == 100.0
    assert progress[-1]["product_versions"] == [
        {
            "product": "cmdb",
            "version": "26.1",
            "processed_items": 1,
            "estimated_total_items": 1,
        }
    ]


def test_basic_auth_reads_secrets_only_from_environment(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = KnowledgeApplication.from_config(config_path)
    app.config.ingestion.http.authentication.mode = "basic"
    synchronizer = OfficialSourceSynchronizer(
        config=app.config,
        ingestion_manager=app.ingestion_manager,
        source_store=SourceStore(app.database),
        catalog=app.catalog,
    )
    with pytest.raises(SourceSyncError, match="BMC_DOCS_USERNAME"):
        synchronizer._authentication()
    monkeypatch.setenv("BMC_DOCS_USERNAME", "automation-user")
    monkeypatch.setenv("BMC_DOCS_PASSWORD", "secret-value")
    authentication = synchronizer._authentication()
    request = httpx.Request("GET", "https://docs.bmc.com")
    flow = authentication.auth_flow(request)
    authenticated = next(flow)
    assert authenticated.headers["Authorization"].startswith("Basic ")
    assert "secret-value" not in str(authenticated.headers)

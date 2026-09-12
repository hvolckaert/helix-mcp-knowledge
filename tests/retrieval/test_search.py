import pytest

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.errors import ChunkNotFoundError
from helix_mcp_knowledge.models.search import SearchRequest
from helix_mcp_knowledge.models.source import SourceScope


def result_ids(response) -> set[str]:
    return {result.chunk_id for result in response.results}


def test_no_project_means_official_only(app) -> None:
    response = app.search_engine.search(SearchRequest(query="reconciliation"))
    assert result_ids(response) == {"chk_official"}
    assert response.context.project_id is None


def test_all_relevant_isolates_active_project(app) -> None:
    app.set_active_project("example_project")
    response = app.search_engine.search(SearchRequest(query="reconciliation"))
    assert result_ids(response) == {"chk_official", "chk_example_project"}
    assert "chk_atlas" not in result_ids(response)
    assert response.context.project_source == "session"


def test_explicit_project_overrides_active_project(app) -> None:
    app.set_active_project("atlas")
    response = app.search_engine.search(
        SearchRequest(
            query="reconciliation",
            project_id="example_project",
            source_scope=SourceScope.PROJECT,
        )
    )
    assert result_ids(response) == {"chk_example_project"}
    assert response.context.project_source == "request"


def test_project_product_infers_and_filters_version(app) -> None:
    response = app.search_engine.search(
        SearchRequest(query="reconciliation", project_id="example_project", product="Helix CMDB")
    )
    assert result_ids(response) == {"chk_example_project"}
    assert response.context.product == "cmdb"
    assert response.context.version == "26.1"
    assert response.context.version_source == "project_configuration"


def test_result_versions_are_unique_across_multiple_products(app) -> None:
    with app.database.connect() as connection:
        connection.execute(
            "INSERT INTO document_products(document_id, product_id) VALUES "
            "('doc_example_project', 'arsystem')"
        )
        connection.execute(
            "INSERT INTO product_versions(product_version_id, product_id, version) "
            "VALUES ('arsystem:26.1', 'arsystem', '26.1')"
        )
        connection.execute(
            "INSERT INTO document_product_versions(document_id, product_version_id) "
            "VALUES ('doc_example_project', 'arsystem:26.1')"
        )
        connection.commit()

    response = app.search_engine.search(
        SearchRequest(
            query="reconciliation",
            project_id="example_project",
            source_scope=SourceScope.PROJECT,
        )
    )

    assert response.results[0].versions == ["26.1"]


def test_exact_technical_term_is_transparent(app) -> None:
    response = app.search_engine.search(
        SearchRequest(query="Explain BMC_ComputerSystem ARERR 120029")
    )
    assert response.results[0].match.exact_terms == ["ARERR 120029", "BMC_ComputerSystem"]


def test_get_section_returns_adjacent_chunks(app, insert_document) -> None:
    insert_document(
        app,
        document_id="doc_section",
        chunk_id="chk_section_0",
        title="Section test",
        text="first adjacent paragraph",
        source_scope="bmc_official",
        project_id=None,
        version="25.1",
        position=0,
    )
    with app.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO chunks(
                chunk_id, document_id, source_scope, project_id, document_type,
                heading_path_json, chunk_type, text, embedding_text, position,
                token_count, content_hash, active
            ) VALUES ('chk_section_1', 'doc_section', 'bmc_official', NULL, 'design',
                      '["Section test"]', 'paragraph', 'second paragraph',
                      'second paragraph', 1, 2, 'hash-section-1', 1)
            """
        )
        connection.commit()
    response = app.search_engine.get_section("chk_section_1", context_before=1, context_after=0)
    assert [chunk.chunk_id for chunk in response.chunks] == ["chk_section_0", "chk_section_1"]


def test_get_section_rejects_inactive_project_chunk(app) -> None:
    with pytest.raises(ChunkNotFoundError):
        app.search_engine.get_section("chk_example_project")

    response = app.search_engine.get_section("chk_example_project", project_id="example_project")
    assert response.selected_chunk_id == "chk_example_project"

    app.set_active_project("example_project")
    assert (
        app.search_engine.get_section("chk_example_project").selected_chunk_id
        == "chk_example_project"
    )
    app.set_active_project(None)

    with pytest.raises(ChunkNotFoundError):
        app.search_engine.get_section("chk_example_project")


def test_product_and_version_filter_must_match_the_same_product(app) -> None:
    with app.database.connect() as connection:
        connection.execute(
            "INSERT INTO document_products(document_id, product_id) VALUES "
            "('doc_official', 'arsystem')"
        )
        connection.execute(
            "INSERT INTO product_versions(product_version_id, product_id, version) "
            "VALUES ('arsystem:99.9', 'arsystem', '99.9')"
        )
        connection.execute(
            "INSERT INTO document_product_versions(document_id, product_version_id) "
            "VALUES ('doc_official', 'arsystem:99.9')"
        )
        connection.commit()

    response = app.search_engine.search(
        SearchRequest(query="reconciliation", product="cmdb", version="99.9")
    )

    assert response.results == []


def test_product_and_version_lists_only_report_indexed_content(app) -> None:
    products = app.search_engine.list_products()
    versions = app.search_engine.list_versions("CMDB")
    assert [product.product_id for product in products.products] == ["cmdb"]
    assert products.products[0].configured is True
    assert products.products[0].indexed is True
    assert {version.version for version in versions.versions} == {"25.1", "26.1"}
    assert all(version.indexed for version in versions.versions)


def test_product_list_reports_configured_product_before_first_sync(config_path) -> None:
    application = KnowledgeApplication.from_config(config_path)

    products = application.search_engine.list_products()

    assert [product.model_dump() for product in products.products] == [
        {
            "product_id": "cmdb",
            "name": "BMC Helix CMDB",
            "aliases": ["CMDB", "BMC CMDB", "Helix CMDB"],
            "configured": True,
            "indexed": False,
        }
    ]


def test_product_list_retains_indexed_unconfigured_product(config_path, insert_document) -> None:
    application = KnowledgeApplication.from_config(config_path)
    application.config.official_docs.products = {}
    insert_document(
        application,
        document_id="doc_retained_product",
        chunk_id="chk_retained_product",
        title="Retained CMDB guide",
        text="Indexed evidence retained after configuration changed.",
        source_scope="bmc_official",
        project_id=None,
        version="25.1",
    )

    products = application.search_engine.list_products()

    assert len(products.products) == 1
    assert products.products[0].product_id == "cmdb"
    assert products.products[0].configured is False
    assert products.products[0].indexed is True


def test_version_list_reports_configured_version_before_first_sync(config_path) -> None:
    application = KnowledgeApplication.from_config(config_path)

    versions = application.search_engine.list_versions("CMDB")

    assert versions.product == "cmdb"
    assert [version.model_dump() for version in versions.versions] == [
        {"version": "26.1", "indexed": False}
    ]


def test_version_list_merges_configured_and_indexed_versions(config_path, insert_document) -> None:
    application = KnowledgeApplication.from_config(config_path)
    insert_document(
        application,
        document_id="doc_previous_version",
        chunk_id="chk_previous_version",
        title="Previous CMDB guide",
        text="Indexed CMDB evidence from an earlier version.",
        source_scope="bmc_official",
        project_id=None,
        version="25.1",
    )

    versions = application.search_engine.list_versions("cmdb")

    assert [version.model_dump() for version in versions.versions] == [
        {"version": "26.1", "indexed": False},
        {"version": "25.1", "indexed": True},
    ]

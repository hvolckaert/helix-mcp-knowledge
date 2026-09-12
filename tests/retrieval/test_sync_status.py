from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.storage.automation import AutomationStore


def test_sync_status_reports_configured_official_corpus_before_sync(config_path) -> None:
    application = KnowledgeApplication.from_config(config_path)

    response = application.get_sync_status()

    assert response.official.status == "not_started"
    assert response.official.ready is False
    assert response.official.automatic_sync is False
    assert response.official.configured_products == {"cmdb": ["26.1"]}
    assert response.official.indexed_documents == 0
    assert response.official.indexed_chunks == 0
    assert response.official.error_count == 0
    assert response.project is None


def test_sync_status_reports_unconfigured_official_corpus(config_path) -> None:
    application = KnowledgeApplication.from_config(config_path)
    application.config.official_docs.products = {}

    response = application.get_sync_status()

    assert response.official.status == "not_configured"
    assert response.official.ready is False
    assert response.official.configured_products == {}


def test_sync_status_reports_ready_index_and_safe_automation_summary(app) -> None:
    store = AutomationStore(app.database)
    store.update_state(
        "official-docs",
        {
            "status": "error",
            "started_at": "2026-09-05T01:00:00+00:00",
            "finished_at": "2026-09-05T01:01:00+00:00",
            "next_run_at": "2026-09-05T01:16:00+00:00",
            "result_counts": {
                "unchanged": 12,
                "purged": 3,
                "error": 1,
                "/secret/count": 99,
            },
            "errors": ["private path /secret/project/document.pdf"],
            "owner_id": "internal-owner",
            "process_id": 1234,
        },
    )

    response = app.get_sync_status()
    payload = response.model_dump_json()

    assert response.official.status == "error"
    assert response.official.ready is True
    assert response.official.indexed_documents == 1
    assert response.official.indexed_chunks == 1
    assert response.official.result_counts == {"unchanged": 12, "purged": 3, "error": 1}
    assert response.official.error_count == 1
    assert "/secret/" not in payload
    assert "internal-owner" not in payload
    assert "1234" not in payload


def test_sync_status_maps_running_automation_state(config_path) -> None:
    application = KnowledgeApplication.from_config(config_path)
    AutomationStore(application.database).update_state(
        "official-docs",
        {
            "status": "running",
            "started_at": "2026-09-05T02:00:00+00:00",
            "selected_products": {"cmdb": ["26.1"]},
            "cancel_requested": True,
            "progress": {
                "phase": "discovering",
                "processed_items": 12,
                "estimated_total_items": 40,
                "percent": 30.0,
                "current_product": "cmdb",
                "current_version": "26.1",
                "last_activity_at": "2026-09-05T02:02:00+00:00",
                "result_counts": {"indexed": 4, "missing": 2, "error": 1, "/secret": 99},
                "chunks_indexed": 80,
                "product_versions": [
                    {
                        "product": "cmdb",
                        "version": "26.1",
                        "processed_items": 12,
                        "estimated_total_items": 40,
                    }
                ],
            },
        },
    )

    response = application.get_sync_status()

    assert response.official.status == "running"
    assert response.official.ready is False
    assert response.official.automation_status == "running"
    assert response.official.started_at == "2026-09-05T02:00:00+00:00"
    assert response.official.cancellation_requested is True
    assert response.official.progress is not None
    assert response.official.progress.phase == "discovering"
    assert response.official.progress.percent == 30.0
    assert response.official.progress.result_counts == {"indexed": 4, "missing": 2, "error": 1}
    assert response.official.notice_count == 2
    assert response.official.error_count == 1
    assert response.official.progress.product_versions[0].processed_items == 12


def test_sync_status_only_exposes_effective_project(app, insert_document) -> None:
    insert_document(
        app,
        document_id="doc_atlas_second",
        chunk_id="chk_atlas_second",
        title="ATLAS second design",
        text="Additional confidential evidence for Atlas only.",
        source_scope="project",
        project_id="atlas",
        version="26.1",
    )

    without_project = app.get_sync_status()
    assert without_project.official.status == "ready"
    assert without_project.official.ready is True
    assert without_project.project is None

    app.set_active_project("example_project")
    example_project = app.get_sync_status().project
    assert example_project is not None
    assert example_project.project_id == "example_project"
    assert example_project.context_source == "session"
    assert example_project.indexed_documents == 1
    assert example_project.indexed_chunks == 1

    atlas = app.get_sync_status("atlas").project
    assert atlas is not None
    assert atlas.project_id == "atlas"
    assert atlas.context_source == "request"
    assert atlas.indexed_documents == 2
    assert atlas.indexed_chunks == 2

from pathlib import Path

import pytest

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.errors import IngestionError
from helix_mcp_knowledge.models.document import DocumentType
from helix_mcp_knowledge.models.ingestion import IngestRequest
from helix_mcp_knowledge.models.search import SearchRequest
from helix_mcp_knowledge.models.source import SourceScope


class FakeEmbedder:
    dimension = 2

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class RecordingVectorIndex:
    enabled = True

    def __init__(self) -> None:
        self.upserts = []
        self.deletes: list[list[str]] = []

    def upsert(self, records) -> None:
        self.upserts.append(records)

    def delete(self, chunk_ids: list[str]) -> None:
        self.deletes.append(chunk_ids)

    def search(self, vector, **kwargs):
        return []


def project_request(path: Path) -> IngestRequest:
    return IngestRequest(
        source_path=path,
        source_scope=SourceScope.PROJECT,
        project_id="example_project",
        document_type=DocumentType.DESIGN,
        product_versions={"Helix CMDB": None},
    )


def test_ingestion_is_searchable_and_infers_project_version(config_path: Path) -> None:
    app = KnowledgeApplication.from_config(config_path)
    path = config_path.parent.parent / "data/sources/projects/example_project/docs/design.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Example Project Design\n\n"
        "## Reconciliation\n\n"
        "Special precedence for ExampleProjectDataset.",
        encoding="utf-8",
    )
    result = app.ingestion_manager.ingest(project_request(path))

    assert result.status == "indexed"
    assert result.chunks_indexed == 1
    response = app.search_engine.search(
        SearchRequest(query="ExampleProjectDataset", project_id="example_project", product="cmdb")
    )
    assert [item.document_id for item in response.results] == [result.document_id]
    assert response.results[0].versions == ["26.1"]


def test_unchanged_source_is_idempotent(config_path: Path, monkeypatch) -> None:
    app = KnowledgeApplication.from_config(config_path)
    path = config_path.parent.parent / "data/sources/projects/example_project/docs/stable.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Stable\n\nStableContent marker.", encoding="utf-8")
    first = app.ingestion_manager.ingest(project_request(path))

    def unexpected_parse(source):
        raise AssertionError("unchanged local source should not be parsed again")

    monkeypatch.setattr(app.ingestion_manager.parsers, "parse", unexpected_parse)
    second = app.ingestion_manager.ingest(project_request(path))
    assert second.status == "unchanged"
    assert second.document_id == first.document_id
    with app.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM documents WHERE source_path = ?", (str(path),)
            ).fetchone()[0]
            == 1
        )


def test_changed_ingestion_metadata_reindexes_unchanged_file(config_path: Path) -> None:
    app = KnowledgeApplication.from_config(config_path)
    path = config_path.parent.parent / "data/sources/projects/example_project/docs/metadata.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Metadata\n\nStable content.", encoding="utf-8")
    first = app.ingestion_manager.ingest(project_request(path))
    changed = project_request(path).model_copy(update={"document_type": DocumentType.ARCHITECTURE})

    second = app.ingestion_manager.ingest(changed)

    assert second.status == "indexed"
    assert second.document_id == first.document_id
    with app.database.connect() as connection:
        row = connection.execute(
            "SELECT document_type FROM documents WHERE document_id = ?",
            (first.document_id,),
        ).fetchone()
    assert row["document_type"] == "architecture"


def test_changed_source_atomically_replaces_chunks_and_fts(config_path: Path) -> None:
    app = KnowledgeApplication.from_config(config_path)
    path = config_path.parent.parent / "data/sources/projects/example_project/docs/change.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Change\n\nOldUniqueMarker.", encoding="utf-8")
    first = app.ingestion_manager.ingest(project_request(path))
    old_chunk_ids = app.ingestion_manager.store.chunk_ids(first.document_id)

    path.write_text("# Change\n\nNewUniqueMarker.", encoding="utf-8")
    second = app.ingestion_manager.ingest(project_request(path))
    new_chunk_ids = app.ingestion_manager.store.chunk_ids(second.document_id)

    assert second.document_id == first.document_id
    assert set(old_chunk_ids).isdisjoint(new_chunk_ids)
    old_results = app.search_engine.search(
        SearchRequest(query="OldUniqueMarker", project_id="example_project")
    )
    new_results = app.search_engine.search(
        SearchRequest(query="NewUniqueMarker", project_id="example_project")
    )
    assert old_results.results == []
    assert [item.document_id for item in new_results.results] == [first.document_id]


def test_failed_refresh_preserves_last_successfully_indexed_revision(
    config_path: Path, monkeypatch
) -> None:
    app = KnowledgeApplication.from_config(config_path)
    path = config_path.parent.parent / "data/sources/projects/example_project/docs/last-good.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Stable\n\nLastGoodUniqueMarker.", encoding="utf-8")
    first = app.ingestion_manager.ingest(project_request(path))
    path.write_text("# Broken\n\nReplacement content.", encoding="utf-8")

    def fail_parse(*_args, **_kwargs):
        raise IngestionError("simulated parser failure")

    monkeypatch.setattr(app.ingestion_manager.parsers, "parse", fail_parse)
    with pytest.raises(IngestionError, match="simulated parser failure"):
        app.ingestion_manager.ingest(project_request(path))

    with app.database.connect() as connection:
        status = connection.execute(
            "SELECT status FROM documents WHERE document_id = ?", (first.document_id,)
        ).fetchone()["status"]
    assert status == "indexed"
    response = app.search_engine.search(
        SearchRequest(query="LastGoodUniqueMarker", project_id="example_project")
    )
    assert [item.document_id for item in response.results] == [first.document_id]


def test_project_source_outside_authoritative_folder_is_rejected(
    config_path: Path,
) -> None:
    app = KnowledgeApplication.from_config(config_path)
    path = config_path.parent.parent / "outside.md"
    path.write_text("outside", encoding="utf-8")
    try:
        app.ingestion_manager.ingest(project_request(path))
    except Exception as exc:
        assert "configured root" in str(exc)
    else:
        raise AssertionError("ingestion unexpectedly accepted an out-of-root source")


def test_expected_empty_official_page_does_not_create_a_diagnostic_file(
    config_path: Path,
) -> None:
    app = KnowledgeApplication.from_config(config_path)
    path = app.ingestion_manager.official_sources_root / "cmdb/26.1/empty.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    with pytest.raises(IngestionError, match="no indexable chunks"):
        app.ingestion_manager.ingest(
            IngestRequest(
                source_path=path,
                source_scope=SourceScope.BMC_OFFICIAL,
                document_type=DocumentType.REFERENCE,
                product_versions={"cmdb": "26.1"},
                source_url="https://docs.bmc.com/empty/",
            )
        )

    assert list(app.ingestion_manager.errors_path.glob("ingestion-*.json")) == []


def test_semantic_ingestion_upserts_vectors_and_removes_superseded_points(
    config_path: Path,
) -> None:
    app = KnowledgeApplication.from_config(config_path)
    vector_index = RecordingVectorIndex()
    app.ingestion_manager.embedder = FakeEmbedder()
    app.ingestion_manager.vector_index = vector_index
    path = config_path.parent.parent / "data/sources/projects/example_project/docs/vector.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Vector\n\nFirst semantic content.", encoding="utf-8")
    first = app.ingestion_manager.ingest(project_request(path))
    old_chunk_ids = app.ingestion_manager.store.chunk_ids(first.document_id)
    assert first.vectors_indexed == first.chunks_indexed
    assert vector_index.upserts[0][0].payload["project_id"] == "example_project"

    path.write_text("# Vector\n\nReplacement semantic content.", encoding="utf-8")
    app.ingestion_manager.ingest(project_request(path))
    assert vector_index.deletes[-1] == old_chunk_ids


def test_failed_sqlite_replacement_compensates_new_vector_points(
    config_path: Path, monkeypatch
) -> None:
    app = KnowledgeApplication.from_config(config_path)
    vector_index = RecordingVectorIndex()
    app.ingestion_manager.embedder = FakeEmbedder()
    app.ingestion_manager.vector_index = vector_index
    path = config_path.parent.parent / "data/sources/projects/example_project/docs/failure.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Failure\n\nCompensation content.", encoding="utf-8")

    def fail_replace(document, **_kwargs) -> None:
        raise RuntimeError("simulated SQLite failure")

    monkeypatch.setattr(app.ingestion_manager.store, "replace", fail_replace)
    with pytest.raises(IngestionError, match="simulated SQLite failure"):
        app.ingestion_manager.ingest(project_request(path))
    inserted_ids = [record.chunk_id for record in vector_index.upserts[0]]
    assert vector_index.deletes == [inserted_ids]

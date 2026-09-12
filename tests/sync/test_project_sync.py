import json
from pathlib import Path

import pytest

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.errors import SourceSyncError
from helix_mcp_knowledge.models.document import DocumentType
from helix_mcp_knowledge.models.ingestion import IngestRequest
from helix_mcp_knowledge.models.search import SearchRequest
from helix_mcp_knowledge.models.source import SourceScope
from helix_mcp_knowledge.storage.vector_cleanup import VectorCleanupStore
from helix_mcp_knowledge.update_lock import UpdateLock, UpdateLockBusyError


class RecordingVectorIndex:
    enabled = True

    def __init__(self) -> None:
        self.deletes: list[list[str]] = []

    def delete(self, chunk_ids: list[str]) -> None:
        self.deletes.append(chunk_ids)


class FailingVectorIndex(RecordingVectorIndex):
    def delete(self, chunk_ids: list[str]) -> None:
        super().delete(chunk_ids)
        raise RuntimeError("simulated vector backend failure")


def configure_manifest(config_path: Path, content: str) -> Path:
    project_path = config_path.parent / "projects/example_project.yaml"
    project_text = project_path.read_text(encoding="utf-8")
    project_path.write_text(
        project_text.replace(
            "  path: data/sources/projects/example_project/docs",
            "  path: data/sources/projects/example_project/docs\n"
            "  sources_manifest: config/projects/example_project.sources.yaml",
        ),
        encoding="utf-8",
    )
    manifest_path = config_path.parent / "projects/example_project.sources.yaml"
    manifest_path.write_text(content, encoding="utf-8")
    return manifest_path


def manifest(*sources: str) -> str:
    if not sources:
        return "schema_version: 1\nproject_id: example_project\nsources: []\n"
    rendered = "\n".join(sources)
    return f"schema_version: 1\nproject_id: example_project\nsources:\n{rendered}\n"


def source(path: str, document_type: str = "design") -> str:
    return f'  - path: "{path}"\n    document_type: {document_type}\n    products: {{cmdb: null}}'


def test_project_sync_is_idempotent_and_adopts_manifest_metadata(config_path: Path) -> None:
    root = config_path.parent.parent / "data/sources/projects/example_project/docs"
    root.mkdir(parents=True)
    document = root / "[example_project] design.md"
    document.write_text("# Design\n\nProjectSyncMarker.", encoding="utf-8")
    manifest_path = configure_manifest(config_path, manifest(source(document.name)))
    app = KnowledgeApplication.from_config(config_path)

    first = app.sync_project_sources("example_project")
    second = app.sync_project_sources("example_project")
    assert [item.status for item in first] == ["indexed"]
    assert [item.status for item in second] == ["unchanged"]

    manifest_path.write_text(manifest(source(document.name, "architecture")), encoding="utf-8")
    third = app.sync_project_sources("example_project")
    assert [item.status for item in third] == ["indexed"]
    with app.database.connect() as connection:
        row = connection.execute(
            "SELECT document_type, metadata_json FROM documents WHERE document_id = ?",
            (third[0].document_id,),
        ).fetchone()
    assert row["document_type"] == "architecture"
    metadata = json.loads(row["metadata_json"])
    assert metadata["_project_sync"]["manifest_id"] == "project:example_project"


def test_project_sync_marks_removed_managed_document_missing(config_path: Path) -> None:
    root = config_path.parent.parent / "data/sources/projects/example_project/docs"
    root.mkdir(parents=True)
    document = root / "retired.md"
    document.write_text("# Retired\n\nRetiredProjectMarker.", encoding="utf-8")
    configure_manifest(config_path, manifest(source(document.name)))
    app = KnowledgeApplication.from_config(config_path)
    indexed = app.sync_project_sources("example_project")[0]
    old_chunk_ids = app.ingestion_manager.store.chunk_ids(indexed.document_id)
    vector_index = RecordingVectorIndex()
    app.ingestion_manager.vector_index = vector_index

    document.unlink()
    result = app.sync_project_sources("example_project")

    assert [item.status for item in result] == ["missing"]
    with app.database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM documents WHERE document_id = ?", (indexed.document_id,)
        ).fetchone()
        active = connection.execute(
            "SELECT count(*) FROM chunks WHERE document_id = ? AND active = 1",
            (indexed.document_id,),
        ).fetchone()[0]
    assert row is None
    assert active == 0
    assert vector_index.deletes == [old_chunk_ids]
    assert (
        app.search_engine.search(
            SearchRequest(query="RetiredProjectMarker", project_id="example_project")
        ).results
        == []
    )


def test_project_sync_retries_durable_vector_cleanup_before_purging_document(
    config_path: Path,
) -> None:
    root = config_path.parent.parent / "data/sources/projects/example_project/docs"
    root.mkdir(parents=True)
    document = root / "retry-cleanup.md"
    document.write_text("# Retired\n\nRetryCleanupMarker.", encoding="utf-8")
    configure_manifest(config_path, manifest(source(document.name)))
    app = KnowledgeApplication.from_config(config_path)
    indexed = app.sync_project_sources("example_project")[0]
    old_chunk_ids = app.ingestion_manager.store.chunk_ids(indexed.document_id)
    app.ingestion_manager.vector_index = FailingVectorIndex()

    document.unlink()
    failed = app.sync_project_sources("example_project")

    assert [item.status for item in failed] == ["error"]
    assert VectorCleanupStore(app.database).count() == len(old_chunk_ids)
    with app.database.connect() as connection:
        status = connection.execute(
            "SELECT status FROM documents WHERE document_id = ?", (indexed.document_id,)
        ).fetchone()["status"]
    assert status == "missing"

    recovered_index = RecordingVectorIndex()
    app.ingestion_manager.vector_index = recovered_index
    recovered = app.sync_project_sources("example_project")

    assert [item.status for item in recovered] == ["missing"]
    assert recovered_index.deletes == [old_chunk_ids]
    assert VectorCleanupStore(app.database).count() == 0
    with app.database.connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM documents WHERE document_id = ?", (indexed.document_id,)
        ).fetchone()
    assert row is None


def test_project_sync_never_prunes_when_documentation_root_is_unavailable(
    config_path: Path,
) -> None:
    root = config_path.parent.parent / "data/sources/projects/example_project/docs"
    root.mkdir(parents=True)
    document = root / "retained.md"
    document.write_text("# Retained\n\nRootUnavailableMarker.", encoding="utf-8")
    configure_manifest(config_path, manifest(source(document.name)))
    app = KnowledgeApplication.from_config(config_path)
    indexed = app.sync_project_sources("example_project")[0]
    document.unlink()
    root.rmdir()

    with pytest.raises(SourceSyncError, match="documentation root is unavailable"):
        app.sync_project_sources("example_project")

    with app.database.connect() as connection:
        status = connection.execute(
            "SELECT status FROM documents WHERE document_id = ?", (indexed.document_id,)
        ).fetchone()["status"]
    assert status == "indexed"


def test_project_sync_does_not_prune_after_an_ingestion_error(config_path: Path) -> None:
    root = config_path.parent.parent / "data/sources/projects/example_project/docs"
    root.mkdir(parents=True)
    stable = root / "stable.md"
    stable.write_text("# Stable\n\nKeepOnErrorMarker.", encoding="utf-8")
    manifest_path = configure_manifest(config_path, manifest(source(stable.name)))
    app = KnowledgeApplication.from_config(config_path)
    indexed = app.sync_project_sources("example_project")[0]

    stable.unlink()
    broken = root / "broken.md"
    broken.write_bytes(b"\xff\xfe\x00")
    manifest_path.write_text(manifest(source(stable.name), source(broken.name)), encoding="utf-8")
    result = app.sync_project_sources("example_project")

    assert [item.status for item in result] == ["error"]
    with app.database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM documents WHERE document_id = ?", (indexed.document_id,)
        ).fetchone()
    assert row["status"] == "indexed"


def test_project_sync_glob_ignores_unsupported_sidecars(config_path: Path) -> None:
    root = config_path.parent.parent / "data/sources/projects/example_project/docs"
    root.mkdir(parents=True)
    document = root / "guide.md"
    document.write_text("# Guide\n\nGlobMarker.", encoding="utf-8")
    (root / "guide.md:Zone.Identifier").write_text("sidecar", encoding="utf-8")
    configure_manifest(config_path, manifest(source("*")))
    app = KnowledgeApplication.from_config(config_path)

    result = app.sync_project_sources("example_project")

    assert len(result) == 1
    assert result[0].source_path == str(document)


def test_project_sync_never_prunes_another_project(config_path: Path) -> None:
    configure_manifest(config_path, manifest())
    app = KnowledgeApplication.from_config(config_path)
    atlas_path = config_path.parent.parent / "data/sources/projects/atlas/docs/atlas.md"
    atlas_path.parent.mkdir(parents=True)
    atlas_path.write_text("# Atlas\n\nAtlasIsolationMarker.", encoding="utf-8")
    atlas = app.ingestion_manager.ingest(
        IngestRequest(
            source_path=atlas_path,
            source_scope=SourceScope.PROJECT,
            project_id="atlas",
            document_type=DocumentType.DESIGN,
            product_versions={"cmdb": None},
            metadata={"_project_sync": {"manifest_id": "project:example_project"}},
        )
    )

    result = app.sync_project_sources("example_project")

    assert result == []
    with app.database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM documents WHERE document_id = ?", (atlas.document_id,)
        ).fetchone()
    assert row["status"] == "indexed"


def test_project_sync_is_excluded_by_the_shared_maintenance_lock(config_path: Path) -> None:
    configure_manifest(config_path, manifest())
    app = KnowledgeApplication.from_config(config_path)

    with (
        UpdateLock(app.config.base_dir / ".update.lock"),
        pytest.raises(UpdateLockBusyError, match="synchronization, update, or cleanup"),
    ):
        app.sync_project_sources("example_project")

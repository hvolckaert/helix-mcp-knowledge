from pathlib import Path

from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.semantic_component import (
    SEMANTIC_COMPONENT_VERSION,
    SemanticComponentStatus,
)
from helix_mcp_knowledge.semantic_install_worker import (
    SEMANTIC_INSTALL_JOB,
    SemanticRebuildResult,
    rebuild_vectors,
    run_install,
)
from helix_mcp_knowledge.storage.automation import AutomationStore
from helix_mcp_knowledge.storage.database import Database


class RecordingSemanticClient:
    def __init__(self) -> None:
        self.reset_called = False
        self.records = []
        self.existing_ids: set[str] = set()
        self.deleted_ids: list[str] = []

    def reset(self) -> None:
        self.reset_called = True
        self.existing_ids.clear()

    def chunk_ids(self) -> set[str]:
        return set(self.existing_ids)

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    def upsert(self, records) -> None:
        self.records.extend(records)

        self.existing_ids.update(record.chunk_id for record in records)

    def delete(self, chunk_ids) -> None:
        self.deleted_ids.extend(chunk_ids)
        self.existing_ids.difference_update(chunk_ids)


def test_rebuild_vectors_covers_every_active_sqlite_chunk(app, monkeypatch) -> None:
    client = RecordingSemanticClient()
    monkeypatch.setattr(
        "helix_mcp_knowledge.semantic_install_worker.SemanticComponentManager.ensure_service",
        lambda _self: client,
    )
    store = AutomationStore(app.database)

    result = rebuild_vectors(app.config, store)

    assert result.total == 3
    assert result.completed == 3
    assert result.cancelled is False
    assert client.reset_called is False
    assert {item.chunk_id for item in client.records} == {
        "chk_official",
        "chk_example_project",
        "chk_atlas",
    }
    example_project = next(
        item for item in client.records if item.chunk_id == "chk_example_project"
    )
    assert example_project.payload["project_id"] == "example_project"
    assert example_project.payload["product_ids"] == ["cmdb"]
    assert example_project.payload["product_versions"] == ["26.1"]
    assert example_project.payload["product_version_pairs"] == ["cmdb:26.1"]


def test_rebuild_vectors_reconciles_existing_and_stale_ids(app, monkeypatch) -> None:
    client = RecordingSemanticClient()
    client.existing_ids = {"chk_official", "chk_stale"}
    monkeypatch.setattr(
        "helix_mcp_knowledge.semantic_install_worker.SemanticComponentManager.ensure_service",
        lambda _self: client,
    )

    result = rebuild_vectors(app.config, AutomationStore(app.database))

    assert result == SemanticRebuildResult(total=3, completed=3, cancelled=False)
    assert client.deleted_ids == ["chk_stale"]
    assert {record.chunk_id for record in client.records} == {"chk_example_project", "chk_atlas"}


def test_worker_enables_semantic_only_after_vector_build(config_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "helix_mcp_knowledge.semantic_install_worker.SemanticComponentManager.install",
        lambda _self: SemanticComponentStatus(
            installed=True,
            status="ready",
            component_version=SEMANTIC_COMPONENT_VERSION,
            installed_bytes=4096,
        ),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.semantic_install_worker.rebuild_vectors",
        lambda _config, _store, **_kwargs: SemanticRebuildResult(
            total=123, completed=123, cancelled=False
        ),
    )

    assert run_install(config_path) is True

    config = load_config(config_path)
    assert config.retrieval.semantic.enabled is True
    state = AutomationStore(Database(config.sqlite_path)).state(SEMANTIC_INSTALL_JOB)
    assert state["status"] == "ready"
    assert state["indexed_chunks"] == 123
    assert state["payload_version"] == 2


def test_worker_respects_disable_while_building(config_path: Path, monkeypatch) -> None:
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    AutomationStore(database).update_state(
        SEMANTIC_INSTALL_JOB,
        {"status": "indexing", "desired_enabled": False},
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.semantic_install_worker.SemanticComponentManager.install",
        lambda _self: SemanticComponentStatus(installed=True, status="ready"),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.semantic_install_worker.rebuild_vectors",
        lambda _config, _store, **_kwargs: SemanticRebuildResult(
            total=0, completed=0, cancelled=True
        ),
    )

    assert run_install(config_path) is True
    assert load_config(config_path).retrieval.semantic.enabled is False
    state = AutomationStore(Database(config.sqlite_path)).state(SEMANTIC_INSTALL_JOB)
    assert state["cancelled"] is True

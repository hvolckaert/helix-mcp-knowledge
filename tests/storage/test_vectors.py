from pathlib import Path

import pytest

pytest.importorskip("qdrant_client")

from helix_mcp_knowledge.models.source import SourceScope
from helix_mcp_knowledge.storage.vectors import QdrantVectorIndex, VectorRecord


def payload(scope: str, project_id: str | None, version: str) -> dict[str, object]:
    return {
        "document_id": f"doc-{project_id or 'official'}",
        "source_scope": scope,
        "project_id": project_id,
        "document_type": "design",
        "product_ids": ["cmdb"],
        "product_versions": [version],
        "product_version_pairs": [f"cmdb:{version}"],
        "classification": "public" if project_id is None else "confidential",
        "language": "es",
        "active": True,
    }


def test_qdrant_filters_all_relevant_to_official_plus_effective_project() -> None:
    index = QdrantVectorIndex(
        url=":memory:",
        collection="test_chunks",
        dimension=2,
        api_key_env="UNUSED_QDRANT_KEY",
    )
    index.upsert(
        [
            VectorRecord("chk_official", [1.0, 0.0], payload("bmc_official", None, "26.1")),
            VectorRecord(
                "chk_example_project", [0.9, 0.1], payload("project", "example_project", "26.1")
            ),
            VectorRecord("chk_atlas", [1.0, 0.0], payload("project", "atlas", "26.1")),
        ]
    )
    assert index.chunk_ids() == {"chk_official", "chk_example_project", "chk_atlas"}
    candidates = index.search(
        [1.0, 0.0],
        source_scope=SourceScope.ALL_RELEVANT,
        project_id="example_project",
        product_id="cmdb",
        version="26.1",
        document_types=["design"],
        limit=10,
    )
    assert {candidate.chunk_id for candidate in candidates} == {
        "chk_official",
        "chk_example_project",
    }

    index.delete(["chk_example_project"])
    remaining = index.search(
        [1.0, 0.0],
        source_scope=SourceScope.PROJECT,
        project_id="example_project",
        product_id="cmdb",
        version="26.1",
        document_types=None,
        limit=10,
    )
    assert remaining == []


def test_qdrant_matches_the_requested_product_and_version_as_a_pair() -> None:
    index = QdrantVectorIndex(
        url=":memory:",
        collection="paired_product_versions",
        dimension=2,
        api_key_env="UNUSED_QDRANT_KEY",
    )
    mismatched = payload("bmc_official", None, "26.1")
    mismatched["product_ids"] = ["cmdb", "itsm"]
    mismatched["product_versions"] = ["26.1", "26.2"]
    mismatched["product_version_pairs"] = ["cmdb:26.1", "itsm:26.2"]
    index.upsert([VectorRecord("chk_mismatched", [1.0, 0.0], mismatched)])

    candidates = index.search(
        [1.0, 0.0],
        source_scope=SourceScope.BMC_OFFICIAL,
        project_id=None,
        product_id="cmdb",
        version="26.2",
        document_types=None,
        limit=10,
    )

    assert candidates == []


def test_local_qdrant_persists_and_can_reset(tmp_path: Path) -> None:
    path = tmp_path / "vectors"
    first = QdrantVectorIndex(
        url=":local:",
        path=path,
        collection="persistent_chunks",
        dimension=2,
        api_key_env="UNUSED_QDRANT_KEY",
    )
    first.upsert([VectorRecord("chk_persisted", [1.0, 0.0], payload("bmc_official", None, "26.1"))])
    first.client.close()

    reopened = QdrantVectorIndex(
        url=":local:",
        path=path,
        collection="persistent_chunks",
        dimension=2,
        api_key_env="UNUSED_QDRANT_KEY",
    )
    found = reopened.search(
        [1.0, 0.0],
        source_scope=SourceScope.BMC_OFFICIAL,
        project_id=None,
        product_id="cmdb",
        version="26.1",
        document_types=None,
        limit=1,
    )
    assert [item.chunk_id for item in found] == ["chk_persisted"]

    reopened.reset()
    assert reopened.chunk_ids() == set()
    assert (
        reopened.search(
            [1.0, 0.0],
            source_scope=SourceScope.BMC_OFFICIAL,
            project_id=None,
            product_id="cmdb",
            version="26.1",
            document_types=None,
            limit=1,
        )
        == []
    )
    reopened.client.close()

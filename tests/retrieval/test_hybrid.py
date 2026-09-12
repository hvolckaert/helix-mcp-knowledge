from dataclasses import dataclass

from helix_mcp_knowledge.errors import SemanticBackendError
from helix_mcp_knowledge.models.search import SearchRequest
from helix_mcp_knowledge.models.source import SourceScope
from helix_mcp_knowledge.retrieval.fusion import FusedCandidate, fuse_rankings
from helix_mcp_knowledge.retrieval.search_engine import SearchEngine
from helix_mcp_knowledge.storage.vectors import SemanticCandidate


@dataclass
class FakeEmbedder:
    dimension: int = 2

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class FakeVectorIndex:
    enabled = True

    def upsert(self, records) -> None:
        return None

    def delete(self, chunk_ids) -> None:
        return None

    def search(self, vector, **kwargs) -> list[SemanticCandidate]:
        return [
            SemanticCandidate(chunk_id="chk_atlas", rank=1),
            SemanticCandidate(chunk_id="chk_example_project", rank=2),
        ]


class FailingEmbedder:
    dimension = 2

    def encode(self, texts: list[str]) -> list[list[float]]:
        raise SemanticBackendError("service unavailable")


def test_rrf_rewards_candidates_present_in_both_rankings() -> None:
    fused = fuse_rankings(
        ["lexical", "both"],
        ["semantic", "both"],
        rrf_k=60,
    )
    assert fused[0].chunk_id == "both"
    assert fused[0].lexical is True
    assert fused[0].semantic is True


def test_semantic_candidates_are_revalidated_by_sqlite_project_filter(app) -> None:
    app.config.retrieval.semantic.enabled = True
    engine = SearchEngine(
        app.database,
        app.config,
        app.catalog,
        app.project_context,
        embedder=FakeEmbedder(),
        vector_index=FakeVectorIndex(),
    )
    response = engine.search(
        SearchRequest(
            query="NoLexicalMatchExpected",
            project_id="example_project",
            source_scope=SourceScope.PROJECT,
        )
    )
    assert [result.chunk_id for result in response.results] == ["chk_example_project"]
    assert response.results[0].match.lexical is False
    assert response.results[0].match.semantic is True


def test_semantic_failure_falls_back_when_lexical_search_is_enabled(app) -> None:
    app.config.retrieval.semantic.enabled = True
    engine = SearchEngine(
        app.database,
        app.config,
        app.catalog,
        app.project_context,
        embedder=FailingEmbedder(),
        vector_index=FakeVectorIndex(),
    )

    candidates = engine._semantic_candidates(
        SearchRequest(query="reconciliation"),
        project_id=None,
        product_id="cmdb",
        version="26.1",
        limit=10,
    )

    assert candidates == []


def test_missing_semantic_backend_falls_back_when_lexical_search_is_enabled(app) -> None:
    app.config.retrieval.semantic.enabled = True
    engine = SearchEngine(
        app.database,
        app.config,
        app.catalog,
        app.project_context,
    )

    candidates = engine._semantic_candidates(
        SearchRequest(query="reconciliation"),
        project_id=None,
        product_id="cmdb",
        version="26.1",
        limit=10,
    )

    assert candidates == []


def _candidate(chunk_id: str, score: float) -> FusedCandidate:
    return FusedCandidate(
        chunk_id=chunk_id,
        lexical=True,
        semantic=False,
        fused_score=score,
    )


def test_diversification_prefers_distinct_documents_before_sections() -> None:
    candidates = [
        _candidate("first", 4.0),
        _candidate("same-section", 3.0),
        _candidate("other-section", 2.0),
        _candidate("other-document", 1.0),
    ]
    rows = {
        "first": {"document_id": "doc-a", "heading_path_json": '["Overview"]'},
        "same-section": {"document_id": "doc-a", "heading_path_json": '["Overview"]'},
        "other-section": {"document_id": "doc-a", "heading_path_json": '["Jobs"]'},
        "other-document": {"document_id": "doc-b", "heading_path_json": '["Overview"]'},
    }

    selected = SearchEngine._diversify_candidates(candidates, rows, 3)

    assert [candidate.chunk_id for candidate in selected] == [
        "first",
        "other-document",
        "other-section",
    ]


def test_diversification_backfills_ranked_chunks_when_unique_sections_are_exhausted() -> None:
    candidates = [
        _candidate("first", 3.0),
        _candidate("same-section", 2.0),
        _candidate("other-section", 1.0),
    ]
    rows = {
        "first": {"document_id": "doc-a", "heading_path_json": '["Overview"]'},
        "same-section": {"document_id": "doc-a", "heading_path_json": '["Overview"]'},
        "other-section": {"document_id": "doc-a", "heading_path_json": '["Jobs"]'},
    }

    selected = SearchEngine._diversify_candidates(candidates, rows, 3)

    assert [candidate.chunk_id for candidate in selected] == [
        "first",
        "other-section",
        "same-section",
    ]

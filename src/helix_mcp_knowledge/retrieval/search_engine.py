"""Hybrid retrieval with SQLite authority and strict project/version filters."""

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from ..catalog.products import ProductCatalog
from ..catalog.versions import normalize_version, version_sort_key
from ..config import AppConfig
from ..errors import ChunkNotFoundError, SearchValidationError, SemanticBackendError
from ..models.product import (
    ProductListResponse,
    ProductSummary,
    VersionInfo,
    VersionListResponse,
)
from ..models.search import (
    MatchInfo,
    SearchContext,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SectionChunk,
    SectionResponse,
)
from ..models.source import SourceScope
from ..projects.context import ProjectContext
from ..storage.database import Database
from ..storage.vectors import NullVectorIndex, VectorIndex
from .embedder import Embedder
from .exact_match import matched_terms
from .fusion import FusedCandidate, fuse_rankings
from .query_parser import build_fts_query, extract_technical_terms
from .reranker import (
    RERANKER_MAX_CANDIDATES,
    RerankCandidate,
    Reranker,
    RerankerRuntime,
    RerankerRuntimeProvider,
    apply_reranker_scores,
)

_DIVERSITY_CANDIDATE_MULTIPLIER = 4
_LOGGER = logging.getLogger(__name__)


class SearchEngine:
    def __init__(
        self,
        database: Database,
        config: AppConfig,
        catalog: ProductCatalog,
        project_context: ProjectContext,
        *,
        embedder: Embedder | None = None,
        vector_index: VectorIndex | None = None,
        reranker: Reranker | None = None,
        reranker_runtime_provider: RerankerRuntimeProvider | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.catalog = catalog
        self.project_context = project_context
        self.embedder = embedder
        self.vector_index = vector_index or NullVectorIndex()
        self.reranker = reranker
        self.reranker_runtime_provider = reranker_runtime_provider

    def search(
        self,
        request: SearchRequest,
        *,
        reranker_enabled: bool | None = None,
    ) -> SearchResponse:
        if request.top_k > self.config.retrieval.max_top_k:
            raise SearchValidationError(
                f"top_k cannot exceed configured maximum {self.config.retrieval.max_top_k}"
            )
        if not self.config.retrieval.lexical.enabled and not self.config.retrieval.semantic.enabled:
            raise SearchValidationError("both lexical and semantic retrieval are disabled")

        effective = self.project_context.resolve(request.project_id)
        project = effective.project
        product_id = (
            self.catalog.resolve(request.product).product_id
            if request.product is not None
            else None
        )
        version: str | None = None
        version_source: str | None = None
        if request.version is not None:
            version = normalize_version(request.version)
            version_source = "request"
        elif project is not None and product_id in project.bmc_products:
            version = project.bmc_products[product_id].version
            version_source = "project_configuration"

        project_id = project.id if project else None
        where, parameters = self._filters(request, project_id, product_id, version)
        reranker_runtime = self._resolve_reranker_runtime()
        if reranker_enabled is False:
            reranker_runtime = RerankerRuntime(
                enabled=False,
                candidates=reranker_runtime.candidates,
                backend=None,
            )
        reranker_candidate_limit = (
            min(reranker_runtime.candidates, RERANKER_MAX_CANDIDATES)
            if reranker_runtime.enabled and reranker_runtime.backend is not None
            else 0
        )
        candidate_limit = max(
            request.top_k * _DIVERSITY_CANDIDATE_MULTIPLIER,
            reranker_candidate_limit,
        )
        lexical_ids = (
            self._lexical_candidates(request.query, where, parameters, candidate_limit)
            if self.config.retrieval.lexical.enabled
            else []
        )
        semantic_ids = self._semantic_candidates(
            request,
            project_id,
            product_id,
            version,
            candidate_limit,
        )
        candidate_ids = list(dict.fromkeys([*lexical_ids, *semantic_ids]))
        rows = self._rows_by_ids(candidate_ids, where, parameters)
        rows_by_id = {row["chunk_id"]: row for row in rows}
        lexical_ids = [chunk_id for chunk_id in lexical_ids if chunk_id in rows_by_id]
        semantic_ids = [chunk_id for chunk_id in semantic_ids if chunk_id in rows_by_id]
        technical_terms = extract_technical_terms(request.query)
        exact_by_id = {
            chunk_id: matched_terms(technical_terms, self._result_haystack(row))
            for chunk_id, row in rows_by_id.items()
        }
        exact_ids = (
            {chunk_id for chunk_id, terms in exact_by_id.items() if terms}
            if self.config.retrieval.exact_match.enabled
            else set()
        )
        fused = fuse_rankings(
            lexical_ids,
            semantic_ids,
            exact_match_ids=exact_ids,
            rrf_k=self.config.retrieval.fusion.rrf_k,
        )
        reranked, reranked_ids = self._rerank_candidates(
            request.query,
            fused,
            rows_by_id,
            exact_by_id if self.config.retrieval.exact_match.enabled else {},
            top_k=request.top_k,
            runtime=reranker_runtime,
        )
        selected = self._diversify_candidates(reranked, rows_by_id, request.top_k)
        return SearchResponse(
            query=request.query,
            context=SearchContext(
                project_id=project_id,
                project_source=effective.source,
                product=product_id,
                version=version,
                version_source=version_source,
            ),
            results=[
                self._row_to_result(
                    rows_by_id[candidate.chunk_id],
                    rank,
                    exact_by_id[candidate.chunk_id],
                    lexical=candidate.lexical,
                    semantic=candidate.semantic,
                    reranked=candidate.chunk_id in reranked_ids,
                )
                for rank, candidate in enumerate(selected, start=1)
            ],
        )

    def _rerank_candidates(
        self,
        query: str,
        candidates: Sequence[FusedCandidate],
        rows_by_id: Mapping[str, Any],
        exact_by_id: Mapping[str, Sequence[str]],
        *,
        top_k: int,
        runtime: RerankerRuntime | None = None,
    ) -> tuple[list[FusedCandidate], set[str]]:
        """Rerank a bounded authorized prefix, failing open without sensitive logs."""

        original = list(candidates)
        current = runtime or RerankerRuntime(
            enabled=self.config.retrieval.reranker.enabled,
            candidates=self.config.retrieval.reranker.candidates,
            backend=self.reranker,
        )
        if not current.enabled or current.backend is None or not original:
            return original, set()
        # Every potentially returned result should be scored even when a user lowers
        # the configured candidate pool below the requested result count.
        candidate_limit = min(
            max(current.candidates, top_k),
            RERANKER_MAX_CANDIDATES,
            len(original),
        )
        payload = []
        for candidate in original[:candidate_limit]:
            row = rows_by_id[candidate.chunk_id]
            payload.append(
                RerankCandidate(
                    chunk_id=candidate.chunk_id,
                    title=str(row["title"]),
                    heading_path=tuple(json.loads(row["heading_path_json"] or "[]")),
                    text=str(row["text"]),
                )
            )
        try:
            scores = current.backend.rerank(query=query, candidates=payload)
            return apply_reranker_scores(
                original,
                scores,
                exact_terms_by_id=exact_by_id,
                candidate_limit=candidate_limit,
            )
        except Exception as exc:
            # Backend errors may contain request bodies, so log only the exception type.
            _LOGGER.warning(
                "optional reranker failed; preserving fused ranking (%s)",
                type(exc).__name__,
            )
            return original, set()

    def _resolve_reranker_runtime(self) -> RerankerRuntime:
        """Resolve one fail-open runtime snapshot for the complete search call."""

        settings = self.config.retrieval.reranker
        fallback = RerankerRuntime(
            enabled=settings.enabled,
            candidates=settings.candidates,
            backend=self.reranker,
        )
        if self.reranker_runtime_provider is None:
            return fallback
        try:
            runtime = self.reranker_runtime_provider()
            if (
                not isinstance(runtime.enabled, bool)
                or isinstance(runtime.candidates, bool)
                or not isinstance(runtime.candidates, int)
                or runtime.candidates < 1
            ):
                raise ValueError("invalid reranker runtime snapshot")
            return RerankerRuntime(
                enabled=runtime.enabled,
                candidates=min(runtime.candidates, RERANKER_MAX_CANDIDATES),
                backend=runtime.backend,
            )
        except Exception as exc:
            # Loading configuration or optional component state must not break the
            # standard search path. Do not log paths, config contents, or the query.
            _LOGGER.warning(
                "optional reranker configuration refresh failed; preserving standard ranking (%s)",
                type(exc).__name__,
            )
            return RerankerRuntime(
                enabled=False,
                candidates=settings.candidates,
                backend=None,
            )

    @staticmethod
    def _diversify_candidates(
        candidates: Sequence[FusedCandidate],
        rows_by_id: Mapping[str, Any],
        limit: int,
    ) -> list[FusedCandidate]:
        """Prefer distinct documents, then distinct sections, and finally backfill."""

        selected: list[FusedCandidate] = []
        deferred_documents: list[FusedCandidate] = []
        deferred_sections: list[FusedCandidate] = []
        documents: set[str] = set()
        sections: set[tuple[str, tuple[str, ...]]] = set()
        for candidate in candidates:
            row = rows_by_id[candidate.chunk_id]
            document_id = str(row["document_id"])
            heading_path = tuple(json.loads(row["heading_path_json"] or "[]"))
            section = (document_id, heading_path)
            if document_id in documents:
                deferred_documents.append(candidate)
                continue
            documents.add(document_id)
            sections.add(section)
            selected.append(candidate)
            if len(selected) == limit:
                return selected

        for candidate in deferred_documents:
            row = rows_by_id[candidate.chunk_id]
            section = (
                str(row["document_id"]),
                tuple(json.loads(row["heading_path_json"] or "[]")),
            )
            if section in sections:
                deferred_sections.append(candidate)
                continue
            sections.add(section)
            selected.append(candidate)
            if len(selected) == limit:
                return selected

        for candidate in deferred_sections:
            selected.append(candidate)
            if len(selected) == limit:
                return selected
        return selected

    def _lexical_candidates(
        self,
        query: str,
        where: str,
        parameters: list[object],
        limit: int,
    ) -> list[str]:
        sql = f"""
            SELECT c.chunk_id,
                   bm25(chunks_fts, 0.0, 3.0, 2.0, 1.0, 4.0) AS lexical_rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            JOIN documents d ON d.document_id = c.document_id
            WHERE chunks_fts MATCH ?
              AND c.active = 1
              AND d.status = 'indexed'
              AND {where}
            ORDER BY lexical_rank ASC, c.chunk_id ASC
            LIMIT ?
        """
        try:
            with self.database.connect() as connection:
                rows = connection.execute(
                    sql,
                    [build_fts_query(query), *parameters, limit],
                ).fetchall()
        except sqlite3.OperationalError as exc:
            raise SearchValidationError(f"lexical search failed: {exc}") from exc
        return [row["chunk_id"] for row in rows]

    def _semantic_candidates(
        self,
        request: SearchRequest,
        project_id: str | None,
        product_id: str | None,
        version: str | None,
        limit: int,
    ) -> list[str]:
        if not self.config.retrieval.semantic.enabled:
            return []
        if self.embedder is None or not self.vector_index.enabled:
            if self.config.retrieval.lexical.enabled:
                return []
            raise SearchValidationError("semantic retrieval is enabled but not configured")
        try:
            vectors = self.embedder.encode([request.query])
        except SemanticBackendError:
            if self.config.retrieval.lexical.enabled:
                return []
            raise
        if len(vectors) != 1:
            raise SearchValidationError("embedder did not return one query vector")
        try:
            candidates = self.vector_index.search(
                vectors[0],
                source_scope=request.source_scope,
                project_id=project_id,
                product_id=product_id,
                version=version,
                document_types=(
                    [item.value for item in request.document_types]
                    if request.document_types
                    else None
                ),
                limit=limit,
            )
        except SemanticBackendError:
            if self.config.retrieval.lexical.enabled:
                return []
            raise
        return [candidate.chunk_id for candidate in candidates]

    def _rows_by_ids(
        self,
        chunk_ids: list[str],
        where: str,
        parameters: list[object],
    ) -> list[sqlite3.Row]:
        if not chunk_ids:
            return []
        placeholders = ", ".join("?" for _ in chunk_ids)
        sql = f"""
            SELECT
                c.chunk_id,
                c.document_id,
                c.source_scope,
                c.project_id,
                c.document_type,
                c.heading_path_json,
                c.text,
                d.title,
                d.source_path,
                d.source_url,
                COALESCE((
                    SELECT group_concat(dp.product_id, '|')
                    FROM document_products dp
                    WHERE dp.document_id = d.document_id
                ), '') AS product_ids,
                COALESCE((
                    SELECT group_concat(pv.version, '|')
                    FROM document_product_versions dpv
                    JOIN product_versions pv
                      ON pv.product_version_id = dpv.product_version_id
                    WHERE dpv.document_id = d.document_id
                ), '') AS versions
            FROM chunks c
            JOIN documents d ON d.document_id = c.document_id
            WHERE c.chunk_id IN ({placeholders})
              AND c.active = 1
              AND d.status = 'indexed'
              AND {where}
        """
        with self.database.connect() as connection:
            return connection.execute(sql, [*chunk_ids, *parameters]).fetchall()

    def _filters(
        self,
        request: SearchRequest,
        project_id: str | None,
        product_id: str | None,
        version: str | None,
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if request.source_scope is SourceScope.BMC_OFFICIAL:
            clauses.append("c.source_scope = 'bmc_official'")
        elif request.source_scope is SourceScope.PROJECT:
            if project_id is None:
                raise SearchValidationError("source_scope=project requires an effective project")
            clauses.append("c.source_scope = 'project' AND c.project_id = ?")
            parameters.append(project_id)
        elif project_id is None:
            clauses.append("c.source_scope = 'bmc_official'")
        else:
            clauses.append("(c.source_scope = 'bmc_official' OR c.project_id = ?)")
            parameters.append(project_id)

        if product_id is not None and version is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM document_product_versions dpv "
                "JOIN product_versions pv ON pv.product_version_id = dpv.product_version_id "
                "WHERE dpv.document_id = d.document_id "
                "AND pv.product_id = ? AND pv.version = ?)"
            )
            parameters.extend((product_id, version))
        elif product_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM document_products dp "
                "WHERE dp.document_id = d.document_id AND dp.product_id = ?)"
            )
            parameters.append(product_id)
        elif version is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM document_product_versions dpv "
                "JOIN product_versions pv ON pv.product_version_id = dpv.product_version_id "
                "WHERE dpv.document_id = d.document_id AND pv.version = ?)"
            )
            parameters.append(version)
        if request.document_types:
            placeholders = ", ".join("?" for _ in request.document_types)
            clauses.append(f"c.document_type IN ({placeholders})")
            parameters.extend(item.value for item in request.document_types)
        return " AND ".join(f"({clause})" for clause in clauses), parameters

    @staticmethod
    def _result_haystack(row: sqlite3.Row) -> str:
        heading_path = json.loads(row["heading_path_json"] or "[]")
        return "\n".join([row["title"], *heading_path, row["text"]])

    @staticmethod
    def _row_to_result(
        row: sqlite3.Row,
        rank: int,
        exact_terms: list[str],
        *,
        lexical: bool,
        semantic: bool,
        reranked: bool = False,
    ) -> SearchResult:
        heading_path = json.loads(row["heading_path_json"] or "[]")
        return SearchResult(
            rank=rank,
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            source_scope=row["source_scope"],
            project_id=row["project_id"],
            title=row["title"],
            document_type=row["document_type"],
            product_ids=[value for value in row["product_ids"].split("|") if value],
            versions=list(dict.fromkeys(value for value in row["versions"].split("|") if value)),
            heading_path=heading_path,
            text=row["text"],
            source_path=row["source_path"],
            source_url=row["source_url"],
            match=MatchInfo(
                lexical=lexical,
                semantic=semantic,
                exact_terms=exact_terms,
                reranked=reranked,
            ),
        )

    def get_section(
        self,
        chunk_id: str,
        context_before: int = 1,
        context_after: int = 1,
        project_id: str | None = None,
    ) -> SectionResponse:
        if not 0 <= context_before <= 10 or not 0 <= context_after <= 10:
            raise SearchValidationError("section context must be between 0 and 10 chunks")
        effective = self.project_context.resolve(project_id)
        effective_project_id = effective.project.id if effective.project is not None else None
        scope_clause = "c.source_scope = 'bmc_official'"
        scope_parameters: tuple[object, ...] = ()
        if effective_project_id is not None:
            scope_clause = (
                "(c.source_scope = 'bmc_official' OR "
                "(c.source_scope = 'project' AND c.project_id = ?))"
            )
            scope_parameters = (effective_project_id,)
        with self.database.connect() as connection:
            selected = connection.execute(
                f"""
                SELECT c.document_id, c.position, d.title
                FROM chunks c
                JOIN documents d ON d.document_id = c.document_id
                WHERE c.chunk_id = ? AND c.active = 1 AND d.status = 'indexed'
                  AND {scope_clause}
                """,
                (chunk_id, *scope_parameters),
            ).fetchone()
            if selected is None:
                raise ChunkNotFoundError(f"active chunk not found: {chunk_id}")
            rows = connection.execute(
                """
                SELECT chunk_id, position, heading_path_json, text
                FROM chunks
                WHERE document_id = ? AND active = 1 AND position BETWEEN ? AND ?
                ORDER BY position
                """,
                (
                    selected["document_id"],
                    selected["position"] - context_before,
                    selected["position"] + context_after,
                ),
            ).fetchall()
        return SectionResponse(
            document_id=selected["document_id"],
            title=selected["title"],
            selected_chunk_id=chunk_id,
            chunks=[
                SectionChunk(
                    chunk_id=row["chunk_id"],
                    position=row["position"],
                    heading_path=json.loads(row["heading_path_json"] or "[]"),
                    text=row["text"],
                )
                for row in rows
            ],
        )

    def list_products(self) -> ProductListResponse:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT p.product_id
                FROM products p
                JOIN document_products dp ON dp.product_id = p.product_id
                JOIN documents d ON d.document_id = dp.document_id AND d.status = 'indexed'
                JOIN chunks c ON c.document_id = d.document_id AND c.active = 1
                WHERE p.active = 1
                """
            ).fetchall()
        indexed_product_ids = {row["product_id"] for row in rows}
        configured_product_ids = {
            self.catalog.resolve(product).product_id
            for product in self.config.official_docs.products
        }
        products = sorted(
            (
                self.catalog.get(product_id)
                for product_id in indexed_product_ids | configured_product_ids
            ),
            key=lambda product: product.name,
        )
        return ProductListResponse(
            products=[
                ProductSummary(
                    product_id=product.product_id,
                    name=product.name,
                    aliases=product.aliases,
                    configured=product.product_id in configured_product_ids,
                    indexed=product.product_id in indexed_product_ids,
                )
                for product in products
            ]
        )

    def list_versions(self, product: str) -> VersionListResponse:
        product_id = self.catalog.resolve(product).product_id
        indexed_versions = self.indexed_versions().get(product_id, set())
        configured = self.config.official_docs.products.get(product_id)
        configured_versions = (
            {normalize_version(version) for version in configured.versions}
            if configured is not None
            else set()
        )
        versions = sorted(
            indexed_versions | configured_versions,
            key=version_sort_key,
            reverse=True,
        )
        return VersionListResponse(
            product=product_id,
            versions=[
                VersionInfo(version=version, indexed=version in indexed_versions)
                for version in versions
            ],
        )

    def indexed_versions(self) -> dict[str, set[str]]:
        """Return every indexed product/version pair in one bounded query."""

        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT pv.product_id, pv.version
                FROM product_versions pv
                WHERE EXISTS (
                    SELECT 1
                    FROM document_product_versions dpv
                    JOIN documents d ON d.document_id = dpv.document_id
                    WHERE dpv.product_version_id = pv.product_version_id
                      AND d.status = 'indexed'
                      AND EXISTS (
                          SELECT 1
                          FROM chunks c
                          WHERE c.document_id = d.document_id AND c.active = 1
                      )
                )
                ORDER BY pv.product_id, pv.version DESC
                """
            ).fetchall()
        indexed: dict[str, set[str]] = {}
        for row in rows:
            indexed.setdefault(row["product_id"], set()).add(row["version"])
        return indexed

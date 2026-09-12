"""Optional dense-vector index adapters; SQLite remains authoritative."""

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import SemanticBackendError
from ..models.source import SourceScope


@dataclass(frozen=True, slots=True)
class VectorRecord:
    chunk_id: str
    vector: list[float]
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    chunk_id: str
    rank: int


class VectorIndex(Protocol):
    enabled: bool

    def upsert(self, records: list[VectorRecord]) -> None: ...

    def delete(self, chunk_ids: list[str]) -> None: ...

    def search(
        self,
        vector: list[float],
        *,
        source_scope: SourceScope,
        project_id: str | None,
        product_id: str | None,
        version: str | None,
        document_types: list[str] | None,
        limit: int,
    ) -> list[SemanticCandidate]: ...


class NullVectorIndex:
    enabled = False

    def upsert(self, records: list[VectorRecord]) -> None:
        if records:
            raise SemanticBackendError("vector indexing is disabled")

    def delete(self, chunk_ids: list[str]) -> None:
        return None

    def search(
        self,
        vector: list[float],
        *,
        source_scope: SourceScope,
        project_id: str | None,
        product_id: str | None,
        version: str | None,
        document_types: list[str] | None,
        limit: int,
    ) -> list[SemanticCandidate]:
        return []


def _point_id(chunk_id: str) -> str:
    """Map chk_<uuid> identifiers to UUIDs accepted by Qdrant point IDs."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"helix-mcp-knowledge:{chunk_id}"))


class QdrantVectorIndex:
    enabled = True

    def __init__(
        self,
        *,
        url: str,
        collection: str,
        dimension: int,
        api_key_env: str,
        path: str | Path | None = None,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
        except ImportError as exc:
            raise SemanticBackendError(
                "semantic dependencies are missing; install the optional semantic component "
                "from the dashboard"
            ) from exc
        self._models = __import__("qdrant_client.models", fromlist=["models"])
        if path is not None:
            self.client = QdrantClient(path=str(path))
        elif url == ":memory:":
            self.client = QdrantClient(location=":memory:")
        else:
            self.client = QdrantClient(url=url, api_key=os.environ.get(api_key_env) or None)
        self.collection = collection
        self.dimension = dimension
        if not self.client.collection_exists(collection):
            self.client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
            )

    def reset(self) -> None:
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=self._models.VectorParams(
                    size=self.dimension,
                    distance=self._models.Distance.COSINE,
                ),
            )
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=self._models.FilterSelector(
                filter=self._models.Filter(must=[]),
            ),
            wait=True,
        )

    def chunk_ids(self) -> set[str]:
        chunk_ids: set[str] = set()
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection,
                offset=offset,
                limit=512,
                with_payload=["chunk_id"],
                with_vectors=False,
            )
            for point in points:
                if point.payload and point.payload.get("chunk_id"):
                    chunk_ids.add(str(point.payload["chunk_id"]))
            if offset is None:
                return chunk_ids

    def upsert(self, records: list[VectorRecord]) -> None:
        if not records:
            return
        points = [
            self._models.PointStruct(
                id=_point_id(record.chunk_id),
                vector=record.vector,
                payload={**record.payload, "chunk_id": record.chunk_id},
            )
            for record in records
        ]
        self.client.upsert(collection_name=self.collection, points=points, wait=True)

    def delete(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=self._models.PointIdsList(
                points=[_point_id(chunk_id) for chunk_id in chunk_ids]
            ),
            wait=True,
        )

    def search(
        self,
        vector: list[float],
        *,
        source_scope: SourceScope,
        project_id: str | None,
        product_id: str | None,
        version: str | None,
        document_types: list[str] | None,
        limit: int,
    ) -> list[SemanticCandidate]:
        conditions = [
            self._models.FieldCondition(key="active", match=self._models.MatchValue(value=True))
        ]
        if source_scope is SourceScope.BMC_OFFICIAL or (
            source_scope is SourceScope.ALL_RELEVANT and project_id is None
        ):
            conditions.append(
                self._models.FieldCondition(
                    key="source_scope",
                    match=self._models.MatchValue(value="bmc_official"),
                )
            )
            query_filter = self._models.Filter(must=conditions)
        elif source_scope is SourceScope.PROJECT:
            conditions.extend(
                [
                    self._models.FieldCondition(
                        key="source_scope",
                        match=self._models.MatchValue(value="project"),
                    ),
                    self._models.FieldCondition(
                        key="project_id", match=self._models.MatchValue(value=project_id)
                    ),
                ]
            )
            query_filter = self._models.Filter(must=conditions)
        else:
            official = self._models.Filter(
                must=[
                    self._models.FieldCondition(
                        key="source_scope",
                        match=self._models.MatchValue(value="bmc_official"),
                    )
                ]
            )
            project = self._models.Filter(
                must=[
                    self._models.FieldCondition(
                        key="source_scope",
                        match=self._models.MatchValue(value="project"),
                    ),
                    self._models.FieldCondition(
                        key="project_id", match=self._models.MatchValue(value=project_id)
                    ),
                ]
            )
            query_filter = self._models.Filter(must=conditions, should=[official, project])
        if product_id is not None and version is not None:
            query_filter.must.append(
                self._models.FieldCondition(
                    key="product_version_pairs",
                    match=self._models.MatchValue(value=f"{product_id}:{version}"),
                )
            )
        elif product_id is not None:
            query_filter.must.append(
                self._models.FieldCondition(
                    key="product_ids", match=self._models.MatchValue(value=product_id)
                )
            )
        elif version is not None:
            query_filter.must.append(
                self._models.FieldCondition(
                    key="product_versions", match=self._models.MatchValue(value=version)
                )
            )
        if document_types:
            query_filter.must.append(
                self._models.FieldCondition(
                    key="document_type",
                    match=self._models.MatchAny(any=document_types),
                )
            )
        points = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=query_filter,
            with_payload=True,
            with_vectors=False,
            limit=limit,
        ).points
        return [
            SemanticCandidate(chunk_id=str(point.payload["chunk_id"]), rank=rank)
            for rank, point in enumerate(points, start=1)
            if point.payload and point.payload.get("chunk_id")
        ]

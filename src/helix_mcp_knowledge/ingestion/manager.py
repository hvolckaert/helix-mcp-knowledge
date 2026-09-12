"""End-to-end local ingestion with idempotency and vector compensation."""

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ..catalog.versions import normalize_version
from ..errors import IngestionError
from ..models.ingestion import IngestRequest, IngestResult
from ..models.project import Classification
from ..models.source import SourceScope, SourceType
from ..projects.registry import ProjectRegistry
from ..retrieval.embedder import Embedder
from ..storage.documents import DocumentStore, PreparedChunk, PreparedDocument
from ..storage.vector_cleanup import VectorCleanupStore, drain_vector_cleanup
from ..storage.vectors import NullVectorIndex, VectorIndex, VectorRecord
from .chunker import Chunker
from .fingerprint import fingerprint_file, fingerprint_parsed
from .parsers.registry import ParserRegistry

SOURCE_TYPES = {
    ".pdf": SourceType.LOCAL_PDF,
    ".docx": SourceType.LOCAL_DOCX,
    ".md": SourceType.LOCAL_MARKDOWN,
    ".html": SourceType.LOCAL_HTML,
    ".htm": SourceType.LOCAL_HTML,
    ".txt": SourceType.LOCAL_TEXT,
}
LOGGER = logging.getLogger(__name__)


class IngestionManager:
    def __init__(
        self,
        *,
        parser_registry: ParserRegistry,
        chunker: Chunker,
        document_store: DocumentStore,
        project_registry: ProjectRegistry,
        official_sources_root: Path,
        errors_path: Path,
        catalog,
        embedder: Embedder | None = None,
        vector_index: VectorIndex | None = None,
        processing_profile: dict[str, object] | None = None,
    ) -> None:
        self.parsers = parser_registry
        self.chunker = chunker
        self.store = document_store
        self.projects = project_registry
        self.official_sources_root = official_sources_root.resolve()
        self.errors_path = errors_path.resolve()
        self.catalog = catalog
        self.embedder = embedder
        self.vector_index = vector_index or NullVectorIndex()
        self.processing_profile = processing_profile or {}

    def ingest(self, request: IngestRequest) -> IngestResult:
        requested_source = request.source_path.expanduser()
        source = requested_source.resolve()
        source_key = self._source_key(
            request.source_scope, request.project_id, source, request.source_url
        )
        existing = None
        try:
            project, classification = self._validate_scope(request, source)
            self.parsers.validate(requested_source)
            product_versions = self._resolve_products(request, project)
            ingestion_signature = self._ingestion_signature(
                request=request,
                classification=classification,
                product_versions=product_versions,
                processing_profile=self.processing_profile,
            )
            existing = self.store.find_by_source_key(source_key)
            existing_metadata = (
                json.loads(existing["metadata_json"] or "{}") if existing is not None else {}
            )
            if request.source_url is None:
                content_hash = fingerprint_file(source)
                if self._is_unchanged(
                    existing, existing_metadata, content_hash, ingestion_signature
                ):
                    return self._unchanged_result(existing, source, content_hash)

            parsed = self.parsers.parse(
                requested_source,
                allow_ocr=request.source_scope is SourceScope.PROJECT,
            )
            if request.title:
                parsed = type(parsed)(
                    title=request.title,
                    blocks=parsed.blocks,
                    metadata=parsed.metadata,
                )
            drafts = self.chunker.chunk(parsed)
            if not drafts:
                raise IngestionError(f"document produced no indexable chunks: {source}")
            if request.source_url is not None:
                content_hash = fingerprint_parsed(parsed)
                if self._is_unchanged(
                    existing, existing_metadata, content_hash, ingestion_signature
                ):
                    return self._unchanged_result(existing, source, content_hash)
            metadata = {**parsed.metadata, **request.metadata}
            metadata["_ingestion_signature"] = ingestion_signature
            document_id = existing["document_id"] if existing is not None else f"doc_{uuid.uuid4()}"
            old_chunk_ids = self.store.chunk_ids(document_id) if existing is not None else []
            chunks = [
                PreparedChunk(chunk_id=f"chk_{uuid.uuid4()}", draft=draft) for draft in drafts
            ]
            prepared = PreparedDocument(
                document_id=document_id,
                source_key=source_key,
                source_scope=request.source_scope,
                project_id=request.project_id,
                title=parsed.title,
                document_type=request.document_type,
                source_type=request.source_type or SOURCE_TYPES[source.suffix.casefold()],
                source_path=str(source),
                source_url=request.source_url,
                language=request.language.casefold(),
                classification=classification,
                content_hash=content_hash,
                file_size=source.stat().st_size,
                etag=request.etag,
                last_modified=request.last_modified,
                metadata=metadata,
                product_versions=product_versions,
                chunks=chunks,
            )
            vector_records = self._vector_records(prepared)
            if vector_records:
                self.vector_index.upsert(vector_records)
            try:
                self.store.replace(
                    prepared,
                    queue_old_vectors=bool(old_chunk_ids and self.vector_index.enabled),
                )
            except Exception:
                if vector_records:
                    new_chunk_ids = [record.chunk_id for record in vector_records]
                    try:
                        self.vector_index.delete(new_chunk_ids)
                    except Exception:
                        VectorCleanupStore(self.store.database).queue(
                            new_chunk_ids,
                            document_id=None,
                            reason="failed_ingestion_compensation",
                        )
                raise
            if old_chunk_ids and self.vector_index.enabled:
                cleanup = drain_vector_cleanup(self.store.database, self.vector_index)
                if cleanup.status == "error":
                    LOGGER.warning(
                        "document %s was refreshed with %s stale vectors pending cleanup",
                        document_id,
                        cleanup.pending,
                    )
            return IngestResult(
                document_id=document_id,
                status="indexed",
                source_path=str(source),
                content_hash=content_hash,
                chunks_indexed=len(chunks),
                vectors_indexed=len(vector_records),
            )
        except Exception as exc:
            # A failed refresh must not hide the last successfully indexed
            # revision. New documents still become errors when a pending row
            # exists, while established evidence remains queryable.
            if existing is None or existing["status"] != "indexed":
                self.store.mark_error(source_key)
            expected_empty_official_page = (
                request.source_scope is SourceScope.BMC_OFFICIAL
                and isinstance(exc, IngestionError)
                and str(exc).startswith("document produced no indexable chunks:")
            )
            if not expected_empty_official_page:
                self._write_error(source, exc)
            if isinstance(exc, IngestionError):
                raise
            raise IngestionError(f"ingestion failed for {source}: {exc}") from exc

    def _validate_scope(
        self, request: IngestRequest, source: Path
    ) -> tuple[object | None, Classification]:
        if request.source_scope is SourceScope.PROJECT:
            project = self.projects.require(request.project_id or "")
            self._require_inside(source, project.documents_path)
            return project, request.classification or project.classification
        self._require_inside(source, self.official_sources_root)
        return None, request.classification or Classification.PUBLIC

    @staticmethod
    def _require_inside(source: Path, root: Path) -> None:
        try:
            source.relative_to(root.resolve())
        except ValueError as exc:
            raise IngestionError(f"source must be inside configured root {root}: {source}") from exc

    @staticmethod
    def _source_key(
        scope: SourceScope,
        project_id: str | None,
        source: Path,
        source_url: str | None = None,
    ) -> str:
        identity = source_url or str(source)
        return f"{scope.value}:{project_id or '-'}:{identity}"

    def _resolve_products(self, request: IngestRequest, project) -> dict[str, str | None]:
        resolved: dict[str, str | None] = {}
        for product, configured_version in request.product_versions.items():
            product_id = self.catalog.resolve(product).product_id
            version = configured_version
            if version is None and project is not None and product_id in project.bmc_products:
                version = project.bmc_products[product_id].version
            resolved[product_id] = normalize_version(version) if version else None
        return resolved

    def _vector_records(self, document: PreparedDocument) -> list[VectorRecord]:
        if not self.vector_index.enabled:
            return []
        if self.embedder is None:
            raise IngestionError("semantic vector index is enabled but no embedder is configured")
        vectors = self.embedder.encode([chunk.draft.embedding_text for chunk in document.chunks])
        if len(vectors) != len(document.chunks):
            raise IngestionError("embedder returned an unexpected number of vectors")
        payload = {
            "document_id": document.document_id,
            "source_scope": document.source_scope.value,
            "project_id": document.project_id,
            "document_type": document.document_type.value,
            "product_ids": list(document.product_versions),
            "product_versions": [
                value for value in document.product_versions.values() if value is not None
            ],
            "product_version_pairs": [
                f"{product_id}:{version}"
                for product_id, version in document.product_versions.items()
                if version is not None
            ],
            "classification": document.classification.value,
            "language": document.language,
            "active": True,
        }
        return [
            VectorRecord(chunk_id=chunk.chunk_id, vector=vector, payload=payload)
            for chunk, vector in zip(document.chunks, vectors, strict=True)
        ]

    @staticmethod
    def _ingestion_signature(
        *,
        request: IngestRequest,
        classification: Classification,
        product_versions: dict[str, str | None],
        processing_profile: dict[str, object],
    ) -> str:
        payload = {
            "signature_version": 1,
            "title": request.title,
            "document_type": request.document_type.value,
            "source_type": (
                request.source_type or SOURCE_TYPES[request.source_path.suffix.casefold()]
            ).value,
            "language": request.language.casefold(),
            "classification": classification.value,
            "product_versions": product_versions,
            "metadata": request.metadata,
            "processing_profile": (
                processing_profile
                if request.source_scope is SourceScope.PROJECT
                and request.source_path.suffix.casefold() == ".pdf"
                else {}
            ),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _is_unchanged(
        existing,
        existing_metadata: dict[str, object],
        content_hash: str,
        ingestion_signature: str,
    ) -> bool:
        return bool(
            existing is not None
            and existing["status"] == "indexed"
            and existing["content_hash"] == content_hash
            and existing_metadata.get("_ingestion_signature") == ingestion_signature
        )

    def _unchanged_result(self, existing, source: Path, content_hash: str) -> IngestResult:
        return IngestResult(
            document_id=existing["document_id"],
            status="unchanged",
            source_path=str(source),
            content_hash=content_hash,
            chunks_indexed=len(self.store.chunk_ids(existing["document_id"])),
            vectors_indexed=0,
        )

    def _write_error(self, source: Path, error: Exception) -> None:
        self.errors_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.errors_path / f"ingestion-{timestamp}.json"
        path.write_text(
            json.dumps(
                {
                    "source": str(source),
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            path.chmod(0o600)

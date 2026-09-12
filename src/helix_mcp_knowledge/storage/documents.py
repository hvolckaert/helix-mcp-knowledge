"""Atomic document/chunk replacement in authoritative SQLite storage."""

import json
import sqlite3
from dataclasses import dataclass

from ..ingestion.chunker import ChunkDraft
from ..models.document import DocumentStatus, DocumentType
from ..models.project import Classification
from ..models.source import SourceScope, SourceType
from .database import Database
from .vector_cleanup import queue_vector_cleanup


@dataclass(frozen=True, slots=True)
class PreparedChunk:
    chunk_id: str
    draft: ChunkDraft


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    document_id: str
    source_key: str
    source_scope: SourceScope
    project_id: str | None
    title: str
    document_type: DocumentType
    source_type: SourceType
    source_path: str
    source_url: str | None
    language: str
    classification: Classification
    content_hash: str
    file_size: int
    etag: str | None
    last_modified: str | None
    metadata: dict[str, object]
    product_versions: dict[str, str | None]
    chunks: list[PreparedChunk]


class DocumentStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def find_by_source_key(self, source_key: str) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            return connection.execute(
                """
                SELECT document_id, content_hash, status, metadata_json
                FROM documents
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()

    def managed_project_documents(
        self, project_id: str, manifest_id: str
    ) -> dict[str, tuple[str, str]]:
        """Return source path -> (document ID, status) for one project manifest."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT document_id, source_path, status
                FROM documents
                WHERE source_scope = 'project'
                  AND project_id = ?
                  AND json_extract(metadata_json, '$._project_sync.manifest_id') = ?
                  AND source_path IS NOT NULL
                """,
                (project_id, manifest_id),
            ).fetchall()
        return {row["source_path"]: (row["document_id"], row["status"]) for row in rows}

    def mark_missing(self, document_id: str, *, queue_vectors: bool = False) -> list[str]:
        """Hide a missing document while retaining chunk IDs for vector cleanup retries."""
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                chunk_ids = [
                    row["chunk_id"]
                    for row in connection.execute(
                        "SELECT chunk_id FROM chunks WHERE document_id = ?",
                        (document_id,),
                    ).fetchall()
                ]
                if chunk_ids:
                    placeholders = ", ".join("?" for _ in chunk_ids)
                    connection.execute(
                        f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})",
                        chunk_ids,
                    )
                    if queue_vectors:
                        queue_vector_cleanup(
                            connection,
                            chunk_ids,
                            document_id=document_id,
                            reason="project_document_missing",
                        )
                connection.execute(
                    "UPDATE chunks SET active = 0 WHERE document_id = ?", (document_id,)
                )
                connection.execute(
                    "UPDATE documents SET status = 'missing' WHERE document_id = ?",
                    (document_id,),
                )
                connection.commit()
                return chunk_ids
            except Exception:
                connection.rollback()
                raise

    def chunk_ids(self, document_id: str) -> list[str]:
        with self.database.connect() as connection:
            return [
                row["chunk_id"]
                for row in connection.execute(
                    "SELECT chunk_id FROM chunks WHERE document_id = ?",
                    (document_id,),
                ).fetchall()
            ]

    def remove_project_index(self, project_id: str, *, queue_vectors: bool = False) -> list[str]:
        """Remove one project's derived index while leaving its source files untouched."""

        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                chunk_ids = [
                    row["chunk_id"]
                    for row in connection.execute(
                        "SELECT chunk_id FROM chunks WHERE project_id = ?",
                        (project_id,),
                    ).fetchall()
                ]
                if chunk_ids:
                    placeholders = ", ".join("?" for _ in chunk_ids)
                    connection.execute(
                        f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})",
                        chunk_ids,
                    )
                    if queue_vectors:
                        queue_vector_cleanup(
                            connection,
                            chunk_ids,
                            document_id=None,
                            reason=f"project_removed:{project_id}",
                        )
                connection.execute(
                    "DELETE FROM documents WHERE project_id = ?",
                    (project_id,),
                )
                connection.execute(
                    "DELETE FROM automation_state WHERE job_id = ?",
                    (f"project:{project_id}",),
                )
                connection.execute(
                    "DELETE FROM projects WHERE project_id = ?",
                    (project_id,),
                )
                connection.commit()
                return chunk_ids
            except Exception:
                connection.rollback()
                raise

    def replace(self, document: PreparedDocument, *, queue_old_vectors: bool = False) -> None:
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                old_chunk_ids = [
                    row["chunk_id"]
                    for row in connection.execute(
                        "SELECT chunk_id FROM chunks WHERE document_id = ?",
                        (document.document_id,),
                    ).fetchall()
                ]
                if old_chunk_ids:
                    placeholders = ", ".join("?" for _ in old_chunk_ids)
                    connection.execute(
                        f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})",
                        old_chunk_ids,
                    )
                    if queue_old_vectors:
                        queue_vector_cleanup(
                            connection,
                            old_chunk_ids,
                            document_id=None,
                            reason=f"document_replaced:{document.document_id}",
                        )
                connection.execute(
                    "DELETE FROM chunks WHERE document_id = ?", (document.document_id,)
                )
                connection.execute(
                    "DELETE FROM document_products WHERE document_id = ?",
                    (document.document_id,),
                )
                connection.execute(
                    "DELETE FROM document_product_versions WHERE document_id = ?",
                    (document.document_id,),
                )
                self._upsert_document(connection, document)
                self._insert_relationships(connection, document)
                self._insert_chunks(connection, document)
                connection.execute(
                    """
                    UPDATE documents
                    SET status = ?, indexed_at = datetime('now')
                    WHERE document_id = ?
                    """,
                    (DocumentStatus.INDEXED.value, document.document_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def purge_missing(self, document_id: str) -> bool:
        """Delete a retired document after any external vector cleanup has completed."""

        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM documents WHERE document_id = ? AND status = 'missing'",
                (document_id,),
            )
            connection.commit()
            return cursor.rowcount == 1

    def mark_error(self, source_key: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE documents SET status = 'error' WHERE source_key = ?",
                (source_key,),
            )
            connection.commit()

    @staticmethod
    def _upsert_document(connection: sqlite3.Connection, document: PreparedDocument) -> None:
        connection.execute(
            """
            INSERT INTO documents(
                document_id, source_key, source_scope, project_id, title,
                document_type, source_type, source_path, source_url, language,
                classification, content_hash, file_size, etag, last_modified,
                retrieved_at, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                source_key=excluded.source_key,
                source_scope=excluded.source_scope,
                project_id=excluded.project_id,
                title=excluded.title,
                document_type=excluded.document_type,
                source_type=excluded.source_type,
                source_path=excluded.source_path,
                source_url=excluded.source_url,
                language=excluded.language,
                classification=excluded.classification,
                content_hash=excluded.content_hash,
                file_size=excluded.file_size,
                etag=excluded.etag,
                last_modified=excluded.last_modified,
                retrieved_at=excluded.retrieved_at,
                status=excluded.status,
                metadata_json=excluded.metadata_json
            """,
            (
                document.document_id,
                document.source_key,
                document.source_scope.value,
                document.project_id,
                document.title,
                document.document_type.value,
                document.source_type.value,
                document.source_path,
                document.source_url,
                document.language,
                document.classification.value,
                document.content_hash,
                document.file_size,
                document.etag,
                document.last_modified,
                DocumentStatus.PROCESSING.value,
                json.dumps(document.metadata, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _insert_relationships(connection: sqlite3.Connection, document: PreparedDocument) -> None:
        for product_id, version in document.product_versions.items():
            connection.execute(
                "INSERT INTO document_products(document_id, product_id) VALUES (?, ?)",
                (document.document_id, product_id),
            )
            if version is None:
                continue
            product_version_id = f"{product_id}:{version}"
            connection.execute(
                """
                INSERT INTO product_versions(product_version_id, product_id, version)
                VALUES (?, ?, ?)
                ON CONFLICT(product_id, version) DO NOTHING
                """,
                (product_version_id, product_id, version),
            )
            connection.execute(
                """
                INSERT INTO document_product_versions(document_id, product_version_id)
                VALUES (?, ?)
                """,
                (document.document_id, product_version_id),
            )

    @staticmethod
    def _insert_chunks(connection: sqlite3.Connection, document: PreparedDocument) -> None:
        for item in document.chunks:
            headings = item.draft.heading_path
            connection.execute(
                """
                INSERT INTO chunks(
                    chunk_id, document_id, source_scope, project_id, document_type,
                    heading_1, heading_2, heading_3, heading_path_json, chunk_type,
                    text, embedding_text, position, token_count, content_hash, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    item.chunk_id,
                    document.document_id,
                    document.source_scope.value,
                    document.project_id,
                    document.document_type.value,
                    headings[0] if len(headings) > 0 else None,
                    headings[1] if len(headings) > 1 else None,
                    headings[2] if len(headings) > 2 else None,
                    json.dumps(headings, ensure_ascii=False),
                    item.draft.chunk_type.value,
                    item.draft.text,
                    item.draft.embedding_text,
                    item.draft.position,
                    item.draft.token_count,
                    item.draft.content_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO chunks_fts(
                    chunk_id, title, heading_path, text, technical_terms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    item.chunk_id,
                    document.title,
                    " > ".join(headings),
                    item.draft.text,
                    " ".join(item.draft.technical_terms),
                ),
            )

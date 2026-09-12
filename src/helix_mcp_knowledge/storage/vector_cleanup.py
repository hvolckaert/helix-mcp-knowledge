"""Durable cleanup journal for vectors derived from retired SQLite chunks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from .database import Database
from .vectors import VectorIndex

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VectorCleanupResult:
    status: str
    processed: int = 0
    pending: int = 0
    error: str | None = None


def queue_vector_cleanup(
    connection,
    chunk_ids: list[str],
    *,
    document_id: str | None,
    reason: str,
) -> None:
    if not chunk_ids:
        return
    queued_at = datetime.now(UTC).isoformat()
    connection.executemany(
        """
        INSERT INTO vector_cleanup_queue(chunk_id, document_id, reason, queued_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chunk_id) DO UPDATE SET
            document_id=COALESCE(excluded.document_id, vector_cleanup_queue.document_id),
            reason=excluded.reason
        """,
        [(chunk_id, document_id, reason, queued_at) for chunk_id in chunk_ids],
    )


class VectorCleanupStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def pending(self, *, limit: int = 10_000) -> list[tuple[str, str | None]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT chunk_id, document_id
                FROM vector_cleanup_queue
                ORDER BY queued_at, chunk_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [(str(row["chunk_id"]), row["document_id"]) for row in rows]

    def queue(
        self,
        chunk_ids: list[str],
        *,
        document_id: str | None,
        reason: str,
    ) -> None:
        if not chunk_ids:
            return
        with self.database.connect() as connection:
            queue_vector_cleanup(
                connection,
                chunk_ids,
                document_id=document_id,
                reason=reason,
            )
            connection.commit()

    def complete(self, chunk_ids: list[str]) -> set[str]:
        if not chunk_ids:
            return set()
        placeholders = ", ".join("?" for _ in chunk_ids)
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                document_ids = {
                    str(row["document_id"])
                    for row in connection.execute(
                        f"SELECT document_id FROM vector_cleanup_queue "
                        f"WHERE chunk_id IN ({placeholders}) AND document_id IS NOT NULL",
                        chunk_ids,
                    ).fetchall()
                }
                connection.execute(
                    f"DELETE FROM vector_cleanup_queue WHERE chunk_id IN ({placeholders})",
                    chunk_ids,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return document_ids

    def has_pending_for_document(self, document_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM vector_cleanup_queue WHERE document_id = ? LIMIT 1",
                (document_id,),
            ).fetchone()
        return row is not None

    def count(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute("SELECT count(*) FROM vector_cleanup_queue").fetchone()
        return int(row[0])


def drain_vector_cleanup(
    database: Database,
    vector_index: VectorIndex,
    *,
    limit: int = 10_000,
) -> VectorCleanupResult:
    store = VectorCleanupStore(database)
    pending = store.pending(limit=limit)
    if not pending:
        return VectorCleanupResult(status="completed")
    if not vector_index.enabled:
        return VectorCleanupResult(status="deferred", pending=store.count())
    chunk_ids = [chunk_id for chunk_id, _ in pending]
    try:
        vector_index.delete(chunk_ids)
        document_ids = store.complete(chunk_ids)
        _purge_retired_documents(database, document_ids, store)
    except Exception as exc:
        LOGGER.warning("derived vector cleanup remains pending: %s", exc)
        return VectorCleanupResult(status="error", pending=store.count(), error=str(exc))
    return VectorCleanupResult(
        status="completed",
        processed=len(chunk_ids),
        pending=store.count(),
    )


def _purge_retired_documents(
    database: Database,
    document_ids: set[str],
    cleanup_store: VectorCleanupStore,
) -> None:
    ready = [
        document_id
        for document_id in document_ids
        if not cleanup_store.has_pending_for_document(document_id)
    ]
    if not ready:
        return
    placeholders = ", ".join("?" for _ in ready)
    with database.connect() as connection:
        connection.execute(
            f"DELETE FROM documents WHERE status = 'missing' AND document_id IN ({placeholders})",
            ready,
        )
        connection.commit()

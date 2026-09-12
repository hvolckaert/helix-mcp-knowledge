"""Persistence for declared sources and their conditional HTTP state."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from ..models.source import SourceType
from ..sync.manifest import OfficialSourceDefinition
from .database import Database


@dataclass(frozen=True)
class MissingCollectionCleanup:
    chunk_ids: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()


class SourceStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, source: OfficialSourceDefinition, source_path: str) -> None:
        metadata = {
            "title": source.title,
            "product": source.product,
            "version": source.version,
            "document_type": source.document_type.value,
            "language": source.language,
            **source.metadata,
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sources(
                    source_id, source_type, source_path, source_url, enabled, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_type=excluded.source_type,
                    source_path=excluded.source_path,
                    source_url=excluded.source_url,
                    enabled=excluded.enabled,
                    metadata_json=excluded.metadata_json
                """,
                (
                    source.source_id,
                    SourceType.BMC_PUBLIC_URL.value,
                    source_path,
                    source.url,
                    int(source.enabled),
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            connection.commit()

    def state(self, source_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM sync_state WHERE source_id = ?", (source_id,)
            ).fetchone()
        if row is None:
            return {}
        value = json.loads(row["state_json"])
        return value if isinstance(value, dict) else {}

    def update_state(self, source_id: str, state: dict[str, object]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sync_state(source_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    json.dumps(state, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()

    def mark_collection_missing(
        self, collection_id: str, seen_source_ids: set[str]
    ) -> MissingCollectionCleanup:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id, source_url, source_path
                FROM sources
                WHERE json_extract(metadata_json, '$.collection_id') = ?
                  AND enabled = 1
                """,
                (collection_id,),
            ).fetchall()
            missing = [row for row in rows if row["source_id"] not in seen_source_ids]
            if not missing:
                return MissingCollectionCleanup()
            source_ids = [row["source_id"] for row in missing]
            urls = [row["source_url"] for row in missing if row["source_url"]]
            source_paths = [str(row["source_path"]) for row in missing if row["source_path"]]
            source_placeholders = ", ".join("?" for _ in source_ids)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"UPDATE sources SET enabled = 0 WHERE source_id IN ({source_placeholders})",
                    source_ids,
                )
                chunk_ids: list[str] = []
                if urls:
                    url_placeholders = ", ".join("?" for _ in urls)
                    chunk_ids = [
                        str(row["chunk_id"])
                        for row in connection.execute(
                            f"SELECT c.chunk_id FROM chunks c "
                            f"JOIN documents d ON d.document_id = c.document_id "
                            f"WHERE d.source_url IN ({url_placeholders})",
                            urls,
                        ).fetchall()
                    ]
                    if chunk_ids:
                        chunk_placeholders = ", ".join("?" for _ in chunk_ids)
                        connection.execute(
                            f"DELETE FROM chunks_fts WHERE chunk_id IN ({chunk_placeholders})",
                            chunk_ids,
                        )
                        connection.execute(
                            f"DELETE FROM chunks WHERE chunk_id IN ({chunk_placeholders})",
                            chunk_ids,
                        )
                    connection.execute(
                        f"UPDATE documents SET status = 'missing' "
                        f"WHERE source_url IN ({url_placeholders})",
                        urls,
                    )
                state_row = connection.execute(
                    "SELECT state_json FROM collection_sync_state WHERE collection_id = ?",
                    (collection_id,),
                ).fetchone()
                try:
                    state = json.loads(state_row["state_json"]) if state_row is not None else {}
                except (TypeError, json.JSONDecodeError):
                    state = {}
                if not isinstance(state, dict):
                    state = {}
                state["vector_cleanup_pending"] = sorted(
                    {
                        *self._string_list(state.get("vector_cleanup_pending")),
                        *chunk_ids,
                    }
                )
                state["file_cleanup_pending"] = sorted(
                    {
                        *self._string_list(state.get("file_cleanup_pending")),
                        *source_paths,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO collection_sync_state(collection_id, state_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(collection_id) DO UPDATE SET
                        state_json=excluded.state_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        collection_id,
                        json.dumps(state, ensure_ascii=False),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return MissingCollectionCleanup(
            chunk_ids=tuple(chunk_ids),
            source_paths=tuple(source_paths),
        )

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item]

    def collection_sources(self, collection_id: str) -> dict[str, str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_url, source_path
                FROM sources
                WHERE json_extract(metadata_json, '$.collection_id') = ?
                  AND source_url IS NOT NULL
                  AND source_path IS NOT NULL
                """,
                (collection_id,),
            ).fetchall()
        return {row["source_url"]: row["source_path"] for row in rows}

    def collection_state(self, collection_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM collection_sync_state WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
        if row is None:
            return {}
        value = json.loads(row["state_json"])
        return value if isinstance(value, dict) else {}

    def update_collection_state(self, collection_id: str, state: dict[str, object]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO collection_sync_state(collection_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(collection_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (
                    collection_id,
                    json.dumps(state, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()

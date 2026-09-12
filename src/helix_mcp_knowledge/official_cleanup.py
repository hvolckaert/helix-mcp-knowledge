"""Preview and physically purge unselected official documentation."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .catalog.products import ProductCatalog
from .catalog.versions import normalize_version
from .storage.automation import AutomationStore
from .storage.database import Database
from .storage.vectors import VectorIndex
from .sync.manifest import OfficialSourceManifest

SQL_BATCH_SIZE = 400
CLEANUP_JOB_ID = "official-cleanup"


@dataclass(frozen=True, slots=True)
class OfficialCleanupGroup:
    product: str
    version: str
    documents: int
    chunks: int
    estimated_bytes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OfficialCleanupDocument:
    document_id: str
    source_url: str
    source_path: str
    product: str
    version: str


@dataclass(frozen=True, slots=True)
class OfficialCleanupPreview:
    required: bool
    documents: int
    chunks: int
    source_files: int
    estimated_bytes: int
    product_versions: tuple[OfficialCleanupGroup, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "product_versions": [item.to_dict() for item in self.product_versions],
        }


@dataclass(frozen=True, slots=True)
class OfficialCleanupReport:
    preview: OfficialCleanupPreview
    documents: tuple[OfficialCleanupDocument, ...]
    deleted_files: int
    deleted_file_bytes: int
    database_compacted: bool
    warnings: tuple[str, ...]


@dataclass(slots=True)
class _CleanupPlan:
    preview: OfficialCleanupPreview
    documents: list[OfficialCleanupDocument]
    document_ids: list[str]
    chunk_ids: list[str]
    source_ids: list[str]
    collection_ids: list[str]
    files: list[Path]


class OfficialCorpusCleaner:
    """Delete only derived and downloaded data for unselected BMC versions."""

    def __init__(
        self,
        *,
        database: Database,
        vector_index: VectorIndex,
        official_sources_root: Path,
        catalog: ProductCatalog,
        manifest: OfficialSourceManifest,
    ) -> None:
        self.database = database
        self.vector_index = vector_index
        self.official_sources_root = official_sources_root.resolve()
        self.catalog = catalog
        self.manifest = manifest

    def preview(self, selected_pairs: set[tuple[str, str]]) -> OfficialCleanupPreview:
        return self._plan(self._normalize_pairs(selected_pairs)).preview

    def purge(self, selected_pairs: set[tuple[str, str]]) -> OfficialCleanupReport:
        recovery_pending, recovery_warnings = self._resume_pending_cleanup()
        plan = self._plan(self._normalize_pairs(selected_pairs))
        if recovery_pending:
            return OfficialCleanupReport(
                preview=plan.preview,
                documents=(),
                deleted_files=0,
                deleted_file_bytes=0,
                database_compacted=False,
                warnings=tuple(recovery_warnings),
            )
        if not plan.preview.required:
            return OfficialCleanupReport(
                preview=plan.preview,
                documents=(),
                deleted_files=0,
                deleted_file_bytes=0,
                database_compacted=False,
                warnings=tuple(recovery_warnings),
            )

        journal = {
            "status": "external_pending",
            "created_at": datetime.now(UTC).isoformat(),
            "chunk_ids": plan.chunk_ids,
            "files": [str(path) for path in plan.files],
        }
        with self.database.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._upsert_journal(connection, journal)
                for values in self._batches(plan.chunk_ids):
                    placeholders = ", ".join("?" for _ in values)
                    connection.execute(
                        f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", values
                    )
                for values in self._batches(plan.document_ids):
                    placeholders = ", ".join("?" for _ in values)
                    connection.execute(
                        f"DELETE FROM documents WHERE document_id IN ({placeholders})", values
                    )
                for values in self._batches(plan.source_ids):
                    placeholders = ", ".join("?" for _ in values)
                    connection.execute(
                        f"DELETE FROM sources WHERE source_id IN ({placeholders})", values
                    )
                for values in self._batches(plan.collection_ids):
                    placeholders = ", ".join("?" for _ in values)
                    connection.execute(
                        f"DELETE FROM collection_sync_state "
                        f"WHERE collection_id IN ({placeholders})",
                        values,
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        deleted_files, deleted_file_bytes, compacted, warnings = self._finish_external_cleanup(
            plan.chunk_ids, plan.files
        )
        self._persist_journal_result(journal, warnings)

        return OfficialCleanupReport(
            preview=plan.preview,
            documents=tuple(plan.documents),
            deleted_files=deleted_files,
            deleted_file_bytes=deleted_file_bytes,
            database_compacted=compacted,
            warnings=tuple([*recovery_warnings, *warnings]),
        )

    def _resume_pending_cleanup(self) -> tuple[bool, list[str]]:
        store = AutomationStore(self.database)
        state = store.state(CLEANUP_JOB_ID)
        if state.get("status") not in {"external_pending", "retry_pending"}:
            return False, []
        raw_chunk_ids = state.get("chunk_ids")
        raw_files = state.get("files")
        if not isinstance(raw_chunk_ids, list) or not all(
            isinstance(value, str) for value in raw_chunk_ids
        ):
            return True, ["pending official cleanup has invalid chunk metadata"]
        if not isinstance(raw_files, list) or not all(
            isinstance(value, str) for value in raw_files
        ):
            return True, ["pending official cleanup has invalid file metadata"]
        files: list[Path] = []
        invalid_files: list[str] = []
        for raw_path in raw_files:
            path = self._safe_pending_file(raw_path)
            if path is None:
                invalid_files.append(raw_path)
            else:
                files.append(path)
        _, _, _, warnings = self._finish_external_cleanup(raw_chunk_ids, files)
        warnings.extend(
            f"unsafe pending cleanup path was rejected: {path}" for path in invalid_files
        )
        self._persist_journal_result(state, warnings)
        return bool(warnings), warnings

    def _finish_external_cleanup(
        self,
        chunk_ids: list[str],
        files: list[Path],
    ) -> tuple[int, int, bool, list[str]]:
        warnings: list[str] = []
        if chunk_ids:
            for values in self._batches(chunk_ids):
                try:
                    self.vector_index.delete(values)
                except Exception as exc:  # SQLite remains authoritative.
                    warnings.append(f"semantic vector cleanup failed: {exc}")
                    break

        deleted_files = 0
        deleted_file_bytes = 0
        for path in files:
            try:
                size = path.stat(follow_symlinks=False).st_size
                path.unlink()
                deleted_files += 1
                deleted_file_bytes += size
                self._prune_empty_parents(path.parent)
            except FileNotFoundError:
                continue
            except OSError as exc:
                warnings.append(f"could not remove downloaded source {path}: {exc}")

        compacted = False
        try:
            with self.database.connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
            compacted = True
        except Exception as exc:
            warnings.append(f"database compaction failed: {exc}")
        return deleted_files, deleted_file_bytes, compacted, warnings

    @staticmethod
    def _upsert_journal(connection, state: dict[str, object]) -> None:
        connection.execute(
            """
            INSERT INTO automation_state(job_id, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                state_json=excluded.state_json,
                updated_at=excluded.updated_at
            """,
            (
                CLEANUP_JOB_ID,
                json.dumps(state, ensure_ascii=False),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _persist_journal_result(self, state: dict[str, object], warnings: list[str]) -> None:
        completed = not warnings
        AutomationStore(self.database).update_state(
            CLEANUP_JOB_ID,
            {
                **state,
                "status": "completed" if completed else "retry_pending",
                "finished_at": datetime.now(UTC).isoformat() if completed else None,
                "warnings": warnings,
                "chunk_ids": [] if completed else state.get("chunk_ids", []),
                "files": [] if completed else state.get("files", []),
            },
        )

    def _plan(self, selected_pairs: set[tuple[str, str]]) -> _CleanupPlan:
        with self.database.connect() as connection:
            document_rows = connection.execute(
                """
                SELECT d.document_id, d.source_url, d.source_path,
                       pv.product_id, pv.version
                FROM documents d
                LEFT JOIN document_product_versions dpv
                  ON dpv.document_id = d.document_id
                LEFT JOIN product_versions pv
                  ON pv.product_version_id = dpv.product_version_id
                WHERE d.source_scope = 'bmc_official'
                ORDER BY d.document_id, pv.product_id, pv.version
                """
            ).fetchall()
            source_rows = connection.execute(
                """
                SELECT source_id, source_path, source_url, metadata_json
                FROM sources
                WHERE source_type = 'bmc_public_url'
                """
            ).fetchall()
            collection_state_ids = {
                str(row["collection_id"])
                for row in connection.execute(
                    "SELECT collection_id FROM collection_sync_state"
                ).fetchall()
            }

        documents: dict[str, dict[str, object]] = {}
        for row in document_rows:
            item = documents.setdefault(
                row["document_id"],
                {
                    "source_url": row["source_url"] or "",
                    "source_path": row["source_path"] or "",
                    "pairs": set(),
                },
            )
            if row["product_id"] and row["version"]:
                item["pairs"].add((str(row["product_id"]), normalize_version(str(row["version"]))))

        candidate_documents: list[OfficialCleanupDocument] = []
        candidate_document_ids: list[str] = []
        kept_paths: set[str] = set()
        kept_urls: set[str] = set()
        primary_pairs: dict[str, tuple[str, str]] = {}
        for document_id, item in documents.items():
            pairs = item["pairs"]
            source_path = str(item["source_path"])
            source_url = str(item["source_url"])
            if not isinstance(pairs, set) or not pairs or pairs & selected_pairs:
                if source_path:
                    kept_paths.add(source_path)
                if source_url:
                    kept_urls.add(source_url)
                continue
            pair = sorted(pairs)[0]
            primary_pairs[document_id] = pair
            candidate_document_ids.append(document_id)
            candidate_documents.append(
                OfficialCleanupDocument(
                    document_id=document_id,
                    source_url=source_url,
                    source_path=source_path,
                    product=pair[0],
                    version=pair[1],
                )
            )

        chunk_ids: list[str] = []
        chunk_counts: dict[str, int] = defaultdict(int)
        index_bytes: dict[str, int] = defaultdict(int)
        if candidate_document_ids:
            with self.database.connect() as connection:
                for values in self._batches(candidate_document_ids):
                    placeholders = ", ".join("?" for _ in values)
                    rows = connection.execute(
                        f"""
                        SELECT chunk_id, document_id,
                               length(text) + length(embedding_text) AS estimated_bytes
                        FROM chunks WHERE document_id IN ({placeholders})
                        """,
                        values,
                    ).fetchall()
                    for row in rows:
                        chunk_ids.append(str(row["chunk_id"]))
                        chunk_counts[str(row["document_id"])] += 1
                        index_bytes[str(row["document_id"])] += int(row["estimated_bytes"] or 0)

        source_ids: list[str] = []
        candidate_paths_by_pair: dict[tuple[str, str], set[str]] = defaultdict(set)
        for document in candidate_documents:
            if document.source_path:
                candidate_paths_by_pair[(document.product, document.version)].add(
                    document.source_path
                )
        for row in source_rows:
            pair = self._source_pair(str(row["metadata_json"] or "{}"))
            if pair is None or pair in selected_pairs:
                continue
            source_path = str(row["source_path"] or "")
            source_url = str(row["source_url"] or "")
            if source_path in kept_paths or source_url in kept_urls:
                continue
            source_ids.append(str(row["source_id"]))
            if source_path:
                candidate_paths_by_pair[pair].add(source_path)

        safe_files: list[Path] = []
        file_sizes: dict[str, int] = {}
        claimed_paths: set[Path] = set()
        for pair in sorted(candidate_paths_by_pair):
            for raw_path in sorted(candidate_paths_by_pair[pair]):
                path = self._safe_file(raw_path)
                if path is None or path in claimed_paths:
                    continue
                claimed_paths.add(path)
                safe_files.append(path)
                try:
                    file_sizes[str(path)] = path.stat(follow_symlinks=False).st_size
                except OSError:
                    file_sizes[str(path)] = 0

        group_documents: dict[tuple[str, str], int] = defaultdict(int)
        group_chunks: dict[tuple[str, str], int] = defaultdict(int)
        group_bytes: dict[tuple[str, str], int] = defaultdict(int)
        for document in candidate_documents:
            pair = (document.product, document.version)
            group_documents[pair] += 1
            group_chunks[pair] += chunk_counts[document.document_id]
            group_bytes[pair] += index_bytes[document.document_id]
        for pair, raw_paths in candidate_paths_by_pair.items():
            for raw_path in raw_paths:
                path = self._safe_file(raw_path)
                if path is not None:
                    group_bytes[pair] += file_sizes.get(str(path), 0)

        groups = tuple(
            OfficialCleanupGroup(
                product=pair[0],
                version=pair[1],
                documents=group_documents[pair],
                chunks=group_chunks[pair],
                estimated_bytes=group_bytes[pair],
            )
            for pair in sorted(set(group_documents) | set(candidate_paths_by_pair))
        )
        selected_collection_ids = {
            collection.collection_id
            for collection in self.manifest.collections
            if self._definition_pair(collection.product, collection.version) in selected_pairs
        }
        collection_ids = sorted(collection_state_ids - selected_collection_ids)
        preview = OfficialCleanupPreview(
            required=bool(candidate_document_ids or source_ids or collection_ids),
            documents=len(candidate_document_ids),
            chunks=len(chunk_ids),
            source_files=len(safe_files),
            estimated_bytes=sum(group.estimated_bytes for group in groups),
            product_versions=groups,
        )
        return _CleanupPlan(
            preview=preview,
            documents=candidate_documents,
            document_ids=candidate_document_ids,
            chunk_ids=chunk_ids,
            source_ids=source_ids,
            collection_ids=collection_ids,
            files=safe_files,
        )

    def _source_pair(self, raw_metadata: str) -> tuple[str, str] | None:
        try:
            metadata = json.loads(raw_metadata)
            product = metadata.get("product")
            version = metadata.get("version")
            if not isinstance(product, str) or not isinstance(version, str):
                return None
            return self._definition_pair(product, version)
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def _definition_pair(self, product: str, version: str) -> tuple[str, str]:
        return self.catalog.resolve(product).product_id, normalize_version(version)

    def _normalize_pairs(self, selected_pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
        return {self._definition_pair(product, version) for product, version in selected_pairs}

    def _safe_file(self, raw_path: str) -> Path | None:
        path = Path(raw_path).expanduser()
        if path.is_symlink():
            return None
        try:
            resolved = path.resolve()
            resolved.relative_to(self.official_sources_root)
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    def _safe_pending_file(self, raw_path: str) -> Path | None:
        path = Path(raw_path).expanduser()
        if path.is_symlink():
            return None
        try:
            resolved = path.resolve()
            resolved.relative_to(self.official_sources_root)
        except (OSError, ValueError):
            return None
        if resolved.exists() and not resolved.is_file():
            return None
        return resolved

    def _prune_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != self.official_sources_root:
            if current.is_symlink():
                return
            try:
                current.relative_to(self.official_sources_root)
                current.rmdir()
            except (OSError, ValueError):
                return
            current = current.parent

    @staticmethod
    def _batches(values: list[str]) -> list[list[str]]:
        return [
            values[index : index + SQL_BATCH_SIZE]
            for index in range(0, len(values), SQL_BATCH_SIZE)
        ]

"""Small, explicit SQLite gateway used by retrieval and administration."""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..catalog.products import ProductCatalog
from ..projects.registry import ProjectRegistry
from .migrations import SCHEMA_VERSION, migrate


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        self._secure_files()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()
            self._secure_files()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.name != "nt":
            self.path.parent.chmod(0o700)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            migrate(connection)
        self._secure_files()

    def _secure_files(self) -> None:
        if os.name == "nt":
            return
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                if path.stat().st_mode & 0o777 != 0o600:
                    path.chmod(0o600)
            except FileNotFoundError:
                continue

    def sync_catalog(self, catalog: ProductCatalog) -> None:
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO products(product_id, name, aliases_json, active)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                    name=excluded.name,
                    aliases_json=excluded.aliases_json,
                    active=excluded.active
                """,
                [
                    (
                        product.product_id,
                        product.name,
                        json.dumps(product.aliases, ensure_ascii=False),
                        int(product.active),
                    )
                    for product in catalog.all()
                ],
            )
            connection.commit()

    def sync_projects(self, registry: ProjectRegistry) -> None:
        declared_version_ids: set[str] = set()
        with self.connect() as connection:
            for project in registry.all_projects():
                loaded = registry.get_loaded(project.id)
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, name, description, status, classification,
                        documents_path, config_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                    ON CONFLICT(project_id) DO UPDATE SET
                        name=excluded.name,
                        description=excluded.description,
                        status=excluded.status,
                        classification=excluded.classification,
                        documents_path=excluded.documents_path,
                        config_hash=excluded.config_hash,
                        updated_at=datetime('now')
                    """,
                    (
                        project.id,
                        project.name,
                        project.description,
                        project.status.value,
                        project.classification.value,
                        str(project.documents_path),
                        loaded.config_hash,
                    ),
                )
                for item in project.bmc_products.values():
                    product_version_id = f"{item.product_id}:{item.version}"
                    declared_version_ids.add(product_version_id)
                    connection.execute(
                        """
                        INSERT INTO product_versions(product_version_id, product_id, version)
                        VALUES (?, ?, ?)
                        ON CONFLICT(product_id, version) DO NOTHING
                        """,
                        (product_version_id, item.product_id, item.version),
                    )
            referenced_version_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT product_version_id FROM document_product_versions"
                ).fetchall()
            }
            retained_version_ids = declared_version_ids | referenced_version_ids
            if retained_version_ids:
                placeholders = ", ".join("?" for _ in retained_version_ids)
                connection.execute(
                    f"DELETE FROM product_versions "
                    f"WHERE product_version_id NOT IN ({placeholders})",
                    sorted(retained_version_ids),
                )
            else:
                connection.execute("DELETE FROM product_versions")
            connection.commit()

    def status(self) -> dict[str, object]:
        with self.connect() as connection:
            version_row = connection.execute(
                "SELECT max(version) FROM schema_migrations"
            ).fetchone()
            counts = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "projects",
                    "products",
                    "documents",
                    "chunks",
                    "sources",
                    "collection_sync_state",
                    "automation_leases",
                    "automation_state",
                    "vector_cleanup_queue",
                )
            }
        return {
            "database": str(self.path),
            "schema_version": version_row[0] if version_row else SCHEMA_VERSION,
            "counts": counts,
        }

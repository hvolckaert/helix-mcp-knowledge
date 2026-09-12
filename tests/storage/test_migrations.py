import os
import sqlite3

import pytest

import helix_mcp_knowledge.storage.migrations as migrations
from helix_mcp_knowledge.storage.migrations import SCHEMA_VERSION


def test_schema_is_initialized_and_idempotent(app) -> None:
    app.database.initialize()
    status = app.database.status()
    assert status["schema_version"] == SCHEMA_VERSION
    assert status["counts"]["documents"] == 3
    assert status["counts"]["vector_cleanup_queue"] == 0
    with app.database.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    if os.name != "nt":
        assert app.database.path.stat().st_mode & 0o777 == 0o600
        assert app.database.path.parent.stat().st_mode & 0o777 == 0o700


def test_project_sync_removes_only_unreferenced_obsolete_versions(app) -> None:
    with app.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO product_versions(product_version_id, product_id, version)
            VALUES ('arsystem:24.3', 'arsystem', '24.3')
            """
        )
        connection.commit()

    app.database.sync_projects(app.registry)

    with app.database.connect() as connection:
        versions = {
            row[0]
            for row in connection.execute(
                "SELECT product_version_id FROM product_versions"
            ).fetchall()
        }
    assert "arsystem:24.3" not in versions
    assert "cmdb:25.1" in versions
    assert "cmdb:26.1" in versions


def test_failed_migration_rolls_back_its_partial_schema(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(
        migrations,
        "MIGRATION_001",
        "CREATE TABLE partial_table(value TEXT); THIS IS NOT VALID SQL;",
    )

    with pytest.raises(sqlite3.OperationalError):
        migrations.migrate(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    applied = connection.execute("SELECT version FROM schema_migrations").fetchall()
    assert "partial_table" not in tables
    assert applied == []

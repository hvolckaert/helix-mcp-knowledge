"""Versioned SQLite schema migrations."""

import sqlite3

SCHEMA_VERSION = 6


MIGRATION_001 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL CHECK(status IN ('active', 'archived', 'disabled')),
    classification TEXT NOT NULL
        CHECK(classification IN ('public', 'internal', 'confidential', 'restricted')),
    documents_path TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
);

CREATE TABLE product_versions (
    product_version_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    version TEXT NOT NULL,
    UNIQUE(product_id, version)
);

CREATE TABLE documents (
    document_id TEXT PRIMARY KEY,
    source_scope TEXT NOT NULL CHECK(source_scope IN ('bmc_official', 'project')),
    project_id TEXT REFERENCES projects(project_id),
    title TEXT NOT NULL,
    document_type TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_path TEXT,
    source_url TEXT,
    language TEXT NOT NULL,
    classification TEXT NOT NULL
        CHECK(classification IN ('public', 'internal', 'confidential', 'restricted')),
    content_hash TEXT NOT NULL,
    file_size INTEGER,
    etag TEXT,
    last_modified TEXT,
    retrieved_at TEXT,
    indexed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'indexed', 'error', 'missing')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK(
        (source_scope = 'bmc_official' AND project_id IS NULL)
        OR (source_scope = 'project' AND project_id IS NOT NULL)
    )
);

CREATE TABLE document_products (
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    PRIMARY KEY(document_id, product_id)
);

CREATE TABLE document_product_versions (
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    product_version_id TEXT NOT NULL REFERENCES product_versions(product_version_id),
    PRIMARY KEY(document_id, product_version_id)
);

CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    source_scope TEXT NOT NULL CHECK(source_scope IN ('bmc_official', 'project')),
    project_id TEXT REFERENCES projects(project_id),
    document_type TEXT NOT NULL,
    heading_1 TEXT,
    heading_2 TEXT,
    heading_3 TEXT,
    heading_path_json TEXT NOT NULL DEFAULT '[]',
    chunk_type TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding_text TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    token_count INTEGER NOT NULL CHECK(token_count >= 0),
    content_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    CHECK(
        (source_scope = 'bmc_official' AND project_id IS NULL)
        OR (source_scope = 'project' AND project_id IS NOT NULL)
    ),
    UNIQUE(document_id, position)
);

CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_path TEXT,
    source_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE sync_state (
    source_id TEXT PRIMARY KEY REFERENCES sources(source_id) ON DELETE CASCADE,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    chunk_id UNINDEXED,
    title,
    heading_path,
    text,
    technical_terms,
    tokenize='unicode61'
);

CREATE INDEX idx_documents_project ON documents(project_id);
CREATE INDEX idx_documents_scope ON documents(source_scope);
CREATE INDEX idx_documents_status ON documents(status);
CREATE INDEX idx_chunks_document ON chunks(document_id);
CREATE INDEX idx_chunks_project ON chunks(project_id);
CREATE INDEX idx_chunks_scope ON chunks(source_scope);
CREATE INDEX idx_chunks_active ON chunks(active);
"""


MIGRATION_002 = """
ALTER TABLE documents ADD COLUMN source_key TEXT;
CREATE UNIQUE INDEX idx_documents_source_key ON documents(source_key);
"""

MIGRATION_003 = """
CREATE TABLE collection_sync_state (
    collection_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
"""

MIGRATION_004 = """
CREATE TABLE automation_leases (
    lease_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE automation_state (
    job_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
"""

MIGRATION_005 = """
CREATE TABLE vector_cleanup_queue (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT,
    reason TEXT NOT NULL,
    queued_at TEXT NOT NULL
);

CREATE INDEX idx_vector_cleanup_document ON vector_cleanup_queue(document_id);
"""

MIGRATION_006 = """
CREATE INDEX idx_document_product_versions_version
    ON document_product_versions(product_version_id, document_id);
CREATE INDEX idx_chunks_document_active ON chunks(document_id, active);
CREATE INDEX idx_documents_scope_status
    ON documents(source_scope, status, document_id);
"""


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {
        row[0] for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    connection.commit()
    migrations = (
        (1, MIGRATION_001),
        (2, MIGRATION_002),
        (3, MIGRATION_003),
        (4, MIGRATION_004),
        (5, MIGRATION_005),
        (6, MIGRATION_006),
    )
    for version, script in migrations:
        if version in applied:
            continue
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + script
                + "\nINSERT INTO schema_migrations(version, applied_at) "
                + f"VALUES ({version}, datetime('now'));\n"
                + "COMMIT;"
            )
        except Exception:
            connection.rollback()
            raise

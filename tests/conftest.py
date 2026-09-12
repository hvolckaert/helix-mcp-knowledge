from __future__ import annotations

import json
from pathlib import Path

import pytest

from helix_mcp_knowledge.application import KnowledgeApplication

CONFIG = """
server:
  name: helix-mcp-knowledge-test
  transport: stdio
paths:
  sources: data/sources
  projects_config: config/projects
  cache: data/cache
  errors: data/errors
projects:
  default_project: null
official_docs:
  automatic_sync: false
  bootstrap_on_empty: true
  interval_hours: 24
  retain_unselected_versions: true
  products:
    cmdb: {versions: ["26.1"]}
updates:
  enabled: false
storage:
  sqlite:
    path: data/sqlite/test.db
  qdrant:
    url: http://localhost:6333
    collection: test_chunks
    api_key_env: QDRANT_API_KEY
embeddings:
  model: BAAI/bge-m3
  dimension: 1024
  batch_size: 4
  device: null
  normalize: true
retrieval:
  default_top_k: 8
  max_top_k: 20
  lexical: {enabled: true}
  semantic: {enabled: false}
  fusion: {algorithm: rrf, rrf_k: 60}
  exact_match: {enabled: true}
  reranker:
    enabled: false
    model: BAAI/bge-reranker-v2-m3
    candidates: 20
ingestion:
  max_file_size_mb: 10
  watch: {enabled: false, debounce_seconds: 5}
  chunking: {target_tokens: 600, max_tokens: 900, overlap_tokens: 80}
  allowed_extensions: [.pdf, .docx, .md, .html, .htm, .txt]
  http:
    timeout_seconds: 30
    user_agent: test
    allowed_domains: [docs.bmc.com]
logging: {level: WARNING}
"""


PROJECT = """
schema_version: 1
id: {project_id}
name: {name}
status: {status}
classification: confidential
documents:
  path: data/sources/projects/{project_id}/docs
languages: [es, en]
bmc:
  products:
    cmdb:
      version: "{version}"
tags: [cmdb]
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    projects_dir = config_dir / "projects"
    projects_dir.mkdir(parents=True)
    path = config_dir / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    (projects_dir / "example_project.yaml").write_text(
        PROJECT.format(
            project_id="example_project", name="example_project", status="active", version="26.1"
        ),
        encoding="utf-8",
    )
    (projects_dir / "atlas.yaml").write_text(
        PROJECT.format(project_id="atlas", name="ATLAS", status="active", version="26.1"),
        encoding="utf-8",
    )
    return path


def _insert_document(
    app: KnowledgeApplication,
    *,
    document_id: str,
    chunk_id: str,
    title: str,
    text: str,
    source_scope: str,
    project_id: str | None,
    version: str,
    position: int = 0,
) -> None:
    with app.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO documents(
                document_id, source_scope, project_id, title, document_type,
                source_type, source_path, language, classification, content_hash,
                indexed_at, status
            ) VALUES (
                ?, ?, ?, ?, 'design', 'local_markdown', ?, 'es', ?, ?,
                datetime('now'), 'indexed'
            )
            """,
            (
                document_id,
                source_scope,
                project_id,
                title,
                f"/{document_id}.md",
                "public" if project_id is None else "confidential",
                f"hash-{document_id}",
            ),
        )
        connection.execute(
            "INSERT INTO document_products(document_id, product_id) VALUES (?, 'cmdb')",
            (document_id,),
        )
        connection.execute(
            """
            INSERT INTO product_versions(product_version_id, product_id, version)
            VALUES (?, 'cmdb', ?)
            ON CONFLICT(product_id, version) DO NOTHING
            """,
            (f"cmdb:{version}", version),
        )
        connection.execute(
            """
            INSERT INTO document_product_versions(document_id, product_version_id)
            VALUES (?, ?)
            """,
            (document_id, f"cmdb:{version}"),
        )
        heading_path = json.dumps([title, "Reconciliation"])
        connection.execute(
            """
            INSERT INTO chunks(
                chunk_id, document_id, source_scope, project_id, document_type,
                heading_1, heading_path_json, chunk_type, text, embedding_text,
                position, token_count, content_hash, active
            ) VALUES (?, ?, ?, ?, 'design', ?, ?, 'section', ?, ?, ?, 20, ?, 1)
            """,
            (
                chunk_id,
                document_id,
                source_scope,
                project_id,
                title,
                heading_path,
                text,
                text,
                position,
                f"hash-{chunk_id}",
            ),
        )
        connection.execute(
            """
            INSERT INTO chunks_fts(chunk_id, title, heading_path, text, technical_terms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chunk_id, title, "Reconciliation", text, "BMC_ComputerSystem ARERR 120029"),
        )
        connection.commit()


@pytest.fixture
def app(config_path: Path) -> KnowledgeApplication:
    application = KnowledgeApplication.from_config(config_path)
    _insert_document(
        application,
        document_id="doc_official",
        chunk_id="chk_official",
        title="BMC reconciliation guide",
        text="Official BMC reconciliation for BMC_ComputerSystem and ARERR 120029.",
        source_scope="bmc_official",
        project_id=None,
        version="25.1",
    )
    _insert_document(
        application,
        document_id="doc_example_project",
        chunk_id="chk_example_project",
        title="example_project design",
        text="example_project reconciliation design with a project-specific precedence rule.",
        source_scope="project",
        project_id="example_project",
        version="26.1",
    )
    _insert_document(
        application,
        document_id="doc_atlas",
        chunk_id="chk_atlas",
        title="ATLAS design",
        text="ATLAS reconciliation design with a confidential mapping.",
        source_scope="project",
        project_id="atlas",
        version="26.1",
    )
    return application


@pytest.fixture
def insert_document():
    return _insert_document

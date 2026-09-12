from pathlib import Path

import pytest
import yaml

from helix_mcp_knowledge.catalog.products import ProductCatalog
from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.errors import ConfigurationError
from helix_mcp_knowledge.projects.registry import ProjectRegistry


def test_configuration_resolves_paths_from_repository_root(config_path: Path) -> None:
    config = load_config(config_path)
    assert config.sqlite_path == config_path.parent.parent / "data/sqlite/test.db"
    assert config.projects.default_project is None
    assert config.official_docs.automatic_sync is False
    assert config.official_docs.products["cmdb"].versions == ["26.1"]
    assert config.updates.enabled is False
    assert config.updates.retention.enabled is True
    assert config.updates.retention.previous_runtimes == 1
    assert config.updates.retention.successful_backups == 1
    assert config.updates.retention.failed_backup_days == 14
    assert config.updates.retention.diagnostic_days == 30
    assert config.updates.retention.max_log_size_mb == 10
    assert config.catalog_updates.enabled is True
    assert config.catalog_updates.release_prefix == "catalog-v"
    assert config.official_catalog_cache_path == (
        config_path.parent.parent / "data/cache/official-catalog/bmc-official-catalog.yaml"
    )
    assert config.ocr_component_path == config_path.parent.parent / "components/ocr"
    assert config.semantic_component_path == config_path.parent.parent / "components/semantic"
    assert config.reranker_component_path == config_path.parent.parent / "components/reranker"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "some/other-model"),
        ("candidates", 33),
    ],
)
def test_disabled_legacy_reranker_configuration_does_not_block_the_base_server(
    config_path: Path,
    field: str,
    value: object,
) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"][field] = value
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    config = load_config(config_path)

    assert getattr(config.retrieval.reranker, field) == value


def test_reranker_model_cannot_be_blank(config_path: Path) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["model"] = "  "
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="invalid configuration"):
        load_config(config_path)


def test_project_documents_cannot_escape_sources(config_path: Path) -> None:
    project_path = config_path.parent / "projects" / "escape.yaml"
    project_path.write_text(
        """
schema_version: 1
id: escape
name: Escape
status: active
classification: restricted
documents: {path: ../../outside}
languages: [es]
bmc: {products: {}}
tags: []
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="must be inside"):
        ProjectRegistry.load(load_config(config_path), ProductCatalog())


def test_project_documents_can_use_one_explicit_external_root(config_path: Path) -> None:
    external = config_path.parent.parent.parent / "authorized-project-docs"
    payload = yaml.safe_load(config_path.read_text())
    payload["projects"]["external_document_roots"] = [str(external)]
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    project_path = config_path.parent / "projects" / "external.yaml"
    project_path.write_text(
        f"""
schema_version: 1
id: external
name: External
status: active
classification: restricted
documents: {{path: {external}}}
languages: [en]
bmc: {{products: {{}}}}
tags: []
""",
        encoding="utf-8",
    )

    registry = ProjectRegistry.load(load_config(config_path), ProductCatalog())

    assert registry.require("external").documents_path == external

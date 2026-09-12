from pathlib import Path

from helix_mcp_knowledge.config import load_config

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_distributed_configuration_is_product_and_project_free() -> None:
    config = load_config(REPOSITORY_ROOT / "config/distribution-config.yaml")
    assert config.projects.default_project is None
    assert config.official_docs.automatic_sync is True
    assert config.official_docs.products == {}


def test_server_architecture_reference_is_not_a_retrieval_source() -> None:
    config = load_config(REPOSITORY_ROOT / "config/config.yaml")
    reference = REPOSITORY_ROOT / "docs/architecture/v1-specification.md"
    assert reference.is_file()
    assert not reference.is_relative_to(config.sources_path)

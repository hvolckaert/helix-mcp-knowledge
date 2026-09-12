import tomllib
from pathlib import Path

from helix_mcp_knowledge.config import load_config

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_publication_metadata_uses_mit_for_original_project_material() -> None:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        project_file = tomllib.load(stream)

    project = project_file["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["authors"] == [{"name": "Hugo Volckaert"}]
    assert "/LICENSE" in project_file["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    license_text = (REPOSITORY_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Hugo Volckaert" in license_text


def test_public_configurations_are_product_and_project_free() -> None:
    for relative_path in ("config/config.yaml", "config/distribution-config.yaml"):
        config = load_config(REPOSITORY_ROOT / relative_path)
        assert config.projects.default_project is None
        assert config.official_docs.automatic_sync is True
        assert config.official_docs.products == {}


def test_server_architecture_reference_is_not_a_retrieval_source() -> None:
    config = load_config(REPOSITORY_ROOT / "config/config.yaml")
    reference = REPOSITORY_ROOT / "docs/architecture/v1-specification.md"
    assert reference.is_file()
    assert not reference.is_relative_to(config.sources_path)

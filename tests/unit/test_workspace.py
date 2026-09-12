import os
from pathlib import Path

import pytest
import yaml

from helix_mcp_knowledge.errors import ConfigurationError
from helix_mcp_knowledge.workspace import (
    CONFIG_ENV,
    discover_config_path,
    initialize_workspace,
    read_packaged_resource,
    secure_managed_project_documents,
    secure_workspace_metadata,
)


def test_config_discovery_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.yaml"
    environment = tmp_path / "environment.yaml"
    checkout = tmp_path / "checkout/config/config.yaml"
    user_workspace = tmp_path / "user"
    checkout.parent.mkdir(parents=True)
    checkout.touch()
    monkeypatch.setenv(CONFIG_ENV, str(environment))
    monkeypatch.setattr(
        "helix_mcp_knowledge.workspace.default_workspace_path", lambda: user_workspace
    )

    assert discover_config_path(explicit, cwd=checkout.parent.parent) == explicit.resolve()
    assert discover_config_path(cwd=checkout.parent.parent) == environment.resolve()

    monkeypatch.delenv(CONFIG_ENV)
    assert discover_config_path(cwd=checkout.parent.parent) == checkout.resolve()
    checkout.unlink()
    assert (
        discover_config_path(cwd=checkout.parent.parent)
        == (user_workspace / "config/config.yaml").resolve()
    )


def test_initialize_workspace_materializes_generic_distribution(tmp_path: Path) -> None:
    result = initialize_workspace(tmp_path)

    assert result.config == tmp_path / "config/config.yaml"
    assert (tmp_path / "config/sources/bmc-official-26.1.yaml").is_file()
    assert (tmp_path / "config/projects/example.yaml.example").is_file()
    assert (tmp_path / "data/sqlite").is_dir()
    config = yaml.safe_load(result.config.read_text(encoding="utf-8"))
    assert config["projects"]["default_project"] is None
    assert config["official_docs"]["products"] == {}
    assert "example_project" not in result.config.read_text(encoding="utf-8").casefold()
    if os.name != "nt":
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        assert result.config.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "data/sqlite").stat().st_mode & 0o777 == 0o700


def test_initialize_workspace_requires_force_to_overwrite(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    config_path = tmp_path / "config/config.yaml"
    config_path.write_text("user-owned: true\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="--force"):
        initialize_workspace(tmp_path)
    assert config_path.read_text(encoding="utf-8") == "user-owned: true\n"

    result = initialize_workspace(tmp_path, force=True)
    assert config_path in result.overwritten
    assert "official_docs:" in config_path.read_text(encoding="utf-8")


def test_packaged_official_manifest_matches_repository() -> None:
    repository_root = Path(__file__).resolve().parents[2]

    assert (
        read_packaged_resource("config/sources/bmc-official-26.1.yaml")
        == (repository_root / "config/sources/bmc-official-26.1.yaml").read_bytes()
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_secure_workspace_metadata_repairs_existing_private_files(tmp_path: Path) -> None:
    config = tmp_path / "config/config.yaml"
    projects = tmp_path / "config/projects"
    errors = tmp_path / "data/errors"
    projects.mkdir(parents=True)
    errors.mkdir(parents=True)
    project = projects / "private.yaml"
    report = errors / "ingestion-report.json"
    for path in (config, project, report):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
        path.chmod(0o644)
    projects.chmod(0o755)
    errors.chmod(0o755)

    secure_workspace_metadata(
        config_path=config,
        projects_path=projects,
        errors_path=errors,
    )

    assert projects.stat().st_mode & 0o777 == 0o700
    assert errors.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (config, project, report))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_secure_workspace_repairs_managed_documents_without_touching_external_files(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "data/sources"
    managed = sources / "projects/private"
    external = tmp_path / "external"
    managed.mkdir(parents=True)
    external.mkdir()
    managed_file = managed / "design.pdf"
    external_file = external / "notes.pdf"
    managed_file.write_text("managed", encoding="utf-8")
    external_file.write_text("external", encoding="utf-8")
    managed.chmod(0o755)
    managed_file.chmod(0o644)
    external.chmod(0o755)
    external_file.chmod(0o644)

    secure_managed_project_documents(
        sources_path=sources,
        documents_paths=[managed, external],
    )

    assert managed.stat().st_mode & 0o777 == 0o700
    assert managed_file.stat().st_mode & 0o777 == 0o600
    assert external.stat().st_mode & 0o777 == 0o755
    assert external_file.stat().st_mode & 0o777 == 0o644

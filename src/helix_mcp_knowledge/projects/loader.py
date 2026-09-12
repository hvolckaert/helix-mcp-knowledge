"""Load one project YAML file into the canonical Project model."""

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..catalog.products import ProductCatalog
from ..config import AppConfig
from ..errors import ConfigurationError
from ..models.project import Classification, Project, ProjectBmcProduct, ProjectStatus


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _DocumentsConfig(_StrictModel):
    path: Path
    sources_manifest: Path | None = None


class _ProductConfig(_StrictModel):
    version: str = Field(min_length=1, max_length=40)


class _BmcConfig(_StrictModel):
    products: dict[str, _ProductConfig] = Field(default_factory=dict)


class _ProjectFile(_StrictModel):
    schema_version: int
    id: str
    name: str
    description: str | None = None
    status: ProjectStatus
    classification: Classification
    documents: _DocumentsConfig
    languages: list[str] = Field(default_factory=list)
    bmc: _BmcConfig = Field(default_factory=_BmcConfig)
    tags: list[str] = Field(default_factory=list)


class LoadedProject(BaseModel):
    project: Project
    config_hash: str
    config_path: Path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load_project(path: Path, config: AppConfig, catalog: ProductCatalog) -> LoadedProject:
    try:
        raw_bytes = path.read_bytes()
        payload = yaml.safe_load(raw_bytes) or {}
        project_file = _ProjectFile.model_validate(payload)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"invalid project configuration {path}: {exc}") from exc

    if path.stem != project_file.id:
        raise ConfigurationError(
            f"project id {project_file.id!r} must match configuration filename {path.stem!r}"
        )

    documents_path = config.resolve_path(project_file.documents.path)
    allowed_roots = [
        config.sources_path,
        *(config.resolve_path(root) for root in config.projects.external_document_roots),
    ]
    if not any(_inside(documents_path, root) for root in allowed_roots):
        raise ConfigurationError(
            "project documents path must be inside the managed sources folder or an "
            f"explicitly allowed external root: {documents_path}"
        )

    sources_manifest_path = None
    if project_file.documents.sources_manifest is not None:
        sources_manifest_path = config.resolve_path(project_file.documents.sources_manifest)
        if not _inside(sources_manifest_path, config.projects_config_path):
            raise ConfigurationError(
                "project sources manifest must be inside "
                f"{config.projects_config_path}: {sources_manifest_path}"
            )

    products: dict[str, ProjectBmcProduct] = {}
    for configured_id, product_config in project_file.bmc.products.items():
        canonical_id = catalog.resolve(configured_id).product_id
        products[canonical_id] = ProjectBmcProduct(
            product_id=canonical_id,
            version=product_config.version.strip(),
        )

    project = Project(
        schema_version=project_file.schema_version,
        id=project_file.id,
        name=project_file.name,
        description=project_file.description,
        status=project_file.status,
        classification=project_file.classification,
        documents_path=documents_path,
        sources_manifest_path=sources_manifest_path,
        languages=project_file.languages,
        bmc_products=products,
        tags=project_file.tags,
    )
    return LoadedProject(
        project=project,
        config_hash=hashlib.sha256(raw_bytes).hexdigest(),
        config_path=path.resolve(),
    )

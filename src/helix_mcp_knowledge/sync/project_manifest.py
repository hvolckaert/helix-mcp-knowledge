"""Validated manifests for automatically synchronized project documents."""

from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ConfigurationError
from ..models.document import DocumentType
from ..models.project import Classification


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSourceDefinition(_StrictModel):
    path: str = Field(min_length=1)
    document_type: DocumentType = DocumentType.OTHER
    products: dict[str, str | None] = Field(default_factory=dict)
    language: str = Field(default="en", min_length=2, max_length=16)
    classification: Classification | None = None
    title: str | None = Field(default=None, min_length=1)
    enabled: bool = True
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def safe_relative_pattern(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("project source paths must use forward slashes")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value.rstrip().endswith("/"):
            raise ValueError(
                "project source path must be a relative file or glob without parent traversal"
            )
        return path.as_posix()

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        return value.strip().casefold()


class ProjectSourceManifest(_StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    sources: list[ProjectSourceDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_patterns(self) -> "ProjectSourceManifest":
        patterns = [source.path.casefold() for source in self.sources]
        if len(patterns) != len(set(patterns)):
            raise ValueError("project source paths must be unique")
        return self

    @classmethod
    def load(cls, path: Path, *, expected_project_id: str) -> "ProjectSourceManifest":
        path = path.expanduser().resolve()
        if not path.is_file():
            raise ConfigurationError(f"project sources manifest not found: {path}")
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            manifest = cls.model_validate(payload)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            raise ConfigurationError(f"invalid project sources manifest {path}: {exc}") from exc
        if manifest.project_id != expected_project_id:
            raise ConfigurationError(
                f"project sources manifest declares {manifest.project_id!r}; "
                f"expected {expected_project_id!r}"
            )
        return manifest

"""Project configuration and public project contracts."""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DISABLED = "disabled"


class Classification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ProjectBmcProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    version: str


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    status: ProjectStatus
    classification: Classification
    documents_path: Path
    sources_manifest_path: Path | None = None
    languages: list[str] = Field(default_factory=list)
    bmc_products: dict[str, ProjectBmcProduct] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: int) -> int:
        if value != 1:
            raise ValueError("only project schema_version 1 is supported")
        return value

    @field_validator("languages")
    @classmethod
    def normalize_languages(cls, values: list[str]) -> list[str]:
        return sorted({value.strip().lower() for value in values if value.strip()})


class ProjectSummary(BaseModel):
    id: str
    name: str
    status: ProjectStatus
    products: dict[str, str]


class ProjectListResponse(BaseModel):
    projects: list[ProjectSummary]


class ActiveProjectResponse(BaseModel):
    project_id: str | None
    source: str

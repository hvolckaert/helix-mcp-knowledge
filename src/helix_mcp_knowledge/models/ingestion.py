"""Public ingestion request and outcome contracts."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .document import DocumentType
from .project import Classification
from .source import SourceScope, SourceType


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: Path
    source_scope: SourceScope
    project_id: str | None = None
    document_type: DocumentType = DocumentType.OTHER
    language: str = Field(default="en", min_length=2, max_length=16)
    classification: Classification | None = None
    product_versions: dict[str, str | None] = Field(default_factory=dict)
    title: str | None = None
    source_url: str | None = None
    source_type: SourceType | None = None
    etag: str | None = None
    last_modified: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("source_scope")
    @classmethod
    def persistent_scope(cls, value: SourceScope) -> SourceScope:
        if value is SourceScope.ALL_RELEVANT:
            raise ValueError("all_relevant is not a valid ingestion scope")
        return value

    @model_validator(mode="after")
    def scope_matches_project(self) -> "IngestRequest":
        if self.source_scope is SourceScope.PROJECT and self.project_id is None:
            raise ValueError("project scope requires project_id")
        if self.source_scope is SourceScope.BMC_OFFICIAL and self.project_id is not None:
            raise ValueError("official scope cannot have project_id")
        if self.source_url is not None and self.source_scope is not SourceScope.BMC_OFFICIAL:
            raise ValueError("source_url is only valid for official sources")
        if self.source_type is SourceType.BMC_PUBLIC_URL and self.source_url is None:
            raise ValueError("bmc_public_url requires source_url")
        return self


class IngestResult(BaseModel):
    document_id: str
    status: str
    source_path: str
    content_hash: str
    chunks_indexed: int
    vectors_indexed: int

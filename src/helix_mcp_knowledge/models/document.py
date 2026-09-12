"""Document metadata stored in SQLite."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .project import Classification
from .source import SourceScope, SourceType


class DocumentType(StrEnum):
    CONCEPT = "concept"
    REFERENCE = "reference"
    ARCHITECTURE = "architecture"
    ADMINISTRATION = "administration"
    HOWTO = "howto"
    API = "api"
    CDM = "cdm"
    RELEASE_NOTES = "release_notes"
    TROUBLESHOOTING = "troubleshooting"
    DEVELOPMENT = "development"
    INTEGRATION = "integration"
    PROCEDURE = "procedure"
    DESIGN = "design"
    DECISION = "decision"
    OTHER = "other"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    ERROR = "error"
    MISSING = "missing"


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    source_scope: SourceScope
    project_id: str | None = None
    title: str
    document_type: DocumentType
    source_type: SourceType
    source_path: str | None = None
    source_url: str | None = None
    language: str
    classification: Classification
    content_hash: str
    file_size: int | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    retrieved_at: datetime | None = None
    indexed_at: datetime | None = None
    status: DocumentStatus
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("source_scope")
    @classmethod
    def stored_scope_only(cls, value: SourceScope) -> SourceScope:
        if value is SourceScope.ALL_RELEVANT:
            raise ValueError("all_relevant is a query scope, not a stored document scope")
        return value

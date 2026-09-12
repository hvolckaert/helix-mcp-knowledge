"""Retrievable document fragment contracts."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .document import DocumentType
from .source import SourceScope


class ChunkType(StrEnum):
    SECTION = "section"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST = "list"
    CODE = "code"
    DEFINITION = "definition"
    NOTE = "note"
    WARNING = "warning"


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    document_id: str
    source_scope: SourceScope
    project_id: str | None = None
    document_type: DocumentType
    heading_1: str | None = None
    heading_2: str | None = None
    heading_3: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    chunk_type: ChunkType
    text: str
    embedding_text: str
    position: int = Field(ge=0)
    token_count: int = Field(ge=0)
    content_hash: str
    active: bool = True

    @field_validator("source_scope")
    @classmethod
    def stored_scope_only(cls, value: SourceScope) -> SourceScope:
        if value is SourceScope.ALL_RELEVANT:
            raise ValueError("all_relevant is a query scope, not a stored chunk scope")
        return value

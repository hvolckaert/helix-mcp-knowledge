"""Search request, context, result and section contracts."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .document import DocumentType
from .source import SourceScope


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2_000)
    project_id: str | None = None
    source_scope: SourceScope = SourceScope.ALL_RELEVANT
    product: str | None = None
    version: str | None = None
    document_types: list[DocumentType] | None = None
    top_k: int = Field(default=8, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def query_must_have_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query cannot be blank")
        return normalized


class SearchContext(BaseModel):
    project_id: str | None
    project_source: str
    product: str | None
    version: str | None
    version_source: str | None


class MatchInfo(BaseModel):
    lexical: bool
    semantic: bool
    exact_terms: list[str] = Field(default_factory=list)
    reranked: bool


class SearchResult(BaseModel):
    rank: int
    chunk_id: str
    document_id: str
    source_scope: SourceScope
    project_id: str | None
    title: str
    document_type: DocumentType
    product_ids: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    heading_path: list[str] = Field(default_factory=list)
    text: str
    source_path: str | None
    source_url: str | None
    match: MatchInfo


class SearchResponse(BaseModel):
    query: str
    context: SearchContext
    results: list[SearchResult]


class SectionChunk(BaseModel):
    chunk_id: str
    position: int
    heading_path: list[str]
    text: str


class SectionResponse(BaseModel):
    document_id: str
    title: str
    selected_chunk_id: str
    chunks: list[SectionChunk]

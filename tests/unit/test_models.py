import pytest
from pydantic import ValidationError

from helix_mcp_knowledge.models.chunk import Chunk, ChunkType
from helix_mcp_knowledge.models.document import DocumentType
from helix_mcp_knowledge.models.ingestion import IngestRequest
from helix_mcp_knowledge.models.search import SearchRequest
from helix_mcp_knowledge.models.source import SourceScope


def test_top_k_zero_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="cmdb", top_k=0)


def test_ingestion_defaults_to_english() -> None:
    request = IngestRequest(
        source_path="document.md",
        source_scope=SourceScope.BMC_OFFICIAL,
    )

    assert request.language == "en"


def test_all_relevant_cannot_be_stored_on_a_chunk() -> None:
    with pytest.raises(ValidationError, match="query scope"):
        Chunk(
            chunk_id="chk_test",
            document_id="doc_test",
            source_scope=SourceScope.ALL_RELEVANT,
            document_type=DocumentType.REFERENCE,
            chunk_type=ChunkType.SECTION,
            text="text",
            embedding_text="text",
            position=0,
            token_count=1,
            content_hash="hash",
        )

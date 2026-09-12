"""Parser-neutral blocks that preserve document structure."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ...models.chunk import ChunkType


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    text: str
    chunk_type: ChunkType = ChunkType.PARAGRAPH
    heading_level: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    title: str
    blocks: list[ParsedBlock]
    metadata: dict[str, object] = field(default_factory=dict)


class DocumentParser(Protocol):
    def parse(self, path: Path) -> ParsedDocument: ...

"""Plain UTF-8 text parser."""

from pathlib import Path

from ...models.chunk import ChunkType
from ..normalizer import normalize_text
from .base import ParsedBlock, ParsedDocument


class TextParser:
    def parse(self, path: Path) -> ParsedDocument:
        text = path.read_text(encoding="utf-8-sig", errors="strict")
        blocks = [
            ParsedBlock(normalize_text(part), ChunkType.PARAGRAPH)
            for part in text.split("\n\n")
            if normalize_text(part)
        ]
        return ParsedDocument(title=path.stem, blocks=blocks)

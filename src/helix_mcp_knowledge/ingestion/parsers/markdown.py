"""Lightweight structural Markdown parser with no HTML rendering step."""

import re
from pathlib import Path

from ...models.chunk import ChunkType
from ..normalizer import normalize_text
from .base import ParsedBlock, ParsedDocument

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")


class MarkdownParser:
    def parse(self, path: Path) -> ParsedDocument:
        lines = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
        blocks: list[ParsedBlock] = []
        title = path.stem
        buffer: list[str] = []
        mode = ChunkType.PARAGRAPH
        in_code = False

        def flush() -> None:
            nonlocal buffer
            text = normalize_text("\n".join(buffer), preserve_lines=mode is not ChunkType.PARAGRAPH)
            if text:
                blocks.append(ParsedBlock(text=text, chunk_type=mode))
            buffer = []

        for line in lines:
            if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
                if in_code:
                    flush()
                    in_code = False
                    mode = ChunkType.PARAGRAPH
                else:
                    flush()
                    in_code = True
                    mode = ChunkType.CODE
                continue
            if in_code:
                buffer.append(line)
                continue
            heading = HEADING.match(line)
            if heading:
                flush()
                heading_text = normalize_text(heading.group(2))
                if heading_text:
                    level = len(heading.group(1))
                    blocks.append(
                        ParsedBlock(
                            text=heading_text,
                            chunk_type=ChunkType.SECTION,
                            heading_level=level,
                        )
                    )
                    if level == 1 and title == path.stem:
                        title = heading_text
                mode = ChunkType.PARAGRAPH
                continue
            if not line.strip():
                flush()
                mode = ChunkType.PARAGRAPH
                continue
            next_mode = self._line_type(line)
            if buffer and next_mode is not mode:
                flush()
            mode = next_mode
            buffer.append(line)
        flush()
        return ParsedDocument(title=title, blocks=blocks, metadata={"format": "markdown"})

    @staticmethod
    def _line_type(line: str) -> ChunkType:
        if LIST_ITEM.match(line):
            return ChunkType.LIST
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            return ChunkType.TABLE
        if stripped.startswith(("> [!NOTE]", "> **Note", "> Nota")):
            return ChunkType.NOTE
        if stripped.startswith(("> [!WARNING]", "> **Warning", "> Aviso")):
            return ChunkType.WARNING
        return ChunkType.PARAGRAPH

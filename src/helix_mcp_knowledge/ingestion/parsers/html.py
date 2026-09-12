"""HTML parser that strips active content and preserves semantic block types."""

from pathlib import Path

from bs4 import BeautifulSoup, Tag

from ...models.chunk import ChunkType
from ..normalizer import normalize_text
from .base import ParsedBlock, ParsedDocument


class HtmlParser:
    def parse(self, path: Path) -> ParsedDocument:
        soup = BeautifulSoup(path.read_text(encoding="utf-8-sig"), "html.parser")
        for unsafe in soup(["script", "style", "noscript", "template", "svg"]):
            unsafe.decompose()
        title = normalize_text(soup.title.get_text(" ")) if soup.title else path.stem
        root = (
            soup.select_one("#xwikicontent")
            or soup.select_one(".xwikicontent")
            or soup.select_one("main")
            or soup.select_one("article")
            or soup.body
            or soup
        )
        for chrome in root.find_all(["nav", "header", "footer", "form", "button"]):
            chrome.decompose()
        blocks: list[ParsedBlock] = []
        tags = root.find_all(
            ["h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "ul", "ol", "table", "aside"]
        )
        for tag in tags:
            if self._nested_in_supported_parent(tag):
                continue
            text = normalize_text(tag.get_text("\n"), preserve_lines=tag.name in {"pre", "table"})
            if not text:
                continue
            if tag.name.startswith("h"):
                level = int(tag.name[1])
                blocks.append(ParsedBlock(text, ChunkType.SECTION, heading_level=level))
                if level == 1 and title == path.stem:
                    title = text
            else:
                blocks.append(ParsedBlock(text, self._chunk_type(tag)))
        return ParsedDocument(title=title, blocks=blocks, metadata={"format": "html"})

    @staticmethod
    def _nested_in_supported_parent(tag: Tag) -> bool:
        supported = {"p", "pre", "ul", "ol", "table", "aside"}
        return any(parent.name in supported for parent in tag.parents if isinstance(parent, Tag))

    @staticmethod
    def _chunk_type(tag: Tag) -> ChunkType:
        return {
            "pre": ChunkType.CODE,
            "ul": ChunkType.LIST,
            "ol": ChunkType.LIST,
            "table": ChunkType.TABLE,
            "aside": ChunkType.NOTE,
        }.get(tag.name, ChunkType.PARAGRAPH)

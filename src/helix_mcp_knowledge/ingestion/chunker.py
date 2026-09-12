"""Structure-aware deterministic chunking with bounded overlap."""

import re
from dataclasses import dataclass

from ..models.chunk import ChunkType
from ..retrieval.query_parser import extract_technical_terms
from .fingerprint import fingerprint_text
from .parsers.base import ParsedBlock, ParsedDocument

TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def estimate_tokens(text: str) -> int:
    return len(TOKEN.findall(text))


def _word_tail(text: str, count: int) -> str:
    if count <= 0:
        return ""
    words = text.split()
    selected: list[str] = []
    tokens = 0
    for word in reversed(words):
        word_tokens = estimate_tokens(word)
        if selected and tokens + word_tokens > count:
            break
        if word_tokens > count:
            continue
        selected.append(word)
        tokens += word_tokens
    return " ".join(reversed(selected))


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    heading_path: list[str]
    chunk_type: ChunkType
    text: str
    embedding_text: str
    position: int
    token_count: int
    content_hash: str
    technical_terms: list[str]


class Chunker:
    def __init__(self, *, target_tokens: int, max_tokens: int, overlap_tokens: int) -> None:
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.payload_limit = max(1, max_tokens - overlap_tokens)

    def chunk(self, document: ParsedDocument) -> list[ChunkDraft]:
        headings: list[str] = []
        pending: list[str] = []
        pending_type = ChunkType.PARAGRAPH
        pending_headings: list[str] = []
        raw: list[tuple[list[str], ChunkType, str]] = []

        def flush() -> None:
            nonlocal pending
            if pending:
                raw.append((pending_headings.copy(), pending_type, "\n\n".join(pending)))
            pending = []

        for block in document.blocks:
            if block.heading_level is not None:
                flush()
                level = max(1, min(6, block.heading_level))
                headings = headings[: level - 1]
                headings.append(block.text)
                continue
            for piece in self._split_block(block):
                piece_tokens = estimate_tokens(piece)
                pending_tokens = estimate_tokens("\n\n".join(pending))
                should_isolate = block.chunk_type in {
                    ChunkType.CODE,
                    ChunkType.TABLE,
                    ChunkType.WARNING,
                    ChunkType.NOTE,
                }
                if pending and (
                    pending_headings != headings
                    or pending_type is not block.chunk_type
                    or pending_tokens + piece_tokens > self.payload_limit
                    or should_isolate
                ):
                    flush()
                if not pending:
                    pending_type = block.chunk_type
                    pending_headings = headings.copy()
                pending.append(piece)
                if should_isolate or estimate_tokens("\n\n".join(pending)) >= self.target_tokens:
                    flush()
        flush()
        return self._with_overlap(document.title, raw)

    def _split_block(self, block: ParsedBlock) -> list[str]:
        if estimate_tokens(block.text) <= self.payload_limit:
            return [block.text]
        words = block.text.split()
        pieces: list[str] = []
        current: list[str] = []
        current_tokens = 0
        for word in words:
            word_tokens = estimate_tokens(word)
            if current and current_tokens + word_tokens > self.payload_limit:
                pieces.append(" ".join(current))
                current = []
                current_tokens = 0
            current.append(word)
            current_tokens += word_tokens
        if current:
            pieces.append(" ".join(current))
        return pieces

    def _with_overlap(
        self,
        title: str,
        raw: list[tuple[list[str], ChunkType, str]],
    ) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        previous_text = ""
        previous_headings: list[str] = []
        previous_type: ChunkType | None = None
        for position, (headings, chunk_type, text) in enumerate(raw):
            can_overlap = (
                self.overlap_tokens > 0
                and headings == previous_headings
                and chunk_type in {ChunkType.PARAGRAPH, ChunkType.LIST}
                and previous_type in {ChunkType.PARAGRAPH, ChunkType.LIST}
            )
            overlap = _word_tail(previous_text, self.overlap_tokens) if can_overlap else ""
            final_text = f"{overlap}\n\n{text}" if overlap else text
            if estimate_tokens(final_text) > self.max_tokens:
                allowed_words = max(1, self.max_tokens - estimate_tokens(text))
                overlap = _word_tail(previous_text, allowed_words)
                final_text = f"{overlap}\n\n{text}" if overlap else text
            embedding_text = "\n".join([title, *headings, final_text])
            drafts.append(
                ChunkDraft(
                    heading_path=headings,
                    chunk_type=chunk_type,
                    text=final_text,
                    embedding_text=embedding_text,
                    position=position,
                    token_count=estimate_tokens(final_text),
                    content_hash=fingerprint_text(final_text),
                    technical_terms=extract_technical_terms(final_text),
                )
            )
            previous_text = text
            previous_headings = headings
            previous_type = chunk_type
        return drafts

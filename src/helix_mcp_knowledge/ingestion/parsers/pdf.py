"""Hybrid PDF parser with optional OCR for scanned project pages."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from ...config import OcrSettings
from ...errors import IngestionError
from ...models.chunk import ChunkType
from ...ocr_component import OcrPage
from ..normalizer import normalize_text
from .base import ParsedBlock, ParsedDocument


class OcrExtractor(Protocol):
    def extract(self, path: Path, page_numbers: list[int]) -> Mapping[int, OcrPage]: ...


class PdfParser:
    def __init__(
        self,
        *,
        ocr_settings: OcrSettings | None = None,
        ocr: OcrExtractor | None = None,
    ) -> None:
        self.ocr_settings = ocr_settings or OcrSettings()
        self.ocr = ocr

    def parse(self, path: Path, *, allow_ocr: bool = False) -> ParsedDocument:
        try:
            reader = PdfReader(path, strict=False)
            if reader.is_encrypted:
                raise IngestionError(f"encrypted PDF is not supported: {path}")
            metadata = reader.metadata or {}
            title = normalize_text(str(metadata.get("/Title", ""))) or path.stem
            page_text: dict[int, str] = {}
            for page_number, page in enumerate(reader.pages, start=1):
                text = normalize_text(
                    page.extract_text(
                        extraction_mode="layout",
                        layout_mode_space_vertically=False,
                    )
                    or ""
                )
                page_text[page_number] = text
            ocr_pages: dict[int, OcrPage] = {}
            candidates = [
                page_number
                for page_number, text in page_text.items()
                if len(text) < self.ocr_settings.min_text_characters
            ]
            if allow_ocr and self.ocr_settings.enabled and candidates:
                if self.ocr is None:
                    raise IngestionError("OCR is enabled but its component is unavailable")
                ocr_pages = dict(self.ocr.extract(path, candidates))
                for page_number, result in ocr_pages.items():
                    normalized = normalize_text(result.text)
                    if normalized:
                        page_text[page_number] = normalized
            blocks: list[ParsedBlock] = []
            for page_number, text in page_text.items():
                if not text:
                    continue
                blocks.append(
                    ParsedBlock(
                        text=f"Page {page_number}",
                        chunk_type=ChunkType.SECTION,
                        heading_level=1,
                    )
                )
                for paragraph in text.split("\n\n"):
                    normalized = normalize_text(paragraph)
                    if normalized:
                        blocks.append(ParsedBlock(normalized, ChunkType.PARAGRAPH))
        except (OSError, PdfReadError, ValueError) as exc:
            raise IngestionError(f"cannot parse PDF {path}: {exc}") from exc
        if not blocks:
            suffix = (
                "OCR produced no text"
                if allow_ocr and self.ocr_settings.enabled
                else "enable the optional project PDF OCR component"
            )
            raise IngestionError(f"PDF has no extractable text layer; {suffix}: {path}")
        confidences = [
            result.confidence
            for result in ocr_pages.values()
            if result.confidence is not None and normalize_text(result.text)
        ]
        return ParsedDocument(
            title=title,
            blocks=blocks,
            metadata={
                "format": "pdf",
                "pages": len(reader.pages),
                "ocr": {
                    "used": bool(confidences or any(result.text for result in ocr_pages.values())),
                    "engine": "rapidocr" if ocr_pages else None,
                    "pages": sorted(
                        page_number
                        for page_number, result in ocr_pages.items()
                        if normalize_text(result.text)
                    ),
                    "average_confidence": (
                        round(sum(confidences) / len(confidences), 4) if confidences else None
                    ),
                },
            },
        )

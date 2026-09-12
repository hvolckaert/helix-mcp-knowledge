"""Parser dispatch with extension, size and symlink checks."""

from pathlib import Path

from ...config import OcrSettings
from ...errors import IngestionError
from ...ocr_component import OcrClient
from .base import DocumentParser, ParsedDocument
from .docx import DocxParser
from .html import HtmlParser
from .markdown import MarkdownParser
from .pdf import PdfParser
from .text import TextParser

SUPPORTED_DOCUMENT_EXTENSIONS = frozenset({".docx", ".htm", ".html", ".md", ".pdf", ".txt"})


class ParserRegistry:
    def __init__(
        self,
        *,
        allowed_extensions: list[str],
        max_file_size_mb: int,
        ocr_settings: OcrSettings | None = None,
        ocr_client: OcrClient | None = None,
    ) -> None:
        self.allowed_extensions = {extension.casefold() for extension in allowed_extensions}
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self._parsers: dict[str, DocumentParser] = {
            ".pdf": PdfParser(ocr_settings=ocr_settings, ocr=ocr_client),
            ".docx": DocxParser(),
            ".md": MarkdownParser(),
            ".html": HtmlParser(),
            ".htm": HtmlParser(),
            ".txt": TextParser(),
        }
        if set(self._parsers) != SUPPORTED_DOCUMENT_EXTENSIONS:
            raise RuntimeError("parser extension registry is inconsistent")

    def parse(self, source: str | Path, *, allow_ocr: bool = False) -> ParsedDocument:
        path = self.validate(source)
        extension = path.suffix.casefold()
        try:
            parser = self._parsers[extension]
            if isinstance(parser, PdfParser):
                return parser.parse(path, allow_ocr=allow_ocr)
            return parser.parse(path)
        except UnicodeError as exc:
            raise IngestionError(f"source is not valid UTF-8: {path}") from exc

    def validate(self, source: str | Path) -> Path:
        """Validate a source without parsing its contents and return its resolved path."""
        path = Path(source)
        if path.is_symlink():
            raise IngestionError(f"symbolic-link sources are not accepted: {path}")
        path = path.expanduser().resolve()
        if not path.is_file():
            raise IngestionError(f"source file not found: {path}")
        extension = path.suffix.casefold()
        if extension not in self.allowed_extensions or extension not in self._parsers:
            raise IngestionError(f"unsupported document extension: {extension or '<none>'}")
        if path.stat().st_size > self.max_file_size:
            limit_mb = self.max_file_size // (1024 * 1024)
            raise IngestionError(f"source exceeds configured limit of {limit_mb} MB: {path}")
        return path

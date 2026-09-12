"""Content hashes used for idempotent ingestion."""

import hashlib
from pathlib import Path

from .parsers.base import ParsedDocument


def fingerprint_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_parsed(document: ParsedDocument) -> str:
    """Hash semantic parser output while ignoring dynamic page chrome."""
    digest = hashlib.sha256()
    digest.update(document.title.encode("utf-8"))
    for block in document.blocks:
        digest.update(b"\0")
        digest.update(block.chunk_type.value.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(block.heading_level or 0).encode("ascii"))
        digest.update(b"\0")
        digest.update(block.text.encode("utf-8"))
    return digest.hexdigest()

"""Validated paths for checksum-locked optional component dependencies."""

from pathlib import Path

from .errors import ConfigurationError

PIP_BOOTSTRAP_REQUIREMENTS = "pip-bootstrap.txt"
OCR_COMPONENT_REQUIREMENTS = "ocr-component.txt"
SEMANTIC_COMPONENT_REQUIREMENTS = "semantic-component.txt"
RERANKER_COMPONENT_REQUIREMENTS = "reranker-component.txt"


def locked_component_requirements(name: str) -> Path:
    path = Path(__file__).resolve().parent / "resources" / "requirements" / name
    if not path.is_file() or path.is_symlink():
        raise ConfigurationError(f"locked component requirements are missing: {name}")
    content = path.read_text(encoding="utf-8")
    if "--hash=sha256:" not in content:
        raise ConfigurationError(f"locked component requirements do not contain hashes: {name}")
    return path

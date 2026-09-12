"""Document source contracts."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SourceScope(StrEnum):
    BMC_OFFICIAL = "bmc_official"
    PROJECT = "project"
    ALL_RELEVANT = "all_relevant"


class SourceType(StrEnum):
    LOCAL_PDF = "local_pdf"
    LOCAL_DOCX = "local_docx"
    LOCAL_MARKDOWN = "local_markdown"
    LOCAL_HTML = "local_html"
    LOCAL_TEXT = "local_text"
    BMC_PUBLIC_URL = "bmc_public_url"


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_type: SourceType
    source_path: str | None = None
    source_url: str | None = None
    enabled: bool = True

"""Validated, curated catalog of official BMC documentation sources."""

from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ConfigurationError
from ..models.document import DocumentType


def _validate_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("official source URL must be an absolute credential-free HTTPS URL")
    if parsed.fragment:
        raise ValueError("official source URL cannot contain a fragment")
    return value


class OfficialSourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    title: str | None = Field(default=None, min_length=1)
    url: str
    local_path: str
    product: str = Field(min_length=1)
    version: str = Field(min_length=1)
    document_type: DocumentType = DocumentType.OTHER
    language: str = Field(default="en", min_length=2, max_length=16)
    enabled: bool = True
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def absolute_https_url(cls, value: str) -> str:
        return _validate_https_url(value)

    @field_validator("local_path")
    @classmethod
    def safe_relative_html_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.suffix.casefold()
            not in {
                ".html",
                ".htm",
            }
        ):
            raise ValueError("local_path must be a relative HTML path without parent traversal")
        return path.as_posix()


class OfficialCollectionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    root_url: str
    local_path_prefix: str
    product: str = Field(min_length=1)
    version: str = Field(min_length=1)
    language: str = Field(default="en", min_length=2, max_length=16)
    max_pages: int = Field(default=500, ge=1, le=5000)
    request_delay_seconds: float = Field(default=0.25, ge=0.0, le=10.0)
    enabled: bool = True

    @field_validator("root_url")
    @classmethod
    def absolute_https_root(cls, value: str) -> str:
        value = _validate_https_url(value)
        return value if value.endswith("/") else f"{value}/"

    @field_validator("local_path_prefix")
    @classmethod
    def safe_relative_directory(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.suffix:
            raise ValueError("local_path_prefix must be a relative directory")
        return path.as_posix()


class OfficialProductDefinition(BaseModel):
    """Product metadata distributed by the official catalog."""

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{1,63}$")
    name: str = Field(min_length=1, max_length=160)
    aliases: list[str] = Field(default_factory=list)
    active: bool = True

    @field_validator("aliases")
    @classmethod
    def unique_non_empty_aliases(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("product aliases cannot be empty")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("product aliases must be unique")
        return normalized


class OfficialSourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=2)
    catalog_revision: int = Field(default=0, ge=0)
    products: list[OfficialProductDefinition] = Field(default_factory=list)
    publisher: str = "BMC Software"
    sources: list[OfficialSourceDefinition] = Field(default_factory=list)
    collections: list[OfficialCollectionDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_sources(self) -> "OfficialSourceManifest":
        product_ids = [product.product_id for product in self.products]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("official product IDs must be unique")
        if self.schema_version == 2 and not self.products:
            raise ValueError("schema-version 2 catalogs must declare their products")
        if self.schema_version == 2:
            known_products = set(product_ids)
            referenced_products = {item.product for item in [*self.sources, *self.collections]}
            unknown_products = sorted(referenced_products - known_products)
            if unknown_products:
                raise ValueError(
                    "schema-version 2 catalog entries reference undeclared products: "
                    + ", ".join(unknown_products)
                )
        source_ids = [source.source_id for source in self.sources]
        paths = [source.local_path.casefold() for source in self.sources]
        urls = [source.url for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("official source IDs must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("official source local paths must be unique")
        if len(urls) != len(set(urls)):
            raise ValueError("official source URLs must be unique")
        collection_ids = [collection.collection_id for collection in self.collections]
        if len(collection_ids) != len(set(collection_ids)):
            raise ValueError("official collection IDs must be unique")
        return self

    @classmethod
    def load(cls, path: Path) -> "OfficialSourceManifest":
        path = path.expanduser().resolve()
        if not path.is_file():
            raise ConfigurationError(f"official source manifest not found: {path}")
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return cls.model_validate(payload)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            raise ConfigurationError(f"invalid official source manifest {path}: {exc}") from exc

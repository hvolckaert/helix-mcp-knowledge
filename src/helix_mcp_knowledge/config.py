"""Validated application configuration and path resolution."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .errors import ConfigurationError
from .workspace import discover_config_path, missing_config_message


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerSettings(StrictModel):
    name: str = "helix-mcp-knowledge"
    transport: Literal["stdio"] = "stdio"


class PathSettings(StrictModel):
    sources: Path
    projects_config: Path
    cache: Path
    errors: Path
    official_manifest: Path = Path("config/sources/bmc-official-26.1.yaml")


class ProjectSettings(StrictModel):
    default_project: str | None = None
    external_document_roots: list[Path] = Field(default_factory=list)


class OfficialProductSettings(StrictModel):
    versions: list[str] = Field(default_factory=list)

    @field_validator("versions")
    @classmethod
    def normalized_versions(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("official documentation versions cannot be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("official documentation versions must be unique per product")
        return normalized


class OfficialDocsSettings(StrictModel):
    automatic_sync: bool = False
    bootstrap_on_empty: bool = True
    interval_hours: float = Field(default=24.0, ge=0.01, le=8760.0)
    retain_unselected_versions: bool = False
    products: dict[str, OfficialProductSettings] = Field(default_factory=dict)


class UpdateRetentionSettings(StrictModel):
    enabled: bool = True
    previous_runtimes: int = Field(default=1, ge=1, le=10)
    successful_backups: int = Field(default=1, ge=1, le=10)
    failed_backup_days: int = Field(default=14, ge=1, le=365)
    diagnostic_days: int = Field(default=30, ge=1, le=365)
    max_log_size_mb: int = Field(default=10, ge=1, le=100)


class UpdateSettings(StrictModel):
    enabled: bool = True
    interval_hours: float = Field(default=24.0, ge=0.01, le=8760.0)
    retry_minutes: float = Field(default=15.0, ge=1.0, le=1440.0)
    repository: str = Field(
        default="hvolckaert/helix-mcp-knowledge",
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    gh_command: str = Field(default="gh", min_length=1)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    retention: UpdateRetentionSettings = Field(default_factory=UpdateRetentionSettings)


class CatalogUpdateSettings(StrictModel):
    enabled: bool = True
    interval_hours: float = Field(default=24.0, ge=0.01, le=8760.0)
    retry_minutes: float = Field(default=30.0, ge=1.0, le=1440.0)
    repository: str = Field(
        default="hvolckaert/helix-mcp-knowledge",
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    release_prefix: str = Field(default="catalog-v", pattern=r"^[A-Za-z0-9._-]+$")
    manifest_asset: str = Field(default="bmc-official-catalog.yaml", pattern=r"^[A-Za-z0-9._-]+$")
    checksum_asset: str = Field(default="bmc-official-catalog.sha256", pattern=r"^[A-Za-z0-9._-]+$")
    cache_path: Path = Path("data/cache/official-catalog/bmc-official-catalog.yaml")
    gh_command: str = Field(default="gh", min_length=1)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class SQLiteSettings(StrictModel):
    path: Path


class QdrantSettings(StrictModel):
    url: str
    collection: str
    api_key_env: str = "QDRANT_API_KEY"


class StorageSettings(StrictModel):
    sqlite: SQLiteSettings
    qdrant: QdrantSettings


class EmbeddingSettings(StrictModel):
    model: str = "BAAI/bge-m3"
    dimension: int = Field(default=1024, ge=1)
    batch_size: int = Field(default=32, ge=1)
    device: str | None = None
    normalize: bool = True


class EnabledSettings(StrictModel):
    enabled: bool = True


class FusionSettings(StrictModel):
    algorithm: Literal["rrf"] = "rrf"
    rrf_k: int = Field(default=60, ge=1)


class RerankerSettings(StrictModel):
    enabled: bool = False
    # Keep disabled legacy placeholders loadable: an optional component must not
    # prevent the base MCP server from starting. Dashboard activation normalizes
    # these fields to the fixed, bounded implementation shipped by the release.
    model: str = Field(default="BAAI/bge-reranker-v2-m3", min_length=1, max_length=256)
    candidates: int = Field(default=10, ge=1)

    @field_validator("model")
    @classmethod
    def normalized_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reranker model cannot be empty")
        return normalized


class RetrievalSettings(StrictModel):
    default_top_k: int = Field(default=8, ge=1)
    max_top_k: int = Field(default=20, ge=1)
    lexical: EnabledSettings
    semantic: EnabledSettings
    fusion: FusionSettings
    exact_match: EnabledSettings
    reranker: RerankerSettings

    @model_validator(mode="after")
    def valid_top_k(self) -> "RetrievalSettings":
        if self.default_top_k > self.max_top_k:
            raise ValueError("default_top_k cannot exceed max_top_k")
        return self


class WatchSettings(StrictModel):
    enabled: bool = False
    debounce_seconds: float = Field(default=5.0, ge=0.1, le=300.0)
    poll_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    lease_seconds: int = Field(default=120, ge=10, le=3600)
    retry_seconds: int = Field(default=30, ge=5, le=3600)

    @model_validator(mode="after")
    def valid_lease(self) -> "WatchSettings":
        if self.lease_seconds <= self.poll_seconds * 2:
            raise ValueError("lease_seconds must exceed twice poll_seconds")
        return self


class ChunkingSettings(StrictModel):
    target_tokens: int = Field(default=600, ge=1)
    max_tokens: int = Field(default=900, ge=1)
    overlap_tokens: int = Field(default=80, ge=0)

    @model_validator(mode="after")
    def valid_sizes(self) -> "ChunkingSettings":
        if self.target_tokens > self.max_tokens:
            raise ValueError("target_tokens cannot exceed max_tokens")
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be smaller than target_tokens")
        return self


class HttpSettings(StrictModel):
    timeout_seconds: int = Field(default=30, ge=1)
    user_agent: str
    allowed_domains: list[str]
    authentication: "HttpAuthenticationSettings" = Field(
        default_factory=lambda: HttpAuthenticationSettings()
    )


class HttpAuthenticationSettings(StrictModel):
    mode: Literal["none", "basic", "browser"] = "none"
    username_env: str = "BMC_DOCS_USERNAME"
    password_env: str = "BMC_DOCS_PASSWORD"
    browser: "BrowserAuthenticationSettings" = Field(
        default_factory=lambda: BrowserAuthenticationSettings()
    )


class BrowserAuthenticationSettings(StrictModel):
    headless: bool = True
    profile_path: Path = Path("data/cache/bmc-browser-profile")
    login_url: str = "https://docs.helixops.ai/bin/login/XWiki/XWikiLogin"
    challenge_timeout_seconds: int = Field(default=45, ge=5, le=180)
    username_selector: str = 'input[name="username"], input[name="identifier"], input[type="email"]'
    password_selector: str = 'input[name="password"], input[type="password"]'
    submit_selector: str = 'button[type="submit"], input[type="submit"]'


class OcrSettings(StrictModel):
    enabled: bool = False
    component_path: Path = Path("components/ocr")
    min_text_characters: int = Field(default=40, ge=0, le=1000)
    dpi: int = Field(default=200, ge=100, le=300)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    max_pages_per_document: int = Field(default=200, ge=1, le=2000)
    timeout_seconds: int = Field(default=1800, ge=30, le=7200)


class IngestionSettings(StrictModel):
    max_file_size_mb: int = Field(default=100, ge=1)
    watch: WatchSettings
    chunking: ChunkingSettings
    allowed_extensions: list[str]
    http: HttpSettings
    ocr: OcrSettings = Field(default_factory=OcrSettings)


class LoggingSettings(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class AppConfig(StrictModel):
    server: ServerSettings
    paths: PathSettings
    projects: ProjectSettings
    official_docs: OfficialDocsSettings = Field(default_factory=OfficialDocsSettings)
    catalog_updates: CatalogUpdateSettings = Field(default_factory=CatalogUpdateSettings)
    updates: UpdateSettings = Field(default_factory=UpdateSettings)
    storage: StorageSettings
    embeddings: EmbeddingSettings
    retrieval: RetrievalSettings
    ingestion: IngestionSettings
    logging: LoggingSettings

    _base_dir: Path = PrivateAttr()
    _config_path: Path = PrivateAttr()

    def bind(self, config_path: Path) -> "AppConfig":
        self._config_path = config_path.resolve()
        self._base_dir = self._config_path.parent.parent
        return self

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def config_path(self) -> Path:
        return self._config_path

    def resolve_path(self, path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (self.base_dir / path).resolve()

    @property
    def sqlite_path(self) -> Path:
        return self.resolve_path(self.storage.sqlite.path)

    @property
    def projects_config_path(self) -> Path:
        return self.resolve_path(self.paths.projects_config)

    @property
    def sources_path(self) -> Path:
        return self.resolve_path(self.paths.sources)

    @property
    def official_manifest_path(self) -> Path:
        return self.resolve_path(self.paths.official_manifest)

    @property
    def official_catalog_cache_path(self) -> Path:
        return self.resolve_path(self.catalog_updates.cache_path)

    @property
    def ocr_component_path(self) -> Path:
        return self.resolve_path(self.ingestion.ocr.component_path)

    @property
    def semantic_component_path(self) -> Path:
        return self.resolve_path(Path("components/semantic"))

    @property
    def reranker_component_path(self) -> Path:
        return self.resolve_path(Path("components/reranker"))


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = discover_config_path(path)
    if not config_path.is_file():
        raise ConfigurationError(missing_config_message(config_path))
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return AppConfig.model_validate(payload).bind(config_path)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"invalid configuration {config_path}: {exc}") from exc

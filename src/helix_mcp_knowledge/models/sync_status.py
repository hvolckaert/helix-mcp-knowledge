"""Read-only synchronization status contracts."""

from typing import Literal

from pydantic import BaseModel, Field

ReadinessStatus = Literal["not_configured", "not_started", "running", "ready", "error"]
AutomationStatus = Literal["pending", "waiting", "running", "ok", "error", "cancelled"]


class ProductVersionProgress(BaseModel):
    product: str
    version: str
    processed_items: int = Field(ge=0)
    estimated_total_items: int = Field(ge=0)


class OfficialSyncProgress(BaseModel):
    phase: Literal["preparing", "downloading", "discovering", "cleaning_up", "finishing"]
    processed_items: int = Field(ge=0)
    estimated_total_items: int = Field(ge=0)
    percent: float | None = Field(default=None, ge=0, le=100)
    current_product: str | None = None
    current_version: str | None = None
    last_activity_at: str | None = None
    result_counts: dict[str, int] = Field(default_factory=dict)
    chunks_indexed: int = Field(default=0, ge=0)
    product_versions: list[ProductVersionProgress] = Field(default_factory=list)


class OfficialSyncStatus(BaseModel):
    status: ReadinessStatus
    ready: bool
    automatic_sync: bool
    configured_products: dict[str, list[str]]
    indexed_documents: int
    indexed_chunks: int
    automation_status: AutomationStatus | None = None
    started_at: str | None = None
    finished_at: str | None = None
    next_run_at: str | None = None
    result_counts: dict[str, int] = Field(default_factory=dict)
    error_count: int = 0
    notice_count: int = 0
    duration_seconds: float | None = Field(default=None, ge=0)
    cancellation_requested: bool = False
    progress: OfficialSyncProgress | None = None


class ProjectSyncStatus(BaseModel):
    project_id: str
    context_source: str
    status: ReadinessStatus
    ready: bool
    automatic_sync: bool
    products: dict[str, str]
    indexed_documents: int
    indexed_chunks: int
    automation_status: AutomationStatus | None = None
    started_at: str | None = None
    finished_at: str | None = None
    result_counts: dict[str, int] = Field(default_factory=dict)
    error_count: int = 0


class SyncStatusResponse(BaseModel):
    official: OfficialSyncStatus
    project: ProjectSyncStatus | None = None

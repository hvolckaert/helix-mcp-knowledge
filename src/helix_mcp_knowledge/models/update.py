"""Read-only release update status contract."""

from typing import Literal

from pydantic import BaseModel


class UpdateStatus(BaseModel):
    status: Literal["disabled", "unknown", "checking", "current", "available", "error"]
    repository: str
    current_version: str
    latest_version: str | None = None
    update_available: bool | None = None
    release_url: str | None = None
    published_at: str | None = None
    checked_at: str | None = None
    next_check_at: str | None = None
    error: str | None = None

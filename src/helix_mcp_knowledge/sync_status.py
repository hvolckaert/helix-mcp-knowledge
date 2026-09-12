"""Safe operational view of official and project synchronization state."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .catalog.products import ProductCatalog
from .config import AppConfig
from .models.project import Project
from .models.sync_status import (
    AutomationStatus,
    OfficialSyncProgress,
    OfficialSyncStatus,
    ProjectSyncStatus,
    ReadinessStatus,
    SyncStatusResponse,
)
from .projects.context import ProjectContext
from .storage.automation import AutomationStore
from .storage.database import Database


class SyncStatusReader:
    def __init__(
        self,
        *,
        database: Database,
        config: AppConfig,
        catalog: ProductCatalog,
        project_context: ProjectContext,
    ) -> None:
        self.database = database
        self.config = config
        self.catalog = catalog
        self.project_context = project_context
        self.store = AutomationStore(database)

    def get(self, project_id: str | None = None) -> SyncStatusResponse:
        effective = self.project_context.resolve(project_id)
        return SyncStatusResponse(
            official=self._official_status(),
            project=(
                self._project_status(effective.project, effective.source)
                if effective.project is not None
                else None
            ),
        )

    def _official_status(self) -> OfficialSyncStatus:
        configured_products = {
            self.catalog.resolve(product).product_id: list(settings.versions)
            for product, settings in sorted(self.config.official_docs.products.items())
        }
        documents, chunks = self._counts(source_scope="bmc_official")
        state = self.store.state("official-docs")
        ready = documents > 0 and chunks > 0
        return OfficialSyncStatus(
            status=self._readiness(
                state,
                ready=ready,
                configured=bool(configured_products),
            ),
            ready=ready,
            automatic_sync=self.config.official_docs.automatic_sync,
            configured_products=configured_products,
            indexed_documents=documents,
            indexed_chunks=chunks,
            automation_status=self._automation_status(state.get("status")),
            started_at=self._timestamp(state.get("started_at")),
            finished_at=self._timestamp(state.get("finished_at")),
            next_run_at=self._timestamp(state.get("next_run_at")),
            result_counts=self._result_counts(state),
            error_count=self._error_count(state),
            notice_count=self._notice_count(state),
            duration_seconds=self._duration_seconds(state),
            cancellation_requested=state.get("cancel_requested") is True,
            progress=self._progress(state),
        )

    def _project_status(self, project: Project, context_source: str) -> ProjectSyncStatus:
        documents, chunks = self._counts(source_scope="project", project_id=project.id)
        state = self.store.state(f"project:{project.id}")
        ready = documents > 0 and chunks > 0
        return ProjectSyncStatus(
            project_id=project.id,
            context_source=context_source,
            status=self._readiness(state, ready=ready, configured=True),
            ready=ready,
            automatic_sync=(
                self.config.ingestion.watch.enabled and project.sources_manifest_path is not None
            ),
            products={
                product_id: settings.version
                for product_id, settings in sorted(project.bmc_products.items())
            },
            indexed_documents=documents,
            indexed_chunks=chunks,
            automation_status=self._automation_status(state.get("status")),
            started_at=self._timestamp(state.get("started_at")),
            finished_at=self._timestamp(state.get("finished_at")),
            result_counts=self._result_counts(state),
            error_count=self._error_count(state),
        )

    def _counts(self, *, source_scope: str, project_id: str | None = None) -> tuple[int, int]:
        conditions = ["d.source_scope = ?", "d.status = 'indexed'", "c.active = 1"]
        parameters: list[str] = [source_scope]
        if project_id is not None:
            conditions.append("d.project_id = ?")
            parameters.append(project_id)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT count(DISTINCT d.document_id) AS documents,
                       count(c.chunk_id) AS chunks
                FROM documents d
                JOIN chunks c ON c.document_id = d.document_id
                WHERE {" AND ".join(conditions)}
                """,
                parameters,
            ).fetchone()
        return int(row["documents"]), int(row["chunks"])

    @staticmethod
    def _readiness(state: Mapping[str, Any], *, ready: bool, configured: bool) -> ReadinessStatus:
        if not configured:
            return "not_configured"
        automation_status = state.get("status")
        if automation_status == "running":
            return "running"
        if automation_status == "error":
            return "error"
        if ready:
            return "ready"
        return "not_started"

    @staticmethod
    def _automation_status(value: Any) -> AutomationStatus | None:
        if isinstance(value, str) and value in {
            "pending",
            "waiting",
            "running",
            "ok",
            "error",
            "cancelled",
        }:
            return value
        return None

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value

    @staticmethod
    def _result_counts(state: Mapping[str, Any]) -> dict[str, int]:
        value = state.get("result_counts")
        if not isinstance(value, dict):
            return {}
        allowed = {
            "empty",
            "error",
            "indexed",
            "missing",
            "purged",
            "retired",
            "unchanged",
        }
        return {
            str(key): count
            for key, count in value.items()
            if key in allowed
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
        }

    @staticmethod
    def _error_count(state: Mapping[str, Any]) -> int:
        errors = state.get("errors")
        if isinstance(errors, list) and errors:
            return len(errors)
        counts = SyncStatusReader._result_counts(state)
        if not counts and isinstance(state.get("progress"), dict):
            counts = SyncStatusReader._result_counts(
                {"result_counts": state["progress"].get("result_counts")}
            )
        return counts.get("error", 0)

    @classmethod
    def _notice_count(cls, state: Mapping[str, Any]) -> int:
        counts = cls._result_counts(state)
        if not counts and isinstance(state.get("progress"), dict):
            counts = cls._result_counts({"result_counts": state["progress"].get("result_counts")})
        return counts.get("missing", 0) + counts.get("empty", 0)

    @staticmethod
    def _duration_seconds(state: Mapping[str, Any]) -> float | None:
        duration = state.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            return round(float(duration), 3)
        started = state.get("started_at")
        if state.get("status") != "running" or not isinstance(started, str):
            return None
        try:
            timestamp = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError:
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return round(max(0.0, (datetime.now(UTC) - timestamp).total_seconds()), 3)

    @classmethod
    def _progress(cls, state: Mapping[str, Any]) -> OfficialSyncProgress | None:
        value = state.get("progress")
        if not isinstance(value, dict):
            return None
        payload = dict(value)
        payload["last_activity_at"] = cls._timestamp(payload.get("last_activity_at"))
        payload["result_counts"] = cls._result_counts(
            {"result_counts": payload.get("result_counts")}
        )
        try:
            return OfficialSyncProgress.model_validate(payload)
        except ValueError:
            return None

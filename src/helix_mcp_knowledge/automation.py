"""Embedded, leader-elected project synchronization for running MCP servers."""

import logging
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import WatchSettings
from .errors import SourceSyncError
from .models.project import Project
from .projects.registry import ProjectRegistry
from .storage.automation import AutomationStore
from .sync.project_service import ProjectSyncResult
from .update_lock import UpdateLockBusyError

LOGGER = logging.getLogger(__name__)
LEASE_NAME = "embedded-project-sync"

Snapshot = tuple[tuple[str, int, int, int], ...]
ConfigurationSnapshot = tuple[tuple[str, int, int, int], ...]
ReloadConfiguration = Callable[
    [],
    tuple[
        ProjectRegistry,
        WatchSettings,
        list[str],
        Callable[[str], list[ProjectSyncResult]],
    ],
]


class ProjectSyncLease:
    """Heartbeating manual lease compatible with the embedded project watcher."""

    def __init__(
        self,
        store: AutomationStore,
        settings: WatchSettings,
        *,
        owner_id: str | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.owner_id = owner_id or f"manual_{uuid.uuid4()}"
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def __enter__(self) -> "ProjectSyncLease":
        if not self.store.acquire_or_renew(
            LEASE_NAME,
            self.owner_id,
            self.settings.lease_seconds,
        ):
            raise SourceSyncError("project documentation synchronization is already running")
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="helix-manual-project-sync-heartbeat",
            daemon=True,
        )
        self._heartbeat.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=1.0)
        try:
            self.store.release(LEASE_NAME, self.owner_id)
        except Exception:
            LOGGER.exception("failed to release manual project synchronization lease")

    def cancel_requested(self) -> bool:
        return self._lost.is_set()

    def _heartbeat_loop(self) -> None:
        interval = min(5.0, max(1.0, self.settings.lease_seconds / 3))
        while not self._stop.wait(interval):
            try:
                if not self.store.renew(
                    LEASE_NAME,
                    self.owner_id,
                    self.settings.lease_seconds,
                ):
                    self._lost.set()
                    return
            except Exception:
                LOGGER.exception("failed to renew manual project synchronization lease")
                self._lost.set()
                return


class EmbeddedSyncCoordinator:
    """Run project synchronization in one elected MCP process at a time."""

    def __init__(
        self,
        *,
        registry: ProjectRegistry,
        store: AutomationStore,
        settings: WatchSettings,
        allowed_extensions: list[str],
        sync_project: Callable[[str], list[ProjectSyncResult]],
        owner_id: str | None = None,
        configuration_snapshot: Callable[[], ConfigurationSnapshot] | None = None,
        reload_configuration: ReloadConfiguration | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.settings = settings
        self.allowed_extensions = {value.casefold() for value in allowed_extensions}
        self.sync_project = sync_project
        self.owner_id = owner_id or f"mcp_{uuid.uuid4()}"
        self.configuration_snapshot = configuration_snapshot
        self.reload_configuration = reload_configuration
        self._stop = threading.Event()
        self._leader = threading.Event()
        self._worker: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._observed: dict[str, Snapshot] = {}
        self._dirty_since: dict[str, float] = {}
        self._retry_at: dict[str, float] = {}
        self._observed_configuration = (
            configuration_snapshot() if configuration_snapshot is not None else None
        )

    @property
    def is_leader(self) -> bool:
        return self._leader.is_set()

    def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("embedded synchronization coordinator already started")
        self._worker = threading.Thread(
            target=self._run,
            name="helix-project-sync",
            daemon=True,
        )
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="helix-project-sync-heartbeat",
            daemon=True,
        )
        self._worker.start()
        self._heartbeat.start()

    def stop(self, timeout: float = 0.25) -> None:
        self._stop.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in (self._heartbeat, self._worker):
            if thread is not None:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        worker_stopped = self._worker is None or not self._worker.is_alive()
        if self._leader.is_set() and worker_stopped:
            try:
                self.store.release(LEASE_NAME, self.owner_id)
            except Exception:
                LOGGER.exception("failed to release embedded synchronization lease")
            self._leader.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.settings.enabled:
                self._poll_manual_requests()
            elif not self._leader.is_set():
                self._try_become_leader()
            else:
                self._poll_projects()
            self._stop.wait(self.settings.poll_seconds)

    def _try_become_leader(self) -> None:
        if not self._acquire_leadership():
            return
        self._reload_configuration_if_changed()
        if not self._leader.is_set():
            return
        for project in self._projects():
            if self._stop.is_set():
                break
            snapshot = self._snapshot(project)
            request = self.store.state(f"project:{project.id}")
            if self.settings.enabled or request.get("status") == "pending":
                self._sync(project)
            self._observed[project.id] = snapshot

    def _acquire_leadership(self) -> bool:
        try:
            acquired = self.store.acquire_or_renew(
                LEASE_NAME, self.owner_id, self.settings.lease_seconds
            )
        except Exception:
            LOGGER.exception("failed to acquire embedded synchronization lease")
            return
        if not acquired:
            return False
        self._leader.set()
        self._observed.clear()
        self._dirty_since.clear()
        self._retry_at.clear()
        LOGGER.info("embedded project synchronization leader acquired: %s", self.owner_id)
        return True

    def _release_leadership(self) -> None:
        try:
            self.store.release(LEASE_NAME, self.owner_id)
        except Exception:
            LOGGER.exception("failed to release embedded synchronization lease")
        self._leader.clear()

    def _poll_manual_requests(self) -> None:
        self._reload_configuration_if_changed()
        now = time.monotonic()
        for project in self._projects():
            persisted = self.store.state(f"project:{project.id}")
            retry_at = self._retry_at.get(project.id)
            if persisted.get("status") != "pending" or not (
                persisted.get("reason") != "maintenance_busy" or retry_at is None or now >= retry_at
            ):
                continue
            if not self._acquire_leadership():
                return
            try:
                claimed = self.store.state(f"project:{project.id}")
                if claimed.get("status") != "pending":
                    return
                self._sync(project)
            finally:
                self._release_leadership()
            return

    def _heartbeat_loop(self) -> None:
        interval = min(5.0, max(1.0, self.settings.lease_seconds / 3))
        while not self._stop.wait(interval):
            if not self._leader.is_set():
                continue
            try:
                renewed = self.store.renew(LEASE_NAME, self.owner_id, self.settings.lease_seconds)
                if not renewed:
                    LOGGER.warning("embedded synchronization leadership was lost")
                    self._leader.clear()
            except Exception:
                LOGGER.exception("failed to renew embedded synchronization lease")

    def _poll_projects(self) -> None:
        self._reload_configuration_if_changed()
        if not self.settings.enabled:
            return
        now = time.monotonic()
        for project in self._projects():
            if self._stop.is_set():
                break
            current = self._snapshot(project)
            job_id = f"project:{project.id}"
            persisted = self.store.state(job_id)
            retry_at = self._retry_at.get(project.id)
            if persisted.get("status") == "pending" and (
                persisted.get("reason") != "maintenance_busy" or retry_at is None or now >= retry_at
            ):
                self._sync(project)
                self._observed[project.id] = current
                self._dirty_since.pop(project.id, None)
                continue
            if not self.settings.enabled:
                self._observed[project.id] = current
                self._dirty_since.pop(project.id, None)
                continue
            previous = self._observed.get(project.id)
            if previous is None or current != previous:
                self._observed[project.id] = current
                self._dirty_since[project.id] = now
                self.store.patch_state(
                    job_id,
                    {
                        "status": "waiting",
                        "project_id": project.id,
                        "reason": "changes_detected",
                        "detected_files": self._source_file_count(current),
                        "detected_at": datetime.now(UTC).isoformat(),
                    },
                )
            dirty_since = self._dirty_since.get(project.id)
            if (
                dirty_since is not None and now - dirty_since >= self.settings.debounce_seconds
            ) or (retry_at is not None and now >= retry_at):
                self._sync(project)
                self._observed[project.id] = current
                self._dirty_since.pop(project.id, None)

    def _projects(self) -> list[Project]:
        return [
            project for project in self.registry.list() if project.sources_manifest_path is not None
        ]

    def _sync(self, project: Project) -> None:
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        detected_files = self._source_file_count(self._snapshot(project))
        try:
            self.store.update_state(
                f"project:{project.id}",
                {
                    "status": "running",
                    "project_id": project.id,
                    "owner_id": self.owner_id,
                    "started_at": started_at,
                    "detected_files": detected_files,
                },
            )
        except Exception:
            LOGGER.exception("failed to persist running state for %s", project.id)
        try:
            results = self.sync_project(project.id)
            statuses = Counter(result.status for result in results)
            errors = [result.error for result in results if result.error]
            status = "error" if errors else "ok"
            state: dict[str, object] = {
                "status": status,
                "project_id": project.id,
                "owner_id": self.owner_id,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "result_counts": dict(sorted(statuses.items())),
                "chunks_indexed": sum(result.chunks_indexed for result in results),
                "detected_files": detected_files,
                "errors": errors,
            }
        except UpdateLockBusyError:
            status = "pending"
            state = {
                "status": status,
                "project_id": project.id,
                "owner_id": self.owner_id,
                "requested_at": started_at,
                "reason": "maintenance_busy",
                "detected_files": detected_files,
                "result_counts": {},
                "chunks_indexed": 0,
                "errors": [],
            }
        except Exception as exc:
            status = "error"
            state = {
                "status": status,
                "project_id": project.id,
                "owner_id": self.owner_id,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "result_counts": {},
                "chunks_indexed": 0,
                "detected_files": detected_files,
                "errors": [str(exc)],
            }
            LOGGER.exception("embedded synchronization failed for project %s", project.id)
        try:
            self.store.update_state(f"project:{project.id}", state)
        except Exception:
            LOGGER.exception("failed to persist synchronization state for %s", project.id)
        if status in {"error", "pending"}:
            self._retry_at[project.id] = time.monotonic() + self.settings.retry_seconds
        else:
            self._retry_at.pop(project.id, None)
            LOGGER.info(
                "project %s synchronized in %.3fs: %s",
                project.id,
                state["duration_seconds"],
                state["result_counts"],
            )

    @staticmethod
    def _source_file_count(snapshot: Snapshot) -> int:
        return sum(1 for label, *_metadata in snapshot if label != "@manifest")

    def _reload_configuration_if_changed(self) -> None:
        if self.configuration_snapshot is None or self.reload_configuration is None:
            return
        current = self.configuration_snapshot()
        if current == self._observed_configuration:
            return
        was_enabled = self.settings.enabled
        registry, settings, allowed_extensions, sync_project = self.reload_configuration()
        self.registry = registry
        self.settings = settings
        self.allowed_extensions = {value.casefold() for value in allowed_extensions}
        self.sync_project = sync_project
        self._observed_configuration = current
        self._observed.clear()
        self._dirty_since.clear()
        self._retry_at.clear()
        if was_enabled and not settings.enabled and self._leader.is_set():
            self._release_leadership()
        LOGGER.info("project synchronization configuration reloaded")

    def _snapshot(self, project: Project) -> Snapshot:
        entries: list[tuple[str, int, int, int]] = []
        manifest = project.sources_manifest_path
        if manifest is not None:
            entries.append(self._stat_entry(manifest, "@manifest"))
        root = project.documents_path.resolve()
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve()
                try:
                    relative = resolved.relative_to(root)
                except ValueError:
                    continue
                if resolved.suffix.casefold() not in self.allowed_extensions:
                    continue
                entries.append(self._stat_entry(resolved, relative.as_posix()))
        return tuple(entries)

    @staticmethod
    def _stat_entry(path: Path, label: str) -> tuple[str, int, int, int]:
        try:
            stat = path.stat()
        except OSError:
            return (label, -1, -1, -1)
        return (label, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

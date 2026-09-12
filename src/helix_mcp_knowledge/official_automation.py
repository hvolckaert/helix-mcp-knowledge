"""Embedded, leader-elected synchronization of selected official documentation."""

import logging
import os
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from .config import OfficialDocsSettings, WatchSettings
from .errors import OfficialSyncCancelled, SourceSyncError
from .storage.automation import AutomationStore
from .sync.service import SourceSyncResult

LOGGER = logging.getLogger(__name__)
LEASE_NAME = "embedded-official-sync"
JOB_ID = "official-docs"


class OfficialSyncLease:
    """Bounded, heartbeating lease for manual official synchronization."""

    def __init__(
        self,
        store: AutomationStore,
        coordination: WatchSettings,
        *,
        owner_id: str | None = None,
    ) -> None:
        self.store = store
        self.coordination = coordination
        self.owner_id = owner_id or f"manual_{uuid.uuid4()}"
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def __enter__(self) -> "OfficialSyncLease":
        if not self.store.acquire_or_renew(
            LEASE_NAME,
            self.owner_id,
            self.coordination.lease_seconds,
        ):
            raise SourceSyncError("official documentation synchronization is already running")
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="helix-manual-official-sync-heartbeat",
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
            LOGGER.exception("failed to release manual official synchronization lease")

    def cancel_requested(self) -> bool:
        return self._lost.is_set()

    def _heartbeat_loop(self) -> None:
        interval = min(5.0, max(1.0, self.coordination.lease_seconds / 3))
        while not self._stop.wait(interval):
            try:
                if not self.store.renew(
                    LEASE_NAME,
                    self.owner_id,
                    self.coordination.lease_seconds,
                ):
                    self._lost.set()
                    return
            except Exception:
                LOGGER.exception("failed to renew manual official synchronization lease")
                self._lost.set()
                return


class EmbeddedOfficialSyncCoordinator:
    """Refresh configured BMC documentation without an external scheduler."""

    def __init__(
        self,
        *,
        store: AutomationStore,
        settings: OfficialDocsSettings,
        coordination: WatchSettings,
        sync_official: Callable[..., list[SourceSyncResult]],
        official_document_count: Callable[[], int],
        owner_id: str | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.coordination = coordination
        self.sync_official = sync_official
        self.official_document_count = official_document_count
        self.owner_id = owner_id or f"official_{uuid.uuid4()}"
        self._stop = threading.Event()
        self._leader = threading.Event()
        self._worker: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None

    @property
    def is_leader(self) -> bool:
        return self._leader.is_set()

    def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("official synchronization coordinator already started")
        self._worker = threading.Thread(
            target=self._run,
            name="helix-official-sync",
            daemon=True,
        )
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="helix-official-sync-heartbeat",
            daemon=True,
        )
        self._worker.start()
        self._heartbeat.start()

    def run_once(self, *, force: bool = False) -> bool:
        """Run one due synchronization under the cross-process lease.

        Returns ``False`` when another process owns the lease. A detached
        worker uses this method so the synchronization lifetime is not tied
        to the MCP stdio connection that launched it.
        """

        if self._worker is not None or self._heartbeat is not None:
            raise RuntimeError("official synchronization coordinator already started")
        if not self._acquire_leadership():
            return False
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="helix-official-sync-heartbeat",
            daemon=True,
        )
        self._heartbeat.start()
        try:
            if force or self._is_due():
                self._sync()
            return True
        finally:
            self._stop.set()
            self._heartbeat.join(timeout=1.0)
            try:
                self.store.release(LEASE_NAME, self.owner_id)
            except Exception:
                LOGGER.exception("failed to release official synchronization lease")
            self._leader.clear()

    def stop(self, timeout: float = 0.25) -> None:
        self._stop.set()
        self._mark_interrupted()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in (self._heartbeat, self._worker):
            if thread is not None:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        worker_stopped = self._worker is None or not self._worker.is_alive()
        if self._leader.is_set() and worker_stopped:
            try:
                self.store.release(LEASE_NAME, self.owner_id)
            except Exception:
                LOGGER.exception("failed to release official synchronization lease")
            self._leader.clear()

    def _mark_interrupted(self) -> None:
        """Make an owned in-flight run explicitly resumable before process exit."""

        try:
            current = self.store.state(JOB_ID)
            if current.get("status") != "running" or current.get("owner_id") != self.owner_id:
                return
            interrupted = time.time()
            pending = {
                **current,
                "status": "pending",
                "reason": "server_shutdown",
                "interrupted_at": self._timestamp(interrupted),
                "next_run_at": self._timestamp(interrupted),
                "next_run_epoch": interrupted,
            }
            self.store.update_state_if_current(
                JOB_ID,
                owner_id=self.owner_id,
                expected_status="running",
                state=pending,
            )
        except Exception:
            LOGGER.exception("failed to mark official synchronization as pending")

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._leader.is_set():
                self._try_become_leader()
            elif self._is_due():
                self._sync()
            self._stop.wait(self.coordination.poll_seconds)

    def _try_become_leader(self) -> None:
        if not self._acquire_leadership():
            return
        try:
            if not self._stop.is_set() and self._is_due():
                self._sync()
        finally:
            try:
                self.store.release(LEASE_NAME, self.owner_id)
            except Exception:
                LOGGER.exception("failed to release idle official synchronization lease")
            self._leader.clear()

    def _acquire_leadership(self) -> bool:
        try:
            acquired = self.store.acquire_or_renew(
                LEASE_NAME, self.owner_id, self.coordination.lease_seconds
            )
        except Exception:
            LOGGER.exception("failed to acquire official synchronization lease")
            return False
        if not acquired:
            return False
        self._leader.set()
        LOGGER.info("embedded official synchronization leader acquired: %s", self.owner_id)
        return True

    def _heartbeat_loop(self) -> None:
        interval = min(5.0, max(1.0, self.coordination.lease_seconds / 3))
        while not self._stop.wait(interval):
            if not self._leader.is_set():
                continue
            try:
                renewed = self.store.renew(
                    LEASE_NAME, self.owner_id, self.coordination.lease_seconds
                )
                if not renewed:
                    LOGGER.warning("embedded official synchronization leadership was lost")
                    self._leader.clear()
            except Exception:
                LOGGER.exception("failed to renew official synchronization lease")

    def _is_due(self) -> bool:
        state = self.store.state(JOB_ID)
        now = time.time()
        if not state:
            if self.settings.bootstrap_on_empty and self.official_document_count() == 0:
                return True
            if self.official_document_count() > 0:
                return True
            next_run = now + self.settings.interval_hours * 3600
            self.store.update_state(
                JOB_ID,
                {
                    "status": "waiting",
                    "reason": "bootstrap_on_empty is disabled",
                    "next_run_at": self._timestamp(next_run),
                    "next_run_epoch": next_run,
                },
            )
            return False
        if state.get("status") == "running":
            # Only the elected leader evaluates this method. Reaching this
            # branch after acquiring the lease means a previous leader died
            # during synchronization, so the interrupted run must be retried.
            return True
        next_run = state.get("next_run_epoch")
        return not isinstance(next_run, (int, float)) or now >= next_run

    def _sync(self) -> None:
        if self._stop.is_set():
            return
        started = time.time()
        started_at = self._timestamp(started)
        selected_products = {
            product: item.versions for product, item in sorted(self.settings.products.items())
        }
        self.store.update_state(
            JOB_ID,
            {
                "status": "running",
                "owner_id": self.owner_id,
                "process_id": os.getpid(),
                "started_at": started_at,
                "selected_products": selected_products,
                "cancel_requested": False,
                "progress": {
                    "phase": "preparing",
                    "processed_items": 0,
                    "estimated_total_items": 0,
                    "percent": None,
                    "current_product": None,
                    "current_version": None,
                    "last_activity_at": started_at,
                    "result_counts": {},
                    "chunks_indexed": 0,
                    "product_versions": [],
                },
            },
        )
        if self._stop.is_set():
            self._mark_interrupted()
            return
        try:
            results = self.sync_official(
                progress_callback=self._record_progress,
                cancel_check=self._cancel_requested,
            )
            statuses = Counter(result.status for result in results)
            errors = [
                result.error for result in results if result.status == "error" and result.error
            ]
            status = "error" if errors else "ok"
            delay = (
                self.coordination.retry_seconds
                if status == "error"
                else self.settings.interval_hours * 3600
            )
            finished = time.time()
            state: dict[str, object] = {
                "status": status,
                "owner_id": self.owner_id,
                "process_id": os.getpid(),
                "started_at": started_at,
                "finished_at": self._timestamp(finished),
                "duration_seconds": round(finished - started, 3),
                "result_counts": dict(sorted(statuses.items())),
                "chunks_indexed": sum(result.chunks_indexed for result in results),
                "errors": errors,
                "selected_products": selected_products,
                "cancel_requested": False,
                "next_run_at": self._timestamp(finished + delay),
                "next_run_epoch": finished + delay,
            }
            progress = self.store.state(JOB_ID).get("progress")
            if isinstance(progress, dict):
                state["progress"] = {**progress, "phase": "finishing"}
        except OfficialSyncCancelled:
            finished = time.time()
            current = self.store.state(JOB_ID)
            user_requested = current.get("cancel_requested") is True
            progress = current.get("progress")
            counts = progress.get("result_counts", {}) if isinstance(progress, dict) else {}
            chunks = progress.get("chunks_indexed", 0) if isinstance(progress, dict) else 0
            delay = self.settings.interval_hours * 3600 if user_requested else 0
            state = {
                "status": "cancelled" if user_requested else "pending",
                "reason": "user_requested" if user_requested else "leadership_lost",
                "owner_id": self.owner_id,
                "process_id": os.getpid(),
                "started_at": started_at,
                "finished_at": self._timestamp(finished),
                "duration_seconds": round(finished - started, 3),
                "result_counts": counts if isinstance(counts, dict) else {},
                "chunks_indexed": chunks if isinstance(chunks, int) else 0,
                "errors": [],
                "selected_products": selected_products,
                "cancel_requested": False,
                "next_run_at": self._timestamp(finished + delay),
                "next_run_epoch": finished + delay,
            }
            if isinstance(progress, dict):
                state["progress"] = progress
        except Exception as exc:
            finished = time.time()
            state = {
                "status": "error",
                "owner_id": self.owner_id,
                "process_id": os.getpid(),
                "started_at": started_at,
                "finished_at": self._timestamp(finished),
                "duration_seconds": round(finished - started, 3),
                "result_counts": {},
                "chunks_indexed": 0,
                "errors": [str(exc)],
                "next_run_at": self._timestamp(finished + self.coordination.retry_seconds),
                "next_run_epoch": finished + self.coordination.retry_seconds,
            }
            LOGGER.exception("embedded official synchronization failed")
        try:
            updated = self.store.update_state_if_current(
                JOB_ID,
                owner_id=self.owner_id,
                expected_status="running",
                state=state,
            )
            if not updated:
                LOGGER.info(
                    "official synchronization result discarded because its run is no longer active"
                )
        except Exception:
            LOGGER.exception("failed to persist official synchronization state")

    def _record_progress(self, progress: dict[str, object]) -> None:
        try:
            values: dict[str, object] = {"progress": progress}
            if isinstance(progress.get("result_counts"), dict):
                values["result_counts"] = progress["result_counts"]
            if isinstance(progress.get("chunks_indexed"), int):
                values["chunks_indexed"] = progress["chunks_indexed"]
            self.store.patch_state_if_current(
                JOB_ID,
                owner_id=self.owner_id,
                expected_status="running",
                values=values,
            )
        except Exception:
            LOGGER.exception("failed to persist official synchronization progress")

    def _cancel_requested(self) -> bool:
        if self._stop.is_set() or not self._leader.is_set():
            return True
        try:
            current = self.store.state(JOB_ID)
        except Exception:
            LOGGER.exception("failed to inspect official synchronization cancellation state")
            return False
        return (
            current.get("status") != "running"
            or current.get("owner_id") != self.owner_id
            or current.get("cancel_requested") is True
        )

    @staticmethod
    def _timestamp(value: float) -> str:
        return datetime.fromtimestamp(value, UTC).isoformat()

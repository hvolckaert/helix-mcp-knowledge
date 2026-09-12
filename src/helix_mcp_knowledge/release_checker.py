"""Embedded, leader-elected checks for newer private GitHub releases."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from . import __version__
from .config import UpdateSettings
from .models.update import UpdateStatus
from .openclaw import CommandRunner, _resolve_command, _run
from .storage.automation import AutomationStore
from .updater import _normalize_version, _version_tuple

LOGGER = logging.getLogger(__name__)
LEASE_NAME = "release-update-check"
JOB_ID = "release-update"
STARTUP_DELAY_SECONDS = 5.0
VALID_STATUSES = frozenset({"disabled", "unknown", "checking", "current", "available", "error"})


class ReleaseUpdateChecker:
    """Persist a cached release status without ever installing an update."""

    def __init__(
        self,
        *,
        store: AutomationStore,
        settings: UpdateSettings,
        current_version: str = __version__,
        runner: CommandRunner = subprocess.run,
        clock: Callable[[], float] = time.time,
        owner_id: str | None = None,
        startup_delay_seconds: float = STARTUP_DELAY_SECONDS,
    ) -> None:
        self.store = store
        self.settings = settings
        self.current_version = _normalize_version(current_version)
        self.runner = runner
        self.clock = clock
        self.owner_id = owner_id or f"update_check_{uuid.uuid4()}"
        self.startup_delay_seconds = max(0.0, startup_delay_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mutex = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="helix-release-update-check",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 0.25) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
            if not self._thread.is_alive():
                self._thread = None

    def status(self) -> UpdateStatus:
        if not self.settings.enabled:
            return self._response({"status": "disabled"})
        state = self.store.state(JOB_ID)
        return self._response(state or {"status": "unknown"})

    def check(self, *, force: bool = False) -> UpdateStatus:
        if not self.settings.enabled:
            return self.status()
        if not self._mutex.acquire(blocking=False):
            return self.status()
        try:
            if not force and not self._is_due():
                return self.status()
            lease_seconds = max(self.settings.timeout_seconds + 30, 60)
            if not self.store.acquire_or_renew(LEASE_NAME, self.owner_id, lease_seconds):
                return self.status()
            try:
                if not force and not self._is_due():
                    return self.status()
                return self._perform_check()
            finally:
                self.store.release(LEASE_NAME, self.owner_id)
        finally:
            self._mutex.release()

    def _run_loop(self) -> None:
        if self._stop.wait(self.startup_delay_seconds):
            return
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                LOGGER.exception("embedded release update check failed")
            self._stop.wait(self._wait_seconds())

    def _is_due(self) -> bool:
        next_check = self.store.state(JOB_ID).get("next_check_epoch")
        return not isinstance(next_check, (int, float)) or self.clock() >= next_check

    def _wait_seconds(self) -> float:
        next_check = self.store.state(JOB_ID).get("next_check_epoch")
        if not isinstance(next_check, (int, float)):
            return 1.0
        return min(300.0, max(1.0, next_check - self.clock()))

    def _perform_check(self) -> UpdateStatus:
        started = self.clock()
        previous = self.store.state(JOB_ID)
        self.store.update_state(
            JOB_ID,
            {
                **self._known_release(previous),
                "status": "checking",
                "owner_id": self.owner_id,
                "started_at": self._timestamp(started),
            },
        )
        try:
            gh_command = _resolve_command(self.settings.gh_command, label="GitHub CLI")
            completed = _run(
                [
                    str(gh_command),
                    "release",
                    "view",
                    "--repo",
                    self.settings.repository,
                    "--json",
                    "tagName,isDraft,isPrerelease,publishedAt,url",
                ],
                runner=self.runner,
                timeout=self.settings.timeout_seconds,
                action="GitHub release update check",
            )
            payload = json.loads(completed.stdout)
            if not isinstance(payload, dict):
                raise ValueError("GitHub release metadata must be a JSON object")
            if payload.get("isDraft") or payload.get("isPrerelease"):
                raise ValueError("GitHub latest release is not stable")
            latest = _normalize_version(str(payload.get("tagName", "")))
            available = _version_tuple(latest) > _version_tuple(self.current_version)
            checked = self.clock()
            state: dict[str, object] = {
                "status": "available" if available else "current",
                "latest_version": latest,
                "update_available": available,
                "release_url": str(payload.get("url") or "") or None,
                "published_at": str(payload.get("publishedAt") or "") or None,
                "checked_at": self._timestamp(checked),
                "next_check_at": self._timestamp(checked + self.settings.interval_hours * 3600),
                "next_check_epoch": checked + self.settings.interval_hours * 3600,
            }
        except Exception as exc:
            checked = self.clock()
            state = {
                **self._known_release(previous),
                "status": "error",
                "checked_at": self._timestamp(checked),
                "next_check_at": self._timestamp(checked + self.settings.retry_minutes * 60),
                "next_check_epoch": checked + self.settings.retry_minutes * 60,
                "error": str(exc)[:1000],
            }
        self.store.update_state(JOB_ID, state)
        return self._response(state)

    def _response(self, state: dict[str, object]) -> UpdateStatus:
        latest = state.get("latest_version")
        latest_version = latest if isinstance(latest, str) else None
        available = (
            _version_tuple(latest_version) > _version_tuple(self.current_version)
            if latest_version
            else None
        )
        raw_status = str(state.get("status", "unknown"))
        status = raw_status if raw_status in VALID_STATUSES else "unknown"
        return UpdateStatus(
            status=status,
            repository=self.settings.repository,
            current_version=self.current_version,
            latest_version=latest_version,
            update_available=available,
            release_url=self._optional_string(state.get("release_url")),
            published_at=self._optional_string(state.get("published_at")),
            checked_at=self._optional_string(state.get("checked_at")),
            next_check_at=self._optional_string(state.get("next_check_at")),
            error=self._optional_string(state.get("error")),
        )

    @staticmethod
    def _known_release(state: dict[str, object]) -> dict[str, object]:
        return {
            key: state[key]
            for key in ("latest_version", "release_url", "published_at")
            if key in state
        }

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _timestamp(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat()

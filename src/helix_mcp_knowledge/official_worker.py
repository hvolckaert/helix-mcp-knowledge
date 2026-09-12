"""Detached one-shot worker for automatic official documentation refreshes."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .logging import configure_logging
from .workspace import CONFIG_ENV, discover_config_path

LOGGER = logging.getLogger(__name__)


class _Coordinator(Protocol):
    def start(self) -> None: ...

    def stop(self, timeout: float = 0.25) -> None: ...


def _dashboard_coordinator(config_path: Path) -> _Coordinator | None:
    """Build a fresh coordinator after each dashboard configuration change."""

    from .application import KnowledgeApplication

    application = KnowledgeApplication.from_config(config_path)
    settings = application.config.official_docs
    cleanup_only = not settings.products and not settings.retain_unselected_versions
    if not settings.automatic_sync or (not settings.products and not cleanup_only):
        return None
    return application.create_official_sync_coordinator()


def _dashboard_configuration_signature(
    config_path: Path,
) -> tuple[tuple[str, int, int], ...]:
    from .config import load_config

    config = load_config(config_path)
    paths = (
        config_path,
        config.official_manifest_path,
        config.official_catalog_cache_path,
    )
    entries: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            entries.append((str(path), -1, -1))
        else:
            entries.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


@dataclass(frozen=True)
class OfficialSyncWorkerProcess:
    pid: int


class OfficialSyncWorkerLauncher:
    """Start a worker that outlives the MCP stdio process that requested it."""

    def __init__(
        self,
        *,
        config_path: Path,
        workspace: Path,
        errors_path: Path,
        python_executable: str | None = None,
    ) -> None:
        self.config_path = config_path.resolve()
        self.workspace = workspace.resolve()
        self.errors_path = errors_path.resolve()
        self.python_executable = python_executable or sys.executable
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, *, force: bool = False) -> OfficialSyncWorkerProcess:
        if self._process is not None:
            raise RuntimeError("official synchronization worker already launched")
        self.errors_path.mkdir(parents=True, exist_ok=True)
        log_path = self.errors_path / "official-sync-worker.log"
        environment = os.environ.copy()
        environment[CONFIG_ENV] = str(self.config_path)
        environment["PYTHONUNBUFFERED"] = "1"
        kwargs: dict[str, object] = {
            "cwd": self.workspace,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":  # pragma: no cover - exercised on Windows installations
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        with log_path.open("ab", buffering=0) as log:
            self._process = subprocess.Popen(
                [
                    self.python_executable,
                    "-m",
                    "helix_mcp_knowledge.official_worker",
                    "--config",
                    str(self.config_path),
                    *(["--force"] if force else []),
                ],
                stderr=log,
                **kwargs,
            )
        threading.Thread(
            target=self._process.wait,
            name="helix-official-sync-worker-reaper",
            daemon=True,
        ).start()
        return OfficialSyncWorkerProcess(pid=self._process.pid)


class PersistentOfficialSyncScheduler:
    """Keep official scheduling alive with the persistent dashboard process.

    The active coordinator is rebuilt when the main configuration is atomically
    replaced, so dashboard changes take effect without restarting the service.
    MCP startup workers remain a fallback and share the same SQLite lease.
    """

    def __init__(
        self,
        config_path: Path,
        *,
        coordinator_factory=_dashboard_coordinator,
        signature_factory=_dashboard_configuration_signature,
        poll_seconds: float = 1.0,
    ) -> None:
        self.config_path = config_path.expanduser().resolve()
        self.coordinator_factory = coordinator_factory
        self.signature_factory = signature_factory
        self.poll_seconds = max(0.05, poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._coordinator: _Coordinator | None = None
        self._signature: tuple[tuple[str, int, int], ...] | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("persistent official synchronization scheduler already started")
        self._thread = threading.Thread(
            target=self._run,
            name="helix-dashboard-official-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 0.5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    signature = self.signature_factory(self.config_path)
                    if signature != self._signature:
                        replacement = self.coordinator_factory(self.config_path)
                        if self._coordinator is not None:
                            self._coordinator.stop(timeout=1.0)
                        self._coordinator = replacement
                        if replacement is not None:
                            replacement.start()
                        self._signature = signature
                except Exception:
                    LOGGER.exception("failed to refresh persistent official scheduler")
                self._stop.wait(self.poll_seconds)
        finally:
            if self._coordinator is not None:
                self._coordinator.stop(timeout=1.0)
                self._coordinator = None


def run_worker(config_path: str | Path | None = None, *, force: bool = False) -> bool:
    from .application import KnowledgeApplication

    resolved = discover_config_path(config_path)
    application = KnowledgeApplication.from_config(resolved)
    configure_logging(application.config.logging.level)
    settings = application.config.official_docs
    cleanup_only = not settings.products and not settings.retain_unselected_versions
    if (not settings.products and not cleanup_only) or (not settings.automatic_sync and not force):
        return False
    coordinator = application.create_official_sync_coordinator()
    return coordinator.run_once(force=force)


def main() -> int:
    parser = argparse.ArgumentParser(prog="helix-mcp-knowledge-worker")
    parser.add_argument("--config", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    run_worker(args.config, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

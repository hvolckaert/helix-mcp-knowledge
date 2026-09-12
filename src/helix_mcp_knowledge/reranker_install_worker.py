"""Detached installer for the optional managed result-reranking component."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .component_install_recovery import operation_is_current
from .config import AppConfig, load_config
from .reranker_component import RerankerComponentManager
from .storage.automation import AutomationStore
from .storage.database import Database
from .update_lock import UpdateLock, UpdateLockBusyError

RERANKER_INSTALL_JOB = "reranker-component-install"


@dataclass(frozen=True)
class RerankerInstallWorkerProcess:
    pid: int


class RerankerInstallWorkerLauncher:
    def __init__(
        self,
        *,
        config_path: Path,
        workspace: Path,
        errors_path: Path,
        python_executable: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.config_path = config_path.resolve()
        self.workspace = workspace.resolve()
        self.errors_path = errors_path.resolve()
        self.python_executable = python_executable or sys.executable
        self.owner_id = owner_id or f"reranker_install_{uuid.uuid4().hex}"
        if len(self.owner_id) > 128:
            raise ValueError("reranker installation owner identifier is too long")
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> RerankerInstallWorkerProcess:
        if self._process is not None:
            raise RuntimeError("reranker installation worker already launched")
        self.errors_path.mkdir(parents=True, exist_ok=True)
        log_path = self.errors_path / "reranker-install-worker.log"
        kwargs: dict[str, object] = {
            "cwd": self.workspace,
            "env": {**os.environ, "PYTHONUNBUFFERED": "1"},
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":  # pragma: no cover - exercised by Windows CI
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
                    "helix_mcp_knowledge.reranker_install_worker",
                    "--config",
                    str(self.config_path),
                    "--owner-id",
                    self.owner_id,
                ],
                stderr=log,
                **kwargs,
            )
        threading.Thread(
            target=self._process.wait,
            name="helix-reranker-install-reaper",
            daemon=True,
        ).start()
        return RerankerInstallWorkerProcess(pid=self._process.pid)


def _set_enabled(config_path: Path, enabled: bool) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    payload.setdefault("retrieval", {}).setdefault("reranker", {})["enabled"] = enabled
    serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, config_path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def run_install(
    config_path: str | Path,
    *,
    lock_timeout_seconds: float = 30.0,
    owner_id: str | None = None,
) -> bool:
    path = Path(config_path).expanduser().resolve()
    config = load_config(path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    try:
        with UpdateLock(config.base_dir / ".update.lock", timeout_seconds=lock_timeout_seconds):
            if owner_id is not None and not operation_is_current(
                store,
                RERANKER_INSTALL_JOB,
                owner_id,
            ):
                return True
            return _run_install_locked(path, config, store)
    except UpdateLockBusyError as exc:
        values = {
            "status": "error",
            "desired_enabled": False,
            "finished_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
        }
        if owner_id is None:
            _set_enabled(path, False)
            store.update_state(RERANKER_INSTALL_JOB, values)
        elif store.patch_state_if_current(
            RERANKER_INSTALL_JOB,
            expected_status="installing",
            owner_id=owner_id,
            values=values,
        ):
            _set_enabled(path, False)
        return False


def _run_install_locked(
    config_path: Path,
    config: AppConfig,
    store: AutomationStore,
) -> bool:
    current = store.patch_state(
        RERANKER_INSTALL_JOB,
        {
            "status": "installing",
            "started_at": datetime.now(UTC).isoformat(),
            "error": None,
        },
        defaults={"desired_enabled": True},
    )
    if current.get("desired_enabled") is False:
        _set_enabled(config_path, False)
        store.patch_state(
            RERANKER_INSTALL_JOB,
            {
                "status": "not_installed",
                "cancelled": True,
                "finished_at": datetime.now(UTC).isoformat(),
            },
        )
        return True
    manager = RerankerComponentManager(config)
    try:
        component = manager.install()
        if not component.installed or component.status != "ready" or not component.service_ready:
            raise RuntimeError("reranker component validation did not complete")
        service_running = True
        component_values = {
            "component_version": component.component_version,
            "installed_bytes": component.installed_bytes,
            "model_bytes": component.model_bytes,
        }
        while True:
            prepared = store.patch_state(
                RERANKER_INSTALL_JOB,
                component_values,
                defaults={"desired_enabled": True},
            )
            desired_enabled = prepared.get("desired_enabled") is not False
            if desired_enabled != service_running:
                if desired_enabled:
                    manager.ensure_service()
                else:
                    manager.stop_service()
                service_running = desired_enabled
            _set_enabled(config_path, desired_enabled)
            completed = store.patch_state(
                RERANKER_INSTALL_JOB,
                {
                    **component_values,
                    "status": "ready",
                    "service_status": "ready" if desired_enabled else "stopped",
                    "service_error": None,
                    "cancelled": not desired_enabled,
                    "finished_at": datetime.now(UTC).isoformat(),
                },
            )
            if (completed.get("desired_enabled") is not False) == desired_enabled:
                break
            # A dashboard request changed the desired state during the final
            # transition. Keep it cancellable while reconciling the service.
            store.patch_state(RERANKER_INSTALL_JOB, {"status": "installing"})
        return True
    except Exception as exc:
        _set_enabled(config_path, False)
        with suppress(Exception):
            manager.stop_service()
        store.update_state(
            RERANKER_INSTALL_JOB,
            {
                "status": "error",
                "desired_enabled": False,
                "finished_at": datetime.now(UTC).isoformat(),
                "error": str(exc)[:2000],
            },
        )
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--owner-id")
    args = parser.parse_args()
    return 0 if run_install(args.config, owner_id=args.owner_id) else 1


if __name__ == "__main__":
    raise SystemExit(main())

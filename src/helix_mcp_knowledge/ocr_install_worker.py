"""Detached installation worker for the optional managed OCR component."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .config import load_config
from .ocr_component import OcrComponentManager
from .storage.automation import AutomationStore
from .storage.database import Database
from .update_lock import UpdateLock, UpdateLockBusyError

OCR_INSTALL_JOB = "ocr-component-install"


@dataclass(frozen=True)
class OcrInstallWorkerProcess:
    pid: int


class OcrInstallWorkerLauncher:
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

    def start(self) -> OcrInstallWorkerProcess:
        if self._process is not None:
            raise RuntimeError("OCR installation worker already launched")
        self.errors_path.mkdir(parents=True, exist_ok=True)
        log_path = self.errors_path / "ocr-install-worker.log"
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
                    "helix_mcp_knowledge.ocr_install_worker",
                    "--config",
                    str(self.config_path),
                ],
                stderr=log,
                **kwargs,
            )
        threading.Thread(
            target=self._process.wait,
            name="helix-ocr-install-reaper",
            daemon=True,
        ).start()
        return OcrInstallWorkerProcess(pid=self._process.pid)


def _set_enabled(config_path: Path, enabled: bool) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    payload.setdefault("ingestion", {}).setdefault("ocr", {})["enabled"] = enabled
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


def run_install(config_path: str | Path, *, lock_timeout_seconds: float = 30.0) -> bool:
    path = Path(config_path).expanduser().resolve()
    config = load_config(path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    try:
        with UpdateLock(
            config.base_dir / ".update.lock",
            timeout_seconds=lock_timeout_seconds,
        ):
            return _run_install_locked(path, config, store)
    except UpdateLockBusyError as exc:
        store.update_state(
            OCR_INSTALL_JOB,
            {
                "status": "error",
                "desired_enabled": False,
                "finished_at": datetime.now(UTC).isoformat(),
                "error": str(exc),
            },
        )
        return False


def _run_install_locked(config_path: Path, config, store: AutomationStore) -> bool:
    current = store.state(OCR_INSTALL_JOB)
    state = {
        **current,
        "status": "installing",
        "started_at": datetime.now(UTC).isoformat(),
    }
    store.update_state(OCR_INSTALL_JOB, state)
    try:
        component = OcrComponentManager(config).install()
        desired_enabled = store.state(OCR_INSTALL_JOB).get("desired_enabled") is not False
        _set_enabled(config_path, desired_enabled)
        store.update_state(
            OCR_INSTALL_JOB,
            {
                "status": "ready",
                "desired_enabled": desired_enabled,
                "finished_at": datetime.now(UTC).isoformat(),
                "component_version": component.component_version,
                "installed_bytes": component.installed_bytes,
            },
        )
        return True
    except Exception as exc:
        _set_enabled(config_path, False)
        store.update_state(
            OCR_INSTALL_JOB,
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
    args = parser.parse_args()
    return 0 if run_install(args.config) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Isolated, transactional installation and invocation of optional PDF OCR."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import venv
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .component_requirements import (
    OCR_COMPONENT_REQUIREMENTS,
    PIP_BOOTSTRAP_REQUIREMENTS,
    locked_component_requirements,
)
from .config import AppConfig, OcrSettings
from .errors import ConfigurationError, IngestionError

OCR_COMPONENT_VERSION = 1
OCR_PACKAGES = (
    "onnxruntime==1.29.0",
    "pypdfium2==5.13.0",
    "rapidocr==3.9.2",
)
ESTIMATED_INSTALL_BYTES = 500 * 1024 * 1024
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class OcrComponentStatus:
    installed: bool
    status: str
    component_version: int | None = None
    installed_bytes: int | None = None
    installed_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "estimated_install_bytes": ESTIMATED_INSTALL_BYTES,
        }


@dataclass(frozen=True)
class OcrPage:
    page_number: int
    text: str
    confidence: float | None


class OcrComponentManager:
    """Install OCR into an isolated component venv and atomically activate it."""

    def __init__(
        self,
        config: AppConfig,
        *,
        python_executable: str | Path | None = None,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self.config = config
        self.root = config.ocr_component_path
        self.python_executable = str(python_executable or sys.executable)
        self.runner = runner

    @property
    def metadata_path(self) -> Path:
        return self.root / "current.json"

    def status(self) -> OcrComponentStatus:
        try:
            metadata = self._metadata()
            if metadata is None:
                return OcrComponentStatus(installed=False, status="not_installed")
            runtime = self._runtime_path(metadata)
            python = self._venv_python(runtime)
            if not python.is_file():
                raise ValueError("component Python executable is missing")
            return OcrComponentStatus(
                installed=True,
                status="ready",
                component_version=int(metadata["component_version"]),
                installed_bytes=int(metadata.get("installed_bytes") or 0),
                installed_at=str(metadata.get("installed_at") or "") or None,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return OcrComponentStatus(installed=False, status="error", error=str(exc))

    def install(self) -> OcrComponentStatus:
        current = self.status()
        if current.installed and current.component_version == OCR_COMPONENT_VERSION:
            return current
        runtime_parent = self.root / "runtime"
        runtime_parent.mkdir(parents=True, exist_ok=True)
        runtime = runtime_parent / f"ocr-{OCR_COMPONENT_VERSION}-{uuid.uuid4().hex}"
        try:
            venv.EnvBuilder(with_pip=True, clear=False, symlinks=os.name != "nt").create(runtime)
            python = self._venv_python(runtime)
            bootstrap_requirements = locked_component_requirements(PIP_BOOTSTRAP_REQUIREMENTS)
            component_requirements = locked_component_requirements(OCR_COMPONENT_REQUIREMENTS)
            self._run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-cache-dir",
                    "--require-hashes",
                    "--requirement",
                    str(bootstrap_requirements),
                ],
                timeout=300,
                action="component installer bootstrap",
            )
            self._run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-cache-dir",
                    "--require-hashes",
                    "--requirement",
                    str(component_requirements),
                ],
                timeout=1200,
                action="OCR dependency installation",
            )
            self._run(
                [str(python), "-m", "helix_mcp_knowledge.ocr_worker", "--self-test"],
                timeout=120,
                action="OCR component smoke test",
                env=self._worker_environment(),
            )
            metadata = {
                "schema_version": 1,
                "component_version": OCR_COMPONENT_VERSION,
                "runtime": runtime.relative_to(self.root).as_posix(),
                "packages": list(OCR_PACKAGES),
                "installed_at": datetime.now(UTC).isoformat(),
                "installed_bytes": self._directory_size(runtime),
            }
            self._atomic_json_write(self.metadata_path, metadata)
            return self.status()
        except Exception:
            self._remove_failed_runtime(runtime, runtime_parent)
            raise

    def command(self) -> list[str]:
        metadata = self._metadata()
        if metadata is None:
            raise ConfigurationError("OCR support is not installed")
        runtime = self._runtime_path(metadata)
        python = self._venv_python(runtime)
        if not python.is_file():
            raise ConfigurationError("OCR component Python executable is missing")
        return [str(python), "-m", "helix_mcp_knowledge.ocr_worker"]

    def _metadata(self) -> dict[str, object] | None:
        if not self.metadata_path.is_file() or self.metadata_path.is_symlink():
            return None
        payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("OCR component metadata is invalid")
        version = payload.get("component_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("OCR component version is invalid")
        return payload

    def _runtime_path(self, metadata: dict[str, object]) -> Path:
        value = metadata.get("runtime")
        if not isinstance(value, str) or not value:
            raise ValueError("OCR component runtime is invalid")
        runtime = (self.root / value).resolve()
        try:
            runtime.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("OCR component runtime escapes its managed root") from exc
        if not runtime.is_dir():
            raise ValueError("OCR component runtime directory is missing")
        return runtime

    @staticmethod
    def _venv_python(runtime: Path) -> Path:
        return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    @staticmethod
    def _worker_environment() -> dict[str, str]:
        package_root = str(Path(__file__).resolve().parent.parent)
        existing = os.environ.get("PYTHONPATH")
        python_path = package_root if not existing else package_root + os.pathsep + existing
        return {**os.environ, "PYTHONPATH": python_path}

    def _run(
        self,
        command: list[str],
        *,
        timeout: int,
        action: str,
        env: dict[str, str] | None = None,
    ) -> None:
        completed = self.runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
            raise ConfigurationError(f"{action} failed: {detail[-2000:]}")

    @staticmethod
    def _directory_size(path: Path) -> int:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    @staticmethod
    def _atomic_json_write(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _remove_failed_runtime(runtime: Path, runtime_parent: Path) -> None:
        if not runtime.exists():
            return
        resolved = runtime.resolve()
        if (
            runtime.is_symlink()
            or resolved.parent != runtime_parent.resolve()
            or not resolved.name.startswith(f"ocr-{OCR_COMPONENT_VERSION}-")
        ):
            raise ConfigurationError("refusing to clean an unexpected OCR runtime path")
        shutil.rmtree(resolved)


class OcrClient:
    """Invoke the isolated OCR worker for selected PDF pages."""

    def __init__(self, config: AppConfig, *, runner: CommandRunner = subprocess.run) -> None:
        self.settings: OcrSettings = config.ingestion.ocr
        self.manager = OcrComponentManager(config)
        self.runner = runner

    def extract(self, path: Path, page_numbers: list[int]) -> dict[int, OcrPage]:
        if not self.settings.enabled:
            return {}
        if len(page_numbers) > self.settings.max_pages_per_document:
            raise IngestionError(
                "PDF requires OCR on more pages than the configured safety limit "
                f"({self.settings.max_pages_per_document})"
            )
        command = [
            *self.manager.command(),
            "--pdf",
            str(path),
            "--pages",
            ",".join(str(number) for number in page_numbers),
            "--dpi",
            str(self.settings.dpi),
            "--min-confidence",
            str(self.settings.min_confidence),
        ]
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.settings.timeout_seconds,
                check=False,
                env=self.manager._worker_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IngestionError(f"OCR worker could not complete: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "OCR worker failed").strip()
            raise IngestionError(f"OCR worker failed: {detail[-1000:]}")
        try:
            payload = json.loads(completed.stdout)
            raw_pages = payload["pages"]
            if not isinstance(raw_pages, dict):
                raise TypeError("pages must be an object")
            return {
                int(number): OcrPage(
                    page_number=int(number),
                    text=str(item.get("text") or ""),
                    confidence=(
                        float(item["confidence"]) if item.get("confidence") is not None else None
                    ),
                )
                for number, item in raw_pages.items()
                if isinstance(item, dict)
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IngestionError("OCR worker returned an invalid response") from exc

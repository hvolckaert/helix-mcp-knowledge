"""Transactional lifecycle for the optional, local result-reranking component."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import venv
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .component_install_recovery import CandidateCleanupResult, candidate_is_old_enough
from .component_requirements import (
    PIP_BOOTSTRAP_REQUIREMENTS,
    RERANKER_COMPONENT_REQUIREMENTS,
    locked_component_requirements,
)
from .config import AppConfig
from .errors import ConfigurationError
from .managed_installation import installation_metadata_path
from .reranker_client import (
    RERANKER_COMPONENT_VERSION,
    RERANKER_MODEL_ID,
    RERANKER_MODEL_REVISION,
    RerankerServiceClient,
)
from .update_lock import UpdateLock

RERANKER_HOST = "127.0.0.1"
RERANKER_PORT = 8768
RERANKER_REQUEST_TIMEOUT_SECONDS = 60.0
RERANKER_MAX_SEQUENCE_LENGTH = 256
RERANKER_BATCH_SIZE = 4
RERANKER_CPU_THREADS = max(1, min(8, os.cpu_count() or 1))
RERANKER_TORCH_PACKAGE = "torch==2.14.0"
RERANKER_TORCH_FIND_LINKS = "https://download.pytorch.org/whl/cpu/torch/"
RERANKER_PACKAGES = ("transformers==5.16.1",)
RERANKER_SERVICE_LOCK_TIMEOUT_SECONDS = 195.0
RERANKER_MODEL_FILES = (
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
ESTIMATED_INSTALL_BYTES = 5 * 1024 * 1024 * 1024
_RUNTIME_CANDIDATE_PATTERN = re.compile(r"reranker-[1-9][0-9]*-[0-9a-f]{32}")
_MODEL_CANDIDATE_PATTERN = re.compile(r"bge-reranker-v2-m3-[0-9a-f]{32}")
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class RerankerComponentStatus:
    installed: bool
    status: str
    removable: bool = False
    component_version: int | None = None
    installed_bytes: int | None = None
    model_bytes: int | None = None
    installed_at: str | None = None
    service_ready: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "estimated_install_bytes": ESTIMATED_INSTALL_BYTES}


class RerankerComponentManager:
    """Install and run the fixed reranker in an isolated managed directory."""

    def __init__(
        self,
        config: AppConfig,
        *,
        python_executable: str | Path | None = None,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self.config = config
        self.root = config.reranker_component_path
        self.python_executable = str(python_executable or sys.executable)
        self.runner = runner
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def metadata_path(self) -> Path:
        return self.root / "current.json"

    @property
    def pending_metadata_path(self) -> Path:
        return self.root / "pending.json"

    @property
    def service_control_path(self) -> Path:
        return self.root / "service-control.json"

    @property
    def service_stop_path(self) -> Path:
        return self.root / "service.stop"

    @property
    def service_lock_path(self) -> Path:
        return self.root / ".service.lock"

    def status(self, *, check_service: bool = False) -> RerankerComponentStatus:
        try:
            metadata = self._metadata()
            if metadata is None:
                removable_bytes = self.removable_storage_bytes()
                return RerankerComponentStatus(
                    installed=False,
                    status="not_installed",
                    removable=removable_bytes is not None,
                    installed_bytes=removable_bytes,
                )
            runtime = self._managed_path(metadata, "runtime")
            model = self._managed_path(metadata, "model_path")
            if not self._venv_python(runtime).is_file() or not model.is_dir():
                raise ValueError("reranker runtime or model is missing")
            component_version = self._component_version(metadata)
            exact_component = (
                component_version == RERANKER_COMPONENT_VERSION
                and metadata.get("model_id") == RERANKER_MODEL_ID
                and metadata.get("model_revision") == RERANKER_MODEL_REVISION
                and metadata.get("inference_runtime") == RERANKER_TORCH_PACKAGE
                and metadata.get("packages") == list(RERANKER_PACKAGES)
            )
            service_ready = (
                self.client(metadata).health() if check_service and exact_component else False
            )
            status = "ready"
            error = None
            if component_version < RERANKER_COMPONENT_VERSION:
                status = "update_required"
                error = (
                    "The installed reranker must be updated; the standard ranking remains active."
                )
            elif component_version > RERANKER_COMPONENT_VERSION:
                status = "incompatible"
                error = (
                    "The installed reranker belongs to a newer server runtime; "
                    "the standard ranking remains active."
                )
            elif not exact_component:
                status = "update_required"
                error = (
                    "The installed reranker runtime or model does not match the pinned release; "
                    "the standard ranking remains active."
                )
            elif check_service and not service_ready:
                status = "degraded"
                error = "The reranker is not responding; the standard ranking remains active."
            return RerankerComponentStatus(
                installed=True,
                status=status,
                removable=True,
                component_version=component_version,
                installed_bytes=int(
                    metadata.get("installed_bytes")
                    or self._directory_size(runtime) + self._directory_size(model)
                ),
                model_bytes=int(metadata.get("model_bytes") or self._directory_size(model)),
                installed_at=str(metadata.get("installed_at") or "") or None,
                service_ready=service_ready,
                error=error,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            removable_bytes = self.removable_storage_bytes()
            return RerankerComponentStatus(
                installed=False,
                status="error",
                removable=removable_bytes is not None,
                installed_bytes=removable_bytes,
                error=str(exc),
            )

    def install(self) -> RerankerComponentStatus:
        current = self.status()
        if current.installed and current.status == "ready":
            self.ensure_service()
            return self.status(check_service=True)

        previous_metadata = self._usable_previous_metadata()
        runtime_parent = self.root / "runtime"
        model_parent = self.root / "models"
        runtime_parent.mkdir(parents=True, exist_ok=True)
        model_parent.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex
        runtime = runtime_parent / f"reranker-{RERANKER_COMPONENT_VERSION}-{suffix}"
        model = model_parent / f"bge-reranker-v2-m3-{suffix}"
        candidate_client: RerankerServiceClient | None = None
        metadata: dict[str, object] | None = None
        previous_stopped = False
        try:
            if not self._service_start_requested():
                raise ConfigurationError("reranker component installation was cancelled")
            venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(runtime)
            python = self._venv_python(runtime)
            self._install_dependencies(python)
            if not self._service_start_requested():
                raise ConfigurationError("reranker component installation was cancelled")
            self._download_model(python, model)
            if not self._service_start_requested():
                raise ConfigurationError("reranker component installation was cancelled")
            self._remove_download_metadata(model)
            self._validate_model(model)
            self._run(
                [
                    str(python),
                    "-m",
                    "helix_mcp_knowledge.reranker_worker",
                    "--self-test",
                    "--model-path",
                    str(model),
                ],
                timeout=900,
                action="reranker component self-test",
                env=self._worker_environment(),
            )
            model_bytes = self._directory_size(model)
            metadata = {
                "schema_version": 1,
                "component_version": RERANKER_COMPONENT_VERSION,
                "runtime": runtime.relative_to(self.root).as_posix(),
                "model_path": model.relative_to(self.root).as_posix(),
                "model_id": RERANKER_MODEL_ID,
                "model_revision": RERANKER_MODEL_REVISION,
                "packages": list(RERANKER_PACKAGES),
                "inference_runtime": RERANKER_TORCH_PACKAGE,
                "host": RERANKER_HOST,
                "port": RERANKER_PORT,
                "token": secrets.token_urlsafe(48),
                "installed_at": datetime.now(UTC).isoformat(),
                "model_bytes": model_bytes,
                "installed_bytes": self._directory_size(runtime) + model_bytes,
                "service_generation": uuid.uuid4().hex,
            }
            if previous_metadata is not None:
                previous_runtime = self._managed_path(previous_metadata, "runtime")
                previous_model = self._managed_path(previous_metadata, "model_path")
                metadata["previous_runtime"] = previous_runtime.relative_to(self.root).as_posix()
                metadata["previous_model_path"] = previous_model.relative_to(self.root).as_posix()
            self._atomic_json_write(self.pending_metadata_path, metadata)
            with UpdateLock(
                self.service_lock_path,
                timeout_seconds=RERANKER_SERVICE_LOCK_TIMEOUT_SECONDS,
            ):
                if previous_metadata is not None:
                    self._stop_service_locked(stop_only_if_requested=False)
                    previous_stopped = True
                candidate_client = self.client(metadata)
                self._start_service(metadata, candidate_client)
                self._promote_pending_metadata(metadata)
            with suppress(OSError):
                os.chmod(self.metadata_path, 0o600)
            # Retention is best-effort after activation and must never roll back a
            # component that already passed both model and live-service checks.
            with suppress(ConfigurationError, OSError):
                self._prune_superseded_candidates(
                    active_runtime=runtime,
                    active_model=model,
                    previous_metadata=previous_metadata,
                )
            return self.status(check_service=True)
        except Exception:
            if candidate_client is not None and candidate_client.health():
                with suppress(Exception):
                    candidate_client.shutdown()
            if metadata is not None:
                self._discard_pending_metadata(metadata)
            self._remove_candidate(runtime, runtime_parent)
            self._remove_candidate(model, model_parent)
            if (
                previous_metadata is not None
                and previous_stopped
                and self._service_start_requested()
            ):
                with (
                    suppress(Exception),
                    UpdateLock(
                        self.service_lock_path,
                        timeout_seconds=RERANKER_SERVICE_LOCK_TIMEOUT_SECONDS,
                    ),
                ):
                    self._start_service(previous_metadata, self.client(previous_metadata))
            raise

    def client(self, metadata: dict[str, object] | None = None) -> RerankerServiceClient:
        details = metadata or self._metadata()
        if details is None:
            raise ConfigurationError("reranker support is not installed")
        host = str(details.get("host"))
        port = int(details.get("port", 0))
        if host != RERANKER_HOST or port != RERANKER_PORT:
            raise ConfigurationError("reranker metadata does not use the managed loopback endpoint")
        return RerankerServiceClient(
            base_url=f"http://{host}:{port}",
            token=str(details.get("token") or ""),
            timeout_seconds=RERANKER_REQUEST_TIMEOUT_SECONDS,
        )

    def ensure_service(self) -> RerankerServiceClient:
        metadata = self._usable_previous_metadata()
        if metadata is None:
            raise ConfigurationError("reranker support is not installed")
        if not self._is_current(metadata):
            raise ConfigurationError("reranker support must be updated before use")
        client = self.client(metadata)
        if not self._service_start_requested():
            raise ConfigurationError("reranker service startup was cancelled")
        if client.health():
            return client
        with UpdateLock(
            self.service_lock_path,
            timeout_seconds=RERANKER_SERVICE_LOCK_TIMEOUT_SECONDS,
        ):
            if not self._service_start_requested():
                raise ConfigurationError("reranker service startup was cancelled")
            # An installer may have promoted a new runtime while this caller
            # waited for the lifecycle lock. Never launch with the stale token,
            # model, or runtime captured by the optimistic health check above.
            metadata = self._usable_previous_metadata()
            if metadata is None:
                raise ConfigurationError("reranker support is not installed")
            if not self._is_current(metadata):
                raise ConfigurationError("reranker support must be updated before use")
            client = self.client(metadata)
            if client.health():
                return client
            # A component update can leave an authenticated worker from the
            # previous protocol on the port. Stop it before launching the
            # current worker; an unrelated process remains untouched.
            if client.health(require_current=False):
                self._stop_service_locked(stop_only_if_requested=False)
            return self._start_service(metadata, client)

    def ensure_service_in_background(self) -> RerankerServiceClient:
        """Return immediately and warm the optional service on a daemon thread."""
        metadata = self._metadata()
        if metadata is None or not self._is_current(metadata):
            raise ConfigurationError("reranker support must be installed and current before use")
        client = self.client(metadata)
        if not client.health():
            threading.Thread(
                target=self._ensure_service_without_propagating,
                name="helix-reranker-service-starter",
                daemon=True,
            ).start()
        return client

    def _ensure_service_without_propagating(self) -> None:
        with suppress(Exception):
            self.ensure_service()

    def request_service_start(self) -> None:
        """Clear a prior cancellation before an explicit enable/install request."""

        self._validate_control_root(create=False)
        stop_path = self.service_stop_path
        if stop_path.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(stop_path)
        ):
            raise ConfigurationError("reranker service stop marker is unexpectedly linked")
        if stop_path.exists() and not stop_path.is_file():
            raise ConfigurationError("reranker service stop marker is invalid")
        stop_path.unlink(missing_ok=True)

    def request_service_stop(self) -> None:
        """Publish cancellation immediately, without waiting for model loading."""

        if not self.root.exists() and not self.root.is_symlink():
            return
        self._validate_control_root(create=False)
        self._atomic_json_write(
            self.service_stop_path,
            {
                "schema_version": 1,
                "desired": False,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        with suppress(OSError, ValueError, json.JSONDecodeError):
            control = self._service_control()
            if control is not None:
                self._atomic_json_write(
                    self.service_control_path,
                    {**control, "desired": False},
                )

    def service_is_inactive(self) -> bool:
        """Prove that no reranker lifecycle operation or listener is active."""

        if not self.root.exists() and not self.root.is_symlink():
            return True
        self._validate_control_root(create=False)
        # Startup and shutdown hold the lifecycle lock until the worker is ready
        # or fully stopped.  Acquiring it without waiting therefore distinguishes
        # a genuinely inactive service from a model that is still loading.
        with UpdateLock(self.service_lock_path, timeout_seconds=0):
            return not self._port_accepting()

    def stop_service(self) -> None:
        if not self.root.exists() and not self.root.is_symlink():
            return
        self.request_service_stop()
        with UpdateLock(
            self.service_lock_path,
            timeout_seconds=RERANKER_SERVICE_LOCK_TIMEOUT_SECONDS,
        ):
            self._stop_service_locked()

    def _stop_service_locked(self, *, stop_only_if_requested: bool = True) -> None:
        if stop_only_if_requested and self._service_start_requested():
            # A later explicit enable removed the stop marker while this stop
            # operation was waiting for the lifecycle lock.
            return
        metadata_candidates: list[dict[str, object]] = []
        for loader in (self._metadata, self._pending_metadata):
            try:
                metadata = loader()
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if metadata is not None:
                metadata_candidates.append(metadata)
        seen_tokens: set[str] = set()
        for metadata in metadata_candidates:
            if stop_only_if_requested and self._service_start_requested():
                return
            token = str(metadata.get("token") or "")
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            client = self.client(metadata)
            if not client.health(require_current=False):
                continue
            client.shutdown()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and client.health(
                timeout=0.5,
                require_current=False,
            ):
                time.sleep(0.25)
            if client.health(timeout=0.5, require_current=False):
                raise ConfigurationError("reranker service did not stop cleanly")
        if stop_only_if_requested and self._service_start_requested():
            return
        if self._port_accepting():
            raise ConfigurationError(
                "refusing to stop an unauthenticated process on the reranker port"
            )

    def remove(self) -> int:
        """Remove only a validated, non-linked component under the workspace."""
        self.stop_service()
        root, allowed_links = self._validated_removal_plan()
        if not root.exists():
            return 0
        reclaimed = self._directory_size(root)
        for link in allowed_links:
            link.unlink()
        shutil.rmtree(root)
        return reclaimed

    def removable_storage_bytes(self) -> int | None:
        """Return safely removable bytes, even when the active marker is damaged."""

        try:
            root, _allowed_links = self._validated_removal_plan()
            if not root.exists() or not any(root.iterdir()):
                return None
            return self._directory_size(root)
        except (ConfigurationError, OSError):
            return None

    def _validated_removal_plan(self) -> tuple[Path, list[Path]]:
        if self.root.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(self.root)
        ):
            raise ConfigurationError("refusing to remove a linked reranker component root")
        root = self.root.resolve()
        workspace = self.config.base_dir.resolve()
        try:
            relative = root.relative_to(workspace)
        except ValueError as exc:
            raise ConfigurationError(
                "refusing to remove a reranker component outside the managed workspace"
            ) from exc
        if not relative.parts or root == workspace or root.name != "reranker":
            raise ConfigurationError("refusing to remove an unexpected reranker component path")
        if not root.exists():
            return root, []
        allowed_links: list[Path] = []
        for entry in root.rglob("*"):
            if entry.is_symlink():
                if self._is_internal_venv_lib64_link(entry, root):
                    allowed_links.append(entry)
                    continue
                raise ConfigurationError(
                    f"refusing to remove reranker storage containing a link: {entry}"
                )
            if hasattr(os.path, "isjunction") and os.path.isjunction(entry):
                raise ConfigurationError(
                    f"refusing to remove reranker storage containing a link: {entry}"
                )
        return root, allowed_links

    def cleanup_incomplete_candidates(
        self,
        *,
        minimum_age_seconds: float = 0.0,
    ) -> CandidateCleanupResult:
        """Remove unreferenced transactional candidates without touching live data."""

        metadata = self._metadata()
        runtime_parent = self.root / "runtime"
        model_parent = self.root / "models"
        keep_runtimes: set[Path] = set()
        keep_models: set[Path] = set()
        if metadata is not None:
            keep_runtimes.add(self._managed_path(metadata, "runtime").resolve())
            keep_models.add(self._managed_path(metadata, "model_path").resolve())
            previous_runtime = self._retained_candidate_path(
                metadata,
                "previous_runtime",
                parent=runtime_parent,
                prefix="reranker-",
            )
            previous_model = self._retained_candidate_path(
                metadata,
                "previous_model_path",
                parent=model_parent,
                prefix="bge-reranker-v2-m3-",
            )
            if previous_runtime is not None:
                keep_runtimes.add(previous_runtime)
            if previous_model is not None:
                keep_models.add(previous_model)

        removable: list[tuple[Path, Path]] = []
        for parent, prefix, keep in (
            (runtime_parent, "reranker-", keep_runtimes),
            (model_parent, "bge-reranker-v2-m3-", keep_models),
        ):
            if not parent.exists():
                continue
            if parent.is_symlink() or (
                hasattr(os.path, "isjunction") and os.path.isjunction(parent)
            ):
                raise ConfigurationError("refusing to clean a linked reranker candidate root")
            for candidate in parent.iterdir():
                if self._is_managed_candidate_name(candidate.name, prefix=prefix) and (
                    candidate.resolve() not in keep
                ):
                    removable.append((candidate, parent))

        if removable and self._port_accepting():
            active_service = metadata is not None and self.client(metadata).health(
                timeout=0.5,
                require_current=False,
            )
            if not active_service:
                raise ConfigurationError(
                    "refusing to clean reranker candidates while an unknown service uses its port"
                )
        reclaimed = 0
        deferred = 0
        for candidate, parent in removable:
            self._validate_candidate(candidate, parent)
            if not candidate_is_old_enough(
                candidate,
                minimum_age_seconds=minimum_age_seconds,
            ):
                deferred += 1
                continue
            reclaimed += self._directory_size(candidate)
            self._remove_candidate(candidate, parent)
        if deferred == 0 and not self._port_accepting():
            self._discard_pending_metadata()
        return CandidateCleanupResult(
            reclaimed_bytes=reclaimed,
            deferred_candidates=deferred,
        )

    def _install_dependencies(self, python: Path) -> None:
        for requirements_name, timeout, action in (
            (PIP_BOOTSTRAP_REQUIREMENTS, 300, "component installer bootstrap"),
            (RERANKER_COMPONENT_REQUIREMENTS, 1800, "reranker dependency installation"),
        ):
            requirements = locked_component_requirements(requirements_name)
            source_arguments = (
                ["--find-links", RERANKER_TORCH_FIND_LINKS]
                if requirements_name == RERANKER_COMPONENT_REQUIREMENTS
                else []
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
                    *source_arguments,
                    "--require-hashes",
                    "--requirement",
                    str(requirements),
                ],
                timeout=timeout,
                action=action,
            )

    def _download_model(self, python: Path, model: Path) -> None:
        download_code = (
            "from huggingface_hub import snapshot_download; import sys; "
            "snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2], "
            "local_dir=sys.argv[3], allow_patterns=sys.argv[4:])"
        )
        self._run(
            [
                str(python),
                "-c",
                download_code,
                RERANKER_MODEL_ID,
                RERANKER_MODEL_REVISION,
                str(model),
                *RERANKER_MODEL_FILES,
            ],
            timeout=3600,
            action="pinned BGE reranker model download",
        )

    @staticmethod
    def _remove_download_metadata(model: Path) -> None:
        cache = model / ".cache" / "huggingface"
        if not cache.exists():
            return
        resolved = cache.resolve()
        try:
            resolved.relative_to(model.resolve())
        except ValueError as exc:
            raise ConfigurationError("reranker download metadata escapes the model root") from exc
        if cache.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(cache)):
            raise ConfigurationError("reranker download metadata is unexpectedly linked")
        for entry in cache.rglob("*"):
            if entry.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(entry)):
                raise ConfigurationError("reranker download metadata contains a filesystem link")
        shutil.rmtree(cache)
        parent = cache.parent
        with suppress(OSError):
            parent.rmdir()

    @staticmethod
    def _validate_model(model: Path) -> None:
        if model.is_symlink() or not model.is_dir():
            raise ConfigurationError("reranker model directory is missing or linked")
        missing = [name for name in RERANKER_MODEL_FILES if not (model / name).is_file()]
        if missing:
            raise ConfigurationError(
                f"reranker model is missing required files: {', '.join(missing)}"
            )
        for entry in model.rglob("*"):
            if entry.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(entry)):
                raise ConfigurationError("reranker model contains an unexpected filesystem link")

    def _start_service(
        self,
        metadata: dict[str, object],
        client: RerankerServiceClient,
    ) -> RerankerServiceClient:
        generation = self._prepare_service_start(metadata)
        command = [
            str(self._venv_python(self._managed_path(metadata, "runtime"))),
            "-m",
            "helix_mcp_knowledge.reranker_worker",
            "--model-path",
            str(self._managed_path(metadata, "model_path")),
            "--host",
            RERANKER_HOST,
            "--port",
            str(RERANKER_PORT),
            "--batch-size",
            str(RERANKER_BATCH_SIZE),
            "--max-seq-length",
            str(RERANKER_MAX_SEQUENCE_LENGTH),
            "--cpu-threads",
            str(RERANKER_CPU_THREADS),
            "--control-path",
            str(self.service_control_path),
            "--stop-path",
            str(self.service_stop_path),
            "--generation",
            generation,
        ]
        managed_metadata = installation_metadata_path(self.config.base_dir)
        if managed_metadata.is_file():
            command.extend(
                [
                    "--managed-installation-path",
                    str(managed_metadata),
                    "--managed-version",
                    __version__,
                ]
            )
        log_path = self.config.resolve_path(self.config.paths.errors) / "reranker-service.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = self._worker_environment()
        environment["HELIX_RERANKER_SERVICE_TOKEN"] = str(metadata["token"])
        kwargs: dict[str, object] = {
            "cwd": self.config.base_dir,
            "env": environment,
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
            self._process = subprocess.Popen(command, stderr=log, **kwargs)
        threading.Thread(
            target=self._process.wait,
            name="helix-reranker-service-reaper",
            daemon=True,
        ).start()
        return self._wait_for_service(
            client,
            generation=generation,
            process=self._process,
            log_path=log_path,
        )

    def _wait_for_service(
        self,
        client: RerankerServiceClient,
        *,
        generation: str,
        process: subprocess.Popen[bytes] | None = None,
        log_path: Path | None = None,
    ) -> RerankerServiceClient:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if not self._service_generation_is_current(generation):
                raise ConfigurationError("reranker service startup was cancelled")
            if client.health(timeout=1):
                return client
            if process is not None and process.poll() is not None:
                detail = (
                    log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                    if log_path is not None and log_path.is_file()
                    else "no diagnostic output"
                )
                raise ConfigurationError(f"reranker service failed to start: {detail}")
            time.sleep(1)
        raise ConfigurationError("reranker service did not become ready within 180 seconds")

    def _metadata(self) -> dict[str, object] | None:
        return self._metadata_at(self.metadata_path)

    def _pending_metadata(self) -> dict[str, object] | None:
        return self._metadata_at(self.pending_metadata_path)

    def _metadata_at(self, path: Path) -> dict[str, object] | None:
        if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
            raise ValueError("reranker component metadata is unexpectedly linked")
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError("reranker component metadata is invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
        required = {
            "component_version",
            "runtime",
            "model_path",
            "model_id",
            "model_revision",
            "host",
            "port",
            "token",
        }
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not required.issubset(payload)
        ):
            raise ValueError("reranker component metadata is invalid")
        self._component_version(payload)
        if payload.get("host") != RERANKER_HOST or payload.get("port") != RERANKER_PORT:
            raise ValueError("reranker component endpoint is invalid")
        token = payload.get("token")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("reranker component token is invalid")
        generation = payload.get("service_generation")
        if generation is not None and not self._valid_generation(generation):
            raise ValueError("reranker component service generation is invalid")
        return payload

    def _promote_pending_metadata(self, expected: dict[str, object]) -> None:
        generation = expected.get("service_generation")
        if not self._valid_generation(generation) or not self._service_generation_is_current(
            str(generation)
        ):
            raise ConfigurationError("reranker service activation was cancelled")
        pending = self._pending_metadata()
        if pending != expected:
            raise ConfigurationError("reranker pending component metadata changed unexpectedly")
        os.replace(self.pending_metadata_path, self.metadata_path)

    def _discard_pending_metadata(
        self,
        expected: dict[str, object] | None = None,
    ) -> None:
        path = self.pending_metadata_path
        if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
            raise ConfigurationError("reranker pending metadata is unexpectedly linked")
        if not path.exists():
            return
        if not path.is_file():
            raise ConfigurationError("reranker pending metadata is invalid")
        if expected is not None:
            try:
                current = self._pending_metadata()
            except (OSError, ValueError, json.JSONDecodeError):
                return
            if current != expected:
                return
        path.unlink()

    def _service_control(self) -> dict[str, object] | None:
        path = self.service_control_path
        if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
            raise ValueError("reranker service control is unexpectedly linked")
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError("reranker service control is invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not self._valid_generation(payload.get("generation"))
            or not isinstance(payload.get("desired"), bool)
        ):
            raise ValueError("reranker service control is invalid")
        return payload

    def _service_start_requested(self) -> bool:
        stop_path = self.service_stop_path
        if stop_path.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(stop_path)
        ):
            return False
        return not stop_path.exists()

    def _prepare_service_start(self, metadata: dict[str, object]) -> str:
        if not self._service_start_requested():
            raise ConfigurationError("reranker service startup was cancelled")
        self._validate_control_root(create=True)
        previous_metadata = dict(metadata)
        # Every process launch gets a fresh generation. Reusing the component's
        # installation generation would allow an old loader to become current
        # again after a fast disable/re-enable cycle.
        generation = uuid.uuid4().hex
        metadata["service_generation"] = generation
        self._persist_service_generation(
            previous=previous_metadata,
            updated=metadata,
        )
        self._atomic_json_write(
            self.service_control_path,
            {"schema_version": 1, "generation": generation, "desired": True},
        )
        if not self._service_start_requested():
            raise ConfigurationError("reranker service startup was cancelled")
        return generation

    def _persist_service_generation(
        self,
        *,
        previous: dict[str, object],
        updated: dict[str, object],
    ) -> None:
        """Persist the launch fence before spawning, without replacing newer metadata."""

        for path, loader in (
            (self.pending_metadata_path, self._pending_metadata),
            (self.metadata_path, self._metadata),
        ):
            current = loader()
            if current == previous:
                self._atomic_json_write(path, updated)
                return
        raise ConfigurationError("reranker component metadata changed before service startup")

    def _service_generation_is_current(self, generation: str) -> bool:
        if not self._service_start_requested():
            return False
        try:
            control = self._service_control()
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return bool(
            control is not None
            and control.get("desired") is True
            and control.get("generation") == generation
        )

    def _validate_control_root(self, *, create: bool) -> None:
        if self.root.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(self.root)
        ):
            raise ConfigurationError("reranker component root is unexpectedly linked")
        resolved = self.root.resolve()
        workspace = self.config.base_dir.resolve()
        try:
            relative = resolved.relative_to(workspace)
        except ValueError as exc:
            raise ConfigurationError(
                "reranker component root is outside the managed workspace"
            ) from exc
        if not relative.parts or resolved == workspace or resolved.name != "reranker":
            raise ConfigurationError("reranker component root is invalid")
        if self.root.exists() and not self.root.is_dir():
            raise ConfigurationError("reranker component root is invalid")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _valid_generation(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None

    def _usable_previous_metadata(self) -> dict[str, object] | None:
        """Return rollback metadata only when both managed payloads still exist."""

        try:
            metadata = self._metadata()
            if metadata is None:
                return None
            runtime_value = metadata.get("runtime")
            model_value = metadata.get("model_path")
            if not isinstance(runtime_value, str) or not isinstance(model_value, str):
                return None
            raw_runtime = self.root / runtime_value
            raw_model = self.root / model_value
            runtime = self._managed_path(metadata, "runtime")
            model = self._managed_path(metadata, "model_path")
            runtime_parent = (self.root / "runtime").resolve()
            model_parent = (self.root / "models").resolve()
            if (
                runtime.parent != runtime_parent
                or model.parent != model_parent
                or not self._is_managed_candidate_name(runtime.name, prefix="reranker-")
                or not self._is_managed_candidate_name(
                    model.name,
                    prefix="bge-reranker-v2-m3-",
                )
                or raw_runtime.is_symlink()
                or raw_model.is_symlink()
                or (hasattr(os.path, "isjunction") and os.path.isjunction(raw_runtime))
                or (hasattr(os.path, "isjunction") and os.path.isjunction(raw_model))
                or not runtime.is_dir()
                or not model.is_dir()
                or not self._venv_python(runtime).is_file()
            ):
                return None
            return metadata
        except (OSError, ValueError, json.JSONDecodeError):
            # A damaged inactive marker must not turn a successful candidate
            # download into a failed upgrade. It remains untouched until the
            # replacement passes its self-test and live-service check.
            return None

    @staticmethod
    def _component_version(metadata: dict[str, object]) -> int:
        version = metadata.get("component_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("reranker component version is invalid")
        return version

    @staticmethod
    def _is_current(metadata: dict[str, object]) -> bool:
        return (
            metadata.get("component_version") == RERANKER_COMPONENT_VERSION
            and metadata.get("model_id") == RERANKER_MODEL_ID
            and metadata.get("model_revision") == RERANKER_MODEL_REVISION
            and metadata.get("inference_runtime") == RERANKER_TORCH_PACKAGE
            and metadata.get("packages") == list(RERANKER_PACKAGES)
        )

    def _managed_path(self, metadata: dict[str, object], key: str) -> Path:
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"reranker {key} is invalid")
        path = (self.root / value).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError(f"reranker {key} escapes its managed root") from exc
        if not path.exists():
            raise ValueError(f"reranker {key} is missing")
        return path

    @staticmethod
    def _venv_python(runtime: Path) -> Path:
        return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    @staticmethod
    def _worker_environment() -> dict[str, str]:
        package_root = str(Path(__file__).resolve().parent.parent)
        existing = os.environ.get("PYTHONPATH")
        return {
            **os.environ,
            "PYTHONPATH": package_root if not existing else package_root + os.pathsep + existing,
        }

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
        if not path.exists():
            return 0
        return sum(
            item.stat().st_size
            for item in path.rglob("*")
            if item.is_file() and not item.is_symlink()
        )

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
                with suppress(OSError):
                    os.chmod(temporary.name, 0o600)
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _remove_candidate(path: Path, parent: Path) -> None:
        if not path.exists():
            return
        resolved = RerankerComponentManager._validate_candidate(path, parent)
        shutil.rmtree(resolved)

    @staticmethod
    def _validate_candidate(path: Path, parent: Path) -> Path:
        resolved = path.resolve()
        if (
            path.is_symlink()
            or (hasattr(os.path, "isjunction") and os.path.isjunction(path))
            or resolved.parent != parent.resolve()
            or not resolved.is_dir()
        ):
            raise ConfigurationError("refusing to clean an unexpected reranker component path")
        return resolved

    def _prune_superseded_candidates(
        self,
        *,
        active_runtime: Path,
        active_model: Path,
        previous_metadata: dict[str, object] | None,
    ) -> None:
        keep_runtimes = {active_runtime.resolve()}
        keep_models = {active_model.resolve()}
        if previous_metadata is not None:
            with suppress(OSError, ValueError):
                keep_runtimes.add(self._managed_path(previous_metadata, "runtime").resolve())
            with suppress(OSError, ValueError):
                keep_models.add(self._managed_path(previous_metadata, "model_path").resolve())
        for parent, prefix, keep in (
            (self.root / "runtime", "reranker-", keep_runtimes),
            (self.root / "models", "bge-reranker-v2-m3-", keep_models),
        ):
            if not parent.is_dir() or parent.is_symlink():
                continue
            for candidate in parent.iterdir():
                if self._is_managed_candidate_name(candidate.name, prefix=prefix) and (
                    candidate.resolve() not in keep
                ):
                    self._remove_candidate(candidate, parent)

    @staticmethod
    def _is_managed_candidate_name(name: str, *, prefix: str) -> bool:
        pattern = _RUNTIME_CANDIDATE_PATTERN if prefix == "reranker-" else _MODEL_CANDIDATE_PATTERN
        return pattern.fullmatch(name) is not None

    def _retained_candidate_path(
        self,
        metadata: dict[str, object],
        key: str,
        *,
        parent: Path,
        prefix: str,
    ) -> Path | None:
        value = metadata.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ConfigurationError(f"reranker {key} is invalid")
        raw_candidate = self.root / value
        candidate = raw_candidate.resolve()
        if (
            raw_candidate.is_symlink()
            or (hasattr(os.path, "isjunction") and os.path.isjunction(raw_candidate))
            or candidate.parent != parent.resolve()
            or not candidate.name.startswith(prefix)
        ):
            raise ConfigurationError(f"reranker {key} is not a managed candidate")
        return candidate if candidate.exists() else None

    @staticmethod
    def _port_accepting() -> bool:
        try:
            with socket.create_connection((RERANKER_HOST, RERANKER_PORT), timeout=0.2):
                return True
        except OSError:
            return False

    @staticmethod
    def _is_internal_venv_lib64_link(path: Path, root: Path) -> bool:
        try:
            relative = path.relative_to(root)
            target = path.resolve(strict=True)
            expected = (path.parent / "lib").resolve(strict=True)
            target.relative_to(root)
        except (OSError, ValueError):
            return False
        return (
            len(relative.parts) == 3
            and relative.parts[0] == "runtime"
            and relative.parts[1].startswith("reranker-")
            and relative.parts[2] == "lib64"
            and os.readlink(path) == "lib"
            and target == expected
            and target.is_dir()
            and not target.is_symlink()
        )

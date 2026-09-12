"""Transactional installation and lifecycle of the optional semantic component."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
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
from .component_requirements import (
    PIP_BOOTSTRAP_REQUIREMENTS,
    SEMANTIC_COMPONENT_REQUIREMENTS,
    locked_component_requirements,
)
from .config import AppConfig
from .errors import ConfigurationError
from .managed_installation import installation_metadata_path
from .semantic_client import SemanticServiceClient
from .update_lock import UpdateLock, UpdateLockBusyError

SEMANTIC_COMPONENT_VERSION = 3
SEMANTIC_MODEL_REVISION = "84790c1a606f60d06c6932e4ecdd174b466d84ac"
SEMANTIC_HOST = "127.0.0.1"
SEMANTIC_PORT = 8767
SEMANTIC_REQUEST_TIMEOUT_SECONDS = 1800
SEMANTIC_MAX_SEQUENCE_LENGTH = 1024
SEMANTIC_SERVICE_LOCK_TIMEOUT_SECONDS = 195.0
SEMANTIC_TORCH_PACKAGE = "torch==2.14.0"
SEMANTIC_TORCH_FIND_LINKS = "https://download.pytorch.org/whl/cpu/torch/"
SEMANTIC_PACKAGES = (
    "qdrant-client==1.19.0",
    "sentence-transformers==6.0.1",
    "transformers==5.16.1",
)
ESTIMATED_INSTALL_BYTES = 5 * 1024 * 1024 * 1024
ESTIMATED_ACTIVE_MEMORY_BYTES = 4 * 1024 * 1024 * 1024
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class SemanticComponentStatus:
    installed: bool
    status: str
    component_version: int | None = None
    installed_bytes: int | None = None
    model_bytes: int | None = None
    vector_bytes: int | None = None
    installed_at: str | None = None
    service_ready: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "estimated_install_bytes": ESTIMATED_INSTALL_BYTES,
            "estimated_active_memory_bytes": ESTIMATED_ACTIVE_MEMORY_BYTES,
        }


class SemanticComponentManager:
    def __init__(
        self,
        config: AppConfig,
        *,
        python_executable: str | Path | None = None,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self.config = config
        self.root = config.semantic_component_path
        self.python_executable = str(python_executable or sys.executable)
        self.runner = runner
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def metadata_path(self) -> Path:
        return self.root / "current.json"

    @property
    def pending_metadata_path(self) -> Path:
        return self.root / "pending.json"

    def status(self, *, check_service: bool = False) -> SemanticComponentStatus:
        try:
            self._validate_root(create=False)
            metadata = self._metadata()
            if metadata is None:
                return SemanticComponentStatus(installed=False, status="not_installed")
            runtime = self._managed_path(metadata, "runtime")
            model = self._managed_path(metadata, "model_path")
            vector_path = self._managed_path(metadata, "vector_path", must_exist=False)
            if not self._venv_python(runtime).is_file() or not model.is_dir():
                raise ValueError("semantic runtime or model is missing")
            component_version = int(metadata["component_version"])
            exact_component = (
                component_version == SEMANTIC_COMPONENT_VERSION
                and metadata.get("model_id") == self.config.embeddings.model
                and metadata.get("model_revision") == SEMANTIC_MODEL_REVISION
                and metadata.get("inference_runtime") == SEMANTIC_TORCH_PACKAGE
                and metadata.get("packages") == list(SEMANTIC_PACKAGES)
            )
            service_ready = (
                self.client(metadata).health() if check_service and exact_component else False
            )
            status = "ready"
            error = None
            if component_version < SEMANTIC_COMPONENT_VERSION:
                status = "update_required"
                error = (
                    "The installed semantic component must be updated before it can be used. "
                    "Lexical search remains available."
                )
                service_ready = False
            elif component_version > SEMANTIC_COMPONENT_VERSION:
                status = "incompatible"
                error = (
                    "The installed semantic component belongs to a newer server runtime. "
                    "Lexical search remains available."
                )
                service_ready = False
            elif not exact_component:
                status = "update_required"
                error = (
                    "The installed semantic runtime or model does not match the pinned release. "
                    "Lexical search remains available."
                )
            elif check_service and not service_ready:
                status = "degraded"
                error = "Semantic service is not responding; lexical search remains available."
            return SemanticComponentStatus(
                installed=True,
                status=status,
                component_version=component_version,
                installed_bytes=int(
                    metadata.get("installed_bytes")
                    or self._directory_size(runtime) + self._directory_size(model)
                ),
                model_bytes=int(metadata.get("model_bytes") or self._directory_size(model)),
                vector_bytes=self._directory_size(vector_path),
                installed_at=str(metadata.get("installed_at") or "") or None,
                service_ready=service_ready,
                error=error,
            )
        except (ConfigurationError, OSError, ValueError, json.JSONDecodeError) as exc:
            return SemanticComponentStatus(installed=False, status="error", error=str(exc))

    def install(self) -> SemanticComponentStatus:
        self._validate_root(create=True)
        self.recover_interrupted_promotion()
        current = self.status()
        if current.installed and current.status == "ready":
            self.ensure_service()
            return self.status(check_service=True)
        previous_metadata = self._metadata()
        runtime_parent = self.root / "runtime"
        model_parent = self.root / "models"
        runtime_parent.mkdir(parents=True, exist_ok=True)
        model_parent.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex
        runtime = runtime_parent / f"semantic-{SEMANTIC_COMPONENT_VERSION}-{suffix}"
        model = model_parent / f"bge-m3-{suffix}"
        candidate_can_be_removed = True
        promotion_journal: dict[str, object] | None = None
        try:
            venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(runtime)
            python = self._venv_python(runtime)
            bootstrap_requirements = locked_component_requirements(PIP_BOOTSTRAP_REQUIREMENTS)
            component_requirements = locked_component_requirements(SEMANTIC_COMPONENT_REQUIREMENTS)
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
                    "--find-links",
                    SEMANTIC_TORCH_FIND_LINKS,
                    "--require-hashes",
                    "--requirement",
                    str(component_requirements),
                ],
                timeout=1800,
                action="semantic dependency installation",
            )
            download_code = (
                "from huggingface_hub import snapshot_download; import sys; "
                "snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2], "
                "local_dir=sys.argv[3], allow_patterns=["
                "'1_Pooling/*','2_Normalize/*','config.json',"
                "'config_sentence_transformers.json','modules.json','model.safetensors',"
                "'sentence_bert_config.json','sentencepiece.bpe.model',"
                "'special_tokens_map.json','tokenizer.json','tokenizer_config.json'])"
            )
            self._run(
                [
                    str(python),
                    "-c",
                    download_code,
                    self.config.embeddings.model,
                    SEMANTIC_MODEL_REVISION,
                    str(model),
                ],
                timeout=3600,
                action="BGE-M3 model download",
            )
            self._run(
                [
                    str(python),
                    "-m",
                    "helix_mcp_knowledge.semantic_worker",
                    "--self-test",
                    "--model-path",
                    str(model),
                    "--dimension",
                    str(self.config.embeddings.dimension),
                ],
                timeout=600,
                action="semantic component self-test",
                env=self._worker_environment(),
            )
            vector_path = self.root / "vectors"
            model_bytes = self._directory_size(model)
            metadata = {
                "schema_version": 1,
                "component_version": SEMANTIC_COMPONENT_VERSION,
                "runtime": runtime.relative_to(self.root).as_posix(),
                "model_path": model.relative_to(self.root).as_posix(),
                "vector_path": vector_path.relative_to(self.root).as_posix(),
                "model_id": self.config.embeddings.model,
                "model_revision": SEMANTIC_MODEL_REVISION,
                "packages": list(SEMANTIC_PACKAGES),
                "inference_runtime": SEMANTIC_TORCH_PACKAGE,
                "host": SEMANTIC_HOST,
                "port": SEMANTIC_PORT,
                "token": secrets.token_urlsafe(48),
                "installed_at": datetime.now(UTC).isoformat(),
                "model_bytes": model_bytes,
                "installed_bytes": self._directory_size(runtime) + model_bytes,
            }
            promotion_journal = self._promotion_journal(
                candidate_metadata=metadata,
                previous_metadata=previous_metadata,
                previous_was_live=False,
            )
            self._atomic_json_write(self.pending_metadata_path, promotion_journal)
            with suppress(OSError):
                os.chmod(self.pending_metadata_path, 0o600)
            # Keep the previous runtime usable throughout the expensive build and
            # self-test. Its short outage and the complete cutover are serialized
            # with every other service start.
            with UpdateLock(
                self.root / ".service.lock",
                timeout_seconds=SEMANTIC_SERVICE_LOCK_TIMEOUT_SECONDS,
            ):
                active_metadata = self._metadata()
                if active_metadata == metadata and self.client(metadata).health():
                    # A concurrent recovery completed this prepared promotion
                    # while this installer was waiting for the lifecycle lock.
                    candidate_can_be_removed = False
                    self._discard_promotion_journal(promotion_journal)
                    return self.status(check_service=True)
                if active_metadata != previous_metadata:
                    raise ConfigurationError(
                        "semantic component metadata changed during candidate preparation"
                    )
                previous_was_live = False
                if previous_metadata is not None:
                    previous_client = self.client(previous_metadata)
                    previous_was_live = previous_client.health()
                    promotion_journal = self._promotion_journal(
                        candidate_metadata=metadata,
                        previous_metadata=previous_metadata,
                        previous_was_live=previous_was_live,
                    )
                    self._atomic_json_write(self.pending_metadata_path, promotion_journal)
                    with suppress(OSError):
                        os.chmod(self.pending_metadata_path, 0o600)
                    if previous_was_live:
                        self._stop_service_client(previous_client)
                try:
                    self._start_service(metadata, self.client(metadata))
                    # current.json remains on the previously validated runtime
                    # until the candidate answers authenticated health checks.
                    self._atomic_json_write(self.metadata_path, metadata)
                    candidate_can_be_removed = False
                    with suppress(OSError):
                        os.chmod(self.metadata_path, 0o600)
                except Exception as activation_error:
                    try:
                        self._stop_failed_candidate_service(metadata)
                    except Exception:
                        # Never remove a runtime that may still own a live process.
                        candidate_can_be_removed = False
                        raise
                    if previous_metadata is not None and previous_was_live:
                        try:
                            self._start_service(
                                previous_metadata,
                                self.client(previous_metadata),
                            )
                        except Exception as rollback_error:
                            raise ConfigurationError(
                                "semantic component activation failed; previous metadata was "
                                "restored but its service could not be restarted: "
                                f"{rollback_error}"
                            ) from activation_error
                    raise
            self._discard_promotion_journal(promotion_journal)
            return self.status(check_service=True)
        except Exception:
            if candidate_can_be_removed:
                self._remove_candidate(runtime, runtime_parent)
                self._remove_candidate(model, model_parent)
                if promotion_journal is not None:
                    self._discard_promotion_journal(promotion_journal)
            raise

    def _stop_failed_candidate_service(self, metadata: dict[str, object]) -> None:
        """Stop only the candidate process owned by this manager before rollback."""

        with suppress(Exception):
            client = self.client(metadata)
            if client.health():
                self._stop_service_client(client)
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise ConfigurationError(
                "semantic candidate service did not stop; rollback was not attempted"
            ) from exc

    def _restore_previous_metadata(
        self,
        *,
        candidate_metadata: dict[str, object],
        previous_metadata: dict[str, object] | None,
    ) -> None:
        active = self._metadata()
        if active != candidate_metadata:
            raise ConfigurationError(
                "semantic component metadata changed during candidate activation"
            )
        if previous_metadata is None:
            self.metadata_path.unlink()
            return
        self._atomic_json_write(self.metadata_path, previous_metadata)
        with suppress(OSError):
            os.chmod(self.metadata_path, 0o600)

    @staticmethod
    def _promotion_journal(
        *,
        candidate_metadata: dict[str, object],
        previous_metadata: dict[str, object] | None,
        previous_was_live: bool,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "candidate": candidate_metadata,
            "previous": previous_metadata,
            "previous_was_live": previous_was_live,
            "prepared_at": datetime.now(UTC).isoformat(),
        }

    def recover_interrupted_promotion(self) -> bool:
        """Complete or roll back a candidate left by an interrupted cutover."""

        self._validate_root(create=False)
        if self._pending_promotion() is None:
            return False
        with UpdateLock(
            self.root / ".service.lock",
            timeout_seconds=SEMANTIC_SERVICE_LOCK_TIMEOUT_SECONDS,
        ):
            journal = self._pending_promotion()
            if journal is None:
                return False
            self._recover_promotion_locked(journal)
            return True

    def _recover_promotion_locked(self, journal: dict[str, object]) -> None:
        candidate = dict(journal["candidate"])  # type: ignore[arg-type]
        previous_value = journal.get("previous")
        previous = dict(previous_value) if isinstance(previous_value, dict) else None
        active = self._metadata()
        if active not in (candidate, previous):
            raise ConfigurationError(
                "semantic component metadata changed during interrupted promotion recovery"
            )
        candidate_runtime, candidate_model = self._promotion_candidate_paths(candidate)
        candidate_client = self.client(candidate)
        previous_was_live = journal.get("previous_was_live") is True
        previous_client = self.client(previous) if previous is not None else None

        if active == candidate:
            try:
                if not candidate_client.health():
                    self._start_service(candidate, candidate_client)
            except Exception as activation_error:
                self._rollback_recovered_promotion(
                    journal=journal,
                    candidate=candidate,
                    previous=previous,
                    previous_client=previous_client,
                    previous_was_live=previous_was_live,
                    candidate_runtime=candidate_runtime,
                    candidate_model=candidate_model,
                )
                raise ConfigurationError(
                    "interrupted semantic candidate could not be restored"
                ) from activation_error
            self._discard_promotion_journal(journal)
            return

        if previous_client is not None and previous_client.health():
            previous_was_live = True
            updated = {
                **journal,
                "previous_was_live": True,
            }
            self._atomic_json_write(self.pending_metadata_path, updated)
            with suppress(OSError):
                os.chmod(self.pending_metadata_path, 0o600)
            journal = updated
            self._stop_service_client(previous_client)
        try:
            if not candidate_client.health():
                self._start_service(candidate, candidate_client)
            self._atomic_json_write(self.metadata_path, candidate)
            with suppress(OSError):
                os.chmod(self.metadata_path, 0o600)
        except Exception as activation_error:
            self._rollback_recovered_promotion(
                journal=journal,
                candidate=candidate,
                previous=previous,
                previous_client=previous_client,
                previous_was_live=previous_was_live,
                candidate_runtime=candidate_runtime,
                candidate_model=candidate_model,
            )
            raise ConfigurationError(
                "interrupted semantic candidate could not be activated"
            ) from activation_error
        self._discard_promotion_journal(journal)

    def _rollback_recovered_promotion(
        self,
        *,
        journal: dict[str, object],
        candidate: dict[str, object],
        previous: dict[str, object] | None,
        previous_client: SemanticServiceClient | None,
        previous_was_live: bool,
        candidate_runtime: Path,
        candidate_model: Path,
    ) -> None:
        try:
            self._stop_failed_candidate_service(candidate)
        except Exception:
            # Leave the journal and files intact when process ownership is
            # uncertain; a later recovery can retry without deleting live code.
            raise
        active = self._metadata()
        if active == candidate:
            self._restore_previous_metadata(
                candidate_metadata=candidate,
                previous_metadata=previous,
            )
        elif active != previous:
            raise ConfigurationError(
                "semantic component metadata changed while rolling back promotion"
            )
        restart_error: Exception | None = None
        if previous_client is not None and previous_was_live and not previous_client.health():
            try:
                self._start_service(previous, previous_client)  # type: ignore[arg-type]
            except Exception as exc:  # preserve cleanup while reporting failed rollback
                restart_error = exc
        self._remove_candidate(candidate_runtime, self.root / "runtime")
        self._remove_candidate(candidate_model, self.root / "models")
        self._discard_promotion_journal(journal)
        if restart_error is not None:
            raise ConfigurationError(
                f"previous semantic service could not be restarted: {restart_error}"
            ) from restart_error

    def _pending_promotion(self) -> dict[str, object] | None:
        path = self.pending_metadata_path
        if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
            raise ValueError("semantic promotion journal is unexpectedly linked")
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError("semantic promotion journal is invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("candidate"), dict)
            or (
                payload.get("previous") is not None
                and not isinstance(payload.get("previous"), dict)
            )
            or not isinstance(payload.get("previous_was_live"), bool)
        ):
            raise ValueError("semantic promotion journal is invalid")
        self._validate_metadata_payload(payload["candidate"])
        previous = payload.get("previous")
        if isinstance(previous, dict):
            self._validate_metadata_payload(previous)
        self._promotion_candidate_paths(payload["candidate"])
        return payload

    def _discard_promotion_journal(self, expected: dict[str, object]) -> None:
        current = self._pending_promotion()
        if current is None:
            return
        if current != expected:
            raise ConfigurationError("semantic promotion journal changed unexpectedly")
        self.pending_metadata_path.unlink()

    def _promotion_candidate_paths(
        self,
        candidate: dict[str, object],
    ) -> tuple[Path, Path]:
        runtime_value = candidate.get("runtime")
        model_value = candidate.get("model_path")
        if not isinstance(runtime_value, str) or not re.fullmatch(
            rf"runtime/semantic-{SEMANTIC_COMPONENT_VERSION}-[0-9a-f]{{32}}",
            runtime_value,
        ):
            raise ValueError("semantic candidate runtime path is invalid")
        if not isinstance(model_value, str) or not re.fullmatch(
            r"models/bge-m3-[0-9a-f]{32}",
            model_value,
        ):
            raise ValueError("semantic candidate model path is invalid")
        return (
            self._managed_path(candidate, "runtime", must_exist=False),
            self._managed_path(candidate, "model_path", must_exist=False),
        )

    def client(self, metadata: dict[str, object] | None = None) -> SemanticServiceClient:
        details = metadata or self._metadata()
        if details is None:
            raise ConfigurationError("semantic support is not installed")
        host = details.get("host")
        port = details.get("port")
        token = details.get("token")
        if host != SEMANTIC_HOST or port != SEMANTIC_PORT:
            raise ConfigurationError("semantic metadata does not use the managed loopback endpoint")
        if not isinstance(token, str) or len(token) < 32:
            raise ConfigurationError("semantic component token is invalid")
        return SemanticServiceClient(
            base_url=f"http://{host}:{port}",
            token=token,
            dimension=self.config.embeddings.dimension,
            timeout_seconds=SEMANTIC_REQUEST_TIMEOUT_SECONDS,
        )

    def ensure_service(self) -> SemanticServiceClient:
        self._validate_root(create=False)
        metadata = self._metadata()
        pending = self._pending_promotion()
        if metadata is None and pending is None:
            raise ConfigurationError("semantic support is not installed")
        client = self.client(metadata) if metadata is not None else None
        if pending is None and client is not None and client.health():
            return client
        try:
            with UpdateLock(
                self.root / ".service.lock",
                timeout_seconds=SEMANTIC_SERVICE_LOCK_TIMEOUT_SECONDS,
            ):
                pending = self._pending_promotion()
                if pending is not None:
                    self._recover_promotion_locked(pending)
                metadata = self._metadata()
                if metadata is None:
                    raise ConfigurationError("semantic support is not installed")
                client = self.client(metadata)
                if client.health():
                    return client
                return self._start_service(metadata, client)
        except UpdateLockBusyError:
            return self._wait_for_service_transition()

    def _wait_for_service_transition(self) -> SemanticServiceClient:
        """Follow metadata changes while another process owns the lifecycle lock."""

        deadline = time.monotonic() + SEMANTIC_SERVICE_LOCK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            metadata = self._metadata()
            pending = self._pending_promotion()
            if metadata is not None:
                client = self.client(metadata)
                if pending is None and client.health(timeout=1):
                    return client
            try:
                with UpdateLock(self.root / ".service.lock", timeout_seconds=0):
                    pending = self._pending_promotion()
                    if pending is not None:
                        self._recover_promotion_locked(pending)
                    metadata = self._metadata()
                    if metadata is None:
                        raise ConfigurationError("semantic support is not installed")
                    client = self.client(metadata)
                    if client.health(timeout=1):
                        return client
                    return self._start_service(metadata, client)
            except UpdateLockBusyError:
                time.sleep(0.25)
        raise ConfigurationError("semantic service transition did not complete safely")

    def _start_service(
        self,
        metadata: dict[str, object],
        client: SemanticServiceClient,
    ) -> SemanticServiceClient:
        command = [
            str(self._venv_python(self._managed_path(metadata, "runtime"))),
            "-m",
            "helix_mcp_knowledge.semantic_worker",
            "--model-path",
            str(self._managed_path(metadata, "model_path")),
            "--vector-path",
            str(self._managed_path(metadata, "vector_path", must_exist=False)),
            "--collection",
            self.config.storage.qdrant.collection,
            "--dimension",
            str(self.config.embeddings.dimension),
            "--batch-size",
            str(self.config.embeddings.batch_size),
            "--max-seq-length",
            str(SEMANTIC_MAX_SEQUENCE_LENGTH),
            "--host",
            str(metadata["host"]),
            "--port",
            str(metadata["port"]),
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
        if self.config.embeddings.device:
            command.extend(["--device", self.config.embeddings.device])
        log_path = self.root / "semantic-service.log"
        environment = self._worker_environment()
        environment["HELIX_SEMANTIC_SERVICE_TOKEN"] = str(metadata["token"])
        kwargs: dict[str, object] = {
            "cwd": self.config.base_dir,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":  # pragma: no cover
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        with log_path.open("ab", buffering=0) as log:
            self._process = subprocess.Popen(command, stderr=log, **kwargs)
        threading.Thread(target=self._process.wait, daemon=True).start()
        return self._wait_for_service(client, process=self._process, log_path=log_path)

    @staticmethod
    def _wait_for_service(
        client: SemanticServiceClient,
        *,
        process: subprocess.Popen[bytes] | None = None,
        log_path: Path | None = None,
    ) -> SemanticServiceClient:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if client.health(timeout=1):
                return client
            if process is not None and process.poll() is not None:
                detail = (
                    log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                    if log_path is not None
                    else "no diagnostic output"
                )
                raise ConfigurationError(f"semantic service failed to start: {detail}")
            time.sleep(1)
        raise ConfigurationError("semantic service did not become ready within 180 seconds")

    def stop_service(self) -> None:
        if not self.root.exists():
            return
        self._validate_root(create=False)
        with UpdateLock(
            self.root / ".service.lock",
            timeout_seconds=SEMANTIC_SERVICE_LOCK_TIMEOUT_SECONDS,
        ):
            metadata = self._metadata()
            if metadata is None:
                return
            self._stop_service_client(self.client(metadata))

    @staticmethod
    def _stop_service_client(client: SemanticServiceClient) -> None:
        if client.health():
            client.shutdown()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and client.health(timeout=0.5):
                time.sleep(0.25)
            if client.health(timeout=0.5):
                raise ConfigurationError("semantic service did not stop cleanly")

    def remove(self) -> int:
        """Remove only the validated component root inside the managed workspace."""
        self._validate_root(create=False)
        self.stop_service()
        if self.root.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(self.root)
        ):
            raise ConfigurationError("refusing to remove a linked semantic component root")
        root = self.root.resolve()
        workspace = self.config.base_dir.resolve()
        try:
            relative = root.relative_to(workspace)
        except ValueError as exc:
            raise ConfigurationError(
                "refusing to remove a semantic component outside the managed workspace"
            ) from exc
        if not relative.parts or root == workspace or root.name != "semantic":
            raise ConfigurationError("refusing to remove an unexpected semantic component path")
        if not root.exists():
            return 0
        entries = list(root.rglob("*"))
        allowed_links: list[Path] = []
        for entry in entries:
            if entry.is_symlink():
                if self._is_internal_venv_lib64_link(entry, root):
                    allowed_links.append(entry)
                    continue
                raise ConfigurationError(
                    f"refusing to remove semantic storage containing a link: {entry}"
                )
            if hasattr(os.path, "isjunction") and os.path.isjunction(entry):
                raise ConfigurationError(
                    f"refusing to remove semantic storage containing a link: {entry}"
                )
        reclaimed = self._directory_size(root)
        for link in allowed_links:
            link.unlink()
        shutil.rmtree(root)
        return reclaimed

    @staticmethod
    def _is_internal_venv_lib64_link(path: Path, root: Path) -> bool:
        """Accept only the standard POSIX virtualenv ``lib64 -> lib`` link."""
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
            and relative.parts[1].startswith("semantic-")
            and relative.parts[2] == "lib64"
            and os.readlink(path) == "lib"
            and target == expected
            and target.is_dir()
            and not target.is_symlink()
        )

    def _validate_root(self, *, create: bool) -> None:
        root = self.root
        components = self.config.base_dir / "components"
        if components.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(components)
        ):
            raise ConfigurationError("semantic component parent is unexpectedly linked")
        if root.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(root)):
            raise ConfigurationError("semantic component root is unexpectedly linked")
        resolved = root.resolve()
        workspace = self.config.base_dir.resolve()
        try:
            relative = resolved.relative_to(workspace)
        except ValueError as exc:
            raise ConfigurationError(
                "semantic component root is outside the managed workspace"
            ) from exc
        if not relative.parts or resolved == workspace or resolved.name != "semantic":
            raise ConfigurationError("semantic component root is invalid")
        if root.exists() and not root.is_dir():
            raise ConfigurationError("semantic component root is invalid")
        if create:
            root.mkdir(parents=True, exist_ok=True)

    def _metadata(self) -> dict[str, object] | None:
        path = self.metadata_path
        if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
            raise ValueError("semantic component metadata is unexpectedly linked")
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError("semantic component metadata is invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._validate_metadata_payload(payload)
        return payload

    @staticmethod
    def _validate_metadata_payload(payload: object) -> None:
        required = {
            "component_version",
            "runtime",
            "model_path",
            "vector_path",
            "host",
            "port",
            "token",
        }
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not required.issubset(payload)
        ):
            raise ValueError("semantic component metadata is invalid")
        if payload.get("host") != SEMANTIC_HOST or payload.get("port") != SEMANTIC_PORT:
            raise ValueError("semantic component endpoint is invalid")
        token = payload.get("token")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("semantic component token is invalid")
        component_version = payload.get("component_version")
        if (
            isinstance(component_version, bool)
            or not isinstance(component_version, int)
            or component_version < 1
        ):
            raise ValueError("semantic component version is invalid")

    def _managed_path(
        self,
        metadata: dict[str, object],
        key: str,
        *,
        must_exist: bool = True,
    ) -> Path:
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"semantic {key} is invalid")
        path = (self.root / value).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError(f"semantic {key} escapes its managed root") from exc
        if must_exist and not path.exists():
            raise ValueError(f"semantic {key} is missing")
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
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _remove_candidate(path: Path, parent: Path) -> None:
        if not path.exists():
            return
        resolved = path.resolve()
        if path.is_symlink() or resolved.parent != parent.resolve():
            raise ConfigurationError("refusing to clean an unexpected semantic component path")
        shutil.rmtree(resolved)

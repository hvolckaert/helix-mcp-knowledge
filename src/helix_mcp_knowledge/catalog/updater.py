"""Checksum-verified updates for the independently published BMC catalog."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ..config import AppConfig
from ..github_public import (
    GITHUB_API_ROOT,
    GITHUB_RELEASE_ROOT,
    PublicGitHubTransport,
    ReleaseTransport,
    verify_public_attestation,
)
from ..openclaw import CommandRunner, _resolve_command
from ..storage.automation import AutomationStore
from ..sync.manifest import OfficialSourceManifest
from .products import ProductCatalog

LOGGER = logging.getLogger(__name__)
LEASE_NAME = "official-catalog-update"
JOB_ID = "official-catalog-update"
STARTUP_DELAY_SECONDS = 7.0
MAX_CATALOG_BYTES = 5 * 1024 * 1024
_CHECKSUM_PATTERN = re.compile(r"^([0-9a-fA-F]{64})\s+\*?([^\s]+)\s*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CatalogUpdateStatus:
    status: str
    current_revision: int
    latest_revision: int | None = None
    checked_at: str | None = None
    next_check_at: str | None = None
    release_url: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogRelease:
    revision: int
    tag: str
    url: str
    asset_sha256: dict[str, str]


class CatalogUpdateChecker:
    """Discover and adopt immutable catalog releases without updating the runtime."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: AutomationStore,
        runner: CommandRunner = subprocess.run,
        transport: ReleaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        owner_id: str | None = None,
        startup_delay_seconds: float = STARTUP_DELAY_SECONDS,
        on_updated: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.settings = config.catalog_updates
        self.store = store
        self.runner = runner
        self.transport = transport or PublicGitHubTransport()
        self.clock = clock
        self.owner_id = owner_id or f"catalog_check_{uuid.uuid4()}"
        self.startup_delay_seconds = max(0.0, startup_delay_seconds)
        self.on_updated = on_updated
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mutex = threading.Lock()
        self._activated_revision = self._safe_current_revision()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="helix-catalog-update-check",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 0.25) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
            if not self._thread.is_alive():
                self._thread = None

    def status(self) -> CatalogUpdateStatus:
        if not self.settings.enabled:
            return CatalogUpdateStatus(
                status="disabled", current_revision=self._safe_current_revision()
            )
        return self._response(self.store.state(JOB_ID) or {"status": "unknown"})

    def check(self, *, force: bool = False) -> CatalogUpdateStatus:
        if not self.settings.enabled:
            return self.status()
        if not self._mutex.acquire(blocking=False):
            return self.status()
        try:
            if not force and not self._is_due():
                self._activate_cached_catalog()
                return self.status()
            lease_seconds = max(self.settings.timeout_seconds * 3 + 30, 120)
            if not self.store.acquire_or_renew(LEASE_NAME, self.owner_id, lease_seconds):
                return self.status()
            try:
                if not force and not self._is_due():
                    self._activate_cached_catalog()
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
                LOGGER.exception("embedded catalog update check failed")
            self._stop.wait(self._wait_seconds())

    def _perform_check(self) -> CatalogUpdateStatus:
        previous = self.store.state(JOB_ID)
        self.store.update_state(
            JOB_ID,
            {
                **self._known_release(previous),
                "status": "checking",
                "owner_id": self.owner_id,
                "started_at": self._timestamp(self.clock()),
            },
        )
        try:
            release = self._latest_release()
            latest_revision = release.revision
            current_revision = self._current_revision()
            if latest_revision < current_revision:
                raise ValueError(
                    f"latest catalog revision {latest_revision} is older than local "
                    f"revision {current_revision}"
                )
            updated = False
            if latest_revision > current_revision:
                manifest = self._download_and_validate(release)
                self._atomic_write_cache(manifest)
                self._activate_cached_catalog()
                current_revision = latest_revision
                updated = True
            checked = self.clock()
            state: dict[str, object] = {
                "status": "updated" if updated else "current",
                "current_revision": current_revision,
                "latest_revision": latest_revision,
                "release_url": release.url,
                "checked_at": self._timestamp(checked),
                "next_check_at": self._timestamp(checked + self.settings.interval_hours * 3600),
                "next_check_epoch": checked + self.settings.interval_hours * 3600,
            }
        except Exception as exc:
            checked = self.clock()
            state = {
                **self._known_release(previous),
                "status": "error",
                "current_revision": self._current_revision(),
                "checked_at": self._timestamp(checked),
                "next_check_at": self._timestamp(checked + self.settings.retry_minutes * 60),
                "next_check_epoch": checked + self.settings.retry_minutes * 60,
                "error": str(exc)[:1000],
            }
        self.store.update_state(JOB_ID, state)
        return self._response(state)

    def _latest_release(self) -> CatalogRelease:
        payload = self.transport.get_json(
            f"{GITHUB_API_ROOT}/repos/{self.settings.repository}/releases?per_page=100",
            timeout=self.settings.timeout_seconds,
            action="GitHub catalog release check",
        )
        if not isinstance(payload, list):
            raise ValueError("GitHub catalog release list must be a JSON array")
        pattern = re.compile(rf"^{re.escape(self.settings.release_prefix)}([0-9]+)$")
        candidates: list[tuple[int, dict[str, object]]] = []
        for item in payload:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            tag = str(item.get("tag_name") or "")
            match = pattern.fullmatch(tag)
            if match:
                candidates.append((int(match.group(1)), item))
        if not candidates:
            raise ValueError(
                f"no published catalog release matches {self.settings.release_prefix}<revision>"
            )
        revision, release = max(candidates, key=lambda candidate: candidate[0])
        tag = str(release["tag_name"])
        asset_sha256 = {
            asset_name: self._release_asset_sha256(release, asset_name=asset_name, tag=tag)
            for asset_name in (self.settings.manifest_asset, self.settings.checksum_asset)
        }
        return CatalogRelease(
            revision=revision,
            tag=tag,
            url=(
                str(release.get("html_url") or "")
                or f"{GITHUB_RELEASE_ROOT}/{self.settings.repository}/releases/tag/{tag}"
            ),
            asset_sha256=asset_sha256,
        )

    def _download_and_validate(self, release: CatalogRelease) -> OfficialSourceManifest:
        gh_command = self._resolve_gh_command()
        with tempfile.TemporaryDirectory(prefix="helix-catalog-") as temporary:
            destination = Path(temporary).resolve()
            for asset_name, expected_sha256 in release.asset_sha256.items():
                asset_path = destination / asset_name
                self.transport.download(
                    (
                        f"{GITHUB_RELEASE_ROOT}/{self.settings.repository}/releases/download/"
                        f"{release.tag}/{asset_name}"
                    ),
                    asset_path,
                    timeout=self.settings.timeout_seconds * 2,
                    action="GitHub catalog asset download",
                )
                self._validate_download_path(asset_path, destination)
                actual_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()
                if actual_sha256 != expected_sha256:
                    raise ValueError(f"catalog release digest verification failed for {asset_name}")
                verify_public_attestation(
                    gh_command,
                    asset_path,
                    repository=self.settings.repository,
                    sha256=expected_sha256,
                    release_tag=release.tag,
                    signer_workflow=".github/workflows/publish-catalog.yml",
                    source_ref="refs/heads/main",
                    runner=self.runner,
                    transport=self.transport,
                )
            manifest_path = destination / self.settings.manifest_asset
            checksum_path = destination / self.settings.checksum_asset
            manifest_bytes = manifest_path.read_bytes()
            if len(manifest_bytes) > MAX_CATALOG_BYTES:
                raise ValueError("downloaded catalog exceeds the 5 MiB safety limit")
            match = _CHECKSUM_PATTERN.fullmatch(checksum_path.read_text(encoding="utf-8"))
            if match is None or match.group(2) != self.settings.manifest_asset:
                raise ValueError("catalog checksum file has an invalid format or filename")
            actual_digest = hashlib.sha256(manifest_bytes).hexdigest()
            if actual_digest.casefold() != match.group(1).casefold():
                raise ValueError("catalog SHA-256 verification failed")
            manifest = OfficialSourceManifest.load(manifest_path)
        if manifest.schema_version != 2:
            raise ValueError("remote catalog must use schema_version 2")
        if manifest.catalog_revision != release.revision:
            raise ValueError(
                f"catalog release revision {release.revision} does not match manifest "
                f"revision {manifest.catalog_revision}"
            )
        if not manifest.products or not manifest.collections:
            raise ValueError("remote catalog must contain products and collections")
        catalog = ProductCatalog.from_manifest(manifest)
        for item in [*manifest.sources, *manifest.collections]:
            catalog.resolve(item.product)
        allowed_domains = {
            domain.casefold() for domain in self.config.ingestion.http.allowed_domains
        }
        for url in [
            *(source.url for source in manifest.sources),
            *(collection.root_url for collection in manifest.collections),
        ]:
            hostname = (urlsplit(url).hostname or "").casefold()
            if hostname not in allowed_domains:
                raise ValueError(f"catalog URL uses a domain outside the allowlist: {hostname}")
        return manifest

    @staticmethod
    def _release_asset_sha256(payload: dict[str, object], *, asset_name: str, tag: str) -> str:
        assets = payload.get("assets")
        matching = (
            [item for item in assets if isinstance(item, dict) and item.get("name") == asset_name]
            if isinstance(assets, list)
            else []
        )
        if len(matching) != 1:
            raise ValueError(f"catalog release {tag} must contain exactly one {asset_name} asset")
        digest = str(matching[0].get("digest") or "")
        algorithm, separator, value = digest.partition(":")
        sha256 = value.casefold() if separator and algorithm.casefold() == "sha256" else ""
        if not _SHA256_PATTERN.fullmatch(sha256):
            raise ValueError(f"catalog release {tag} has no valid SHA-256 digest for {asset_name}")
        return sha256

    def _resolve_gh_command(self) -> Path:
        configured = self.settings.gh_command
        if configured == "gh" and self.config.updates.gh_command != "gh":
            configured = self.config.updates.gh_command
        return _resolve_command(configured, label="GitHub CLI")

    def _atomic_write_cache(self, manifest: OfficialSourceManifest) -> None:
        destination = self.config.official_catalog_cache_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = manifest.model_dump_json(indent=2).encode()
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, destination)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def _current_revision(self) -> int:
        from .distribution import load_effective_official_catalog

        return load_effective_official_catalog(self.config).catalog_revision

    def _safe_current_revision(self) -> int:
        try:
            return self._current_revision()
        except Exception:
            return 0

    def _activate_cached_catalog(self) -> None:
        revision = self._current_revision()
        if revision == self._activated_revision:
            return
        if self.on_updated is not None:
            self.on_updated()
        self._activated_revision = revision

    @staticmethod
    def _validate_download_path(path: Path, parent: Path) -> None:
        if not path.is_file() or path.is_symlink() or path.parent.resolve(strict=True) != parent:
            raise ValueError(f"catalog release asset is missing or unsafe: {path.name}")

    def _is_due(self) -> bool:
        next_check = self.store.state(JOB_ID).get("next_check_epoch")
        return not isinstance(next_check, (int, float)) or self.clock() >= next_check

    def _wait_seconds(self) -> float:
        next_check = self.store.state(JOB_ID).get("next_check_epoch")
        if not isinstance(next_check, (int, float)):
            return 1.0
        return min(300.0, max(1.0, next_check - self.clock()))

    def _response(self, state: dict[str, object]) -> CatalogUpdateStatus:
        return CatalogUpdateStatus(
            status=str(state.get("status") or "unknown"),
            current_revision=self._safe_current_revision(),
            latest_revision=self._optional_int(state.get("latest_revision")),
            checked_at=self._optional_string(state.get("checked_at")),
            next_check_at=self._optional_string(state.get("next_check_at")),
            release_url=self._optional_string(state.get("release_url")),
            error=self._optional_string(state.get("error")),
        )

    @staticmethod
    def _known_release(state: dict[str, object]) -> dict[str, object]:
        return {key: state[key] for key in ("latest_revision", "release_url") if key in state}

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _timestamp(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat()

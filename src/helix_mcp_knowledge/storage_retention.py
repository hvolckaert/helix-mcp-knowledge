"""Safe automatic retention for managed update artifacts."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .config import AppConfig, UpdateRetentionSettings
from .errors import KnowledgeError
from .managed_installation import load_managed_installation
from .update_lock import UpdateLock, UpdateLockBusyError

_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_LEGACY_BACKUP_PATTERN = re.compile(
    r"^pre-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
    r"(?:-manual)?-(?P<timestamp>[0-9]{8}T[0-9]{6}Z)$"
)


@dataclass(frozen=True)
class RetentionResult:
    status: Literal["completed", "disabled", "deferred", "error"]
    reclaimed_bytes: int = 0
    removed_runtimes: list[str] = field(default_factory=list)
    removed_backups: list[str] = field(default_factory=list)
    removed_downloads: list[str] = field(default_factory=list)
    removed_diagnostics: list[str] = field(default_factory=list)
    trimmed_logs: list[str] = field(default_factory=list)
    protected_entries: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _Removal:
    category: Literal["runtime", "backup", "download"]
    path: Path
    size: int


def apply_storage_retention(
    *,
    workspace: Path,
    settings: UpdateRetentionSettings,
    active_version: str,
    previous_version: str | None,
    now: datetime | None = None,
) -> RetentionResult:
    """Remove only classified, inactive update artifacts after a successful update."""

    if not settings.enabled:
        return RetentionResult(status="disabled")
    try:
        removals, protected = _retention_plan(
            workspace=workspace,
            settings=settings,
            active_version=active_version,
            previous_version=previous_version,
            now=now or datetime.now(UTC),
        )
    except (KnowledgeError, OSError, ValueError, json.JSONDecodeError) as exc:
        return RetentionResult(status="error", error=str(exc))

    removed: dict[str, list[str]] = {"runtime": [], "backup": [], "download": []}
    reclaimed = 0
    try:
        for removal in removals:
            _remove_tree(removal.path)
            removed[removal.category].append(removal.path.name)
            reclaimed += removal.size
    except OSError as exc:
        return RetentionResult(
            status="error",
            reclaimed_bytes=reclaimed,
            removed_runtimes=removed["runtime"],
            removed_backups=removed["backup"],
            removed_downloads=removed["download"],
            protected_entries=protected,
            error=str(exc),
        )
    return RetentionResult(
        status="completed",
        reclaimed_bytes=reclaimed,
        removed_runtimes=removed["runtime"],
        removed_backups=removed["backup"],
        removed_downloads=removed["download"],
        protected_entries=protected,
    )


def maintain_managed_storage(config: AppConfig) -> RetentionResult:
    """Enforce retention on normal starts as well as immediately after updates."""

    if not config.updates.retention.enabled:
        return RetentionResult(status="disabled")
    try:
        with UpdateLock(config.base_dir / ".update.lock"):
            operational = _maintain_operational_files(config)
            managed = load_managed_installation(config.base_dir)
            if managed is None:
                return operational
            previous_version = _previous_successful_version(
                config.base_dir, active_version=managed.active_version
            )
            artifacts = apply_storage_retention(
                workspace=config.base_dir,
                settings=config.updates.retention,
                active_version=managed.active_version,
                previous_version=previous_version,
            )
            return _combine_results(artifacts, operational)
    except UpdateLockBusyError:
        return RetentionResult(status="deferred")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return RetentionResult(status="error", error=str(exc))


def _maintain_operational_files(
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> RetentionResult:
    workspace = config.base_dir.expanduser().resolve(strict=True)
    configured_errors = config.resolve_path(config.paths.errors)
    if not configured_errors.exists():
        return RetentionResult(status="completed")
    if _is_link(configured_errors):
        return RetentionResult(
            status="completed",
            protected_entries=["operational-errors-linked"],
        )
    errors_path = configured_errors.resolve(strict=True)
    try:
        relative = errors_path.relative_to(workspace)
    except ValueError:
        return RetentionResult(
            status="completed",
            protected_entries=["operational-errors-outside-workspace"],
        )
    if not relative.parts or not errors_path.is_dir() or _is_link(errors_path):
        raise ValueError(f"managed errors path must be a real workspace directory: {errors_path}")

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = current_time - timedelta(days=config.updates.retention.diagnostic_days)
    max_log_bytes = config.updates.retention.max_log_size_mb * 1024 * 1024
    removed: list[str] = []
    trimmed: list[str] = []
    reclaimed = 0
    for path in _children(errors_path):
        if not path.is_file() or _is_link(path):
            continue
        if re.fullmatch(r"ingestion-\d{8}T\d{12}Z\.json", path.name):
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if modified < cutoff:
                size = path.stat().st_size
                path.unlink()
                removed.append(path.name)
                reclaimed += size
        elif re.fullmatch(r"[A-Za-z0-9._-]+\.log", path.name):
            size = path.stat().st_size
            if size > max_log_bytes:
                _trim_log(path, max_log_bytes)
                trimmed.append(path.name)
                reclaimed += size - path.stat().st_size
    return RetentionResult(
        status="completed",
        reclaimed_bytes=reclaimed,
        removed_diagnostics=removed,
        trimmed_logs=trimmed,
    )


def _trim_log(path: Path, max_bytes: int) -> None:
    if path.parent == path or not path.is_file() or _is_link(path):
        raise ValueError(f"unsafe log retention target: {path}")
    with path.open("r+b") as stream:
        stream.seek(-max_bytes, os.SEEK_END)
        tail = stream.read(max_bytes)
        stream.seek(0)
        stream.write(tail)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def _combine_results(artifacts: RetentionResult, operational: RetentionResult) -> RetentionResult:
    if artifacts.status == "error":
        return artifacts
    return RetentionResult(
        status=artifacts.status,
        reclaimed_bytes=artifacts.reclaimed_bytes + operational.reclaimed_bytes,
        removed_runtimes=artifacts.removed_runtimes,
        removed_backups=artifacts.removed_backups,
        removed_downloads=artifacts.removed_downloads,
        removed_diagnostics=operational.removed_diagnostics,
        trimmed_logs=operational.trimmed_logs,
        protected_entries=sorted({*artifacts.protected_entries, *operational.protected_entries}),
        error=artifacts.error or operational.error,
    )


def _retention_plan(
    *,
    workspace: Path,
    settings: UpdateRetentionSettings,
    active_version: str,
    previous_version: str | None,
    now: datetime,
) -> tuple[list[_Removal], list[str]]:
    resolved_workspace = workspace.expanduser().resolve(strict=True)
    if not resolved_workspace.is_dir() or _is_link(resolved_workspace):
        raise ValueError(f"managed workspace must be a real directory: {resolved_workspace}")
    if now.tzinfo is None:
        raise ValueError("retention clock must be timezone-aware")
    current_time = now.astimezone(UTC)
    removals: list[_Removal] = []
    protected: list[str] = []

    runtime_root = _managed_root(resolved_workspace, "runtime")
    if runtime_root is not None:
        versioned_runtimes: dict[str, Path] = {}
        for path in _children(runtime_root):
            if not path.is_dir() or _is_link(path):
                if path.name != "installation.json":
                    protected.append(f"runtime/{path.name}")
                continue
            if not _VERSION_PATTERN.fullmatch(path.name):
                protected.append(f"runtime/{path.name}")
            else:
                versioned_runtimes[path.name] = path
        kept_previous: list[str] = []
        if (
            settings.previous_runtimes
            and previous_version is not None
            and previous_version != active_version
            and previous_version in versioned_runtimes
        ):
            kept_previous.append(previous_version)
        for version in sorted(versioned_runtimes, key=_version_key, reverse=True):
            if version == active_version or version in kept_previous:
                continue
            if len(kept_previous) < settings.previous_runtimes:
                kept_previous.append(version)
        keep_runtimes = {active_version, *kept_previous}
        for version, path in versioned_runtimes.items():
            if version not in keep_runtimes:
                removals.append(_removal("runtime", path, runtime_root))

    backup_root = _managed_root(resolved_workspace, "backups")
    if backup_root is not None:
        successful: list[tuple[int, Path]] = []
        failed: list[tuple[datetime, Path]] = []
        for path in _children(backup_root):
            if not path.is_dir() or _is_link(path):
                protected.append(f"backups/{path.name}")
                continue
            manifest = path / "update-result.json"
            if not manifest.is_file() or _is_link(manifest):
                legacy_timestamp = _legacy_backup_time(path.name)
                if legacy_timestamp is None:
                    protected.append(f"backups/{path.name}")
                else:
                    successful.append((legacy_timestamp, path))
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                protected.append(f"backups/{path.name}")
                continue
            status = payload.get("status")
            if status == "updated":
                successful.append((manifest.stat().st_mtime_ns, path))
            elif status == "failed":
                try:
                    failed_at = _manifest_time(payload.get("failed_at"))
                except (TypeError, ValueError):
                    protected.append(f"backups/{path.name}")
                else:
                    failed.append((failed_at, path))
            else:
                protected.append(f"backups/{path.name}")
        successful.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        for _, path in successful[settings.successful_backups :]:
            removals.append(_removal("backup", path, backup_root))
        cutoff = current_time - timedelta(days=settings.failed_backup_days)
        for failed_at, path in failed:
            if failed_at < cutoff:
                removals.append(_removal("backup", path, backup_root))

    download_root = _managed_root(resolved_workspace, "downloads")
    if download_root is not None:
        for path in _children(download_root):
            if not path.is_dir() or _is_link(path):
                protected.append(f"downloads/{path.name}")
                continue
            if not _VERSION_PATTERN.fullmatch(path.name):
                protected.append(f"downloads/{path.name}")
            elif path.name != active_version:
                removals.append(_removal("download", path, download_root))

    category_order = {"runtime": 0, "backup": 1, "download": 2}
    removals.sort(key=lambda item: (category_order[item.category], item.path.name))
    return removals, sorted(protected)


def _managed_root(workspace: Path, name: str) -> Path | None:
    root = workspace / name
    if not root.exists():
        return None
    if not root.is_dir() or _is_link(root):
        raise ValueError(f"managed {name} root must be a real directory: {root}")
    if root.parent.resolve(strict=True) != workspace:
        raise ValueError(f"managed {name} root is outside the workspace: {root}")
    return root


def _children(root: Path) -> list[Path]:
    return sorted((Path(entry.path) for entry in os.scandir(root)), key=lambda path: path.name)


def _manifest_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("failed update backup has no valid failed_at timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("failed update backup timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _legacy_backup_time(name: str) -> int | None:
    """Return a stable sort key for a recognized pre-manifest backup name."""

    match = _LEGACY_BACKUP_PATTERN.fullmatch(name)
    if match is None:
        return None
    try:
        parsed = datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int(parsed.timestamp() * 1_000_000_000)


def _previous_successful_version(workspace: Path, *, active_version: str) -> str | None:
    backup_root = workspace / "backups"
    if not backup_root.is_dir() or _is_link(backup_root):
        return None
    candidates: list[tuple[int, str]] = []
    for backup in _children(backup_root):
        manifest = backup / "update-result.json"
        if not backup.is_dir() or _is_link(backup) or not manifest.is_file():
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        previous = payload.get("current_version")
        if (
            payload.get("status") == "updated"
            and payload.get("target_version") == active_version
            and isinstance(previous, str)
            and _VERSION_PATTERN.fullmatch(previous)
        ):
            candidates.append((manifest.stat().st_mtime_ns, previous))
    if not candidates:
        return None
    return max(candidates)[1]


def _version_key(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _removal(
    category: Literal["runtime", "backup", "download"], path: Path, parent: Path
) -> _Removal:
    if path.parent != parent or not path.exists() or not path.is_dir() or _is_link(path):
        raise ValueError(f"unsafe retention target: {path}")
    return _Removal(category=category, path=path, size=_tree_size(path))


def _tree_size(root: Path) -> int:
    total = root.lstat().st_size
    for entry in os.scandir(root):
        path = Path(entry.path)
        if entry.is_symlink() or _is_junction(path):
            total += path.lstat().st_size
        elif entry.is_dir(follow_symlinks=False):
            total += _tree_size(path)
        else:
            total += path.lstat().st_size
    return total


def _remove_tree(root: Path) -> None:
    if not root.is_dir() or _is_link(root):
        raise OSError(f"refusing to remove unsafe retention target: {root}")
    for entry in os.scandir(root):
        path = Path(entry.path)
        if entry.is_symlink():
            path.unlink()
        elif _is_junction(path):
            path.rmdir()
        elif entry.is_dir(follow_symlinks=False):
            _remove_tree(path)
        else:
            path.unlink()
    root.rmdir()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or _is_junction(path)


def _is_junction(path: Path) -> bool:
    return bool(hasattr(path, "is_junction") and path.is_junction())

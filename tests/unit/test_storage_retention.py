from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from helix_mcp_knowledge.config import UpdateRetentionSettings, load_config
from helix_mcp_knowledge.managed_installation import (
    activate_managed_installation,
    versioned_runtime_paths,
)
from helix_mcp_knowledge.storage_retention import (
    apply_storage_retention,
    maintain_managed_storage,
)
from helix_mcp_knowledge.update_lock import UpdateLock


def _artifact(root: Path, name: str, content: bytes = b"artifact") -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "payload.bin").write_bytes(content)
    return path


def _backup(
    root: Path,
    name: str,
    *,
    status: str | None,
    failed_at: str | None = None,
    mtime: int | None = None,
) -> Path:
    path = _artifact(root, name)
    if status is not None:
        manifest = path / "update-result.json"
        payload: dict[str, object] = {"status": status}
        if failed_at is not None:
            payload["failed_at"] = failed_at
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        if mtime is not None:
            os.utime(manifest, (mtime, mtime))
    return path


def test_retention_keeps_active_previous_and_recent_recovery_state(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    for version in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"):
        _artifact(runtime, version)
    _artifact(runtime, "custom-runtime")
    (runtime / "installation.json").write_text("{}", encoding="utf-8")

    backups = tmp_path / "backups"
    old_success = _backup(backups, "update-old-success", status="updated", mtime=1)
    latest_success = _backup(backups, "update-latest-success", status="updated", mtime=2)
    old_failure = _backup(
        backups,
        "update-old-failure",
        status="failed",
        failed_at="2026-08-01T00:00:00+00:00",
    )
    recent_failure = _backup(
        backups,
        "update-recent-failure",
        status="failed",
        failed_at="2026-09-01T00:00:00+00:00",
    )
    manual_backup = _backup(backups, "pre-release-manual", status=None)

    downloads = tmp_path / "downloads"
    _artifact(downloads, "1.1.0")
    _artifact(downloads, "1.3.0")
    _artifact(downloads, "manual-download")

    result = apply_storage_retention(
        workspace=tmp_path,
        settings=UpdateRetentionSettings(),
        active_version="1.3.0",
        previous_version="1.2.0",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert result.status == "completed"
    assert result.removed_runtimes == ["1.0.0", "1.1.0"]
    assert result.removed_backups == [old_failure.name, old_success.name]
    assert result.removed_downloads == ["1.1.0"]
    assert result.reclaimed_bytes > 0
    assert set(result.protected_entries) == {
        "backups/pre-release-manual",
        "downloads/manual-download",
        "runtime/custom-runtime",
    }
    assert (runtime / "1.2.0").is_dir()
    assert (runtime / "1.3.0").is_dir()
    assert latest_success.is_dir()
    assert recent_failure.is_dir()
    assert manual_backup.is_dir()
    assert (downloads / "1.3.0").is_dir()


def test_retention_can_be_disabled_without_removing_artifacts(tmp_path: Path) -> None:
    old_runtime = _artifact(tmp_path / "runtime", "1.0.0")

    result = apply_storage_retention(
        workspace=tmp_path,
        settings=UpdateRetentionSettings(enabled=False),
        active_version="1.2.0",
        previous_version="1.1.0",
    )

    assert result.status == "disabled"
    assert result.reclaimed_bytes == 0
    assert old_runtime.is_dir()


def test_invalid_failed_manifest_is_protected(tmp_path: Path) -> None:
    backup = _backup(
        tmp_path / "backups",
        "update-invalid-failure",
        status="failed",
        failed_at="not-a-date",
    )

    result = apply_storage_retention(
        workspace=tmp_path,
        settings=UpdateRetentionSettings(),
        active_version="1.2.0",
        previous_version="1.1.0",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert result.status == "completed"
    assert result.protected_entries == ["backups/update-invalid-failure"]
    assert backup.is_dir()


def test_retention_classifies_only_strict_legacy_backup_names(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    oldest = _backup(
        backups,
        "pre-v1.0.1-20260825T090117Z",
        status=None,
    )
    middle = _backup(
        backups,
        "pre-v1.0.2-20260904T161405Z",
        status=None,
    )
    latest = _backup(
        backups,
        "pre-v1.9.0-manual-20260905T132016Z",
        status=None,
    )
    unrelated = _backup(backups, "pre-release-manual", status=None)
    invalid_date = _backup(
        backups,
        "pre-v1.10.0-20261340T256199Z",
        status=None,
    )

    result = apply_storage_retention(
        workspace=tmp_path,
        settings=UpdateRetentionSettings(),
        active_version="1.10.0",
        previous_version="1.9.0",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert result.status == "completed"
    assert result.removed_backups == [oldest.name, middle.name]
    assert set(result.protected_entries) == {
        f"backups/{invalid_date.name}",
        f"backups/{unrelated.name}",
    }
    assert latest.is_dir()
    assert unrelated.is_dir()
    assert invalid_date.is_dir()


def test_normal_start_enforces_retention_for_a_managed_installation(
    config_path: Path,
) -> None:
    config = load_config(config_path)
    workspace = config.base_dir
    active_python, active_server = versioned_runtime_paths(workspace, "1.3.0")
    active_python.parent.mkdir(parents=True)
    active_python.write_text("python", encoding="utf-8")
    active_server.write_text("server", encoding="utf-8")
    previous_runtime = _artifact(workspace / "runtime", "1.2.0")
    old_runtime = _artifact(workspace / "runtime", "1.1.0")
    activate_managed_installation(
        workspace=workspace,
        version="1.3.0",
        server_command=active_server,
        config_path=config_path,
        client="standalone",
    )
    _backup(workspace / "backups", "update-old", status="updated", mtime=1)
    latest = _backup(workspace / "backups", "update-latest", status="updated", mtime=2)
    (latest / "update-result.json").write_text(
        json.dumps(
            {
                "status": "updated",
                "current_version": "1.2.0",
                "target_version": "1.3.0",
            }
        ),
        encoding="utf-8",
    )

    result = maintain_managed_storage(config)

    assert result.status == "completed"
    assert result.removed_runtimes == ["1.1.0"]
    assert old_runtime.exists() is False
    assert previous_runtime.is_dir()
    assert active_python.parent.parent.parent.is_dir()


def test_normal_start_defers_cleanup_while_an_update_holds_the_lock(
    config_path: Path,
) -> None:
    config = load_config(config_path)
    workspace = config.base_dir
    active_python, active_server = versioned_runtime_paths(workspace, "1.3.0")
    active_server.parent.mkdir(parents=True)
    active_python.write_text("python", encoding="utf-8")
    active_server.write_text("server", encoding="utf-8")
    activate_managed_installation(
        workspace=workspace,
        version="1.3.0",
        server_command=active_server,
        config_path=config_path,
        client="standalone",
    )

    with UpdateLock(workspace / ".update.lock"):
        result = maintain_managed_storage(config)

    assert result.status == "deferred"


def test_normal_start_removes_old_diagnostics_and_caps_logs(config_path: Path) -> None:
    config = load_config(config_path)
    config.updates.retention.diagnostic_days = 30
    config.updates.retention.max_log_size_mb = 1
    errors = config.resolve_path(config.paths.errors)
    errors.mkdir(parents=True, exist_ok=True)
    diagnostic = errors / "ingestion-20200101T000000000000Z.json"
    diagnostic.write_text("old", encoding="utf-8")
    os.utime(diagnostic, (1, 1))
    log = errors / "dashboard-supervisor.log"
    log.write_bytes(b"old-prefix\n" + b"x" * (1024 * 1024))

    result = maintain_managed_storage(config)

    assert result.status == "completed"
    assert result.removed_diagnostics == [diagnostic.name]
    assert result.trimmed_logs == [log.name]
    assert result.reclaimed_bytes > 0
    assert diagnostic.exists() is False
    assert log.stat().st_size == 1024 * 1024


def test_normal_start_does_not_manage_external_error_directory(config_path: Path) -> None:
    config = load_config(config_path)
    external = config.base_dir.parent / "external-errors"
    external.mkdir()
    diagnostic = external / "ingestion-20200101T000000000000Z.json"
    diagnostic.write_text("keep", encoding="utf-8")
    config.paths.errors = external

    result = maintain_managed_storage(config)

    assert result.status == "completed"
    assert result.protected_entries == ["operational-errors-outside-workspace"]
    assert diagnostic.read_text(encoding="utf-8") == "keep"


def test_retention_unlinks_nested_symlink_without_touching_its_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    old_runtime = _artifact(tmp_path / "runtime", "1.0.0")
    link = old_runtime / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    _artifact(tmp_path / "runtime", "1.1.0")
    _artifact(tmp_path / "runtime", "1.2.0")

    result = apply_storage_retention(
        workspace=tmp_path,
        settings=UpdateRetentionSettings(),
        active_version="1.2.0",
        previous_version="1.1.0",
    )

    assert result.status == "completed"
    assert old_runtime.exists() is False
    assert outside.read_text(encoding="utf-8") == "keep"


def test_retention_refuses_a_linked_managed_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / "runtime").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    result = apply_storage_retention(
        workspace=workspace,
        settings=UpdateRetentionSettings(),
        active_version="1.2.0",
        previous_version="1.1.0",
    )

    assert result.status == "error"
    assert outside.is_dir()

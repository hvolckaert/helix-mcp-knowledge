"""Crash-safe state recovery for detached optional-component installers."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .storage.automation import AutomationStore

COMPONENT_INSTALL_LAUNCH_GRACE_SECONDS = 60.0
SAME_BOOT_CANDIDATE_GRACE_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class CandidateCleanupResult:
    reclaimed_bytes: int = 0
    deferred_candidates: int = 0


def current_boot_id() -> str | None:
    """Return a stable Linux/WSL boot identifier when the OS exposes one."""

    path = Path("/proc/sys/kernel/random/boot_id")
    try:
        value = path.read_text(encoding="ascii").strip().casefold()
    except OSError:
        return None
    return value if len(value) == 36 else None


def candidate_cleanup_minimum_age(state: dict[str, object]) -> float:
    """Delete immediately after reboot, otherwise allow surviving children to exit."""

    previous_boot = state.get("boot_id")
    boot = current_boot_id()
    if isinstance(previous_boot, str) and boot is not None and previous_boot != boot:
        return 0.0
    return float(SAME_BOOT_CANDIDATE_GRACE_SECONDS)


def candidate_is_old_enough(
    path: Path,
    *,
    minimum_age_seconds: float,
    now_epoch: float | None = None,
) -> bool:
    """Inspect mtimes without following symlinked or junctioned directories."""

    latest = path.lstat().st_mtime
    for directory, names, filenames in os.walk(path, followlinks=False):
        directory_path = Path(directory)
        retained_names: list[str] = []
        for name in names:
            entry = directory_path / name
            latest = max(latest, entry.lstat().st_mtime)
            if not entry.is_symlink() and not (
                hasattr(os.path, "isjunction") and os.path.isjunction(entry)
            ):
                retained_names.append(name)
        names[:] = retained_names
        for name in filenames:
            latest = max(latest, (directory_path / name).lstat().st_mtime)
    now = time.time() if now_epoch is None else float(now_epoch)
    return math.isfinite(now) and now - latest >= max(0.0, minimum_age_seconds)


def install_state_is_stale(
    state: dict[str, object],
    *,
    now_epoch: float | None = None,
) -> bool:
    """Return whether an installing state is old enough to recover.

    The caller must still acquire the global update lock before changing state or
    deleting candidates.  Holding that lock is the authoritative proof that the
    detached installer is no longer running.
    """

    if state.get("status") != "installing":
        return False
    requested_at = _state_epoch(state)
    if requested_at is None:
        return True
    now = time.time() if now_epoch is None else float(now_epoch)
    return (
        not math.isfinite(now) or abs(now - requested_at) > COMPONENT_INSTALL_LAUNCH_GRACE_SECONDS
    )


def mark_interrupted_install(
    store: AutomationStore,
    job_id: str,
    *,
    now_epoch: float | None = None,
) -> bool:
    """Atomically mark one stale operation as interrupted.

    This function must be called while holding the shared update lock.  The
    operation owner check prevents a delayed process from affecting a newer run.
    """

    state = store.state(job_id)
    if not install_state_is_stale(state, now_epoch=now_epoch):
        return False
    owner_id = state.get("owner_id")
    expected_owner = owner_id if isinstance(owner_id, str) and owner_id else None
    return store.patch_state_if_current(
        job_id,
        expected_status="installing",
        owner_id=expected_owner,
        values={
            "status": "error",
            "desired_enabled": False,
            "process_id": None,
            "owner_id": None,
            "interrupted": True,
            "finished_at": datetime.now(UTC).isoformat(),
            "error": (
                "The previous optional-component installation was interrupted. "
                "Incomplete files will be cleaned up safely; enable it again to retry."
            ),
        },
    )


def operation_is_current(
    store: AutomationStore,
    job_id: str,
    owner_id: str,
) -> bool:
    """Validate that a delayed detached process still owns the pending request."""

    state = store.state(job_id)
    return state.get("status") == "installing" and state.get("owner_id") == owner_id


def _state_epoch(state: dict[str, object]) -> float | None:
    raw_epoch = state.get("requested_at_epoch")
    if not isinstance(raw_epoch, bool):
        try:
            epoch = float(raw_epoch)
        except (TypeError, ValueError):
            pass
        else:
            if math.isfinite(epoch):
                return epoch
    for key in ("requested_at", "started_at"):
        raw = state.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None

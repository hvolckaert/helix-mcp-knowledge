from __future__ import annotations

import os
import time
from pathlib import Path

from helix_mcp_knowledge.component_install_recovery import (
    candidate_cleanup_minimum_age,
    candidate_is_old_enough,
    install_state_is_stale,
    mark_interrupted_install,
    operation_is_current,
)
from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.storage.automation import AutomationStore
from helix_mcp_knowledge.storage.database import Database


def _store(config_path: Path) -> AutomationStore:
    database = Database(load_config(config_path).sqlite_path)
    database.initialize()
    return AutomationStore(database)


def test_install_state_requires_a_launch_grace_period() -> None:
    assert not install_state_is_stale(
        {"status": "installing", "requested_at_epoch": 950.0},
        now_epoch=1_000.0,
    )
    assert install_state_is_stale(
        {"status": "installing", "requested_at_epoch": 900.0},
        now_epoch=1_000.0,
    )
    assert not install_state_is_stale({"status": "ready"}, now_epoch=1_000.0)


def test_mark_interrupted_install_is_owner_safe(config_path: Path) -> None:
    store = _store(config_path)
    store.update_state(
        "reranker-component-install",
        {
            "status": "installing",
            "owner_id": "old-owner",
            "requested_at_epoch": 1.0,
            "desired_enabled": True,
        },
    )

    assert mark_interrupted_install(
        store,
        "reranker-component-install",
        now_epoch=1_000.0,
    )
    state = store.state("reranker-component-install")
    assert state["status"] == "error"
    assert state["interrupted"] is True
    assert state["desired_enabled"] is False
    assert state["owner_id"] is None
    assert not operation_is_current(store, "reranker-component-install", "old-owner")


def test_candidate_age_checks_nested_files_without_following_links(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    nested = candidate / "model.bin"
    nested.write_bytes(b"model")
    old = time.time() - 600
    os.utime(candidate, (old, old))
    os.utime(nested, (old, old))

    assert candidate_is_old_enough(
        candidate,
        minimum_age_seconds=300,
        now_epoch=time.time(),
    )
    nested.touch()
    assert not candidate_is_old_enough(
        candidate,
        minimum_age_seconds=300,
        now_epoch=time.time(),
    )


def test_candidate_cleanup_is_immediate_after_a_known_reboot(monkeypatch) -> None:
    monkeypatch.setattr(
        "helix_mcp_knowledge.component_install_recovery.current_boot_id",
        lambda: "new-boot",
    )

    assert candidate_cleanup_minimum_age({"boot_id": "old-boot"}) == 0
    assert candidate_cleanup_minimum_age({"boot_id": "new-boot"}) > 0

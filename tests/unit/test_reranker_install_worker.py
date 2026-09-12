from __future__ import annotations

from pathlib import Path

from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.reranker_component import (
    RERANKER_COMPONENT_VERSION,
    RerankerComponentStatus,
)
from helix_mcp_knowledge.reranker_install_worker import RERANKER_INSTALL_JOB, run_install
from helix_mcp_knowledge.storage.automation import AutomationStore
from helix_mcp_knowledge.storage.database import Database


def _store(config_path: Path) -> AutomationStore:
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    return AutomationStore(database)


def _ready_status() -> RerankerComponentStatus:
    return RerankerComponentStatus(
        installed=True,
        status="ready",
        component_version=RERANKER_COMPONENT_VERSION,
        installed_bytes=4096,
        model_bytes=2048,
        service_ready=True,
    )


def test_install_worker_enables_only_after_component_validation(
    config_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_install_worker.RerankerComponentManager.install",
        lambda _self: _ready_status(),
    )

    assert run_install(config_path) is True

    assert load_config(config_path).retrieval.reranker.enabled is True
    state = _store(config_path).state(RERANKER_INSTALL_JOB)
    assert state["status"] == "ready"
    assert state["desired_enabled"] is True
    assert state["component_version"] == RERANKER_COMPONENT_VERSION


def test_install_worker_skips_cancelled_install(config_path: Path, monkeypatch) -> None:
    store = _store(config_path)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"status": "installing", "desired_enabled": False},
    )
    calls: list[object] = []
    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_install_worker.RerankerComponentManager.install",
        lambda _self: calls.append(object()),
    )

    assert run_install(config_path) is True

    assert calls == []
    assert load_config(config_path).retrieval.reranker.enabled is False
    assert _store(config_path).state(RERANKER_INSTALL_JOB)["cancelled"] is True


def test_disable_request_during_install_keeps_component_but_stops_service(
    config_path: Path, monkeypatch
) -> None:
    _store(config_path).update_state(
        RERANKER_INSTALL_JOB,
        {"status": "installing", "desired_enabled": True},
    )
    stopped: list[bool] = []

    def install(_manager):
        _store(config_path).update_state(
            RERANKER_INSTALL_JOB,
            {"status": "installing", "desired_enabled": False},
        )
        return _ready_status()

    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_install_worker.RerankerComponentManager.install",
        install,
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_install_worker.RerankerComponentManager.stop_service",
        lambda _self: stopped.append(True),
    )

    assert run_install(config_path) is True

    assert load_config(config_path).retrieval.reranker.enabled is False
    assert stopped == [True]
    state = _store(config_path).state(RERANKER_INSTALL_JOB)
    assert state["status"] == "ready"
    assert state["cancelled"] is True


def test_install_failure_leaves_reranker_disabled(config_path: Path, monkeypatch) -> None:
    def fail(_manager):
        raise RuntimeError("component failed")

    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_install_worker.RerankerComponentManager.install",
        fail,
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_install_worker.RerankerComponentManager.stop_service",
        lambda _self: None,
    )

    assert run_install(config_path) is False

    assert load_config(config_path).retrieval.reranker.enabled is False
    state = _store(config_path).state(RERANKER_INSTALL_JOB)
    assert state["status"] == "error"
    assert state["desired_enabled"] is False


def test_delayed_worker_cannot_take_over_a_newer_install(
    config_path: Path,
    monkeypatch,
) -> None:
    store = _store(config_path)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {
            "status": "installing",
            "desired_enabled": True,
            "owner_id": "new-owner",
        },
    )
    calls: list[object] = []
    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_install_worker.RerankerComponentManager.install",
        lambda _self: calls.append(object()),
    )

    assert run_install(config_path, owner_id="old-owner") is True

    assert calls == []
    assert _store(config_path).state(RERANKER_INSTALL_JOB)["owner_id"] == "new-owner"

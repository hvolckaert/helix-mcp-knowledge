from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.reranker_client import RERANKER_COMPONENT_VERSION
from helix_mcp_knowledge.reranker_install_worker import RERANKER_INSTALL_JOB
from helix_mcp_knowledge.storage.automation import AutomationStore


def _enable_reranker(config_path) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class _Client:
    def __init__(self, *, healthy: bool) -> None:
        self.healthy = healthy

    def health(self, *, timeout: float = 1.0) -> bool:
        assert timeout > 0
        return self.healthy


def test_disabled_reranker_does_not_touch_optional_component(config_path, monkeypatch) -> None:
    class UnexpectedManager:
        def __init__(self, _config) -> None:
            raise AssertionError("disabled reranker component was accessed")

    monkeypatch.setattr(
        "helix_mcp_knowledge.application.RerankerComponentManager",
        UnexpectedManager,
    )

    application = KnowledgeApplication.from_config(config_path)

    assert application.reranker is None
    assert application.search_engine.reranker is None


def test_unsupported_legacy_model_fails_open_when_enabled(config_path, monkeypatch) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["enabled"] = True
    payload["retrieval"]["reranker"]["model"] = "legacy/unused-model"
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    class UnexpectedManager:
        def __init__(self, _config) -> None:
            raise AssertionError("an unsupported optional model must not start")

    monkeypatch.setattr(
        "helix_mcp_knowledge.application.RerankerComponentManager",
        UnexpectedManager,
    )

    application = KnowledgeApplication.from_config(config_path)

    assert application.reranker is None
    state = AutomationStore(application.database).state(RERANKER_INSTALL_JOB)
    assert state["service_status"] == "degraded"
    assert "not supported" in state["service_error"]


def test_cold_reranker_starts_in_background_without_blocking_application(
    config_path, monkeypatch
) -> None:
    _enable_reranker(config_path)
    client = _Client(healthy=False)
    started: list[tuple[object, tuple[object, ...]]] = []

    class Manager:
        def __init__(self, _config) -> None:
            pass

        def status(self):
            return SimpleNamespace(
                installed=True,
                status="ready",
                component_version=RERANKER_COMPONENT_VERSION,
                error=None,
            )

        def client(self):
            return client

    class DeferredThread:
        def __init__(self, *, target, args, name, daemon) -> None:
            assert name == "helix-reranker-background-start"
            assert daemon is True
            self.target = target
            self.args = args

        def start(self) -> None:
            started.append((self.target, self.args))

    monkeypatch.setattr("helix_mcp_knowledge.application.RerankerComponentManager", Manager)
    monkeypatch.setattr("helix_mcp_knowledge.application.threading.Thread", DeferredThread)

    application = KnowledgeApplication.from_config(config_path)

    assert application.reranker is client
    assert application.search_engine.reranker is client
    assert len(started) == 1
    state = AutomationStore(application.database).state(RERANKER_INSTALL_JOB)
    assert state["service_status"] == "starting"
    assert state["desired_enabled"] is True


def test_ready_reranker_is_injected_without_background_start(config_path, monkeypatch) -> None:
    _enable_reranker(config_path)
    client = _Client(healthy=True)

    class Manager:
        def __init__(self, _config) -> None:
            pass

        def status(self):
            return SimpleNamespace(
                installed=True,
                status="ready",
                component_version=RERANKER_COMPONENT_VERSION,
                error=None,
            )

        def client(self):
            return client

    monkeypatch.setattr("helix_mcp_knowledge.application.RerankerComponentManager", Manager)
    monkeypatch.setattr(
        "helix_mcp_knowledge.application.threading.Thread",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected background start")),
    )

    application = KnowledgeApplication.from_config(config_path)

    assert application.search_engine.reranker is client
    state = AutomationStore(application.database).state(RERANKER_INSTALL_JOB)
    assert state["service_status"] == "ready"


def test_background_start_stops_service_when_disabled_during_model_load(
    config_path,
) -> None:
    application = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(application.database)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"service_status": "starting", "desired_enabled": True},
    )
    calls: list[str] = []

    class Manager:
        def request_service_start(self) -> None:
            pass

        def ensure_service(self) -> None:
            calls.append("started")
            store.patch_state(RERANKER_INSTALL_JOB, {"desired_enabled": False})

        def stop_service(self) -> None:
            calls.append("stopped")

    KnowledgeApplication._start_reranker_service(Manager(), store)  # type: ignore[arg-type]

    state = store.state(RERANKER_INSTALL_JOB)
    assert calls == ["started", "stopped"]
    assert state["service_status"] == "stopped"
    assert state["desired_enabled"] is False


def test_background_start_reconciles_reenable_during_shutdown(config_path) -> None:
    application = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(application.database)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"service_status": "stopping", "desired_enabled": False},
    )
    calls: list[str] = []

    class Manager:
        def request_service_start(self) -> None:
            pass

        def ensure_service(self) -> None:
            calls.append("started")

        def stop_service(self) -> None:
            calls.append("stopped")
            store.patch_state(RERANKER_INSTALL_JOB, {"desired_enabled": True})

    KnowledgeApplication._start_reranker_service(Manager(), store)  # type: ignore[arg-type]

    state = store.state(RERANKER_INSTALL_JOB)
    assert calls == ["stopped", "started"]
    assert state["service_status"] == "ready"
    assert state["desired_enabled"] is True


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("invalid component metadata"),
        json.JSONDecodeError("invalid component metadata", "{", 1),
    ],
)
def test_background_lifecycle_finishes_as_degraded_for_invalid_metadata(
    config_path,
    failure: Exception,
) -> None:
    application = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(application.database)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"service_status": "starting", "desired_enabled": True},
    )

    class Manager:
        def request_service_start(self) -> None:
            pass

        def ensure_service(self) -> None:
            raise failure

        def stop_service(self) -> None:
            raise failure

    KnowledgeApplication._start_reranker_service(Manager(), store)  # type: ignore[arg-type]

    state = store.state(RERANKER_INSTALL_JOB)
    assert state["service_status"] == "degraded"
    assert "invalid component metadata" in str(state["service_error"])
    assert state["desired_enabled"] is True


def test_background_start_clears_a_stale_downgrade_stop_request(config_path) -> None:
    application = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(application.database)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"service_status": "starting", "desired_enabled": True},
    )
    calls: list[str] = []

    class Manager:
        def request_service_start(self) -> None:
            calls.append("requested")

        def ensure_service(self) -> None:
            calls.append("started")

        def stop_service(self) -> None:
            calls.append("stopped")

    KnowledgeApplication._start_reranker_service(Manager(), store)  # type: ignore[arg-type]

    state = store.state(RERANKER_INSTALL_JOB)
    assert calls == ["requested", "started"]
    assert state["service_status"] == "ready"
    assert state["desired_enabled"] is True


def test_application_recovers_an_orphaned_disabled_service_transition(
    config_path,
    monkeypatch,
) -> None:
    initial = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(initial.database)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"service_status": "stopping", "desired_enabled": False},
    )
    stops: list[None] = []

    class Manager:
        def __init__(self, _config) -> None:
            pass

        def service_is_inactive(self) -> bool:
            return False

        def stop_service(self) -> None:
            stops.append(None)

    class ImmediateThread:
        def __init__(self, *, target, args, name, daemon) -> None:
            assert name == "helix-reranker-background-stop-recovery"
            assert daemon is True
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr("helix_mcp_knowledge.application.RerankerComponentManager", Manager)
    monkeypatch.setattr("helix_mcp_knowledge.application.threading.Thread", ImmediateThread)

    recovered = KnowledgeApplication.from_config(config_path)

    state = AutomationStore(recovered.database).state(RERANKER_INSTALL_JOB)
    assert stops == [None]
    assert state["service_status"] == "stopped"
    assert state["desired_enabled"] is False


def test_application_stops_a_live_component_when_durable_config_is_disabled(
    config_path,
    monkeypatch,
) -> None:
    initial = KnowledgeApplication.from_config(config_path)
    config = initial.config
    config.reranker_component_path.mkdir(parents=True)
    store = AutomationStore(initial.database)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"status": "ready", "service_status": "ready", "desired_enabled": True},
    )
    stops: list[None] = []

    class Manager:
        def __init__(self, _config) -> None:
            pass

        def service_is_inactive(self) -> bool:
            return False

        def stop_service(self) -> None:
            stops.append(None)

    class ImmediateThread:
        def __init__(self, *, target, args, name, daemon) -> None:
            assert name == "helix-reranker-background-stop-recovery"
            assert daemon is True
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr("helix_mcp_knowledge.application.RerankerComponentManager", Manager)
    monkeypatch.setattr("helix_mcp_knowledge.application.threading.Thread", ImmediateThread)

    recovered = KnowledgeApplication.from_config(config_path)

    state = AutomationStore(recovered.database).state(RERANKER_INSTALL_JOB)
    assert stops == [None]
    assert state["status"] == "ready"
    assert state["service_status"] == "stopped"
    assert state["desired_enabled"] is False


def test_application_does_not_reopen_a_completed_disabled_transition(
    config_path,
    monkeypatch,
) -> None:
    initial = KnowledgeApplication.from_config(config_path)
    config = initial.config
    config.reranker_component_path.mkdir(parents=True)
    store = AutomationStore(initial.database)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"status": "ready", "service_status": "stopping", "desired_enabled": False},
    )

    class Manager:
        def __init__(self, _config) -> None:
            pass

        def service_is_inactive(self) -> bool:
            return True

    monkeypatch.setattr("helix_mcp_knowledge.application.RerankerComponentManager", Manager)
    monkeypatch.setattr(
        "helix_mcp_knowledge.application.threading.Thread",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected recovery thread")),
    )

    recovered = KnowledgeApplication.from_config(config_path)

    state = AutomationStore(recovered.database).state(RERANKER_INSTALL_JOB)
    assert state["status"] == "ready"
    assert state["service_status"] == "stopped"
    assert state["service_error"] is None
    assert state["desired_enabled"] is False


def test_application_does_not_stop_the_provisional_disabled_installation(
    config_path,
    monkeypatch,
) -> None:
    initial = KnowledgeApplication.from_config(config_path)
    initial.config.reranker_component_path.mkdir(parents=True)
    store = AutomationStore(initial.database)
    store.update_state(
        RERANKER_INSTALL_JOB,
        {"status": "installing", "desired_enabled": True},
    )

    class UnexpectedManager:
        def __init__(self, _config) -> None:
            raise AssertionError("provisional installation was stopped")

    monkeypatch.setattr(
        "helix_mcp_knowledge.application.RerankerComponentManager",
        UnexpectedManager,
    )

    recovered = KnowledgeApplication.from_config(config_path)

    state = AutomationStore(recovered.database).state(RERANKER_INSTALL_JOB)
    assert state["status"] == "installing"
    assert state["desired_enabled"] is True

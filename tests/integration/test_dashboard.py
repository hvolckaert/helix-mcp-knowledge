import http.client
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.dashboard import (
    DashboardHTTPServer,
    DashboardProcessLauncher,
    DashboardService,
)
from helix_mcp_knowledge.dashboard_update_worker import DashboardUpdateWorkerLauncher
from helix_mcp_knowledge.errors import ConfigurationError, ProjectNotFoundError
from helix_mcp_knowledge.managed_installation import (
    ClientIntegration,
    activate_managed_installation,
)
from helix_mcp_knowledge.models.update import UpdateStatus
from helix_mcp_knowledge.ocr_component import OcrComponentStatus
from helix_mcp_knowledge.ocr_install_worker import OcrInstallWorkerLauncher
from helix_mcp_knowledge.official_worker import OfficialSyncWorkerLauncher
from helix_mcp_knowledge.release_checker import ReleaseUpdateChecker
from helix_mcp_knowledge.reranker_component import (
    RERANKER_COMPONENT_VERSION,
    RerankerComponentStatus,
)
from helix_mcp_knowledge.reranker_install_worker import RerankerInstallWorkerLauncher
from helix_mcp_knowledge.semantic_component import (
    SEMANTIC_COMPONENT_VERSION,
    SemanticComponentStatus,
)
from helix_mcp_knowledge.semantic_install_worker import SemanticInstallWorkerLauncher
from helix_mcp_knowledge.storage.automation import AutomationStore
from helix_mcp_knowledge.storage.database import Database
from helix_mcp_knowledge.storage_retention import RetentionResult
from helix_mcp_knowledge.update_lock import UpdateLock, UpdateLockBusyError


def _write_manifest(config_path: Path) -> None:
    manifest = config_path.parent / "sources/bmc-official-26.1.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        """schema_version: 1
collections:
  - collection_id: bmc-cmdb-26-1-dashboard
    root_url: https://docs.bmc.com/cmdb/26.1/
    local_path_prefix: cmdb/26.1/discovered
    product: cmdb
    version: "26.1"
  - collection_id: bmc-itsm-26-1-dashboard
    root_url: https://docs.bmc.com/itsm/26.1/
    local_path_prefix: itsm/26.1/discovered
    product: itsm
    version: "26.1"
  - collection_id: bmc-cmdb-26-2-dashboard
    root_url: https://docs.bmc.com/cmdb/26.2/
    local_path_prefix: cmdb/26.2/discovered
    product: cmdb
    version: "26.2"
""",
        encoding="utf-8",
    )


def _configuration() -> dict[str, object]:
    return {
        "products": {"cmdb": ["26.1", "26.2"], "itsm": ["26.1"]},
        "automatic_sync": True,
        "bootstrap_on_empty": True,
        "interval_hours": 12,
        "retain_unselected_versions": False,
        "projects": {
            "example_project": {"products": {"cmdb": "26.2"}},
            "atlas": {"products": {"cmdb": "26.1"}},
        },
    }


def test_dashboard_state_lists_available_selected_and_project_versions(config_path: Path) -> None:
    _write_manifest(config_path)

    state = DashboardService(config_path).state()

    products = {item["product_id"]: item for item in state["products"]}
    assert products["cmdb"]["available_versions"] == ["26.3", "26.2", "26.1"]
    assert products["cmdb"]["selected_versions"] == ["26.1"]
    assert products["itsm"]["available_versions"] == ["26.3", "26.2", "26.1"]
    assert products["arsystem"]["available_versions"] == ["26.3", "26.2", "26.1"]
    assert products["digital_workplace"]["available_versions"] == [
        "26.3",
        "26.2",
        "26.1",
    ]
    assert products["business_workflows"]["available_versions"] == [
        "26.3",
        "26.2",
        "26.1",
    ]
    assert products["discovery"]["available_versions"] == ["current"]
    assert state["catalog"]["effective_revision"] == 1
    assert state["catalog"]["current_revision"] == 1
    projects = {item["project_id"]: item for item in state["projects"]}
    assert projects["example_project"]["products"] == {"cmdb": "26.1"}
    assert state["sync"]["official"]["status"] == "not_started"
    assert state["ocr"]["enabled"] is False
    assert state["ocr"]["status"] == "not_installed"
    assert state["semantic"]["enabled"] is False
    assert state["semantic"]["status"] == "not_installed"
    assert state["semantic"]["estimated_active_memory_bytes"] == 4 * 1024 * 1024 * 1024
    assert state["reranker"]["enabled"] is False
    assert state["reranker"]["status"] == "not_installed"
    assert state["reranker"]["setup_requires_download"] is True


@pytest.mark.parametrize("component_status", ["update_required", "incompatible"])
def test_dashboard_requires_confirmation_state_for_reranker_replacement(
    config_path: Path,
    monkeypatch,
    component_status: str,
) -> None:
    _write_manifest(config_path)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.status",
        lambda _self, **_kwargs: RerankerComponentStatus(
            installed=True,
            status=component_status,
            component_version=RERANKER_COMPONENT_VERSION - 1,
        ),
    )

    state = DashboardService(config_path).state()

    assert state["reranker"]["installed"] is True
    assert state["reranker"]["setup_requires_download"] is True


def test_dashboard_state_reuses_application_until_configuration_changes(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)

    service.state()
    first = service._state_application
    service.state()
    assert service._state_application is first

    project_path = config_path.parent / "projects/example_project.yaml"
    project_path.write_text(
        project_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    service.state()
    assert service._state_application is not first


def test_dashboard_retries_storage_retention_after_a_busy_start(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    outcomes = iter(
        [
            RetentionResult(status="deferred"),
            RetentionResult(status="completed", reclaimed_bytes=1024),
        ]
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.maintain_managed_storage",
        lambda _config: next(outcomes),
    )

    state = DashboardService(config_path).state()

    assert state["storage_retention"]["status"] == "completed"
    assert state["storage_retention"]["reclaimed_bytes"] == 1024


def test_dashboard_starts_optional_ocr_install_on_first_activation(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.OcrComponentManager.status",
        lambda _self: OcrComponentStatus(installed=False, status="not_installed"),
    )
    monkeypatch.setattr(
        OcrInstallWorkerLauncher,
        "start",
        lambda _self: SimpleNamespace(pid=8642),
    )

    state = DashboardService(config_path).configure_ocr({"enabled": True})

    assert state["ocr"]["status"] == "installing"
    assert load_config(config_path).ingestion.ocr.enabled is False
    operation = AutomationStore(Database(load_config(config_path).sqlite_path)).state(
        "ocr-component-install"
    )
    assert operation["desired_enabled"] is True
    assert operation["process_id"] == 8642


def test_dashboard_cannot_start_ocr_during_managed_maintenance(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.OcrComponentManager.status",
        lambda _self: OcrComponentStatus(installed=False, status="not_installed"),
    )
    service = DashboardService(config_path)

    with (
        UpdateLock(config_path.parent.parent / ".update.lock"),
        pytest.raises(UpdateLockBusyError),
    ):
        service.configure_ocr({"enabled": True})


def test_dashboard_normalizes_a_stale_successful_update_state() -> None:
    normalized = DashboardService._dashboard_update_state(
        {
            "status": "success",
            "current_version": "1.13.2",
            "target_version": "1.14.0",
            "storage_retention": {
                "status": "completed",
                "reclaimed_bytes": 260506169,
            },
        },
        server_version="1.23.0",
    )

    assert normalized == {"status": "success", "target_version": "1.23.0"}


def test_dashboard_starts_optional_semantic_install_on_first_activation(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.SemanticComponentManager.status",
        lambda _self, **_kwargs: SemanticComponentStatus(installed=False, status="not_installed"),
    )
    monkeypatch.setattr(
        SemanticInstallWorkerLauncher,
        "start",
        lambda _self: SimpleNamespace(pid=9753),
    )

    state = DashboardService(config_path).configure_semantic({"enabled": True})

    assert state["semantic"]["status"] == "installing"
    assert load_config(config_path).retrieval.semantic.enabled is False
    operation = AutomationStore(Database(load_config(config_path).sqlite_path)).state(
        "semantic-component-install"
    )
    assert operation["desired_enabled"] is True
    assert operation["process_id"] == 9753
    assert isinstance(operation["requested_at_epoch"], float)


@pytest.mark.parametrize("active_status", ["installing", "indexing"])
def test_dashboard_recovers_interrupted_semantic_setup(
    config_path: Path,
    monkeypatch,
    active_status: str,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "semantic-component-install",
        {
            "status": active_status,
            "desired_enabled": True,
            "requested_at_epoch": time.time() - 3600,
        },
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.SemanticComponentManager.status",
        lambda _self, **_kwargs: SemanticComponentStatus(
            installed=False,
            status="not_installed",
        ),
    )

    state = service.state()

    operation = store.state("semantic-component-install")
    assert operation["status"] == "error"
    assert operation["desired_enabled"] is False
    assert operation["interrupted"] is True
    assert operation["process_id"] is None
    assert state["semantic"]["status"] == "error"
    assert "Enable it again" in state["semantic"]["error"]


def test_dashboard_does_not_recover_semantic_setup_while_worker_lock_is_held(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    original = {
        "status": "indexing",
        "desired_enabled": True,
        "requested_at_epoch": time.time() - 3600,
    }
    store.update_state("semantic-component-install", original)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.SemanticComponentManager.status",
        lambda _self, **_kwargs: SemanticComponentStatus(
            installed=False,
            status="not_installed",
        ),
    )

    with UpdateLock(config.base_dir / ".update.lock"):
        service._recover_interrupted_semantic_setup(config)

    assert store.state("semantic-component-install") == original


def test_dashboard_stops_installed_semantic_service_during_crash_recovery(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "semantic-component-install",
        {
            "status": "indexing",
            "desired_enabled": True,
            "requested_at_epoch": time.time() - 3600,
        },
    )
    stops: list[None] = []
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.SemanticComponentManager.status",
        lambda _self, **_kwargs: SemanticComponentStatus(
            installed=True,
            status="ready",
            component_version=SEMANTIC_COMPONENT_VERSION,
        ),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.SemanticComponentManager.stop_service",
        lambda _self: stops.append(None),
    )

    service._recover_interrupted_semantic_setup(config)

    operation = store.state("semantic-component-install")
    assert stops == [None]
    assert operation["status"] == "error"
    assert operation["service_status"] == "stopped"


def test_recent_semantic_setup_is_not_mistaken_for_a_crash(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    original = {
        "status": "installing",
        "desired_enabled": True,
        "requested_at_epoch": time.time(),
    }
    store.update_state("semantic-component-install", original)

    service._recover_interrupted_semantic_setup(config)

    assert store.state("semantic-component-install") == original


def test_dashboard_cancels_semantic_indexing_without_waiting_for_worker_lock(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "semantic-component-install",
        {
            "status": "indexing",
            "desired_enabled": True,
            "requested_at_epoch": time.time(),
        },
    )

    with UpdateLock(config.base_dir / ".update.lock"):
        state = service.configure_semantic({"enabled": False})

    operation = store.state("semantic-component-install")
    assert operation["status"] == "indexing"
    assert operation["desired_enabled"] is False
    assert state["semantic"]["status"] == "indexing"


def test_dashboard_requires_an_explicit_reindex_after_semantic_payload_upgrade(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["semantic"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    AutomationStore(database).update_state(
        "semantic-component-install",
        {"status": "ready", "desired_enabled": True, "payload_version": 1},
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.SemanticComponentManager.status",
        lambda _self, **_kwargs: SemanticComponentStatus(
            installed=True,
            status="ready",
            component_version=SEMANTIC_COMPONENT_VERSION,
        ),
    )

    state = DashboardService(config_path).state()

    assert state["semantic"]["status"] == "reindex_required"
    assert state["semantic"]["enabled"] is False


def test_dashboard_disables_an_outdated_semantic_component_until_it_is_upgraded(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["semantic"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    AutomationStore(database).update_state(
        "semantic-component-install",
        {"status": "ready", "desired_enabled": True, "payload_version": 2},
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.semantic_component.SemanticComponentManager.status",
        lambda _self, **_kwargs: SemanticComponentStatus(
            installed=True,
            status="update_required",
            component_version=SEMANTIC_COMPONENT_VERSION - 1,
            error=(
                "The installed semantic component must be updated before it can be used. "
                "Lexical search remains available."
            ),
        ),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.application.SemanticComponentManager.ensure_service",
        lambda _self: pytest.fail("an outdated semantic service must not start"),
    )

    state = DashboardService(config_path).state()

    assert state["semantic"]["status"] == "update_required"
    assert state["semantic"]["enabled"] is False
    assert "Lexical search remains available" in state["semantic"]["error"]


def test_dashboard_removes_semantic_component_only_while_disabled(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.SemanticComponentManager.remove",
        lambda _self: 8192,
    )

    state = DashboardService(config_path).remove_semantic()

    assert state["semantic"]["enabled"] is False
    operation = AutomationStore(Database(load_config(config_path).sqlite_path)).state(
        "semantic-component-install"
    )
    assert operation["status"] == "not_installed"
    assert operation["reclaimed_bytes"] == 8192


@pytest.mark.parametrize(
    ("configured_candidates", "expected_candidates"),
    [(20, 10), (33, 32)],
)
def test_dashboard_starts_optional_reranker_install_on_first_activation(
    config_path: Path,
    monkeypatch,
    configured_candidates: int,
    expected_candidates: int,
) -> None:
    _write_manifest(config_path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["model"] = "legacy/unused-model"
    payload["retrieval"]["reranker"]["candidates"] = configured_candidates
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.status",
        lambda _self, **_kwargs: RerankerComponentStatus(installed=False, status="not_installed"),
    )
    monkeypatch.setattr(
        RerankerInstallWorkerLauncher,
        "start",
        lambda _self: SimpleNamespace(pid=4862),
    )

    state = DashboardService(config_path).configure_reranker({"enabled": True})

    assert state["reranker"]["status"] == "installing"
    configured = load_config(config_path).retrieval.reranker
    assert configured.enabled is False
    assert configured.model == "BAAI/bge-reranker-v2-m3"
    assert configured.candidates == expected_candidates
    operation = AutomationStore(Database(load_config(config_path).sqlite_path)).state(
        "reranker-component-install"
    )
    assert operation["desired_enabled"] is True
    assert operation["process_id"] == 4862


def test_reranker_install_intent_precedes_every_provisional_config_write(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["model"] = "legacy/unused-model"
    payload["retrieval"]["reranker"]["candidates"] = 20
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    service = DashboardService(config_path)
    provider = service._state_application._reranker_runtime_provider
    database = Database(load_config(config_path).sqlite_path)
    store = AutomationStore(database)
    stop_requests: list[None] = []
    observed_writes: list[None] = []

    class ProviderManager:
        def __init__(self, _config) -> None:
            pass

        def request_service_stop(self) -> None:
            stop_requests.append(None)

    monkeypatch.setattr(
        "helix_mcp_knowledge.application.RerankerComponentManager",
        ProviderManager,
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.status",
        lambda _self, **_kwargs: RerankerComponentStatus(
            installed=False,
            status="not_installed",
        ),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.request_service_start",
        lambda _self: None,
    )
    monkeypatch.setattr(
        RerankerInstallWorkerLauncher,
        "start",
        lambda _self: SimpleNamespace(pid=4863),
    )
    atomic_write = service._atomic_yaml_write

    def interleaved_write(path, updated_payload) -> None:
        operation = store.state("reranker-component-install")
        assert operation["status"] == "installing"
        assert operation["desired_enabled"] is True
        atomic_write(path, updated_payload)
        runtime = provider()
        assert runtime.enabled is False
        observed_writes.append(None)

    monkeypatch.setattr(service, "_atomic_yaml_write", interleaved_write)

    state = service.configure_reranker({"enabled": True})

    assert state["reranker"]["status"] == "installing"
    assert len(observed_writes) == 2
    assert stop_requests == []


def test_dashboard_recovers_an_interrupted_reranker_install(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "reranker-component-install",
        {
            "status": "installing",
            "desired_enabled": True,
            "owner_id": "abandoned-owner",
            "requested_at_epoch": time.time() - 120,
        },
    )
    orphan = config.reranker_component_path / f"runtime/reranker-1-{'e' * 32}"
    orphan.mkdir(parents=True)
    (orphan / "partial").write_bytes(b"partial")
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.candidate_cleanup_minimum_age",
        lambda _state: 0,
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_component.RerankerComponentManager._port_accepting",
        lambda _self: False,
    )

    state = service.state()

    operation = store.state("reranker-component-install")
    assert state["reranker"]["status"] == "error"
    assert operation["interrupted"] is True
    assert operation["cleanup_pending"] is False
    assert operation["interrupted_reclaimed_bytes"] == len(b"partial")
    assert not orphan.exists()
    assert load_config(config_path).retrieval.reranker.enabled is False


def test_dashboard_does_not_recover_while_installer_lock_is_held(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "reranker-component-install",
        {
            "status": "installing",
            "desired_enabled": True,
            "owner_id": "live-owner",
            "requested_at_epoch": time.time() - 120,
        },
    )

    with UpdateLock(config.base_dir / ".update.lock"):
        state = service.state()

    assert state["reranker"]["status"] == "installing"
    assert store.state("reranker-component-install")["owner_id"] == "live-owner"


def test_dashboard_reconciles_an_orphaned_completed_reranker_stop(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "reranker-component-install",
        {"status": "ready", "service_status": "stopping", "desired_enabled": False},
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.service_is_inactive",
        lambda _self: True,
    )

    service._recover_interrupted_reranker_install(config, store=store)

    operation = store.state("reranker-component-install")
    assert operation["status"] == "ready"
    assert operation["service_status"] == "stopped"
    assert operation["service_error"] is None
    assert operation["desired_enabled"] is False


def test_dashboard_preserves_a_live_reranker_stop_transition(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "reranker-component-install",
        {"status": "ready", "service_status": "stopping", "desired_enabled": False},
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.service_is_inactive",
        lambda _self: False,
    )

    service._recover_interrupted_reranker_install(config, store=store)

    operation = store.state("reranker-component-install")
    assert operation["service_status"] == "stopping"
    assert operation["desired_enabled"] is False


def test_dashboard_does_not_start_again_while_candidate_cleanup_is_deferred(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    AutomationStore(database).update_state(
        "reranker-component-install",
        {
            "status": "error",
            "desired_enabled": False,
            "cleanup_pending": True,
            "cleanup_retry_at_epoch": time.time() + 300,
        },
    )

    with pytest.raises(ConfigurationError, match="still being cleaned up"):
        service.configure_reranker({"enabled": True})


def test_dashboard_can_cancel_desired_reranker_state_while_installing(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "reranker-component-install",
        {
            "status": "installing",
            "desired_enabled": True,
            "requested_at_epoch": time.time(),
        },
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.status",
        lambda _self, **_kwargs: RerankerComponentStatus(installed=False, status="not_installed"),
    )

    state = DashboardService(config_path).configure_reranker({"enabled": False})

    assert state["reranker"]["status"] == "installing"
    assert store.state("reranker-component-install")["desired_enabled"] is False
    assert load_config(config_path).retrieval.reranker.enabled is False


def test_dashboard_can_disable_reranker_while_model_is_starting(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "reranker-component-install",
        {
            "status": "ready",
            "service_status": "starting",
            "desired_enabled": True,
        },
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.status",
        lambda _self, **_kwargs: RerankerComponentStatus(
            installed=True,
            status="degraded",
            component_version=RERANKER_COMPONENT_VERSION,
        ),
    )
    monkeypatch.setattr(
        service,
        "state",
        lambda: {"reranker": {"enabled": False, "status": "stopping"}},
    )

    state = service.configure_reranker({"enabled": False})

    assert state["reranker"]["enabled"] is False
    assert state["reranker"]["status"] == "stopping"
    assert store.state("reranker-component-install")["desired_enabled"] is False
    assert load_config(config_path).retrieval.reranker.enabled is False


def test_dashboard_can_reverse_a_startup_cancellation_without_losing_intent(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "reranker-component-install",
        {
            "status": "ready",
            "service_status": "starting",
            "desired_enabled": False,
        },
    )
    monkeypatch.setattr(service, "state", lambda: {"result": "transitioning"})

    state = service.configure_reranker({"enabled": True})

    assert state == {"result": "transitioning"}
    assert store.state("reranker-component-install")["desired_enabled"] is True
    assert load_config(config_path).retrieval.reranker.enabled is True


def test_dashboard_enables_and_disables_an_installed_reranker(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    calls: list[str] = []
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.status",
        lambda _self, **_kwargs: RerankerComponentStatus(
            installed=True,
            status="ready",
            component_version=RERANKER_COMPONENT_VERSION,
            installed_bytes=16384,
        ),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.ensure_service",
        lambda _self: calls.append("started"),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_component.RerankerComponentManager.client",
        lambda _self: SimpleNamespace(health=lambda **_kwargs: True),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.stop_service",
        lambda _self: calls.append("stopped"),
    )
    service = DashboardService(config_path)

    enabled = service.configure_reranker({"enabled": True})
    disabled = service.configure_reranker({"enabled": False})

    assert enabled["reranker"]["enabled"] is True
    assert enabled["reranker"]["setup_requires_download"] is False
    assert disabled["reranker"]["enabled"] is False
    assert disabled["reranker"]["installed"] is True
    assert disabled["reranker"]["setup_requires_download"] is False
    assert calls == ["started", "stopped"]


def test_dashboard_restarts_a_stopped_reranker_without_reinstalling(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    calls: list[str] = []

    def component_status(_self, *, check_service: bool = False):
        return RerankerComponentStatus(
            installed=True,
            status="degraded" if check_service and not calls else "ready",
            component_version=RERANKER_COMPONENT_VERSION,
            installed_bytes=16384,
            service_ready=bool(calls),
        )

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.status",
        component_status,
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.ensure_service",
        lambda _self: calls.append("started"),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.reranker_component.RerankerComponentManager.client",
        lambda _self: SimpleNamespace(health=lambda **_kwargs: True),
    )
    monkeypatch.setattr(
        RerankerInstallWorkerLauncher,
        "start",
        lambda _self: pytest.fail("an installed component must not launch the installer"),
    )

    state = DashboardService(config_path).configure_reranker({"enabled": True})

    assert state["reranker"]["status"] == "ready"
    assert state["reranker"]["enabled"] is True
    assert calls == ["started"]
    operation = AutomationStore(Database(load_config(config_path).sqlite_path)).state(
        "reranker-component-install"
    )
    assert operation["status"] == "ready"
    assert operation["desired_enabled"] is True
    assert operation["service_status"] == "ready"


def test_dashboard_removes_reranker_only_while_disabled(config_path: Path, monkeypatch) -> None:
    _write_manifest(config_path)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.remove",
        lambda _self: 12288,
    )

    state = DashboardService(config_path).remove_reranker()

    assert state["reranker"]["enabled"] is False
    operation = AutomationStore(Database(load_config(config_path).sqlite_path)).state(
        "reranker-component-install"
    )
    assert operation["status"] == "not_installed"
    assert operation["reclaimed_bytes"] == 12288


def test_dashboard_exposes_remove_for_damaged_managed_reranker_storage(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    # Construct the service before introducing damage so its independent startup
    # recovery thread cannot race this state-rendering assertion.
    service = DashboardService(config_path)
    config = load_config(config_path)
    root = config.reranker_component_path
    root.mkdir(parents=True)
    (root / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component_version": RERANKER_COMPONENT_VERSION,
                "runtime": f"runtime/reranker-1-{'d' * 32}",
                "model_path": f"models/bge-reranker-v2-m3-{'e' * 32}",
                "model_id": "BAAI/bge-reranker-v2-m3",
                # pragma: allowlist nextline secret
                "model_revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
                "host": "127.0.0.1",
                "port": 8768,
                "token": "x" * 48,
            }
        ),
        encoding="utf-8",
    )
    state = service.state()

    assert state["reranker"]["status"] == "error"
    assert state["reranker"]["installed"] is False
    assert state["reranker"]["removable"] is True
    assert state["reranker"]["installed_bytes"] > 0


def test_dashboard_preserves_disable_when_install_completes_during_state_cas(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    store = AutomationStore(database)
    store.update_state(
        "reranker-component-install",
        {"status": "installing", "desired_enabled": True, "requested_at_epoch": time.time()},
    )
    crossed = False
    stop_calls: list[str] = []
    original_patch = AutomationStore.patch_state_if_current

    def complete_before_compare_and_swap(
        candidate_store,
        job_id,
        *,
        expected_status,
        values,
        owner_id=None,
    ):
        nonlocal crossed
        if not crossed and job_id == "reranker-component-install":
            crossed = True
            candidate_store.update_state(
                job_id,
                {
                    "status": "ready",
                    "desired_enabled": True,
                    "service_status": "ready",
                },
            )
            return False
        return original_patch(
            candidate_store,
            job_id,
            expected_status=expected_status,
            values=values,
            owner_id=owner_id,
        )

    monkeypatch.setattr(AutomationStore, "patch_state_if_current", complete_before_compare_and_swap)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.status",
        lambda _self, **_kwargs: RerankerComponentStatus(
            installed=True,
            status="ready",
            component_version=RERANKER_COMPONENT_VERSION,
            installed_bytes=16384,
        ),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.request_service_stop",
        lambda _self: stop_calls.append("requested"),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.RerankerComponentManager.stop_service",
        lambda _self: stop_calls.append("stopped"),
    )
    monkeypatch.setattr(service, "state", lambda: {"result": "disabled"})

    state = service.configure_reranker({"enabled": False})

    assert state == {"result": "disabled"}
    assert crossed is True
    assert load_config(config_path).retrieval.reranker.enabled is False
    final = store.state("reranker-component-install")
    assert final["status"] == "ready"
    assert final["desired_enabled"] is False
    assert final["service_status"] == "stopped"
    assert stop_calls == ["requested", "requested", "stopped"]


def test_dashboard_rejects_reranker_removal_while_enabled_or_busy(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["retrieval"]["reranker"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="disable result reranking"):
        service.remove_reranker()

    payload["retrieval"]["reranker"]["enabled"] = False
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    AutomationStore(database).update_state("reranker-component-install", {"status": "installing"})

    with pytest.raises(ConfigurationError, match="installation or shutdown is running"):
        service.remove_reranker()

    AutomationStore(database).update_state(
        "reranker-component-install",
        {"status": "ready", "service_status": "starting", "desired_enabled": False},
    )

    with pytest.raises(ConfigurationError, match="installation or shutdown is running"):
        service.remove_reranker()


def test_dashboard_saves_validated_configuration_and_projects(config_path: Path) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)

    state = service.configure(_configuration())

    configured = load_config(config_path).official_docs
    assert set(configured.products) == {"cmdb", "itsm"}
    assert configured.products["cmdb"].versions == ["26.2", "26.1"]
    assert configured.interval_hours == 12
    assert configured.retain_unselected_versions is False
    example_project = yaml.safe_load(
        (config_path.parent / "projects/example_project.yaml").read_text()
    )
    assert example_project["bmc"]["products"] == {"cmdb": {"version": "26.2"}}
    assert state["settings"]["interval_hours"] == 12


def _activate_dashboard_test_installation(
    config_path: Path,
    *,
    client: ClientIntegration,
) -> None:
    workspace = config_path.parent.parent
    executable_dir = workspace / "runtime/1.27.0/venv" / ("Scripts" if os.name == "nt" else "bin")
    executable_dir.mkdir(parents=True)
    server = executable_dir / (
        "helix-mcp-knowledge-server.exe" if os.name == "nt" else "helix-mcp-knowledge-server"
    )
    python = executable_dir / ("python.exe" if os.name == "nt" else "python")
    server.write_text("server", encoding="utf-8")
    python.write_text("python", encoding="utf-8")
    activate_managed_installation(
        workspace=workspace,
        version="1.27.0",
        server_command=server,
        config_path=config_path,
        client=client,
        server_name="helix_knowledge" if client == "openclaw" else None,
        openclaw_command="/opt/openclaw" if client == "openclaw" else None,
    )


def test_dashboard_reload_applies_saved_configuration_to_openclaw(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    _activate_dashboard_test_installation(config_path, client="openclaw")
    calls: list[str] = []
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.reload_openclaw",
        lambda command: calls.append(str(command)),
    )

    state = DashboardService(config_path).configure(_configuration())

    assert calls == ["/opt/openclaw"]
    assert state["application"] == {
        "client": "openclaw",
        "status": "applied",
        "error_code": None,
    }
    assert state["restart_required"] is False
    assert load_config(config_path).official_docs.products["cmdb"].versions == [
        "26.2",
        "26.1",
    ]


def test_dashboard_keeps_saved_configuration_when_openclaw_reload_fails(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    _activate_dashboard_test_installation(config_path, client="openclaw")

    def fail_reload(_command) -> None:
        raise RuntimeError("reload unavailable")

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.reload_openclaw",
        fail_reload,
    )

    state = DashboardService(config_path).configure(_configuration())

    assert state["application"] == {
        "client": "openclaw",
        "status": "reload_failed",
        "error_code": "OPENCLAW_INTEGRATION_ERROR",
    }
    assert state["restart_required"] is True
    assert load_config(config_path).official_docs.products["cmdb"].versions == [
        "26.2",
        "26.1",
    ]


def test_dashboard_requests_reconnection_for_non_openclaw_client(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    _activate_dashboard_test_installation(config_path, client="codex")
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.reload_openclaw",
        lambda _command: pytest.fail("a non-OpenClaw client must not be reloaded"),
    )

    state = DashboardService(config_path).configure(_configuration())

    assert state["application"] == {
        "client": "codex",
        "status": "restart_required",
        "error_code": None,
    }
    assert state["restart_required"] is True


def test_dashboard_does_not_reload_openclaw_without_an_effective_change(
    config_path: Path,
    monkeypatch,
) -> None:
    _write_manifest(config_path)
    _activate_dashboard_test_installation(config_path, client="openclaw")
    calls: list[str] = []
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.reload_openclaw",
        lambda command: calls.append(str(command)),
    )
    service = DashboardService(config_path)

    service.configure(_configuration())
    state = service.configure(_configuration())

    assert calls == ["/opt/openclaw"]
    assert state["application"] == {
        "client": "openclaw",
        "status": "unchanged",
        "error_code": None,
    }
    assert state["restart_required"] is False


def test_dashboard_can_select_and_remove_any_supported_project_product(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    payload = _configuration()
    payload["projects"] = {
        "example_project": {
            "products": {
                "digital_workplace": "26.3",
                "discovery": "current",
            }
        }
    }

    state = service.configure(payload)

    example_project_path = config_path.parent / "projects/example_project.yaml"
    example_project = yaml.safe_load(example_project_path.read_text())
    assert example_project["bmc"]["products"] == {
        "digital_workplace": {"version": "26.3"},
        "discovery": {"version": "current"},
    }
    project = next(item for item in state["projects"] if item["project_id"] == "example_project")
    assert project["products"] == {
        "digital_workplace": "26.3",
        "discovery": "current",
    }

    payload["projects"]["example_project"]["products"] = {"discovery": "current"}
    service.configure(payload)

    example_project = yaml.safe_load(example_project_path.read_text())
    assert example_project["bmc"]["products"] == {"discovery": {"version": "current"}}


def test_dashboard_creates_project_with_managed_folder_and_manifest(config_path: Path) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)

    state = service.create_project(
        {
            "project_id": "customer-portal",
            "name": "Customer Portal",
            "language": "en",
        }
    )

    workspace = config_path.parent.parent
    documents = workspace / "data/sources/projects/customer-portal/docs"
    project_path = config_path.parent / "projects/customer-portal.yaml"
    manifest_path = config_path.parent / "projects/customer-portal.sources.yaml"
    assert documents.is_dir()
    project = yaml.safe_load(project_path.read_text())
    assert project["documents"] == {
        "path": "data/sources/projects/customer-portal/docs",
        "sources_manifest": "config/projects/customer-portal.sources.yaml",
    }
    manifest = yaml.safe_load(manifest_path.read_text())
    assert manifest["project_id"] == "customer-portal"
    assert manifest["sources"][0]["path"] == "**/*"
    assert manifest["sources"][0]["metadata"]["dashboard_managed"] is True
    created = next(item for item in state["projects"] if item["project_id"] == "customer-portal")
    assert created["documents_path"] == str(documents)
    assert created["managed_documents_path"] is True


def test_dashboard_allows_one_explicit_external_project_folder(config_path: Path) -> None:
    _write_manifest(config_path)
    external = config_path.parent.parent / "customer-documents"

    state = DashboardService(config_path).create_project(
        {
            "project_id": "external-docs",
            "name": "External documents",
            "documents_path": str(external),
        }
    )

    config = load_config(config_path)
    assert config.projects.external_document_roots == [external]
    project = next(item for item in state["projects"] if item["project_id"] == "external-docs")
    assert project["documents_path"] == str(external)
    assert project["managed_documents_path"] is False


def test_dashboard_rejects_home_as_project_documentation_folder(config_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="dedicated project documentation folder"):
        DashboardService(config_path).create_project(
            {
                "project_id": "too-broad",
                "name": "Too Broad",
                "documents_path": str(Path.home()),
            }
        )


def test_dashboard_browses_local_project_folders(config_path: Path) -> None:
    _write_manifest(config_path)
    root = config_path.parent.parent / "data/sources/projects"
    (root / "Zulu").mkdir(parents=True)
    (root / "alpha").mkdir()
    (root / "Document.PDF").write_bytes(b"project-pdf")
    (root / "document.txt").write_text("project text", encoding="utf-8")
    (root / "installer.exe").write_bytes(b"not indexable")
    service = DashboardService(config_path)

    listing = service.browse_directories({})

    assert listing["path"] == str(root)
    assert [entry["name"] for entry in listing["entries"]] == ["alpha", "Zulu"]
    assert listing["files"] == [
        {
            "name": "Document.PDF",
            "extension": ".pdf",
            "size_bytes": 11,
            "supported": True,
            "support_reason": "indexable",
        },
        {
            "name": "document.txt",
            "extension": ".txt",
            "size_bytes": 12,
            "supported": True,
            "support_reason": "indexable",
        },
        {
            "name": "installer.exe",
            "extension": ".exe",
            "size_bytes": 13,
            "supported": False,
            "support_reason": "unsupported format",
        },
    ]
    assert ".pdf" in listing["supported_extensions"]
    assert all("path" not in item for item in listing["files"])
    assert listing["selectable"] is False
    assert listing["selection_error"] == "open a dedicated subfolder before selecting it"
    assert listing["truncated"] is False
    assert any(
        item == {"label": "Managed projects", "path": str(root)} for item in listing["roots"]
    )

    nested = service.browse_directories({"path": str(root / "alpha/missing/docs")})
    assert nested["path"] == str(root / "alpha")
    assert nested["parent"] == str(root)
    assert nested["selectable"] is True

    oversized = root / "alpha/oversized.pdf"
    with oversized.open("wb") as stream:
        stream.truncate(11 * 1024 * 1024)
    nested = service.browse_directories({"path": str(root / "alpha")})
    assert nested["files"] == [
        {
            "name": "oversized.pdf",
            "extension": ".pdf",
            "size_bytes": 11 * 1024 * 1024,
            "supported": False,
            "support_reason": "exceeds 10 MB limit",
        }
    ]


def test_dashboard_queues_manual_project_indexing_and_exposes_status(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    created = service.create_project({"project_id": "portal", "name": "Portal"})
    project = next(item for item in created["projects"] if item["project_id"] == "portal")
    assert project["sync"]["status"] == "pending"
    store = AutomationStore(Database(load_config(config_path).sqlite_path))
    store.update_state("project:portal", {"status": "ok", "project_id": "portal"})
    Path(project["documents_path"]).joinpath("guide.md").write_text(
        "# Portal\n\nPrivate project documentation.", encoding="utf-8"
    )

    first = service.request_project_sync({"project_id": "portal"})
    second = service.request_project_sync({"project_id": "portal"})
    state = service.state()
    project = next(item for item in state["projects"] if item["project_id"] == "portal")

    assert first == {"status": "started", "project_id": "portal"}
    assert second == {"status": "already_pending", "project_id": "portal"}
    assert project["sync"]["status"] == "pending"
    assert project["sync"]["indexed_documents"] == 0
    assert project["sync"]["indexed_chunks"] == 0


def test_dashboard_reports_indexed_project_documents_and_chunks(config_path: Path) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    created = service.create_project({"project_id": "portal", "name": "Portal"})
    project = next(item for item in created["projects"] if item["project_id"] == "portal")
    Path(project["documents_path"]).joinpath("guide.md").write_text(
        "# Portal\n\nSearchable project documentation.", encoding="utf-8"
    )
    results = KnowledgeApplication.from_config(config_path).sync_project_sources("portal")
    assert results[0].status == "indexed"
    AutomationStore(Database(load_config(config_path).sqlite_path)).update_state(
        "project:portal",
        {"status": "ok", "project_id": "portal", "detected_files": 1},
    )

    state = service.state()
    project = next(item for item in state["projects"] if item["project_id"] == "portal")

    assert project["sync"]["status"] == "ready"
    assert project["sync"]["indexed_documents"] == 1
    assert project["sync"]["indexed_chunks"] >= 1


def test_dashboard_rejects_manual_indexing_for_unknown_or_archived_project(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)

    with pytest.raises(ProjectNotFoundError, match="unknown project"):
        service.request_project_sync({"project_id": "missing-project"})

    service.create_project({"project_id": "archive-me", "name": "Archive Me"})
    project_path = config_path.parent / "projects/archive-me.yaml"
    payload = yaml.safe_load(project_path.read_text(encoding="utf-8"))
    payload["status"] = "archived"
    project_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not active"):
        service.request_project_sync({"project_id": "archive-me"})


def test_dashboard_folder_browser_rejects_non_directory_location(config_path: Path) -> None:
    _write_manifest(config_path)
    not_a_folder = config_path.parent.parent / "not-a-folder.txt"
    not_a_folder.write_text("not a directory", encoding="utf-8")

    try:
        DashboardService(config_path).browse_directories({"path": str(not_a_folder)})
    except Exception as exc:
        assert "folder is not accessible" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("folder browser accepted a file")


def test_dashboard_updates_project_folder_and_managed_manifest_products(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    service.create_project({"project_id": "portal", "name": "Portal"})
    payload = _configuration()
    external = config_path.parent.parent / "portal-external"
    payload["projects"]["portal"] = {
        "documents_path": str(external),
        "products": {"cmdb": "26.2", "discovery": "current"},
    }

    state = service.configure(payload)

    portal = yaml.safe_load((config_path.parent / "projects/portal.yaml").read_text())
    manifest = yaml.safe_load((config_path.parent / "projects/portal.sources.yaml").read_text())
    assert portal["documents"]["path"] == "portal-external"
    assert manifest["sources"][0]["products"] == {"cmdb": None, "discovery": None}
    assert load_config(config_path).projects.external_document_roots == [external]
    updated = next(item for item in state["projects"] if item["project_id"] == "portal")
    assert updated["documents_path"] == str(external)
    assert updated["sync"]["status"] == "pending"
    assert (
        AutomationStore(Database(load_config(config_path).sqlite_path)).state("project:portal")[
            "reason"
        ]
        == "configuration_changed"
    )

    store = AutomationStore(Database(load_config(config_path).sqlite_path))
    store.update_state("project:portal", {"status": "ok", "project_id": "portal"})
    unchanged = service.configure(payload)
    portal_state = next(item for item in unchanged["projects"] if item["project_id"] == "portal")
    assert portal_state["sync"]["status"] == "ready"
    assert store.state("project:portal") == {"status": "ok", "project_id": "portal"}


def test_dashboard_removes_project_index_but_preserves_source_documents(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    state = service.create_project({"project_id": "portal", "name": "Portal"})
    portal = next(item for item in state["projects"] if item["project_id"] == "portal")
    documents = Path(portal["documents_path"])
    source = documents / "keep-me.md"
    source.write_text("Private project documentation", encoding="utf-8")
    indexed = KnowledgeApplication.from_config(config_path).sync_project_sources("portal")
    assert indexed[0].status == "indexed"
    database = Database(load_config(config_path).sqlite_path)
    with database.connect() as connection:
        chunk_id = connection.execute(
            "SELECT chunk_id FROM chunks WHERE project_id = 'portal'"
        ).fetchone()["chunk_id"]

    state = service.remove_project({"project_id": "portal"})

    assert source.read_text(encoding="utf-8") == "Private project documentation"
    assert not (config_path.parent / "projects/portal.yaml").exists()
    assert list((config_path.parent / "projects/.removed").glob("portal-*/portal.yaml"))
    assert all(item["project_id"] != "portal" for item in state["projects"])
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT project_id FROM projects WHERE project_id = 'portal'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT document_id FROM documents WHERE project_id = 'portal'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute("SELECT chunk_id FROM chunks WHERE project_id = 'portal'").fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT chunk_id FROM chunks_fts WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
            is None
        )


def test_dashboard_rejects_duplicate_project_without_modifying_it(config_path: Path) -> None:
    _write_manifest(config_path)
    example_project_path = config_path.parent / "projects/example_project.yaml"
    original = example_project_path.read_bytes()

    try:
        DashboardService(config_path).create_project(
            {"project_id": "example_project", "name": "Duplicate"}
        )
    except Exception as exc:
        assert "already exists" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("duplicate project was accepted")

    assert example_project_path.read_bytes() == original


def test_dashboard_can_disable_an_official_product(config_path: Path) -> None:
    _write_manifest(config_path)
    payload = _configuration()
    payload["products"] = {"cmdb": ["26.1", "26.2"]}

    DashboardService(config_path).configure(payload)

    configured = load_config(config_path).official_docs
    assert set(configured.products) == {"cmdb"}
    assert configured.products["cmdb"].versions == ["26.2", "26.1"]


def test_dashboard_cannot_change_selection_during_managed_maintenance(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)

    with (
        UpdateLock(config_path.parent.parent / ".update.lock"),
        pytest.raises(UpdateLockBusyError),
    ):
        service.configure(_configuration())


def test_dashboard_can_save_an_empty_product_selection(config_path: Path) -> None:
    _write_manifest(config_path)
    payload = _configuration()
    payload["products"] = {}

    state = DashboardService(config_path).configure(payload)

    assert load_config(config_path).official_docs.products == {}
    assert state["sync"]["official"]["status"] == "not_configured"


def test_dashboard_previews_physical_cleanup_without_project_documents(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    application = KnowledgeApplication.from_config(config_path)
    source = application.ingestion_manager.official_sources_root / "cmdb/26.1/test.html"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("official downloaded content", encoding="utf-8")
    with application.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO documents(
                document_id, source_key, source_scope, project_id, title, document_type,
                source_type, source_path, source_url, language, classification,
                content_hash, file_size, indexed_at, status, metadata_json
            ) VALUES (
                'doc_cleanup', 'cleanup', 'bmc_official', NULL, 'Cleanup', 'reference',
                'bmc_public_url', ?, 'https://docs.bmc.com/cmdb/26.1/', 'en', 'public',
                'hash', ?, datetime('now'), 'indexed', '{}'
            )
            """,
            (str(source), source.stat().st_size),
        )
        connection.execute("INSERT INTO document_products VALUES ('doc_cleanup', 'cmdb')")
        connection.execute(
            "INSERT OR IGNORE INTO product_versions VALUES ('cmdb:26.1', 'cmdb', '26.1')"
        )
        connection.execute(
            "INSERT INTO document_product_versions VALUES ('doc_cleanup', 'cmdb:26.1')"
        )
        connection.execute(
            """
            INSERT INTO chunks(
                chunk_id, document_id, source_scope, project_id, document_type,
                heading_path_json, chunk_type, text, embedding_text, position,
                token_count, content_hash, active
            ) VALUES (
                'chk_cleanup', 'doc_cleanup', 'bmc_official', NULL, 'reference',
                '[]', 'section', 'text', 'text', 0, 1, 'chunk-hash', 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sources(source_id, source_type, source_path, source_url, metadata_json)
            VALUES (
                'source_cleanup', 'bmc_public_url', ?,
                'https://docs.bmc.com/cmdb/26.1/',
                '{"product":"cmdb","version":"26.1"}'
            )
            """,
            (str(source),),
        )
        connection.commit()

    preview = DashboardService(config_path).cleanup_preview({"products": {"itsm": ["26.1"]}})

    assert preview["required"] is True
    assert preview["documents"] == 1
    assert preview["chunks"] == 1
    assert preview["source_files"] == 1
    assert preview["estimated_bytes"] >= source.stat().st_size
    assert preview["product_versions"] == [
        {
            "product": "cmdb",
            "version": "26.1",
            "documents": 1,
            "chunks": 1,
            "estimated_bytes": preview["product_versions"][0]["estimated_bytes"],
        }
    ]
    assert preview["project_documents_affected"] is False
    assert source.is_file()


def test_dashboard_rejects_unavailable_version_without_modifying_files(config_path: Path) -> None:
    _write_manifest(config_path)
    original_config = config_path.read_bytes()
    project_path = config_path.parent / "projects/example_project.yaml"
    original_project = project_path.read_bytes()
    payload = _configuration()
    payload["products"] = {"cmdb": ["99.9"]}

    try:
        DashboardService(config_path).configure(payload)
    except Exception as exc:
        assert "unavailable" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("unavailable dashboard version was accepted")

    assert config_path.read_bytes() == original_config
    assert project_path.read_bytes() == original_project


def test_dashboard_requests_detached_forced_sync(config_path: Path, monkeypatch) -> None:
    _write_manifest(config_path)
    captured: dict[str, object] = {}

    def fake_start(self, *, force=False):
        captured["force"] = force
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(OfficialSyncWorkerLauncher, "start", fake_start)

    result = DashboardService(config_path).request_sync()

    assert result == {"status": "started", "process_id": 4321}
    assert captured == {"force": True}
    store = AutomationStore(Database(load_config(config_path).sqlite_path))
    assert store.state("official-docs")["status"] == "pending"


def test_dashboard_replaces_an_abandoned_running_sync_request(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    application = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(application.database)
    store.update_state("official-docs", {"status": "running", "owner_id": "dead-worker"})
    monkeypatch.setattr(
        OfficialSyncWorkerLauncher,
        "start",
        lambda self, *, force=False: SimpleNamespace(pid=5432),
    )

    result = DashboardService(config_path).request_sync()

    assert result == {"status": "started", "process_id": 5432}
    assert store.state("official-docs")["status"] == "pending"


def test_dashboard_marks_abandoned_project_sync_interrupted_before_saving(
    config_path: Path,
) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    application = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(application.database)
    store.update_state(
        "project:example_project",
        {"status": "running", "owner_id": "dead-worker"},
    )

    service.configure(_configuration())

    state = store.state("project:example_project")
    assert state["status"] == "interrupted"
    assert state["error"] == "previous worker lease expired"


def test_dashboard_requests_cleanup_sync_with_no_products(config_path: Path, monkeypatch) -> None:
    _write_manifest(config_path)
    payload = _configuration()
    payload["products"] = {}
    payload["retain_unselected_versions"] = False
    service = DashboardService(config_path)
    service.configure(payload)
    monkeypatch.setattr(
        OfficialSyncWorkerLauncher,
        "start",
        lambda self, *, force=False: SimpleNamespace(pid=9876),
    )

    result = service.request_sync()

    assert result == {"status": "started", "process_id": 9876}


def test_dashboard_cancels_pending_and_running_syncs(config_path: Path) -> None:
    _write_manifest(config_path)
    service = DashboardService(config_path)
    service.state()
    database = Database(load_config(config_path).sqlite_path)
    store = AutomationStore(database)
    store.update_state("official-docs", {"status": "pending", "next_run_epoch": 0})

    assert service.request_sync_cancellation()["status"] == "cancelled"
    assert store.state("official-docs")["status"] == "cancelled"

    store.update_state(
        "official-docs",
        {"status": "running", "owner_id": "worker", "progress": {"processed_items": 8}},
    )
    assert store.acquire_or_renew("official-docs-test", "worker", 30)
    assert service.request_sync_cancellation()["status"] == "requested"
    state = store.state("official-docs")
    assert state["status"] == "running"
    assert state["cancel_requested"] is True
    assert state["progress"] == {"processed_items": 8}
    assert service.request_sync_cancellation()["status"] == "already_requested"


def test_dashboard_checks_for_updates_on_demand(config_path: Path, monkeypatch) -> None:
    expected = UpdateStatus(
        status="available",
        repository="hvolckaert/helix-mcp-knowledge",
        current_version="1.8.0",
        latest_version="1.9.0",
        update_available=True,
    )
    monkeypatch.setattr(ReleaseUpdateChecker, "check", lambda self, force=False: expected)

    result = DashboardService(config_path).check_update()

    assert result["status"] == "available"
    assert result["current_version"] == "1.8.0"
    assert result["latest_version"] == "1.9.0"


def test_dashboard_reports_standalone_openclaw_status(config_path: Path) -> None:
    _write_manifest(config_path)
    workspace = config_path.parent.parent
    server = workspace / "runtime/1.9.0/venv/bin/helix-mcp-knowledge-server"
    server.parent.mkdir(parents=True)
    server.write_text("server", encoding="utf-8")
    (server.parent / ("python.exe" if os.name == "nt" else "python")).write_text(
        "python", encoding="utf-8"
    )
    activate_managed_installation(
        workspace=workspace,
        version="1.9.0",
        server_command=server,
        config_path=config_path,
        client="standalone",
    )

    integrations = DashboardService(config_path).state()["client_integrations"]

    assert integrations["managed"] is True
    assert integrations["active_client"] == "standalone"
    assert integrations["openclaw"] == {
        "name": "OpenClaw",
        "connected": False,
        "status": "not_connected",
        "description": (
            "Not detected during installation. Helix Knowledge remains available as a "
            "standalone MCP server."
        ),
    }
    assert "clients" not in integrations
    assert "launcher" not in integrations


def test_dashboard_reports_connected_openclaw_status(config_path: Path) -> None:
    _write_manifest(config_path)
    workspace = config_path.parent.parent
    server = workspace / "runtime/1.9.0/venv/bin/helix-mcp-knowledge-server"
    server.parent.mkdir(parents=True)
    server.write_text("server", encoding="utf-8")
    (server.parent / ("python.exe" if os.name == "nt" else "python")).write_text(
        "python", encoding="utf-8"
    )
    activate_managed_installation(
        workspace=workspace,
        version="1.9.0",
        server_command=server,
        config_path=config_path,
        client="openclaw",
        server_name="helix_knowledge",
        openclaw_command="/usr/bin/openclaw",
    )

    integrations = DashboardService(config_path).state()["client_integrations"]

    assert integrations["active_client"] == "openclaw"
    assert integrations["openclaw"] == {
        "name": "OpenClaw",
        "connected": True,
        "status": "connected",
        "description": "Registered and ready for new OpenClaw sessions.",
    }


def test_dashboard_starts_transactional_update_worker(config_path: Path, monkeypatch) -> None:
    expected = UpdateStatus(
        status="available",
        repository="hvolckaert/helix-mcp-knowledge",
        current_version="1.8.0",
        latest_version="1.9.0",
        update_available=True,
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(ReleaseUpdateChecker, "check", lambda self, force=False: expected)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.load_managed_installation",
        lambda _workspace: SimpleNamespace(client="openclaw"),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.supports_transactional_updates",
        lambda *_args: True,
    )

    def fake_start(self):
        captured.update(self.__dict__)
        return SimpleNamespace(pid=9876)

    monkeypatch.setattr(DashboardUpdateWorkerLauncher, "start", fake_start)

    result = DashboardService(
        config_path,
        server_name="custom_knowledge",
        openclaw_command="/usr/bin/openclaw",
    ).request_update(dashboard_port=8877)

    assert result == {
        "status": "started",
        "process_id": 9876,
        "target_version": "1.9.0",
    }
    assert captured["target_version"] == "1.9.0"
    assert captured["server_name"] == "custom_knowledge"
    assert captured["openclaw_command"] == "/usr/bin/openclaw"
    assert captured["dashboard_port"] == 8877
    store = AutomationStore(Database(load_config(config_path).sqlite_path))
    assert store.state("dashboard-update")["status"] == "pending"
    assert store.state("dashboard-update")["process_id"] == 9876


def test_dashboard_queues_update_while_documentation_sync_is_running(
    config_path: Path, monkeypatch
) -> None:
    _write_manifest(config_path)
    expected = UpdateStatus(
        status="available",
        repository="hvolckaert/helix-mcp-knowledge",
        current_version="1.9.3",
        latest_version="1.10.0",
        update_available=True,
    )
    workspace = config_path.parent.parent
    server = workspace / "runtime/1.9.3/venv/bin/helix-mcp-knowledge-server"
    server.parent.mkdir(parents=True)
    server.write_text("server", encoding="utf-8")
    (server.parent / ("python.exe" if os.name == "nt" else "python")).write_text(
        "python", encoding="utf-8"
    )
    activate_managed_installation(
        workspace=workspace,
        version="1.9.3",
        server_command=server,
        config_path=config_path,
        client="standalone",
    )
    application = DashboardService(config_path)
    application.state()
    store = AutomationStore(Database(load_config(config_path).sqlite_path))
    store.update_state("official-docs", {"status": "running", "owner_id": "sync-worker"})
    monkeypatch.setattr(ReleaseUpdateChecker, "check", lambda self, force=False: expected)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard.supports_transactional_updates",
        lambda *_args: True,
    )
    captured: dict[str, object] = {}

    def fake_start(self):
        captured.update(self.__dict__)
        return SimpleNamespace(pid=2468)

    monkeypatch.setattr(DashboardUpdateWorkerLauncher, "start", fake_start)

    result = application.request_update(
        dashboard_port=8765,
        dashboard_token="private-token",
    )

    assert result["status"] == "queued"
    assert result["target_version"] == "1.10.0"
    assert captured["dashboard_token"] == "private-token"


def test_dashboard_http_surface_and_csrf_protection(config_path: Path) -> None:
    _write_manifest(config_path)
    server = DashboardHTTPServer(
        ("127.0.0.1", 0), DashboardService(config_path), token="test-token"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode()
        assert response.status == 200
        assert "IntelliAgentia" in html
        assert "<title>IntelliAgentia · Helix MCP Knowledge</title>" in html
        assert "Helix MCP Knowledge · Documentation administration" in html
        assert '<html lang="en">' in html
        assert '<div class="savebar" id="savebar" role="status" aria-live="polite">' in html
        assert 'id="save-note"' in html
        assert "Changes are validated before saving" in html
        assert "Save and synchronize" in html
        assert (
            '<input id="interval-hours" type="number" min="0.01" max="8760" step="0.01" required>'
        ) in html
        assert 'id="review-dialog"' in html
        assert "Review changes before saving" in html
        assert 'class="tab-dirty"' in html
        assert "unsaved changes" in html
        assert "Selecting a product preselects its latest version" in html
        assert "Start with the documentation you need" in html
        assert "took about 44 minutes in reference testing" in html
        assert "Confirm the first documentation selection" in html
        assert "Check for updates" in html
        assert "current:'Updated'" in html
        assert "available:'Update available'" in html
        assert "`Updated to ${updateOperation.target_version}`" in html
        assert "formatBytes(retention.reclaimed_bytes)} reclaimed" not in html
        assert "Refresh catalog" in html
        assert "/api/catalog/refresh" in html
        assert "Scanned PDF OCR" in html
        assert 'role="tablist"' in html
        assert 'data-dashboard-tab="documentation"' in html
        assert 'data-dashboard-tab="settings"' in html
        assert "Project documentation" in html
        assert "Add project" in html
        assert "Documentation folder" in html
        assert "Choose documentation folder" in html
        assert "Browse…" in html
        assert "data-project-product-enabled" in html
        assert "data-project-path" in html
        assert "data-browse-project-path" in html
        assert "Index documents now" in html
        assert "Files are indexed in place and are never uploaded" in html
        assert "/api/projects/create" in html
        assert "/api/projects/remove" in html
        assert "/api/projects/sync" in html
        assert "/api/directories/browse" in html
        assert "Official downloads remain controlled above" in html
        assert "Documentation maintenance" in html
        assert "Optional components" in html
        assert "Downloaded on demand" in html
        assert "/api/ocr/configuration" in html
        assert "Result reranking" in html
        assert "Result reranking · Experimental" not in html
        assert "Optional and disabled by default" in html
        assert (
            "Plan for approximately ${formatBytes(semantic.estimated_active_memory_bytes)} of RAM"
            in html
        )
        assert "its model memory has been released" in html
        assert "reranker.installed && reranker.status === 'ready'" in html
        assert "Enable result reranking" in html
        assert "/api/reranker/configuration" in html
        assert "/api/reranker/remove" in html
        assert "!reranker.removable" in html
        assert "dashboardState?.reranker?.setup_requires_download" in html
        reranker_cancel = html[
            html.index("$('reranker-cancel').addEventListener") : html.index(
                "$('install-update-button').addEventListener"
            )
        ]
        assert "if (requireSavedConfiguration()) return;" in reranker_cancel
        assert "Install Helix Knowledge" in html
        assert "This page will reconnect" in html
        assert "Runtime connections" in html
        assert "Dashboard service" in html
        assert "Managed and running" in html
        assert "Running manually" in html
        assert "Automatic startup unavailable" in html
        assert "OpenClaw is connected automatically when detected during installation" in html
        assert "OpenClaw will reload its MCP runtime after this save" in html
        assert "Configuration saved and applied to OpenClaw" in html
        assert "Configuration saved, but OpenClaw could not be reloaded" in html
        assert "Reconnect your MCP client to apply the new settings" in html
        assert "renderRuntimeConnections" in html
        assert "Claude Code" not in html
        assert "Other MCP client" not in html
        assert "data-copy-client" not in html
        assert "Select documentation or disable retention to clean local data" in html
        assert "Keep unselected documentation" in html
        assert "/api/cleanup/preview" in html
        assert "Project documentation is never affected" in html
        assert "Cancel safely" in html
        assert "items processed" in html
        assert "last activity" in html
        assert "statePollDelay" in html
        assert "document.hidden" in html
        assert "setInterval(() => loadState(true), 5000)" not in html
        assert "/api/sync/cancel" in html
        assert "The update will wait for documentation synchronization to finish" in html
        assert "Guardar y sincronizar" not in html
        assert "test-token" in html

        connection.request("GET", "/api/health")
        response = connection.getresponse()
        health = json.loads(response.read())
        assert response.status == 200
        assert health["status"] == "ok"
        assert health["server_version"]

        connection.request("GET", "/api/health", headers={"Host": "attacker.example"})
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["error"] == "invalid request host"

        connection.request("GET", "/api/state")
        response = connection.getresponse()
        response.read()
        assert response.status == 403

        connection.request(
            "GET",
            "/api/state",
            headers={
                "X-Helix-Dashboard-Token": "test-token",
                "Origin": "http://attacker.example",
            },
        )
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["error"] == "invalid request origin"

        connection.request(
            "GET",
            "/api/state",
            headers={"X-Helix-Dashboard-Token": "test-token"},
        )
        response = connection.getresponse()
        state = json.loads(response.read())
        assert response.status == 200
        assert state["server_version"]
        assert state["client_integrations"]["openclaw"]["name"] == "OpenClaw"
        assert state["client_integrations"]["openclaw"]["connected"] is False
        assert state["dashboard_runtime"]["port"] == 8765
        assert "clients" not in state["client_integrations"]

        connection.request(
            "POST",
            "/api/cleanup/preview",
            body=json.dumps({"products": {"cmdb": ["26.1"]}}),
            headers={
                "Content-Type": "application/json",
                "X-Helix-Dashboard-Token": "test-token",
            },
        )
        response = connection.getresponse()
        cleanup = json.loads(response.read())
        assert response.status == 200
        assert cleanup["required"] is False
        assert cleanup["project_documents_affected"] is False

        connection.request(
            "POST",
            "/api/projects/create",
            body=json.dumps({"project_id": "web-project", "name": "Web Project"}),
            headers={
                "Content-Type": "application/json",
                "X-Helix-Dashboard-Token": "test-token",
            },
        )
        response = connection.getresponse()
        created = json.loads(response.read())
        assert response.status == 201
        assert any(item["project_id"] == "web-project" for item in created["projects"])

        connection.request(
            "POST",
            "/api/directories/browse",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "X-Helix-Dashboard-Token": "test-token",
            },
        )
        response = connection.getresponse()
        folders = json.loads(response.read())
        assert response.status == 200
        assert Path(folders["path"]) == config_path.parent.parent / "data/sources/projects"
        assert folders["selectable"] is False
        assert "files" in folders

        connection.request(
            "POST",
            "/api/projects/sync",
            body=json.dumps({"project_id": "web-project"}),
            headers={
                "Content-Type": "application/json",
                "X-Helix-Dashboard-Token": "test-token",
            },
        )
        response = connection.getresponse()
        project_sync = json.loads(response.read())
        assert response.status == 202
        assert project_sync == {"status": "already_pending", "project_id": "web-project"}

        connection.request(
            "POST",
            "/api/configuration",
            body=json.dumps(_configuration()),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["error"] == "invalid dashboard token"

        store = AutomationStore(Database(load_config(config_path).sqlite_path))
        store.update_state("official-docs", {"status": "running", "owner_id": "worker"})
        assert store.acquire_or_renew("official-docs-test", "worker", 30)
        connection.request(
            "POST",
            "/api/sync/cancel",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "X-Helix-Dashboard-Token": "test-token",
            },
        )
        response = connection.getresponse()
        assert response.status == 202
        assert json.loads(response.read())["status"] == "requested"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class FakeDashboardProcess:
    pid = 8765

    def __init__(self) -> None:
        self.waited = threading.Event()

    def wait(self) -> int:
        self.waited.set()
        return 0


def test_dashboard_launcher_detaches_and_opens_first_run_ui(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeDashboardProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    config_path = tmp_path / "config/config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("placeholder", encoding="utf-8")
    launcher = DashboardProcessLauncher(
        config_path=config_path,
        workspace=tmp_path,
        errors_path=tmp_path / "data/errors",
        python_executable="/runtime/python",
        port=8877,
    )

    process = launcher.start()

    assert process.to_dict() == {"pid": 8765, "url": "http://127.0.0.1:8877/"}
    assert launcher._process is not None
    assert launcher._process.waited.wait(timeout=1.0)
    assert captured["command"] == [
        "/runtime/python",
        "-m",
        "helix_mcp_knowledge.dashboard_worker",
        "--config",
        str(config_path),
        "--port",
        "8877",
        "--server-name",
        "helix_knowledge",
        "--openclaw-command",
        "openclaw",
    ]
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    if os.name == "nt":
        assert kwargs["creationflags"]
    else:
        assert kwargs["start_new_session"] is True

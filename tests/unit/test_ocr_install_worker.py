from pathlib import Path

from helix_mcp_knowledge.config import load_config
from helix_mcp_knowledge.ocr_component import OcrComponentStatus
from helix_mcp_knowledge.ocr_install_worker import OCR_INSTALL_JOB, run_install
from helix_mcp_knowledge.storage.automation import AutomationStore
from helix_mcp_knowledge.storage.database import Database
from helix_mcp_knowledge.update_lock import UpdateLock


def test_install_worker_enables_ocr_only_after_component_validation(
    config_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "helix_mcp_knowledge.ocr_install_worker.OcrComponentManager.install",
        lambda _self: OcrComponentStatus(
            installed=True,
            status="ready",
            component_version=1,
            installed_bytes=2048,
        ),
    )

    assert run_install(config_path) is True

    config = load_config(config_path)
    assert config.ingestion.ocr.enabled is True
    state = AutomationStore(Database(config.sqlite_path)).state(OCR_INSTALL_JOB)
    assert state["status"] == "ready"
    assert state["installed_bytes"] == 2048


def test_install_worker_respects_disable_request_while_installing(
    config_path: Path, monkeypatch
) -> None:
    config = load_config(config_path)
    database = Database(config.sqlite_path)
    database.initialize()
    AutomationStore(database).update_state(
        OCR_INSTALL_JOB,
        {"status": "installing", "desired_enabled": False},
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.ocr_install_worker.OcrComponentManager.install",
        lambda _self: OcrComponentStatus(
            installed=True,
            status="ready",
            component_version=1,
            installed_bytes=2048,
        ),
    )

    assert run_install(config_path) is True

    assert load_config(config_path).ingestion.ocr.enabled is False


def test_install_worker_does_not_modify_config_while_maintenance_is_locked(
    config_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "helix_mcp_knowledge.ocr_install_worker.OcrComponentManager.install",
        lambda _self: (_ for _ in ()).throw(AssertionError("installation must not start")),
    )
    config = load_config(config_path)

    with UpdateLock(config.base_dir / ".update.lock"):
        assert run_install(config_path, lock_timeout_seconds=0) is False

    assert load_config(config_path).ingestion.ocr.enabled is False
    state = AutomationStore(Database(config.sqlite_path)).state(OCR_INSTALL_JOB)
    assert state["status"] == "error"
    assert "synchronization, update, or cleanup is running" in state["error"]

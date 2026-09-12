import time
from threading import Event

import pytest

from helix_mcp_knowledge.config import OfficialDocsSettings, WatchSettings
from helix_mcp_knowledge.official_automation import (
    EmbeddedOfficialSyncCoordinator,
    OfficialSyncLease,
)
from helix_mcp_knowledge.storage.automation import AutomationStore


def wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached before timeout")


def settings(*, bootstrap_on_empty: bool = True) -> OfficialDocsSettings:
    return OfficialDocsSettings(
        automatic_sync=True,
        bootstrap_on_empty=bootstrap_on_empty,
        interval_hours=24,
        products={"cmdb": {"versions": ["26.1"]}},
    )


def coordination() -> WatchSettings:
    return WatchSettings(
        enabled=False,
        debounce_seconds=0.1,
        poll_seconds=0.1,
        lease_seconds=10,
        retry_seconds=5,
    )


def test_official_coordinator_bootstraps_once_and_persists_schedule(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    calls: list[str] = []

    def sync(**_kwargs) -> list:
        state = AutomationStore(app.database).state("official-docs")
        assert state["status"] == "running"
        assert state["selected_products"] == {"cmdb": ["26.1"]}
        calls.append("sync")
        return []

    coordinator = EmbeddedOfficialSyncCoordinator(
        store=AutomationStore(app.database),
        settings=settings(),
        coordination=coordination(),
        sync_official=sync,
        official_document_count=lambda: 0,
        owner_id="one",
    )

    coordinator.start()
    try:
        wait_until(lambda: calls == ["sync"])
        wait_until(
            lambda: AutomationStore(app.database).state("official-docs").get("status") == "ok"
        )
        state = AutomationStore(app.database).state("official-docs")
        assert state["status"] == "ok"
        assert state["selected_products"] == {"cmdb": ["26.1"]}
        time.sleep(0.2)
        assert calls == ["sync"]
    finally:
        coordinator.stop()


def test_official_coordinator_runs_again_when_interval_expires(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    frequent = settings()
    frequent.interval_hours = 0.00001
    calls: list[str] = []
    coordinator = EmbeddedOfficialSyncCoordinator(
        store=AutomationStore(app.database),
        settings=frequent,
        coordination=coordination(),
        sync_official=lambda **_kwargs: calls.append("sync") or [],
        official_document_count=lambda: 1,
        owner_id="persistent-dashboard",
    )

    coordinator.start()
    try:
        wait_until(lambda: len(calls) >= 2)
    finally:
        coordinator.stop(timeout=1.0)


def test_official_coordinator_run_once_releases_lease(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    calls: list[str] = []
    coordinator = EmbeddedOfficialSyncCoordinator(
        store=store,
        settings=settings(),
        coordination=coordination(),
        sync_official=lambda **_kwargs: calls.append("sync") or [],
        official_document_count=lambda: 0,
        owner_id="detached-worker",
    )

    assert coordinator.run_once() is True

    assert calls == ["sync"]
    assert store.state("official-docs")["status"] == "ok"
    assert store.acquire_or_renew("embedded-official-sync", "replacement", 10) is True


def test_official_coordinator_force_runs_before_the_next_scheduled_time(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    store.update_state("official-docs", {"status": "ok", "next_run_epoch": time.time() + 3600})
    calls: list[str] = []
    coordinator = EmbeddedOfficialSyncCoordinator(
        store=store,
        settings=settings(),
        coordination=coordination(),
        sync_official=lambda **_kwargs: calls.append("sync") or [],
        official_document_count=lambda: 1,
        owner_id="manual-worker",
    )

    assert coordinator.run_once(force=True) is True
    assert calls == ["sync"]


def test_official_coordinator_run_once_exits_when_worker_already_owns_lease(
    config_path,
) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    assert store.acquire_or_renew("embedded-official-sync", "existing-worker", 30) is True
    calls: list[str] = []
    coordinator = EmbeddedOfficialSyncCoordinator(
        store=store,
        settings=settings(),
        coordination=coordination(),
        sync_official=lambda **_kwargs: calls.append("sync") or [],
        official_document_count=lambda: 0,
        owner_id="duplicate-worker",
    )

    assert coordinator.run_once() is False
    assert calls == []


def test_manual_official_lease_rejects_a_concurrent_sync(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication
    from helix_mcp_knowledge.errors import SourceSyncError

    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    first = OfficialSyncLease(store, coordination(), owner_id="manual-one")

    with (
        first,
        pytest.raises(SourceSyncError, match="already running"),
        OfficialSyncLease(store, coordination(), owner_id="manual-two"),
    ):
        raise AssertionError("concurrent lease unexpectedly acquired")


def test_official_coordinator_retries_interrupted_running_state(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    store.update_state("official-docs", {"status": "running", "owner_id": "dead"})
    calls: list[str] = []
    coordinator = EmbeddedOfficialSyncCoordinator(
        store=store,
        settings=settings(),
        coordination=coordination(),
        sync_official=lambda **_kwargs: calls.append("retry") or [],
        official_document_count=lambda: 0,
        owner_id="replacement",
    )

    coordinator.start()
    try:
        wait_until(lambda: calls == ["retry"])
        wait_until(lambda: store.state("official-docs").get("status") == "ok")
        assert store.state("official-docs")["status"] == "ok"
    finally:
        coordinator.stop()


def test_official_coordinator_resumes_pending_state(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    store.update_state(
        "official-docs",
        {"status": "pending", "owner_id": "stopped", "next_run_epoch": 0},
    )
    calls: list[str] = []
    coordinator = EmbeddedOfficialSyncCoordinator(
        store=store,
        settings=settings(),
        coordination=coordination(),
        sync_official=lambda **_kwargs: calls.append("resume") or [],
        official_document_count=lambda: 0,
        owner_id="replacement",
    )

    coordinator.start()
    try:
        wait_until(lambda: calls == ["resume"])
        wait_until(lambda: store.state("official-docs").get("status") == "ok")
        assert store.state("official-docs")["owner_id"] == "replacement"
    finally:
        coordinator.stop()


def test_official_coordinator_shutdown_is_bounded_and_marks_run_pending(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    entered = Event()
    release = Event()

    def blocked_sync(**_kwargs) -> list:
        entered.set()
        release.wait(timeout=3)
        return []

    coordinator = EmbeddedOfficialSyncCoordinator(
        store=store,
        settings=settings(),
        coordination=coordination(),
        sync_official=blocked_sync,
        official_document_count=lambda: 0,
        owner_id="stopping",
    )
    coordinator.start()
    try:
        assert entered.wait(timeout=3)
        started = time.monotonic()
        coordinator.stop(timeout=0.05)
        elapsed = time.monotonic() - started

        state = store.state("official-docs")
        assert elapsed < 0.5
        assert state["status"] == "pending"
        assert state["reason"] == "server_shutdown"
        assert state["owner_id"] == "stopping"
        assert store.acquire_or_renew("embedded-official-sync", "other", 10) is False

        release.set()
        wait_until(lambda: coordinator._worker is not None and not coordinator._worker.is_alive())
        assert store.state("official-docs")["status"] == "pending"
    finally:
        release.set()
        coordinator.stop()


def test_official_coordinator_persists_progress_and_user_cancellation(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication
    from helix_mcp_knowledge.errors import OfficialSyncCancelled

    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    entered = Event()

    def cancellable_sync(*, progress_callback, cancel_check) -> list:
        progress_callback(
            {
                "phase": "discovering",
                "processed_items": 7,
                "estimated_total_items": 20,
                "percent": 35.0,
                "current_product": "cmdb",
                "current_version": "26.1",
                "last_activity_at": "2026-09-06T10:00:00+00:00",
                "result_counts": {"unchanged": 7},
                "chunks_indexed": 0,
                "product_versions": [
                    {
                        "product": "cmdb",
                        "version": "26.1",
                        "processed_items": 7,
                        "estimated_total_items": 20,
                    }
                ],
            }
        )
        entered.set()
        wait_until(cancel_check)
        raise OfficialSyncCancelled()

    coordinator = EmbeddedOfficialSyncCoordinator(
        store=store,
        settings=settings(),
        coordination=coordination(),
        sync_official=cancellable_sync,
        official_document_count=lambda: 0,
        owner_id="cancellable",
    )
    coordinator.start()
    try:
        assert entered.wait(timeout=3)
        assert store.patch_state_if_current(
            "official-docs",
            expected_status="running",
            values={"cancel_requested": True},
        )
        wait_until(lambda: store.state("official-docs").get("status") == "cancelled")
        state = store.state("official-docs")
        assert state["reason"] == "user_requested"
        assert state["cancel_requested"] is False
        assert state["result_counts"] == {"unchanged": 7}
        assert state["progress"]["processed_items"] == 7
        assert state["errors"] == []
    finally:
        coordinator.stop()


def test_official_coordinator_does_not_start_sync_after_stop(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    calls: list[str] = []
    coordinator = EmbeddedOfficialSyncCoordinator(
        store=AutomationStore(app.database),
        settings=settings(),
        coordination=coordination(),
        sync_official=lambda **_kwargs: calls.append("sync") or [],
        official_document_count=lambda: 0,
        owner_id="stopped",
    )

    coordinator.stop()
    coordinator._sync()

    assert calls == []
    assert AutomationStore(app.database).state("official-docs") == {}


def test_official_coordinator_can_defer_empty_bootstrap(config_path) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    app = KnowledgeApplication.from_config(config_path)
    calls: list[str] = []
    coordinator = EmbeddedOfficialSyncCoordinator(
        store=AutomationStore(app.database),
        settings=settings(bootstrap_on_empty=False),
        coordination=coordination(),
        sync_official=lambda **_kwargs: calls.append("sync") or [],
        official_document_count=lambda: 0,
        owner_id="one",
    )

    coordinator.start()
    try:
        wait_until(
            lambda: AutomationStore(app.database).state("official-docs").get("status") == "waiting"
        )
        assert calls == []
    finally:
        coordinator.stop()

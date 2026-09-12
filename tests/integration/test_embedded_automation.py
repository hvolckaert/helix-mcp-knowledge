import threading
import time
from pathlib import Path

import pytest

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.automation import EmbeddedSyncCoordinator, ProjectSyncLease
from helix_mcp_knowledge.config import WatchSettings
from helix_mcp_knowledge.errors import SourceSyncError
from helix_mcp_knowledge.storage.automation import AutomationStore


def wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached before timeout")


def configure_project_manifest(config_path: Path) -> Path:
    project_path = config_path.parent / "projects/example_project.yaml"
    project_path.write_text(
        project_path.read_text(encoding="utf-8").replace(
            "  path: data/sources/projects/example_project/docs",
            "  path: data/sources/projects/example_project/docs\n"
            "  sources_manifest: config/projects/example_project.sources.yaml",
        ),
        encoding="utf-8",
    )
    manifest = config_path.parent / "projects/example_project.sources.yaml"
    manifest.write_text(
        "schema_version: 1\nproject_id: example_project\nsources: []\n", encoding="utf-8"
    )
    return config_path.parent.parent / "data/sources/projects/example_project/docs/watched.md"


def coordinator(app, callback, owner_id: str) -> EmbeddedSyncCoordinator:
    return EmbeddedSyncCoordinator(
        registry=app.registry,
        store=AutomationStore(app.database),
        settings=WatchSettings(
            enabled=True,
            debounce_seconds=0.1,
            poll_seconds=0.1,
            lease_seconds=10,
            retry_seconds=5,
        ),
        allowed_extensions=app.config.ingestion.allowed_extensions,
        sync_project=callback,
        owner_id=owner_id,
    )


def test_embedded_coordinator_watches_changes_and_fails_over(config_path: Path) -> None:
    watched = configure_project_manifest(config_path)
    watched.parent.mkdir(parents=True)
    watched.write_text("initial", encoding="utf-8")
    first_app = KnowledgeApplication.from_config(config_path)
    second_app = KnowledgeApplication.from_config(config_path)
    first_calls: list[str] = []
    second_calls: list[str] = []

    def first_sync(project_id: str) -> list:
        state = AutomationStore(first_app.database).state(f"project:{project_id}")
        assert state["status"] == "running"
        assert state["owner_id"] == "one"
        first_calls.append(project_id)
        return []

    first = coordinator(first_app, first_sync, "one")
    second = coordinator(
        second_app, lambda project_id: second_calls.append(project_id) or [], "two"
    )

    first.start()
    try:
        wait_until(lambda: first_calls == ["example_project"])
        second.start()
        time.sleep(0.2)
        assert second_calls == []

        watched.write_text("changed", encoding="utf-8")
        wait_until(lambda: len(first_calls) == 2)

        first.stop(timeout=2.0)
        wait_until(lambda: second_calls == ["example_project"])
        assert second.is_leader is True
    finally:
        first.stop()
        second.stop()


def test_manual_project_sync_rejects_an_existing_watcher_lease(config_path: Path) -> None:
    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    assert store.acquire_or_renew("embedded-project-sync", "watcher", 30)

    with (
        pytest.raises(SourceSyncError, match="already running"),
        ProjectSyncLease(store, app.config.ingestion.watch),
    ):
        raise AssertionError("concurrent project lease unexpectedly acquired")


def test_dashboard_request_is_consumed_when_automatic_watching_is_disabled(
    config_path: Path,
) -> None:
    watched = configure_project_manifest(config_path)
    watched.parent.mkdir(parents=True)
    watched.write_text("manual request", encoding="utf-8")
    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    calls: list[str] = []
    manual_only = EmbeddedSyncCoordinator(
        registry=app.registry,
        store=store,
        settings=WatchSettings(
            enabled=False,
            debounce_seconds=0.1,
            poll_seconds=0.1,
            lease_seconds=10,
            retry_seconds=5,
        ),
        allowed_extensions=app.config.ingestion.allowed_extensions,
        sync_project=lambda project_id: calls.append(project_id) or [],
        owner_id="dashboard",
    )

    manual_only.start()
    try:
        time.sleep(0.15)
        assert calls == []
        with ProjectSyncLease(store, app.config.ingestion.watch):
            pass
        store.update_state(
            "project:example_project",
            {"status": "pending", "project_id": "example_project", "reason": "user_requested"},
        )
        wait_until(
            lambda: (
                calls == ["example_project"]
                and store.state("project:example_project").get("status") == "ok"
                and not manual_only.is_leader
            )
        )
        state = store.state("project:example_project")
        assert state["status"] == "ok"
        assert state["detected_files"] == 1
        with ProjectSyncLease(store, app.config.ingestion.watch):
            pass
    finally:
        manual_only.stop(timeout=2.0)


def test_two_manual_only_coordinators_claim_one_request_once(config_path: Path) -> None:
    configure_project_manifest(config_path)
    app = KnowledgeApplication.from_config(config_path)
    first_store = AutomationStore(app.database)
    second_store = AutomationStore(app.database)
    observer = AutomationStore(app.database)
    barrier = threading.Barrier(2)
    calls: list[str] = []
    calls_lock = threading.Lock()

    def synchronize_first_pending_read(store: AutomationStore) -> None:
        original_state = store.state
        first_read = True

        def state(job_name: str):
            nonlocal first_read
            value = original_state(job_name)
            if first_read and job_name == "project:example_project":
                first_read = False
                barrier.wait(timeout=2)
            return value

        store.state = state  # type: ignore[method-assign]

    synchronize_first_pending_read(first_store)
    synchronize_first_pending_read(second_store)

    def sync(project_id: str) -> list:
        with calls_lock:
            calls.append(project_id)
        return []

    settings = WatchSettings(
        enabled=False,
        debounce_seconds=0.1,
        poll_seconds=0.1,
        lease_seconds=10,
        retry_seconds=5,
    )
    first = EmbeddedSyncCoordinator(
        registry=app.registry,
        store=first_store,
        settings=settings,
        allowed_extensions=app.config.ingestion.allowed_extensions,
        sync_project=sync,
        owner_id="dashboard-one",
    )
    second = EmbeddedSyncCoordinator(
        registry=app.registry,
        store=second_store,
        settings=settings,
        allowed_extensions=app.config.ingestion.allowed_extensions,
        sync_project=sync,
        owner_id="dashboard-two",
    )
    first_store.update_state(
        "project:example_project",
        {"status": "pending", "project_id": "example_project", "reason": "user_requested"},
    )

    first.start()
    second.start()
    try:
        wait_until(lambda: observer.state("project:example_project").get("status") == "ok")
        time.sleep(0.2)
        assert calls == ["example_project"]
    finally:
        first.stop(timeout=2.0)
        second.stop(timeout=2.0)


def test_new_dashboard_request_bypasses_a_previous_error_backoff(config_path: Path) -> None:
    configure_project_manifest(config_path)
    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    attempts: list[str] = []

    def fail_once(project_id: str) -> list:
        attempts.append(project_id)
        if len(attempts) == 1:
            raise RuntimeError("temporary parser failure")
        return []

    manual_only = EmbeddedSyncCoordinator(
        registry=app.registry,
        store=store,
        settings=WatchSettings(
            enabled=False,
            debounce_seconds=0.1,
            poll_seconds=0.1,
            lease_seconds=10,
            retry_seconds=5,
        ),
        allowed_extensions=app.config.ingestion.allowed_extensions,
        sync_project=fail_once,
        owner_id="dashboard",
    )
    store.update_state(
        "project:example_project",
        {"status": "pending", "project_id": "example_project", "reason": "user_requested"},
    )

    manual_only.start()
    try:
        wait_until(lambda: store.state("project:example_project").get("status") == "error")
        store.update_state(
            "project:example_project",
            {"status": "pending", "project_id": "example_project", "reason": "user_requested"},
        )
        wait_until(
            lambda: (
                len(attempts) == 2 and store.state("project:example_project").get("status") == "ok"
            )
        )
    finally:
        manual_only.stop(timeout=2.0)


def test_long_lived_dashboard_coordinator_reloads_watch_enabled(config_path: Path) -> None:
    configure_project_manifest(config_path)
    app = KnowledgeApplication.from_config(config_path)
    store = AutomationStore(app.database)
    calls: list[str] = []
    revision = [0]
    enabled = [False]

    def reload_configuration():
        return (
            app.registry,
            WatchSettings(
                enabled=enabled[0],
                debounce_seconds=0.1,
                poll_seconds=0.1,
                lease_seconds=10,
                retry_seconds=5,
            ),
            app.config.ingestion.allowed_extensions,
            lambda project_id: calls.append(project_id) or [],
        )

    coordinator = EmbeddedSyncCoordinator(
        registry=app.registry,
        store=store,
        settings=reload_configuration()[1],
        allowed_extensions=app.config.ingestion.allowed_extensions,
        sync_project=lambda project_id: calls.append(project_id) or [],
        owner_id="dashboard",
        configuration_snapshot=lambda: (("configuration", revision[0], 0, 0),),
        reload_configuration=reload_configuration,
    )

    coordinator.start()
    try:
        time.sleep(0.2)
        assert calls == []
        assert coordinator.is_leader is False

        enabled[0] = True
        revision[0] += 1
        wait_until(lambda: calls == ["example_project"] and coordinator.is_leader)

        enabled[0] = False
        revision[0] += 1
        wait_until(lambda: coordinator.is_leader is False)
        time.sleep(0.25)
        assert calls == ["example_project"]
    finally:
        coordinator.stop(timeout=2.0)


def test_application_coordinator_reloads_project_product_configuration(
    config_path: Path,
) -> None:
    watched = configure_project_manifest(config_path)
    watched.parent.mkdir(parents=True)
    watched.write_text("# Watched\n\nProject configuration evidence.", encoding="utf-8")
    manifest_path = config_path.parent / "projects/example_project.sources.yaml"
    manifest_path.write_text(
        """schema_version: 1
project_id: example_project
sources:
  - path: watched.md
    document_type: design
    products:
      cmdb: null
""",
        encoding="utf-8",
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "watch: {enabled: false, debounce_seconds: 5}",
            "watch: {enabled: true, debounce_seconds: 0.1, poll_seconds: 0.1, "
            "lease_seconds: 10, retry_seconds: 5}",
        ),
        encoding="utf-8",
    )
    app = KnowledgeApplication.from_config(config_path)
    coordinator = app.create_sync_coordinator()
    coordinator.start()
    try:
        wait_until(
            lambda: _indexed_project_version(app, "example_project") == "26.1",
            timeout=5,
        )
        project_path = config_path.parent / "projects/example_project.yaml"
        updated = project_path.read_text(encoding="utf-8").replace(
            'version: "26.1"', 'version: "26.2"'
        )
        project_path.write_text(updated, encoding="utf-8")
        wait_until(
            lambda: _indexed_project_version(app, "example_project") == "26.2",
            timeout=5,
        )
    finally:
        coordinator.stop(timeout=2.0)


def _indexed_project_version(app: KnowledgeApplication, project_id: str) -> str | None:
    with app.database.connect() as connection:
        row = connection.execute(
            """
            SELECT pv.version
            FROM documents d
            JOIN document_product_versions dpv ON dpv.document_id = d.document_id
            JOIN product_versions pv ON pv.product_version_id = dpv.product_version_id
            WHERE d.project_id = ? AND d.status = 'indexed'
            """,
            (project_id,),
        ).fetchone()
    return str(row["version"]) if row is not None else None

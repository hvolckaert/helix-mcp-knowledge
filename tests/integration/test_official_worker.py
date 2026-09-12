import os
import subprocess
import threading
from pathlib import Path

from helix_mcp_knowledge.official_worker import (
    OfficialSyncWorkerLauncher,
    PersistentOfficialSyncScheduler,
    run_worker,
)


class FakeProcess:
    pid = 4321

    def __init__(self) -> None:
        self.waited = threading.Event()

    def wait(self) -> int:
        self.waited.set()
        return 0


def test_worker_launcher_detaches_from_mcp_stdio(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    config_path = tmp_path / "config/config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("placeholder", encoding="utf-8")
    launcher = OfficialSyncWorkerLauncher(
        config_path=config_path,
        workspace=tmp_path,
        errors_path=tmp_path / "data/errors",
        python_executable="/runtime/python",
    )

    process = launcher.start()

    assert process.pid == 4321
    assert launcher._process is not None
    assert launcher._process.waited.wait(timeout=1.0)
    assert captured["command"] == [
        "/runtime/python",
        "-m",
        "helix_mcp_knowledge.official_worker",
        "--config",
        str(config_path),
    ]
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["env"]["HELIX_KNOWLEDGE_CONFIG"] == str(config_path)
    if os.name == "nt":
        assert kwargs["creationflags"]
    else:
        assert kwargs["start_new_session"] is True


def test_worker_launcher_can_force_manual_sync(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    config_path = tmp_path / "config/config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("placeholder", encoding="utf-8")
    launcher = OfficialSyncWorkerLauncher(
        config_path=config_path,
        workspace=tmp_path,
        errors_path=tmp_path / "data/errors",
        python_executable="/runtime/python",
    )

    launcher.start(force=True)

    assert captured["command"][-1] == "--force"


def test_worker_skips_when_automatic_sync_is_disabled(config_path: Path) -> None:
    assert run_worker(config_path) is False


def test_worker_skips_when_no_products_are_selected(config_path: Path) -> None:
    payload = config_path.read_text(encoding="utf-8").replace(
        "automatic_sync: false", "automatic_sync: true"
    )
    payload = payload.replace(
        '  products:\n    cmdb: {versions: ["26.1"]}',
        "  products: {}",
    )
    config_path.write_text(payload, encoding="utf-8")

    assert run_worker(config_path) is False


def test_worker_force_runs_cleanup_when_no_products_are_selected(
    config_path: Path, monkeypatch
) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    payload = config_path.read_text(encoding="utf-8")
    payload = payload.replace(
        '  products:\n    cmdb: {versions: ["26.1"]}',
        "  products: {}",
    ).replace("retain_unselected_versions: true", "retain_unselected_versions: false")
    config_path.write_text(payload, encoding="utf-8")
    calls: list[str] = []

    class Coordinator:
        def run_once(self, *, force: bool = False) -> bool:
            calls.append(f"run_once:{force}")
            return True

    monkeypatch.setattr(
        KnowledgeApplication,
        "create_official_sync_coordinator",
        lambda self: Coordinator(),
    )

    assert run_worker(config_path, force=True) is True
    assert calls == ["run_once:True"]


def test_worker_force_runs_when_automatic_sync_is_disabled(config_path: Path, monkeypatch) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    calls: list[str] = []

    class Coordinator:
        def run_once(self, *, force: bool = False) -> bool:
            calls.append(f"run_once:{force}")
            return True

    monkeypatch.setattr(
        KnowledgeApplication,
        "create_official_sync_coordinator",
        lambda self: Coordinator(),
    )

    assert run_worker(config_path, force=True) is True
    assert calls == ["run_once:True"]


def test_worker_runs_one_coordinated_pass(config_path: Path, monkeypatch) -> None:
    from helix_mcp_knowledge.application import KnowledgeApplication

    payload = config_path.read_text(encoding="utf-8").replace(
        "automatic_sync: false", "automatic_sync: true"
    )
    config_path.write_text(payload, encoding="utf-8")
    calls: list[str] = []

    class Coordinator:
        def run_once(self, *, force: bool = False) -> bool:
            calls.append(f"run_once:{force}")
            return True

    monkeypatch.setattr(
        KnowledgeApplication,
        "create_official_sync_coordinator",
        lambda self: Coordinator(),
    )

    assert run_worker(config_path) is True
    assert calls == ["run_once:False"]


def test_persistent_scheduler_reloads_after_dashboard_configuration_change(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("first", encoding="utf-8")

    class FakeCoordinator:
        def __init__(self, value: str) -> None:
            self.value = value
            self.started = threading.Event()
            self.stopped = threading.Event()

        def start(self) -> None:
            self.started.set()

        def stop(self, timeout: float = 0.25) -> None:
            self.stopped.set()

    created: list[FakeCoordinator] = []

    def factory(path: Path) -> FakeCoordinator:
        coordinator = FakeCoordinator(path.read_text(encoding="utf-8"))
        created.append(coordinator)
        return coordinator

    scheduler = PersistentOfficialSyncScheduler(
        config_path,
        coordinator_factory=factory,
        signature_factory=lambda path: ((str(path), path.stat().st_mtime_ns, path.stat().st_size),),
        poll_seconds=0.05,
    )
    scheduler.start()
    try:
        assert _wait_for(lambda: len(created) == 1 and created[0].started.is_set())
        replacement = config_path.with_suffix(".tmp")
        replacement.write_text("second-value", encoding="utf-8")
        replacement.replace(config_path)
        assert _wait_for(lambda: len(created) == 2 and created[1].started.is_set())
        assert created[0].stopped.is_set()
        assert created[1].value == "second-value"
    finally:
        scheduler.stop(timeout=1.0)
    assert created[1].stopped.is_set()


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False

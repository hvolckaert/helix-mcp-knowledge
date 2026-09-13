from __future__ import annotations

import os
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from helix_mcp_knowledge.application import KnowledgeApplication
from helix_mcp_knowledge.dashboard_runtime import DASHBOARD_MODE_ENV
from helix_mcp_knowledge.dashboard_update_worker import (
    DASHBOARD_UPDATE_JOB,
    DashboardUpdateWorkerLauncher,
    _consume_dashboard_token,
    _documentation_sync_active,
    _wait_for_documentation_sync,
    run_update,
)
from helix_mcp_knowledge.errors import KnowledgeError
from helix_mcp_knowledge.storage.automation import AutomationStore
from helix_mcp_knowledge.storage.database import Database
from helix_mcp_knowledge.storage_retention import RetentionResult


class FakeProcess:
    pid = 9876

    def __init__(self) -> None:
        self.waited = threading.Event()

    def wait(self) -> int:
        self.waited.set()
        return 0


def test_dashboard_update_launcher_detaches_from_http_process(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(DASHBOARD_MODE_ENV, raising=False)
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    config_path = tmp_path / "config/config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("placeholder", encoding="utf-8")
    launcher = DashboardUpdateWorkerLauncher(
        config_path=config_path,
        workspace=tmp_path,
        errors_path=tmp_path / "data/errors",
        repository="owner/repository",
        target_version="1.9.0",
        server_name="custom_knowledge",
        openclaw_command="/opt/openclaw",
        gh_command="/opt/gh",
        dashboard_port=8877,
        dashboard_token="private-dashboard-token",
        python_executable="/runtime/python",
    )

    process = launcher.start()

    assert process.pid == 9876
    assert launcher._process is not None
    assert launcher._process.waited.wait(timeout=1.0)
    assert captured["command"] == [
        "/runtime/python",
        "-m",
        "helix_mcp_knowledge.dashboard_update_worker",
        "--config",
        str(config_path),
        "--repository",
        "owner/repository",
        "--target-version",
        "1.9.0",
        "--server-name",
        "custom_knowledge",
        "--openclaw-command",
        "/opt/openclaw",
        "--gh-command",
        "/opt/gh",
        "--dashboard-port",
        "8877",
    ]
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["env"]["HELIX_KNOWLEDGE_DASHBOARD_TOKEN"] == "private-dashboard-token"
    assert "private-dashboard-token" not in captured["command"]
    if os.name == "nt":
        assert kwargs["creationflags"]
    else:
        assert kwargs["start_new_session"] is True


@pytest.mark.skipif(os.name == "nt", reason="systemd user services are POSIX-only")
def test_systemd_dashboard_update_launcher_uses_independent_transient_unit(
    tmp_path: Path, monkeypatch
) -> None:
    captured: list[list[str]] = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        if command[0] == "/usr/bin/systemctl":
            return SimpleNamespace(returncode=0, stdout="4321\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv(DASHBOARD_MODE_ENV, "systemd_user")
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("systemd-managed update used Popen"),
    )
    config_path = tmp_path / "config/config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("placeholder", encoding="utf-8")
    launcher = DashboardUpdateWorkerLauncher(
        config_path=config_path,
        workspace=tmp_path,
        errors_path=tmp_path / "data/errors",
        repository="owner/repository",
        target_version="1.9.0",
        server_name="custom_knowledge",
        openclaw_command="/opt/openclaw",
        gh_command="/opt/gh",
        dashboard_port=8877,
        dashboard_token="private-dashboard-token",
        python_executable="/runtime/python",
    )

    process = launcher.start()

    assert process.pid == 4321
    launch_command = captured[0]
    assert launch_command[0] == "/usr/bin/systemd-run"
    assert "--user" in launch_command
    assert "--collect" in launch_command
    assert "--service-type=exec" in launch_command
    assert any(
        argument.startswith("--unit=helix-mcp-knowledge-update-") for argument in launch_command
    )
    assert "helix_mcp_knowledge.dashboard_update_worker" in launch_command
    assert "private-dashboard-token" not in launch_command
    token_option = launch_command.index("--dashboard-token-file")
    token_path = Path(launch_command[token_option + 1])
    assert token_path.read_text(encoding="utf-8") == "private-dashboard-token"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600

    assert _consume_dashboard_token(token_path) == "private-dashboard-token"
    assert not token_path.exists()


def test_update_worker_waits_for_active_documentation_sync(app, monkeypatch) -> None:
    store = AutomationStore(app.database)
    store.update_state("official-docs", {"status": "running", "owner_id": "sync-worker"})
    checks = iter([True, False])
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker._documentation_sync_active",
        lambda _store: next(checks),
    )
    monkeypatch.setattr("helix_mcp_knowledge.dashboard_update_worker.time.sleep", lambda _: None)

    assert _wait_for_documentation_sync(
        store,
        target_version="1.10.0",
        timeout_seconds=30,
    )

    state = store.state(DASHBOARD_UPDATE_JOB)
    assert state["status"] == "waiting_for_sync"
    assert state["target_version"] == "1.10.0"


def test_update_worker_ignores_stale_running_state_but_honours_live_lease(app) -> None:
    store = AutomationStore(app.database)
    store.update_state("official-docs", {"status": "running", "owner_id": "sync-worker"})

    assert _documentation_sync_active(store) is False
    assert store.acquire_or_renew("embedded-official-sync", "sync-worker", 30)
    assert _documentation_sync_active(store) is True


def test_update_worker_restarts_gateway_and_dashboard_with_target_runtime(
    config_path: Path, monkeypatch
) -> None:
    workspace = config_path.parent.parent
    KnowledgeApplication.from_config(config_path)
    target_runtime = workspace / "runtime/1.9.0"
    target_python = target_runtime / "venv" / ("Scripts" if os.name == "nt" else "bin")
    target_python.mkdir(parents=True)
    (target_python / ("python.exe" if os.name == "nt" else "python")).write_text(
        "target", encoding="utf-8"
    )
    captured: dict[str, object] = {}

    def fake_update(**kwargs):
        kwargs["post_activation_check"](SimpleNamespace(active_version="1.9.0"))
        return SimpleNamespace(
            status="updated",
            target_version="1.9.0",
            target_runtime=target_runtime,
            storage_retention=RetentionResult(status="completed", reclaimed_bytes=1048576),
        )

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker.update_installation", fake_update
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker._resolve_command",
        lambda *_, **__: Path("/opt/openclaw"),
    )

    def fake_run(command, **kwargs):
        captured["gateway_command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("helix_mcp_knowledge.dashboard_update_worker._run", fake_run)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker._wait_until_port_is_free",
        lambda *_args, **_kwargs: None,
    )

    class FakeDashboardManager:
        def __init__(self, installation, **kwargs):
            captured["dashboard"] = {"installation": installation, **kwargs}

        def restart_and_verify(self, *, expected_version):
            captured["expected_version"] = expected_version
            return SimpleNamespace(
                process_id=5432,
                to_dict=lambda: {"manager": "systemd_user", "process_id": 5432},
            )

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker.DashboardRuntimeManager",
        FakeDashboardManager,
    )

    succeeded = run_update(
        config_path=config_path,
        repository="owner/repository",
        target_version="1.9.0",
        server_name="custom_knowledge",
        openclaw_command="/opt/openclaw",
        gh_command="/opt/gh",
        dashboard_port=8877,
    )

    assert succeeded is True
    assert captured["gateway_command"] == [str(Path("/opt/openclaw")), "gateway", "restart"]
    assert captured["dashboard"]["server_name"] == "custom_knowledge"
    assert captured["expected_version"] == "1.9.0"
    state = AutomationStore(Database(config_path.parent.parent / "data/sqlite/test.db")).state(
        DASHBOARD_UPDATE_JOB
    )
    assert state["status"] == "success"
    assert state["current_version"] == "1.9.0"
    assert state["gateway_restarted"] is True
    assert state["dashboard_runtime"]["manager"] == "systemd_user"
    assert state["storage_retention"]["status"] == "completed"
    assert state["storage_retention"]["reclaimed_bytes"] == 1048576


def test_update_worker_does_not_require_or_restart_openclaw_for_standalone(
    config_path: Path, monkeypatch
) -> None:
    workspace = config_path.parent.parent
    KnowledgeApplication.from_config(config_path)
    target_runtime = workspace / "runtime/1.9.0"
    executable_dir = target_runtime / "venv" / ("Scripts" if os.name == "nt" else "bin")
    executable_dir.mkdir(parents=True)
    target_python = executable_dir / ("python.exe" if os.name == "nt" else "python")
    target_python.write_text("target", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_update(**kwargs):
        kwargs["post_activation_check"](SimpleNamespace(active_version="1.9.0"))
        return SimpleNamespace(
            status="updated",
            target_version="1.9.0",
            target_runtime=target_runtime,
            client_integration="standalone",
        )

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker.update_installation", fake_update
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker._resolve_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OpenClaw must not be resolved")
        ),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker._run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OpenClaw must not be restarted")
        ),
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker._wait_until_port_is_free",
        lambda *_args, **_kwargs: None,
    )

    class FakeDashboardManager:
        def __init__(self, installation, **kwargs):
            captured["dashboard"] = {"installation": installation, **kwargs}

        def restart_and_verify(self, *, expected_version):
            return SimpleNamespace(
                process_id=7654,
                to_dict=lambda: {"manager": "systemd_user", "process_id": 7654},
            )

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker.DashboardRuntimeManager",
        FakeDashboardManager,
    )

    succeeded = run_update(
        config_path=config_path,
        repository="owner/repository",
        target_version="1.9.0",
        server_name="helix_knowledge",
        openclaw_command="missing-openclaw",
        gh_command="/opt/gh",
        dashboard_port=8877,
    )

    assert succeeded is True
    assert captured["dashboard"]["server_name"] == "helix_knowledge"
    state = AutomationStore(Database(workspace / "data/sqlite/test.db")).state(DASHBOARD_UPDATE_JOB)
    assert state["status"] == "success"
    assert state["current_version"] == "1.9.0"
    assert state["gateway_restarted"] is False


def test_update_worker_restores_current_dashboard_after_failure(
    config_path: Path, monkeypatch
) -> None:
    KnowledgeApplication.from_config(config_path)
    captured: dict[str, object] = {}

    def fail_update(**kwargs):
        raise KnowledgeError("simulated transactional failure; rollback succeeded")

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker.update_installation", fail_update
    )
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker._wait_until_port_is_free",
        lambda *_args, **_kwargs: None,
    )

    restored_installation = SimpleNamespace(active_version="1.13.2")
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker.load_managed_installation",
        lambda *_: restored_installation,
    )

    class FakeDashboardManager:
        def __init__(self, installation, **kwargs):
            captured["dashboard"] = {"installation": installation, **kwargs}

        def restart_and_verify(self, *, expected_version):
            return SimpleNamespace(
                process_id=6543,
                to_dict=lambda: {"manager": "systemd_user", "process_id": 6543},
            )

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_update_worker.DashboardRuntimeManager",
        FakeDashboardManager,
    )

    succeeded = run_update(
        config_path=config_path,
        repository="owner/repository",
        target_version="1.9.0",
        server_name="helix_knowledge",
        openclaw_command="/opt/openclaw",
        gh_command="/opt/gh",
        dashboard_port=8877,
    )

    assert succeeded is False
    assert captured["dashboard"]["installation"] is restored_installation
    state = AutomationStore(Database(config_path.parent.parent / "data/sqlite/test.db")).state(
        DASHBOARD_UPDATE_JOB
    )
    assert state["status"] == "error"
    assert "rollback succeeded" in state["error"]
    assert state["dashboard_process_id"] == 6543

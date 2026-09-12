from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from helix_mcp_knowledge.dashboard_host import supervise_dashboard
from helix_mcp_knowledge.dashboard_runtime import (
    DASHBOARD_SERVICE_NAME,
    DASHBOARD_WINDOWS_RUN_NAME,
    DashboardRuntimeManager,
    dashboard_workspace_id,
)
from helix_mcp_knowledge.errors import KnowledgeError
from helix_mcp_knowledge.managed_installation import (
    activate_managed_installation,
    versioned_runtime_paths,
)


def _installation(tmp_path: Path):
    workspace = tmp_path / "workspace with spaces"
    config = workspace / "config/config.yaml"
    executable_dir = workspace / "runtime/1.14.0/venv" / ("Scripts" if os.name == "nt" else "bin")
    server = executable_dir / (
        "helix-mcp-knowledge-server.exe" if os.name == "nt" else "helix-mcp-knowledge-server"
    )
    python = executable_dir / ("python.exe" if os.name == "nt" else "python")
    config.parent.mkdir(parents=True)
    executable_dir.mkdir(parents=True)
    config.write_text("schema_version: 1\n", encoding="utf-8")
    server.write_text("server", encoding="utf-8")
    python.write_text("python", encoding="utf-8")
    return activate_managed_installation(
        workspace=workspace,
        version="1.14.0",
        server_command=server,
        config_path=config,
        client="standalone",
    )


@pytest.mark.skipif(os.name == "nt", reason="systemd user services are POSIX-only")
def test_installs_no_admin_systemd_user_service(tmp_path: Path, monkeypatch) -> None:
    installation = _installation(tmp_path)
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        output = ""
        if "is-enabled" in command:
            output = "enabled\n"
        elif "is-active" in command:
            output = "active\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_runtime.shutil.which", lambda _: "/bin/systemctl"
    )
    monkeypatch.setattr(DashboardRuntimeManager, "_port_is_open", lambda _self: False)
    manager = DashboardRuntimeManager(
        installation,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        runner=runner,
        platform_name="posix",
        user_config_dir=tmp_path / "user-config",
    )

    result = manager.install_and_start(verify=False)

    assert result.status.manager == "systemd_user"
    assert result.status.enabled is True
    unit = (tmp_path / "user-config/systemd/user" / DASHBOARD_SERVICE_NAME).read_text(
        encoding="utf-8"
    )
    assert "Restart=on-failure" in unit
    assert "RestartSec=5s" in unit
    assert "KillMode=control-group" in unit
    assert "WantedBy=default.target" in unit
    assert "StandardOutput=append:" in unit
    assert "dashboard-supervisor.log" in unit
    assert str(installation.dashboard_launcher) in unit
    assert '"8765"' in unit
    assert ["systemctl", "--user", "enable", "--now", DASHBOARD_SERVICE_NAME] in calls


@pytest.mark.skipif(os.name == "nt", reason="systemd user services are POSIX-only")
def test_install_restarts_an_active_systemd_service_when_registration_changes(
    tmp_path: Path, monkeypatch
) -> None:
    installation = _installation(tmp_path)
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        output = ""
        if "is-enabled" in command:
            output = "enabled\n"
        elif "is-active" in command:
            output = "active\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_runtime.shutil.which", lambda _: "/bin/systemctl"
    )
    monkeypatch.setattr(DashboardRuntimeManager, "_port_is_open", lambda _self: False)
    manager = DashboardRuntimeManager(
        installation,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        runner=runner,
        platform_name="posix",
        user_config_dir=tmp_path / "user-config",
    )
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text("[Service]\nExecStart=/old/dashboard\n", encoding="utf-8")

    manager.install_and_start(verify=False)

    assert ["systemctl", "--user", "restart", DASHBOARD_SERVICE_NAME] in calls
    assert str(installation.dashboard_launcher) in manager.unit_path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="systemd user services are POSIX-only")
def test_install_restores_previous_systemd_service_when_candidate_health_fails(
    tmp_path: Path, monkeypatch
) -> None:
    installation = _installation(tmp_path)
    calls: list[list[str]] = []
    previous_unit = "[Service]\nExecStart=/old/dashboard\n"

    def runner(command, **_kwargs):
        calls.append(command)
        output = ""
        if "is-enabled" in command:
            output = "enabled\n"
        elif "is-active" in command:
            output = "active\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_runtime.shutil.which", lambda _: "/bin/systemctl"
    )
    monkeypatch.setattr(DashboardRuntimeManager, "_port_is_open", lambda _self: False)
    manager = DashboardRuntimeManager(
        installation,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        runner=runner,
        platform_name="posix",
        user_config_dir=tmp_path / "user-config",
    )
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text(previous_unit, encoding="utf-8")
    monkeypatch.setattr(
        manager,
        "wait_until_healthy",
        lambda **_kwargs: (_ for _ in ()).throw(KnowledgeError("candidate failed")),
    )

    with pytest.raises(KnowledgeError, match="candidate failed"):
        manager.install_and_start()

    assert manager.unit_path.read_text(encoding="utf-8") == previous_unit
    assert ["systemctl", "--user", "stop", DASHBOARD_SERVICE_NAME] in calls
    assert ["systemctl", "--user", "start", DASHBOARD_SERVICE_NAME] in calls
    assert calls.count(["systemctl", "--user", "daemon-reload"]) == 2


@pytest.mark.skipif(os.name == "nt", reason="systemd user services are POSIX-only")
def test_systemd_service_preserves_a_required_python_loader_path(
    tmp_path: Path, monkeypatch
) -> None:
    installation = _installation(tmp_path)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/custom python/lib")
    manager = DashboardRuntimeManager(
        installation,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        platform_name="posix",
        user_config_dir=tmp_path / "user-config",
    )

    unit = manager._render_unit()

    assert 'Environment="LD_LIBRARY_PATH=/opt/custom python/lib"' in unit


@pytest.mark.skipif(os.name == "nt", reason="systemd user services are POSIX-only")
def test_refresh_registration_migrates_systemd_unit_without_restart(
    tmp_path: Path, monkeypatch
) -> None:
    installation = _installation(tmp_path)
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        output = ""
        if "is-enabled" in command:
            output = "enabled\n"
        elif "is-active" in command:
            output = "active\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_runtime.shutil.which", lambda _: "/bin/systemctl"
    )
    manager = DashboardRuntimeManager(
        installation,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        runner=runner,
        platform_name="posix",
        user_config_dir=tmp_path / "user-config",
    )
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text("[Service]\nKillMode=process\n", encoding="utf-8")

    status = manager.refresh_registration()

    assert "KillMode=control-group" in manager.unit_path.read_text(encoding="utf-8")
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", DASHBOARD_SERVICE_NAME] in calls
    assert not any("restart" in call for call in calls)
    assert status.installed is True
    assert status.active is True


class _FakeRegistry:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def read(self, name: str) -> str | None:
        return self.values.get(name)

    def write(self, name: str, command: str) -> None:
        self.values[name] = command


def test_windows_uses_per_user_startup_supervisor_without_task_scheduler(
    tmp_path: Path, monkeypatch
) -> None:
    installation = _installation(tmp_path)
    registry = _FakeRegistry()
    launched: dict[str, object] = {}

    def process_factory(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(DashboardRuntimeManager, "_supervisor_alive", lambda _self: False)
    monkeypatch.setattr(DashboardRuntimeManager, "_port_is_open", lambda _self: False)
    manager = DashboardRuntimeManager(
        installation,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        process_factory=process_factory,
        platform_name="windows",
        windows_registry=registry,
    )

    result = manager.install_and_start(verify=False)

    assert result.process_id == 4321
    assert DASHBOARD_WINDOWS_RUN_NAME in registry.values
    startup = registry.values[DASHBOARD_WINDOWS_RUN_NAME]
    assert "powershell.exe" in startup
    assert "Task Scheduler" not in startup
    assert str(installation.dashboard_launcher) in startup
    assert "supervise" in launched["command"]
    assert int(launched["kwargs"]["creationflags"]) & 0x08000000


def test_windows_retries_a_launcher_that_exits_without_state_or_port(
    tmp_path: Path, monkeypatch
) -> None:
    installation = _installation(tmp_path)
    registry = _FakeRegistry()
    launched: list[list[str]] = []
    health_attempts = 0

    def process_factory(command, **_kwargs):
        launched.append(command)
        return SimpleNamespace(pid=4300 + len(launched))

    def wait_until_healthy(_self, *, expected_version: str) -> None:
        nonlocal health_attempts
        assert expected_version == installation.active_version
        health_attempts += 1
        if health_attempts == 1:
            raise KnowledgeError("first Windows launcher exited")

    monkeypatch.setattr(DashboardRuntimeManager, "_supervisor_alive", lambda _self: False)
    monkeypatch.setattr(DashboardRuntimeManager, "_port_is_open", lambda _self: False)
    monkeypatch.setattr(DashboardRuntimeManager, "wait_until_healthy", wait_until_healthy)
    manager = DashboardRuntimeManager(
        installation,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        process_factory=process_factory,
        platform_name="windows",
        windows_registry=registry,
    )

    result = manager.install_and_start()

    assert health_attempts == 2
    assert len(launched) == 2
    assert result.process_id == 4302


def test_falls_back_to_detached_supervisor_when_systemd_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    installation = _installation(tmp_path)
    launched: dict[str, object] = {}

    def process_factory(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return SimpleNamespace(pid=7654)

    monkeypatch.setenv("HELIX_KNOWLEDGE_DASHBOARD_MANAGER", "detached")
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_runtime.shutil.which", lambda _: "/bin/systemctl"
    )
    monkeypatch.setattr(DashboardRuntimeManager, "_supervisor_alive", lambda _self: False)
    monkeypatch.setattr(DashboardRuntimeManager, "_port_is_open", lambda _self: False)
    manager = DashboardRuntimeManager(
        installation,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        process_factory=process_factory,
        platform_name="posix",
    )

    result = manager.install_and_start(verify=False)

    assert result.process_id == 7654
    assert launched["command"][0] == str(installation.dashboard_launcher)
    assert launched["kwargs"]["start_new_session"] is True
    assert Path(launched["kwargs"]["stdout"].name).name == "dashboard-supervisor.log"
    assert launched["kwargs"]["stderr"] is subprocess.STDOUT


def test_supervisor_restarts_a_failed_dashboard_and_stops_after_clean_exit(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "workspace/config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("schema_version: 1\n", encoding="utf-8")
    exit_codes = iter([1, 0])
    commands: list[list[str]] = []
    registrations: list[dict[str, object]] = []

    class FakeChild:
        def __init__(self, code: int) -> None:
            self.pid = 5000 + code
            self.returncode = code

        def poll(self):
            return self.returncode

    def fake_popen(command, **_kwargs):
        commands.append(command)
        return FakeChild(next(exit_codes))

    monkeypatch.setattr("helix_mcp_knowledge.dashboard_host.subprocess.Popen", fake_popen)
    monkeypatch.setattr("helix_mcp_knowledge.dashboard_host._port_is_open", lambda _port: False)
    monkeypatch.setattr(
        "helix_mcp_knowledge.dashboard_host._refresh_runtime_registration",
        lambda **values: registrations.append(values),
    )

    result = supervise_dashboard(
        config_path=config,
        port=8765,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        restart_seconds=0,
    )

    assert result == 0
    assert len(commands) == 2
    assert registrations == [
        {
            "workspace": config.parent.parent,
            "server_name": "helix_knowledge",
            "openclaw_command": "openclaw",
        }
    ]
    assert not (tmp_path / "workspace/runtime/dashboard-supervisor.json").exists()


def test_supervisor_identity_requires_a_fresh_matching_heartbeat(
    tmp_path: Path, monkeypatch
) -> None:
    installation = _installation(tmp_path)
    manager = DashboardRuntimeManager(
        installation,
        server_name="helix_knowledge",
        openclaw_command="openclaw",
        platform_name="windows",
    )
    state_path = manager.workspace / "runtime/dashboard-supervisor.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "supervisor_pid": 4321,
        "port": manager.port,
        "workspace_id": dashboard_workspace_id(manager.workspace),
        "heartbeat_at_epoch": time.time(),
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("helix_mcp_knowledge.dashboard_runtime.os.kill", lambda _pid, _sig: None)

    assert manager._supervisor_alive() is True
    payload["heartbeat_at_epoch"] = time.time() - 60
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    assert manager._supervisor_alive() is False
    payload["heartbeat_at_epoch"] = time.time()
    payload["workspace_id"] = "another-workspace"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    assert manager._supervisor_alive() is False


def test_install_rejects_a_port_owned_by_another_local_service(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        _, server = versioned_runtime_paths(
            installation.config_path.parent.parent, installation.active_version
        )
        conflicting = activate_managed_installation(
            workspace=installation.config_path.parent.parent,
            version=installation.active_version,
            server_command=server,
            config_path=installation.config_path,
            client="standalone",
            dashboard_port=listener.getsockname()[1],
        )
        manager = DashboardRuntimeManager(
            conflicting,
            server_name="helix_knowledge",
            openclaw_command="openclaw",
        )

        with pytest.raises(KnowledgeError, match="already used"):
            manager.install_and_start(verify=False)

import json
import os
import subprocess
from pathlib import Path

import pytest

from helix_mcp_knowledge import openclaw as openclaw_module
from helix_mcp_knowledge.errors import KnowledgeError
from helix_mcp_knowledge.openclaw import (
    EXPOSED_TOOLS,
    install_openclaw_server,
    reload_openclaw,
)


def _executable(path: Path) -> Path:
    path.write_text("placeholder", encoding="utf-8")
    return path


def _powershell_log_shim(openclaw: Path, calls_log: Path) -> None:
    escaped_log = str(calls_log).replace("'", "''")
    openclaw.with_suffix(".ps1").write_text(
        f"Add-Content -LiteralPath '{escaped_log}' -Value ($args -join ' ')\nexit 0\n",
        encoding="utf-8",
    )


def test_install_openclaw_registers_exact_server_contract(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    openclaw = _executable(tmp_path / "openclaw")
    server = _executable(tmp_path / "helix-mcp-knowledge-server")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    result = install_openclaw_server(
        config_path=config,
        workspace=workspace,
        openclaw_command=openclaw,
        server_command=server,
        runner=runner,
    )

    registration = calls[0][0]
    assert registration[:4] == [str(openclaw), "mcp", "add", "helix_knowledge"]
    assert registration[registration.index("--command") + 1] == str(server)
    assert registration[registration.index("--cwd") + 1] == str(workspace)
    assert registration[registration.index("--env") + 1] == f"HELIX_KNOWLEDGE_CONFIG={config}"
    assert registration[registration.index("--include") + 1] == ",".join(EXPOSED_TOOLS)
    assert registration[registration.index("--connect-timeout") + 1] == "30"
    assert registration[registration.index("--timeout") + 1] == "60"
    assert calls[1][0] == [str(openclaw), "mcp", "reload"]
    assert calls[1][1]["timeout"] == 60
    assert all(call[1]["capture_output"] is True for call in calls)
    assert result.probed is True
    assert result.reloaded is True
    assert result.registration_output == "ok"


def test_install_openclaw_can_skip_probe_and_reload(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    openclaw = _executable(tmp_path / "openclaw")
    server = _executable(tmp_path / "server")
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="saved", stderr="")

    result = install_openclaw_server(
        config_path=config,
        workspace=workspace,
        openclaw_command=openclaw,
        server_command=server,
        probe=False,
        reload=False,
        runner=runner,
    )

    assert len(calls) == 1
    assert "--no-probe" in calls[0]
    assert result.probed is False
    assert result.reloaded is False


def test_reload_openclaw_discards_cached_mcp_runtimes(tmp_path: Path) -> None:
    openclaw = _executable(tmp_path / "openclaw")
    calls: list[tuple[list[str], int]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        return subprocess.CompletedProcess(command, 0, stdout="reloaded", stderr="")

    reload_openclaw(openclaw, runner=runner)

    assert calls == [([str(openclaw), "mcp", "reload"], 90)]


def test_install_openclaw_replaces_an_existing_server_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    openclaw = _executable(tmp_path / "openclaw")
    server = _executable(tmp_path / "helix-mcp-knowledge-server")
    calls: list[list[str]] = []
    previous = {"command": "/previous/server", "args": [], "cwd": "/previous"}

    def runner(command, **_kwargs):
        rendered = [str(value) for value in command]
        calls.append(rendered)
        if rendered[1:3] == ["mcp", "add"]:
            return subprocess.CompletedProcess(rendered, 1, stdout="", stderr="already exists")
        if rendered[1:3] == ["mcp", "show"]:
            return subprocess.CompletedProcess(rendered, 0, stdout=json.dumps(previous), stderr="")
        return subprocess.CompletedProcess(rendered, 0, stdout="ok", stderr="")

    result = install_openclaw_server(
        config_path=config,
        workspace=workspace,
        openclaw_command=openclaw,
        server_command=server,
        runner=runner,
    )

    assert [call[1:3] for call in calls] == [
        ["mcp", "add"],
        ["mcp", "show"],
        ["mcp", "set"],
        ["mcp", "reload"],
        ["mcp", "probe"],
    ]
    definition = json.loads(calls[2][4])
    assert definition == {
        "command": str(server),
        "args": [],
        "cwd": str(workspace),
        "env": {"HELIX_KNOWLEDGE_CONFIG": str(config)},
        "connectionTimeoutMs": 30000,
        "requestTimeoutMs": 60000,
        "toolFilter": {"include": list(EXPOSED_TOOLS)},
    }
    assert result.probed is True
    assert result.reloaded is True


def test_install_openclaw_restores_existing_server_after_probe_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    openclaw = _executable(tmp_path / "openclaw")
    server = _executable(tmp_path / "helix-mcp-knowledge-server")
    previous = {"command": "/previous/server", "args": [], "cwd": "/previous"}
    definitions: list[dict[str, object]] = []

    def runner(command, **_kwargs):
        rendered = [str(value) for value in command]
        if rendered[1:3] == ["mcp", "add"]:
            return subprocess.CompletedProcess(rendered, 1, stdout="", stderr="already exists")
        if rendered[1:3] == ["mcp", "show"]:
            return subprocess.CompletedProcess(rendered, 0, stdout=json.dumps(previous), stderr="")
        if rendered[1:3] == ["mcp", "set"]:
            definitions.append(json.loads(rendered[4]))
        if rendered[1:3] == ["mcp", "probe"]:
            return subprocess.CompletedProcess(rendered, 7, stdout="", stderr="probe failed")
        return subprocess.CompletedProcess(rendered, 0, stdout="ok", stderr="")

    with pytest.raises(KnowledgeError, match="probe failed; rollback succeeded"):
        install_openclaw_server(
            config_path=config,
            workspace=workspace,
            openclaw_command=openclaw,
            server_command=server,
            runner=runner,
        )

    assert definitions[-1] == previous


def test_install_openclaw_finds_server_beside_symlinked_venv_python(
    tmp_path: Path, monkeypatch
) -> None:
    if os.name == "nt":
        pytest.skip("Windows virtual environments use executable launchers instead of symlinks")
    workspace = tmp_path / "workspace"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    openclaw = _executable(tmp_path / "openclaw")
    venv_bin = tmp_path / "venv/bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.symlink_to(Path("/usr/bin/python3"))
    server = _executable(venv_bin / "helix-mcp-knowledge-server")
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="saved", stderr="")

    monkeypatch.setattr(openclaw_module.sys, "executable", str(python))
    result = install_openclaw_server(
        config_path=config,
        workspace=workspace,
        openclaw_command=openclaw,
        probe=False,
        reload=False,
        runner=runner,
    )

    assert result.server_command == server
    assert calls[0][calls[0].index("--command") + 1] == str(server)


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launch contract")
def test_install_openclaw_wraps_native_batch_command(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    openclaw = _executable(tmp_path / "openclaw.cmd")
    _executable(openclaw.with_suffix(".ps1"))
    server = _executable(tmp_path / "helix-mcp-knowledge-server.cmd")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="saved", stderr="")

    install_openclaw_server(
        config_path=config,
        workspace=workspace,
        openclaw_command=openclaw,
        server_command=server,
        runner=runner,
    )

    assert len(calls) == 2
    assert calls[0][0][1:7] == [
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    ]
    assert Path(calls[0][0][7]) == openclaw.with_suffix(".ps1")
    registration = calls[0][0][8:]
    assert registration[:3] == ["mcp", "add", "helix_knowledge"]
    command = Path(registration[registration.index("--command") + 1])
    assert command == Path(os.environ["COMSPEC"]).resolve()
    arguments = [
        registration[index + 1] for index, value in enumerate(registration) if value == "--arg"
    ]
    assert arguments == ["/d", "/s", "/c", str(server)]
    assert calls[1][0][8:] == ["mcp", "reload"]
    assert all(kwargs["shell"] is False for _, kwargs in calls)


@pytest.mark.skipif(os.name != "nt", reason="Windows npm launch contract")
def test_install_openclaw_invokes_npm_module_without_a_shell(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace & design"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    runtime = tmp_path / "npm runtime"
    runtime.mkdir()
    openclaw = _executable(runtime / "openclaw.cmd")
    module = runtime / "node_modules/openclaw/openclaw.mjs"
    module.parent.mkdir(parents=True)
    module.write_text("placeholder", encoding="utf-8")
    node = _executable(runtime / "node.exe")
    server = _executable(runtime / "helix-mcp-knowledge-server.cmd")
    calls: list[tuple[list[str], dict[str, object]]] = []

    real_which = openclaw_module.shutil.which

    def fake_which(command: str) -> str | None:
        if command.casefold() in {"node", "node.exe"}:
            return str(node)
        return real_which(command)

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="saved", stderr="")

    monkeypatch.setattr(openclaw_module.shutil, "which", fake_which)
    install_openclaw_server(
        config_path=config,
        workspace=workspace,
        openclaw_command=openclaw,
        server_command=server,
        runner=runner,
    )

    assert calls[0][0][:2] == [str(node.resolve()), str(module.resolve())]
    assert calls[0][0][2:5] == ["mcp", "add", "helix_knowledge"]
    assert calls[1][0] == [str(node.resolve()), str(module.resolve()), "mcp", "reload"]
    assert all(kwargs["shell"] is False for _, kwargs in calls)


@pytest.mark.skipif(os.name != "nt", reason="Windows batch execution contract")
def test_install_openclaw_executes_native_batch_command(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace with spaces"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    runtime = tmp_path / "OpenClaw Runtime"
    runtime.mkdir()
    calls_log = runtime / "calls.txt"
    openclaw = runtime / "openclaw.cmd"
    openclaw.write_text(
        f'@echo off\n>>"{calls_log}" echo %*\nexit /b 0\n',
        encoding="utf-8",
    )
    _powershell_log_shim(openclaw, calls_log)
    server = _executable(runtime / "helix-mcp-knowledge-server.cmd")

    result = install_openclaw_server(
        config_path=config,
        workspace=workspace,
        openclaw_command=openclaw,
        server_command=server,
    )

    calls = calls_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert calls[0].startswith("mcp add helix_knowledge")
    assert str(workspace) in calls[0]
    assert str(server) in calls[0]
    assert "--arg /d --arg /s --arg /c" in calls[0]
    assert calls[1] == "mcp reload"
    assert result.reloaded is True


@pytest.mark.skipif(os.name != "nt", reason="Windows batch execution contract")
def test_install_openclaw_treats_windows_metacharacters_as_argument_data(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace & design ^ review"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    runtime = tmp_path / "OpenClaw & Runtime"
    runtime.mkdir()
    calls_log = runtime / "calls.txt"
    openclaw = runtime / "openclaw.cmd"
    openclaw.write_text(
        f'@echo off\n>>"{calls_log}" echo %*\nexit /b 0\n',
        encoding="utf-8",
    )
    _powershell_log_shim(openclaw, calls_log)
    server = _executable(runtime / "helix-mcp-knowledge-server.cmd")

    install_openclaw_server(
        config_path=config,
        workspace=workspace,
        openclaw_command=openclaw,
        server_command=server,
        probe=False,
        reload=False,
    )

    call = calls_log.read_text(encoding="utf-8").strip()
    assert str(workspace) in call
    assert str(server) in call


def test_install_openclaw_reports_command_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config = workspace / "config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("placeholder", encoding="utf-8")
    openclaw = _executable(tmp_path / "openclaw")
    server = _executable(tmp_path / "server")

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="probe failed")

    with pytest.raises(KnowledgeError, match="probe failed"):
        install_openclaw_server(
            config_path=config,
            workspace=workspace,
            openclaw_command=openclaw,
            server_command=server,
            runner=runner,
        )

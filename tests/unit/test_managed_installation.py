from __future__ import annotations

import json
import os
from pathlib import Path

from helix_mcp_knowledge.managed_installation import (
    activate_managed_installation,
    load_managed_installation,
    stable_dashboard_launcher_path,
    stable_launcher_path,
    supports_transactional_updates,
)


def test_managed_installation_creates_stable_self_configuring_launcher(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace with spaces"
    config = workspace / "config/config.yaml"
    executable_dir = workspace / "runtime/1.9.0/venv" / ("Scripts" if os.name == "nt" else "bin")
    server = executable_dir / (
        "helix-mcp-knowledge-server.exe" if os.name == "nt" else "helix-mcp-knowledge-server"
    )
    config.parent.mkdir(parents=True)
    server.parent.mkdir(parents=True)
    config.write_text("schema_version: 1\n", encoding="utf-8")
    server.write_text("server", encoding="utf-8")
    python = server.parent / ("python.exe" if os.name == "nt" else "python")
    python.write_text("python", encoding="utf-8")

    installation = activate_managed_installation(
        workspace=workspace,
        version="1.9.0",
        server_command=server,
        config_path=config,
        client="standalone",
    )

    assert installation.launcher == stable_launcher_path(workspace)
    assert installation.dashboard_launcher == stable_dashboard_launcher_path(workspace)
    assert installation.launcher.is_file()
    assert installation.dashboard_launcher.is_file()
    content = installation.launcher.read_text(encoding="utf-8")
    assert str(server) in content
    assert str(config) in content
    if os.name != "nt":
        assert content.startswith("#!/bin/sh\n")
        assert os.access(installation.launcher, os.X_OK)
        dashboard_content = installation.dashboard_launcher.read_text(encoding="utf-8")
        assert "helix_mcp_knowledge.dashboard_host" in dashboard_content
        assert str(server.parent / "python") in dashboard_content
    loaded = load_managed_installation(workspace)
    assert loaded == installation
    assert supports_transactional_updates(workspace, installation) is True
    python.unlink()
    assert supports_transactional_updates(workspace, installation) is False


def test_managed_installation_switches_launcher_without_changing_its_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config = workspace / "config/config.yaml"
    executable_dir = "Scripts" if os.name == "nt" else "bin"
    server_name = (
        "helix-mcp-knowledge-server.exe" if os.name == "nt" else "helix-mcp-knowledge-server"
    )
    old_server = workspace / "runtime/1.8.0/venv" / executable_dir / server_name
    new_server = workspace / "runtime/1.9.0/venv" / executable_dir / server_name
    config.parent.mkdir(parents=True)
    old_server.parent.mkdir(parents=True)
    new_server.parent.mkdir(parents=True)
    config.write_text("schema_version: 1\n", encoding="utf-8")
    old_server.write_text("old", encoding="utf-8")
    new_server.write_text("new", encoding="utf-8")
    (old_server.parent / ("python.exe" if os.name == "nt" else "python")).write_text(
        "python", encoding="utf-8"
    )
    (new_server.parent / ("python.exe" if os.name == "nt" else "python")).write_text(
        "python", encoding="utf-8"
    )
    first = activate_managed_installation(
        workspace=workspace,
        version="1.8.0",
        server_command=old_server,
        config_path=config,
        client="standalone",
    )

    second = activate_managed_installation(
        workspace=workspace,
        version="1.9.0",
        server_command=new_server,
        config_path=config,
        client="standalone",
    )

    assert first.launcher == second.launcher
    assert first.dashboard_launcher == second.dashboard_launcher
    assert str(new_server) in second.launcher.read_text(encoding="utf-8")
    assert str(old_server) not in second.launcher.read_text(encoding="utf-8")
    assert str(new_server.parent / ("python.exe" if os.name == "nt" else "python")) in (
        second.dashboard_launcher.read_text(encoding="utf-8")
    )
    assert load_managed_installation(workspace).active_version == "1.9.0"  # type: ignore[union-attr]


def test_loads_schema_one_metadata_with_dashboard_defaults(tmp_path: Path) -> None:
    workspace = tmp_path / "legacy"
    config = workspace / "config/config.yaml"
    launcher = stable_launcher_path(workspace)
    metadata = workspace / "runtime/installation.json"
    config.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    metadata.parent.mkdir(parents=True)
    config.write_text("schema_version: 1\n", encoding="utf-8")
    launcher.write_text("legacy", encoding="utf-8")
    metadata.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_version": "1.13.2",
                "client": "standalone",
                "launcher": str(launcher),
                "config_path": str(config),
            }
        ),
        encoding="utf-8",
    )

    loaded = load_managed_installation(workspace)

    assert loaded is not None
    assert loaded.schema_version == 2
    assert loaded.dashboard_port == 8765
    assert loaded.dashboard_launcher == stable_dashboard_launcher_path(workspace)
